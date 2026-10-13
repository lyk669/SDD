"""日报生成器 — 输出 Markdown + HTML（企业周报风格 v3.0，无 emoji）。

结构：
  # 标题 + 日期期数
  ## 一句话结论
  ## 今日概览
  ## 今日亮点
  ## 工作明细（按性质）
  ## 飞书协作动态
    ### 任务进展
    ### 群讨论要点
    <details>原始消息</details>
  ## 问题与思考（支持 AI 草稿 / 用户输入 双源）
  ## 明日计划（同上）
  ## 风险与待办
  底部元信息
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


_STATUS_CN = {
    "added": "新增", "modified": "修改",
    "removed": "删除", "deleted": "删除", "renamed": "重命名",
}

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_TYPE_CN = {"decision": "决策", "todo": "待办", "issue": "问题"}

_SOURCE_LABEL = {
    "ai": "AI 草稿",
    "manual": "用户输入",
    "hybrid": "AI + 用户混合",
}


# ---------- 工具函数 ----------

def _to_local(dt: datetime) -> datetime:
    try:
        return dt.astimezone()
    except Exception:  # noqa: BLE001
        return dt


def _fmt_time(dt) -> str:
    if dt is None:
        return "--:--"
    return _to_local(dt).strftime("%H:%M")


def _fmt_datetime(dt) -> str:
    if dt is None:
        return "---- --:--"
    return _to_local(dt).strftime("%Y-%m-%d %H:%M")


def _fmt_num(n) -> str:
    try:
        return f"{int(n):,}"
    except Exception:  # noqa: BLE001
        return str(n)


def _short_sha(sha) -> str:
    return (sha or "-------")[:7]


def _status_cn(status: str) -> str:
    return _STATUS_CN.get((status or "").lower(), "修改")


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


def _sort_messages(messages: list) -> list:
    return sorted(
        messages or [],
        key=lambda x: getattr(x, "timestamp", None) or datetime.min,
    )


def _delta_str(cur: int, prev):
    if prev is None:
        return "—"
    diff = cur - prev
    if prev == 0:
        if cur == 0:
            return "—"
        return f"↑ {cur}"
    pct = diff / prev * 100
    if abs(diff) < 1e-9:
        return "—"
    sign = "↑" if diff > 0 else "↓"
    return f"{sign} {abs(pct):.1f}%"


def _compute_metrics(members, feishu_messages, tasks, yesterday_stats, feishu_stats):
    all_commits = [c for m in members for c in (getattr(m, "commits", []) or [])]
    total_files = sum(int(getattr(c, "files_changed", 0) or 0) for c in all_commits)
    total_add = sum(int(getattr(c, "additions", 0) or 0) for c in all_commits)
    total_del = sum(int(getattr(c, "deletions", 0) or 0) for c in all_commits)
    net = total_add - total_del
    kept = int(feishu_stats.get("kept", len(feishu_messages)) or 0)
    task_count = len(getattr(tasks, "records", []) or []) if tasks else 0

    y = yesterday_stats or {}
    today = {
        "commits": len(all_commits),
        "files": total_files,
        "additions": total_add,
        "deletions": total_del,
        "net": net,
        "msgs": kept,
        "tasks": task_count,
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
            "tasks": int(y.get("tasks", 0) or 0) if "tasks" in y else None,
        }
    return {"today": today, "yesterday": yest}


def _group_risks_by_category(risks: list) -> dict:
    result: dict[str, list[str]] = {}
    for r in (risks or []):
        cat = (r.get("category") or "其他").strip()
        text = (r.get("text") or "").strip()
        if not text:
            continue
        result.setdefault(cat, []).append(text)
    return result


def _extract_task_changes(tasks) -> list:
    out = []
    for t in (getattr(tasks, "records", []) or []):
        title = getattr(t, "title", "") or ""
        assignee = getattr(t, "assignee", "") or "-"
        sf = getattr(t, "status_from", "") or "—"
        st = getattr(t, "status_to", "") or "—"
        ts = _fmt_time(getattr(t, "updated_at", None))
        if not title:
            continue
        out.append({
            "title": title,
            "assignee": assignee,
            "status_from": sf,
            "status_to": st,
            "time": ts,
        })
    return out


def _render_dual_source_markdown(
    final_items: list,
    ai_items: list,
    source: str,
    empty_hint: str = "_（无内容）_",
) -> list[str]:
    """渲染「问题与思考」或「明日计划」的双源 Markdown。

    规则：
      - source = "ai"     → 只显示 final（即 AI 草稿），无折叠
      - source = "manual" → 只显示 final（用户输入），AI 草稿折叠
      - source = "hybrid" → 显示 final，AI 草稿折叠
    """
    lines: list[str] = []
    src_label = _SOURCE_LABEL.get(source, "AI 草稿")

    # 提取纯文本
    def _texts(items):
        out = []
        for it in (items or []):
            if isinstance(it, dict):
                t = (it.get("text") or "").strip()
            else:
                t = str(it).strip()
            if t:
                out.append(t)
        return out

    final_texts = _texts(final_items)
    ai_texts = _texts(ai_items)

    lines.append(f"> 来源：{src_label}")
    lines.append("")

    if final_texts:
        for i, t in enumerate(final_texts, 1):
            lines.append(f"{i}. {t}")
        lines.append("")
    else:
        lines.append(empty_hint)
        lines.append("")

    # 需要折叠 AI 草稿的情况
    if source in {"manual", "hybrid"} and ai_texts:
        lines.append("<details>")
        lines.append(f"<summary>AI 草稿（点开对比）</summary>")
        lines.append("")
        for i, t in enumerate(ai_texts, 1):
            lines.append(f"{i}. {t}")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    return lines


def _render_dual_source_html(
    final_items: list,
    ai_items: list,
    source: str,
    empty_hint: str = "<p style='color:#999;'>（无内容）</p>",
) -> str:
    """渲染双源 HTML。"""
    src_label = _SOURCE_LABEL.get(source, "AI 草稿")
    parts: list[str] = []

    def _texts(items):
        out = []
        for it in (items or []):
            if isinstance(it, dict):
                t = (it.get("text") or "").strip()
            else:
                t = str(it).strip()
            if t:
                out.append(t)
        return out

    final_texts = _texts(final_items)
    ai_texts = _texts(ai_items)

    parts.append(
        f"<p style='color:#888;font-size:13px;margin:6px 0;'>来源：{_html.escape(src_label)}</p>"
    )

    if final_texts:
        parts.append("<ol style='margin:8px 0;padding-left:24px;'>")
        for t in final_texts:
            parts.append(f"<li style='margin:6px 0;color:#333;'>{_html.escape(t)}</li>")
        parts.append("</ol>")
    else:
        parts.append(empty_hint)

    if source in {"manual", "hybrid"} and ai_texts:
        parts.append("<details style='margin-top:12px;'>")
        parts.append(
            "<summary style='cursor:pointer;color:#4a6cf7;'>AI 草稿（点开对比）</summary>"
        )
        parts.append("<ol style='margin:8px 0;padding-left:24px;color:#666;'>")
        for t in ai_texts:
            parts.append(f"<li style='margin:4px 0;'>{_html.escape(t)}</li>")
        parts.append("</ol>")
        parts.append("</details>")

    return "".join(parts)


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
    work_classification: dict | None = None,
    issue_number: int = 0,
    risks: list[dict] | None = None,
    problems: list[dict] | None = None,
    next_plan: list[dict] | None = None,
    ai_problems: list[dict] | None = None,
    ai_next_plan: list[dict] | None = None,
    reflection_source: str = "ai",
    tasks=None,
    **_ignored,
) -> str:
    events = events or []
    feishu_messages = feishu_messages or []
    feishu_stats = feishu_stats or {}
    yesterday_stats = yesterday_stats or {}
    highlights = highlights or []
    action_items = action_items or []
    commit_summaries = commit_summaries or {}
    risks = risks or []
    problems = problems or []
    next_plan = next_plan or []
    ai_problems = ai_problems or []
    ai_next_plan = ai_next_plan or []
    summary = (summary or "").strip()

    all_commits = [c for m in members for c in (getattr(m, "commits", []) or [])]
    total_commits = len(all_commits)
    weekday_cn = _WEEKDAY_CN[report_date.weekday()]

    kept = int(feishu_stats.get("kept", len(feishu_messages)) or 0)
    skipped = int(feishu_stats.get("skipped", 0) or 0)
    total_msgs = int(feishu_stats.get("total", kept + skipped) or 0)

    metrics = _compute_metrics(members, feishu_messages, tasks, yesterday_stats, feishu_stats)
    today = metrics["today"]
    yest = metrics["yesterday"]

    task_changes = _extract_task_changes(tasks)
    risks_grouped = _group_risks_by_category(risks)

    lines: list[str] = []

    # ============ 标题 ============
    lines.append(f"# {team_name}")
    issue_str = f"· 第 {issue_number} 期" if issue_number else ""
    lines.append(f"## {report_date.isoformat()}（{weekday_cn}）{issue_str}")
    lines.append("")
    lines.append(f"> 项目：智能日报生成器　负责人：刘雨可  ")
    lines.append(f"> 采集窗口：{report_date.isoformat()} 00:00 ~ {_fmt_time(generated_at)}（北京时间）")
    lines.append("")

    # ============ 一句话结论 ============
    if summary:
        lines.append("## 一句话结论")
        lines.append("")
        for para in summary.split("\n"):
            para = para.strip()
            if para:
                lines.append(para)
        lines.append("")

    # ============ 今日概览 ============
    lines.append("## 今日概览")
    lines.append("")
    lines.append("| 指标 | 今日 | 昨日 | 环比 |")
    lines.append("|:-----|:----:|:----:|:----:|")

    def row(label, key):
        cur_v = today.get(key, 0)
        y_v = yest.get(key) if yest else None
        cur_s = _fmt_num(cur_v)
        y_s = _fmt_num(y_v) if y_v is not None else "—"
        return f"| {label} | {cur_s} | {y_s} | {_delta_str(cur_v, y_v)} |"

    lines.append(row("代码提交", "commits"))
    lines.append(row("涉及文件", "files"))
    lines.append(f"| 代码变更 | +{_fmt_num(today['additions'])} / -{_fmt_num(today['deletions'])} | "
                 + (f"+{_fmt_num(yest['additions'])} / -{_fmt_num(yest['deletions'])}"
                    if yest else "—")
                 + f" | {_delta_str(today['net'], yest['net'] if yest else None)} |")
    lines.append(row("飞书任务", "tasks"))
    lines.append(row("群消息", "msgs"))
    lines.append("")

    # ============ 今日亮点 ============
    if highlights:
        lines.append("## 今日亮点")
        lines.append("")
        for i, h in enumerate(highlights, 1):
            lines.append(f"{i}. {h}")
        lines.append("")

    # ============ 工作明细 ============
    lines.append("## 工作明细（按性质）")
    lines.append("")
    categories = (work_classification or {}).get("categories") or []
    if categories:
        lines.append("| 类型 | 占比 | 做了什么 |")
        lines.append("|:-----|:----:|:---------|")
        for cat in categories:
            if cat.get("count", 0) == 0:
                continue
            label = cat.get("label", "")
            percent = f"{cat.get('percent', 0)}%"
            details = cat.get("details", "") or ""
            details = details.replace("|", "\\|")
            lines.append(f"| {label} | {percent} | {details} |")
        lines.append("")
    else:
        lines.append("_本日无代码提交记录。_")
        lines.append("")

    # ============ 飞书协作动态 ============
    has_any_feishu = bool(task_changes) or bool(feishu_messages) or bool(action_items)
    if has_any_feishu:
        lines.append("## 飞书协作动态")
        lines.append("")

        if task_changes:
            lines.append(f"### 任务进展（{len(task_changes)} 项状态流转）")
            lines.append("")
            lines.append("| 任务 | 负责人 | 状态流转 | 时间 |")
            lines.append("|:-----|:------:|:---------|:----:|")
            for tc in task_changes:
                title = tc["title"].replace("|", "\\|")
                lines.append(
                    f"| {title} | {tc['assignee']} | "
                    f"{tc['status_from']} → {tc['status_to']} | {tc['time']} |"
                )
            lines.append("")

        if action_items:
            lines.append(f"### 群讨论要点（{len(action_items)} 条）")
            lines.append("")
            lines.append("| 类型 | 内容 | 相关人 |")
            lines.append("|:----:|:-----|:------:|")
            for item in action_items:
                t = _TYPE_CN.get(item.get("type", ""), "其他")
                text = (item.get("text", "") or "").replace("|", "\\|")
                people = item.get("people", "") or "-"
                lines.append(f"| {t} | {text} | {people} |")
            lines.append("")

        if feishu_messages:
            lines.append("<details>")
            lines.append(f"<summary>原始消息（{len(feishu_messages)} 条）</summary>")
            lines.append("")
            lines.append("| 时间 | 发言人 | 内容 |")
            lines.append("|:----:|:------:|:-----|")
            for msg in _sort_messages(feishu_messages):
                ts = _fmt_time(getattr(msg, "timestamp", None))
                sender = getattr(msg, "sender_name", "") or "未知"
                text = (getattr(msg, "text", "") or "").replace("\n", " ").replace("|", "\\|")
                lines.append(f"| {ts} | {sender} | {text} |")
            lines.append("")
            lines.append("</details>")
            lines.append("")

            if total_msgs or skipped:
                lines.append(f"> 本日共收到 {total_msgs} 条消息，保留 {kept} 条，"
                             f"过滤 {skipped} 条（撤回 / @某人 / 太短 / 系统消息）。")
                lines.append("")

    # ============ 问题与思考 ============
    if problems or ai_problems:
        lines.append("## 问题与思考")
        lines.append("")
        lines.extend(_render_dual_source_markdown(
            final_items=problems,
            ai_items=ai_problems,
            source=reflection_source,
            empty_hint="_（无内容）_",
        ))

    # ============ 明日计划 ============
    if next_plan or ai_next_plan:
        lines.append("## 明日计划")
        lines.append("")
        lines.extend(_render_dual_source_markdown(
            final_items=next_plan,
            ai_items=ai_next_plan,
            source=reflection_source,
            empty_hint="_（无内容）_",
        ))

    # ============ 风险与待办 ============
    if risks_grouped:
        lines.append("## 风险与待办")
        lines.append("")
        for cat, items in risks_grouped.items():
            lines.append(f"### {cat}")
            lines.append("")
            for text in items:
                lines.append(text)
                lines.append("")

    # ============ 底部 ============
    lines.append("---")
    lines.append("")
    lines.append(f"**数据来源**　GitHub · 飞书开放平台  ")
    lines.append(f"**文档版本**　v3.0 · 由智能日报生成器自动生成")

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
    work_classification: dict | None = None,
    issue_number: int = 0,
    risks: list[dict] | None = None,
    problems: list[dict] | None = None,
    next_plan: list[dict] | None = None,
    ai_problems: list[dict] | None = None,
    ai_next_plan: list[dict] | None = None,
    reflection_source: str = "ai",
    tasks=None,
    **_ignored,
) -> str:
    events = events or []
    feishu_messages = feishu_messages or []
    feishu_stats = feishu_stats or {}
    yesterday_stats = yesterday_stats or {}
    highlights = highlights or []
    action_items = action_items or []
    commit_summaries = commit_summaries or {}
    risks = risks or []
    problems = problems or []
    next_plan = next_plan or []
    ai_problems = ai_problems or []
    ai_next_plan = ai_next_plan or []
    summary = (summary or "").strip()

    def esc(s) -> str:
        return _html.escape(str(s or ""))

    all_commits = [c for m in members for c in (getattr(m, "commits", []) or [])]
    total_commits = len(all_commits)
    weekday_cn = _WEEKDAY_CN[report_date.weekday()]

    kept = int(feishu_stats.get("kept", len(feishu_messages)) or 0)
    skipped = int(feishu_stats.get("skipped", 0) or 0)
    total_msgs = int(feishu_stats.get("total", kept + skipped) or 0)

    metrics = _compute_metrics(members, feishu_messages, tasks, yesterday_stats, feishu_stats)
    today = metrics["today"]
    yest = metrics["yesterday"]

    task_changes = _extract_task_changes(tasks)
    risks_grouped = _group_risks_by_category(risks)

    h2_style = ("color:#1a237e;border-bottom:2px solid #4a6cf7;padding-bottom:6px;"
                "margin-top:32px;margin-bottom:16px;font-size:20px;")
    h3_style = ("color:#283593;margin-top:22px;margin-bottom:10px;font-size:16px;")
    table_style = "border-collapse:collapse;width:100%;font-size:13px;margin:8px 0;"
    th_style = ("background:#f0f3ff;color:#1a237e;padding:6px 10px;"
                "text-align:left;border:1px solid #d0d7f0;font-weight:600;")
    th_c_style = th_style.replace("text-align:left", "text-align:center")
    td_style = "padding:5px 10px;border:1px solid #e0e5f5;"
    td_c_style = td_style.replace("padding:5px 10px", "padding:5px 10px;text-align:center")
    quote_style = ("background:#fff8e1;border-left:4px solid #f59e0b;"
                   "padding:10px 14px;margin:10px 0;border-radius:4px;")

    def table(headers, rows, aligns=None):
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

    def delta_html(cur, prev):
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
    issue_str = f"· 第 {issue_number} 期" if issue_number else ""
    p.append(f"<h1 style='color:#1a237e;margin-bottom:4px;font-size:26px;'>{esc(team_name)}</h1>")
    p.append(f"<h2 style='color:#3949ab;margin-top:0;font-size:18px;font-weight:500;'>"
             f"{esc(report_date.isoformat())}（{esc(weekday_cn)}）{esc(issue_str)}</h2>")
    p.append(f"<div style='color:#666;font-size:13px;margin-top:8px;'>"
             f"项目：智能日报生成器　负责人：刘雨可<br>"
             f"采集窗口：{esc(report_date.isoformat())} 00:00 ~ {esc(_fmt_time(generated_at))}（北京时间）</div>")

    # 一句话结论
    if summary:
        p.append("<h2 style='" + h2_style + "'>一句话结论</h2>")
        p.append("<div style='" + quote_style + "'>")
        for para in summary.split("\n"):
            para = esc(para.strip())
            if para:
                p.append(f"<div style='color:#333;margin:4px 0;'>{para}</div>")
        p.append("</div>")

    # 今日概览
    p.append("<h2 style='" + h2_style + "'>今日概览</h2>")
    headers = ["指标", "今日", "昨日", "环比"]
    aligns = ["left", "center", "center", "center"]

    def build_metric_row(label, key):
        cur_v = today.get(key, 0)
        y_v = yest.get(key) if yest else None
        cur_s = _fmt_num(cur_v)
        y_s = _fmt_num(y_v) if y_v is not None else "—"
        return [label, cur_s, y_s, delta_html(cur_v, y_v)]

    rows = [
        build_metric_row("代码提交", "commits"),
        build_metric_row("涉及文件", "files"),
    ]
    y_change_s = (f"+{_fmt_num(yest['additions'])} / -{_fmt_num(yest['deletions'])}"
                  if yest else "—")
    rows.append([
        "代码变更",
        f"+{_fmt_num(today['additions'])} / -{_fmt_num(today['deletions'])}",
        y_change_s,
        delta_html(today['net'], yest['net'] if yest else None),
    ])
    rows.append(build_metric_row("飞书任务", "tasks"))
    rows.append(build_metric_row("群消息", "msgs"))
    p.append(table(headers, rows, aligns))

    # 今日亮点
    if highlights:
        p.append("<h2 style='" + h2_style + "'>今日亮点</h2>")
        p.append("<ol style='margin:8px 0;padding-left:24px;'>")
        for h in highlights:
            p.append(f"<li style='margin:4px 0;'>{esc(h)}</li>")
        p.append("</ol>")

    # 工作明细
    p.append("<h2 style='" + h2_style + "'>工作明细（按性质）</h2>")
    categories = (work_classification or {}).get("categories") or []
    if categories:
        crows = []
        for cat in categories:
            if cat.get("count", 0) == 0:
                continue
            crows.append([
                esc(cat.get("label", "")),
                f"{cat.get('percent', 0)}%",
                esc(cat.get("details", "")),
            ])
        p.append(table(["类型", "占比", "做了什么"], crows, ["left", "center", "left"]))
    else:
        p.append("<p style='color:#999;'>本日无代码提交记录。</p>")

    # 飞书协作动态
    has_any_feishu = bool(task_changes) or bool(feishu_messages) or bool(action_items)
    if has_any_feishu:
        p.append("<h2 style='" + h2_style + "'>飞书协作动态</h2>")

        if task_changes:
            p.append(f"<h3 style='" + h3_style + f"'>任务进展（{len(task_changes)} 项状态流转）</h3>")
            trows = []
            for tc in task_changes:
                trows.append([
                    esc(tc["title"]), esc(tc["assignee"]),
                    f"{esc(tc['status_from'])} → {esc(tc['status_to'])}",
                    esc(tc["time"]),
                ])
            p.append(table(["任务", "负责人", "状态流转", "时间"], trows,
                           ["left", "center", "center", "center"]))

        if action_items:
            p.append(f"<h3 style='" + h3_style + f"'>群讨论要点（{len(action_items)} 条）</h3>")
            arows = []
            for item in action_items:
                t = _TYPE_CN.get(item.get("type", ""), "其他")
                arows.append([
                    esc(t),
                    esc(item.get("text", "")),
                    esc(item.get("people", "") or "-"),
                ])
            p.append(table(["类型", "内容", "相关人"], arows,
                           ["center", "left", "center"]))

        if feishu_messages:
            p.append("<details style='margin-top:12px;'>")
            p.append(f"<summary style='cursor:pointer;color:#4a6cf7;'>"
                     f"原始消息（{len(feishu_messages)} 条）</summary>")
            mrows = []
            for msg in _sort_messages(feishu_messages):
                ts = _fmt_time(getattr(msg, "timestamp", None))
                sender = esc(getattr(msg, "sender_name", "") or "未知")
                text = esc(getattr(msg, "text", "") or "")
                mrows.append([esc(ts), sender, text])
            p.append(table(["时间", "发言人", "内容"], mrows,
                           ["center", "center", "left"]))
            p.append("</details>")

            if total_msgs or skipped:
                p.append(f"<p style='color:#666;font-size:13px;margin-top:8px;'>"
                         f"本日共收到 <b>{total_msgs}</b> 条消息，保留 <b>{kept}</b> 条，"
                         f"过滤 <b>{skipped}</b> 条（撤回 / @某人 / 太短 / 系统消息）。</p>")

    # 问题与思考
    if problems or ai_problems:
        p.append("<h2 style='" + h2_style + "'>问题与思考</h2>")
        p.append("<div style='background:#f5f0ff;border-left:4px solid #8b5cf6;"
                 "border-radius:6px;padding:12px 16px;margin:10px 0;'>")
        p.append(_render_dual_source_html(
            final_items=problems,
            ai_items=ai_problems,
            source=reflection_source,
            empty_hint="<p style='color:#999;'>（无内容）</p>",
        ))
        p.append("</div>")

    # 明日计划
    if next_plan or ai_next_plan:
        p.append("<h2 style='" + h2_style + "'>明日计划</h2>")
        p.append("<div style='background:#e8f5e9;border-left:4px solid #2e7d32;"
                 "border-radius:6px;padding:12px 16px;margin:10px 0;'>")
        p.append(_render_dual_source_html(
            final_items=next_plan,
            ai_items=ai_next_plan,
            source=reflection_source,
            empty_hint="<p style='color:#999;'>（无内容）</p>",
        ))
        p.append("</div>")

    # 风险与待办
    if risks_grouped:
        p.append("<h2 style='" + h2_style + "'>风险与待办</h2>")
        for cat, items in risks_grouped.items():
            p.append(f"<h3 style='" + h3_style + f"'>{esc(cat)}</h3>")
            for text in items:
                p.append(f"<p style='margin:6px 0;color:#333;'>{esc(text)}</p>")

    # 底部
    p.append("<hr style='border:none;border-top:2px solid #e0e5f5;margin:32px 0 20px 0;'>")
    p.append("<div style='color:#999;font-size:12px;line-height:1.9;'>")
    p.append(f"<b>数据来源</b>　GitHub · 飞书开放平台<br>")
    p.append(f"<b>文档版本</b>　v3.0 · 由智能日报生成器自动生成")
    p.append("</div>")

    p.append("</body></html>")
    return "".join(p)


# ---------- 对外入口 ----------

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
    suggestions: list[dict] | None = None,      # 兼容保留，内部不使用
    work_classification: dict | None = None,
    issue_number: int = 0,
    risks: list[dict] | None = None,
    problems: list[dict] | None = None,
    next_plan: list[dict] | None = None,
    ai_problems: list[dict] | None = None,
    ai_next_plan: list[dict] | None = None,
    reflection_source: str = "ai",
    tasks=None,
) -> Report:
    generated_at = generated_at or datetime.now()
    md = _render_markdown(
        members, report_date, team_name, events, feishu_messages,
        summary, generated_at, feishu_stats, yesterday_stats, highlights,
        action_items, commit_summaries, comparison,
        work_classification, issue_number,
        risks, problems, next_plan, ai_problems, ai_next_plan,
        reflection_source, tasks,
    )
    html = _render_html(
        members, report_date, team_name, events, feishu_messages,
        summary, generated_at, feishu_stats, yesterday_stats, highlights,
        action_items, commit_summaries, comparison,
        work_classification, issue_number,
        risks, problems, next_plan, ai_problems, ai_next_plan,
        reflection_source, tasks,
    )
    return Report(
        date=report_date,
        team_name=team_name,
        markdown=md,
        html=html,
        generated_at=generated_at,
    )