"""用 LLM（DeepSeek）生成日报的各类分析。

包含：
  1. summarize             今日总结段落
  2. extract_highlights    今日亮点 Top N
  3. extract_action_items  群讨论要点（决策 / 待办 / 问题）
  4. summarize_commits     commit diff 摘要（读代码说人话）
  5. compare_with_yesterday 相比昨天变化分析
  6. generate_suggestions  工作建议（3 条）
  7. classify_work         commit 分类统计
"""

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


# ---------- 通用工具 ----------

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
        with httpx.Client(base_url=base_url, timeout=120.0) as client:
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
    """从 LLM 输出里提取 JSON。kind: 'array' | 'object'"""
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
    """通用 prompt：把今天的 commit / 群消息整理成文本。"""
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
            "③ 突出关键成果，不要罗列每一条；"
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


# ============ 5. 相比昨天的变化 ============

def compare_with_yesterday(
    today_members,
    today_summary: str,
    today_highlights: list,
    yesterday_payload: dict | None,
    report_date,
) -> dict | None:
    """对比昨天和今天，生成变化分析。

    返回 {"summary": str, "changes": list[str], "trend": str,
          "yesterday": {...}, "today": {...}} 或 None。
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


# ============ 6. 工作建议 ⭐ 新增 ============

def generate_suggestions(
    members,
    feishu_messages,
    commit_summaries: dict,
    yesterday_payload: dict | None,
    report_date,
    max_items: int = 3,
) -> list[dict]:
    """生成工作建议。返回 [{"type": "🛠 代码质量", "text": "..."}]。"""
    api_key = os.environ.get(_API_KEY_ENV, "").strip()
    if not api_key:
        return []

    # 收集信息
    all_commits = [c for m in members for c in (getattr(m, "commits", None) or [])]
    if not all_commits and not feishu_messages:
        return []

    lines = []
    lines.append(f"日期：{report_date.isoformat()}（北京时间）")
    lines.append("")

    # commit 概要
    lines.append("【今日代码提交】")
    total_files = 0
    for c in all_commits:
        msg = (getattr(c, "message", "") or "").split("\n")[0].strip()
        files_n = int(getattr(c, "files_changed", 0) or 0)
        add = int(getattr(c, "additions", 0) or 0)
        dele = int(getattr(c, "deletions", 0) or 0)
        total_files += files_n
        lines.append(f"- {msg}（{files_n} 个文件，+{add}/-{dele}）")
        # 找出单文件改动最大的
        for fd in (getattr(c, "file_details", None) or []):
            fd_add = int(getattr(fd, "additions", 0) or 0)
            if fd_add > 500:
                lines.append(f"  ⚠️ {getattr(fd, 'filename', '?')} 单次 +{fd_add} 行")
    lines.append("")

    # 群消息概要
    if feishu_messages:
        lines.append("【今日群消息】")
        for msg in feishu_messages[:10]:
            sender = getattr(msg, "sender_name", "") or "未知"
            text = (getattr(msg, "text", "") or "").strip()
            lines.append(f"- {sender}：{text}")
        lines.append("")

    # 昨天的情况
    if yesterday_payload:
        y_members = yesterday_payload.get("members", []) or []
        y_commits = sum(len(m.get("commits", [])) for m in y_members)
        lines.append(f"【昨天情况】{y_commits} 次提交")
        lines.append("")

    instruction = (
        f"请根据以上信息，提出 {max_items} 条今天的工作建议。"
        "要求："
        "① 严格输出 JSON 数组，格式："
        '[{"type": "🛠 代码质量", "text": "..."}, '
        '{"type": "⚠️ 待办", "text": "..."}, '
        '{"type": "📝 内容输出", "text": "..."}]'
        "；"
        "② type 从这几个里选："
        "'🛠 代码质量'、'⚠️ 待办'、'📝 内容输出'、'🎯 进度'、'🔒 风险'；"
        "③ text 要具体、可执行，不超过 50 字；"
        "④ 不要空话，要针对今天的数据给出具体建议；"
        "⑤ 如果今天没什么可建议的，返回空数组；"
        "⑥ 只输出 JSON，不要其它文字。"
    )

    content = _call_llm(
        "\n".join(lines) + "\n" + instruction,
        system="你是一个专业的技术团队顾问，擅长根据工作数据给出具体可执行的改进建议。",
        max_tokens=500,
    )
    arr = _parse_json(content or "", "array")
    if not isinstance(arr, list):
        return []

    result = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        t = str(item.get("type", "")).strip()
        text = str(item.get("text", "")).strip()
        if t and text:
            result.append({"type": t, "text": text})
    if result:
        logger.info(f"工作建议生成成功（{len(result)} 条）")
    return result[:max_items]


# ============ 7. commit 分类统计 ⭐ 新增 ============

_CATEGORY_DEFS = [
    ("feat", "🆕 新增功能"),
    ("fix", "🐛 Bug 修复"),
    ("refactor", "♻️ 代码重构"),
    ("docs", "📝 文档更新"),
    ("test", "🧪 测试"),
    ("chore", "🔧 配置调整"),
    ("style", "🎨 样式"),
    ("perf", "⚡ 性能优化"),
    ("ci", "🚀 CI/CD"),
    ("build", "📦 构建"),
    ("revert", "⏪ 回滚"),
]


def _classify_commit_message(msg: str) -> str | None:
    """从 commit message 解析类别。返回类别 key（如 'feat'）。"""
    if not msg:
        return None
    import re
    m = msg.strip().lower()
    m = re.sub(r"^([a-z]+)\s*\([^)]*\)\s*:", r"\1:", m)
    for prefix, _ in _CATEGORY_DEFS:
        if m.startswith(prefix + ":") or m.startswith(prefix + " "):
            return prefix
    return None


def classify_work(members, commit_summaries: dict, report_date) -> dict:
    """按 commit type 分类今天的提交。

    返回：
      {"categories": [
          {"key": "feat", "label": "🆕 新增功能", "count": 3, "percent": 60,
           "details": "飞书采集、LLM 总结、diff 摘要"},
          ...
      ]}
    """
    all_commits = [c for m in members for c in (getattr(m, "commits", None) or [])]
    if not all_commits:
        return {"categories": []}

    # 统计
    from collections import defaultdict
    bucket: dict[str, list] = defaultdict(list)
    unclassified = 0

    for c in all_commits:
        msg = (getattr(c, "message", "") or "").strip()
        key = _classify_commit_message(msg)
        if key is None:
            unclassified += 1
            continue
        bucket[key].append(c)

    total = len(all_commits)

    # 用 LLM 给每类生成"详情"
    label_map = dict(_CATEGORY_DEFS)
    categories = []

    # 如果有未分类的，加一类
    if unclassified > 0:
        label_map["_other"] = "📌 其他"
        bucket["_other"] = [c for c in all_commits
                            if _classify_commit_message(
                                (getattr(c, "message", "") or "").strip()) is None]

    # 只对有用的类调 LLM 生成详情（一次调用批量处理）
    summary_lines = []
    for key, items in bucket.items():
        msgs = []
        for c in items:
            msg = (getattr(c, "message", "") or "").strip().splitlines()[0]
            msgs.append(msg)
        summary_lines.append(f"{key}: {msgs}")

    details_map = {}
    if len(bucket) > 1 or total <= 5:
        # 只有 > 1 类时才调 LLM 生成详情（否则就是唯一类，没必要）
        prompt = (
            "以下是今天 commit 按类型的分组，请为每组生成一句简短的中文详情"
            "（不超过 25 字，概括这组做了什么），用顿号分隔关键词。\n\n"
            + "\n".join(summary_lines)
            + "\n\n要求："
            "① 严格输出 JSON 对象，key 是类别名，value 是详情字符串；"
            "② 详情要具体（说功能名 / bug 名 / 模块名），不要空话；"
            '③ 例如：{"feat": "飞书采集、LLM 总结、diff 摘要"}'
            "；④ 只输出 JSON。"
        )
        content = _call_llm(
            prompt,
            system="你是一个代码分析助手，擅长用关键词概括一组改动。",
            max_tokens=300,
        )
        d = _parse_json(content or "", "object")
        if isinstance(d, dict):
            details_map = {str(k): str(v) for k, v in d.items()}

    for key, items in bucket.items():
        label = label_map.get(key, key)
        count = len(items)
        percent = round(count / total * 100)
        details = details_map.get(key, "")
        categories.append({
            "key": key,
            "label": label,
            "count": count,
            "percent": percent,
            "details": details,
        })

    # 按数量降序
    categories.sort(key=lambda x: -x["count"])

    logger.info(f"工作分类生成成功（{len(categories)} 类，共 {total} 个 commit）")
    return {"categories": categories}