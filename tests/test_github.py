"""GitHub 采集模块测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from collector.github import collect


def _commit_item(sha: str, login: str, message: str) -> dict:
    return {
        "sha": sha,
        "author": {"login": login},
        "commit": {
            "message": message,
            "author": {"name": login, "date": "2026-09-26T02:00:00Z"},
        },
    }


def test_collect_commits_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits") and request.url.path.count("/") == 4:
            return httpx.Response(200, json=[_commit_item("abc", "zhangsan", "fix: bug")])
        if request.url.path.endswith("/commits/abc"):
            return httpx.Response(
                200,
                json={
                    "html_url": "https://github.com/org/repo/commit/abc",
                    "stats": {"additions": 3, "deletions": 1},
                    "files": [{"filename": "a.py"}, {"filename": "b.py"}],
                },
            )
        return httpx.Response(404, json={"message": "not found"})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="https://api.github.com")
    result = collect(
        ["org/repo"],
        datetime(2026, 9, 26, tzinfo=timezone.utc),
        datetime(2026, 9, 26, 18, tzinfo=timezone.utc),
        token="t",
        client=client,
    )
    assert result.success
    assert len(result.records) == 1
    rec = result.records[0]
    assert rec.author == "zhangsan"
    assert rec.message == "fix: bug"
    assert rec.additions == 3
    assert rec.deletions == 1
    assert rec.files_changed == 2
    assert rec.repo == "org/repo"
    assert rec.files == ["a.py", "b.py"]
    assert rec.sha == "abc"


def test_collect_failure_returns_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.com",
    )
    # 缩短重试：直接让 raise_for_status 失败多次会很慢；这里 mock 成立即失败但 with_retries 会 sleep
    # 用 monkeypatch 降低延迟
    import collector.github as mod

    original = mod.with_retries

    def fast_retry(fn, **kwargs):
        kwargs["delay_seconds"] = 0
        kwargs["max_attempts"] = 1
        return original(fn, **kwargs)

    mod.with_retries = fast_retry  # type: ignore[assignment]
    try:
        result = collect(
            ["org/repo"],
            datetime(2026, 9, 26, tzinfo=timezone.utc),
            datetime(2026, 9, 26, 18, tzinfo=timezone.utc),
            client=client,
        )
    finally:
        mod.with_retries = original  # type: ignore[assignment]

    assert result.success is False
    assert result.records == []
    assert result.error_message


def test_collect_multi_repo_partial_failure() -> None:
    """一仓失败不影响另一仓已采集的记录。"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/org/ok/commits":
            return httpx.Response(200, json=[_commit_item("abc", "zhangsan", "ok")])
        if path == "/repos/org/ok/commits/abc":
            return httpx.Response(
                200,
                json={"stats": {"additions": 1, "deletions": 0}, "files": [{}]},
            )
        if path.startswith("/repos/org/bad/"):
            return httpx.Response(500, text="boom")
        return httpx.Response(404)

    import collector.github as mod

    original = mod.with_retries

    def fast_retry(fn, **kwargs):
        kwargs["delay_seconds"] = 0
        kwargs["max_attempts"] = 1
        return original(fn, **kwargs)

    mod.with_retries = fast_retry  # type: ignore[assignment]
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api.github.com",
    )
    try:
        result = collect(
            ["org/ok", "org/bad"],
            datetime(2026, 9, 26, tzinfo=timezone.utc),
            datetime(2026, 9, 26, 18, tzinfo=timezone.utc),
            client=client,
        )
    finally:
        mod.with_retries = original  # type: ignore[assignment]

    assert result.success is False
    assert len(result.records) == 1
    assert result.records[0].repo == "org/ok"
    assert "org/bad" in (result.error_message or "")
