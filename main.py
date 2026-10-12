"""智能日报生成器 — 主编排入口。"""

from __future__ import annotations

import argparse
import sys
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from collector import feishu_message as feishu_message_collector
from collector import github as github_collector
from collector import github_events as github_events_collector
from collector import lark_task as lark_task_collector
from generator.formatter import generate
from generator.summarizer import (
    extract_action_items,        # ⭐ 新增
    extract_highlights,
    summarize,
    summarize_commits,           # ⭐ 新增
)
from notifier import email as email_notifier
from notifier import feishu as feishu_notifier
from shared.config import AppConfig, load_config
from shared.errors import ConfigError
from shared.logger import get_logger, setup_logger
from shared.models import (
    CollectResult, CommitRecord, EventRecord, MemberReport, MessageRecord, TaskRecord,
)
from shared.storage import ReportStorage

logger = get_logger("daily_report.main")

FALLBACK_RECIPIENTS: list[str] = ["2327929235@qq.com"]
DEFAULT_SUBJECT_PREFIX = "刘雨可—智能日报"

EMAIL_MAX_RETRIES = 2
EMAIL_RETRY_INTERVAL = 5

_OPEN_ID_NAME_MAP: dict[str, str] = {}


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
    parser.add_argument("--github-only", action="store_true",
                        help="只采集 GitHub，跳过飞书任务和消息")
    parser.add_argument("--auto", action="store_true",
                        help="一键自动：采集全部数据源，生成并发送")
    parser.add_argument("--to", action="append", default=None,
                        help="临时指定收件人邮箱，可重复")
    return parser.parse_args(argv)


def collection_window(now=None, lookback_days=1):
    now = now or datetime.now(LOCAL_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=LOCAL_TZ)
    days = max(1, min(int(lookback_days or 1), 30))
    start_day = now.date() - timedelta(days=days - 1)
    start = datetime.combine(start_day, time.min, tzinfo=now.tzinfo)
    return start, now


def is_workday(cfg, day=None) -> bool:
    day = day or datetime.now(LOCAL_TZ).date()
    schedule = cfg.schedule or {}
    if schedule.get("skip_weekends", True) and day.weekday() >= 5:
        return False
    holidays = set(schedule.get("holidays") or [])
    if day.isoformat() in holidays:
        return False
    return True


def _build_open_id_name_map(cfg) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for oid, name in _OPEN_ID_NAME_MAP.items():
        if oid and name:
            mapping[oid.strip().lower()] = name.strip()
    for m in cfg.members:
        name = m.name
        if getattr(m, "lark_open_id", None):
            mapping.setdefault(m.lark_open_id.strip().lower(), name)
        if getattr(m, "lark_user_id", None):
            mapping.setdefault(m.lark_user_id.strip().lower(), name)
    return mapping


def aggregate_members(cfg, commits, tasks) -> list[MemberReport]:
    github_by_user: dict[str, list[CommitRecord]] = {}
    for c in commits.records:
        github_by_user.setdefault(c.author.lower(), []).append(c)

    open_id_name_map = _build_open_id_name_map(cfg)

    tasks_by_user: dict[str, list[TaskRecord]] = {}
    for t in tasks.records:
        raw = (t.assignee or "").strip()
        key = raw.lower()
        mapped = open_id_name_map.get(key)
        if mapped:
            t.assignee = mapped
            key = mapped.lower()
        tasks_by_user.setdefault(key, []).append(t)

    def _collect_for(keys, bucket):
        seen = set()
        out = []
        for key in keys:
            for item in bucket.get(key, []):
                ident = id(item)
                if ident in seen:
                    continue
                seen.add(ident)
                out.append(item)
        return out

    members = []
    for m in cfg.members:
        keys = m.identity_keys()
        if getattr(m, "lark_open_id", None):
            keys.add(m.lark_open_id.strip().lower())
        if getattr(m, "lark_user_id", None):
            keys.add(m.lark_user_id.strip().lower())
        for oid, name in open_id_name_map.items():
            if name == m.name:
                keys.add(oid)
        members.append(MemberReport(
            name=m.name, github_username=m.github,
            commits=_collect_for(keys | {m.github.lower()}, github_by_user),
            tasks=_collect_for(keys, tasks_by_user),
            commits_failed=not commits.success,
            tasks_failed=not tasks.success,
        ))

    assigned = {id(c) for m in members for c in m.commits}
    leftover: dict[str, list[CommitRecord]] = {}
    for rec in commits.records:
        if id(rec) in assigned:
            continue
        leftover.setdefault(rec.author or "unknown", []).append(rec)
    for author, recs in leftover.items():
        members.append(MemberReport(
            name=f"未映射 · {author}", github_username=author,
            commits=recs, tasks=[],
            commits_failed=not commits.success,
            tasks_failed=not tasks.success,
        ))

    assigned_t = {id(t) for m in members for t in m.tasks}
    leftover_t: dict[str, list[TaskRecord]] = {}
    for rec in tasks.records:
        if id(rec) in assigned_t:
            continue
        leftover_t.setdefault(rec.assignee or "unknown", []).append(rec)
    for assignee, recs in leftover_t.items():
        members.append(MemberReport(
            name=f"未映射 · {assignee}", github_username="",
            commits=[], tasks=recs,
            commits_failed=not commits.success,
            tasks_failed=not tasks.success,
        ))
    return members


def run_collect(cfg, since, until, *, github_only=False):
    github_token = None
    try:
        github_token = cfg.github_token()
    except ConfigError as exc:
        logger.error(str(exc))

    branch = cfg.github.get("branch") or "main"
    commits = github_collector.collect(
        cfg.github["repos"], since, until, branch=branch, token=github_token,
    )
    logger.info(f"GitHub 采集完成: success={commits.success}, count={len(commits.records)}, branch={branch}")

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
        if not tasklist_id:
            logger.info("未配置 lark.tasklist_id，跳过飞书任务采集")
            tasks = CollectResult(success=True, records=[], error_message="")
        else:
            tasks = lark_task_collector.collect(
                tasklist_id, since, until, app_id=app_id, app_secret=app_secret,
            )
    else:
        tasks = CollectResult(success=False, records=[], error_message="缺少飞书凭证")

    logger.info(f"飞书任务采集完成: success={tasks.success}, count={len(tasks.records)}")
    return commits, tasks


def run_collect_events(cfg, since, until):
    usernames = [m.github for m in cfg.members if m.github]
    if not usernames:
        return CollectResult(success=True, records=[])
    try:
        token = cfg.github_token()
    except ConfigError:
        token = None
    try:
        result = github_events_collector.collect(usernames, since, until, token=token)
        logger.info(f"GitHub 活动采集完成: success={result.success}, count={len(result.records)}")
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"GitHub 活动采集跳过: {exc}")
        return CollectResult(success=False, records=[], error_message=str(exc))


def run_collect_feishu_messages(cfg, since, until):
    chat_id = cfg.lark.get("target_chat_id") or ""
    if not chat_id:
        logger.info("未配置 lark.target_chat_id，跳过飞书消息采集")
        return CollectResult(success=True, records=[])
    try:
        app_id, app_secret = cfg.lark_credentials()
    except ConfigError:
        return CollectResult(success=False, records=[], error_message="缺少飞书凭证")
    if not app_id or not app_secret:
        return CollectResult(success=False, records=[], error_message="缺少飞书凭证")
    try:
        result = feishu_message_collector.collect(
            chat_id, since, until, app_id=app_id, app_secret=app_secret,
        )
        logger.info(f"飞书消息采集完成: success={result.success}, count={len(result.records)}")
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"飞书消息采集跳过: {exc}")
        return CollectResult(success=False, records=[], error_message=str(exc))


def run_check(cfg) -> int:
    import httpx, os
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
    logger.info(f"[check] recipients: {_resolve_recipients(cfg)}")
    logger.info(f"[check] sender_name: {cfg.email.get('sender_name') or '(未配置)'}")
    logger.info(f"[check] subject_prefix: {cfg.email.get('subject_prefix') or DEFAULT_SUBJECT_PREFIX}")
    logger.info(f"[check] 飞书消息采集群: {cfg.lark.get('target_chat_id') or '(未配置)'}")
    logger.info(f"[check] 飞书日报推送群: {cfg.lark.get('notify_chat_id') or '(未配置，不推送)'}")
    logger.info(f"[check] DeepSeek: {'已配置' if os.environ.get('DEEPSEEK_API_KEY') else '(未配置)'}")

    try:
        app_id, app_secret = cfg.lark_credentials()
        if app_id and app_secret:
            with httpx.Client(base_url="https://open.feishu.cn/open-apis", timeout=15.0) as client:
                token = LarkTokenProvider(app_id, app_secret, client=client).get_token()
                logger.info(f"[check] Lark token OK (len={len(token)})")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[check] Lark token 跳过: {exc}")

    try:
        import smtplib
        user, password = cfg.smtp_credentials()
        host = cfg.email["smtp_host"]
        port = int(cfg.email.get("smtp_port") or 465)
        use_ssl = bool(cfg.email.get("use_ssl", True))
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=15) as s:
                s.login(user, password)
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.starttls()
                s.login(user, password)
        logger.info(f"[check] SMTP OK ({host}:{port})，发件人={user}")
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
    feishu_msgs: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    highlights: list[str] = field(default_factory=list)
    action_items: list[dict] = field(default_factory=list)      # ⭐ 新增
    commit_summaries: dict[str, str] = field(default_factory=dict)  # ⭐ 新增
    sender: str = ""
    recipients: list[str] = field(default_factory=list)
    email_sent: bool = False
    feishu_sent: bool = False


def _iso(value):
    if value is None:
        return ""
    try:
        return value.isoformat()
    except Exception:  # noqa: BLE001
        return str(value)


def _commit_payload(c: CommitRecord) -> dict[str, Any]:
    file_details = []
    for fd in (getattr(c, "file_details", None) or []):
        file_details.append({
            "filename": getattr(fd, "filename", ""),
            "status": getattr(fd, "status", "modified"),
            "additions": int(getattr(fd, "additions", 0) or 0),
            "deletions": int(getattr(fd, "deletions", 0) or 0),
        })
    return {
        "author": c.author, "message": c.message, "timestamp": _iso(c.timestamp),
        "repo": c.repo, "additions": c.additions, "deletions": c.deletions,
        "files_changed": c.files_changed, "sha": c.sha,
        "files": list(c.files or []), "file_details": file_details, "url": c.url,
    }


def _event_payload(e: EventRecord) -> dict[str, Any]:
    return {"actor": e.actor, "event_type": e.event_type, "repo": e.repo,
            "summary": e.summary, "timestamp": _iso(e.timestamp), "url": e.url}


def _message_payload(m: MessageRecord) -> dict[str, Any]:
    return {"sender_id": m.sender_id, "sender_name": m.sender_name,
            "chat_id": m.chat_id, "text": m.text,
            "msg_type": m.msg_type, "timestamp": _iso(m.timestamp)}


def _task_payload(t: TaskRecord) -> dict[str, Any]:
    return {"assignee": t.assignee, "title": t.title,
            "status_from": t.status_from, "status_to": t.status_to,
            "updated_at": _iso(t.updated_at)}


def _resolve_sender(cfg) -> str:
    sender = cfg.email.get("from_addr") or cfg.email.get("sender") or ""
    if sender:
        return sender.strip()
    try:
        user, _ = cfg.smtp_credentials()
        return user
    except ConfigError:
        return ""


def _resolve_recipients(cfg, override=None) -> list[str]:
    candidates = []
    if override:
        candidates.extend(override)
    else:
        candidates.extend(list(cfg.email.get("recipients") or []))
        if not candidates:
            candidates.extend(FALLBACK_RECIPIENTS)
    recipients = []
    for addr in candidates:
        addr = (addr or "").strip()
        if addr and addr not in recipients:
            recipients.append(addr)
    return recipients


def _build_subject(cfg, report_date) -> str:
    prefix = cfg.email.get("subject_prefix") or DEFAULT_SUBJECT_PREFIX
    return f"{prefix} · {report_date.isoformat()}"


def _send_email_with_retry(report, recipients, cfg, subject,
                            *, max_retries=EMAIL_MAX_RETRIES, interval=EMAIL_RETRY_INTERVAL):
    user, password = cfg.smtp_credentials()
    from_addr = _resolve_sender(cfg) or user
    sender_name = cfg.email.get("sender_name", "") or ""

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            ok = email_notifier.send(
                report, recipients,
                smtp_host=cfg.email["smtp_host"],
                smtp_port=int(cfg.email.get("smtp_port") or 465),
                use_ssl=bool(cfg.email.get("use_ssl", True)),
                username=user, password=password,
                from_addr=from_addr, sender_name=sender_name,
                subject=subject,
            )
            if ok:
                return True
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            logger.warning(f"邮件发送异常: {exc}")
        if attempt < max_retries:
            _time.sleep(interval)
    if last_err:
        logger.error(f"邮件发送最终失败: {last_err}")
    return False


def _send_feishu_with_report(cfg, report, members, highlights, summary, subject) -> bool:
    chat_id = cfg.lark.get("notify_chat_id") or ""
    if not chat_id:
        logger.info("未配置 lark.notify_chat_id，跳过飞书推送")
        return False
    try:
        app_id, app_secret = cfg.lark_credentials()
    except ConfigError:
        return False
    if not app_id or not app_secret:
        return False
    try:
        return feishu_notifier.send_daily_report(
            chat_id,
            app_id=app_id, app_secret=app_secret,
            subject=subject, report_date=report.date, team_name=report.team_name,
            members=members, highlights=highlights, summary=summary,
            generated_at=report.generated_at,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"飞书推送异常: {exc}")
        return False


def execute_pipeline(cfg, *, dry_run=False, force=False, lookback_days=1,
                     github_only=False, recipients_override=None) -> PipelineOutcome:
    started = datetime.now(LOCAL_TZ)
    logger.info(f"日报任务开始: {started.isoformat()}")

    if not force and not is_workday(cfg):
        logger.info("今日为非工作日，跳过日报生成")
        return PipelineOutcome(code=0, skipped=True, message="今日为非工作日，已跳过")

    since, until = collection_window(started, lookback_days=lookback_days)
    commits, tasks = run_collect(cfg, since, until, github_only=github_only)
    github_info = {"success": commits.success, "count": len(commits.records),
                   "error": commits.error_message or "",
                   "since": _iso(since), "until": _iso(until)}
    lark_info = {"success": tasks.success, "count": len(tasks.records),
                 "error": tasks.error_message or ""}

    if not (commits.success or tasks.success):
        logger.error("所有数据源不可用，不生成空日报")
        return PipelineOutcome(code=1, message="所有数据源不可用", github=github_info, lark=lark_info)

    events_result = run_collect_events(cfg, since, until)
    events_records = events_result.records
    events_info = {"success": events_result.success, "count": len(events_records),
                   "error": events_result.error_message or ""}

    feishu_msgs_result = run_collect_feishu_messages(cfg, since, until)
    feishu_msgs = feishu_msgs_result.records
    feishu_msgs_info = {"success": feishu_msgs_result.success,
                        "count": len(feishu_msgs),
                        "error": feishu_msgs_result.error_message or "",
                        "meta": feishu_msgs_result.meta}

    members = aggregate_members(cfg, commits, tasks)

    storage = ReportStorage(cfg.storage["db_path"])
    yesterday_stats = storage.get_yesterday_stats(until.date())

    # LLM：总结、亮点、群讨论要点、commit 摘要
    summary = summarize(members, feishu_msgs, until.date()) or ""
    highlights = extract_highlights(members, feishu_msgs, until.date(), top_n=3)
    action_items = extract_action_items(members, feishu_msgs, until.date(), max_items=5)  # ⭐
    commit_summaries = summarize_commits(members, until.date(), max_commits=20)           # ⭐

    report = generate(
        members, until.date(), cfg.team_name,
        events=events_records,
        feishu_messages=feishu_msgs,
        feishu_stats=feishu_msgs_result.meta,
        yesterday_stats=yesterday_stats,
        highlights=highlights,
        action_items=action_items,                # ⭐
        commit_summaries=commit_summaries,        # ⭐
        summary=summary,
        generated_at=started,
    )
    payload = {
        "sources": {"github": github_info, "lark": lark_info,
                    "events": events_info, "feishu_messages": feishu_msgs_info},
        "summary": summary,
        "highlights": highlights,
        "action_items": action_items,
        "commit_summaries": commit_summaries,
        "members": [
            {"name": m.name, "github_username": m.github_username,
             "commits_failed": m.commits_failed, "tasks_failed": m.tasks_failed,
             "commits": [_commit_payload(c) for c in m.commits],
             "tasks": [_task_payload(t) for t in m.tasks]}
            for m in members
        ],
        "events": [_event_payload(e) for e in events_records],
        "feishu_messages": [_message_payload(m) for m in feishu_msgs],
    }

    report_id = storage.save(report.date, report.team_name, report.markdown,
                              report.html, report.generated_at, payload=payload)
    logger.info(f"日报已写入 SQLite id={report_id}")

    sender = _resolve_sender(cfg)
    recipients = _resolve_recipients(cfg, override=recipients_override)
    subject = _build_subject(cfg, report.date)

    if dry_run:
        logger.info("dry-run 模式，跳过推送")
        logger.info(f"邮件主题: {subject}")
        logger.info(f"发件人: {sender}")
        logger.info(f"收件人: {recipients}")
        logger.info(f"飞书推送群: {cfg.lark.get('notify_chat_id') or '(未配置)'}")
        return PipelineOutcome(
            code=0, report_id=report_id,
            message="日报已生成（dry-run）",
            github=github_info, lark=lark_info, events=events_info,
            feishu_msgs=feishu_msgs_info, summary=summary,
            highlights=highlights, action_items=action_items,
            commit_summaries=commit_summaries,
            sender=sender, recipients=recipients,
        )

    email_ok = _send_email_with_retry(report, recipients, cfg, subject)
    feishu_ok = _send_feishu_with_report(cfg, report, members, highlights, summary, subject)

    logger.info(f"推送结果: email={email_ok} feishu={feishu_ok}")
    logger.info(f"日报任务结束: {(datetime.now(LOCAL_TZ) - started)}")
    return PipelineOutcome(
        code=0 if (email_ok or feishu_ok) else 1,
        report_id=report_id,
        message="日报已生成",
        github=github_info, lark=lark_info, events=events_info,
        feishu_msgs=feishu_msgs_info, summary=summary,
        highlights=highlights, action_items=action_items,
        commit_summaries=commit_summaries,
        sender=sender, recipients=recipients,
        email_sent=email_ok, feishu_sent=feishu_ok,
    )


def run_pipeline(cfg, *, dry_run=False, force=False, lookback_days=1,
                 github_only=False, recipients_override=None) -> int:
    return execute_pipeline(
        cfg, dry_run=dry_run, force=force, lookback_days=lookback_days,
        github_only=github_only, recipients_override=recipients_override,
    ).code


def run_auto(config_path: str = "config.yaml", recipients=None) -> PipelineOutcome:
    setup_logger()
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    cfg = load_config(config_path)
    return execute_pipeline(cfg, dry_run=False, force=True, github_only=False,
                            lookback_days=1, recipients_override=recipients)


def main(argv=None) -> int:
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
            cfg, dry_run=args.dry_run, force=True, github_only=False,
            lookback_days=1, recipients_override=args.to,
        )
        return outcome.code

    return run_pipeline(cfg, dry_run=args.dry_run, force=args.force,
                        github_only=args.github_only, recipients_override=args.to)


if __name__ == "__main__":
    sys.exit(main())