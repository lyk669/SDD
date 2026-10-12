"""统一日志：终端人话格式 + 可选 JSON + 文件落盘。"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_CONFIGURED = False


class _ColorFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[41m",
    }
    RESET = "\033[0m"
    ICONS = {
        "DEBUG": "·",
        "INFO": "✓",
        "WARNING": "⚠",
        "ERROR": "✗",
        "CRITICAL": "‼",
    }

    def __init__(self, use_color: bool = True):
        super().__init__()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        level = record.levelname
        icon = self.ICONS.get(level, "·")
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)

        if self.use_color:
            color = self.COLORS.get(level, "")
            level_str = f"{color}{icon} {level:<7}{self.RESET}"
        else:
            level_str = f"{icon} {level:<7}"

        name = record.name.replace("daily_report.", "")
        return f"{ts} {level_str} {name:<22} {msg}"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        import json
        payload = {
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logger(level: str = "INFO", log_dir: str | Path = "logs",
                 *, to_file: bool = True) -> None:
    """初始化日志。

    环境变量：
      LOG_LEVEL  默认 INFO
      LOG_JSON   设为 1 终端也输出 JSON（默认人话格式）
      LOG_FILE   设为 0 关闭文件日志
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    level_name = os.environ.get("LOG_LEVEL", level).upper()
    log_level = getattr(logging, level_name, logging.INFO)

    use_json = os.environ.get("LOG_JSON", "").strip() in {"1", "true", "yes"}
    use_color = sys.stderr.isatty() and not use_json

    root = logging.getLogger("daily_report")
    root.setLevel(log_level)
    root.handlers.clear()
    root.propagate = False

    # 终端
    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(log_level)
    stream.setFormatter(_JsonFormatter() if use_json else _ColorFormatter(use_color))
    root.addHandler(stream)

    # 文件
    if to_file and os.environ.get("LOG_FILE", "1") not in {"0", "false", "no"}:
        try:
            log_path = Path(log_dir)
            log_path.mkdir(parents=True, exist_ok=True)
            fh = TimedRotatingFileHandler(
                log_path / "daily_report.log",
                when="midnight", backupCount=14, encoding="utf-8",
            )
            fh.setLevel(log_level)
            fh.setFormatter(_JsonFormatter())
            root.addHandler(fh)
        except Exception as exc:  # noqa: BLE001
            root.warning(f"文件日志初始化失败: {exc}")

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    if not name.startswith("daily_report."):
        name = f"daily_report.{name}" if name != "daily_report" else name
    return logging.getLogger(name)