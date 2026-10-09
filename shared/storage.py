"""SQLite 日报历史存储。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


@dataclass
class StoredReport:
    id: int
    report_date: date
    team_name: str
    markdown: str
    html: str
    generated_at: datetime
    payload: dict[str, Any] | None = None


class ReportStorage:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_date TEXT NOT NULL,
                    team_name TEXT NOT NULL,
                    markdown TEXT NOT NULL,
                    html TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    UNIQUE(report_date, team_name)
                )
                """
            )
            cols = [r[1] for r in conn.execute("PRAGMA table_info(daily_reports)").fetchall()]
            if "payload_json" not in cols:
                conn.execute("ALTER TABLE daily_reports ADD COLUMN payload_json TEXT")
            conn.commit()

    def save(
        self,
        report_date: date,
        team_name: str,
        markdown: str,
        html: str,
        generated_at: datetime | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        generated_at = generated_at or datetime.now()
        payload_json = json.dumps(payload, ensure_ascii=False) if payload else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_reports (
                    report_date, team_name, markdown, html, generated_at, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(report_date, team_name) DO UPDATE SET
                    markdown=excluded.markdown,
                    html=excluded.html,
                    generated_at=excluded.generated_at,
                    payload_json=excluded.payload_json
                """,
                (
                    report_date.isoformat(),
                    team_name,
                    markdown,
                    html,
                    generated_at.isoformat(),
                    payload_json,
                ),
            )
            conn.commit()
            row = conn.execute(
                """
                SELECT id FROM daily_reports
                WHERE report_date = ? AND team_name = ?
                """,
                (report_date.isoformat(), team_name),
            ).fetchone()
            return int(row["id"]) if row else 0

    def get(self, report_date: date, team_name: str) -> StoredReport | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, report_date, team_name, markdown, html, generated_at, payload_json
                FROM daily_reports
                WHERE report_date = ? AND team_name = ?
                """,
                (report_date.isoformat(), team_name),
            ).fetchone()
        if row is None:
            return None
        return self._from_row(row)

    def get_by_id(self, report_id: int) -> StoredReport | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, report_date, team_name, markdown, html, generated_at, payload_json
                FROM daily_reports
                WHERE id = ?
                """,
                (report_id,),
            ).fetchone()
        if row is None:
            return None
        return self._from_row(row)

    def list_recent(self, limit: int = 30) -> list[StoredReport]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, report_date, team_name, markdown, html, generated_at, payload_json
                FROM daily_reports
                ORDER BY report_date DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._from_row(r) for r in rows]

    def _from_row(self, row: sqlite3.Row) -> StoredReport:
        raw = None
        try:
            raw = row["payload_json"]
        except (KeyError, IndexError):
            raw = None
        payload = None
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None
        return StoredReport(
            id=row["id"],
            report_date=date.fromisoformat(row["report_date"]),
            team_name=row["team_name"],
            markdown=row["markdown"],
            html=row["html"],
            generated_at=datetime.fromisoformat(row["generated_at"]),
            payload=payload,
        )
