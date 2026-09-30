"""WhatsApp Cloud API adapter: what a parent forwards, turned into what the
gateway needs.

Meta's Cloud API cannot read a parent's groups, and no API ever will -- reading
someone's personal message stream is not on offer at any price. What it does
give is messages sent *to* a business number, which is enough: forwarding is the
one habit parents already have, it takes four taps for a morning's worth of
class-group traffic, and it works the same on both phones.

Identity is the part worth noticing. `intake/email.py` needs authserv-id
checking, DKIM alignment and a quarantine lane because a From header is
forgeable. Here Meta has already verified the number before the webhook fires,
so the sender is simply known. What remains is whether that number belongs to
anyone: a message from a number we cannot place is refused, because unlike
email there is no household address on it to say where it should have gone.

Nothing in this module trusts the request until `verify_signature` has passed.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# What a forwarded school message can reasonably be.
MEDIA_KINDS = ("image", "document", "audio", "video", "sticker")


class BadSignature(Exception):
    """The body did not come from Meta."""


@dataclass
class Media:
    media_id: str
    media_type: str
    filename: str | None = None
    caption: str | None = None


@dataclass
class Message:
    message_id: str
    sender: str                 # digits only, as Meta reports it
    sender_name: str | None
    sent_at: str | None
    text: str | None = None
    forwarded: bool = False
    media: list[Media] = field(default_factory=list)


def normalise_phone(number: str | None) -> str | None:
    """Digits only. Meta reports 919876543210; a guardian may have typed
    +91 98765 43210 or 098765 43210, and they are the same person."""
    if not number:
        return None
    digits = re.sub(r"\D", "", number)
    return digits or None


def phone_variants(number: str) -> list[str]:
    """The same number as it might have been stored. A guardian typing a local
    number omits the country code; Meta never does."""
    digits = normalise_phone(number) or ""
    out = [digits]
    # India is the first market; a 12-digit 91XXXXXXXXXX is also 10 digits local.
    if len(digits) > 10:
        out.append(digits[-10:])
    return list(dict.fromkeys(out))


def verify_signature(raw: bytes, header: str | None, app_secret: str) -> None:
    """Meta signs every delivery with the app secret (X-Hub-Signature-256).
    Without a configured secret nothing is accepted: an unsigned webhook is an
    open door to anyone who learns the URL."""
    if not app_secret:
        raise BadSignature("FAMILYOS_WHATSAPP_APP_SECRET is not set, so no delivery can be trusted")
    if not header or not header.startswith("sha256="):
        raise BadSignature("missing X-Hub-Signature-256")
    expected = hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, header[len("sha256="):].strip()):
        raise BadSignature("signature does not match")


def parse(payload: dict) -> list[Message]:
    """Every message in a delivery. Meta batches, and sends status callbacks
    (delivered, read) through the same webhook: those carry no `messages` and
    are ignored."""
    messages: list[Message] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            names = {p.get("wa_id"): (p.get("profile") or {}).get("name")
                     for p in value.get("contacts") or []}
            for raw in value.get("messages") or []:
                message = _one(raw, names)
                if message is not None:
                    messages.append(message)
    return messages


def _one(raw: dict, names: dict) -> Message | None:
    message_id = raw.get("id")
    sender = normalise_phone(raw.get("from"))
    if not message_id or not sender:
        return None
    kind = raw.get("type")
    message = Message(
        message_id=message_id,
        sender=sender,
        sender_name=names.get(raw.get("from")),
        sent_at=raw.get("timestamp"),
        # Meta marks a forward, which is what a school notice almost always is.
        forwarded=bool((raw.get("context") or {}).get("forwarded")),
    )
    if kind == "text":
        message.text = ((raw.get("text") or {}).get("body") or "").strip() or None
    elif kind in MEDIA_KINDS:
        part = raw.get(kind) or {}
        media_id = part.get("id")
        if not media_id:
            return None
        message.media.append(Media(
            media_id=media_id,
            media_type=part.get("mime_type") or "application/octet-stream",
            filename=part.get("filename"),
            caption=(part.get("caption") or "").strip() or None,
        ))
        message.text = message.media[0].caption
    else:
        # Reactions, location, contacts, button replies: nothing a notice needs.
        logger.debug("ignoring whatsapp message of type %s", kind)
        return None
    return message


# ----------------------------------------------------------------------------
# fetching the bytes behind a media id
# ----------------------------------------------------------------------------

async def download(media_id: str, *, access_token: str, api_base: str, fetch=None) -> tuple[bytes, str]:
    """Media arrives as an id; the bytes are a second call. `fetch` is injected
    by the tests so none of this needs the network."""
    if fetch is not None:
        return await fetch(media_id)
    import httpx

    headers = {"Authorization": "Bearer " + access_token}
    async with httpx.AsyncClient(timeout=30) as client:
        described = await client.get(f"{api_base}/{media_id}", headers=headers)
        described.raise_for_status()
        described = described.json()
        url = described.get("url")
        if not url:
            raise RuntimeError(f"whatsapp media {media_id} has no url")
        got = await client.get(url, headers=headers)
        got.raise_for_status()
        return got.content, described.get("mime_type") or "application/octet-stream"
