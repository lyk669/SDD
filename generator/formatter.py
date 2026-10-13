"""日报生成器 — 输出 Markdown + HTML（企业周报风格 v1.3）。

结构：
  一、核心指标（表格 + 一句话摘要）
  二、工作明细（提交记录 + 工作分类）
  三、变化分析（昨日回顾 + 今日对比 + 趋势判断）
  四、飞书群消息
  五、工作建议
  尾部元信息
"""

from __future__ import annotations

import html as _html
from dataclasses import dataclass
from datetime import date, datetime

from shared.logger import get_logger

logger = get_logger("daily_report.generator.formatter")


@dataclass
class Report:
    date: date
    team_name: str
    markdown: str
    html: str
    generated_at: datetime


_ICON_MAP = {
    ".py": "🐍", ".pyi": "🐍",
    ".js": "📜", ".mjs": "📜", ".cjs": "📜",
    ".ts": "📜", ".tsx": "📜", ".jsx": "📜",
    ".md": "📄", ".rst": "📄", ".txt": "📄",
    ".yaml": "⚙️", ".yml": "⚙️", ".toml": "⚙️", ".ini": "⚙️", ".cfg": "⚙️",
    ".json": "🔧",
    ".html": "🌐", ".htm": "🌐", ".css": "🎨", ".scss": "🎨", ".less": "🎨",
    ".sh": "💻", ".bash": "💻", ".ps1": "💻", ".bat": "💻",
    ".sql": "🗃️",
    ".png": "🖼️", ".jpg": "🖼️", ".jpeg": "🖼️", ".gif": "🖼️", ".svg": "🖼️",
    ".go": "🐹", ".rs": "🦀", ".java": "☕", ".kt": "🅺",
    ".c": "🔵", ".cpp": "🔵", ".h": "🔵", ".hpp": "🔵",
    ".php": "🐘", ".rb": "💎", ".swift": "🦅",
    ".lock": "🔒", ".gitignore": "🙈",
}

_STATUS_CN = {
    "added": "🆕 新增", "modified": "✏️ 修改",
    "removed": "🗑 删除", "deleted": "🗑 删除", "renamed": "🔀 重命名",
}

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_COMMIT_TYPE_MAP = {
    "feat": ("🆕", "新功能"), "feature": ("🆕", "新功能"),
    "fix": ("🐛", "修复"), "bugfix": ("🐛", "修复"), "hotfix": ("🐛", "紧急修复"),
    "docs": ("📝", "文档"), "doc": ("📝", "文档"),
    "refactor": ("♻️", "重构"),
    "test": ("🧪", "测试"), "tests": ("🧪", "测试"),
    "chore": ("🔧", "配置"),
    "style": ("🎨", "样式"),
    "perf": ("⚡", "性能"),
    "ci": ("🚀", "CI"), "build": ("📦", "构建"),
    "revert": ("⏪", "回滚"),
}

_ACTION_LABEL = {"decision": ("✅", "决策"), "todo": ("📌", "待办"), "issue": ("⚠️", "问题")}

_TREND_ICON = {
    "功能推进": "📈", "修复完善": "🔧", "重构优化": "♻️",
    "文档维护": "📝", "常规迭代": "🔁",
}


# ---------- 工具函数 ----------

def _commit_type_tag(message: str) -> str:
    if not message:
        return ""
    import re
    m = message.strip().lower()
    m = re.sub(r"^([a-z]+)\s*\([^)]*\)\s*:", r"\1:", m)
    for prefix, (icon, cn) in _COMMIT_TYPE_MAP.items():
        if m.startswith(prefix + ":") or m.startswith(prefix + " "):
            return f"{icon} {cn}"
    return ""


def _file_icon(filename: str) -> str:
    name = (filename or "").lower()
    for suffix, icon in _ICON_MAP.items():
        if name.endswith(suffix):
            return icon
    return "📄"


def _status_cn(status: str) -> str:
    return _STATUS_CN.get((status or "").lower(), "✏️ 修改")


def _to_local(dt: datetime) -> datetime:
    try:
        return dt.astimezone()
    except Exception:  # noqa: BLE001
        return dt


def _time_slot(dt: datetime) -> str:
    local_dt = _to_local(dt)
    h = local_dt.hour
    if h < 12:
        return "morning"
    if h < 18:
        return "afternoon"
    return "evening"


def _fmt_time(dt) -> str:
    if dt is None:
        return "--:--"
    return _to_local(dt).strftime("%H:%M")


def _fmt_datetime(dt) -> str:
    if dt is None:
        return "---- --:--"
    return _to_local(dt).strftime("%Y-%m-%d %H:%M")


def _fmt_num(n) -> str:
    """千分位：1882 → 1,882"""
    try:
        return f"{int(n):,}"
    except Exception:  # noqa: BLE001
        return str(n)


def _short_sha(sha) -> str:
    return (sha or "-------")[:7]


def _commit_link(commit) -> str:
    url = getattr(commit, "url", "") or ""
    if url:
        return url
    repo = getattr(commit, "repo", "") or ""
    sha = getattr(commit, "sha", "") or ""
    if repo and sha:
        return f"https://github.com/{repo}/commit/{sha}"
    return ""


def _commit_files(commit) -> list:
    details = getattr(commit, "file_details", None) or []
    if details:
        return details
    files = getattr(commit, "files", None) or []

    class _Fallback:
        def __init__(self, name):
            self.filename = name
            self.status = "modified"
            self.additions = 0
            self.deletions = 0
    return [_Fallback(str(f)) for f in files]


def _collect_files_by_status(all_commits: list, target_statuses: set) -> list:
    seen: set = set()
    result: list = []
    for c in all_commits:
        for fd in _commit_files(c):
            st = (getattr(fd, "status", "") or "").lower()
            if st not in target_statuses:
                continue
            fname = getattr(fd, "filename", "") or ""
            if not fname or fname in seen:
                continue
            seen.add(fname)
            result.append({
                "filename": fname,
                "additions": int(getattr(fd, "additions", 0) or 0),
                "deletions": int(getattr(fd, "deletions", 0) or 0),
                "status": st,
            })
    result.sort(key=lambda x: x["filename"])
    return result


def _sort_messages(messages: list) -> list:
    return sorted(
        messages or [],
        key=lambda x: getattr(x, "timestamp", None) or datetime.min,
    )


def _delta_str(cur: int, prev) -> tuple[str, str]:
    """返回 (环比字符串, 方向)。方向：up / down / same / none"""
    if prev is None:
        return ("—", "none")
    diff = cur - prev
    if prev == 0:
        if cur == 0:
            return ("—", "same")
        return (f"↑ {cur}", "up")
    pct = diff / prev * 100
    if abs(diff) < 1e-9:
        return ("—", "same")
    sign = "↑" if diff > 0 else "↓"
    direction = "up" if diff > 0 else "down"
    return (f"{sign} {abs(pct):.1f}%", direction)


def _compute_metrics(
    members: list,
    feishu_messages: list,
    yesterday_stats: dict | None,
    feishu_stats: dict,
) -> dict:
    """计算核心指标（今日 + 昨日 + 环比）。"""
    all_commits = [c for m in members for c in (getattr(m, "commits", []) or [])]
    total_files = sum(int(getattr(c, "files_changed", 0) or 0) for c in all_commits)
    total_add = sum(int(getattr(c, "additions", 0) or 0) for c in all_commits)
    total_del = sum(int(getattr(c, "deletions", 0) or 0) for c in all_commits)
    net = total_add - total_del
    kept = int(feishu_stats.get("kept", len(feishu_messages)) or 0)

    y = yesterday_stats or {}
    today = {
        "commits": len(all_commits),
        "files": total_files,
        "additions": total_add,
        "deletions": total_del,
        "net": net,
        "msgs": kept,
    }
    yest = None
    if yesterday_stats:
        yest = {
            "commits": int(y.get("commits", 0) or 0),
            "files": int(y.get("files", 0) or 0),
            "additions": int(y.get("additions", 0) or 0),
            "deletions": int(y.get("deletions", 0) or 0),
            "net": int(y.get("additions", 0) or 0) - int(y.get("deletions", 0) or 0),
            "msgs": int(y.get("msgs", 0) or 0) if "msgs" in y else None,
        }
    return {"today": today, "yesterday": yest}


# ---------- Markdown ----------

def _render_markdown(
    members: list,
    report_date: date,
    team_name: str,
    events: list | None,
    feishu_messages: list | None,
    summary: str | None,
    generated_at: datetime,
    feishu_stats: dict | None = None,
    yesterday_stats: dict | None = None,
    highlights: list[str] | None = None,
    action_items: list[dict] | None = None,
    commit_summaries: dict[str, str] | None = None,
    comparison: dict | None = None,
    suggestions: list[dict] | None = None,
    work_classification: dict | None = None,
    issue_number: int = 0,
) -> str:
    events = events or []
    feishu_messages = feishu_messages or []
    feishu_stats = feishu_stats or {}
    yesterday_stats = yesterday_stats or {}
    highlights = highlights or []
    action_items = action_items or []
    commit_summaries = commit_summaries or {}
    suggestions = suggestions or []
    summary = (summary or "").strip()

    all_commits = [c for m in members for c in (getattr(m, "commits", []) or [])]
    total_commits = len(all_commits)
    repos = sorted({getattr(c, "repo", "") for c in all_commits if getattr(c, "repo", "")})
    weekday_cn = _WEEKDAY_CN[report_date.weekday()]

    kept = int(feishu_stats.get("kept", len(feishu_messages)) or 0)
    skipped = int(feishu_stats.get("skipped", 0) or 0)
    total_msgs = int(feishu_stats.get("total", kept + skipped) or 0)

    # 指标
    metrics = _compute_metrics(members, feishu_messages, yesterday_stats, feishu_stats)
    today = metrics["today"]
    yest = metrics["yesterday"]

    lines: list[str] = []

    # ═══════ 标题 ═══════
    lines.append(f"# {team_name}")
    issue_str = f" · 第 {issue_number} 期" if issue_number else ""
    lines.append(f"## {report_date.isoformat()}（{weekday_cn}）{issue_str}")
    lines.append("")
    lines.append(f"> 项目：智能日报生成器 · 负责人：刘雨可  ")
    lines.append(f"> 周期：{report_date.isoformat()} 00:00 ~ {_fmt_time(generated_at)}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ═══════ 一、核心指标 ═══════
    lines.append("## 一、核心指标")
    lines.append("")
    lines.append("| 指标 | 今日 | 昨日 | 环比 |")
    lines.append("|:-----|:----:|:----:|:----:|")

    def row(label, key, fmt="num"):
        cur_v = today.get(key, 0)
        y_v = yest.get(key) if yest else None
        cur_s = _fmt_num(cur_v) if fmt == "num" else f"{cur_v}"
        y_s = _fmt_num(y_v) if (y_v is not None and fmt == "num") else ("—" if y_v is None else f"{y_v}")
        delta_s, _ = _delta_str(cur_v, y_v)
        return f"| {label} | {cur_s} | {y_s} | {delta_s} |"

    lines.append(row("代码提交", "commits"))
    lines.append(row("涉及文件", "files"))
    lines.append(row("新增代码", "additions"))
    lines.append(row("删除代码", "deletions"))
    lines.append(row("净变更", "net"))
    lines.append(row("群消息", "msgs"))
    lines.append("")

    # 一句话摘要
    if summary:
        lines.append("**一句话摘要**  ")
        lines.append(f"> {summary}")
        lines.append("")

    lines.append("---")
    lines.append("")

    # ═══════ 二、工作明细 ═══════
    lines.append("## 二、工作明细")
    lines.append("")

    if total_commits > 0:
        lines.append("### 2.1 提交记录")
        lines.append("")
        idx = 0
        for m in members:
            member_commits = [c for c in (getattr(m, "commits", []) or [])]
            if not member_commits:
                continue
            for c in sorted(member_commits,
                            key=lambda x: getattr(x, "timestamp", None) or datetime.min):
                idx += 1
                ts = _fmt_time(getattr(c, "timestamp", None))
                msg = (getattr(c, "message", "") or "").strip().splitlines()[0]
                tag = _commit_type_tag(msg)
                tag_str = f" · {tag}" if tag else ""
                sha = _short_sha(getattr(c, "sha", ""))
                additions = int(getattr(c, "additions", 0) or 0)
                deletions = int(getattr(c, "deletions", 0) or 0)
                files_n = int(getattr(c, "files_changed", 0) or 0)
                ai_summary = commit_summaries.get(getattr(c, "sha", ""), "")
                url = _commit_link(c)

                lines.append(f"#### {idx}. {ts} · {msg}{tag_str}")
                lines.append("")
                lines.append("| 项 | 内容 |")
                lines.append("|:---|:-----|")
                lines.append(f"| Commit | `{sha}` |")
                lines.append(f"| 变更 | **+{_fmt_num(additions)} / -{_fmt_num(deletions)}** · {files_n} 个文件 |")
                if ai_summary:
                    lines.append(f"| 摘要 | {ai_summary} |")
                if url:
                    lines.append(f"| 链接 | [查看提交]({url}) |")
                lines.append("")

                flist = _commit_files(c)
                if flist:
                    lines.append("**涉及文件**")
                    lines.append("")
                    lines.append("| 文件 | 变更 | 状态 |")
                    lines.append("|:-----|:----:|:----:|")
                    for fd in flist:
                        fname = getattr(fd, "filename", "") or "?"
                        add = int(getattr(fd, "additions", 0) or 0)
                        dele = int(getattr(fd, "deletions", 0) or 0)
                        icon = _file_icon(fname)
                        status_cn = _status_cn(getattr(fd, "status", "modified"))
                        lines.append(
                            f"| {icon} `{fname}` | +{add} / -{dele} | {status_cn} |"
                        )
                    lines.append("")

        # 2.2 工作分类
        if work_classification and work_classification.get("categories"):
            lines.append("### 2.2 工作分类")
            lines.append("")
            lines.append("| 类型 | 数量 | 占比 | 详情 |")
            lines.append("|:-----|:----:|:----:|:-----|")
            for cat in work_classification["categories"]:
                if cat.get("count", 0) == 0:
                    continue
                lines.append(
                    f"| {cat.get('label', '')} | {cat.get('count', 0)} "
                    f"| {cat.get('percent', 0)}% | {cat.get('details', '')} |"
                )
            lines.append("")

        # 新增/删除文件一览（附录）
        new_files = _collect_files_by_status(all_commits, {"added"})
        del_files = _collect_files_by_status(all_commits, {"removed", "deleted"})
        if new_files:
            lines.append(f"**📤 本次新增文件（{len(new_files)} 个）**")
            lines.append("")
            lines.append("| 文件 | 变更 |")
            lines.append("|:-----|:----:|")
            for nf in new_files:
                icon = _file_icon(nf["filename"])
                lines.append(f"| {icon} `{nf['filename']}` | +{nf['additions']} / -{nf['deletions']} |")
            lines.append("")
        if del_files:
            lines.append(f"**🗑 本次删除文件（{len(del_files)} 个）**")
            lines.append("")
            lines.append("| 文件 | 变更 |")
            lines.append("|:-----|:----:|")
            for df in del_files:
                icon = _file_icon(df["filename"])
                lines.append(f"| {icon} `{df['filename']}` | +{df['additions']} / -{df['deletions']} |")
            lines.append("")
    else:
        lines.append("_本日无代码提交记录。_")
        lines.append("")

    lines.append("---")
    lines.append("")

    # ═══════ 三、变化分析 ═══════
    lines.append("## 三、变化分析")
    lines.append("")

    # 3.1 昨日回顾
    if yesterday_stats:
        y_date = yesterday_stats.get("date", "")
        lines.append(f"### 3.1 昨日回顾（{y_date}）")
        lines.append("")
        y_highlights = []
        if comparison:
            # 从 comparison 里读昨日亮点不容易，改为读 yesterday_payload（由 main 传入）
            pass
        # 用 today 的 highlights 反向展示不了昨日，这里直接用"昨日数字"
        y_c = int(yesterday_stats.get("commits", 0) or 0)
        y_f = int(yesterday_stats.get("files", 0) or 0)
        y_a = int(yesterday_stats.get("additions", 0) or 0)
        y_d = int(yesterday_stats.get("deletions", 0) or 0)
        lines.append("| 指标 | 数值 |")
        lines.append("|:-----|:----:|")
        lines.append(f"| 代码提交 | {y_c} 次 |")
        lines.append(f"| 涉及文件 | {y_f} 个 |")
        lines.append(f"| 代码变更 | +{_fmt_num(y_a)} / -{_fmt_num(y_d)} |")
        lines.append("")

    # 3.2 今日对比
    if comparison:
        y = comparison.get("yesterday", {}) or {}
        t = comparison.get("today", {}) or {}
        yc = int(y.get("commits", 0) or 0)
        tc = int(t.get("commits", 0) or 0)
        trend = comparison.get("trend", "常规迭代")
        c_summary = (comparison.get("summary") or "").strip()
        changes = comparison.get("changes") or []

        lines.append("### 3.2 今日对比")
        lines.append("")
        lines.append("| 对比项 | 昨日 | 今日 |")
        lines.append("|:-------|:----:|:----:|")
        lines.append(f"| 提交次数 | {yc} | {tc} |")
        lines.append(f"| 侧重点 | — | — |")
        lines.append("")

        if changes:
            for ch in changes:
                ch = (ch or "").strip()
                if ch:
                    lines.append(f"- {ch}")
            lines.append("")

        # 3.3 趋势判断
        lines.append("### 3.3 趋势判断")
        lines.append("")
        trend_icon = _TREND_ICON.get(trend, "🔁")
        lines.append(f"> {trend_icon} **{trend}**  ")
        if c_summary:
            lines.append(f"> {c_summary}")
        lines.append("")
    elif yesterday_stats:
        lines.append("_（今日对比数据不足）_")
        lines.append("")

    lines.append("---")
    lines.append("")

    # ═══════ 四、飞书群消息 ═══════
    lines.append("## 四、飞书群消息")
    lines.append("")
    if feishu_messages:
        lines.append("| 时间 | 发言人 | 内容 |")
        lines.append("|:----:|:------:|:-----|")
        for msg in _sort_messages(feishu_messages):
            ts = _fmt_time(getattr(msg, "timestamp", None))
            sender = getattr(msg, "sender_name", "") or "未知"
            text = (getattr(msg, "text", "") or "").replace("\n", " ").replace("|", "\\|")
            lines.append(f"| {ts} | {sender} | {text} |")
        lines.append("")
    else:
        lines.append("_本日无有效群消息。_")
        lines.append("")

    if total_msgs or skipped:
        lines.append(f"> 本日共收到 {total_msgs} 条消息，保留 {kept} 条，"
                     f"过滤 {skipped} 条（撤回 / @某人 / 太短 / 系统消息）。")
        lines.append("")

    # 群讨论要点
    if action_items:
        lines.append("**群讨论要点**")
        lines.append("")
        lines.append("| 类型 | 内容 |")
        lines.append("|:----:|:-----|")
        for item in action_items:
            icon, label = _ACTION_LABEL.get(item.get("type", ""), ("·", ""))
            text = (item.get("text", "") or "").strip()
            if text:
                lines.append(f"| {icon} {label} | {text} |")
        lines.append("")

    lines.append("---")
    lines.append("")

    # ═══════ 五、工作建议 ═══════
    lines.append("## 五、工作建议")
    lines.append("")
    if suggestions:
        lines.append("| # | 类型 | 建议 |")
        lines.append("|:-:|:----:|:-----|")
        for i, s in enumerate(suggestions, 1):
            stype = s.get("type", "建议")
            stext = s.get("text", "")
            lines.append(f"| {i} | {stype} | {stext} |")
        lines.append("")
    else:
        lines.append("_本日无特别建议。_")
        lines.append("")

    lines.append("---")
    lines.append("")

    # ═══════ 尾部 ═══════
    lines.append(f"**数据来源**　GitHub · 飞书开放平台  ")
    lines.append(f"**采集窗口**　{report_date.isoformat()} 00:00 ~ {_fmt_time(generated_at)}（北京时间）  ")
    lines.append(f"**生成时间**　{_fmt_datetime(generated_at)}  ")
    lines.append(f"**文档版本**　v1.3 · 由智能日报生成器自动生成")

    return "\n".join(lines)


# ---------- HTML ----------

def _render_html(
    members: list,
    report_date: date,
    team_name: str,
    events: list | None,
    feishu_messages: list | None,
    summary: str | None,
    generated_at: datetime,
    feishu_stats: dict | None = None,
    yesterday_stats: dict | None = None,
    highlights: list[str] | None = None,
    action_items: list[dict] | None = None,
    commit_summaries: dict[str, str] | None = None,
    comparison: dict | None = None,
    suggestions: list[dict] | None = None,
    work_classification: dict | None = None,
    issue_number: int = 0,
) -> str:
    events = events or []
    feishu_messages = feishu_messages or []
    feishu_stats = feishu_stats or {}
    yesterday_stats = yesterday_stats or {}
    highlights = highlights or []
    action_items = action_items or []
    commit_summaries = commit_summaries or {}
    suggestions = suggestions or []
    summary = (summary or "").strip()

    def esc(s) -> str:
        return _html.escape(str(s or ""))

    all_commits = [c for m in members for c in (getattr(m, "commits", []) or [])]
    total_commits = len(all_commits)
    repos = sorted({getattr(c, "repo", "") for c in all_commits if getattr(c, "repo", "")})
    weekday_cn = _WEEKDAY_CN[report_date.weekday()]

    kept = int(feishu_stats.get("kept", len(feishu_messages)) or 0)
    skipped = int(feishu_stats.get("skipped", 0) or 0)
    total_msgs = int(feishu_stats.get("total", kept + skipped) or 0)

    metrics = _compute_metrics(members, feishu_messages, yesterday_stats, feishu_stats)
    today = metrics["today"]
    yest = metrics["yesterday"]

    # 样式常量
    h2_style = ("color:#1a237e;border-bottom:2px solid #4a6cf7;padding-bottom:6px;"
                "margin-top:32px;margin-bottom:16px;font-size:20px;")
    h3_style = ("color:#283593;margin-top:22px;margin-bottom:10px;font-size:16px;")
    h4_style = ("color:#3949ab;margin-top:16px;margin-bottom:8px;font-size:14px;")
    table_style = "border-collapse:collapse;width:100%;font-size:13px;margin:8px 0;"
    th_style = ("background:#f0f3ff;color:#1a237e;padding:6px 10px;"
                "text-align:left;border:1px solid #d0d7f0;font-weight:600;")
    th_c_style = th_style.replace("text-align:left", "text-align:center")
    td_style = "padding:5px 10px;border:1px solid #e0e5f5;"
    td_c_style = td_style.replace("padding:5px 10px", "padding:5px 10px;text-align:center")
    quote_style = ("background:#fff8e1;border-left:4px solid #f59e0b;"
                   "padding:10px 14px;margin:10px 0;border-radius:4px;")

    def table(headers: list, rows: list[list[str]], aligns: list[str] | None = None) -> str:
        if not headers:
            return ""
        aligns = aligns or ["left"] * len(headers)
        parts = [f"<table style='{table_style}'>", "<thead><tr>"]
        for h, a in zip(headers, aligns):
            st = th_c_style if a == "center" else th_style
            parts.append(f"<th style='{st}'>{h}</th>")
        parts.append("</tr></thead><tbody>")
        for row in rows:
            parts.append("<tr>")
            for i, cell in enumerate(row):
                a = aligns[i] if i < len(aligns) else "left"
                st = td_c_style if a == "center" else td_style
                parts.append(f"<td style='{st}'>{cell}</td>")
            parts.append("</tr>")
        parts.append("</tbody></table>")
        return "".join(parts)

    def delta_html(cur: int, prev) -> str:
        if prev is None:
            return "<span style='color:#999;'>—</span>"
        diff = cur - prev
        if abs(diff) < 1e-9:
            return "<span style='color:#999;'>—</span>"
        if prev == 0:
            color = "#2e7d32" if diff > 0 else "#c62828"
            return f"<span style='color:{color};'>{'↑' if diff>0 else '↓'} {abs(diff)}</span>"
        pct = diff / prev * 100
        color = "#2e7d32" if diff > 0 else "#c62828"
        return f"<span style='color:{color};'>{'↑' if diff>0 else '↓'} {abs(pct):.1f}%</span>"

    p: list[str] = []
    p.append("<html><body style=\"font-family:-apple-system,'Segoe UI','PingFang SC',"
             "Arial,sans-serif;line-height:1.7;color:#222;max-width:920px;margin:0 auto;"
             "padding:20px;background:#fafafa;\">")

    # 标题
    issue_str = f" · 第 {issue_number} 期" if issue_number else ""
    p.append(f"<h1 style='color:#1a237e;margin-bottom:4px;font-size:26px;'>{esc(team_name)}</h1>")
    p.append(f"<h2 style='color:#3949ab;margin-top:0;font-size:18px;font-weight:500;'>"
             f"{esc(report_date.isoformat())}（{esc(weekday_cn)}）{esc(issue_str)}</h2>")
    p.append(f"<div style='color:#666;font-size:13px;margin-top:8px;'>"
             f"项目：智能日报生成器 · 负责人：刘雨可<br>"
             f"周期：{esc(report_date.isoformat())} 00:00 ~ {esc(_fmt_time(generated_at))}</div>")
    p.append("<hr style='border:none;border-top:2px solid #e0e5f5;margin:24px 0;'>")

    # 一、核心指标
    p.append("<h2 style='" + h2_style + "'>一、核心指标</h2>")
    headers = ["指标", "今日", "昨日", "环比"]
    aligns = ["left", "center", "center", "center"]

    def build_metric_row(label, key, fmt="num"):
        cur_v = today.get(key, 0)
        y_v = yest.get(key) if yest else None
        cur_s = _fmt_num(cur_v) if fmt == "num" else f"{cur_v}"
        y_s = _fmt_num(y_v) if (y_v is not None and fmt == "num") else ("—" if y_v is None else f"{y_v}")
        return [label, cur_s, y_s, delta_html(cur_v, y_v)]

    rows = [
        build_metric_row("代码提交", "commits"),
        build_metric_row("涉及文件", "files"),
        build_metric_row("新增代码", "additions"),
        build_metric_row("删除代码", "deletions"),
        build_metric_row("净变更", "net"),
        build_metric_row("群消息", "msgs"),
    ]
    p.append(table(headers, rows, aligns))

    if summary:
        p.append("<div style='" + quote_style + "'>")
        p.append(f"<div style='font-weight:600;color:#8a6d3b;margin-bottom:6px;'>一句话摘要</div>")
        p.append(f"<div style='color:#333;'>{esc(summary)}</div>")
        p.append("</div>")

    p.append("<hr style='border:none;border-top:1px solid #e0e5f5;margin:28px 0;'>")

    # 二、工作明细
    p.append("<h2 style='" + h2_style + "'>二、工作明细</h2>")

    if total_commits > 0:
        p.append("<h3 style='" + h3_style + "'>2.1 提交记录</h3>")
        idx = 0
        for m in members:
            member_commits = [c for c in (getattr(m, "commits", []) or [])]
            if not member_commits:
                continue
            for c in sorted(member_commits,
                            key=lambda x: getattr(x, "timestamp", None) or datetime.min):
                idx += 1
                ts = _fmt_time(getattr(c, "timestamp", None))
                msg = (getattr(c, "message", "") or "").strip().splitlines()[0]
                tag = _commit_type_tag(msg)
                tag_html = ""
                if tag:
                    tag_html = (f" <span style='background:#eef4ff;color:#4a6cf7;"
                                f"padding:1px 8px;border-radius:10px;font-size:11px;'>"
                                f"{esc(tag)}</span>")
                sha = _short_sha(getattr(c, "sha", ""))
                additions = int(getattr(c, "additions", 0) or 0)
                deletions = int(getattr(c, "deletions", 0) or 0)
                files_n = int(getattr(c, "files_changed", 0) or 0)
                ai_summary = commit_summaries.get(getattr(c, "sha", ""), "")
                url = _commit_link(c)

                p.append(f"<h4 style='" + h4_style + f"'>{idx}. {esc(ts)} · {esc(msg)}{tag_html}</h4>")
                info_rows = [
                    ["Commit", f"<code>{esc(sha)}</code>"],
                    ["变更", f"<b>+{_fmt_num(additions)} / -{_fmt_num(deletions)}</b> · {files_n} 个文件"],
                ]
                if ai_summary:
                    info_rows.append(["摘要", esc(ai_summary)])
                if url:
                    info_rows.append(["链接", f"<a href='{esc(url)}' style='color:#4a6cf7;'>查看提交</a>"])
                p.append(table(["项", "内容"], info_rows, ["left", "left"]))

                flist = _commit_files(c)
                if flist:
                    frows = []
                    for fd in flist:
                        fname = getattr(fd, "filename", "") or "?"
                        add = int(getattr(fd, "additions", 0) or 0)
                        dele = int(getattr(fd, "deletions", 0) or 0)
                        icon = _file_icon(fname)
                        status_cn = _status_cn(getattr(fd, "status", "modified"))
                        frows.append([
                            f"{esc(icon)} <code>{esc(fname)}</code>",
                            f"+{add} / -{dele}",
                            esc(status_cn),
                        ])
                    p.append(table(["文件", "变更", "状态"], frows, ["left", "center", "center"]))

        # 2.2 工作分类
        if work_classification and work_classification.get("categories"):
            p.append("<h3 style='" + h3_style + "'>2.2 工作分类</h3>")
            crows = []
            for cat in work_classification["categories"]:
                if cat.get("count", 0) == 0:
                    continue
                crows.append([
                    esc(cat.get("label", "")),
                    str(cat.get("count", 0)),
                    f"{cat.get('percent', 0)}%",
                    esc(cat.get("details", "")),
                ])
            p.append(table(["类型", "数量", "占比", "详情"], crows,
                           ["left", "center", "center", "left"]))

        # 附录
        new_files = _collect_files_by_status(all_commits, {"added"})
        del_files = _collect_files_by_status(all_commits, {"removed", "deleted"})
        if new_files:
            p.append(f"<h4 style='" + h4_style + f"'>📤 本次新增文件（{len(new_files)} 个）</h4>")
            nrows = []
            for nf in new_files:
                icon = _file_icon(nf["filename"])
                nrows.append([f"{esc(icon)} <code>{esc(nf['filename'])}</code>",
                              f"+{nf['additions']} / -{nf['deletions']}"])
            p.append(table(["文件", "变更"], nrows, ["left", "center"]))
        if del_files:
            p.append(f"<h4 style='" + h4_style + f"'>🗑 本次删除文件（{len(del_files)} 个）</h4>")
            drows = []
            for df in del_files:
                icon = _file_icon(df["filename"])
                drows.append([f"{esc(icon)} <code>{esc(df['filename'])}</code>",
                              f"+{df['additions']} / -{df['deletions']}"])
            p.append(table(["文件", "变更"], drows, ["left", "center"]))
    else:
        p.append("<p style='color:#999;'>本日无代码提交记录。</p>")

    p.append("<hr style='border:none;border-top:1px solid #e0e5f5;margin:28px 0;'>")

    # 三、变化分析
    p.append("<h2 style='" + h2_style + "'>三、变化分析</h2>")

    if yesterday_stats:
        y_date = yesterday_stats.get("date", "")
        p.append(f"<h3 style='" + h3_style + f"'>3.1 昨日回顾（{esc(y_date)}）</h3>")
        y_c = int(yesterday_stats.get("commits", 0) or 0)
        y_f = int(yesterday_stats.get("files", 0) or 0)
        y_a = int(yesterday_stats.get("additions", 0) or 0)
        y_d = int(yesterday_stats.get("deletions", 0) or 0)
        p.append(table(["指标", "数值"], [
            ["代码提交", f"{y_c} 次"],
            ["涉及文件", f"{y_f} 个"],
            ["代码变更", f"+{_fmt_num(y_a)} / -{_fmt_num(y_d)}"],
        ], ["left", "center"]))

    if comparison:
        y = comparison.get("yesterday", {}) or {}
        t = comparison.get("today", {}) or {}
        yc = int(y.get("commits", 0) or 0)
        tc = int(t.get("commits", 0) or 0)
        trend = comparison.get("trend", "常规迭代")
        c_summary = (comparison.get("summary") or "").strip()
        changes = comparison.get("changes") or []

        p.append("<h3 style='" + h3_style + "'>3.2 今日对比</h3>")
        p.append(table(["对比项", "昨日", "今日"], [
            ["提交次数", str(yc), str(tc)],
        ], ["left", "center", "center"]))

        if changes:
            p.append("<ul style='margin:8px 0;padding-left:22px;'>")
            for ch in changes:
                ch = (ch or "").strip()
                if ch:
                    p.append(f"<li style='margin:3px 0;'>{esc(ch)}</li>")
            p.append("</ul>")

        p.append("<h3 style='" + h3_style + "'>3.3 趋势判断</h3>")
        trend_icon = _TREND_ICON.get(trend, "🔁")
        p.append("<div style='background:#e8f5e9;border-left:4px solid #2e7d32;"
                 "padding:10px 14px;margin:10px 0;border-radius:4px;'>")
        p.append(f"<div style='font-weight:600;color:#1b5e20;margin-bottom:4px;'>"
                 f"{esc(trend_icon)} {esc(trend)}</div>")
        if c_summary:
            p.append(f"<div style='color:#333;'>{esc(c_summary)}</div>")
        p.append("</div>")

    p.append("<hr style='border:none;border-top:1px solid #e0e5f5;margin:28px 0;'>")

    # 四、飞书群消息
    p.append("<h2 style='" + h2_style + "'>四、飞书群消息</h2>")
    if feishu_messages:
        mrows = []
        for msg in _sort_messages(feishu_messages):
            ts = _fmt_time(getattr(msg, "timestamp", None))
            sender = esc(getattr(msg, "sender_name", "") or "未知")
            text = esc(getattr(msg, "text", "") or "")
            mrows.append([esc(ts), sender, text])
        p.append(table(["时间", "发言人", "内容"], mrows, ["center", "center", "left"]))
    else:
        p.append("<p style='color:#999;'>本日无有效群消息。</p>")

    if total_msgs or skipped:
        p.append(f"<p style='color:#666;font-size:13px;'>"
                 f"本日共收到 <b>{total_msgs}</b> 条消息，保留 <b>{kept}</b> 条，"
                 f"过滤 <b>{skipped}</b> 条（撤回 / @某人 / 太短 / 系统消息）。</p>")

    if action_items:
        p.append(f"<h3 style='" + h3_style + "'>群讨论要点</h3>")
        arows = []
        for item in action_items:
            icon, label = _ACTION_LABEL.get(item.get("type", ""), ("·", ""))
            text = esc(item.get("text", "") or "")
            if text:
                arows.append([f"{esc(icon)} {esc(label)}", text])
        p.append(table(["类型", "内容"], arows, ["center", "left"]))

    p.append("<hr style='border:none;border-top:1px solid #e0e5f5;margin:28px 0;'>")

    # 五、工作建议
    p.append("<h2 style='" + h2_style + "'>五、工作建议</h2>")
    if suggestions:
        srows = []
        for i, s in enumerate(suggestions, 1):
            srows.append([str(i), esc(s.get("type", "建议")), esc(s.get("text", ""))])
        p.append(table(["#", "类型", "建议"], srows, ["center", "center", "left"]))
    else:
        p.append("<p style='color:#999;'>本日无特别建议。</p>")

    p.append("<hr style='border:none;border-top:2px solid #e0e5f5;margin:28px 0;'>")

    # 尾部
    p.append("<div style='color:#999;font-size:12px;line-height:1.9;'>")
    p.append(f"<b>数据来源</b>　GitHub · 飞书开放平台<br>")
    p.append(f"<b>采集窗口</b>　{esc(report_date.isoformat())} 00:00 ~ {esc(_fmt_time(generated_at))}（北京时间）<br>")
    p.append(f"<b>生成时间</b>　{esc(_fmt_datetime(generated_at))}<br>")
    p.append(f"<b>文档版本</b>　v1.3 · 由智能日报生成器自动生成")
    p.append("</div>")

    p.append("</body></html>")
    return "".join(p)


def generate(
    members: list,
    report_date: date,
    team_name: str,
    *,
    events: list | None = None,
    feishu_messages: list | None = None,
    summary: str | None = None,
    generated_at: datetime | None = None,
    feishu_stats: dict | None = None,
    yesterday_stats: dict | None = None,
    highlights: list[str] | None = None,
    action_items: list[dict] | None = None,
    commit_summaries: dict[str, str] | None = None,
    comparison: dict | None = None,
    suggestions: list[dict] | None = None,
    work_classification: dict | None = None,
    issue_number: int = 0,
) -> Report:
    generated_at = generated_at or datetime.now()
    md = _render_markdown(
        members, report_date, team_name, events, feishu_messages,
        summary, generated_at, feishu_stats, yesterday_stats, highlights,
        action_items, commit_summaries, comparison,
        suggestions, work_classification, issue_number,
    )
    html = _render_html(
        members, report_date, team_name, events, feishu_messages,
        summary, generated_at, feishu_stats, yesterday_stats, highlights,
        action_items, commit_summaries, comparison,
        suggestions, work_classification, issue_number,
    )
    return Report(
        date=report_date,
        team_name=team_name,
        markdown=md,
        html=html,
        generated_at=generated_at,
    )