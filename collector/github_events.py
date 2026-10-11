"""GitHub user public events collector (Events API)."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from shared.logger import get_logger
from shared.models import CollectResult, EventRecord

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
            url = "/users/" + str(username) + "/events"
            logger.info(f"[events] user={username!r} url={url!r}")
            try:
                resp = client.get(url, params={"per_page": 30, "page": 1})
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[events] request failed user={username}: {exc}")
                errors.append(f"{username}: {exc}")
                continue

            for item in payload or []:
                ts = _parse_dt(item.get("created_at"))
                if ts < since or ts >= until:
                    continue
                summary, link = _describe(item)
                if summary is None:
                    continue
                records.append(
                    EventRecord(
                        actor=str(username),
                        event_type=item.get("type", ""),
                        repo=(item.get("repo") or {}).get("name", ""),
                        summary=summary,
                        timestamp=ts,
                        url=link,
                    )
                )

        if errors and not records:
            return CollectResult(success=False, records=[], error_message="; ".join(errors))
        return CollectResult(success=True, records=records)
    finally:
        if owns_client:
            client.close()


def _describe(item: dict) -> tuple[str | None, str]:
    etype = item.get("type", "")
    repo = (item.get("repo") or {}).get("name", "")
    payload = item.get("payload") or {}

    if etype == "PushEvent":
        commits = payload.get("commits") or []
        if not commits:
            return None, ""
        return f"push to {repo} ({len(commits)} commits)", f"https://github.com/{repo}"

    if etype == "PullRequestEvent":
        action = payload.get("action", "")
        pr = payload.get("pull_request") or {}
        return (
            f"{action} PR #{pr.get('number', '')} {pr.get('title', '')} ({repo})",
            pr.get("html_url", ""),
        )

    if etype == "PullRequestReviewEvent":
        pr = payload.get("pull_request") or {}
        return f"review PR #{pr.get('number', '')} ({repo})", pr.get("html_url", "")

    if etype == "IssuesEvent":
        action = payload.get("action", "")
        issue = payload.get("issue") or {}
        return (
            f"{action} issue #{issue.get('number', '')} {issue.get('title', '')} ({repo})",
            issue.get("html_url", ""),
        )

    if etype == "IssueCommentEvent":
        issue = payload.get("issue") or {}
        return f"comment issue #{issue.get('number', '')} ({repo})", issue.get("html_url", "")

    if etype == "WatchEvent":
        return f"star {repo}", f"https://github.com/{repo}"

    if etype == "ForkEvent":
        return f"fork {repo}", f"https://github.com/{repo}"

    if etype == "CreateEvent":
        ref_type = payload.get("ref_type", "")
        ref = payload.get("ref", "")
        if ref_type == "repository":
            return f"create repo {repo}", f"https://github.com/{repo}"
        return f"create {ref_type} {ref} ({repo})", f"https://github.com/{repo}"

    if etype == "DeleteEvent":
        ref_type = payload.get("ref_type", "")
        ref = payload.get("ref", "")
        return f"delete {ref_type} {ref} ({repo})", f"https://github.com/{repo}"

    if etype == "ReleaseEvent":
        rel = payload.get("release") or {}
        return f"release {rel.get('tag_name', '')} ({repo})", rel.get("html_url", "")

    if etype == "PublicEvent":
        return f"public repo {repo}", f"https://github.com/{repo}"

    return None, ""


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))