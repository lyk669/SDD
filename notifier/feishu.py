"""飞书群日报推送（消息卡片 · 摘要版）。

设计：飞书卡片是"通知"，只放概要 + 亮点 + 总结。
      完整日报（commit 明细 + 文件增删 + 群消息）走邮件。
"""

from __future__ import annotations

import json
from datetime import datetime

import httpx

from shared.logger import get_logger

logger = get_logger("daily_report.notifier.feishu")

FEISHU_API = "https://open.feishu.cn/open-apis"


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


def _build_card(
    *,
    subject: str,
    report_date,
    team_name: str,
    total_commits: int,
    total_files: int,
    total_add: int,
    total_del: int,
    net: int,
    highlights: list,
    summary: str,
    repos: list,
    generated_at: datetime,
) -> dict:
    """构造飞书消息卡片（摘要版，不含 commit 明细 / 群消息）。"""
    header_title = f"📋 {subject}"

    elements: list[dict] = []

    # ① 概览（一行）
    overview_md = (
        f"**📊 {total_commits} 次提交** · "
        f"**{total_files}** 个文件 · "
        f"**+{total_add} / -{total_del}**"
        f"（净 **{'+' if net >= 0 else ''}{net}** 行）"
    )
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": overview_md},
    })

    # ② 今日亮点
    if highlights:
        elements.append({"tag": "hr"})
        hl_md = "**⭐ 今日亮点**\n"
        for i, h in enumerate(highlights, 1):
            hl_md += f"{i}. {h}\n"
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": hl_md.rstrip()},
        })

    # ③ 今日总结
    if summary:
        elements.append({"tag": "hr"})
        s = summary if len(summary) <= 400 else summary[:400] + "…"
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**📝 今日总结**\n{s}"},
        })

    # ④ 底部：引导去看邮件
    elements.append({"tag": "hr"})
    footer_md = (
        f"📧 **完整日报（含 commit 明细）见邮箱**\n"
        f"数据来源：GitHub · {', '.join(repos) if repos else '（无）'}\n"
        f"生成时间：{generated_at.strftime('%Y-%m-%d %H:%M')}"
    )
    elements.append({
        "tag": "div",
        "text": {"tag": "lark_md", "content": footer_md},
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": header_title,
            },
        },
        "elements": elements,
    }


def send_daily_report(
    chat_id: str,
    *,
    app_id: str,
    app_secret: str,
    subject: str,
    report_date,
    team_name: str,
    members: list,
    highlights: list,
    summary: str,
    generated_at: datetime,
    client: httpx.Client | None = None,
) -> bool:
    """把日报摘要以消息卡片形式发到飞书群。成功返回 True。"""
    if not chat_id:
        logger.info("未配置飞书通知群，跳过飞书推送")
        return False

    # 统计
    all_commits = [c for m in members for c in (getattr(m, "commits", []) or [])]
    total_commits = len(all_commits)
    total_files = sum(int(getattr(c, "files_changed", 0) or 0) for c in all_commits)
    total_add = sum(int(getattr(c, "additions", 0) or 0) for c in all_commits)
    total_del = sum(int(getattr(c, "deletions", 0) or 0) for c in all_commits)
    net = total_add - total_del
    repos = sorted({getattr(c, "repo", "") for c in all_commits if getattr(c, "repo", "")})

    card = _build_card(
        subject=subject,
        report_date=report_date,
        team_name=team_name,
        total_commits=total_commits,
        total_files=total_files,
        total_add=total_add,
        total_del=total_del,
        net=net,
        highlights=highlights or [],
        summary=summary or "",
        repos=repos,
        generated_at=generated_at,
    )

    owns_client = client is None
    client = client or httpx.Client(base_url=FEISHU_API, timeout=30.0)

    try:
        token = _get_tenant_token(app_id, app_secret, client)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        resp = client.post(
            "/im/v1/messages",
            headers=headers,
            params={"receive_id_type": "chat_id"},
            json=payload,
        )
        data = resp.json()
        if data.get("code") == 0:
            logger.info(f"飞书日报推送成功: chat_id={chat_id}")
            return True
        logger.error(f"飞书日报推送失败: {data}")
        return False
    except Exception as exc:  # noqa: BLE001
        logger.error(f"飞书日报推送异常: {exc}")
        return False
    finally:
        if owns_client:
            client.close()