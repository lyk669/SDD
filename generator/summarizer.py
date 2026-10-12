"""用 LLM（DeepSeek）生成日报的"今日总结"。"""

from __future__ import annotations

import os

import httpx

from shared.logger import get_logger

logger = get_logger("daily_report.generator.summarizer")

_API_KEY_ENV = "DEEPSEEK_API_KEY"
_BASE_URL_ENV = "DEEPSEEK_BASE_URL"
_MODEL_ENV = "DEEPSEEK_MODEL"

_DEFAULT_BASE_URL = "https://api.deepseek.com"
_DEFAULT_MODEL = "deepseek-chat"


def _build_prompt(members, feishu_messages, report_date) -> str:
    """把今天的数据整理成给 LLM 的提示。"""
    lines = []
    lines.append(f"日期：{report_date.isoformat()}")
    lines.append("")

    # 代码提交
    lines.append("【代码提交】")
    has_commit = False
    total_add = total_del = 0
    for m in members:
        commits = getattr(m, "commits", None) or []
        for c in commits:
            has_commit = True
            msg = (getattr(c, "message", "") or "").strip().splitlines()[0]
            add = int(getattr(c, "additions", 0) or 0)
            dele = int(getattr(c, "deletions", 0) or 0)
            total_add += add
            total_del += dele
            files = int(getattr(c, "files_changed", 0) or 0)
            ts = getattr(c, "timestamp", None)
            time_str = ts.strftime("%H:%M") if ts else "--:--"
            lines.append(
                f"- [{time_str}] {msg} (+{add}/-{dele}, {files} 个文件)"
            )
    if not has_commit:
        lines.append("（无）")
    lines.append("")

    # 群消息
    lines.append("【群消息】")
    has_msg = False
    for msg in (feishu_messages or []):
        has_msg = True
        sender = getattr(msg, "sender_name", "") or "未知"
        text = (getattr(msg, "text", "") or "").replace("\n", " ").strip()
        ts = getattr(msg, "timestamp", None)
        time_str = ts.strftime("%H:%M") if ts else "--:--"
        lines.append(f"- [{time_str}] {sender}：{text}")
    if not has_msg:
        lines.append("（无）")
    lines.append("")

    # 提示 LLM
    if has_msg and has_commit:
        lines.append(
            "请根据以上信息，用中文写一段 3-5 句的『今日工作总结』。"
            "要求："
            "① 语气正式，像团队日报的总结；"
            "② 先说代码提交方面的工作（改了什么、加了什么功能、修了什么 bug），"
            "再说群里的讨论内容（讨论主题、达成的共识、待办事项）；"
            "③ 突出关键成果，不要罗列每一条；"
            "④ 不要用 markdown 列表、不要加标题、不要写代码；"
            "⑤ 直接输出总结内容，不要任何前缀。"
        )
    elif has_commit:
        lines.append(
            "请根据以上信息，用中文写一段 2-4 句的『今日工作总结』。"
            "要求：语气正式，突出完成的工作和进展，"
            "不要用 markdown 列表、不要加标题、不要写代码。"
            "直接输出总结内容。"
        )
    else:
        lines.append(
            "请根据以上信息，用中文写一段 2-3 句的『今日工作总结』。"
            "主要概括群里的讨论内容，"
            "不要用 markdown 列表、不要加标题、不要写代码。"
            "直接输出总结内容。"
        )
    return "\n".join(lines)


def summarize(members, feishu_messages, report_date) -> str | None:
    """调 DeepSeek 生成今日总结。失败返回 None。"""
    api_key = os.environ.get(_API_KEY_ENV, "").strip()
    if not api_key:
        logger.info("未配置 DEEPSEEK_API_KEY，跳过今日总结")
        return None

    base_url = os.environ.get(_BASE_URL_ENV, _DEFAULT_BASE_URL).rstrip("/")
    model = os.environ.get(_MODEL_ENV, _DEFAULT_MODEL)

    has_any = False
    for m in members:
        if getattr(m, "commits", None):
            has_any = True
            break
    if not has_any and not feishu_messages:
        logger.info("无 commit 也无群消息，跳过今日总结")
        return None

    prompt = _build_prompt(members, feishu_messages, report_date)

    try:
        with httpx.Client(base_url=base_url, timeout=60.0) as client:
            resp = client.post(
                "/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "你是一个专业的技术日报助手，擅长把零散的工作记录整理成简洁清晰的总结。",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 600,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = (
                (data.get("choices") or [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            if not content:
                logger.warning("DeepSeek 返回空总结")
                return None
            logger.info(f"今日总结生成成功（{len(content)} 字）")
            return content
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"今日总结生成失败: {exc}")
        return None