"""GitHub commit 采集。"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx

from shared.logger import get_logger
from shared.models import CollectResult, CommitRecord
from shared.retry import with_retries

logger = get_logger("daily_report.collector.github")

GITHUB_API = "https://api.github.com"


def collect(
    repos: list[str],
    since: datetime,
    until: datetime,
    *,
    token: str | None = None,
    client: httpx.Client | None = None,
) -> CollectResult[CommitRecord]:
    """获取指定仓库在 [since, until) 内的 commit 记录。

    多仓隔离：单个仓库失败不影响其他仓库已采集的数据。
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
                records.extend(_collect_repo(client, repo, since, until))
            except Exception as exc:  # noqa: BLE001
                logger.error(f"GitHub 仓库采集失败 {repo}: {exc}")
                errors.append(f"{repo}: {exc}")

        if errors and not records:
            return CollectResult(
                success=False,
                records=[],
                error_message="; ".join(errors),
            )
        if errors:
            return CollectResult(
                success=False,
                records=records,
                error_message="; ".join(errors),
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
                    "since": since_iso,
                    "until": until_iso,
                    "per_page": 100,
                    "page": current_page,
                },
            )
            resp = _handle_rate_limit(client, resp, repo, since_iso, until_iso, current_page)
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
            stats = _fetch_detail(client, repo, sha) if sha else (0, 0, 0, [], "")
            records.append(
                CommitRecord(
                    author=author_login,
                    message=(commit.get("message") or "").split("\n", 1)[0],
                    timestamp=_parse_dt(author_info.get("date")),
                    repo=repo,
                    additions=stats[0],
                    deletions=stats[1],
                    files_changed=stats[2],
                    sha=sha or "",
                    files=stats[3],
                    url=stats[4] or f"https://github.com/{repo}/commit/{sha}" if sha else "",
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
    # 上限 120s，避免联调卡死；仍尊重 reset 语义
    wait = min(wait, 120)
    logger.error(f"GitHub 限流 {repo}，等待 {wait}s 后重试")
    time.sleep(wait)
    retry = client.get(
        f"/repos/{repo}/commits",
        params={
            "since": since_iso,
            "until": until_iso,
            "per_page": 100,
            "page": page,
        },
    )
    return retry


def _fetch_detail(
    client: httpx.Client, repo: str, sha: str
) -> tuple[int, int, int, list[str], str]:
    """取提交统计与改动文件路径（不含 patch/源码正文）。"""

    def _request() -> httpx.Response:
        resp = client.get(f"/repos/{repo}/commits/{sha}")
        resp.raise_for_status()
        return resp

    try:
        detail = with_retries(_request, max_attempts=3, delay_seconds=5.0).json()
        stats = detail.get("stats") or {}
        files = detail.get("files") or []
        names = [str(f.get("filename") or "").strip() for f in files]
        names = [n for n in names if n][:40]
        return (
            int(stats.get("additions") or 0),
            int(stats.get("deletions") or 0),
            len(files),
            names,
            str(detail.get("html_url") or ""),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"获取 commit 详情失败 {repo}@{sha}: {exc}")
        return 0, 0, 0, [], ""


def _to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
