"""智能日报生成器 — 主编排入口。

用法:
  python main.py            # 完整流程：采集 → 生成 → 推送
  python main.py --check    # 健康检查
  python main.py --dry-run  # 采集+生成，不推送
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from collector import github as github_collector
from collector import lark_task as lark_task_collector
from generator.formatter import generate
from notifier import email as email_notifier
from shared.config import AppConfig, load_config
from shared.errors import ConfigError
from shared.logger import get_logger, setup_logger
from shared.models import CollectResult, CommitRecord, MemberReport, TaskRecord
from shared.storage import ReportStorage

logger = get_logger("daily_report.main")


def _local_tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Shanghai")
    except Exception:  # noqa: BLE001 — Windows 无 tzdata 时回退固定 UTC+8
        return timezone(timedelta(hours=8), name="Asia/Shanghai")


LOCAL_TZ = _local_tz()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="智能日报生成器")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--check", action="store_true", help="健康检查模式")
    parser.add_argument("--dry-run", action="store_true", help="不推送，仅采集生成")
    parser.add_argument("--force", action="store_true", help="忽略非工作日限制")
    return parser.parse_args(argv)


def collection_window(
    now: datetime | None = None, lookback_days: int = 1
) -> tuple[datetime, datetime]:
    """采集窗口：默认当天 00:00 至执行时刻；lookback_days>1 时向前扩大。"""
    now = now or datetime.now(LOCAL_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=LOCAL_TZ)
    days = max(1, min(int(lookback_days or 1), 30))
    start_day = now.date() - timedelta(days=days - 1)
    start = datetime.combine(start_day, time.min, tzinfo=now.tzinfo)
    return start, now


def is_workday(cfg: AppConfig, day: date | None = None) -> bool:
    day = day or datetime.now(LOCAL_TZ).date()
    schedule = cfg.schedule or {}
    if schedule.get("skip_weekends", True) and day.weekday() >= 5:
        return False
    holidays = set(schedule.get("holidays") or [])
    if day.isoformat() in holidays:
        return False
    return True


def aggregate_members(
    cfg: AppConfig,
    commits: CollectResult[CommitRecord],
    tasks: CollectResult[TaskRecord],
) -> list[MemberReport]:
    github_by_user: dict[str, list[CommitRecord]] = {}
    for c in commits.records:
        github_by_user.setdefault(c.author.lower(), []).append(c)

    tasks_by_user: dict[str, list[TaskRecord]] = {}
    for t in tasks.records:
        tasks_by_user.setdefault(t.assignee.lower(), []).append(t)

    def _collect_for(keys: set[str], bucket: dict) -> list:
        seen: set[int] = set()
        out: list = []
        for key in keys:
            for item in bucket.get(key, []):
                ident = id(item)
                if ident in seen:
                    continue
                seen.add(ident)
                out.append(item)
        return out

    members: list[MemberReport] = []
    for m in cfg.members:
        keys = m.identity_keys()
        members.append(
            MemberReport(
                name=m.name,
                github_username=m.github,
                commits=_collect_for(keys | {m.github.lower()}, github_by_user),
                tasks=_collect_for(keys, tasks_by_user),
                commits_failed=not commits.success,
                tasks_failed=not tasks.success,
            )
        )

    assigned: set[int] = {id(c) for m in members for c in m.commits}
    leftover: dict[str, list[CommitRecord]] = {}
    for rec in commits.records:
        if id(rec) in assigned:
            continue
        leftover.setdefault(rec.author or "unknown", []).append(rec)
    for author, recs in leftover.items():
        members.append(
            MemberReport(
                name=f"未映射 · {author}",
                github_username=author,
                commits=recs,
                tasks=[],
                commits_failed=not commits.success,
                tasks_failed=not tasks.success,
            )
        )
    return members


def run_collect(
    cfg: AppConfig, since: datetime, until: datetime
) -> tuple[CollectResult[CommitRecord], CollectResult[TaskRecord]]:
    github_token = None
    app_id = app_secret = None
    try:
        github_token = cfg.github_token()
    except ConfigError as exc:
        logger.error(str(exc))
    try:
        app_id, app_secret = cfg.lark_credentials()
    except ConfigError as exc:
        logger.error(str(exc))

    commits = github_collector.collect(
        cfg.github["repos"],
        since,
        until,
        token=github_token,
    )
    logger.info(
        f"GitHub 采集完成: success={commits.success}, count={len(commits.records)}",
    )

    if app_id and app_secret:
        tasklist_id = cfg.lark.get("tasklist_id") or cfg.lark.get("project_id") or ""
        tasks = lark_task_collector.collect(
            tasklist_id,
            since,
            until,
            app_id=app_id,
            app_secret=app_secret,
        )
    else:
        tasks = CollectResult(success=False, records=[], error_message="缺少飞书凭证")

    logger.info(f"飞书任务采集完成: success={tasks.success}, count={len(tasks.records)}")
    return commits, tasks


def run_check(cfg: AppConfig) -> int:
    """验证配置完整性，并探测外部 API 连通性。"""
    import httpx

    from collector.lark_task import LarkTokenProvider

    ok = True

    for label, fn in (
        ("GITHUB_TOKEN", cfg.github_token),
        ("LARK credentials", cfg.lark_credentials),
        ("SMTP credentials", cfg.smtp_credentials),
    ):
        try:
            fn()
            logger.info(f"[check] env {label} OK")
        except ConfigError as exc:
            ok = False
            logger.error(f"[check] env {label} FAIL: {exc}")

    storage = ReportStorage(cfg.storage["db_path"])
    logger.info(f"[check] SQLite OK: {storage.db_path}")
    logger.info(f"[check] members={len(cfg.members)}, repos={cfg.github['repos']}")

    try:
        token = cfg.github_token()
        with httpx.Client(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15.0,
        ) as client:
            resp = client.get("/user")
            resp.raise_for_status()
            login = resp.json().get("login", "?")
            logger.info(f"[check] GitHub API OK (user={login})")
            for repo in cfg.github.get("repos") or []:
                r = client.get(f"/repos/{repo}")
                if r.status_code == 200:
                    logger.info(f"[check] GitHub repo OK: {repo}")
                else:
                    ok = False
                    logger.error(f"[check] GitHub repo FAIL: {repo} ({r.status_code})")
    except ConfigError:
        pass
    except Exception as exc:  # noqa: BLE001
        ok = False
        logger.error(f"[check] GitHub API FAIL: {exc}")

    try:
        app_id, app_secret = cfg.lark_credentials()
        with httpx.Client(base_url="https://open.feishu.cn/open-apis", timeout=15.0) as client:
            token = LarkTokenProvider(app_id, app_secret, client=client).get_token()
            logger.info(f"[check] Lark token OK (len={len(token)})")
    except ConfigError:
        pass
    except Exception as exc:  # noqa: BLE001
        ok = False
        logger.error(f"[check] Lark token FAIL: {exc}")

    try:
        import smtplib

        user, password = cfg.smtp_credentials()
        host = cfg.email["smtp_host"]
        port = int(cfg.email.get("smtp_port") or 465)
        use_ssl = bool(cfg.email.get("use_ssl", True))
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=15) as server:
                server.login(user, password)
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.starttls()
                server.login(user, password)
        logger.info(f"[check] SMTP OK ({host}:{port})")
    except ConfigError:
        pass
    except Exception as exc:  # noqa: BLE001
        ok = False
        logger.error(f"[check] SMTP FAIL: {exc}")

    return 0 if ok else 1


@dataclass
class PipelineOutcome:
    code: int
    report_id: int | None = None
    skipped: bool = False
    message: str = ""
    github: dict[str, Any] = field(default_factory=dict)
    lark: dict[str, Any] = field(default_factory=dict)


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    try:
        return value.isoformat()
    except Exception:  # noqa: BLE001
        return str(value)


def _commit_payload(c: CommitRecord) -> dict[str, Any]:
    return {
        "author": c.author,
        "message": c.message,
        "timestamp": _iso(c.timestamp),
        "repo": c.repo,
        "additions": c.additions,
        "deletions": c.deletions,
        "files_changed": c.files_changed,
        "sha": c.sha,
        "files": list(c.files or []),
        "url": c.url,
    }


def _task_payload(t: TaskRecord) -> dict[str, Any]:
    return {
        "assignee": t.assignee,
        "title": t.title,
        "status_from": t.status_from,
        "status_to": t.status_to,
        "updated_at": _iso(t.updated_at),
    }


def execute_pipeline(
    cfg: AppConfig,
    *,
    dry_run: bool = False,
    force: bool = False,
    lookback_days: int = 1,
) -> PipelineOutcome:
    started = datetime.now(LOCAL_TZ)
    logger.info(f"日报任务开始: {started.isoformat()}")

    if not force and not is_workday(cfg):
        logger.info("今日为非工作日，跳过日报生成")
        return PipelineOutcome(code=0, skipped=True, message="今日为非工作日，已跳过")

    since, until = collection_window(started, lookback_days=lookback_days)
    commits, tasks = run_collect(cfg, since, until)
    github_info = {
        "success": commits.success,
        "count": len(commits.records),
        "error": commits.error_message or "",
        "since": _iso(since),
        "until": _iso(until),
    }
    lark_info = {
        "success": tasks.success,
        "count": len(tasks.records),
        "error": tasks.error_message or "",
    }

    if not (commits.success or tasks.success):
        logger.error("所有数据源不可用，不生成空日报")
        return PipelineOutcome(
            code=1,
            message="所有数据源不可用，未生成日报",
            github=github_info,
            lark=lark_info,
        )

    members = aggregate_members(cfg, commits, tasks)
    report = generate(members, until.date(), cfg.team_name, generated_at=started)
    payload = {
        "sources": {"github": github_info, "lark": lark_info},
        "members": [
            {
                "name": m.name,
                "github_username": m.github_username,
                "commits_failed": m.commits_failed,
                "tasks_failed": m.tasks_failed,
                "commits": [_commit_payload(c) for c in m.commits],
                "tasks": [_task_payload(t) for t in m.tasks],
            }
            for m in members
        ],
    }

    storage = ReportStorage(cfg.storage["db_path"])
    report_id = storage.save(
        report.date,
        report.team_name,
        report.markdown,
        report.html,
        report.generated_at,
        payload=payload,
    )
    logger.info(f"日报已写入 SQLite id={report_id}")

    if dry_run:
        logger.info("dry-run 模式，跳过推送")
        logger.info(f"日报任务结束: {(datetime.now(LOCAL_TZ) - started)}")
        return PipelineOutcome(
            code=0,
            report_id=report_id,
            message="日报已生成（dry-run，未发信）",
            github=github_info,
            lark=lark_info,
        )

    email_ok = False
    try:
        user, password = cfg.smtp_credentials()
        email_ok = email_notifier.send(
            report,
            cfg.email.get("recipients") or [],
            smtp_host=cfg.email["smtp_host"],
            smtp_port=int(cfg.email.get("smtp_port") or 465),
            use_ssl=bool(cfg.email.get("use_ssl", True)),
            username=user,
            password=password,
            from_addr=cfg.email.get("from_addr") or user,
        )
    except ConfigError as exc:
        logger.error(f"邮件推送跳过: {exc}")

    logger.info(f"推送结果: email={email_ok}")
    logger.info(f"日报任务结束: {(datetime.now(LOCAL_TZ) - started)}")
    return PipelineOutcome(
        code=0 if email_ok else 1,
        report_id=report_id,
        message="日报已生成" + ("并已发信" if email_ok else "，邮件未发送"),
        github=github_info,
        lark=lark_info,
    )


def run_pipeline(
    cfg: AppConfig,
    *,
    dry_run: bool = False,
    force: bool = False,
    lookback_days: int = 1,
) -> int:
    return execute_pipeline(
        cfg, dry_run=dry_run, force=force, lookback_days=lookback_days
    ).code


def main(argv: list[str] | None = None) -> int:
    setup_logger()
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    args = parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        logger.error(f"配置错误: {exc}")
        return 1

    if args.check:
        return run_check(cfg)
    return run_pipeline(cfg, dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
