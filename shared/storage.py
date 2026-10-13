"""日报持久化（SQLite）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from shared.logger import get_logger

logger = get_logger("daily_report.storage")


class ReportStorage:
    """日报存储。表结构：

    daily_reports(id, report_date, team_name, markdown, html, generated_at, payload_json)
    """

    def __init__(self, db_path: str | Path = "data/daily_reports.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_date TEXT NOT NULL,
                    team_name TEXT NOT NULL,
                    markdown TEXT NOT NULL,
                    html TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    payload_json TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_report_date ON daily_reports(report_date)"
            )
            conn.commit()

    # ---------- 写 ----------

    def save(
        self,
        report_date: date,
        team_name: str,
        markdown: str,
        html: str,
        generated_at: datetime,
        payload: dict | None = None,
    ) -> int:
        """保存日报，返回 id。同一天重复保存会**覆盖**（保留最新一条）。"""
        payload_json = json.dumps(payload, ensure_ascii=False) if payload else None
        with sqlite3.connect(self.db_path) as conn:
            # 如果当天已有，先删掉
            conn.execute(
                "DELETE FROM daily_reports WHERE report_date = ?",
                (report_date.isoformat(),),
            )
            cur = conn.execute(
                """
                INSERT INTO daily_reports
                    (report_date, team_name, markdown, html, generated_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
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
            return cur.lastrowid or 0

    # ---------- 读 ----------

    def get_payload_by_date(self, target_date: date) -> dict | None:
        """读某天的 payload（含 sources / members 等）。没有返回 None。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT payload_json FROM daily_reports
                    WHERE report_date = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (target_date.isoformat(),),
                ).fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"读取 {target_date} 日报失败: {exc}")
            return None

        if not row or not row[0]:
            return None
        try:
            return json.loads(row[0])
        except Exception:  # noqa: BLE001
            return None

    def get_yesterday_stats(self, today: date) -> dict | None:
        """读昨天的统计数据。没有返回 None。"""
        yest = today - timedelta(days=1)
        payload = self.get_payload_by_date(yest)
        if not payload:
            return None

        commits = 0
        files = 0
        additions = 0
        deletions = 0
        for m in payload.get("members", []):
            for c in m.get("commits", []):
                commits += 1
                files += int(c.get("files_changed", 0) or 0)
                additions += int(c.get("additions", 0) or 0)
                deletions += int(c.get("deletions", 0) or 0)

        # 群消息数
        msgs = len(payload.get("feishu_messages", []) or [])

        if commits == 0 and files == 0:
            # 没 commit 但可能有群消息
            if msgs == 0:
                return None

        return {
            "date": yest.isoformat(),
            "commits": commits,
            "files": files,
            "additions": additions,
            "deletions": deletions,
            "msgs": msgs,
        }

    def count_reports(self) -> int:
        """统计 SQLite 里一共有多少条日报（用于计算期数）。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM daily_reports"
                ).fetchone()
                return int(row[0]) if row else 0
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"统计日报条数失败: {exc}")
            return 0

    def get_recent_stats(self, days: int = 7, end_date: date | None = None) -> list[dict]:
        """读最近 N 天的统计（用于趋势分析）。返回列表，按日期升序。"""
        end_date = end_date or date.today()
        results = []
        for i in range(days):
            d = end_date - timedelta(days=i)
            payload = self.get_payload_by_date(d)
            if not payload:
                continue
            commits = 0
            additions = 0
            deletions = 0
            for m in payload.get("members", []):
                for c in m.get("commits", []):
                    commits += 1
                    additions += int(c.get("additions", 0) or 0)
                    deletions += int(c.get("deletions", 0) or 0)
            results.append({
                "date": d.isoformat(),
                "commits": commits,
                "additions": additions,
                "deletions": deletions,
                "net": additions - deletions,
            })
        results.sort(key=lambda x: x["date"])
        return results

    def get_latest_report(self) -> dict | None:
        """取最新一条日报的完整信息（含 markdown / html）。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT id, report_date, team_name, markdown, html, generated_at, payload_json
                    FROM daily_reports
                    ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"读取最新日报失败: {exc}")
            return None

        if not row:
            return None
        return {
            "id": row[0],
            "report_date": row[1],
            "team_name": row[2],
            "markdown": row[3],
            "html": row[4],
            "generated_at": row[5],
            "payload": json.loads(row[6]) if row[6] else None,
        }