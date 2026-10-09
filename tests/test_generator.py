"""日报生成模块测试。"""

from __future__ import annotations

from datetime import date, datetime

from generator.formatter import generate
from shared.models import CommitRecord, MemberReport, TaskRecord


def test_generate_sections_and_empty() -> None:
    members = [
        MemberReport(
            name="张三",
            github_username="zhangsan",
            commits=[
                CommitRecord(
                    author="zhangsan",
                    message="feat: x",
                    timestamp=datetime(2026, 9, 26, 10),
                    repo="org/a",
                    additions=1,
                    deletions=0,
                    files_changed=1,
                )
            ],
            tasks=[],
        ),
        MemberReport(
            name="李四",
            github_username="lisi",
            commits=[],
            tasks=[],
            tasks_failed=True,
        ),
    ]
    report = generate(members, date(2026, 9, 26), "研发一组")
    assert "代码提交" in report.markdown
    assert "任务进展" in report.markdown
    assert "协作沟通" not in report.markdown
    assert "今日无记录" in report.markdown
    assert "数据获取失败" in report.markdown
    assert "<html" in report.html.lower()
    assert "张三" in report.html


def test_generate_task_lines() -> None:
    members = [
        MemberReport(
            name="张三",
            github_username="zhangsan",
            tasks=[
                TaskRecord(
                    assignee="zhangsan@c.com",
                    title="修 bug",
                    status_from="进行中",
                    status_to="已完成",
                    updated_at=datetime(2026, 9, 26, 12),
                )
            ],
        )
    ]
    report = generate(members, date(2026, 9, 26), "研发一组")
    assert "修 bug" in report.markdown
    assert "已完成" in report.html
