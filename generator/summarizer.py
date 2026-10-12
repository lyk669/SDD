"""用 LLM（DeepSeek）生成日报的各类分析。"""

from __future__ import annotations

import json
import os
from datetime import datetime

import httpx

from shared.logger import get_logger

logger = get_logger("daily_report.generator.summarizer")

_API_KEY_ENV = "DEEPSEEK_API_KEY"
_BASE_URL_ENV = "DEEPSEEK_BASE_URL"
_MODEL_ENV = "DEEPSEEK_MODEL"

_DEFAULT_BASE_URL = "https://api.deepseek.com"
_DEFAULT_MODEL = "deepseek-chat"


def _to_local(dt: datetime) -> datetime:
    try:
        return dt.astimezone()
    except Exception:  # noqa: BLE001
        return dt


def _fmt_time(dt) -> str:
    if dt is None:
        return "--:--"
    return _to_local(dt).strftime("%H:%M")


def _call_llm(prompt: str, *, system: str, max_tokens: int = 600) -> str | None:
    api_key = os.environ.get(_API_KEY_ENV, "").strip()
    if not api_key:
        return None

    base_url = os.environ.get(_BASE_URL_ENV, _DEFAULT_BASE_URL).rstrip("/")
    model = os.environ.get(_MODEL_ENV, _DEFAULT_MODEL)

    try:
        with httpx.Client(base_url=base_url, timeout=90.0) as client:
            resp = client.post(
                "/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                    "max_tokens": max_tokens,
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
            return content or None
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"LLM 调用失败: {exc}")
        return None


def _parse_json(text: str, kind: str = "array"):
    """从 LLM 输出里提取 JSON（数组或对象）。"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:].strip()
    if kind == "array":
        start, end = t.find("["), t.rfind("]")
    else:
        start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(t[start:end + 1])
    except Exception:  # noqa: BLE001
        return None


def _build_prompt(members, feishu_messages, report_date) -> str:
    lines = [f"日期：{report_date.isoformat()}（北京时间）", ""]

    lines.append("【代码提交】")
    has_commit = False
    for m in members:
        for c in (getattr(m, "commits", None) or []):
            has_commit = True
            msg = (getattr(c, "message", "") or "").strip().splitlines()[0]
            add = int(getattr(c, "additions", 0) or 0)
            dele = int(getattr(c, "deletions", 0) or 0)
            files = int(getattr(c, "files_changed", 0) or 0)
            time_str = _fmt_time(getattr(c, "timestamp", None))
            lines.append(f"- [{time_str}] {msg} (+{add}/-{dele}, {files} 个文件)")
    if not has_commit:
        lines.append("（无）")
    lines.append("")

    lines.append("【群消息】")
    has_msg = False
    for msg in (feishu_messages or []):
        has_msg = True
        sender = getattr(msg, "sender_name", "") or "未知"
        text = (getattr(msg, "text", "") or "").replace("\n", " ").strip()
        time_str = _fmt_time(getattr(msg, "timestamp", None))
        lines.append(f"- [{time_str}] {sender}：{text}")
    if not has_msg:
        lines.append("（无）")
    lines.append("")

    return "\n".join(lines)


# ============ 1. 今日总结 ============

def summarize(members, feishu_messages, report_date) -> str | None:
    api_key = os.environ.get(_API_KEY_ENV, "").strip()
    if not api_key:
        logger.info("未配置 DEEPSEEK_API_KEY，跳过今日总结")
        return None

    has_any = any(getattr(m, "commits", None) for m in members)
    if not has_any and not feishu_messages:
        return None

    data_prompt = _build_prompt(members, feishu_messages, report_date)
    has_msg = bool(feishu_messages)

    if has_msg and has_any:
        instruction = (
            "请根据以上信息，用中文写一段 3-5 句的『今日工作总结』。"
            "要求：① 语气正式；② 先说代码提交，再说群消息讨论；"
            "③ 突出关键成果；"
            "④ 如果提到时间，请使用上面给出的时间（已经是北京时间）；"
            "⑤ 不要用 markdown；⑥ 直接输出。"
        )
    elif has_any:
        instruction = (
            "请根据以上信息，用中文写一段 2-4 句的『今日工作总结』。"
            "语气正式，突出完成的工作和进展；"
            "如果提到时间，请使用上面给出的时间（已经是北京时间）；"
            "不要用 markdown，直接输出。"
        )
    else:
        instruction = (
            "请根据以上信息，用中文写一段 2-3 句的『今日工作总结』。"
            "主要概括群里的讨论内容，不要用 markdown，直接输出。"
        )

    content = _call_llm(
        data_prompt + "\n" + instruction,
        system="你是一个专业的技术日报助手。",
        max_tokens=600,
    )
    if content:
        logger.info(f"今日总结生成成功（{len(content)} 字）")
    return content


# ============ 2. 今日亮点 ============

def extract_highlights(members, feishu_messages, report_date, top_n: int = 3) -> list[str]:
    api_key = os.environ.get(_API_KEY_ENV, "").strip()
    if not api_key:
        return []

    has_any = any(getattr(m, "commits", None) for m in members)
    if not has_any and not feishu_messages:
        return []

    data_prompt = _build_prompt(members, feishu_messages, report_date)
    instruction = (
        f"请从以上信息中提炼今天最重要的 {top_n} 条工作亮点。"
        "要求：① 每条不超过 30 个字；"
        "② 突出【做了什么功能 / 改了什么 bug / 加了什么能力】，"
        "而不是罗列数字；"
        "③ 不要写『完成了工作』『代码提交完成』这种空话；"
        "④ 不要用 markdown 语法、不要加序号前缀；"
        "⑤ 严格输出 JSON 字符串数组，例如："
        '["完成飞书群消息接入", "修复时区显示bug"]'
        "；⑥ 只输出 JSON。"
    )
    content = _call_llm(
        data_prompt + "\n" + instruction,
        system="你是一个专业的技术日报助手，擅长提炼关键成果。",
        max_tokens=300,
    )
    arr = _parse_json(content or "", "array")
    if isinstance(arr, list):
        result = [str(x).strip() for x in arr if str(x).strip()]
        logger.info(f"今日亮点生成成功（{len(result)} 条）")
        return result[:top_n]
    return []


# ============ 3. 群讨论要点 ============

def extract_action_items(members, feishu_messages, report_date, max_items: int = 5) -> list[dict]:
    api_key = os.environ.get(_API_KEY_ENV, "").strip()
    if not api_key or not feishu_messages:
        return []

    lines = [f"日期：{report_date.isoformat()}（北京时间）", "", "【群消息】"]
    for msg in feishu_messages:
        sender = getattr(msg, "sender_name", "") or "未知"
        text = (getattr(msg, "text", "") or "").replace("\n", " ").strip()
        time_str = _fmt_time(getattr(msg, "timestamp", None))
        lines.append(f"- [{time_str}] {sender}：{text}")
    data_prompt = "\n".join(lines)

    instruction = (
        "请从以上群消息中，提炼出【决策】【待办】【问题】三类要点。"
        "要求："
        "① 每条不超过 40 个字；"
        "② type 只能是 'decision'（决策/共识）、'todo'（待办/待完成）、"
        "'issue'（问题/风险/待验证）；"
        "③ 只提取真正有价值的，最多 5 条；无内容时返回空数组；"
        "④ 严格输出 JSON 数组，例如："
        '[{"type":"decision","text":"日报每天18:00自动发送"},'
        '{"type":"todo","text":"刘雨可：本周完成Web面板"}]'
        "；⑤ 只输出 JSON。"
    )
    content = _call_llm(
        data_prompt + "\n" + instruction,
        system="你是一个专业的技术团队助手，擅长从聊天记录里提炼要点。",
        max_tokens=400,
    )
    arr = _parse_json(content or "", "array")
    if not isinstance(arr, list):
        return []
    result = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        t = str(item.get("type", "")).strip().lower()
        text = str(item.get("text", "")).strip()
        if t in {"decision", "todo", "issue"} and text:
            result.append({"type": t, "text": text})
    if result:
        logger.info(f"群讨论要点生成成功（{len(result)} 条）")
    return result[:max_items]


# ============ 4. commit diff 摘要 ============

def summarize_commits(members, report_date, max_commits: int = 20) -> dict[str, str]:
    api_key = os.environ.get(_API_KEY_ENV, "").strip()
    if not api_key:
        return {}

    targets = []
    for m in members:
        for c in (getattr(m, "commits", None) or []):
            patch = (getattr(c, "patch_text", "") or "").strip()
            sha = getattr(c, "sha", "") or ""
            if patch and sha:
                targets.append(c)
    targets = targets[:max_commits]

    if not targets:
        logger.info("无 diff 数据，跳过 commit 摘要")
        return {}

    results: dict[str, str] = {}
    for c in targets:
        sha = c.sha
        msg = (getattr(c, "message", "") or "").strip().splitlines()[0]
        patch = c.patch_text

        prompt = (
            f"【Commit message】{msg}\n\n"
            f"【Diff】\n{patch}\n\n"
            "请用一句中文（不超过 60 字）概括这次提交具体做了什么改动。"
            "要求："
            "① 从 diff 内容出发，说人话，不要重复 commit message；"
            "② 突出『新增了什么』『修复了什么』『改动了什么逻辑』；"
            "③ 不要用 markdown，不要加前缀，直接输出一句话。"
        )
        content = _call_llm(
            prompt,
            system="你是一个代码审查助手，擅长用一句话概括代码改动。",
            max_tokens=200,
        )
        if content:
            results[sha] = content.replace("\n", " ").strip()

    logger.info(f"commit 摘要生成成功（{len(results)}/{len(targets)} 条）")
    return results


# ============ 5. 相比昨天的变化 ⭐ 新增 ============

def compare_with_yesterday(
    today_members,
    today_summary: str,
    today_highlights: list,
    yesterday_payload: dict | None,
    report_date,
) -> dict | None:
    """对比昨天和今天，生成变化分析。

    返回 {"summary": str, "changes": list[str], "trend": str} 或 None。
    """
    if not yesterday_payload:
        return None

    api_key = os.environ.get(_API_KEY_ENV, "").strip()
    if not api_key:
        logger.info("未配置 DEEPSEEK_API_KEY，跳过昨日对比")
        return None

    # 昨天
    y_summary = yesterday_payload.get("summary", "") or ""
    y_highlights = yesterday_payload.get("highlights", []) or []
    y_members = yesterday_payload.get("members", []) or []
    y_commit_msgs = []
    y_commits_count = 0
    y_add = y_del = 0
    for m in y_members:
        for c in m.get("commits", []):
            y_commits_count += 1
            msg = (c.get("message") or "").split("\n")[0].strip()
            if msg:
                y_commit_msgs.append(msg)
            y_add += int(c.get("additions", 0) or 0)
            y_del += int(c.get("deletions", 0) or 0)

    # 今天
    t_commit_msgs = []
    t_commits_count = 0
    t_add = t_del = 0
    for m in today_members:
        for c in (getattr(m, "commits", None) or []):
            t_commits_count += 1
            msg = (getattr(c, "message", "") or "").split("\n")[0].strip()
            if msg:
                t_commit_msgs.append(msg)
            t_add += int(getattr(c, "additions", 0) or 0)
            t_del += int(getattr(c, "deletions", 0) or 0)

    if not t_commit_msgs and not today_summary:
        return None

    lines = []
    lines.append("【昨天】")
    if y_commit_msgs:
        lines.append("代码提交：")
        for msg in y_commit_msgs[:15]:
            lines.append(f"- {msg}")
    else:
        lines.append("代码提交：（无）")
    if y_summary:
        lines.append(f"昨日总结：{y_summary}")
    if y_highlights:
        lines.append("昨日亮点：")
        for i, h in enumerate(y_highlights, 1):
            lines.append(f"{i}. {h}")
    lines.append(f"数字：{y_commits_count} 次提交 · +{y_add}/-{y_del}")
    lines.append("")

    lines.append("【今天】")
    if t_commit_msgs:
        lines.append("代码提交：")
        for msg in t_commit_msgs[:15]:
            lines.append(f"- {msg}")
    else:
        lines.append("代码提交：（无）")
    if today_summary:
        lines.append(f"今日总结：{today_summary}")
    if today_highlights:
        lines.append("今日亮点：")
        for i, h in enumerate(today_highlights, 1):
            lines.append(f"{i}. {h}")
    lines.append(f"数字：{t_commits_count} 次提交 · +{t_add}/-{t_del}")
    lines.append("")

    instruction = (
        "请对比今天和昨天，分析今天相比昨天有什么变化。"
        "要求："
        "① 严格输出 JSON 对象，格式："
        '{"summary": "一句话总结今天相比昨天的核心变化（≤50字）", '
        '"changes": ["变化要点1", "变化要点2", "变化要点3"], '
        '"trend": "功能推进"}'
        "；"
        "② summary：一句话概括核心变化；"
        "③ changes：2-3 条要点，每条不超过 40 字，说清楚"
        "『新增了什么功能』『改进了什么』『修复了什么』；"
        "④ trend：只能从这 5 个里选一个——"
        "'功能推进'（新增/扩展功能）、"
        "'修复完善'（主要修 bug / 优化）、"
        "'重构优化'（重构 / 性能 / 结构调整）、"
        "'文档维护'（文档 / 配置 / 杂项）、"
        "'常规迭代'（无明显变化）；"
        "⑤ 只输出 JSON，不要其它文字。"
    )

    content = _call_llm(
        "\n".join(lines) + instruction,
        system="你是一个专业的技术日报助手，擅长对比分析两天的工作变化。",
        max_tokens=400,
    )
    data = _parse_json(content or "", "object")
    if not isinstance(data, dict):
        return None

    result = {
        "summary": str(data.get("summary", "")).strip(),
        "changes": [str(x).strip() for x in (data.get("changes") or []) if str(x).strip()],
        "trend": str(data.get("trend", "")).strip() or "常规迭代",
        # 数字部分一起带上，方便渲染
        "yesterday": {
            "commits": y_commits_count,
            "additions": y_add,
            "deletions": y_del,
        },
        "today": {
            "commits": t_commits_count,
            "additions": t_add,
            "deletions": t_del,
        },
    }
    logger.info(f"相比昨天分析生成成功: trend={result['trend']}")
    return result