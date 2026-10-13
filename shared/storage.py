"""日报持久化（SQLite）。

包含：
  - daily_reports 表：日报主数据
  - reflections 表：问题与思考 + 明日计划（含 AI 草稿 / 用户输入标记）
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from shared.logger import get_logger

logger = get_logger("daily_report.storage")


class ReportStorage:
    """日报存储。"""

    def __init__(self, db_path: str | Path = "data/daily_reports.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            # 日报主表
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

            # 反思表（问题与思考 + 明日计划）
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reflections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_date TEXT NOT NULL UNIQUE,
                    problems_json TEXT,
                    next_plan_json TEXT,
                    ai_problems_json TEXT,
                    ai_next_plan_json TEXT,
                    source TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_reflection_date ON reflections(report_date)"
            )
            conn.commit()

    # ---------- 日报主表 ----------

    def save(
        self,
        report_date: date,
        team_name: str,
        markdown: str,
        html: str,
        generated_at: datetime,
        payload: dict | None = None,
    ) -> int:
        """保存日报，返回 id。同一天重复保存会覆盖。"""
        payload_json = json.dumps(payload, ensure_ascii=False) if payload else None
        with sqlite3.connect(self.db_path) as conn:
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

    def get_payload_by_date(self, target_date: date) -> dict | None:
        """读某天的 payload。没有返回 None。"""
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

        msgs = len(payload.get("feishu_messages", []) or [])
        # 任务数：从 sources.lark.count 或 members[].tasks 里算
        tasks = 0
        try:
            tasks = int((payload.get("sources") or {}).get("lark", {}).get("count", 0) or 0)
        except Exception:  # noqa: BLE001
            for m in payload.get("members", []):
                tasks += len(m.get("tasks", []) or [])

        if commits == 0 and files == 0 and msgs == 0:
            return None

        return {
            "date": yest.isoformat(),
            "commits": commits,
            "files": files,
            "additions": additions,
            "deletions": deletions,
            "msgs": msgs,
            "tasks": tasks,
        }

    def count_reports(self) -> int:
        """统计日报总条数（用于计算期数）。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute("SELECT COUNT(*) FROM daily_reports").fetchone()
                return int(row[0]) if row else 0
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"统计日报条数失败: {exc}")
            return 0

    def get_recent_stats(self, days: int = 7, end_date: date | None = None) -> list[dict]:
        """读最近 N 天的统计，按日期升序。"""
        end_date = end_date or date.today()
        results = []
        for i in range(days):
            d = end_date - timedelta(days=i)
            payload = self.get_payload_by_date(d)
            if not payload:
                continue
            commits = additions = deletions = 0
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
        """取最新一条日报。"""
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

    # ---------- 反思表（问题与思考 + 明日计划） ----------

    def save_reflection(
        self,
        report_date: date,
        problems: list[dict] | None,
        next_plan: list[dict] | None,
        ai_problems: list[dict] | None = None,
        ai_next_plan: list[dict] | None = None,
        source: str = "ai",
    ) -> None:
        """保存当天的反思。同一天覆盖。

        Args:
            problems: 最终版的问题（用户输入 or AI 草稿）
            next_plan: 最终版的计划
            ai_problems: AI 原始草稿（保留供对比）
            ai_next_plan: AI 原始计划
            source: "ai" / "manual" / "hybrid"
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM reflections WHERE report_date = ?",
                (report_date.isoformat(),),
            )
            conn.execute(
                """
                INSERT INTO reflections
                    (report_date, problems_json, next_plan_json,
                     ai_problems_json, ai_next_plan_json,
                     source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_date.isoformat(),
                    json.dumps(problems or [], ensure_ascii=False),
                    json.dumps(next_plan or [], ensure_ascii=False),
                    json.dumps(ai_problems or [], ensure_ascii=False),
                    json.dumps(ai_next_plan or [], ensure_ascii=False),
                    source,
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()
        logger.info(f"反思已保存: {report_date} source={source}")

    def get_reflection_by_date(self, target_date: date) -> dict | None:
        """读某天的反思。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT problems_json, next_plan_json,
                           ai_problems_json, ai_next_plan_json,
                           source, created_at
                    FROM reflections
                    WHERE report_date = ?
                    """,
                    (target_date.isoformat(),),
                ).fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"读取反思失败: {exc}")
            return None

        if not row:
            return None

        def _load(s):
            if not s:
                return []
            try:
                return json.loads(s)
            except Exception:  # noqa: BLE001
                return []

        return {
            "report_date": target_date.isoformat(),
            "problems": _load(row[0]),
            "next_plan": _load(row[1]),
            "ai_problems": _load(row[2]),
            "ai_next_plan": _load(row[3]),
            "source": row[4] or "ai",
            "created_at": row[5] or "",
        }

    def get_recent_reflections(
        self,
        days: int = 7,
        end_date: date | None = None,
        only_user_written: bool = True,
    ) -> list[dict]:
        """读最近 N 天的反思，按日期升序。

        Args:
            only_user_written: True 表示只返回 source 为 manual/hybrid 的
                （即用户真正写过的），用于给 LLM 学习风格。
        """
        end_date = end_date or date.today()
        start_date = end_date - timedelta(days=days - 1)

        try:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT report_date, problems_json, next_plan_json,
                           ai_problems_json, ai_next_plan_json,
                           source, created_at
                    FROM reflections
                    WHERE report_date >= ? AND report_date <= ?
                    ORDER BY report_date ASC
                    """,
                    (start_date.isoformat(), end_date.isoformat()),
                ).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"读最近反思失败: {exc}")
            return []

        def _load(s):
            if not s:
                return []
            try:
                return json.loads(s)
            except Exception:  # noqa: BLE001
                return []

        results = []
        for row in rows:
            source = row[5] or "ai"
            if only_user_written and source not in {"manual", "hybrid"}:
                continue
            results.append({
                "report_date": row[0],
                "problems": _load(row[1]),
                "next_plan": _load(row[2]),
                "ai_problems": _load(row[3]),
                "ai_next_plan": _load(row[4]),
                "source": source,
                "created_at": row[6] or "",
            })
        return results