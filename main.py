"""智能日报生成器 — 主编排入口。

用法:
  python main.py                    # 完整流程：采集 → 生成 → 推送
  python main.py --auto             # 一键自动：读 GitHub → 生成 → 发到配置的收件人
  python main.py --check            # 健康检查
  python main.py --dry-run          # 采集+生成，不推送
  python main.py --github-only      # 只采集 GitHub 并发送日报，跳过飞书任务
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from collector import github as github_collector
from collector import github_events as github_events_collector
from collector import lark_task as lark_task_collector
from generator.formatter import generate
from notifier import email as email_notifier
from shared.config import AppConfig, load_config
from shared.errors import ConfigError
from shared.logger import get_logger, setup_logger
from shared.models import CollectResult, CommitRecord, EventRecord, MemberReport, TaskRecord
from shared.storage import ReportStorage

logger = get_logger("daily_report.main")

FALLBACK_RECIPIENTS: list[str] = ["2327929235@qq.com"]

EMAIL_MAX_RETRIES = 2
EMAIL_RETRY_INTERVAL = 5


def _local_tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Shanghai")
    except Exception:  # noqa: BLE001
        return timezone(timedelta(hours=8), name="Asia/Shanghai")


LOCAL_TZ = _local_tz()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="智能日报生成器")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--check", action="store_true", help="健康检查模式")
    parser.add_argument("--dry-run", action="store_true", help="不推送，仅采集生成")
    parser.add_argument("--force", action="store_true", help="忽略非工作日限制")
    parser.add_argument(
        "--github-only",
        action="store_true",
        help="只采集 GitHub 并发送日报，跳过飞书任务",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="一键自动：只读 GitHub、忽略工作日限制，生成并发送到配置的收件人",
    )
    parser.add_argument(
        "--to",
        action="append",
        default=None,
        help="临时指定收件人邮箱，可重复，如 --to a@x.com --to b@y.com",
    )
    return parser.parse_args(argv)


def collection_window(
    now: datetime | None = None, lookback_days: int = 1
) -> tuple[datetime, datetime]:
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
    cfg: AppConfig,
    since: datetime,
    until: datetime,
    *,
    github_only: bool = False,
) -> tuple[CollectResult[CommitRecord], CollectResult[TaskRecord]]:
    github_token = None
    try:
        github_token = cfg.github_token()
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

    if github_only:
        logger.info("github-only 模式，跳过飞书任务采集")
        tasks = CollectResult(success=True, records=[], error_message="")
        return commits, tasks

    app_id = app_secret = None
    try:
        app_id, app_secret = cfg.lark_credentials()
    except ConfigError as exc:
        logger.error(str(exc))

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


def run_collect_events(
    cfg: AppConfig,
    since: datetime,
    until: datetime,
) -> CollectResult[EventRecord]:
    """采集成员 GitHub 公开活动。失败不影响主流程。"""
    usernames = [m.github for m in cfg.members if m.github]
    if not usernames:
        return CollectResult(success=True, records=[])
    try:
        token = cfg.github_token()
    except ConfigError:
        token = None
    try:
        result = github_events_collector.collect(
            usernames, since, until, token=token,
        )
        logger.info(f"GitHub 活动采集完成: success={result.success}, count={len(result.records)}")
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"GitHub 活动采集跳过: {exc}")
        return CollectResult(success=False, records=[], error_message=str(exc))


def run_check(cfg: AppConfig) -> int:
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
        user, _ = cfg.smtp_credentials()
        logger.info(f"[check] SMTP sender (发件人): {user}")
    except ConfigError:
        pass
    logger.info(f"[check] recipients (收件人): {_resolve_recipients(cfg)}")

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
        if app_id and app_secret:
            with httpx.Client(base_url="https://open.feishu.cn/open-apis", timeout=15.0) as client:
                token = LarkTokenProvider(app_id, app_secret, client=client).get_token()
                logger.info(f"[check] Lark token OK (len={len(token)})")
        else:
            logger.info("[check] Lark credentials 未配置，跳过（仅 GitHub 模式无需）")
    except ConfigError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[check] Lark token 跳过: {exc}")

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
        logger.info(f"[check] SMTP OK ({host}:{port})，发件人={user}")
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
    events: dict[str, Any] = field(default_factory=dict)
    sender: str = ""
    recipients: list[str] = field(default_factory=list)
    email_sent: bool = False


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    try:
        return value.isoformat()
    except Exception:  # noqa: BLE001
        return str(value)


def _commit_payload(c: CommitRecord) -> dict[str, Any]:
    file_details = []
    for fd in (getattr(c, "file_details", None) or []):
        file_details.append(
            {
                "filename": getattr(fd, "filename", ""),
                "status": getattr(fd, "status", "modified"),
                "additions": int(getattr(fd, "additions", 0) or 0),
                "deletions": int(getattr(fd, "deletions", 0) or 0),
            }
        )
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
        "file_details": file_details,
        "url": c.url,
    }


def _event_payload(e: EventRecord) -> dict[str, Any]:
    return {
        "actor": e.actor,
        "event_type": e.event_type,
        "repo": e.repo,
        "summary": e.summary,
        "timestamp": _iso(e.timestamp),
        "url": e.url,
    }


def _task_payload(t: TaskRecord) -> dict[str, Any]:
    return {
        "assignee": t.assignee,
        "title": t.title,
        "status_from": t.status_from,
        "status_to": t.status_to,
        "updated_at": _iso(t.updated_at),
    }


def _resolve_sender(cfg: AppConfig) -> str:
    sender = (
        cfg.email.get("from_addr")
        or cfg.email.get("sender")
        or ""
    )
    if sender:
        return sender.strip()
    try:
        user, _ = cfg.smtp_credentials()
        return user
    except ConfigError:
        return ""


def _resolve_recipients(
    cfg: AppConfig,
    override: list[str] | None = None,
) -> list[str]:
    candidates: list[str] = []
    if override:
        candidates.extend(override)
    else:
        candidates.extend(list(cfg.email.get("recipients") or []))
        if not candidates:
            candidates.extend(FALLBACK_RECIPIENTS)

    recipients: list[str] = []
    for addr in candidates:
        addr = (addr or "").strip()
        if addr and addr not in recipients:
            recipients.append(addr)
    return recipients


def _send_email_with_retry(
    report,
    recipients: list[str],
    cfg: AppConfig,
    *,
    max_retries: int = EMAIL_MAX_RETRIES,
    interval: int = EMAIL_RETRY_INTERVAL,
) -> bool:
    user, password = cfg.smtp_credentials()
    from_addr = _resolve_sender(cfg) or user

    last_err: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            ok = email_notifier.send(
                report,
                recipients,
                smtp_host=cfg.email["smtp_host"],
                smtp_port=int(cfg.email.get("smtp_port") or 465),
                use_ssl=bool(cfg.email.get("use_ssl", True)),
                username=user,
                password=password,
                from_addr=from_addr,
            )
            if ok:
                if attempt > 1:
                    logger.info(f"邮件第 {attempt} 次尝试发送成功")
                return True
            logger.warning(f"邮件发送返回失败（第 {attempt}/{max_retries} 次）")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            logger.warning(f"邮件发送异常（第 {attempt}/{max_retries} 次）: {exc}")
        if attempt < max_retries:
            _time.sleep(interval)
    if last_err:
        logger.error(f"邮件发送最终失败: {last_err}")
    return False


def execute_pipeline(
    cfg: AppConfig,
    *,
    dry_run: bool = False,
    force: bool = False,
    lookback_days: int = 1,
    github_only: bool = False,
    recipients_override: list[str] | None = None,
) -> PipelineOutcome:
    started = datetime.now(LOCAL_TZ)
    logger.info(f"日报任务开始: {started.isoformat()}")

    if not force and not is_workday(cfg):
        logger.info("今日为非工作日，跳过日报生成")
        return PipelineOutcome(code=0, skipped=True, message="今日为非工作日，已跳过")

    since, until = collection_window(started, lookback_days=lookback_days)
    commits, tasks = run_collect(cfg, since, until, github_only=github_only)
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

    # 采集 GitHub 公开活动
    events_result = run_collect_events(cfg, since, until)
    events_records = events_result.records
    events_info = {
        "success": events_result.success,
        "count": len(events_records),
        "error": events_result.error_message or "",
    }

    members = aggregate_members(cfg, commits, tasks)
    report = generate(
        members,
        until.date(),
        cfg.team_name,
        events=events_records,
        generated_at=started,
    )
    payload = {
        "sources": {"github": github_info, "lark": lark_info, "events": events_info},
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
        "events": [_event_payload(e) for e in events_records],
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

    sender = _resolve_sender(cfg)
    recipients = _resolve_recipients(cfg, override=recipients_override)

    if dry_run:
        logger.info("dry-run 模式，跳过推送")
        logger.info(f"发件人: {sender}")
        logger.info(f"收件人: {recipients}")
        logger.info(f"日报任务结束: {(datetime.now(LOCAL_TZ) - started)}")
        return PipelineOutcome(
            code=0,
            report_id=report_id,
            message="日报已生成（dry-run，未发信）",
            github=github_info,
            lark=lark_info,
            events=events_info,
            sender=sender,
            recipients=recipients,
        )

    email_ok = False
    try:
        logger.info(f"准备发送日报邮件 | 发件人: {sender} | 收件人: {recipients}")
        email_ok = _send_email_with_retry(report, recipients, cfg)
        logger.info(f"邮件发送{'成功' if email_ok else '失败'}: {sender} -> {recipients}")
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
        events=events_info,
        sender=sender,
        recipients=recipients,
        email_sent=email_ok,
    )


def run_pipeline(
    cfg: AppConfig,
    *,
    dry_run: bool = False,
    force: bool = False,
    lookback_days: int = 1,
    github_only: bool = False,
    recipients_override: list[str] | None = None,
) -> int:
    return execute_pipeline(
        cfg,
        dry_run=dry_run,
        force=force,
        lookback_days=lookback_days,
        github_only=github_only,
        recipients_override=recipients_override,
    ).code


def run_auto(
    config_path: str = "config.yaml",
    recipients: list[str] | None = None,
) -> PipelineOutcome:
    setup_logger()
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    cfg = load_config(config_path)
    return execute_pipeline(
        cfg,
        dry_run=False,
        force=True,
        github_only=True,
        lookback_days=1,
        recipients_override=recipients,
    )


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

    if args.auto:
        outcome = execute_pipeline(
            cfg,
            dry_run=False,
            force=True,
            github_only=True,
            lookback_days=1,
            recipients_override=args.to,
        )
        return outcome.code

    return run_pipeline(
        cfg,
        dry_run=args.dry_run,
        force=args.force,
        github_only=args.github_only,
        recipients_override=args.to,
    )


if __name__ == "__main__":
    sys.exit(main())