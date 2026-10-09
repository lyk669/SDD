"""邮件推送测试。"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock, patch

from notifier.email import send
from shared.models import DailyReport, MemberReport


def _report() -> DailyReport:
    return DailyReport(
        date=date(2026, 9, 26),
        team_name="研发一组",
        members=[MemberReport(name="张三", github_username="z")],
        generated_at=datetime(2026, 9, 26, 18),
        markdown="# md",
        html="<p>html</p>",
    )


def test_email_send_success() -> None:
    mock_server = MagicMock()
    mock_server.__enter__ = MagicMock(return_value=mock_server)
    mock_server.__exit__ = MagicMock(return_value=False)
    with patch("notifier.email.smtplib.SMTP_SSL", return_value=mock_server):
        ok = send(
            _report(),
            ["leader@c.com"],
            smtp_host="smtp.c.com",
            username="u",
            password="p",
            from_addr="bot@c.com",
        )
    assert ok is True
    mock_server.login.assert_called_once()
    mock_server.sendmail.assert_called_once()


def test_email_send_retry_then_fail() -> None:
    with patch("notifier.email.smtplib.SMTP_SSL", side_effect=OSError("down")), patch(
        "notifier.email.time.sleep"
    ):
        ok = send(
            _report(),
            ["leader@c.com"],
            smtp_host="smtp.c.com",
            username="u",
            password="p",
            from_addr="bot@c.com",
            max_retries=2,
        )
    assert ok is False
