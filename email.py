"""邮件通知模块。

发件人（SMTP 登录账号）与收件人（日报发给谁）完全解耦：
  - username / password / from_addr 来自配置里的"发件人"
  - recipients 由调用方传入，可以是任意别人的邮箱，可多个

用法:
    from notifier import email as email_notifier
    ok = email_notifier.send(
        report,
        ["2327929235@qq.com"],
        smtp_host="smtp.qq.com",
        smtp_port=465,
        use_ssl=True,
        username="your@qq.com",
        password="授权码",
        from_addr="your@qq.com",
    )
"""

from __future__ import annotations

import smtplib
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any

from shared.logger import get_logger

logger = get_logger("daily_report.notifier.email")


def _get(obj: Any, key: str, default: str = "") -> str:
    """兼容 dataclass / dict / 属性三种取值方式。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _build_message(
    report: Any,
    recipients: list[str],
    from_addr: str,
) -> MIMEMultipart:
    """组装 multipart/alternative 邮件（纯文本 + HTML 双版本）。"""
    team_name = _get(report, "team_name", "团队")
    report_date = _get(report, "date", "")
    markdown = _get(report, "markdown", "")
    html = _get(report, "html", "")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(f"{team_name} 日报 {report_date}", "utf-8")
    # 发件人显示名用团队名，实际地址用 from_addr
    msg["From"] = formataddr((str(Header(team_name, "utf-8")), from_addr))
    msg["To"] = ", ".join(recipients)

    # 纯文本兜底（有些客户端不渲染 HTML）
    if markdown:
        msg.attach(MIMEText(markdown, "plain", "utf-8"))
    # HTML 正文
    if html:
        msg.attach(MIMEText(html, "html", "utf-8"))
    return msg


def send(
    report: Any,
    recipients: list[str],
    *,
    smtp_host: str,
    smtp_port: int = 465,
    use_ssl: bool = True,
    username: str,
    password: str,
    from_addr: str | None = None,
) -> bool:
    """发送日报邮件。

    Args:
        report: 日报对象，需含 team_name / date / markdown / html
        recipients: 收件人邮箱列表（可以是别人，可多个）
        smtp_host: SMTP 服务器，如 smtp.qq.com
        smtp_port: 端口，QQ 用 465
        use_ssl: True 走 SMTP_SSL(465)；False 走 STARTTLS(587)
        username: 发件人 SMTP 登录账号
        password: 发件人 SMTP 授权码（不是登录密码）
        from_addr: 邮件头 From，缺省用 username

    Returns:
        成功 True，失败 False（异常已捕获并记录日志）
    """
    recipients = [r.strip() for r in (recipients or []) if r and r.strip()]
    if not recipients:
        logger.error("收件人为空，取消发送")
        return False

    from_addr = (from_addr or username or "").strip()
    if not from_addr:
        logger.error("发件人地址为空，取消发送")
        return False

    try:
        msg = _build_message(report, recipients, from_addr)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"组装邮件失败: {exc}")
        return False

    server: smtplib.SMTP | None = None
    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(smtp_host, int(smtp_port), timeout=30)
        else:
            server = smtplib.SMTP(smtp_host, int(smtp_port), timeout=30)
            server.ehlo()
            server.starttls()
            server.ehlo()

        server.login(username, password)
        refused = server.sendmail(from_addr, recipients, msg.as_string())

        if refused:
            # 部分收件人被拒，仍算失败，但日志里列出
            logger.warning(f"部分收件人被拒: {refused}")
            return False

        logger.info(f"邮件已投递: {from_addr} -> {recipients}")
        return True

    except smtplib.SMTPAuthenticationError as exc:
        logger.error(
            f"SMTP 认证失败（QQ 邮箱请确认用的是授权码而非登录密码）: {exc}"
        )
        return False
    except smtplib.SMTPException as exc:
        logger.error(f"SMTP 发送失败: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001
        logger.error(f"发送邮件异常: {exc}")
        return False
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass