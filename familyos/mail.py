"""Sending mail, in one place.

Reminders tell a member about a date. Actions reply to the school that wrote.
Both go out over the same SMTP settings, so the connection, the From header and
the attachment handling live here rather than twice.

Nothing in here decides *who* to write to. A reminder's recipient is a member
of the household; an action's recipient is derived from the notice being
replied to and never supplied by a caller. Both are settled before this module
is called, because a sender that accepts an arbitrary address is an
exfiltration primitive wearing a useful hat.
"""
from __future__ import annotations

import asyncio
import logging

from familyos.settings import settings

logger = logging.getLogger(__name__)


class NotConfigured(RuntimeError):
    """No SMTP host, so nothing can be sent. Said plainly rather than failing
    at the socket."""


def configured() -> bool:
    return bool(settings.smtp_host)


async def send(to: str, subject: str, body: str, *, ics: tuple[str, bytes] | None = None) -> None:
    """One message, optionally carrying a calendar entry.

    An .ics goes as `text/calendar; method=REQUEST`, which is what makes a mail
    client offer to add it rather than showing an attachment nobody opens.
    """
    if not configured():
        raise NotConfigured("FAMILYOS_SMTP_HOST is not set, so nothing can be sent")
    if not to:
        raise ValueError("no recipient")

    def deliver() -> None:
        import smtplib
        from email.message import EmailMessage

        msg = EmailMessage()
        msg["From"] = settings.smtp_from or settings.smtp_username
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        if ics is not None:
            filename, payload = ics
            msg.add_attachment(payload, maintype="text", subtype="calendar",
                               filename=filename, params={"method": "REQUEST", "charset": "UTF-8"})
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
            if settings.smtp_starttls:
                server.starttls()
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(msg)

    await asyncio.to_thread(deliver)
