"""数据模型定义（采集 / 生成层契约）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass
class FileChange:
    """单个文件的改动明细。"""
    filename: str
    status: str = "modified"
    additions: int = 0
    deletions: int = 0


@dataclass
class CommitRecord:
    author: str
    message: str
    timestamp: datetime
    repo: str
    additions: int
    deletions: int
    files_changed: int
    sha: str = ""
    files: list[str] = field(default_factory=list)
    url: str = ""
    file_details: list[FileChange] = field(default_factory=list)


@dataclass
class EventRecord:
    """GitHub 公开活动（Events API）。"""
    actor: str
    event_type: str
    repo: str
    summary: str
    timestamp: datetime
    url: str = ""


@dataclass
class MessageRecord:
    """飞书群消息。"""
    sender_id: str
    sender_name: str
    chat_id: str
    text: str
    msg_type: str
    timestamp: datetime


@dataclass
class TaskRecord:
    assignee: str
    title: str
    status_from: str
    status_to: str
    updated_at: datetime


@dataclass
class CollectResult(Generic[T]):
    """采集层统一返回封装（ADR-003）。

    success=True 且 records=[]  → 今日无记录
    success=False              → 数据获取失败
    """

    success: bool
    records: list[T] = field(default_factory=list)
    error_message: str | None = None
    meta: dict = field(default_factory=dict)     # ← 新增：附加统计信息


@dataclass
class MemberReport:
    name: str
    github_username: str
    commits: list[CommitRecord] = field(default_factory=list)
    tasks: list[TaskRecord] = field(default_factory=list)
    commits_failed: bool = False
    tasks_failed: bool = False


@dataclass
class DailyReport:
    date: date
    team_name: str
    members: list[MemberReport]
    generated_at: datetime
    markdown: str
    html: str