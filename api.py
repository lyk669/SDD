"""智能日报管道的 HTTP 门面。

后端本体是 CLI 管道（main.py）：采集当日工作痕迹 → 按成员生成日报 → 可选邮件推送。
本模块把存档查询、配置维护、手动触发管道暴露给前端工作台，并托管 web/ 静态资源。

启动：
  .\\.venv\\Scripts\\python.exe -m uvicorn api:app --reload --port 8000
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from shared.config import load_config
from shared.errors import ConfigError
from shared.storage import ReportStorage

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
CONFIG_PATH = ROOT / "config.yaml"

load_dotenv(ROOT / ".env")

app = FastAPI(title="智能日报管理 API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _db_path() -> Path:
    if CONFIG_PATH.exists():
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        raw = (data.get("storage") or {}).get("db_path") or "data/daily_reports.db"
        path = Path(raw)
        return path if path.is_absolute() else ROOT / path
    return ROOT / "data" / "daily_reports.db"


def _storage() -> ReportStorage:
    return ReportStorage(_db_path())


def _read_raw_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise HTTPException(status_code=404, detail="config.yaml 不存在")
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="配置文件格式错误")
    return data


def _write_raw_config(data: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _config_token() -> str:
    return os.environ.get("CONFIG_TOKEN", "local-demo-token").strip()


def _require_config_token(token: str | None) -> None:
    expected = _config_token()
    if not token or token.strip() != expected:
        raise HTTPException(status_code=401, detail="配置令牌无效")


def _summarize_markdown(markdown: str) -> tuple[int, int, int, str]:
    """从日报 Markdown 估算成员数 / 提交数 / 任务数与状态。"""
    members = re.findall(r"(?m)^##\s+(.+)$", markdown or "")
    member_count = len(members)

    commits = 0
    tasks = 0
    degraded = False
    section = None
    for line in (markdown or "").splitlines():
        if line.startswith("### 代码提交"):
            section = "commits"
            continue
        if line.startswith("### 任务进展"):
            section = "tasks"
            continue
        if line.startswith("## ") or line.startswith("# "):
            section = None
            continue
        if not line.startswith("- "):
            continue
        body = line[2:].strip()
        if "数据获取失败" in body:
            degraded = True
            continue
        if body == "今日无记录":
            continue
        if section == "commits":
            commits += 1
        elif section == "tasks":
            tasks += 1

    status = "degraded" if degraded else "ok"
    return member_count, commits, tasks, status


def _parse_members_from_markdown(markdown: str) -> list[dict[str, Any]]:
    """把 Markdown 还原为详情页可用的成员结构（尽力解析）。"""
    members: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    section: str | None = None

    for line in (markdown or "").splitlines():
        if line.startswith("## "):
            if current:
                members.append(current)
            name = line[3:].strip()
            current = {
                "name": name,
                "github_username": "",
                "commits": [],
                "tasks": [],
                "commits_failed": False,
                "tasks_failed": False,
            }
            section = None
            continue
        if current is None:
            continue
        if line.startswith("### 代码提交"):
            section = "commits"
            continue
        if line.startswith("### 任务进展"):
            section = "tasks"
            continue
        if line.startswith("#") or line.startswith("*生成时间"):
            section = None
            continue
        if not line.startswith("- "):
            continue
        body = line[2:].strip()
        if body == "数据获取失败":
            if section == "commits":
                current["commits_failed"] = True
            elif section == "tasks":
                current["tasks_failed"] = True
            continue
        if body.startswith("数据获取失败"):
            if section == "commits":
                current["commits_failed"] = True
            elif section == "tasks":
                current["tasks_failed"] = True
            continue
        if body == "今日无记录":
            continue
        if section == "commits":
            # `repo` message (+a/-d, n files)
            m = re.match(
                r"`([^`]+)`\s+(.*?)\s+\(\+(\d+)/-(\d+),\s*(\d+)\s*files?\)\s*$",
                body,
            )
            if m:
                current["commits"].append(
                    {
                        "repo": m.group(1),
                        "message": m.group(2),
                        "additions": int(m.group(3)),
                        "deletions": int(m.group(4)),
                        "files_changed": int(m.group(5)),
                        "timestamp": "",
                    }
                )
            else:
                current["commits"].append(
                    {
                        "repo": "—",
                        "message": body,
                        "additions": 0,
                        "deletions": 0,
                        "files_changed": 0,
                        "timestamp": "",
                    }
                )
        elif section == "tasks":
            if "：" in body:
                title, rest = body.split("：", 1)
            else:
                title, rest = body, "— → —"
            parts = [p.strip() for p in rest.split("→")]
            current["tasks"].append(
                {
                    "title": title.strip(),
                    "status_from": parts[0] if parts else "—",
                    "status_to": parts[1] if len(parts) > 1 else "—",
                    "updated_at": "",
                }
            )

    if current:
        members.append(current)
    return members


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------


class ReportSummary(BaseModel):
    id: int
    report_date: str
    team_name: str
    generated_at: str
    member_count: int = 0
    commits_count: int = 0
    tasks_count: int = 0
    status: str = "ok"


class ReportDetail(ReportSummary):
    markdown: str
    html: str
    members: list[dict[str, Any]] = Field(default_factory=list)
    sources: dict[str, Any] = Field(default_factory=dict)


class MemberIn(BaseModel):
    id: str | None = None
    name: str
    github: str
    lark: str
    lark_open_id: str | None = None
    lark_user_id: str | None = None


class GenerateBody(BaseModel):
    dry_run: bool = True
    force: bool = True
    lookback_days: int = Field(default=1, ge=1, le=30)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, Any]:
    """健康检查：配置 + GitHub 仓库可达性。"""
    import httpx

    db = _db_path()
    sources = [
        {"id": "github", "ok": False, "detail": "未检测"},
        {"id": "lark_task", "ok": False, "detail": "未检测"},
        {"id": "lark_msg", "ok": False, "detail": "本期不做群消息采集"},
    ]
    cfg_ok = False
    try:
        cfg = load_config(CONFIG_PATH)
        cfg_ok = True
        repos = cfg.github.get("repos") or []
        token = ""
        try:
            token = cfg.github_token()
        except ConfigError as exc:
            token = ""
            sources[0]["detail"] = str(exc)
        if repos:
            headers = {"Accept": "application/vnd.github+json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                with httpx.Client(timeout=12.0, headers=headers) as client:
                    resp = client.get(f"https://api.github.com/repos/{repos[0]}")
                if resp.status_code == 200:
                    sources[0] = {
                        "id": "github",
                        "ok": True,
                        "detail": f"仓库可达 {repos[0]}",
                    }
                else:
                    sources[0] = {
                        "id": "github",
                        "ok": False,
                        "detail": f"{repos[0]} HTTP {resp.status_code}",
                    }
            except Exception as exc:  # noqa: BLE001
                sources[0] = {"id": "github", "ok": False, "detail": str(exc)}
        else:
            sources[0] = {"id": "github", "ok": False, "detail": "未配置仓库"}

        try:
            cfg.lark_credentials()
            sources[1] = {
                "id": "lark_task",
                "ok": True,
                "detail": "飞书凭证已配置",
            }
        except ConfigError as exc:
            sources[1] = {"id": "lark_task", "ok": False, "detail": str(exc)}
    except ConfigError as exc:
        sources[0]["detail"] = str(exc)
        sources[1]["detail"] = str(exc)

    return {
        "ok": db.parent.exists() and CONFIG_PATH.exists() and sources[0]["ok"],
        "db": str(db),
        "config_ok": cfg_ok,
        "sources": sources,
    }


@app.get("/api/reports", response_model=list[ReportSummary])
def list_reports(
    limit: int = Query(50, ge=1, le=200),
    date_from: str | None = None,
    date_to: str | None = None,
    team: str | None = None,
) -> list[ReportSummary]:
    """日报列表（支持日期区间与团队筛选）。"""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=400, detail="开始日期晚于结束日期")

    items = _storage().list_recent(limit=limit)
    out: list[ReportSummary] = []
    for r in items:
        d = r.report_date.isoformat()
        if date_from and d < date_from:
            continue
        if date_to and d > date_to:
            continue
        if team and team not in ("", "全部") and r.team_name != team:
            continue
        mc, cc, tc, status = _summarize_markdown(r.markdown)
        out.append(
            ReportSummary(
                id=r.id,
                report_date=d,
                team_name=r.team_name,
                generated_at=r.generated_at.isoformat(),
                member_count=mc,
                commits_count=cc,
                tasks_count=tc,
                status=status,
            )
        )
    return out


@app.get("/api/reports/{report_id}", response_model=ReportDetail)
def get_report(report_id: int) -> ReportDetail:
    """日报详情。"""
    report = _storage().get_by_id(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="日报不存在")
    mc, cc, tc, status = _summarize_markdown(report.markdown)
    members = _parse_members_from_markdown(report.markdown)
    degraded = status == "degraded"
    return ReportDetail(
        id=report.id,
        report_date=report.report_date.isoformat(),
        team_name=report.team_name,
        generated_at=report.generated_at.isoformat(),
        member_count=mc,
        commits_count=cc,
        tasks_count=tc,
        status=status,
        markdown=report.markdown,
        html=report.html,
        members=members,
        sources={
            "github": {
                "success": not degraded,
                "count": cc,
                "error": "部分提交采集失败" if degraded else "",
            },
            "lark": {
                "success": not degraded,
                "count": tc,
                "error": "部分任务采集失败" if degraded else "",
            },
        },
    )


@app.get("/api/members")
def list_members() -> dict[str, Any]:
    """成员映射列表（来自 config.yaml）。"""
    data = _read_raw_config()
    members = []
    for i, item in enumerate(data.get("members") or []):
        members.append(
            {
                "id": f"m-{i + 1}",
                "name": item.get("name") or "",
                "github": item.get("github") or "",
                "lark": item.get("lark") or "",
                "lark_open_id": item.get("lark_open_id") or item.get("open_id") or "",
                "lark_user_id": item.get("lark_user_id") or item.get("user_id") or "",
            }
        )
    return {"can_view_all": True, "members": members, "team_name": data.get("team_name") or ""}


@app.put("/api/members")
def save_members(
    members: list[MemberIn],
    x_config_token: str | None = Header(default=None, alias="X-Config-Token"),
) -> dict[str, Any]:
    """整体保存成员列表到 config.yaml。"""
    _require_config_token(x_config_token)
    data = _read_raw_config()
    data["members"] = [
        {
            "name": m.name.strip(),
            "github": m.github.strip(),
            "lark": m.lark.strip(),
            **({"lark_open_id": m.lark_open_id.strip()} if m.lark_open_id else {}),
            **({"lark_user_id": m.lark_user_id.strip()} if m.lark_user_id else {}),
        }
        for m in members
        if m.name.strip() and m.github.strip() and m.lark.strip()
    ]
    if not data["members"]:
        raise HTTPException(status_code=400, detail="至少保留一名成员")
    _write_raw_config(data)
    return {"ok": True, "message": "成员已保存，下次执行时生效", "count": len(data["members"])}


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    """读取 config.yaml（不含密钥明文）。"""
    data = _read_raw_config()
    return data


@app.put("/api/config")
def put_config(
    body: dict[str, Any],
    x_config_token: str | None = Header(default=None, alias="X-Config-Token"),
) -> dict[str, Any]:
    """保存配置到 config.yaml。"""
    _require_config_token(x_config_token)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="配置必须是对象")
    # 保留 storage.db_path 不被前端篡改路径穿越
    current = _read_raw_config()
    if "storage" in body and isinstance(body["storage"], dict):
        body["storage"]["db_path"] = (current.get("storage") or {}).get(
            "db_path", "data/daily_reports.db"
        )
    _write_raw_config(body)
    return {"ok": True, "message": "配置已保存，下次执行时自动生效"}


@app.post("/api/generate")
def generate_report(body: GenerateBody | None = None) -> dict[str, Any]:
    """触发日报管道（默认 dry-run + force）。"""
    body = body or GenerateBody()
    try:
        cfg = load_config(CONFIG_PATH)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    from main import run_pipeline

    code = run_pipeline(cfg, dry_run=body.dry_run, force=body.force)
    if code != 0:
        raise HTTPException(status_code=500, detail="日报生成失败，请查看服务端日志")
    return {
        "ok": True,
        "message": "日报已生成" + ("（dry-run，未发信）" if body.dry_run else ""),
        "dry_run": body.dry_run,
    }


@app.post("/api/reports/{report_id}/send")
def send_report(report_id: int) -> dict[str, Any]:
    """按已存日报重新发送邮件。"""
    report = _storage().get_by_id(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="日报不存在")
    try:
        cfg = load_config(CONFIG_PATH)
        user, password = cfg.smtp_credentials()
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    from notifier import email as email_notifier
    from shared.models import DailyReport

    daily = DailyReport(
        date=report.report_date,
        team_name=report.team_name,
        members=[],
        markdown=report.markdown,
        html=report.html,
        generated_at=report.generated_at,
    )
    ok = email_notifier.send(
        daily,
        cfg.email.get("recipients") or [],
        smtp_host=cfg.email["smtp_host"],
        smtp_port=int(cfg.email.get("smtp_port") or 465),
        use_ssl=bool(cfg.email.get("use_ssl", True)),
        username=user,
        password=password,
        from_addr=cfg.email.get("from_addr") or user,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="邮件发送失败")
    return {"ok": True, "message": "邮件已发送"}


# ---------------------------------------------------------------------------
# 静态前端
# ---------------------------------------------------------------------------


@app.get("/")
def index_page() -> FileResponse:
    index = WEB_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="前端未部署")
    return FileResponse(index)


if WEB_DIR.exists():
    app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")
