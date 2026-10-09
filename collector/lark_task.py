"""飞书任务清单采集（Tasklist GUID）。"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from shared.logger import get_logger
from shared.models import CollectResult, TaskRecord
from shared.retry import with_retries

logger = get_logger("daily_report.collector.lark_task")

LARK_API = "https://open.feishu.cn/open-apis"


class LarkTokenProvider:
    """飞书 tenant_access_token 获取与刷新。"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self._client = client
        self._token: str | None = None

    def get_token(self) -> str:
        if self._token:
            return self._token
        return self.refresh()

    def refresh(self) -> str:
        owns = self._client is None
        client = self._client or httpx.Client(base_url=LARK_API, timeout=30.0)
        try:
            resp = client.post(
                "/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code", 0) != 0:
                raise RuntimeError(f"获取飞书 token 失败: {data}")
            token = data.get("tenant_access_token")
            if not token:
                raise RuntimeError(f"获取飞书 token 失败: {data}")
            self._token = token
            return token
        finally:
            if owns:
                client.close()


def collect(
    tasklist_id: str,
    since: datetime,
    until: datetime,
    *,
    app_id: str | None = None,
    app_secret: str | None = None,
    token_provider: LarkTokenProvider | None = None,
    client: httpx.Client | None = None,
) -> CollectResult[TaskRecord]:
    """获取指定任务清单（task-lists URL 中的 GUID）在时间窗口内的任务。

    对应开放接口：
    GET /task/v2/tasklists/:tasklist_guid/tasks
    """
    owns_client = client is None
    client = client or httpx.Client(base_url=LARK_API, timeout=30.0)

    try:
        provider = token_provider or LarkTokenProvider(
            app_id or "",
            app_secret or "",
            client=client,
        )
        records = _collect_tasks(client, provider, tasklist_id, since, until)
        return CollectResult(success=True, records=records)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"飞书任务采集失败: {exc}")
        return CollectResult(success=False, records=[], error_message=str(exc))
    finally:
        if owns_client:
            client.close()


def _collect_tasks(
    client: httpx.Client,
    provider: LarkTokenProvider,
    tasklist_id: str,
    since: datetime,
    until: datetime,
) -> list[TaskRecord]:
    records: list[TaskRecord] = []
    page_token: str | None = None
    since_ms = str(int(since.timestamp() * 1000))
    until_ms = str(int(until.timestamp() * 1000))
    path = f"/task/v2/tasklists/{tasklist_id}/tasks"

    while True:
        token = provider.get_token()

        def _request(
            current_token: str = token,
            current_page_token: str | None = page_token,
        ) -> httpx.Response:
            params: dict = {
                "page_size": 50,
                "created_from": since_ms,
                "created_to": until_ms,
                "user_id_type": "open_id",
            }
            if current_page_token:
                params["page_token"] = current_page_token
            resp = client.get(
                path,
                headers={"Authorization": f"Bearer {current_token}"},
                params=params,
            )
            if resp.status_code == 401:
                new_token = provider.refresh()
                resp = client.get(
                    path,
                    headers={"Authorization": f"Bearer {new_token}"},
                    params=params,
                )
            resp.raise_for_status()
            return resp

        response = with_retries(_request, max_attempts=3, delay_seconds=5.0)
        body = response.json()
        if body.get("code", 0) != 0:
            raise RuntimeError(f"飞书任务 API 错误: {body}")

        data = body.get("data") or {}
        items = data.get("items") or []

        for item in items:
            records.append(_to_task_record(item, since, until))

        page_token = data.get("page_token") or None
        if not data.get("has_more") or not page_token:
            break

    # 再取「已完成」任务，覆盖「今日完成但非今日创建」的情况
    records.extend(_collect_completed_in_window(client, provider, tasklist_id, since, until))
    return _dedupe(records)


def _collect_completed_in_window(
    client: httpx.Client,
    provider: LarkTokenProvider,
    tasklist_id: str,
    since: datetime,
    until: datetime,
) -> list[TaskRecord]:
    records: list[TaskRecord] = []
    page_token: str | None = None
    path = f"/task/v2/tasklists/{tasklist_id}/tasks"

    while True:
        token = provider.get_token()

        def _request(
            current_token: str = token,
            current_page_token: str | None = page_token,
        ) -> httpx.Response:
            params: dict = {
                "page_size": 50,
                "completed": "true",
                "user_id_type": "open_id",
            }
            if current_page_token:
                params["page_token"] = current_page_token
            resp = client.get(
                path,
                headers={"Authorization": f"Bearer {current_token}"},
                params=params,
            )
            if resp.status_code == 401:
                new_token = provider.refresh()
                resp = client.get(
                    path,
                    headers={"Authorization": f"Bearer {new_token}"},
                    params=params,
                )
            resp.raise_for_status()
            return resp

        try:
            response = with_retries(_request, max_attempts=3, delay_seconds=5.0)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"拉取已完成任务失败（可忽略）: {exc}")
            break

        body = response.json()
        if body.get("code", 0) != 0:
            break

        data = body.get("data") or {}
        for item in data.get("items") or []:
            completed_at = _parse_ms(item.get("completed_at"))
            since_cmp = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
            until_cmp = until if until.tzinfo else until.replace(tzinfo=timezone.utc)
            if since_cmp <= completed_at <= until_cmp:
                records.append(_to_task_record(item, since, until))

        page_token = data.get("page_token") or None
        if not data.get("has_more") or not page_token:
            break

    return records


def _to_task_record(item: dict, since: datetime, until: datetime) -> TaskRecord:
    _ = (since, until)
    completed_raw = item.get("completed_at")
    is_done = bool(completed_raw) and str(completed_raw) not in {"0", ""}
    if is_done:
        status_from, status_to = "进行中", "已完成"
        updated_at = _parse_ms(completed_raw)
    else:
        status_from, status_to = "", "未完成"
        start = (item.get("start") or {}).get("timestamp")
        updated_at = _parse_ms(start) if start else datetime.now(timezone.utc)

    return TaskRecord(
        assignee=_extract_assignee(item),
        title=item.get("summary") or item.get("title") or "",
        status_from=status_from,
        status_to=status_to,
        updated_at=updated_at,
    )


def _dedupe(records: list[TaskRecord]) -> list[TaskRecord]:
    seen: set[tuple] = set()
    out: list[TaskRecord] = []
    for r in records:
        key = (r.assignee, r.title, r.status_to, r.updated_at.isoformat())
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _extract_assignee(item: dict) -> str:
    members = item.get("members") or []
    if isinstance(members, list):
        # 优先 assignee / owner 角色
        for role in ("assignee", "owner", "editor", "follower"):
            for m in members:
                if isinstance(m, dict) and m.get("role") == role:
                    return m.get("id") or m.get("name") or "unknown"
        if members:
            first = members[0]
            if isinstance(first, dict):
                return first.get("id") or first.get("name") or "unknown"
            return str(first)
    return "unknown"


def _parse_ms(value: int | str | None) -> datetime:
    if value is None or value == "" or value == "0":
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        try:
            value = int(value)
        except ValueError:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value > 10_000_000_000:
        value = value / 1000
    return datetime.fromtimestamp(value, tz=timezone.utc)
