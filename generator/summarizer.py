"""用 LLM（DeepSeek）生成日报的各类分析。

包含：
  1. summarize               一句话结论
  2. extract_highlights      今日亮点 Top N
  3. extract_action_items    群讨论要点（决策 / 待办 / 问题）
  4. summarize_commits       commit diff 摘要
  5. compare_with_yesterday  相比昨天变化分析
  6. classify_work           commit 分类统计
  7. generate_risks          风险与待办
  8. generate_problems       问题与思考（新增，参考历史风格）
  9. generate_next_plan      明日计划（新增，参考历史风格）
"""

from __future__ import annotations

import json
import os
import re
import time
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


def _call_llm(prompt: str, *, system: str, max_tokens: int = 600,
              max_retries: int = 3) -> str | None:
    """调 LLM，带指数退避重试。"""
    api_key = os.environ.get(_API_KEY_ENV, "").strip()
    if not api_key:
        return None

    base_url = os.environ.get(_BASE_URL_ENV, _DEFAULT_BASE_URL).rstrip("/")
    model = os.environ.get(_MODEL_ENV, _DEFAULT_MODEL)

    last_err = None
    for attempt in range(1, max_retries + 1):
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
                if content:
                    return content
                logger.warning(f"LLM 返回空内容（第 {attempt}/{max_retries} 次）")
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            logger.warning(f"LLM 调用失败（第 {attempt}/{max_retries} 次）: {exc}")
            if attempt < max_retries:
                wait = 2 ** attempt  # 2s, 4s, 8s
                time.sleep(wait)
    if last_err:
        logger.error(f"LLM 调用最终失败: {last_err}")
    return None


def _parse_json(text: str, kind: str = "array"):
    """从 LLM 输出里提取 JSON。"""
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


def _format_history_reflections(reflections: list, max_days: int = 7) -> str:
    """把历史反思格式化成 prompt 里的"风格参考"。"""
    if not reflections:
        return ""
    items = []
    for r in reflections[-max_days:]:
        date_str = r.get("report_date", "")
        problems = r.get("problems") or []
        next_plan = r.get("next_plan") or []
        p_texts = [p.get("text", "") if isinstance(p, dict) else str(p)
                   for p in problems]
        n_texts = [p.get("text", "") if isinstance(p, dict) else str(p)
                   for p in next_plan]
        if p_texts or n_texts:
            block = [f"- {date_str}"]
            for t in p_texts[:3]:
                block.append(f"  · 问题：{t}")
            for t in n_texts[:3]:
                block.append(f"  · 计划：{t}")
            items.append("\n".join(block))
    return "\n".join(items)


# ============ 1. 一句话结论 ============

def summarize(members, feishu_messages, report_date) -> str | None:
    api_key = os.environ.get(_API_KEY_ENV, "").strip()
    if not api_key:
        logger.info("未配置 DEEPSEEK_API_KEY，跳过一句话结论")
        return None

    has_any = any(getattr(m, "commits", None) for m in members)
    if not has_any and not feishu_messages:
        return None

    data_prompt = _build_prompt(members, feishu_messages, report_date)
    has_msg = bool(feishu_messages)

    if has_msg and has_any:
        instruction = (
            "请根据以上信息，用中文写一段 2-3 句的『一句话结论』。"
            "要求："
            "① 结论先行，先说今天最重要的成果；"
            "② 说清楚进展（完成了什么、进入了什么状态）；"
            "③ 语气正式，像管理层汇报；"
            "④ 不要用 markdown、不要 emoji，直接输出。"
        )
    elif has_any:
        instruction = (
            "请根据以上信息，用中文写一段 2-3 句的『一句话结论』。"
            "结论先行，突出今天最重要的成果和进展；"
            "不要用 markdown、不要 emoji，直接输出。"
        )
    else:
        instruction = (
            "请根据以上信息，用中文写一段 2 句的『一句话结论』。"
            "概括群里讨论的主要内容；不要 emoji，直接输出。"
        )

    content = _call_llm(
        data_prompt + "\n" + instruction,
        system="你是一个专业的技术日报助手，擅长写结论先行的汇报。",
        max_tokens=500,
    )
    if content:
        logger.info(f"一句话结论生成成功（{len(content)} 字）")
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
        "要求："
        "① 每条 20-40 字；"
        "② 突出【做了什么功能 / 改了什么 bug / 加了什么能力】，不要罗列数字；"
        "③ 不要写『完成了工作』『代码提交完成』这类空话；"
        "④ 不要用 markdown、不要 emoji、不要加序号前缀；"
        "⑤ 严格输出 JSON 字符串数组，例如："
        '["日报结构重构为五段式周报格式", "新增工作建议和分类统计能力"]'
        "；⑥ 只输出 JSON。"
    )
    content = _call_llm(
        data_prompt + "\n" + instruction,
        system="你是一个专业的技术日报助手，擅长提炼关键成果。",
        max_tokens=400,
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
        "③ 每条要识别『相关人』（谁负责/谁参与），放 'people' 字段，"
        "如果全员相关就写 '全员'；"
        "④ 只提取真正有价值的，最多 5 条；无内容时返回空数组；"
        "⑤ 严格输出 JSON 数组，例如："
        '[{"type":"decision","text":"日报每天18:00自动发送","people":"全员"},'
        '{"type":"todo","text":"本周完成Web面板","people":"刘雨可"}]'
        "；⑥ 只输出 JSON，不要 emoji。"
    )
    content = _call_llm(
        data_prompt + "\n" + instruction,
        system="你是一个专业的技术团队助手，擅长从聊天记录里提炼要点。",
        max_tokens=500,
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
        people = str(item.get("people", "")).strip() or "-"
        if t in {"decision", "todo", "issue"} and text:
            result.append({"type": t, "text": text, "people": people})
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
            "③ 不要用 markdown、不要 emoji，直接输出一句话。"
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
    if not yesterday_payload:
        return None

    api_key = os.environ.get(_API_KEY_ENV, "").strip()
    if not api_key:
        logger.info("未配置 DEEPSEEK_API_KEY，跳过昨日对比")
        return None

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

    lines = ["【昨天】"]
    if y_commit_msgs:
        lines.append("代码提交：")
        for msg in y_commit_msgs[:15]:
            lines.append(f"- {msg}")
    if y_summary:
        lines.append(f"昨日结论：{y_summary}")
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
    if today_summary:
        lines.append(f"今日结论：{today_summary}")
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
        '{"summary": "一句话总结核心变化（≤60字）", '
        '"changes": ["变化要点1", "变化要点2", "变化要点3"], '
        '"trend": "功能推进"}'
        "；"
        "② summary：一句话概括核心变化；"
        "③ changes：2-3 条要点，每条 ≤ 40 字；"
        "④ trend：从这 5 个里选一个——"
        "'功能推进'、'修复完善'、'重构优化'、'文档维护'、'常规迭代'；"
        "⑤ 不要 emoji，只输出 JSON。"
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
        "yesterday": {"commits": y_commits_count, "additions": y_add, "deletions": y_del},
        "today": {"commits": t_commits_count, "additions": t_add, "deletions": t_del},
    }
    logger.info(f"相比昨天分析生成成功: trend={result['trend']}")
    return result


# ============ 6. commit 分类统计 ============

_CATEGORY_DEFS = [
    ("feat", "新功能"),
    ("fix", "修复"),
    ("refactor", "重构"),
    ("docs", "文档"),
    ("test", "测试"),
    ("chore", "配置"),
    ("style", "样式"),
    ("perf", "性能"),
    ("ci", "CI/CD"),
    ("build", "构建"),
    ("revert", "回滚"),
    ("cleanup", "清理"),
]


def _classify_commit_message(msg: str) -> str | None:
    if not msg:
        return None
    m = msg.strip().lower()
    m = re.sub(r"^([a-z]+)\s*\([^)]*\)\s*:", r"\1:", m)
    for prefix, _ in _CATEGORY_DEFS:
        if m.startswith(prefix + ":") or m.startswith(prefix + " "):
            return prefix
    if any(k in m for k in ["delete", "remove", "清理", "删除"]):
        return "cleanup"
    if any(k in m for k in ["update", "更新"]):
        return "_other"
    return None


def classify_work(members, commit_summaries: dict, report_date) -> dict:
    """按 commit type 分类。"""
    all_commits = [c for m in members for c in (getattr(m, "commits", None) or [])]
    if not all_commits:
        return {"categories": []}

    from collections import defaultdict
    bucket: dict[str, list] = defaultdict(list)
    for c in all_commits:
        msg = (getattr(c, "message", "") or "").strip()
        key = _classify_commit_message(msg)
        if key is None:
            key = "_other"
        bucket[key].append(c)

    total = len(all_commits)
    label_map = dict(_CATEGORY_DEFS)
    label_map["_other"] = "其他"
    label_map["cleanup"] = "清理"

    summary_lines = []
    for key, items in bucket.items():
        msgs = [(getattr(c, "message", "") or "").strip().splitlines()[0] for c in items]
        summary_lines.append(f"{key}: {msgs}")

    details_map = {}
    if len(bucket) >= 1 and total >= 1:
        prompt = (
            "以下是今天 commit 按类型的分组，请为每组生成一句简短的中文详情"
            "（不超过 25 字，说清楚做了什么）。\n\n"
            + "\n".join(summary_lines)
            + "\n\n要求："
            "① 严格输出 JSON 对象，key 是类别名，value 是详情字符串；"
            "② 详情要具体（说功能名 / 模块名），不要空话；"
            '③ 例如：{"refactor": "日报结构整体重写为五段式周报"}'
            "；④ 不要 emoji，只输出 JSON。"
        )
        content = _call_llm(
            prompt,
            system="你是一个代码分析助手，擅长用关键词概括一组改动。",
            max_tokens=300,
        )
        d = _parse_json(content or "", "object")
        if isinstance(d, dict):
            details_map = {str(k): str(v) for k, v in d.items()}

    categories = []
    for key, items in bucket.items():
        count = len(items)
        percent = round(count / total * 100)
        categories.append({
            "key": key,
            "label": label_map.get(key, key),
            "count": count,
            "percent": percent,
            "details": details_map.get(key, ""),
        })
    categories.sort(key=lambda x: -x["count"])

    logger.info(f"工作分类生成成功（{len(categories)} 类，共 {total} 个 commit）")
    return {"categories": categories}


# ============ 7. 风险与待办 ============

_BAD_FILE_PATTERNS = [
    (re.compile(r"(^|/)logs?/.*\.log$"), "日志文件", "加入 .gitignore：`logs/` 或 `*.log`"),
    (re.compile(r"\.log$"), "日志文件", "加入 .gitignore：`*.log`"),
    (re.compile(r"(^|/)\.env$"), "环境变量文件（密钥泄漏风险）", "加入 .gitignore：`.env`，并更换密钥"),
    (re.compile(r"__pycache__/"), "Python 缓存", "加入 .gitignore：`__pycache__/`"),
    (re.compile(r"\.pyc$"), "Python 编译文件", "加入 .gitignore：`*.pyc`"),
    (re.compile(r"data/.*\.db$"), "数据库文件", "加入 .gitignore：`data/*.db`"),
    (re.compile(r"\.(bak|tmp|swp)$"), "临时文件", "加入 .gitignore：`*.bak / *.tmp / *.swp`"),
    (re.compile(r"\.venv/|venv/"), "虚拟环境", "加入 .gitignore：`.venv/`"),
]


def _detect_bad_files(members) -> list[dict]:
    found = {}
    for m in members:
        for c in (getattr(m, "commits", None) or []):
            for fd in (getattr(c, "file_details", None) or []):
                fname = getattr(fd, "filename", "") or ""
                if not fname:
                    continue
                for pat, name, fix in _BAD_FILE_PATTERNS:
                    if pat.search(fname):
                        if name not in found:
                            found[name] = {"name": name, "fix": fix, "files": []}
                        found[name]["files"].append(fname)
    return list(found.values())


def generate_risks(
    members,
    feishu_messages,
    commit_summaries: dict,
    work_classification: dict,
    yesterday_payload: dict | None,
    report_date,
) -> list[dict]:
    """生成风险与待办。返回 [{"category": "...", "text": "..."}]"""
    api_key = os.environ.get(_API_KEY_ENV, "").strip()
    if not api_key:
        return []

    risks: list[dict] = []

    # 误提交文件检测
    bad_files = _detect_bad_files(members)
    for bf in bad_files:
        sample = "、".join(bf["files"][:3])
        if len(bf["files"]) > 3:
            sample += f" 等 {len(bf['files'])} 个"
        risks.append({
            "category": "数据质量",
            "text": f"检测到误提交的{bf['name']}（{sample}），{bf['fix']}",
        })

    # commit message 规范检测
    all_commits = [c for m in members for c in (getattr(m, "commits", None) or [])]
    bad_msgs = []
    for c in all_commits:
        msg = (getattr(c, "message", "") or "").strip().splitlines()[0]
        if not msg:
            continue
        key = _classify_commit_message(msg)
        if key is None or key == "_other":
            bad_msgs.append(msg)
    if len(bad_msgs) >= 2 or (all_commits and len(bad_msgs) / max(len(all_commits), 1) > 0.5):
        sample = "、".join(f"「{m}」" for m in bad_msgs[:3])
        risks.append({
            "category": "数据质量",
            "text": f"commit message 不规范（如 {sample}），"
                    f"建议统一使用 feat / fix / refactor 前缀，便于分类统计",
        })

    # LLM 补充
    if all_commits or feishu_messages:
        lines = [f"日期：{report_date.isoformat()}", ""]
        lines.append("【今日代码提交】")
        for c in all_commits:
            msg = (getattr(c, "message", "") or "").split("\n")[0].strip()
            files_n = int(getattr(c, "files_changed", 0) or 0)
            add = int(getattr(c, "additions", 0) or 0)
            dele = int(getattr(c, "deletions", 0) or 0)
            lines.append(f"- {msg}（{files_n} 个文件，+{add}/-{dele}）")
            for fd in (getattr(c, "file_details", None) or []):
                fd_add = int(getattr(fd, "additions", 0) or 0)
                if fd_add > 500:
                    lines.append(f"  · 大改动：{getattr(fd, 'filename', '?')} 单次 +{fd_add} 行")
        lines.append("")

        if feishu_messages:
            lines.append("【今日群消息】")
            for msg in feishu_messages[:10]:
                sender = getattr(msg, "sender_name", "") or "未知"
                text = (getattr(msg, "text", "") or "").strip()
                lines.append(f"- {sender}：{text}")
            lines.append("")

        instruction = (
            "请根据以上信息，识别今天的『风险与待办』（最多 4 条）。"
            "要求："
            "① 严格输出 JSON 数组，格式："
            '[{"category": "稳定性", "text": "..."}, '
            '{"category": "待办事项", "text": "..."}]'
            "；"
            "② category 从这些里选："
            "'稳定性'、'数据质量'、'待办事项'、'进度风险'；"
            "③ text 要具体、可执行，不超过 60 字；"
            "④ 没有风险就返回空数组；"
            "⑤ 不要 emoji，只输出 JSON。"
        )

        content = _call_llm(
            "\n".join(lines) + instruction,
            system="你是一个专业的技术团队顾问，擅长识别项目风险和待办。",
            max_tokens=500,
        )
        arr = _parse_json(content or "", "array")
        if isinstance(arr, list):
            for item in arr:
                if not isinstance(item, dict):
                    continue
                cat = str(item.get("category", "")).strip()
                text = str(item.get("text", "")).strip()
                if cat and text:
                    risks.append({"category": cat, "text": text})

    # 去重
    seen = set()
    unique = []
    for r in risks:
        key = r["text"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)

    logger.info(f"风险与待办生成成功（{len(unique)} 条）")
    return unique[:5]


# ============ 8. 问题与思考（新增） ============

def generate_problems(
    members,
    feishu_messages,
    action_items: list[dict],
    risks: list[dict],
    yesterday_payload: dict | None,
    report_date,
    *,
    recent_reflections: list[dict] | None = None,
    max_items: int = 5,
) -> list[dict]:
    """生成「问题与思考」。

    返回 [{"text": "...", "tag": "推测"}]。
    参考近 7 天用户真实反思的风格。
    """
    api_key = os.environ.get(_API_KEY_ENV, "").strip()
    if not api_key:
        return []

    all_commits = [c for m in members for c in (getattr(m, "commits", None) or [])]
    if not all_commits and not feishu_messages and not risks:
        return []

    lines = []
    lines.append(f"日期：{report_date.isoformat()}")
    lines.append("")

    lines.append("【今日代码提交】")
    for c in all_commits:
        msg = (getattr(c, "message", "") or "").split("\n")[0].strip()
        files_n = int(getattr(c, "files_changed", 0) or 0)
        add = int(getattr(c, "additions", 0) or 0)
        dele = int(getattr(c, "deletions", 0) or 0)
        lines.append(f"- {msg}（{files_n} 个文件，+{add}/-{dele}）")
    lines.append("")

    if feishu_messages:
        lines.append("【今日群消息】")
        for msg in feishu_messages[:10]:
            sender = getattr(msg, "sender_name", "") or "未知"
            text = (getattr(msg, "text", "") or "").strip()
            lines.append(f"- {sender}：{text}")
        lines.append("")

    if action_items:
        lines.append("【群讨论要点】")
        for item in action_items:
            lines.append(f"- [{item.get('type', '')}] {item.get('text', '')}")
        lines.append("")

    if risks:
        lines.append("【已识别的风险】")
        for r in risks:
            lines.append(f"- [{r.get('category', '')}] {r.get('text', '')}")
        lines.append("")

    if yesterday_payload:
        y_summary = yesterday_payload.get("summary", "")
        if y_summary:
            lines.append(f"【昨日结论】{y_summary}")
            lines.append("")

    # 历史风格参考
    history = _format_history_reflections(recent_reflections or [], max_days=7)
    if history:
        lines.append("【近 7 天用户真实反思（风格参考，不是今天的内容）】")
        lines.append(history)
        lines.append("")
        lines.append(
            "请模仿上述用户反思的写法风格（用词习惯、思考角度、"
            "喜欢说什么、不喜欢说什么），但内容必须是针对今天的情况。"
        )
        lines.append("")

    instruction = (
        f"请根据以上信息，生成今天 3-5 条『问题与思考』。"
        "要求："
        "① 严格输出 JSON 数组，格式："
        '[{"text": "...", "tag": "推测"}]'
        "；"
        "② 每条 30-80 字，说清楚『遇到了什么问题』或『有什么反思』；"
        "③ 语气：像写给自己的工作笔记，不要空话、不要口号；"
        "④ 优先从以下角度思考："
        "『今天哪里卡住了』『哪个决定做错了』『哪个技术点值得深挖』"
        "『数据质量哪里出了问题』；"
        "⑤ 不要 emoji，只输出 JSON。"
    )

    content = _call_llm(
        "\n".join(lines) + instruction,
        system="你是一个专业的技术人员，擅长写反思笔记，风格务实、不空话。",
        max_tokens=800,
    )
    arr = _parse_json(content or "", "array")
    if not isinstance(arr, list):
        return []

    result = []
    for item in arr:
        if isinstance(item, dict):
            text = str(item.get("text", "")).strip()
            if text:
                result.append({"text": text, "tag": "推测"})
        elif isinstance(item, str) and item.strip():
            result.append({"text": item.strip(), "tag": "推测"})

    logger.info(f"问题与思考生成成功（{len(result)} 条）")
    return result[:max_items]


# ============ 9. 明日计划（升级：参考历史风格） ============

def generate_next_plan(
    members,
    feishu_messages,
    action_items: list[dict],
    risks: list[dict],
    yesterday_payload: dict | None,
    report_date,
    *,
    recent_reflections: list[dict] | None = None,
    max_items: int = 4,
) -> list[dict]:
    """生成明日计划。

    返回 [{"text": "...", "tag": "推测"}]。
    参考近 7 天用户真实计划。
    """
    api_key = os.environ.get(_API_KEY_ENV, "").strip()
    if not api_key:
        return []

    all_commits = [c for m in members for c in (getattr(m, "commits", None) or [])]
    if not all_commits and not feishu_messages and not risks:
        return []

    lines = []
    lines.append(f"日期：{report_date.isoformat()}")
    lines.append("")

    lines.append("【今日工作】")
    for c in all_commits:
        msg = (getattr(c, "message", "") or "").split("\n")[0].strip()
        lines.append(f"- {msg}")
    lines.append("")

    if action_items:
        lines.append("【群里的待办和问题】")
        for item in action_items:
            lines.append(f"- [{item.get('type', '')}] {item.get('text', '')}")
        lines.append("")

    if risks:
        lines.append("【风险与待办】")
        for r in risks:
            lines.append(f"- [{r.get('category', '')}] {r.get('text', '')}")
        lines.append("")

    if yesterday_payload:
        y_summary = yesterday_payload.get("summary", "")
        if y_summary:
            lines.append(f"【昨日结论】{y_summary}")
            lines.append("")

    # 历史风格参考
    history = _format_history_reflections(recent_reflections or [], max_days=7)
    if history:
        lines.append("【近 7 天用户真实计划（风格参考）】")
        lines.append(history)
        lines.append("")
        lines.append(
            "请模仿上述用户写计划时的风格（用词习惯、颗粒度、"
            "喜欢写具体技术动作还是抽象目标），但内容必须是针对明天的。"
        )
        lines.append("")

    instruction = (
        f"请根据以上信息，生成 {max_items} 条明日计划。"
        "要求："
        "① 严格输出 JSON 数组，格式："
        '[{"text": "把工作建议渲染进日报", "tag": "推测"}]'
        "；"
        "② 每条不超过 30 字；"
        "③ 具体、可执行，说清楚『要做什么』，避免『继续推进』这种虚词；"
        "④ 优先级：先待办/风险，再新功能；"
        "⑤ 不要 emoji，只输出 JSON。"
    )

    content = _call_llm(
        "\n".join(lines) + instruction,
        system="你是一个专业的技术团队助手，擅长根据今天的工作规划明天。",
        max_tokens=500,
    )
    arr = _parse_json(content or "", "array")
    if not isinstance(arr, list):
        return []

    result = []
    for item in arr:
        if isinstance(item, dict):
            text = str(item.get("text", "")).strip()
            if text:
                result.append({"text": text, "tag": "推测"})
        elif isinstance(item, str) and item.strip():
            result.append({"text": item.strip(), "tag": "推测"})

    logger.info(f"明日计划生成成功（{len(result)} 条）")
    return result[:max_items]