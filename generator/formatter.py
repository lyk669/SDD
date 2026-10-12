"""日报生成器 — 输出 Markdown + HTML。

结构：概览 → 消息过滤 → 今日亮点 → 今日总结 → 相比昨天 → 群讨论要点
      → 新增/删除文件 → commit 明细（含 diff 摘要）→ 群消息 → 底部
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
    "added": "新增", "modified": "修改",
    "removed": "删除", "deleted": "删除", "renamed": "重命名",
}

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_COMMIT_TYPE_MAP = {
    "feat": ("🆕", "新功能"), "feature": ("🆕", "新功能"),
    "fix": ("🐛", "修复"), "bugfix": ("🐛", "修复"), "hotfix": ("🐛", "紧急修复"),
    "docs": ("📝", "文档"), "doc": ("📝", "文档"),
    "refactor": ("♻️", "重构"),
    "test": ("🧪", "测试"), "tests": ("🧪", "测试"),
    "chore": ("🔧", "杂项"),
    "style": ("🎨", "样式"),
    "perf": ("⚡", "性能"),
    "ci": ("🚀", "CI"), "build": ("📦", "构建"),
    "revert": ("⏪", "回滚"),
}

_ACTION_LABEL = {
    "decision": ("✅", "决策"),
    "todo": ("📌", "待办"),
    "issue": ("⚠️", "问题"),
}

_TREND_ICON = {
    "功能推进": "📈", "修复完善": "🔧", "重构优化": "♻️",
    "文档维护": "📝", "常规迭代": "🔁",
}


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
    return _STATUS_CN.get((status or "").lower(), "修改")


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


def _disp_width(s: str) -> int:
    w = 0
    for ch in s:
        w += 2 if ord(ch) > 0x2E80 else 1
    return w


def _pad_right(s: str, width: int) -> str:
    w = _disp_width(s)
    if w >= width:
        return s
    return s + " " * (width - w)


def _shorten(s: str, max_w: int = 30) -> str:
    w = 0
    out = []
    for ch in s:
        w += 2 if ord(ch) > 0x2E80 else 1
        if w > max_w:
            out.append("…")
            break
        out.append(ch)
    return "".join(out)


def _wrap_text(text: str, indent: str, width: int = 76,
               cont_indent: str | None = None) -> list[str]:
    """按显示宽度折行。第一行用 indent，续行用 cont_indent（缺省=indent）。"""
    text = (text or "").strip()
    if not text:
        return []
    if cont_indent is None:
        cont_indent = indent
    lines = []
    cur = indent
    cur_w = _disp_width(indent)
    for ch in text:
        cw = 2 if ord(ch) > 0x2E80 else 1
        if cur_w + cw > width:
            lines.append(cur)
            cur = cont_indent
            cur_w = _disp_width(cont_indent)
        cur += ch
        cur_w += cw
    if cur.strip():
        lines.append(cur)
    return lines


def _collect_files_by_status(all_commits: list, target_statuses: set) -> list:
    seen: set = set()
    result: list = []
    for c in all_commits:
        repo = getattr(c, "repo", "") or ""
        sha = getattr(c, "sha", "") or ""
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
                "repo": repo, "sha": sha,
            })
    result.sort(key=lambda x: x["filename"])
    return result


def _collect_new_files(all_commits: list) -> list:
    return _collect_files_by_status(all_commits, {"added"})


def _collect_deleted_files(all_commits: list) -> list:
    return _collect_files_by_status(all_commits, {"removed", "deleted"})


def _sort_messages(messages: list) -> list:
    return sorted(
        messages or [],
        key=lambda x: getattr(x, "timestamp", None) or datetime.min,
    )


def _delta_str(cur: int, prev) -> str:
    if prev is None:
        return ""
    diff = cur - prev
    if diff == 0:
        return "（较昨日 持平）"
    if diff > 0:
        return f"（较昨日 +{diff} ↑）"
    return f"（较昨日 {diff} ↓）"


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
) -> str:
    events = events or []
    feishu_messages = feishu_messages or []
    feishu_stats = feishu_stats or {}
    yesterday_stats = yesterday_stats or {}
    highlights = highlights or []
    action_items = action_items or []
    commit_summaries = commit_summaries or {}
    summary = (summary or "").strip()

    all_commits = [c for m in members for c in (getattr(m, "commits", []) or [])]
    total_commits = len(all_commits)
    total_files = sum(int(getattr(c, "files_changed", 0) or 0) for c in all_commits)
    total_add = sum(int(getattr(c, "additions", 0) or 0) for c in all_commits)
    total_del = sum(int(getattr(c, "deletions", 0) or 0) for c in all_commits)
    net = total_add - total_del
    repos = sorted({getattr(c, "repo", "") for c in all_commits if getattr(c, "repo", "")})

    added = modified = removed = 0
    for c in all_commits:
        for fd in _commit_files(c):
            st = (getattr(fd, "status", "modified") or "modified").lower()
            if st == "added":
                added += 1
            elif st in {"removed", "deleted"}:
                removed += 1
            else:
                modified += 1

    new_files = _collect_new_files(all_commits)
    deleted_files = _collect_deleted_files(all_commits)
    weekday_cn = _WEEKDAY_CN[report_date.weekday()]

    kept = feishu_stats.get("kept", len(feishu_messages))
    skipped = int(feishu_stats.get("skipped", 0) or 0)
    total_msgs = int(feishu_stats.get("total", kept + skipped) or 0)

    has_yest = bool(yesterday_stats)
    y_commits = int(yesterday_stats.get("commits", 0)) if has_yest else None
    y_files = int(yesterday_stats.get("files", 0)) if has_yest else None
    y_add = int(yesterday_stats.get("additions", 0)) if has_yest else None
    y_del = int(yesterday_stats.get("deletions", 0)) if has_yest else None
    y_net = (y_add - y_del) if has_yest else None

    lines: list[str] = []
    lines.append(f"# {team_name} · {report_date.isoformat()}（{weekday_cn}）")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    lines.append("📊 今日概览")
    lines.append(f"  提交次数    {total_commits} 次{_delta_str(total_commits, y_commits)}")
    lines.append(
        f"  涉及文件    {total_files} 个（新增 {added} · 修改 {modified} · 删除 {removed}）"
        f"{_delta_str(total_files, y_files)}"
    )
    lines.append(
        f"  代码变更    +{total_add} / -{total_del}（净 {'+' if net >= 0 else ''}{net} 行）"
        f"{_delta_str(net, y_net)}"
    )
    lines.append(f"  覆盖仓库    {len(repos)} 个")
    if kept or skipped:
        senders = sorted({getattr(m, "sender_name", "") for m in feishu_messages
                          if getattr(m, "sender_name", "")})
        msg_line = f"  群消息      {kept} 条 · {len(senders)} 人参与"
        if skipped:
            msg_line += f"（已过滤 {skipped} 条无效消息）"
        lines.append(msg_line)
    lines.append("")

    if skipped:
        lines.append(
            f"🧹 消息过滤：共收到 {total_msgs} 条，保留 {kept} 条，"
            f"过滤 {skipped} 条（撤回 / @某人 / 太短 / 系统消息）"
        )
        lines.append("")

    if highlights:
        lines.append("⭐ 今日亮点")
        for i, h in enumerate(highlights, 1):
            lines.append(f"  {i}. {h}")
        lines.append("")

    if summary:
        lines.append("📝 今日总结")
        for para in summary.split("\n"):
            para = para.strip()
            if para:
                for wl in _wrap_text(para, "  "):
                    lines.append(wl)
        lines.append("")

    # 相比昨天
    if comparison:
        y = comparison.get("yesterday", {}) or {}
        t = comparison.get("today", {}) or {}
        yc = int(y.get("commits", 0) or 0)
        ya = int(y.get("additions", 0) or 0)
        yd = int(y.get("deletions", 0) or 0)
        tc = int(t.get("commits", 0) or 0)
        ta = int(t.get("additions", 0) or 0)
        td = int(t.get("deletions", 0) or 0)
        trend = comparison.get("trend", "常规迭代")
        trend_icon = _TREND_ICON.get(trend, "🔁")

        lines.append("📈 相比昨天")
        lines.append(f"  【昨日】{yc} 次提交 · +{ya} / -{yd}")
        lines.append(f"  【今日】{tc} 次提交 · +{ta} / -{td}")
        c_summary = (comparison.get("summary") or "").strip()
        if c_summary:
            lines.append("")
            for wl in _wrap_text(c_summary, "  🔄 "):
                lines.append(wl)
        for ch in (comparison.get("changes") or []):
            ch = (ch or "").strip()
            if ch:
                lines.append(f"  · {ch}")
        lines.append(f"  {trend_icon} 趋势：{trend}")
        lines.append("")

    if action_items:
        lines.append("📌 群讨论要点")
        for item in action_items:
            icon, label = _ACTION_LABEL.get(item.get("type", ""), ("·", ""))
            text = (item.get("text", "") or "").strip()
            if text:
                lines.append(f"  {icon} 【{label}】{text}")
        lines.append("")

    if new_files:
        lines.append(f"📤 本次新增文件（{len(new_files)} 个）")
        for nf in new_files:
            icon = _file_icon(nf["filename"])
            name = _shorten(nf["filename"], 40)
            add_del = f"+{nf['additions']} / -{nf['deletions']}"
            lines.append(f"   {icon} {_pad_right(name, 42)} {add_del}")
        lines.append("")

    if deleted_files:
        lines.append(f"🗑️ 本次删除文件（{len(deleted_files)} 个）")
        for df in deleted_files:
            icon = _file_icon(df["filename"])
            name = _shorten(df["filename"], 40)
            add_del = f"+{df['additions']} / -{df['deletions']}"
            lines.append(f"   {icon} {_pad_right(name, 42)} {add_del}")
        lines.append("")

    if total_commits == 0:
        lines.append("📭 今日无代码提交记录")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    if total_commits > 0:
        for repo in repos or ["（未指定仓库）"]:
            repo_commits = [c for c in all_commits if getattr(c, "repo", "") == repo]
            r_add = sum(int(getattr(c, "additions", 0) or 0) for c in repo_commits)
            r_del = sum(int(getattr(c, "deletions", 0) or 0) for c in repo_commits)
            r_files = sum(int(getattr(c, "files_changed", 0) or 0) for c in repo_commits)
            lines.append(f"📦 {repo}")
            lines.append(f"   {len(repo_commits)} 次提交 · {r_files} 个文件 · +{r_add} / -{r_del}")
            lines.append("─" * 58)
            lines.append("")

            for m in members:
                member_commits = [c for c in (getattr(m, "commits", []) or [])
                                  if getattr(c, "repo", "") == repo]
                if not member_commits:
                    continue

                m_add = sum(int(getattr(c, "additions", 0) or 0) for c in member_commits)
                m_del = sum(int(getattr(c, "deletions", 0) or 0) for c in member_commits)
                lines.append(
                    f"👤 {m.name}（{m.github_username}）· {len(member_commits)} 次提交 · +{m_add} / -{m_del}"
                )
                lines.append("")

                buckets = {"morning": [], "afternoon": [], "evening": []}
                for c in member_commits:
                    ts = getattr(c, "timestamp", None) or generated_at
                    buckets[_time_slot(ts)].append(c)

                slot_names = {
                    "morning": "🌅 上午（00:00 - 12:00）",
                    "afternoon": "🌆 下午（12:00 - 18:00）",
                    "evening": "🌙 晚上（18:00 - 24:00）",
                }

                idx = 0
                for slot in ("morning", "afternoon", "evening"):
                    lines.append(slot_names[slot])
                    slot_commits = sorted(
                        buckets[slot],
                        key=lambda c: getattr(c, "timestamp", None) or datetime.min,
                    )
                    if not slot_commits:
                        lines.append("   （暂无提交）")
                        lines.append("")
                        continue

                    for c in slot_commits:
                        idx += 1
                        full_ts = _fmt_datetime(getattr(c, "timestamp", None))
                        msg = (getattr(c, "message", "") or "").strip().splitlines()[0]
                        tag = _commit_type_tag(msg)
                        tag_str = f"  {tag}" if tag else ""
                        sha = _short_sha(getattr(c, "sha", ""))
                        additions = int(getattr(c, "additions", 0) or 0)
                        deletions = int(getattr(c, "deletions", 0) or 0)
                        url = _commit_link(c)
                        ai_summary = commit_summaries.get(getattr(c, "sha", ""), "")

                        lines.append(f"  {idx:02d}. {full_ts} · {msg}{tag_str}")
                        lines.append(f"      {sha}")
                        if ai_summary:
                            for wl in _wrap_text(
                                ai_summary,
                                "      💬 ",
                                width=78,
                                cont_indent="         ",   # 9 空格，对齐到 💬 后
                            ):
                                lines.append(wl)
                        lines.append("")
                        lines.append("      文件                              增 / 删           状态")
                        lines.append("      " + "─" * 56)

                        for fd in _commit_files(c):
                            fname = getattr(fd, "filename", "") or "?"
                            add = int(getattr(fd, "additions", 0) or 0)
                            dele = int(getattr(fd, "deletions", 0) or 0)
                            status_cn = _status_cn(getattr(fd, "status", "modified"))
                            icon = _file_icon(fname)
                            short_name = _shorten(fname, 28)
                            name_cell = _pad_right(f"{icon} {short_name}", 34)
                            add_del = f"+{add} / -{dele}"
                            lines.append(
                                f"      {name_cell}  {_pad_right(add_del, 16)} {status_cn}"
                            )

                        lines.append("      " + "─" * 56)
                        lines.append(
                            f"      小计                              +{additions} / -{deletions}"
                        )
                        if url:
                            lines.append(f"      🔗 {url}")
                        lines.append("")

            lines.append("")

    # 飞书群消息 —— 独立区块
    if feishu_messages:
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"💬 飞书群消息（共 {len(feishu_messages)} 条）")
        lines.append("")
        for msg in _sort_messages(feishu_messages):
            ts = _fmt_time(getattr(msg, "timestamp", None))
            sender = getattr(msg, "sender_name", "") or "未知"
            text = (getattr(msg, "text", "") or "").replace("\n", " ")
            lines.append(f"  {ts}  {sender}：{text}")
        lines.append("")

    if events:
        lines.append("─" * 58)
        lines.append("📢 今日 GitHub 公开活动")
        for e in sorted(events, key=lambda x: getattr(x, "timestamp", None) or datetime.min):
            ts = _fmt_time(getattr(e, "timestamp", None))
            s_text = getattr(e, "summary", "")
            lines.append(f"   {ts}   {s_text}")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"数据来源    GitHub · {', '.join(repos) if repos else '（未配置）'}")
    lines.append(
        f"采集窗口    {report_date.isoformat()} 00:00 ~ {_fmt_time(generated_at)}（北京时间）"
    )
    lines.append(f"生成时间    {_fmt_datetime(generated_at)}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
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
) -> str:
    events = events or []
    feishu_messages = feishu_messages or []
    feishu_stats = feishu_stats or {}
    yesterday_stats = yesterday_stats or {}
    highlights = highlights or []
    action_items = action_items or []
    commit_summaries = commit_summaries or {}
    summary = (summary or "").strip()

    def esc(s) -> str:
        return _html.escape(str(s or ""))

    all_commits = [c for m in members for c in (getattr(m, "commits", []) or [])]
    total_commits = len(all_commits)
    total_files = sum(int(getattr(c, "files_changed", 0) or 0) for c in all_commits)
    total_add = sum(int(getattr(c, "additions", 0) or 0) for c in all_commits)
    total_del = sum(int(getattr(c, "deletions", 0) or 0) for c in all_commits)
    net = total_add - total_del
    repos = sorted({getattr(c, "repo", "") for c in all_commits if getattr(c, "repo", "")})

    added = modified = removed = 0
    for c in all_commits:
        for fd in _commit_files(c):
            st = (getattr(fd, "status", "modified") or "modified").lower()
            if st == "added":
                added += 1
            elif st in {"removed", "deleted"}:
                removed += 1
            else:
                modified += 1

    new_files = _collect_new_files(all_commits)
    deleted_files = _collect_deleted_files(all_commits)
    weekday_cn = _WEEKDAY_CN[report_date.weekday()]

    kept = feishu_stats.get("kept", len(feishu_messages))
    skipped = int(feishu_stats.get("skipped", 0) or 0)
    total_msgs = int(feishu_stats.get("total", kept + skipped) or 0)

    has_yest = bool(yesterday_stats)
    y_commits = int(yesterday_stats.get("commits", 0)) if has_yest else None
    y_files = int(yesterday_stats.get("files", 0)) if has_yest else None
    y_add = int(yesterday_stats.get("additions", 0)) if has_yest else None
    y_del = int(yesterday_stats.get("deletions", 0)) if has_yest else None
    y_net = (y_add - y_del) if has_yest else None

    def delta_html(cur, prev):
        if prev is None:
            return ""
        diff = cur - prev
        if diff == 0:
            return "<span style='color:#999;font-size:12px;'>（较昨日 持平）</span>"
        color = "#2e7d32" if diff > 0 else "#c62828"
        arrow = "↑" if diff > 0 else "↓"
        sign = "+" if diff > 0 else ""
        return f"<span style='color:{color};font-size:12px;'>（较昨日 {sign}{diff} {arrow}）</span>"

    p: list[str] = []
    p.append("<html><body style=\"font-family:-apple-system,'Segoe UI',Arial,"
             "sans-serif;line-height:1.6;color:#222;max-width:900px;margin:0 auto;padding:16px;\">")
    p.append(
        f"<h1 style='border-bottom:2px solid #4a6cf7;padding-bottom:8px;'>"
        f"{esc(team_name)} · {esc(report_date.isoformat())}"
        f"<span style='font-size:15px;color:#666;font-weight:normal;'>"
        f"（{esc(weekday_cn)}）</span></h1>"
    )

    # 概览
    p.append("<div style='background:#f0f3ff;border-radius:6px;padding:12px 16px;margin:12px 0;'>")
    p.append("<div style='font-weight:bold;margin-bottom:6px;'>📊 今日概览</div>")
    p.append("<table style='font-size:14px;'>")
    p.append(f"<tr><td style='padding:2px 12px 2px 0;color:#666;'>提交次数</td>"
             f"<td><b>{total_commits}</b> 次 {delta_html(total_commits, y_commits)}</td></tr>")
    p.append(f"<tr><td style='padding:2px 12px 2px 0;color:#666;'>涉及文件</td>"
             f"<td><b>{total_files}</b> 个（新增 {added} · 修改 {modified} · 删除 {removed}）"
             f"{delta_html(total_files, y_files)}</td></tr>")
    p.append(f"<tr><td style='padding:2px 12px 2px 0;color:#666;'>代码变更</td><td>"
             f"<span style='color:#2e7d32;'>+{total_add}</span> / "
             f"<span style='color:#c62828;'>-{total_del}</span>"
             f"（净 <b>{'+' if net >= 0 else ''}{net}</b> 行）"
             f"{delta_html(net, y_net)}</td></tr>")
    p.append(f"<tr><td style='padding:2px 12px 2px 0;color:#666;'>覆盖仓库</td>"
             f"<td><b>{len(repos)}</b> 个</td></tr>")
    if kept or skipped:
        senders = sorted({getattr(m, "sender_name", "") for m in feishu_messages
                          if getattr(m, "sender_name", "")})
        msg_html = f"<b>{kept}</b> 条 · {len(senders)} 人参与"
        if skipped:
            msg_html += f"<span style='color:#999;'>（已过滤 {skipped} 条无效消息）</span>"
        p.append(f"<tr><td style='padding:2px 12px 2px 0;color:#666;'>群消息</td>"
                 f"<td>{msg_html}</td></tr>")
    p.append("</table></div>")

    if skipped:
        p.append("<div style='background:#f5f5f5;border-radius:6px;padding:8px 14px;"
                 "margin:8px 0;font-size:13px;color:#666;'>"
                 f"🧹 消息过滤：共收到 <b>{total_msgs}</b> 条，"
                 f"保留 <b>{kept}</b> 条，"
                 f"过滤 <b>{skipped}</b> 条（撤回 / @某人 / 太短 / 系统消息）</div>")

    if highlights:
        p.append("<div style='background:#eef4ff;border-left:4px solid #4a6cf7;"
                 "border-radius:6px;padding:12px 16px;margin:12px 0;'>")
        p.append("<div style='font-weight:bold;margin-bottom:6px;'>⭐ 今日亮点</div>")
        p.append("<ol style='margin:4px 0;padding-left:24px;color:#333;'>")
        for h in highlights:
            p.append(f"<li style='margin:4px 0;'>{esc(h)}</li>")
        p.append("</ol></div>")

    if summary:
        p.append("<div style='background:#fff8e1;border-left:4px solid #f59e0b;"
                 "border-radius:6px;padding:12px 16px;margin:12px 0;'>")
        p.append("<div style='font-weight:bold;margin-bottom:6px;'>📝 今日总结</div>")
        for para in summary.split("\n"):
            para = esc(para.strip())
            if para:
                p.append(f"<p style='margin:4px 0;color:#333;'>{para}</p>")
        p.append("</div>")

    # 相比昨天
    if comparison:
        y = comparison.get("yesterday", {}) or {}
        t = comparison.get("today", {}) or {}
        trend = comparison.get("trend", "常规迭代")
        trend_icon = _TREND_ICON.get(trend, "🔁")

        p.append("<div style='background:#e8f5e9;border-left:4px solid #2e7d32;"
                 "border-radius:6px;padding:12px 16px;margin:12px 0;'>")
        p.append("<div style='font-weight:bold;margin-bottom:8px;'>📈 相比昨天</div>")

        yc = int(y.get("commits", 0) or 0)
        ya = int(y.get("additions", 0) or 0)
        yd = int(y.get("deletions", 0) or 0)
        tc = int(t.get("commits", 0) or 0)
        ta = int(t.get("additions", 0) or 0)
        td = int(t.get("deletions", 0) or 0)

        p.append("<table style='font-size:13px;border-collapse:collapse;margin-bottom:8px;'>")
        p.append(f"<tr><td style='padding:2px 12px 2px 0;color:#666;'>昨日</td>"
                 f"<td>{yc} 次提交 · "
                 f"<span style='color:#2e7d32;'>+{ya}</span> / "
                 f"<span style='color:#c62828;'>-{yd}</span></td></tr>")
        p.append(f"<tr><td style='padding:2px 12px 2px 0;color:#666;'>今日</td>"
                 f"<td>{tc} 次提交 · "
                 f"<span style='color:#2e7d32;'>+{ta}</span> / "
                 f"<span style='color:#c62828;'>-{td}</span></td></tr>")
        p.append("</table>")

        c_summary = (comparison.get("summary") or "").strip()
        if c_summary:
            p.append(f"<p style='margin:6px 0;color:#1b5e20;'>"
                     f"<b>🔄 变化：</b>{esc(c_summary)}</p>")

        changes = comparison.get("changes") or []
        if changes:
            p.append("<ul style='margin:6px 0;padding-left:20px;color:#333;'>")
            for ch in changes:
                ch = (ch or "").strip()
                if ch:
                    p.append(f"<li style='margin:3px 0;'>{esc(ch)}</li>")
            p.append("</ul>")

        p.append(f"<p style='margin:6px 0 0 0;font-size:13px;color:#2e7d32;'>"
                 f"<b>{trend_icon} 趋势：</b>{esc(trend)}</p>")
        p.append("</div>")

    # 群讨论要点
    if action_items:
        p.append("<div style='background:#f5f0ff;border-left:4px solid #8b5cf6;"
                 "border-radius:6px;padding:12px 16px;margin:12px 0;'>")
        p.append("<div style='font-weight:bold;margin-bottom:6px;'>📌 群讨论要点</div>")
        p.append("<ul style='margin:4px 0;padding-left:20px;color:#333;list-style:none;'>")
        for item in action_items:
            icon, label = _ACTION_LABEL.get(item.get("type", ""), ("·", ""))
            text = esc(item.get("text", "") or "")
            if text:
                p.append(f"<li style='margin:4px 0;'>{esc(icon)} "
                         f"<b style='color:#8b5cf6;'>【{esc(label)}】</b>{text}</li>")
        p.append("</ul></div>")

    if new_files:
        p.append("<div style='background:#eaf6ee;border-radius:6px;padding:12px 16px;margin:12px 0;'>")
        p.append(f"<div style='font-weight:bold;margin-bottom:6px;'>📤 本次新增文件（{len(new_files)} 个）</div>")
        p.append("<table style='font-size:13px;border-collapse:collapse;width:100%;'>")
        for nf in new_files:
            icon = _file_icon(nf["filename"])
            p.append(f"<tr><td style='padding:2px 8px 2px 0;'>"
                     f"{esc(icon)} <code>{esc(nf['filename'])}</code></td>"
                     f"<td style='padding:2px 8px;text-align:right;color:#2e7d32;'>+{nf['additions']}</td>"
                     f"<td style='padding:2px 8px;text-align:right;color:#c62828;'>-{nf['deletions']}</td>"
                     "</tr>")
        p.append("</table></div>")

    if deleted_files:
        p.append("<div style='background:#fdeaea;border-radius:6px;padding:12px 16px;margin:12px 0;'>")
        p.append(f"<div style='font-weight:bold;margin-bottom:6px;'>🗑️ 本次删除文件（{len(deleted_files)} 个）</div>")
        p.append("<table style='font-size:13px;border-collapse:collapse;width:100%;'>")
        for df in deleted_files:
            icon = _file_icon(df["filename"])
            p.append(f"<tr><td style='padding:2px 8px 2px 0;'>"
                     f"{esc(icon)} <code>{esc(df['filename'])}</code></td>"
                     f"<td style='padding:2px 8px;text-align:right;color:#2e7d32;'>+{df['additions']}</td>"
                     f"<td style='padding:2px 8px;text-align:right;color:#c62828;'>-{df['deletions']}</td>"
                     "</tr>")
        p.append("</table></div>")

    if total_commits > 0:
        for repo in repos or ["（未指定仓库）"]:
            repo_commits = [c for c in all_commits if getattr(c, "repo", "") == repo]
            r_add = sum(int(getattr(c, "additions", 0) or 0) for c in repo_commits)
            r_del = sum(int(getattr(c, "deletions", 0) or 0) for c in repo_commits)
            r_files = sum(int(getattr(c, "files_changed", 0) or 0) for c in repo_commits)

            p.append(f"<h2 style='color:#4a6cf7;margin-top:24px;'>📦 {esc(repo)}</h2>")
            p.append(f"<div style='color:#666;font-size:14px;margin-bottom:10px;'>"
                     f"{len(repo_commits)} 次提交 · {r_files} 个文件 · "
                     f"<span style='color:#2e7d32;'>+{r_add}</span> / "
                     f"<span style='color:#c62828;'>-{r_del}</span></div>")

            for m in members:
                member_commits = [c for c in (getattr(m, "commits", []) or [])
                                  if getattr(c, "repo", "") == repo]
                if not member_commits:
                    continue

                m_add = sum(int(getattr(c, "additions", 0) or 0) for c in member_commits)
                m_del = sum(int(getattr(c, "deletions", 0) or 0) for c in member_commits)

                p.append(f"<h3 style='margin-top:20px;'>👤 {esc(m.name)}"
                         f" <code style='font-weight:normal;color:#666;'>({esc(m.github_username)})</code>"
                         f" <span style='font-weight:normal;font-size:14px;color:#666;'>·"
                         f" {len(member_commits)} 次提交 · "
                         f"<span style='color:#2e7d32;'>+{m_add}</span> / "
                         f"<span style='color:#c62828;'>-{m_del}</span></span></h3>")

                buckets = {"morning": [], "afternoon": [], "evening": []}
                for c in member_commits:
                    ts = getattr(c, "timestamp", None) or generated_at
                    buckets[_time_slot(ts)].append(c)

                slot_names = {
                    "morning": "🌅 上午（00:00 - 12:00）",
                    "afternoon": "🌆 下午（12:00 - 18:00）",
                    "evening": "🌙 晚上（18:00 - 24:00）",
                }

                idx = 0
                for slot in ("morning", "afternoon", "evening"):
                    p.append(f"<h4 style='color:#555;margin:14px 0 6px;'>{esc(slot_names[slot])}</h4>")
                    slot_commits = sorted(
                        buckets[slot],
                        key=lambda c: getattr(c, "timestamp", None) or datetime.min,
                    )
                    if not slot_commits:
                        p.append("<p style='color:#aaa;font-size:13px;margin:4px 0;'>（暂无提交）</p>")
                        continue

                    for c in slot_commits:
                        idx += 1
                        full_ts = _fmt_datetime(getattr(c, "timestamp", None))
                        msg = esc((getattr(c, "message", "") or "").strip().splitlines()[0])
                        tag = _commit_type_tag(getattr(c, "message", "") or "")
                        tag_html = ""
                        if tag:
                            tag_html = (f" <span style='background:#eef4ff;color:#4a6cf7;"
                                        f"padding:1px 8px;border-radius:10px;font-size:12px;'>"
                                        f"{esc(tag)}</span>")
                        sha = esc(_short_sha(getattr(c, "sha", "")))
                        additions = int(getattr(c, "additions", 0) or 0)
                        deletions = int(getattr(c, "deletions", 0) or 0)
                        url = _commit_link(c)
                        ai_summary = commit_summaries.get(getattr(c, "sha", ""), "")

                        p.append("<div style='border-left:3px solid #4a6cf7;padding:10px 14px;"
                                 "margin:10px 0;background:#f8f9fc;border-radius:4px;'>")
                        p.append(f"<div><b>{idx:02d}. {full_ts} · {msg}</b>{tag_html}</div>")
                        p.append(f"<div style='color:#888;font-size:13px;margin:4px 0;'><code>{sha}</code></div>")

                        if ai_summary:
                            p.append("<div style='background:#eef4ff;border-left:3px solid #93b4ff;"
                                     "padding:6px 10px;margin:8px 0;border-radius:4px;"
                                     "font-size:13px;color:#333;'>"
                                     f"💬 {esc(ai_summary)}</div>")

                        p.append("<table style='border-collapse:collapse;width:100%;font-size:13px;margin:6px 0;'>")
                        p.append("<tr style='color:#666;'>"
                                 "<th style='text-align:left;padding:3px 8px 3px 0;'>文件</th>"
                                 "<th style='text-align:right;padding:3px 8px;'>增</th>"
                                 "<th style='text-align:right;padding:3px 8px;'>删</th>"
                                 "<th style='text-align:center;padding:3px 8px;'>状态</th>"
                                 "</tr>")
                        for fd in _commit_files(c):
                            fname = getattr(fd, "filename", "") or "?"
                            add = int(getattr(fd, "additions", 0) or 0)
                            dele = int(getattr(fd, "deletions", 0) or 0)
                            status_cn = _status_cn(getattr(fd, "status", "modified"))
                            icon = _file_icon(fname)
                            p.append(f"<tr><td style='padding:2px 8px 2px 0;'>"
                                     f"{esc(icon)} <code>{esc(fname)}</code></td>"
                                     f"<td style='padding:2px 8px;text-align:right;color:#2e7d32;'>+{add}</td>"
                                     f"<td style='padding:2px 8px;text-align:right;color:#c62828;'>-{dele}</td>"
                                     f"<td style='padding:2px 8px;text-align:center;color:#666;'>{esc(status_cn)}</td>"
                                     "</tr>")
                        p.append(f"<tr><td style='padding:4px 8px 4px 0;color:#666;"
                                 f"font-weight:bold;'>小计</td>"
                                 f"<td style='padding:4px 8px;text-align:right;color:#2e7d32;"
                                 f"font-weight:bold;'>+{additions}</td>"
                                 f"<td style='padding:4px 8px;text-align:right;color:#c62828;"
                                 f"font-weight:bold;'>-{deletions}</td>"
                                 "<td></td></tr>"
                                 "</table>")
                        if url:
                            p.append(f"<div style='font-size:12px;'>"
                                     f"<a href='{esc(url)}' style='color:#4a6cf7;'>🔗 查看提交</a></div>")
                        p.append("</div>")

    # 飞书群消息 —— 独立区块
    if feishu_messages:
        p.append("<hr style='border:none;border-top:1px solid #eee;margin:24px 0;'>")
        p.append(f"<h2 style='color:#10b981;margin-top:0;'>"
                 f"💬 飞书群消息（共 {len(feishu_messages)} 条）</h2>")
        p.append("<div style='background:#f0fdf4;border-radius:6px;"
                 "padding:10px 14px;font-size:14px;'>")
        for msg in _sort_messages(feishu_messages):
            ts = _fmt_time(getattr(msg, "timestamp", None))
            sender = esc(getattr(msg, "sender_name", "") or "未知")
            text = esc(getattr(msg, "text", "") or "")
            p.append(f"<div style='margin:6px 0;padding:6px 0;"
                     f"border-bottom:1px dashed #d1fae5;'>"
                     f"<code style='color:#059669;'>{esc(ts)}</code> "
                     f"<b>{sender}</b>：{text}</div>")
        p.append("</div>")

    if events:
        p.append("<hr style='border:none;border-top:1px solid #eee;margin:20px 0;'>")
        p.append("<h3>📢 今日 GitHub 公开活动</h3>")
        p.append("<ul style='font-size:14px;color:#333;'>")
        for e in sorted(events, key=lambda x: getattr(x, "timestamp", None) or datetime.min):
            ts = _fmt_time(getattr(e, "timestamp", None))
            s_text = esc(getattr(e, "summary", ""))
            url = getattr(e, "url", "") or ""
            if url:
                p.append(f"<li><code>{esc(ts)}</code> "
                         f"<a href='{esc(url)}' style='color:#4a6cf7;'>{s_text}</a></li>")
            else:
                p.append(f"<li><code>{esc(ts)}</code> {s_text}</li>")
        p.append("</ul>")

    p.append("<hr style='border:none;border-top:1px solid #eee;margin:24px 0;'>")
    p.append(
        f"<p style='color:#999;font-size:12px;line-height:1.8;'>"
        f"数据来源　　GitHub · {esc(', '.join(repos) or '（未配置）')}<br>"
        f"采集窗口　　{esc(report_date.isoformat())} 00:00 ~ {esc(_fmt_time(generated_at))}（北京时间）<br>"
        f"生成时间　　{esc(_fmt_datetime(generated_at))}</p>"
    )
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
) -> Report:
    generated_at = generated_at or datetime.now()
    md = _render_markdown(
        members, report_date, team_name, events, feishu_messages,
        summary, generated_at, feishu_stats, yesterday_stats, highlights,
        action_items, commit_summaries, comparison,
    )
    html = _render_html(
        members, report_date, team_name, events, feishu_messages,
        summary, generated_at, feishu_stats, yesterday_stats, highlights,
        action_items, commit_summaries, comparison,
    )
    return Report(
        date=report_date,
        team_name=team_name,
        markdown=md,
        html=html,
        generated_at=generated_at,
    )