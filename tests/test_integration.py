"""端到端集成测试（mock 外部依赖）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import yaml

from main import aggregate_members, run_pipeline
from shared.config import AppConfig, MemberConfig, load_config
from shared.models import CollectResult, CommitRecord, TaskRecord


def _config(tmp_path: Path) -> Path:
    data = {
        "team_name": "研发一组",
        "schedule": {"skip_weekends": False, "holidays": []},
        "github": {"token_env": "GITHUB_TOKEN", "repos": ["org/a"]},
        "lark": {
            "app_id_env": "LARK_APP_ID",
            "app_secret_env": "LARK_APP_SECRET",
            "tasklist_id": "p1",
        },
        "email": {
            "smtp_host": "smtp.test",
            "smtp_port": 465,
            "username_env": "SMTP_USERNAME",
            "password_env": "SMTP_PASSWORD",
            "from_addr": "a@t.com",
            "recipients": ["l@t.com"],
        },
        "storage": {"db_path": str(tmp_path / "t.db")},
        "members": [
            {"name": "张三", "github": "zhangsan", "lark": "zhangsan@c.com"},
            {"name": "李四", "github": "lisi", "lark": "lisi@c.com"},
        ],
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_normal_and_empty_member(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("LARK_APP_ID", "id")
    monkeypatch.setenv("LARK_APP_SECRET", "sec")
    monkeypatch.setenv("SMTP_USERNAME", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")

    commits = CollectResult(
        success=True,
        records=[
            CommitRecord(
                author="zhangsan",
                message="feat",
                timestamp=datetime(2026, 9, 26, 10),
                repo="org/a",
                additions=1,
                deletions=0,
                files_changed=1,
            )
        ],
    )
    tasks = CollectResult(success=True, records=[])

    cfg = load_config(_config(tmp_path))
    with (
        patch("main.run_collect", return_value=(commits, tasks)),
        patch("main.email_notifier.send", return_value=True) as email_send,
        patch("main.is_workday", return_value=True),
    ):
        code = run_pipeline(cfg, dry_run=False, force=True)

    assert code == 0
    email_send.assert_called_once()
    report = email_send.call_args.args[0]
    assert "今日无记录" in report.markdown
    assert "feat" in report.markdown
    assert "协作沟通" not in report.markdown


def test_degraded_source(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("LARK_APP_ID", "id")
    monkeypatch.setenv("LARK_APP_SECRET", "sec")
    monkeypatch.setenv("SMTP_USERNAME", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")

    commits = CollectResult(success=False, records=[], error_message="timeout")
    tasks = CollectResult(
        success=True,
        records=[
            TaskRecord(
                assignee="zhangsan@c.com",
                title="任务A",
                status_from="待办",
                status_to="进行中",
                updated_at=datetime(2026, 9, 26, 11),
            )
        ],
    )
    cfg = load_config(_config(tmp_path))

    with (
        patch("main.run_collect", return_value=(commits, tasks)),
        patch("main.email_notifier.send", return_value=True) as email_send,
        patch("main.is_workday", return_value=True),
    ):
        code = run_pipeline(cfg, force=True)

    assert code == 0
    report = email_send.call_args.args[0]
    assert "数据获取失败" in report.markdown
    assert "任务A" in report.markdown


def test_all_sources_failed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("LARK_APP_ID", "id")
    monkeypatch.setenv("LARK_APP_SECRET", "sec")
    monkeypatch.setenv("SMTP_USERNAME", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")

    empty_fail = CollectResult(success=False, records=[], error_message="down")
    cfg = load_config(_config(tmp_path))
    with (
        patch("main.run_collect", return_value=(empty_fail, empty_fail)),
        patch("main.email_notifier.send") as email_send,
        patch("main.is_workday", return_value=True),
    ):
        code = run_pipeline(cfg, force=True)

    assert code == 1
    email_send.assert_not_called()


def test_aggregate_identity_mapping() -> None:
    cfg = AppConfig(
        team_name="t",
        github={},
        lark={},
        email={},
        storage={},
        members=[MemberConfig(name="张三", github="ZhangSan", lark="zhangsan@c.com")],
    )
    commits = CollectResult(
        success=True,
        records=[
            CommitRecord(
                author="zhangsan",
                message="x",
                timestamp=datetime.now(),
                repo="r",
                additions=1,
                deletions=0,
                files_changed=1,
            )
        ],
    )
    tasks = CollectResult(success=True, records=[])
    members = aggregate_members(cfg, commits, tasks)
    assert len(members[0].commits) == 1


def test_aggregate_open_id_mapping() -> None:
    cfg = AppConfig(
        team_name="t",
        github={},
        lark={},
        email={},
        storage={},
        members=[
            MemberConfig(
                name="张三",
                github="zhangsan",
                lark="zhangsan@c.com",
                lark_open_id="ou_abc123",
            )
        ],
    )
    commits = CollectResult(success=True, records=[])
    tasks = CollectResult(
        success=True,
        records=[
            TaskRecord(
                assignee="ou_abc123",
                title="任务",
                status_from="待办",
                status_to="完成",
                updated_at=datetime.now(),
            )
        ],
    )
    members = aggregate_members(cfg, commits, tasks)
    assert len(members[0].tasks) == 1
