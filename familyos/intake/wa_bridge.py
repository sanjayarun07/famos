"""A development bridge for unofficial WhatsApp clients.

Meta's Cloud API cannot read a class group, so during development there is no
way to get realistic group traffic through the pipeline. Unofficial clients
(OpenWA, wa-automate, whatsapp-web.js, Baileys) can, by linking as a companion
device and relaying what they see.

**This is for development only and is off by default.** Those clients are
against WhatsApp's terms, the ban lands on the linked number rather than on the
server, and a companion session can read every chat on the account -- not only
the ones a family wants read. None of that is acceptable in a product that
seals a notice with the household's own key and keeps filenames out of the
audit log. Link a throwaway number to a group you made for testing, not a
parent's real account and a real class group: the other families in it did not
agree to any of this.

The contract below is ours, not any client's. Map the client's webhook onto it
in a few lines rather than teaching FamilyOS a third-party payload shape that
changes without notice.

Who the message belongs to
--------------------------
A group message is not *from* a member -- it is from whichever parent typed it,
and most of them are strangers to this household. Treating the author as the
sender would refuse nearly everything.

So the shape mirrors email: the member whose account is linked is the one who
*received* it, and so the one it is stored for. The original author, the group
name and the client's own id go into `source`, the way an email's From and
Message-ID do. A group message is marked forwarded, because second-hand is
exactly what it is.
"""
from __future__ import annotations

import base64
import binascii
import logging

from familyos.intake.whatsapp import Media, Message, normalise_phone

logger = logging.getLogger(__name__)


class BadBridgePayload(ValueError):
    """The relay sent something this cannot read."""


def parse(payload: dict) -> tuple[Message, dict]:
    """One relayed message, as (Message, extra source fields).

    Expected shape, which the relay script produces:

        {
          "linked_phone": "919876543210",      # whose account is linked
          "id":           "false_...@g.us_3EB0",
          "timestamp":    1790000000,
          "is_group":     true,
          "chat_name":    "Class II Parents",
          "author":       "919999999999",      # who actually typed it
          "author_name":  "Priya",
          "text":         "Sports day moved to the 14th",
          "media": {"mimetype": "application/pdf", "filename": "circular.pdf",
                    "data_base64": "..."}
        }
    """
    linked = normalise_phone(payload.get("linked_phone"))
    if not linked:
        raise BadBridgePayload("linked_phone is required: it says whose household this belongs to")
    message_id = str(payload.get("id") or "").strip()
    if not message_id:
        raise BadBridgePayload("id is required, so a replayed relay stores nothing new")

    is_group = bool(payload.get("is_group"))
    author = normalise_phone(payload.get("author"))
    text = (payload.get("text") or "").strip() or None

    media: list[Media] = []
    raw = payload.get("media") or None
    if raw:
        encoded = raw.get("data_base64") or ""
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise BadBridgePayload("media.data_base64 is not valid base64") from exc
        if data:
            # The bytes are already here, so download() is never called: the
            # adapter's media_id is a label rather than something to fetch.
            media.append(Media(media_id="bridge:" + message_id,
                               media_type=raw.get("mimetype") or "application/octet-stream",
                               filename=raw.get("filename"),
                               caption=(raw.get("caption") or "").strip() or None))
            text = text or media[0].caption

    message = Message(
        message_id="bridge:" + message_id,
        sender=linked,
        sender_name=payload.get("linked_name"),
        sent_at=str(payload.get("timestamp") or "") or None,
        text=text,
        # Anything out of a group is somebody else's words, which is what the
        # hearsay penalty on claims is for.
        forwarded=is_group or bool(payload.get("forwarded")),
        media=media,
    )
    source = {
        "relay": "bridge",
        "chat_name": payload.get("chat_name"),
        "is_group": is_group,
        "author": author,
        "author_name": payload.get("author_name"),
    }
    return message, source


def media_fetcher(payload: dict):
    """The bytes came in the payload, so hand them back instead of calling
    Meta. Returns None when there is nothing to fetch."""
    raw = payload.get("media") or None
    if not raw or not raw.get("data_base64"):
        return None
    data = base64.b64decode(raw["data_base64"], validate=True)
    media_type = raw.get("mimetype") or "application/octet-stream"

    async def fetch(_media_id: str) -> tuple[bytes, str]:
        return data, media_type

    return fetch
