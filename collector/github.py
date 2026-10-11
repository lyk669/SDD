"""GitHub commit collector."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx

from shared.logger import get_logger
from shared.models import CollectResult, CommitRecord, FileChange
from shared.retry import with_retries

logger = get_logger("daily_report.collector.github")

GITHUB_API = "https://api.github.com"


def collect(
    repos: list[str],
    since: datetime,
    until: datetime,
    *,
    branch: str = "main",
    token: str | None = None,
    client: httpx.Client | None = None,
) -> CollectResult[CommitRecord]:
    """Fetch commits in [since, until) for the given repos on the specified branch.

    Multi-repo isolation: one repo failing does not affect the others.
    """
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

    records: list[CommitRecord] = []
    errors: list[str] = []
    try:
        for repo in repos:
            try:
                records.extend(
                    _collect_repo(client, repo, since, until, branch=branch)
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(f"GitHub repo collect failed {repo}: {exc}")
                errors.append(f"{repo}: {exc}")

        if errors and not records:
            return CollectResult(
                success=False, records=[], error_message="; ".join(errors),
            )
        if errors:
            return CollectResult(
                success=False, records=records, error_message="; ".join(errors),
            )
        return CollectResult(success=True, records=records)
    finally:
        if owns_client:
            client.close()


def _collect_repo(
    client: httpx.Client,
    repo: str,
    since: datetime,
    until: datetime,
    branch: str = "main",
) -> list[CommitRecord]:
    records: list[CommitRecord] = []
    page = 1
    since_iso = _to_iso(since)
    until_iso = _to_iso(until)

    while True:
        def _request(current_page: int = page) -> httpx.Response:
            resp = client.get(
                f"/repos/{repo}/commits",
                params={
                    "sha": branch,
                    "since": since_iso,
                    "until": until_iso,
                    "per_page": 100,
                    "page": current_page,
                },
            )
            resp = _handle_rate_limit(
                client, resp, repo, branch, since_iso, until_iso, current_page
            )
            resp.raise_for_status()
            return resp

        response = with_retries(_request, max_attempts=3, delay_seconds=5.0)
        payload = response.json()
        if not payload:
            break

        for item in payload:
            sha = item.get("sha")
            commit = item.get("commit") or {}
            author_info = commit.get("author") or {}
            author_login = (
                (item.get("author") or {}).get("login")
                or author_info.get("name")
                or "unknown"
            )
            detail = _fetch_detail(client, repo, sha) if sha else _empty_detail()
            records.append(
                CommitRecord(
                    author=author_login,
                    message=(commit.get("message") or "").split("\n", 1)[0],
                    timestamp=_parse_dt(author_info.get("date")),
                    repo=repo,
                    additions=detail["additions"],
                    deletions=detail["deletions"],
                    files_changed=detail["files_changed"],
                    sha=sha or "",
                    files=detail["files"],
                    file_details=detail["file_details"],
                    url=detail["url"] or (
                        f"https://github.com/{repo}/commit/{sha}" if sha else ""
                    ),
                )
            )

        if len(payload) < 100:
            break
        page += 1

    return records


def _handle_rate_limit(
    client: httpx.Client,
    resp: httpx.Response,
    repo: str,
    branch: str,
    since_iso: str,
    until_iso: str,
    page: int,
) -> httpx.Response:
    if resp.status_code != 403:
        return resp
    text = resp.text.lower()
    if "rate limit" not in text and "secondary rate" not in text:
        return resp

    reset = int(resp.headers.get("X-RateLimit-Reset", "0") or 0)
    wait = max(reset - int(datetime.now(timezone.utc).timestamp()), 1)
    wait = min(wait, 120)
    logger.error(f"GitHub rate limited {repo}, sleeping {wait}s")
    time.sleep(wait)
    retry = client.get(
        f"/repos/{repo}/commits",
        params={
            "sha": branch,
            "since": since_iso,
            "until": until_iso,
            "per_page": 100,
            "page": page,
        },
    )
    return retry


def _empty_detail() -> dict:
    return {
        "additions": 0,
        "deletions": 0,
        "files_changed": 0,
        "files": [],
        "file_details": [],
        "url": "",
    }


def _fetch_detail(client: httpx.Client, repo: str, sha: str) -> dict:
    """Fetch commit stats and per-file change details (without patch body)."""

    def _request() -> httpx.Response:
        resp = client.get(f"/repos/{repo}/commits/{sha}")
        resp.raise_for_status()
        return resp

    try:
        detail = with_retries(_request, max_attempts=3, delay_seconds=5.0).json()
        stats = detail.get("stats") or {}
        raw_files = detail.get("files") or []

        file_details: list[FileChange] = []
        for f in raw_files[:40]:
            name = str(f.get("filename") or "").strip()
            if not name:
                continue
            file_details.append(
                FileChange(
                    filename=name,
                    status=str(f.get("status") or "modified"),
                    additions=int(f.get("additions") or 0),
                    deletions=int(f.get("deletions") or 0),
                )
            )

        return {
            "additions": int(stats.get("additions") or 0),
            "deletions": int(stats.get("deletions") or 0),
            "files_changed": len(raw_files),
            "files": [fd.filename for fd in file_details],
            "file_details": file_details,
            "url": str(detail.get("html_url") or ""),
        }
    except Exception as exc:  # noqa: BLE001
        logger.error(f"fetch commit detail failed {repo}@{sha}: {exc}")
        return _empty_detail()


def _to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))