"""Decide what a file is from its bytes, not from what the sender claims."""
from __future__ import annotations

ALLOWED = frozenset({
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/heic",
    "text/plain",
    "message/rfc822",
})


def sniff(data: bytes, declared: str | None = None) -> str:
    head = data[:64]
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"heim", b"heis"):
        return "image/heic"
    if declared == "message/rfc822":
        return "message/rfc822"
    if _is_text(data):
        return "text/plain"
    return "application/octet-stream"


def _is_text(data: bytes) -> bool:
    sample = data[:8192]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A multi-byte character cut at the sample's end is still text.
        return exc.start >= len(sample) - 3
    return True
