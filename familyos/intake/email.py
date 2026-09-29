"""Forwarding-email adapter: parse a raw message into what the gateway needs.

Sender authentication is read from the Authentication-Results header that
the receiving mail provider adds (RFC 8601). The webhook that delivers the
message is authenticated with a shared secret, so this header is the
provider's, not the sender's; any copy the sender forged sits below it and
is ignored because only the topmost header is read.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr

from familyos.settings import settings

# An inline image this large is a photo someone pasted, not a signature logo.
INLINE_IMAGE_MIN_BYTES = 20 * 1024


@dataclass
class Attachment:
    filename: str | None
    media_type: str
    data: bytes


@dataclass
class ParsedEmail:
    sender: str | None
    recipients: list[str]
    subject: str | None
    message_id: str | None
    date: str | None
    sender_authenticated: bool
    attachments: list[Attachment] = field(default_factory=list)


def parse(raw: bytes) -> ParsedEmail:
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw)
    sender = parseaddr(str(msg.get("From", "")))[1].lower() or None
    recipients = [addr.lower() for _, addr in getaddresses(
        [str(v) for h in ("Delivered-To", "X-Original-To", "To", "Cc") for v in msg.get_all(h, [])]) if addr]
    message_id = str(msg.get("Message-ID", "")).strip().strip("<>") or None
    return ParsedEmail(
        sender=sender,
        recipients=list(dict.fromkeys(recipients)),
        subject=str(msg.get("Subject", "")) or None,
        message_id=message_id,
        date=str(msg.get("Date", "")) or None,
        sender_authenticated=sender_authenticated(msg, sender),
        attachments=list(_attachments(msg)),
    )


def inbound_token(address: str, domain: str) -> str | None:
    """The household token in <token>[+tag]@<domain>, or None."""
    local, _, host = (address or "").lower().rpartition("@")
    if host != domain.lower() or not local:
        return None
    return local.split("+", 1)[0]


def authserv_id(header: str) -> str:
    """The authserv-id naming who made the verdict: the first token of the
    header, before the version and the first ';' (RFC 8601 s.2.2)."""
    first = header.split(";", 1)[0].strip().split()
    return first[0].lower() if first else ""


def sender_authenticated(msg: EmailMessage, sender: str | None) -> bool:
    """True when *our* provider's verdict says the From domain is genuine:
    DMARC passed, or DKIM passed for the From domain exactly.

    Only a header whose authserv-id matches `settings.inbound_authserv_id` is
    read (RFC 8601 s.5): anyone can add an Authentication-Results header, so
    the id is what separates the receiving provider's verdict from one the
    sender wrote themselves. Without that setting nothing is authenticated
    and mail waits in quarantine for a guardian, which is the safe failure.

    A parent domain's signature does not authenticate a subdomain here; that
    is relaxed DMARC alignment, and the dmarc=pass branch already covers it.
    """
    if not sender or "@" not in sender:
        return False
    trusted = (settings.inbound_authserv_id or "").strip().lower()
    if not trusted:
        return False
    domain = sender.rsplit("@", 1)[1]
    for raw in msg.get_all("Authentication-Results") or []:
        header = str(raw)
        if authserv_id(header) != trusted:
            continue
        # The provider prepends its own verdict, so the first header bearing
        # our authserv-id is genuine; a forged copy sits below it, unread.
        return _passes(header.lower(), domain)
    return False


def _passes(verdict: str, domain: str) -> bool:
    if re.search(r"\bdmarc=pass\b", verdict):
        return True
    return any(match.group(1).rstrip(".") == domain
               for match in re.finditer(r"\bdkim=pass\b[^;]*?header\.d=([a-z0-9.-]+)", verdict))


def _attachments(msg: EmailMessage):
    # walk() descends into attached messages too, so a notice forwarded "as
    # attachment" still yields its PDF.
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        content_type = part.get_content_type()
        if disposition == "attachment":
            pass
        elif content_type.startswith("image/") and (disposition == "inline" or part.get_filename()):
            payload = part.get_payload(decode=True) or b""
            if len(payload) < INLINE_IMAGE_MIN_BYTES:
                continue
        else:
            continue
        data = part.get_payload(decode=True) or b""
        yield Attachment(filename=part.get_filename(), media_type=content_type, data=data)
