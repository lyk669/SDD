"""SMTP 邮件推送。"""

from __future__ import annotations

import os
import smtplib
import time
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from shared.logger import get_logger
from shared.models import DailyReport

logger = get_logger("daily_report.notifier.email")


def send(
    report: DailyReport,
    recipients: list[str],
    *,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
    use_ssl: bool | None = None,
    username: str | None = None,
    password: str | None = None,
    from_addr: str | None = None,
    sender_name: str | None = None,
    max_retries: int = 2,
) -> bool:
    """发送 HTML 格式日报邮件。

    契约核心参数为 (report, recipients)；SMTP 细节可通过关键字传入，
    缺省时回退环境变量：SMTP_HOST / SMTP_PORT / SMTP_USERNAME / SMTP_PASSWORD / SMTP_FROM。

    sender_name 用于在邮件客户端显示"刘雨可 <2327929235@qq.com>"这种友好格式。
    """
    if not recipients:
        logger.error("邮件收件人为空")
        return False

    smtp_host = smtp_host or os.environ.get("SMTP_HOST") or ""
    smtp_port = int(smtp_port or os.environ.get("SMTP_PORT") or 465)
    use_ssl = True if use_ssl is None else use_ssl
    if os.environ.get("SMTP_USE_SSL", "").lower() in {"0", "false", "no"}:
        use_ssl = False if use_ssl is True and smtp_port == 587 else use_ssl
    username = username or os.environ.get("SMTP_USERNAME") or ""
    password = password or os.environ.get("SMTP_PASSWORD") or ""
    from_addr = from_addr or os.environ.get("SMTP_FROM") or username

    if not smtp_host or not username or not password or not from_addr:
        logger.error("SMTP 配置不完整（host/username/password/from）")
        return False

    subject = f"{report.team_name} 日报 — {report.date.isoformat()}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject

    # 显示名优先 sender_name（如"刘雨可"），没有就只显示地址
    if sender_name:
        msg["From"] = formataddr(
            (str(Header(sender_name, "utf-8")), from_addr)
        )
    else:
        msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)

    msg.attach(MIMEText(report.markdown, "plain", "utf-8"))
    msg.attach(MIMEText(report.html, "html", "utf-8"))

    attempts = max_retries + 1
    for i in range(1, attempts + 1):
        try:
            _deliver(
                msg,
                recipients,
                smtp_host=smtp_host,
                smtp_port=smtp_port,
                use_ssl=use_ssl,
                username=username,
                password=password,
                envelope_from=from_addr,   # 纯邮箱地址，用于 SMTP 信封
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"邮件发送失败 ({i}/{attempts}): {exc}")
            if i < attempts:
                time.sleep(2)
    return False


def _deliver(
    msg: MIMEMultipart,
    recipients: list[str],
    *,
    smtp_host: str,
    smtp_port: int,
    use_ssl: bool,
    username: str,
    password: str,
    envelope_from: str,
) -> None:
    if use_ssl:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as server:
            server.login(username, password)
            server.sendmail(envelope_from, recipients, msg.as_string())
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(username, password)
            server.sendmail(envelope_from, recipients, msg.as_string())