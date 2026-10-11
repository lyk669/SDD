"""日报生成器 — 输出 Markdown + HTML。

格式：顶部概览 → 本次新增/删除文件 → 仓库 → 成员 → 按时间段分组 → commit 明细 → 公开活动 → 底部来源
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
    "added": "新增",
    "modified": "修改",
    "removed": "删除",
    "deleted": "删除",
    "renamed": "重命名",
}

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _file_icon(filename: str) -> str:
    name = (filename or "").lower()
    for suffix, icon in _ICON_MAP.items():
        if name.endswith(suffix):
            return icon
    return "📄"


def _status_cn(status: str) -> str:
    return _STATUS_CN.get((status or "").lower(), "修改")


def _time_slot(dt: datetime) -> str:
    h = dt.hour
    if h < 12:
        return "morning"
    if h < 18:
        return "afternoon"
    return "evening"


def _fmt_time(dt: datetime | None) -> str:
    if dt is None:
        return "--:--"
    try:
        return dt.astimezone().strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return dt.strftime("%H:%M")


def _short_sha(sha: str | None) -> str:
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


def _collect_files_by_status(all_commits: list, target_statuses: set[str]) -> list:
    """收集指定 status 的文件，去重（同一文件只保留一次）。

    返回 [{"filename", "additions", "deletions", "repo", "sha"}, ...]，按文件名排序。
    """
    seen: set[str] = set()
    result: list[dict] = []
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
                "repo": repo,
                "sha": sha,
            })
    result.sort(key=lambda x: x["filename"])
    return result


def _collect_new_files(all_commits: list) -> list:
    return _collect_files_by_status(all_commits, {"added"})


def _collect_deleted_files(all_commits: list) -> list:
    return _collect_files_by_status(all_commits, {"removed", "deleted"})


# ---------- Markdown ----------

def _render_markdown(
    members: list,
    report_date: date,
    team_name: str,
    events: list | None,
    generated_at: datetime,
) -> str:
    events = events or []
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

    lines: list[str] = []
    lines.append(f"# {team_name} 日报 — {report_date.isoformat()}（{weekday_cn}）")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    if total_commits == 0:
        lines.append("📭 今日暂无代码提交记录")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("")
        lines.append("可能原因：")
        lines.append("  · 今天没有 push 代码")
        lines.append("  · 提交的作者未映射到团队成员")
        lines.append("")
        lines.append(f"采集仓库    {', '.join(repos) if repos else '（未配置）'}")
        lines.append(f"采集窗口    {report_date.isoformat()} 00:00 ~ {_fmt_time(generated_at)}")
        lines.append(f"生成时间    {generated_at.strftime('%Y-%m-%d %H:%M:%S')}")
        return "\n".join(lines)

    # 今日概览
    lines.append("📊 今日概览")
    lines.append(f"  提交次数    {total_commits} 次")
    lines.append(f"  涉及文件    {total_files} 个（新增 {added} · 修改 {modified} · 删除 {removed}）")
    lines.append(f"  代码变更    +{total_add} / -{total_del}（净 {'+' if net >= 0 else ''}{net} 行）")
    lines.append(f"  覆盖仓库    {len(repos)} 个")
    lines.append("")

    # 本次新增文件（仅当有新增时展示）
    if new_files:
        lines.append(f"📤 本次新增文件（{len(new_files)} 个）")
        for nf in new_files:
            icon = _file_icon(nf["filename"])
            name = _shorten(nf["filename"], 40)
            add_del = f"+{nf['additions']} / -{nf['deletions']}"
            lines.append(f"   {icon} {_pad_right(name, 42)} {add_del}")
        lines.append("")

    # 本次删除文件（仅当有删除时展示）
    if deleted_files:
        lines.append(f"🗑️ 本次删除文件（{len(deleted_files)} 个）")
        for df in deleted_files:
            icon = _file_icon(df["filename"])
            name = _shorten(df["filename"], 40)
            add_del = f"+{df['additions']} / -{df['deletions']}"
            lines.append(f"   {icon} {_pad_right(name, 42)} {add_del}")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # 仓库 → 成员 → 时段 → 提交
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
            commits = [c for c in (getattr(m, "commits", []) or [])
                       if getattr(c, "repo", "") == repo]
            if not commits:
                continue
            m_add = sum(int(getattr(c, "additions", 0) or 0) for c in commits)
            m_del = sum(int(getattr(c, "deletions", 0) or 0) for c in commits)
            lines.append(
                f"👤 {m.name}（{m.github_username}）· {len(commits)} 次提交 · +{m_add} / -{m_del}"
            )
            lines.append("")

            buckets = {"morning": [], "afternoon": [], "evening": []}
            for c in commits:
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
                    ts = _fmt_time(getattr(c, "timestamp", None))
                    msg = (getattr(c, "message", "") or "").strip().splitlines()[0]
                    sha = _short_sha(getattr(c, "sha", ""))
                    additions = int(getattr(c, "additions", 0) or 0)
                    deletions = int(getattr(c, "deletions", 0) or 0)
                    url = _commit_link(c)

                    lines.append(f"  {idx:02d}. {ts} · {msg}")
                    lines.append(f"      {sha}")
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
                    lines.append("")
                    if url:
                        lines.append(f"      🔗 {url}")
                    lines.append("")

    # 公开活动
    if events:
        lines.append("─" * 58)
        lines.append("📢 今日 GitHub 公开活动")
        for e in sorted(events, key=lambda x: getattr(x, "timestamp", None) or datetime.min):
            ts = _fmt_time(getattr(e, "timestamp", None))
            summary = getattr(e, "summary", "")
            lines.append(f"   {ts}   {summary}")
        lines.append("")

    # 底部
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"数据来源    GitHub · {', '.join(repos) if repos else '（未配置）'}")
    lines.append(
        f"采集窗口    {report_date.isoformat()} 00:00 ~ {_fmt_time(generated_at)}（北京时间）"
    )
    lines.append(f"生成时间    {generated_at.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


# ---------- HTML ----------

def _render_html(
    members: list,
    report_date: date,
    team_name: str,
    events: list | None,
    generated_at: datetime,
) -> str:
    events = events or []

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

    p: list[str] = []
    p.append(
        "<html><body style=\"font-family:-apple-system,'Segoe UI',Arial,"
        "sans-serif;line-height:1.6;color:#222;max-width:900px;margin:0 auto;padding:16px;\">"
    )
    p.append(
        f"<h1 style='border-bottom:2px solid #4a6cf7;padding-bottom:8px;'>"
        f"{esc(team_name)} 日报 — {esc(report_date.isoformat())}"
        f"<span style='font-size:15px;color:#666;font-weight:normal;'>"
        f"（{esc(weekday_cn)}）</span></h1>"
    )

    if total_commits == 0:
        p.append("<h3>📭 今日暂无代码提交记录</h3>")
        p.append("<p style='color:#666;'>可能原因：</p>")
        p.append("<ul style='color:#666;'>")
        p.append("<li>今天没有 push 代码</li>")
        p.append("<li>提交的作者未映射到团队成员</li>")
        p.append("</ul>")
        p.append(
            f"<p style='color:#888;font-size:13px;'>"
            f"采集仓库: {esc(', '.join(repos) or '（未配置）')}<br>"
            f"采集窗口: {esc(report_date.isoformat())} 00:00 ~ {esc(_fmt_time(generated_at))}<br>"
            f"生成时间: {esc(generated_at.strftime('%Y-%m-%d %H:%M:%S'))}</p>"
        )
        p.append("</body></html>")
        return "".join(p)

    # 概览
    p.append("<div style='background:#f0f3ff;border-radius:6px;padding:12px 16px;margin:12px 0;'>")
    p.append("<div style='font-weight:bold;margin-bottom:6px;'>📊 今日概览</div>")
    p.append("<table style='font-size:14px;'>")
    p.append(f"<tr><td style='padding:2px 12px 2px 0;color:#666;'>提交次数</td><td><b>{total_commits}</b> 次</td></tr>")
    p.append(f"<tr><td style='padding:2px 12px 2px 0;color:#666;'>涉及文件</td><td><b>{total_files}</b> 个（新增 {added} · 修改 {modified} · 删除 {removed}）</td></tr>")
    p.append(f"<tr><td style='padding:2px 12px 2px 0;color:#666;'>代码变更</td><td>"
             f"<span style='color:#2e7d32;'>+{total_add}</span> / "
             f"<span style='color:#c62828;'>-{total_del}</span>"
             f"（净 <b>{'+' if net >= 0 else ''}{net}</b> 行）</td></tr>")
    p.append(f"<tr><td style='padding:2px 12px 2px 0;color:#666;'>覆盖仓库</td><td><b>{len(repos)}</b> 个</td></tr>")
    p.append("</table></div>")

    # 本次新增文件
    if new_files:
        p.append("<div style='background:#eaf6ee;border-radius:6px;padding:12px 16px;margin:12px 0;'>")
        p.append(f"<div style='font-weight:bold;margin-bottom:6px;'>📤 本次新增文件（{len(new_files)} 个）</div>")
        p.append("<table style='font-size:13px;border-collapse:collapse;width:100%;'>")
        for nf in new_files:
            icon = _file_icon(nf["filename"])
            p.append(
                f"<tr><td style='padding:2px 8px 2px 0;'>"
                f"{esc(icon)} <code>{esc(nf['filename'])}</code></td>"
                f"<td style='padding:2px 8px;text-align:right;color:#2e7d32;'>+{nf['additions']}</td>"
                f"<td style='padding:2px 8px;text-align:right;color:#c62828;'>-{nf['deletions']}</td>"
                "</tr>"
            )
        p.append("</table></div>")

    # 本次删除文件
    if deleted_files:
        p.append("<div style='background:#fdeaea;border-radius:6px;padding:12px 16px;margin:12px 0;'>")
        p.append(f"<div style='font-weight:bold;margin-bottom:6px;'>🗑️ 本次删除文件（{len(deleted_files)} 个）</div>")
        p.append("<table style='font-size:13px;border-collapse:collapse;width:100%;'>")
        for df in deleted_files:
            icon = _file_icon(df["filename"])
            p.append(
                f"<tr><td style='padding:2px 8px 2px 0;'>"
                f"{esc(icon)} <code>{esc(df['filename'])}</code></td>"
                f"<td style='padding:2px 8px;text-align:right;color:#2e7d32;'>+{df['additions']}</td>"
                f"<td style='padding:2px 8px;text-align:right;color:#c62828;'>-{df['deletions']}</td>"
                "</tr>"
            )
        p.append("</table></div>")

    for repo in repos or ["（未指定仓库）"]:
        repo_commits = [c for c in all_commits if getattr(c, "repo", "") == repo]
        r_add = sum(int(getattr(c, "additions", 0) or 0) for c in repo_commits)
        r_del = sum(int(getattr(c, "deletions", 0) or 0) for c in repo_commits)
        r_files = sum(int(getattr(c, "files_changed", 0) or 0) for c in repo_commits)

        p.append(f"<h2 style='color:#4a6cf7;margin-top:24px;'>📦 {esc(repo)}</h2>")
        p.append(
            f"<div style='color:#666;font-size:14px;margin-bottom:10px;'>"
            f"{len(repo_commits)} 次提交 · {r_files} 个文件 · "
            f"<span style='color:#2e7d32;'>+{r_add}</span> / "
            f"<span style='color:#c62828;'>-{r_del}</span></div>"
        )

        for m in members:
            commits = [c for c in (getattr(m, "commits", []) or [])
                       if getattr(c, "repo", "") == repo]
            if not commits:
                continue
            m_add = sum(int(getattr(c, "additions", 0) or 0) for c in commits)
            m_del = sum(int(getattr(c, "deletions", 0) or 0) for c in commits)

            p.append(
                f"<h3 style='margin-top:20px;'>👤 {esc(m.name)}"
                f" <code style='font-weight:normal;color:#666;'>({esc(m.github_username)})</code>"
                f" <span style='font-weight:normal;font-size:14px;color:#666;'>·"
                f" {len(commits)} 次提交 · "
                f"<span style='color:#2e7d32;'>+{m_add}</span> / "
                f"<span style='color:#c62828;'>-{m_del}</span></span></h3>"
            )

            buckets = {"morning": [], "afternoon": [], "evening": []}
            for c in commits:
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
                    ts = _fmt_time(getattr(c, "timestamp", None))
                    msg = esc((getattr(c, "message", "") or "").strip().splitlines()[0])
                    sha = esc(_short_sha(getattr(c, "sha", "")))
                    additions = int(getattr(c, "additions", 0) or 0)
                    deletions = int(getattr(c, "deletions", 0) or 0)
                    url = _commit_link(c)

                    p.append(
                        "<div style='border-left:3px solid #4a6cf7;padding:10px 14px;"
                        "margin:10px 0;background:#f8f9fc;border-radius:4px;'>"
                    )
                    p.append(f"<div><b>{idx:02d}. {ts} · {msg}</b></div>")
                    p.append(f"<div style='color:#888;font-size:13px;margin:4px 0;'><code>{sha}</code></div>")

                    p.append(
                        "<table style='border-collapse:collapse;width:100%;"
                        "font-size:13px;margin:6px 0;'>"
                    )
                    p.append(
                        "<tr style='color:#666;'>"
                        "<th style='text-align:left;padding:3px 8px 3px 0;'>文件</th>"
                        "<th style='text-align:right;padding:3px 8px;'>增</th>"
                        "<th style='text-align:right;padding:3px 8px;'>删</th>"
                        "<th style='text-align:center;padding:3px 8px;'>状态</th>"
                        "</tr>"
                    )
                    for fd in _commit_files(c):
                        fname = getattr(fd, "filename", "") or "?"
                        add = int(getattr(fd, "additions", 0) or 0)
                        dele = int(getattr(fd, "deletions", 0) or 0)
                        status_cn = _status_cn(getattr(fd, "status", "modified"))
                        icon = _file_icon(fname)
                        p.append(
                            f"<tr><td style='padding:2px 8px 2px 0;'>"
                            f"{esc(icon)} <code>{esc(fname)}</code></td>"
                            f"<td style='padding:2px 8px;text-align:right;color:#2e7d32;'>+{add}</td>"
                            f"<td style='padding:2px 8px;text-align:right;color:#c62828;'>-{dele}</td>"
                            f"<td style='padding:2px 8px;text-align:center;color:#666;'>{esc(status_cn)}</td>"
                            "</tr>"
                        )
                    p.append(
                        f"<tr><td style='padding:4px 8px 4px 0;color:#666;"
                        f"font-weight:bold;'>小计</td>"
                        f"<td style='padding:4px 8px;text-align:right;color:#2e7d32;"
                        f"font-weight:bold;'>+{additions}</td>"
                        f"<td style='padding:4px 8px;text-align:right;color:#c62828;"
                        f"font-weight:bold;'>-{deletions}</td>"
                        "<td></td></tr>"
                        "</table>"
                    )
                    if url:
                        p.append(
                            f"<div style='font-size:12px;'>"
                            f"<a href='{esc(url)}' style='color:#4a6cf7;'>🔗 查看提交</a></div>"
                        )
                    p.append("</div>")

    # 公开活动
    if events:
        p.append("<hr style='border:none;border-top:1px solid #eee;margin:20px 0;'>")
        p.append("<h3>📢 今日 GitHub 公开活动</h3>")
        p.append("<ul style='font-size:14px;color:#333;'>")
        for e in sorted(events, key=lambda x: getattr(x, "timestamp", None) or datetime.min):
            ts = _fmt_time(getattr(e, "timestamp", None))
            summary = esc(getattr(e, "summary", ""))
            url = getattr(e, "url", "") or ""
            if url:
                p.append(
                    f"<li><code>{esc(ts)}</code> "
                    f"<a href='{esc(url)}' style='color:#4a6cf7;'>{summary}</a></li>"
                )
            else:
                p.append(f"<li><code>{esc(ts)}</code> {summary}</li>")
        p.append("</ul>")

    p.append("<hr style='border:none;border-top:1px solid #eee;margin:24px 0;'>")
    p.append(
        f"<p style='color:#999;font-size:12px;line-height:1.8;'>"
        f"数据来源　　GitHub · {esc(', '.join(repos) or '（未配置）')}<br>"
        f"采集窗口　　{esc(report_date.isoformat())} 00:00 ~ {esc(_fmt_time(generated_at))}（北京时间）<br>"
        f"生成时间　　{esc(generated_at.strftime('%Y-%m-%d %H:%M:%S'))}</p>"
    )
    p.append("</body></html>")
    return "".join(p)


def generate(
    members: list,
    report_date: date,
    team_name: str,
    *,
    events: list | None = None,
    generated_at: datetime | None = None,
) -> Report:
    """生成日报对象（Markdown + HTML）。"""
    generated_at = generated_at or datetime.now()
    md = _render_markdown(members, report_date, team_name, events, generated_at)
    html = _render_html(members, report_date, team_name, events, generated_at)
    return Report(
        date=report_date,
        team_name=team_name,
        markdown=md,
        html=html,
        generated_at=generated_at,
    )