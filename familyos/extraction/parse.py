"""Turn an original into page text that every claim can point back into.

Each page's text is built from its words, and every word keeps its
character span and its box on the page, so a quoted piece of text can be
mapped back to exactly where it sits in the original (see ground.py).

- PDF: the text layer, via PyMuPDF. A page with almost no text (a scan)
  is rendered and read with OCR instead.
- Images (photos of notices, WhatsApp forwards): OCR with Tesseract.
- Email: the subject and the plain-text body, one page, no boxes.
- Plain text: one page, no boxes.

Coordinates are PDF points for PDFs (origin top left) and pixels for images.
"""
from __future__ import annotations

import html
import io
import re
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser

from familyos.settings import settings

# Unicode ranges, which is as far as letters alone can honestly take you.
SCRIPT_RANGES = (
    ("devanagari", 0x0900, 0x097F),   # Hindi, Marathi, Nepali, Sanskrit
    ("bengali", 0x0980, 0x09FF),
    ("gurmukhi", 0x0A00, 0x0A7F),
    ("gujarati", 0x0A80, 0x0AFF),
    ("oriya", 0x0B00, 0x0B7F),
    ("tamil", 0x0B80, 0x0BFF),
    ("telugu", 0x0C00, 0x0C7F),
    ("kannada", 0x0C80, 0x0CFF),
    ("malayalam", 0x0D00, 0x0D7F),
    ("arabic", 0x0600, 0x06FF),       # Urdu
    ("latin", 0x0041, 0x024F),
)
# Below this share of the letters a script is incidental: a Latin word inside a
# Hindi notice does not make it bilingual.
SCRIPT_MIN_SHARE = 0.08


def detect_scripts(text: str) -> list[str]:
    """Which writing systems the text uses, most used first.

    Script, not language: Hindi, Marathi and Nepali all write in Devanagari,
    and claiming to have detected a language from letters alone would be a
    guess dressed up as a fact."""
    counts: dict[str, int] = {}
    total = 0
    for ch in text:
        if not ch.isalpha():
            continue
        for name, low, high in SCRIPT_RANGES:
            if low <= ord(ch) <= high:
                counts[name] = counts.get(name, 0) + 1
                total += 1
                break
    if not total:
        return []
    return [name for name, n in sorted(counts.items(), key=lambda kv: -kv[1])
            if n / total >= SCRIPT_MIN_SHARE]


PARSER_VERSION = "parse-v1"

# A PDF page with fewer words than this in its text layer is treated as a scan.
MIN_TEXT_WORDS = 8
OCR_DPI = 200


@dataclass
class Word:
    start: int
    end: int
    box: tuple[float, float, float, float] | None


@dataclass
class Page:
    number: int                 # 1-based
    text: str
    words: list[Word] = field(default_factory=list)
    width: float | None = None
    height: float | None = None
    source: str = "text"        # text | ocr


@dataclass
class ParsedDocument:
    media_type: str
    pages: list[Page]
    notes: list[str] = field(default_factory=list)
    parser_version: str = PARSER_VERSION

    @property
    def ocr_pages(self) -> int:
        return sum(1 for p in self.pages if p.source == "ocr")

    @property
    def scripts(self) -> list[str]:
        """Which writing systems the text is in, most used first.

        Script, not language: Hindi, Marathi and Nepali all write in
        Devanagari, and claiming to have detected a language from letters
        alone would be a guess dressed up as a fact."""
        return detect_scripts(" ".join(p.text for p in self.pages))

    @property
    def char_count(self) -> int:
        return sum(len(p.text) for p in self.pages)


class _Builder:
    """Joins words into page text, recording each word's span."""

    def __init__(self):
        self.parts: list[str] = []
        self.words: list[Word] = []
        self.length = 0
        self._line = None
        self._block = None

    def add(self, text: str, box, block, line) -> None:
        if not text:
            return
        if self.words:
            sep = " " if (block, line) == (self._block, self._line) else ("\n" if block == self._block else "\n\n")
            self.parts.append(sep)
            self.length += len(sep)
        self._block, self._line = block, line
        self.words.append(Word(self.length, self.length + len(text), box))
        self.parts.append(text)
        self.length += len(text)

    @property
    def text(self) -> str:
        return "".join(self.parts)


def parse(data: bytes, media_type: str) -> ParsedDocument:
    if media_type == "application/pdf":
        return _pdf(data)
    if media_type.startswith("image/"):
        return _image(data, media_type)
    if media_type == "message/rfc822":
        return _email(data)
    if media_type.startswith("text/"):
        text = data.decode("utf-8", errors="replace")
        return ParsedDocument(media_type, [_plain_page(1, _strip_html(text) if media_type == "text/html" else text)])
    return ParsedDocument(media_type, [], notes=[f"unsupported media type {media_type}"])


# ----------------------------------------------------------------------------

def _pdf(data: bytes) -> ParsedDocument:
    import pymupdf

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - a damaged file yields no text, not a failed job
        return ParsedDocument("application/pdf", [], notes=[f"unreadable: {type(exc).__name__}"])
    pages, notes = [], []
    for index, pdf_page in enumerate(doc):
        b = _Builder()
        for x0, y0, x1, y1, text, block, line, _ in pdf_page.get_text("words", sort=True):
            b.add(text, (x0, y0, x1, y1), block, line)
        page = Page(index + 1, b.text, b.words, pdf_page.rect.width, pdf_page.rect.height)
        if len(b.words) < MIN_TEXT_WORDS and pdf_page.get_images():
            pix = pdf_page.get_pixmap(dpi=OCR_DPI)
            scale = 72 / OCR_DPI
            ocr = _ocr(pix.tobytes("png"), scale, notes)
            if ocr is not None and len(ocr.words) > len(b.words):
                page = Page(index + 1, ocr.text, ocr.words, pdf_page.rect.width, pdf_page.rect.height, "ocr")
        pages.append(page)
    return ParsedDocument("application/pdf", pages, notes)


def _image(data: bytes, media_type: str) -> ParsedDocument:
    from PIL import Image

    notes: list[str] = []
    with Image.open(io.BytesIO(data)) as im:
        width, height = im.size
    b = _ocr(data, 1.0, notes)
    page = Page(1, b.text if b else "", b.words if b else [], width, height, "ocr")
    return ParsedDocument(media_type, [page], notes)


def _ocr(image_bytes: bytes, scale: float, notes: list[str]) -> _Builder | None:
    """Tesseract words with boxes, scaled by `scale`. None if OCR is unavailable."""
    try:
        import pytesseract
        from PIL import Image
        from pytesseract import Output

        with Image.open(io.BytesIO(image_bytes)) as im:
            d = pytesseract.image_to_data(im.convert("RGB"), lang=settings.ocr_languages,
                                          output_type=Output.DICT)
    except Exception as exc:  # noqa: BLE001 - no OCR means no text, not a failed job
        if "ocr_unavailable" not in notes:
            notes.append("ocr_unavailable")
        # A missing language pack fails here rather than silently reading a
        # Hindi notice as nonsense English, which is the worse outcome.
        notes.append(f"ocr_error: {type(exc).__name__}")
        return None
    b = _Builder()
    for i, text in enumerate(d["text"]):
        text = (text or "").strip()
        if not text or float(d["conf"][i]) < 0:
            continue
        x, y, w, h = (d[k][i] * scale for k in ("left", "top", "width", "height"))
        b.add(text, (x, y, x + w, y + h), (d["block_num"][i], d["par_num"][i]), d["line_num"][i])
    return b


def _email(raw: bytes) -> ParsedDocument:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    subject = str(msg.get("Subject", "") or "")
    body = msg.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        try:
            text = body.get_content()
        except (LookupError, UnicodeDecodeError):
            text = (body.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
        if body.get_content_type() == "text/html":
            text = _strip_html(text)
    content = (f"Subject: {subject}\n\n" if subject else "") + text
    return ParsedDocument("message/rfc822", [_plain_page(1, content)])


def _plain_page(number: int, text: str) -> Page:
    text = text.replace("\r\n", "\n")
    words = [Word(m.start(), m.end(), None) for m in re.finditer(r"\S+", text)]
    return Page(number, text, words)


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", text)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))
