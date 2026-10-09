"""日报 Markdown / HTML 模板。"""

from __future__ import annotations

from jinja2 import Template

MARKDOWN_TEMPLATE = Template(
    """# {{ team_name }} 日报 — {{ date }}

{% for member in members %}
## {{ member.name }}

### 代码提交
{% if member.commits_failed and member.commits %}
- 数据获取失败（以下为已成功采集的部分）
{% for c in member.commits %}
- `{{ c.repo }}` {{ c.message }} (+{{ c.additions }}/-{{ c.deletions }}, {{ c.files_changed }} files)
{% endfor %}
{% elif member.commits_failed %}
- 数据获取失败
{% elif not member.commits %}
- 今日无记录
{% else %}
{% for c in member.commits %}
- `{{ c.repo }}` {{ c.message }} (+{{ c.additions }}/-{{ c.deletions }}, {{ c.files_changed }} files)
{% endfor %}
{% endif %}

### 任务进展
{% if member.tasks_failed and member.tasks %}
- 数据获取失败（以下为已成功采集的部分）
{% for t in member.tasks %}
- {{ t.title }}：{{ t.status_from or '—' }} → {{ t.status_to }}
{% endfor %}
{% elif member.tasks_failed %}
- 数据获取失败
{% elif not member.tasks %}
- 今日无记录
{% else %}
{% for t in member.tasks %}
- {{ t.title }}：{{ t.status_from or '—' }} → {{ t.status_to }}
{% endfor %}
{% endif %}

{% endfor %}
*生成时间：{{ generated_at }}*
"""
)

HTML_TEMPLATE = Template(
    """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>{{ team_name }} 日报 — {{ date }}</title>
  <style>
    body { font-family: "Segoe UI", "PingFang SC", sans-serif; max-width: 800px; margin: 2rem auto; color: #1a1a1a; line-height: 1.6; }
    h1 { border-bottom: 2px solid #2563eb; padding-bottom: .4rem; }
    h2 { margin-top: 2rem; color: #1e40af; }
    h3 { margin-top: 1.2rem; color: #334155; }
    ul { padding-left: 1.2rem; }
    li { margin: .25rem 0; }
    .meta { color: #64748b; font-size: .9rem; margin-top: 2rem; }
    code { background: #f1f5f9; padding: .1rem .35rem; border-radius: 3px; }
  </style>
</head>
<body>
  <h1>{{ team_name }} 日报 — {{ date }}</h1>
  {% for member in members %}
  <h2>{{ member.name }}</h2>
  <h3>代码提交</h3>
  <ul>
  {% if member.commits_failed and member.commits %}
    <li>数据获取失败（以下为已成功采集的部分）</li>
  {% for c in member.commits %}
    <li><code>{{ c.repo }}</code> {{ c.message }} (+{{ c.additions }}/-{{ c.deletions }}, {{ c.files_changed }} files)</li>
  {% endfor %}
  {% elif member.commits_failed %}
    <li>数据获取失败</li>
  {% elif not member.commits %}
    <li>今日无记录</li>
  {% else %}
  {% for c in member.commits %}
    <li><code>{{ c.repo }}</code> {{ c.message }} (+{{ c.additions }}/-{{ c.deletions }}, {{ c.files_changed }} files)</li>
  {% endfor %}
  {% endif %}
  </ul>
  <h3>任务进展</h3>
  <ul>
  {% if member.tasks_failed and member.tasks %}
    <li>数据获取失败（以下为已成功采集的部分）</li>
  {% for t in member.tasks %}
    <li>{{ t.title }}：{{ t.status_from or '—' }} → {{ t.status_to }}</li>
  {% endfor %}
  {% elif member.tasks_failed %}
    <li>数据获取失败</li>
  {% elif not member.tasks %}
    <li>今日无记录</li>
  {% else %}
  {% for t in member.tasks %}
    <li>{{ t.title }}：{{ t.status_from or '—' }} → {{ t.status_to }}</li>
  {% endfor %}
  {% endif %}
  </ul>
  {% endfor %}
  <p class="meta">生成时间：{{ generated_at }}</p>
</body>
</html>
"""
)


def render_markdown(context: dict) -> str:
    return MARKDOWN_TEMPLATE.render(**context).strip() + "\n"


def render_html(context: dict) -> str:
    return HTML_TEMPLATE.render(**context).strip() + "\n"
