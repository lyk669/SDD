"""日报生成：将成员数据整理为 DailyReport。"""

from __future__ import annotations

from datetime import date, datetime

from generator import template
from shared.errors import GeneratorError
from shared.models import DailyReport, MemberReport


def generate(
    members: list[MemberReport],
    date: date,
    team_name: str,
    *,
    generated_at: datetime | None = None,
) -> DailyReport:
    """按「代码提交 → 任务进展」组织日报内容。"""
    if not team_name:
        raise GeneratorError("team_name 不能为空")

    generated_at = generated_at or datetime.now()
    context = {
        "team_name": team_name,
        "date": date.isoformat(),
        "members": members,
        "generated_at": generated_at.strftime("%Y-%m-%d %H:%M:%S"),
    }

    try:
        markdown = template.render_markdown(context)
        html = template.render_html(context)
    except Exception as exc:  # noqa: BLE001
        raise GeneratorError(f"渲染日报失败: {exc}") from exc

    return DailyReport(
        date=date,
        team_name=team_name,
        members=members,
        generated_at=generated_at,
        markdown=markdown,
        html=html,
    )
