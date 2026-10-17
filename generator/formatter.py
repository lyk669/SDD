"""日报生成器 — 输出 Markdown + HTML（企业周报风格 v4.3，无 emoji）。

v4.3 改动：
  - generate() 里一次性构建 model（metrics / work / task_changes / risks_grouped）
  - _render_markdown / _render_html 从 model 里读数据，不再各自重算
  - 消除了「同一份分析算两遍」的重复计算

结构：
  # 标题 + 日期期数
  ## 一句话结论
  ## 今日概览
  ## 今日亮点
  ## 工作明细（分类概览 + 全天汇总 + 提交明细）
  ## 飞书协作动态
  ## 问题与思考
  ## 明日计划
  ## 风险与待办
  底部元信息
"""

from __future__ import annotations

import html as _html
from dataclasses import dataclass
from datetime import date, datetime

from generator.classify import classify_commit_message, label_of
from shared.logger import get_logger

logger = get_logger("daily_report.generator.formatter")


@dataclass
class Report:
    date: date
    team_name: str
    markdown: str
    html: str
    generated_at: datetime


# ============ 常量 ============

_STATUS_TEXT = {
    "added": "新增",
    "modified": "修改",
    "removed": "删除",
    "deleted": "删除",
    "renamed": "重命名",
}

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_TYPE_CN = {"decision": "决策", "todo": "待办", "issue": "问题"}

_SOURCE_LABEL = {
    "ai": "AI 草稿",
    "manual": "用户输入",
    "hybrid": "AI + 用户混合",
}


# ============ 工具函数 ============

def _to_local(dt: datetime) -> datetime:
    try:
        return dt.astimezone()
    except Exception:  # noqa: BLE001
        return dt


def _fmt_time(dt) -> str:
    if dt is None:
        return "--:--"
    return _to_local(dt).strftime("%H:%M")


def _fmt_num(n) -> str:
    try:
        return f"{int(n):,}"
    except Exception:  # noqa: BLE001
        return str(n)


def _status_text(status: str) -> str:
    return _STATUS_TEXT.get((status or "").lower(), "修改")


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


def _texts_of_items(items) -> list:
    out = []
    for it in (items or []):
        if isinstance(it, dict):
            t = (it.get("text") or "").strip()
        else:
            t = str(it).strip()
        if t:
            out.append(t)
    return out


# ============ 数据计算（一次性） ============

def _compute_metrics(members, feishu_messages, tasks, yesterday_stats, feishu_stats) -> dict:
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


def _build_work_summary(members: list, commit_summaries: dict) -> dict:
    """构建工作明细的数据结构。"""
    all_commits = [c for m in members for c in (getattr(m, "commits", []) or [])]
    if not all_commits:
        return {
            "categories": [],
            "commits": [],
            "total": {
                "commits": 0, "files": 0, "added": 0, "modified": 0, "removed": 0,
                "additions": 0, "deletions": 0,
            },
        }

    from collections import defaultdict
    bucket: dict[str, list] = defaultdict(list)
    for c in all_commits:
        msg = (getattr(c, "message", "") or "").strip()
        key = classify_commit_message(msg)
        bucket[key].append(c)

    total = len(all_commits)
    categories = []
    for key, items in bucket.items():
        count = len(items)
        percent = round(count / total * 100)
        categories.append({
            "label": label_of(key),
            "count": count,
            "percent": percent,
            "commits": items,
        })
    categories.sort(key=lambda x: -x["count"])

    sorted_commits = sorted(
        all_commits,
        key=lambda c: getattr(c, "timestamp", None) or datetime.min,
        reverse=True,
    )

    commit_details = []
    total_add = total_del = total_files = 0
    status_count = {"added": 0, "modified": 0, "removed": 0}

    for idx, c in enumerate(sorted_commits, 1):
        ts = _fmt_time(getattr(c, "timestamp", None))
        msg = (getattr(c, "message", "") or "").strip().splitlines()[0]
        sha = getattr(c, "sha", "") or ""
        url = _commit_link(c)
        add = int(getattr(c, "additions", 0) or 0)
        dele = int(getattr(c, "deletions", 0) or 0)
        fc = int(getattr(c, "files_changed", 0) or 0)
        ai_summary = commit_summaries.get(sha, "") if commit_summaries else ""

        files = []
        for fd in _commit_files(c):
            fname = getattr(fd, "filename", "") or "?"
            fadd = int(getattr(fd, "additions", 0) or 0)
            fdel = int(getattr(fd, "deletions", 0) or 0)
            raw_status = (getattr(fd, "status", "modified") or "modified").lower()
            fstatus = _status_text(raw_status)
            files.append({
                "filename": fname, "status": fstatus,
                "additions": fadd, "deletions": fdel,
            })
            total_files += 1
            if raw_status == "added":
                status_count["added"] += 1
            elif raw_status in {"removed", "deleted"}:
                status_count["removed"] += 1
            else:
                status_count["modified"] += 1

        total_add += add
        total_del += dele

        commit_details.append({
            "idx": idx, "time": ts, "msg": msg,
            "sha": sha, "short_sha": sha[:7] if sha else "-------",
            "url": url,
            "additions": add, "deletions": dele, "files_count": fc,
            "ai_summary": ai_summary,
            "files": files,
        })

    return {
        "categories": categories,
        "commits": commit_details,
        "total": {
            "commits": total,
            "files": total_files,
            "added": status_count["added"],
            "modified": status_count["modified"],
            "removed": status_count["removed"],
            "additions": total_add,
            "deletions": total_del,
        },
    }


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


def _group_risks_by_category(risks: list) -> dict:
    result: dict[str, list[str]] = {}
    for r in (risks or []):
        cat = (r.get("category") or "其他").strip()
        text = (r.get("text") or "").strip()
        if not text:
            continue
        result.setdefault(cat, []).append(text)
    return result


def _build_model(
    members: list,
    feishu_messages: list,
    tasks,
    yesterday_stats: dict | None,
    feishu_stats: dict | None,
    commit_summaries: dict | None,
    risks: list | None,
) -> dict:
    """一次性构建所有渲染所需的数据。

    v4.3 新增：把之前分别跑两遍的计算合并成一份。
    """
    feishu_stats = feishu_stats or {}
    commit_summaries = commit_summaries or {}
    risks = risks or []
    return {
        "metrics": _compute_metrics(
            members, feishu_messages, tasks, yesterday_stats, feishu_stats,
        ),
        "work": _build_work_summary(members, commit_summaries),
        "task_changes": _extract_task_changes(tasks),
        "risks_grouped": _group_risks_by_category(risks),
    }


# ============ 单条 commit 渲染 ============

def _render_one_commit_markdown(c: dict) -> list:
    lines = []
    lines.append(f"#### {c['idx']}. {c['time']} · {c['msg']}")
    lines.append("")
    sha_link = f"[查看提交]({c['url']})" if c['url'] else "（无链接）"
    lines.append(f"- Commit `{c['short_sha']}` · {sha_link}")
    lines.append(
        f"- 变更：**+{_fmt_num(c['additions'])} / -{_fmt_num(c['deletions'])}**"
        f" · {c['files_count']} 个文件"
    )
    if c['ai_summary']:
        lines.append(f"- 摘要：{c['ai_summary']}")
    lines.append("")

    if c['files']:
        lines.append("| 文件 | 状态 | 增 / 删 |")
        lines.append("|:-----|:----:|:------:|")
        for f in c['files']:
            fname = f['filename'].replace("|", "\\|")
            lines.append(
                f"| `{fname}` | {f['status']} "
                f"| +{_fmt_num(f['additions'])} / -{_fmt_num(f['deletions'])} |"
            )
        lines.append("")
    return lines


def _render_one_commit_html(c: dict, esc) -> str:
    parts = []
    parts.append(
        "<div style='border:1px solid #e0e5f5;border-radius:6px;"
        "margin:12px 0;overflow:hidden;'>"
    )
    parts.append(
        f"<div style='background:#4a6cf7;color:#fff;padding:8px 14px;"
        f"font-weight:600;font-size:14px;'>"
        f"{c['idx']}. {esc(c['time'])} · {esc(c['msg'])}</div>"
    )
    parts.append("<div style='padding:12px 14px;'>")
    sha = esc(c['short_sha'])
    url = esc(c['url'])
    if url:
        link_html = f"<a href='{url}' style='color:#4a6cf7;'>查看提交</a>"
    else:
        link_html = "<span style='color:#999;'>（无链接）</span>"
    parts.append(
        f"<p style='margin:4px 0;font-size:13px;'>"
        f"<b>Commit</b> <code>{sha}</code> · {link_html}</p>"
    )
    parts.append(
        f"<p style='margin:4px 0;font-size:13px;'>"
        f"<b>变更</b> "
        f"<span style='color:#2e7d32;'>+{_fmt_num(c['additions'])}</span> / "
        f"<span style='color:#c62828;'>-{_fmt_num(c['deletions'])}</span>"
        f" · {c['files_count']} 个文件</p>"
    )
    if c['ai_summary']:
        parts.append(
            f"<p style='margin:4px 0;font-size:13px;'>"
            f"<b>摘要</b> {esc(c['ai_summary'])}</p>"
        )

    if c['files']:
        parts.append(
            "<table style='border-collapse:collapse;width:100%;"
            "font-size:12px;margin:8px 0 0 0;'>"
            "<tr style='color:#666;'>"
            "<th style='text-align:left;padding:4px 8px;"
            "border:1px solid #e0e5f5;background:#f5f7ff;'>文件</th>"
            "<th style='text-align:center;padding:4px 8px;"
            "border:1px solid #e0e5f5;background:#f5f7ff;width:60px;'>状态</th>"
            "<th style='text-align:center;padding:4px 8px;"
            "border:1px solid #e0e5f5;background:#f5f7ff;width:110px;'>增 / 删</th>"
            "</tr>"
        )
        for f in c['files']:
            fname = esc(f['filename'])
            fstatus = esc(f['status'])
            parts.append(
                f"<tr>"
                f"<td style='padding:3px 8px;border:1px solid #e0e5f5;'>"
                f"<code>{fname}</code></td>"
                f"<td style='padding:3px 8px;border:1px solid #e0e5f5;"
                f"text-align:center;'>{fstatus}</td>"
                f"<td style='padding:3px 8px;border:1px solid #e0e5f5;"
                f"text-align:center;'>"
                f"<span style='color:#2e7d32;'>+{_fmt_num(f['additions'])}</span> / "
                f"<span style='color:#c62828;'>-{_fmt_num(f['deletions'])}</span>"
                f"</td>"
                f"</tr>"
            )
        parts.append("</table>")

    parts.append("</div>")
    parts.append("</div>")
    return "".join(parts)


# ============ 双源渲染（问题与思考 / 明日计划） ============

def _render_dual_source_markdown(final_items, ai_items, source: str) -> list:
    lines = []
    src_label = _SOURCE_LABEL.get(source, "AI 草稿")
    final_texts = _texts_of_items(final_items)
    ai_texts = _texts_of_items(ai_items)

    lines.append(f"> 来源：{src_label}")
    lines.append("")

    if final_texts:
        for i, t in enumerate(final_texts, 1):
            lines.append(f"{i}. {t}")
        lines.append("")
    else:
        lines.append("_（无内容）_")
        lines.append("")

    if source in {"manual", "hybrid"} and ai_texts:
        lines.append("<details>")
        lines.append("<summary>AI 草稿（点开对比）</summary>")
        lines.append("")
        for i, t in enumerate(ai_texts, 1):
            lines.append(f"{i}. {t}")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    return lines


def _render_dual_source_html(final_items, ai_items, source: str) -> str:
    src_label = _SOURCE_LABEL.get(source, "AI 草稿")
    parts = []
    final_texts = _texts_of_items(final_items)
    ai_texts = _texts_of_items(ai_items)

    parts.append(
        f"<p style='color:#888;font-size:13px;margin:6px 0;'>"
        f"来源：{_html.escape(src_label)}</p>"
    )

    if final_texts:
        parts.append("<ol style='margin:8px 0;padding-left:24px;'>")
        for t in final_texts:
            parts.append(f"<li style='margin:6px 0;color:#333;'>{_html.escape(t)}</li>")
        parts.append("</ol>")
    else:
        parts.append("<p style='color:#999;'>（无内容）</p>")

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


# ============ Markdown 渲染 ============

def _render_markdown(
    *,
    model: dict,
    members: list,
    report_date: date,
    team_name: str,
    feishu_messages: list,
    summary: str,
    generated_at: datetime,
    feishu_stats: dict,
    yesterday_stats: dict,
    highlights: list,
    action_items: list,
    commit_summaries: dict,
    issue_number: int,
    risks: list,
    problems: list,
    next_plan: list,
    ai_problems: list,
    ai_next_plan: list,
    reflection_source: str,
) -> str:
    # 从 model 读预算好的数据（不再重算）
    metrics = model["metrics"]
    work = model["work"]
    task_changes = model["task_changes"]
    risks_grouped = model["risks_grouped"]

    today = metrics["today"]
    yest = metrics["yesterday"]

    weekday_cn = _WEEKDAY_CN[report_date.weekday()]

    kept = int(feishu_stats.get("kept", len(feishu_messages)) or 0)
    skipped = int(feishu_stats.get("skipped", 0) or 0)
    total_msgs = int(feishu_stats.get("total", kept + skipped) or 0)

    lines: list = []

    # 标题
    lines.append(f"# {team_name}")
    issue_str = f"· 第 {issue_number} 期" if issue_number else ""
    lines.append(f"## {report_date.isoformat()}（{weekday_cn}）{issue_str}")
    lines.append("")
    lines.append("> 项目：智能日报生成器　负责人：刘雨可  ")
    lines.append(f"> 采集窗口：{report_date.isoformat()} 00:00 ~ {_fmt_time(generated_at)}（北京时间）")
    lines.append("")

    # 一句话结论
    if summary:
        lines.append("## 一句话结论")
        lines.append("")
        for para in summary.split("\n"):
            para = para.strip()
            if para:
                lines.append(para)
        lines.append("")

    # 今日概览
    lines.append("## 今日概览")
    lines.append("")
    lines.append("| 指标 | 今日 | 昨日 | 环比 |")
    lines.append("|:-----|:----:|:----:|:----:|")

    def _row(label, key):
        cur_v = today.get(key, 0)
        y_v = yest.get(key) if yest else None
        cur_s = _fmt_num(cur_v)
        y_s = _fmt_num(y_v) if y_v is not None else "—"
        return f"| {label} | {cur_s} | {y_s} | {_delta_str(cur_v, y_v)} |"

    lines.append(_row("代码提交", "commits"))
    lines.append(_row("涉及文件", "files"))
    y_change_s = (
        f"+{_fmt_num(yest['additions'])} / -{_fmt_num(yest['deletions'])}"
        if yest else "—"
    )
    lines.append(
        f"| 代码变更 | +{_fmt_num(today['additions'])} / -{_fmt_num(today['deletions'])} "
        f"| {y_change_s} | {_delta_str(today['net'], yest['net'] if yest else None)} |"
    )
    lines.append(_row("飞书任务", "tasks"))
    lines.append(_row("群消息", "msgs"))
    lines.append("")

    # 今日亮点
    if highlights:
        lines.append("## 今日亮点")
        lines.append("")
        for i, h in enumerate(highlights, 1):
            lines.append(f"{i}. {h}")
        lines.append("")

    # 工作明细
    lines.append("## 工作明细")
    lines.append("")

    if not work["commits"]:
        lines.append("_本日无代码提交记录。_")
        lines.append("")
    else:
        lines.append("### 分类概览")
        lines.append("")
        lines.append("| 类型 | 占比 | 做了什么 |")
        lines.append("|:-----|:----:|:---------|")
        for cat in work["categories"]:
            if cat["count"] == 0:
                continue
            label = cat["label"]
            percent = f"{cat['percent']}%"
            msgs = [
                (getattr(c, "message", "") or "").split("\n")[0].strip()
                for c in cat["commits"]
            ]
            details = "、".join(msgs[:2])
            if len(msgs) > 2:
                details += f" 等 {len(msgs)} 项"
            details = details.replace("|", "\\|")
            lines.append(f"| {label} | {percent} | {details} |")
        lines.append("")

        t = work["total"]
        lines.append(
            f"**全天汇总**：{t['commits']} 次提交 · {t['files']} 个文件"
            f"（新增 {t['added']} · 修改 {t['modified']} · 删除 {t['removed']}）"
            f" · +{_fmt_num(t['additions'])} / -{_fmt_num(t['deletions'])}"
        )
        lines.append("")

        lines.append("### 提交明细")
        lines.append("")

        commits_list = work["commits"]
        inline_limit = 3 if len(commits_list) > 5 else len(commits_list)

        for c in commits_list[:inline_limit]:
            lines.extend(_render_one_commit_markdown(c))

        if len(commits_list) > inline_limit:
            rest = len(commits_list) - inline_limit
            lines.append("<details>")
            lines.append(f"<summary>展开后 {rest} 条提交（点击展开）</summary>")
            lines.append("")
            for c in commits_list[inline_limit:]:
                lines.extend(_render_one_commit_markdown(c))
            lines.append("</details>")
            lines.append("")

    # 飞书协作动态
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
                lines.append(
                    f"> 本日共收到 {total_msgs} 条消息，保留 {kept} 条，"
                    f"过滤 {skipped} 条（撤回 / @某人 / 太短 / 系统消息）。"
                )
                lines.append("")

    # 问题与思考
    if problems or ai_problems:
        lines.append("## 问题与思考")
        lines.append("")
        lines.extend(_render_dual_source_markdown(problems, ai_problems, reflection_source))

    # 明日计划
    if next_plan or ai_next_plan:
        lines.append("## 明日计划")
        lines.append("")
        lines.extend(_render_dual_source_markdown(next_plan, ai_next_plan, reflection_source))

    # 风险与待办
    if risks_grouped:
        lines.append("## 风险与待办")
        lines.append("")
        for cat, items in risks_grouped.items():
            lines.append(f"### {cat}")
            lines.append("")
            for text in items:
                lines.append(text)
                lines.append("")

    # 底部
    lines.append("---")
    lines.append("")
    lines.append("**数据来源**　GitHub · 飞书开放平台  ")
    lines.append("**文档版本**　v4.3 · 由智能日报生成器自动生成")

    return "\n".join(lines)


# ============ HTML 渲染 ============

def _render_html(
    *,
    model: dict,
    members: list,
    report_date: date,
    team_name: str,
    feishu_messages: list,
    summary: str,
    generated_at: datetime,
    feishu_stats: dict,
    yesterday_stats: dict,
    highlights: list,
    action_items: list,
    commit_summaries: dict,
    issue_number: int,
    risks: list,
    problems: list,
    next_plan: list,
    ai_problems: list,
    ai_next_plan: list,
    reflection_source: str,
) -> str:
    # 从 model 读预算好的数据（不再重算）
    metrics = model["metrics"]
    work = model["work"]
    task_changes = model["task_changes"]
    risks_grouped = model["risks_grouped"]

    today = metrics["today"]
    yest = metrics["yesterday"]

    def esc(s) -> str:
        return _html.escape(str(s or ""))

    weekday_cn = _WEEKDAY_CN[report_date.weekday()]

    kept = int(feishu_stats.get("kept", len(feishu_messages)) or 0)
    skipped = int(feishu_stats.get("skipped", 0) or 0)
    total_msgs = int(feishu_stats.get("total", kept + skipped) or 0)

    h2_style = ("color:#1a237e;border-bottom:2px solid #4a6cf7;padding-bottom:6px;"
                "margin-top:32px;margin-bottom:16px;font-size:20px;")
    h3_style = "color:#283593;margin-top:22px;margin-bottom:10px;font-size:16px;"
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

    p: list = []
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
    p.append("<h2 style='" + h2_style + "'>工作明细</h2>")
    if not work["commits"]:
        p.append("<p style='color:#999;'>本日无代码提交记录。</p>")
    else:
        p.append("<h3 style='" + h3_style + "'>分类概览</h3>")
        crows = []
        for cat in work["categories"]:
            if cat["count"] == 0:
                continue
            msgs = [
                (getattr(c, "message", "") or "").split("\n")[0].strip()
                for c in cat["commits"]
            ]
            details = "、".join(msgs[:2])
            if len(msgs) > 2:
                details += f" 等 {len(msgs)} 项"
            crows.append([esc(cat["label"]), f"{cat['percent']}%", esc(details)])
        p.append(table(["类型", "占比", "做了什么"], crows,
                       ["left", "center", "left"]))

        t = work["total"]
        p.append(
            f"<p style='background:#f0f3ff;border-radius:4px;padding:8px 12px;"
            f"margin:8px 0;font-size:13px;'>"
            f"<b>全天汇总</b>：{t['commits']} 次提交 · {t['files']} 个文件"
            f"（新增 {t['added']} · 修改 {t['modified']} · 删除 {t['removed']}）"
            f" · <span style='color:#2e7d32;'>+{_fmt_num(t['additions'])}</span> / "
            f"<span style='color:#c62828;'>-{_fmt_num(t['deletions'])}</span>"
            f"</p>"
        )

        p.append("<h3 style='" + h3_style + "'>提交明细</h3>")
        commits_list = work["commits"]
        inline_limit = 3 if len(commits_list) > 5 else len(commits_list)
        for c in commits_list[:inline_limit]:
            p.append(_render_one_commit_html(c, esc))
        if len(commits_list) > inline_limit:
            rest = len(commits_list) - inline_limit
            p.append("<details style='margin-top:12px;'>")
            p.append(
                f"<summary style='cursor:pointer;color:#4a6cf7;'>"
                f"展开后 {rest} 条提交（点击展开）</summary>"
            )
            for c in commits_list[inline_limit:]:
                p.append(_render_one_commit_html(c, esc))
            p.append("</details>")

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
        p.append(_render_dual_source_html(problems, ai_problems, reflection_source))
        p.append("</div>")

    # 明日计划
    if next_plan or ai_next_plan:
        p.append("<h2 style='" + h2_style + "'>明日计划</h2>")
        p.append("<div style='background:#e8f5e9;border-left:4px solid #2e7d32;"
                 "border-radius:6px;padding:12px 16px;margin:10px 0;'>")
        p.append(_render_dual_source_html(next_plan, ai_next_plan, reflection_source))
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
    p.append("<b>数据来源</b>　GitHub · 飞书开放平台<br>")
    p.append("<b>文档版本</b>　v4.3 · 由智能日报生成器自动生成")
    p.append("</div>")

    p.append("</body></html>")
    return "".join(p)


# ============ 对外入口 ============

def generate(
    members: list,
    report_date: date,
    team_name: str,
    *,
    feishu_messages: list | None = None,
    summary: str | None = None,
    generated_at: datetime | None = None,
    feishu_stats: dict | None = None,
    yesterday_stats: dict | None = None,
    highlights: list[str] | None = None,
    action_items: list[dict] | None = None,
    commit_summaries: dict[str, str] | None = None,
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

    # 归一化默认值
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

    # ⭐ v4.3：一次性构建 model（之前算两遍）
    model = _build_model(
        members=members,
        feishu_messages=feishu_messages,
        tasks=tasks,
        yesterday_stats=yesterday_stats,
        feishu_stats=feishu_stats,
        commit_summaries=commit_summaries,
        risks=risks,
    )

    common = dict(
        model=model,
        members=members,
        report_date=report_date,
        team_name=team_name,
        feishu_messages=feishu_messages,
        summary=summary,
        generated_at=generated_at,
        feishu_stats=feishu_stats,
        yesterday_stats=yesterday_stats,
        highlights=highlights,
        action_items=action_items,
        commit_summaries=commit_summaries,
        issue_number=issue_number,
        risks=risks,
        problems=problems,
        next_plan=next_plan,
        ai_problems=ai_problems,
        ai_next_plan=ai_next_plan,
        reflection_source=reflection_source,
    )

    md = _render_markdown(**common)
    html = _render_html(**common)
    return Report(
        date=report_date,
        team_name=team_name,
        markdown=md,
        html=html,
        generated_at=generated_at,
    )