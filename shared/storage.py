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
from shared.models import StoredReport

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

            # 邮件发送记录（快照已发送邮件的主题 / 收件人 / 正文）
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sent_emails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_id INTEGER,
                    report_date TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    recipients_json TEXT NOT NULL,
                    from_addr TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    markdown TEXT NOT NULL,
                    html TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'sent'
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sent_email_date ON sent_emails(report_date)"
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

    # ---------- 日报查询（API / 工作台） ----------

    @staticmethod
    def _row_to_stored(row) -> StoredReport:
        payload = None
        if row[6]:
            try:
                payload = json.loads(row[6])
            except Exception:  # noqa: BLE001
                payload = None
        return StoredReport(
            id=row[0],
            report_date=date.fromisoformat(row[1]),
            team_name=row[2],
            markdown=row[3],
            html=row[4],
            generated_at=datetime.fromisoformat(row[5]),
            payload=payload,
        )

    def get(self, report_date: date, team_name: str) -> StoredReport | None:
        """按日期 + 团队取最新一条存档。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT id, report_date, team_name, markdown, html, generated_at, payload_json
                    FROM daily_reports
                    WHERE report_date = ? AND team_name = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (report_date.isoformat(), team_name),
                ).fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"读取日报失败: {exc}")
            return None
        return self._row_to_stored(row) if row else None

    def list_recent(self, limit: int = 50) -> list[StoredReport]:
        """按 id 倒序取最近 N 条日报。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT id, report_date, team_name, markdown, html, generated_at, payload_json
                    FROM daily_reports
                    ORDER BY id DESC LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"读取日报列表失败: {exc}")
            return []
        return [self._row_to_stored(r) for r in rows]

    def get_by_id(self, report_id: int) -> StoredReport | None:
        """按 id 取一条日报。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT id, report_date, team_name, markdown, html, generated_at, payload_json
                    FROM daily_reports
                    WHERE id = ?
                    """,
                    (int(report_id),),
                ).fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"读取日报失败: {exc}")
            return None
        return self._row_to_stored(row) if row else None

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

    def list_reflections(self, limit: int = 30) -> list[dict]:
        """按日期倒序列出反思记录（问题与思考 + 明日计划）。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT report_date, problems_json, next_plan_json,
                           ai_problems_json, ai_next_plan_json,
                           source, created_at
                    FROM reflections ORDER BY report_date DESC LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"读反思列表失败: {exc}")
            return []

        def _load(s):
            if not s:
                return []
            try:
                return json.loads(s)
            except Exception:  # noqa: BLE001
                return []

        return [
            {
                "report_date": row[0],
                "problems": _load(row[1]),
                "next_plan": _load(row[2]),
                "ai_problems": _load(row[3]),
                "ai_next_plan": _load(row[4]),
                "source": row[5] or "ai",
                "created_at": row[6] or "",
            }
            for row in rows
        ]

    # ---------- 邮件发送记录（sent_emails） ----------

    @staticmethod
    def _email_row_to_dict(row, with_body: bool = False) -> dict:
        recipients = []
        if row[4]:
            try:
                recipients = json.loads(row[4])
            except Exception:  # noqa: BLE001
                recipients = []
        out = {
            "id": row[0],
            "report_id": row[1],
            "report_date": row[2],
            "subject": row[3],
            "recipients": recipients,
            "from_addr": row[5],
            "sent_at": row[6],
            "status": row[7],
        }
        if with_body:
            out["markdown"] = row[8]
            out["html"] = row[9]
        return out

    def save_email_log(
        self,
        *,
        report_id: int | None,
        report_date: str,
        subject: str,
        recipients: list[str],
        from_addr: str,
        markdown: str,
        html: str,
        sent_at: str | None = None,
        status: str = "sent",
    ) -> int:
        """记录一次已发送的邮件（主题 / 收件人 / 正文快照），返回 id。"""
        sent_at = sent_at or datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO sent_emails
                    (report_id, report_date, subject, recipients_json,
                     from_addr, sent_at, markdown, html, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    report_date,
                    subject,
                    json.dumps(recipients or [], ensure_ascii=False),
                    from_addr,
                    sent_at,
                    markdown,
                    html,
                    status,
                ),
            )
            conn.commit()
            return cur.lastrowid or 0

    def list_email_logs(
        self,
        limit: int = 100,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict]:
        """按 id 倒序取发送邮件记录（摘要，不含正文）。"""
        sql = (
            "SELECT id, report_id, report_date, subject, recipients_json, "
            "from_addr, sent_at, status FROM sent_emails"
        )
        conds: list[str] = []
        params: list[object] = []
        if date_from:
            conds.append("report_date >= ?")
            params.append(date_from)
        if date_to:
            conds.append("report_date <= ?")
            params.append(date_to)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        try:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(sql, params).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"读取邮件记录失败: {exc}")
            return []
        return [self._email_row_to_dict(r, with_body=False) for r in rows]

    def get_email_log(self, email_id: int) -> dict | None:
        """取一条邮件记录（含正文快照）。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT id, report_id, report_date, subject, recipients_json,
                           from_addr, sent_at, status, markdown, html
                    FROM sent_emails WHERE id = ?
                    """,
                    (int(email_id),),
                ).fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"读取邮件记录失败: {exc}")
            return None
        return self._email_row_to_dict(row, with_body=True) if row else None

    def latest_email_for_report(self, report_id: int) -> dict | None:
        """取某条日报最近一次发送的邮件记录（摘要）。"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT id, report_id, report_date, subject, recipients_json,
                           from_addr, sent_at, status
                    FROM sent_emails WHERE report_id = ? ORDER BY id DESC LIMIT 1
                    """,
                    (int(report_id),),
                ).fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"读取邮件记录失败: {exc}")
            return None
        return self._email_row_to_dict(row, with_body=False) if row else None