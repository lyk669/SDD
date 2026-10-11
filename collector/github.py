"""GitHub 用户公开活动采集（Events API）。

限制：
  · 只返回公开活动（私有仓库的活动不出现）
  · 只保留最近 90 天
  · 每用户最多取 3 页（90 条）
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from shared.logger import get_logger
from shared.models import CollectResult, EventRecord
from shared.retry import with_retries

logger = get_logger("daily_report.collector.github_events")

GITHUB_API = "https://api.github.com"


def collect(
    usernames: list[str],
    since: datetime,
    until: datetime,
    *,
    token: str | None = None,
    client: httpx.Client | None = None,
) -> CollectResult[EventRecord]:
    owns_client = client is None
    client = client or httpx.Client(
        base_url=GITHUB_API,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
        timeout=30.0,
    )

    records: list[EventRecord] = []
    errors: list[str] = []
    try:
        for username in usernames:
            try:
                records.extend(_collect_user(client, username, since, until))
            except Exception as exc:  # noqa: BLE001
                logger.error(f"GitHub 活动采集失败 {username}: {exc}")
                errors.append(f"{username}: {exc}")

        if errors and not records:
            return CollectResult(success=False, records=[], error_message="; ".join(errors))
        if errors:
            return CollectResult(success=False, records=records, error_message="; ".join(errors))
        return CollectResult(success=True, records=records)
    finally:
        if owns_client:
            client.close()


def _collect_user(
    client: httpx.Client,
    username: str,
    since: datetime,
    until: datetime,
) -> list[EventRecord]:
    records: list[EventRecord] = []
    page = 1
    max_pages = 3

    while page <= max_pages:
        def _request(p: int = page) -> httpx.Response:
            resp = client.get(
                f"/users/{username}/events",
                params={"per_page": 30, "page": p},
            )
            resp.raise_for_status()
            return resp

        try:
            payload = with_retries(_request, max_attempts=3, delay_seconds=5.0).json()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"获取 {username} 活动失败 page={page}: {exc}")
            break

        if not payload:
            break

        stop = False
        for item in payload:
            ts = _parse_dt(item.get("created_at"))
            if ts < since:
                stop = True
                break
            if ts >= until:
                continue
            summary, url = _describe(item)
            if summary is None:
                continue
            records.append(
                EventRecord(
                    actor=username,
                    event_type=item.get("type", ""),
                    repo=(item.get("repo") or {}).get("name", ""),
                    summary=summary,
                    timestamp=ts,
                    url=url,
                )
            )

        if stop or len(payload) < 30:
            break
        page += 1

    return records


def _describe(item: dict) -> tuple[str | None, str]:
    """把 event 转成中文摘要。返回 (None, "") 表示跳过。"""
    etype = item.get("type", "")
    repo = (item.get("repo") or {}).get("name", "")
    payload = item.get("payload") or {}

    if etype == "PushEvent":
        commits = payload.get("commits") or []
        n = len(commits)
        if n == 0:
            return None, ""
        return f"push 到 {repo}（{n} 个提交）", f"https://github.com/{repo}"

    if etype == "PullRequestEvent":
        action = payload.get("action", "")
        pr = payload.get("pull_request") or {}
        num = pr.get("number", "")
        title = pr.get("title", "")
        action_cn = {
            "opened": "打开", "closed": "关闭",
            "merged": "合并", "reopened": "重新打开",
        }.get(action, action)
        url = pr.get("html_url", "")
        return f"{action_cn} PR #{num} · {title}（{repo}）", url

    if etype == "PullRequestReviewEvent":
        pr = payload.get("pull_request") or {}
        num = pr.get("number", "")
        return f"review 了 PR #{num}（{repo}）", pr.get("html_url", "")

    if etype == "PullRequestReviewCommentEvent":
        pr = payload.get("pull_request") or {}
        num = pr.get("number", "")
        return f"评论 PR #{num}（{repo}）", pr.get("html_url", "")

    if etype == "IssuesEvent":
        action = payload.get("action", "")
        issue = payload.get("issue") or {}
        num = issue.get("number", "")
        title = issue.get("title", "")
        action_cn = {
            "opened": "打开", "closed": "关闭", "reopened": "重新打开",
        }.get(action, action)
        return f"{action_cn} issue #{num} · {title}（{repo}）", issue.get("html_url", "")

    if etype == "IssueCommentEvent":
        issue = payload.get("issue") or {}
        num = issue.get("number", "")
        return f"评论 issue #{num}（{repo}）", issue.get("html_url", "")

    if etype == "CreateEvent":
        ref_type = payload.get("ref_type", "")
        ref = payload.get("ref", "")
        if ref_type == "repository":
            return f"创建仓库 {repo}", f"https://github.com/{repo}"
        type_cn = {"branch": "分支", "tag": "标签"}.get(ref_type, ref_type)
        return f"创建{type_cn} {ref}（{repo}）", f"https://github.com/{repo}"

    if etype == "DeleteEvent":
        ref_type = payload.get("ref_type", "")
        ref = payload.get("ref", "")
        type_cn = {"branch": "分支", "tag": "标签"}.get(ref_type, ref_type)
        return f"删除{type_cn} {ref}（{repo}）", f"https://github.com/{repo}"

    if etype == "WatchEvent":
        return f"star 了 {repo}", f"https://github.com/{repo}"

    if etype == "ForkEvent":
        return f"fork 了 {repo}", f"https://github.com/{repo}"

    if etype == "ReleaseEvent":
        rel = payload.get("release") or {}
        tag = rel.get("tag_name", "")
        return f"发布 release {tag}（{repo}）", rel.get("html_url", "")

    if etype == "PublicEvent":
        return f"公开仓库 {repo}", f"https://github.com/{repo}"

    return None, ""


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))