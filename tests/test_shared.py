"""shared 层单元测试。"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml

from shared.config import load_config
from shared.errors import CollectorError, ConfigError, GeneratorError, NotifierError
from shared.storage import ReportStorage


def _write_config(tmp_path: Path, **overrides) -> Path:
    data = {
        "team_name": "研发一组",
        "github": {"token_env": "GITHUB_TOKEN", "repos": ["org/a"]},
        "lark": {
            "app_id_env": "LARK_APP_ID",
            "app_secret_env": "LARK_APP_SECRET",
            "project_id": "p1",  # 兼容旧字段；优先读取 tasklist_id
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
        "members": [{"name": "张三", "github": "zhangsan", "lark": "zhangsan@c.com"}],
    }
    data.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_load_config_ok(tmp_path: Path) -> None:
    cfg = load_config(_write_config(tmp_path))
    assert cfg.team_name == "研发一组"
    assert len(cfg.members) == 1


def test_load_config_missing_field(tmp_path: Path) -> None:
    path = _write_config(tmp_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    del raw["members"]
    path.write_text(yaml.dump(raw), encoding="utf-8")
    with pytest.raises(ConfigError, match="members"):
        load_config(path)


def test_errors_exist() -> None:
    assert issubclass(CollectorError, Exception)
    assert issubclass(GeneratorError, Exception)
    assert issubclass(NotifierError, Exception)


def test_logger_json_lines(capsys: pytest.CaptureFixture[str]) -> None:
    from shared.logger import JsonLineFormatter
    import logging
    import sys

    lg = logging.getLogger("json_line_test")
    lg.handlers.clear()
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonLineFormatter())
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    lg.info("hello")
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["msg"] == "hello"
    assert payload["level"] == "INFO"


def test_storage_roundtrip(tmp_path: Path) -> None:
    storage = ReportStorage(tmp_path / "r.db")
    rid = storage.save(
        date(2026, 9, 26),
        "研发一组",
        "# md",
        "<html/>",
        datetime(2026, 9, 26, 18, 0),
    )
    assert rid > 0
    got = storage.get(date(2026, 9, 26), "研发一组")
    assert got is not None
    assert got.markdown == "# md"
    assert len(storage.list_recent()) == 1
