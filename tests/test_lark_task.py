"""飞书任务清单采集测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from collector.lark_task import LarkTokenProvider, collect


def test_collect_tasklist_and_token_refresh() -> None:
    calls = {"token": 0, "tasks": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            calls["token"] += 1
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": f"tok-{calls['token']}"},
            )
        if "/task/v2/tasklists/" in request.url.path and request.url.path.endswith("/tasks"):
            calls["tasks"] += 1
            auth = request.headers.get("Authorization", "")
            if auth.endswith("tok-1"):
                return httpx.Response(401, json={"code": 99991663, "msg": "token invalid"})
            # completed=true 的第二路请求返回空即可
            if request.url.params.get("completed") == "true":
                return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "summary": "实现登录",
                                "members": [
                                    {"id": "ou_zhangsan", "role": "assignee", "name": "张三"}
                                ],
                                "completed_at": "0",
                                "start": {"timestamp": str(int(datetime(2026, 9, 26, 10, tzinfo=timezone.utc).timestamp() * 1000))},
                            }
                        ],
                        "has_more": False,
                    },
                },
            )
        return httpx.Response(404)

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://open.feishu.cn/open-apis",
    )
    provider = LarkTokenProvider("id", "secret", client=client)
    result = collect(
        "aeabc15e-dd81-4d42-a559-6b7fb0aff2dd",
        datetime(2026, 9, 26, tzinfo=timezone.utc),
        datetime(2026, 9, 26, 18, tzinfo=timezone.utc),
        token_provider=provider,
        client=client,
    )
    assert result.success
    assert len(result.records) == 1
    assert result.records[0].title == "实现登录"
    assert result.records[0].assignee == "ou_zhangsan"
    assert calls["token"] >= 2
