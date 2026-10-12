"""飞书群消息采集（官方开放平台 API）。

过滤规则：
  · 跳过机器人消息
  · 跳过系统消息（加入群、撤回提示等，非 text 类型）
  · 跳过已撤回的消息（deleted=true）
  · 跳过纯 @ 消息（例如 "@_user_1" 或 "@张三"）
  · 跳过字数 < 3 的消息
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import httpx

from shared.logger import get_logger
from shared.models import CollectResult, MessageRecord

logger = get_logger("daily_report.collector.feishu_message")

FEISHU_API = "https://open.feishu.cn/open-apis"

# ⭐ 本地 open_id → 名字映射（优先于 API 查询）
_OPEN_ID_NAME_MAP: dict[str, str] = {
    "ou_b4ebc00c714fdb8106be03417170341d": "刘雨可",
}

_MIN_TEXT_LEN = 3

_PURE_MENTION_RE = re.compile(
    r"^(?:\s*(?:@_user_\d+|@[\u4e00-\u9fff\w\u3000-\u303F\-]{1,30}|@all|@所有人)\s*)+$",
    re.IGNORECASE,
)

_STRIP_MENTIONS_RE = re.compile(
    r"(?:@_user_\d+|@[\u4e00-\u9fff\w\u3000-\u303F\-]{1,30}|@all|@所有人)",
    re.IGNORECASE,
)


def _get_tenant_token(app_id: str, app_secret: str, client: httpx.Client) -> str:
    resp = client.post(
        "/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取 tenant_token 失败: {data}")
    return data["tenant_access_token"]


def _get_user_name(
    open_id: str,
    client: httpx.Client,
    headers: dict,
    cache: dict[str, str],
) -> str:
    if not open_id:
        return "未知"
    if open_id in _OPEN_ID_NAME_MAP:
        return _OPEN_ID_NAME_MAP[open_id]
    if open_id in cache:
        return cache[open_id]
    try:
        resp = client.get(
            f"/contact/v3/users/{open_id}",
            headers=headers,
            params={"user_id_type": "open_id"},
        )
        data = resp.json()
        if data.get("code") == 0:
            user = (data.get("data") or {}).get("user") or {}
            name = user.get("name") or user.get("en_name") or ""
            if name:
                cache[open_id] = name
                return name
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"获取用户 {open_id} 名称失败: {exc}")
    fallback = open_id[:8]
    cache[open_id] = fallback
    return fallback


def _should_skip(text: str) -> bool:
    """过滤没意义的消息。返回 True 表示跳过。"""
    t = (text or "").strip()
    if not t:
        return True

    if _PURE_MENTION_RE.match(t):
        return True

    stripped = _STRIP_MENTIONS_RE.sub("", t).strip()
    stripped = re.sub(r"[\s，,。\.!！?？~～、；;：:\-]+", "", stripped)
    if not stripped:
        return True

    if len(stripped) < _MIN_TEXT_LEN:
        return True

    return False


def collect(
    chat_id: str,
    since: datetime,
    until: datetime,
    *,
    app_id: str,
    app_secret: str,
    client: httpx.Client | None = None,
) -> CollectResult[MessageRecord]:
    if not chat_id:
        return CollectResult(
            success=False, records=[], error_message="chat_id 为空",
        )

    owns_client = client is None
    client = client or httpx.Client(base_url=FEISHU_API, timeout=30.0)

    try:
        token = _get_tenant_token(app_id, app_secret, client)
        headers = {"Authorization": f"Bearer {token}"}
        params = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "start_time": str(int(since.timestamp())),
            "end_time": str(int(until.timestamp())),
            "page_size": 50,
            "sort_type": "ByCreateTimeAsc",
        }

        records: list[MessageRecord] = []
        name_cache: dict[str, str] = {}
        page_token = None
        skipped_count = 0
        received_count = 0

        while True:
            if page_token:
                params["page_token"] = page_token
            resp = client.get("/im/v1/messages", headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") != 0:
                return CollectResult(
                    success=False, records=[],
                    error_message=f"{data.get('code')}: {data.get('msg')}",
                )

            items = (data.get("data") or {}).get("items") or []
            for item in items:
                msg_type = item.get("msg_type", "")
                if msg_type != "text":
                    continue

                # ⭐ 跳过已撤回的消息
                if item.get("deleted"):
                    skipped_count += 1
                    logger.debug(f"跳过已撤回消息: {item.get('message_id')}")
                    continue

                received_count += 1

                sender_info = item.get("sender") or {}
                sender_type = (sender_info.get("sender_type") or "").lower()
                sender = sender_info.get("id") or ""

                if sender_type in {"app", "bot"}:
                    skipped_count += 1
                    continue

                content_str = item.get("body", {}).get("content", "")
                text = ""
                try:
                    text = json.loads(content_str).get("text", "")
                except Exception:  # noqa: BLE001
                    text = content_str

                if _should_skip(text):
                    skipped_count += 1
                    logger.debug(f"跳过消息: {text[:50]}")
                    continue

                sender_name = _get_user_name(sender, client, headers, name_cache)

                ts_ms = int(item.get("create_time") or 0)
                ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)

                records.append(
                    MessageRecord(
                        sender_id=sender,
                        sender_name=sender_name,
                        chat_id=chat_id,
                        text=text,
                        msg_type=msg_type,
                        timestamp=ts,
                    )
                )

            if not (data.get("data") or {}).get("has_more"):
                break
            page_token = (data.get("data") or {}).get("page_token")

        logger.info(
            f"飞书群消息采集完成: 保留 {len(records)} 条，过滤 {skipped_count} 条"
        )
        return CollectResult(
            success=True,
            records=records,
            meta={
                "kept": len(records),
                "skipped": skipped_count,
                "total": received_count,
            },
        )

    except Exception as exc:  # noqa: BLE001
        logger.error(f"飞书消息采集失败: {exc}")
        return CollectResult(success=False, records=[], error_message=str(exc))
    finally:
        if owns_client:
            client.close()