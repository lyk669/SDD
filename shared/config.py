"""配置读取与校验。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from shared.errors import ConfigError


REQUIRED_TOP_KEYS = ("team_name", "github", "lark", "email", "members", "storage")


@dataclass
class MemberConfig:
    name: str
    github: str
    lark: str
    lark_open_id: str | None = None
    lark_user_id: str | None = None

    def identity_keys(self) -> set[str]:
        """用于对齐采集结果的全部身份键（小写）。"""
        keys = {self.lark, self.github}
        if self.lark_open_id:
            keys.add(self.lark_open_id)
        if self.lark_user_id:
            keys.add(self.lark_user_id)
        return {k.strip().lower() for k in keys if k and str(k).strip()}


@dataclass
class AppConfig:
    team_name: str
    github: dict[str, Any]
    lark: dict[str, Any]
    email: dict[str, Any]
    storage: dict[str, Any]
    members: list[MemberConfig]
    schedule: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def github_token(self) -> str:
        return _env(self.github.get("token_env", "GITHUB_TOKEN"))

    def lark_credentials(self) -> tuple[str, str]:
        app_id = _env(self.lark.get("app_id_env", "LARK_APP_ID"))
        app_secret = _env(self.lark.get("app_secret_env", "LARK_APP_SECRET"))
        return app_id, app_secret

    def smtp_credentials(self) -> tuple[str, str]:
        user = _env(self.email.get("username_env", "SMTP_USERNAME"))
        password = _env(self.email.get("password_env", "SMTP_PASSWORD"))
        return user, password


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"环境变量 {name} 未设置或为空")
    return value


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"配置文件不存在: {config_path}")

    with config_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ConfigError("配置文件根节点必须是映射表")

    missing = [k for k in REQUIRED_TOP_KEYS if k not in data]
    if missing:
        raise ConfigError(f"配置缺少必填字段: {', '.join(missing)}")

    members_raw = data.get("members") or []
    if not members_raw:
        raise ConfigError("配置缺少必填字段: members（至少一名成员）")

    members: list[MemberConfig] = []
    for i, item in enumerate(members_raw):
        for key in ("name", "github", "lark"):
            if not item.get(key):
                raise ConfigError(f"members[{i}] 缺少必填字段: {key}")
        members.append(
            MemberConfig(
                name=item["name"],
                github=item["github"],
                lark=item["lark"],
                lark_open_id=item.get("lark_open_id") or item.get("open_id"),
                lark_user_id=item.get("lark_user_id") or item.get("user_id"),
            )
        )

    if not data["github"].get("repos"):
        raise ConfigError("github.repos 不能为空")
    if not data["lark"].get("tasklist_id") and not data["lark"].get("project_id"):
        raise ConfigError("lark.tasklist_id 不能为空（飞书任务清单 URL 中 task-lists/ 后的 GUID）")
    # 兼容旧字段名 project_id
    if not data["lark"].get("tasklist_id") and data["lark"].get("project_id"):
        data["lark"]["tasklist_id"] = data["lark"]["project_id"]
    if not data["storage"].get("db_path"):
        raise ConfigError("storage.db_path 不能为空")

    return AppConfig(
        team_name=data["team_name"],
        github=data["github"],
        lark=data["lark"],
        email=data["email"],
        storage=data["storage"],
        members=members,
        schedule=data.get("schedule") or {},
        raw=data,
    )
