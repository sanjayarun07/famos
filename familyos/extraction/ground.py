"""Find where a quoted piece of text sits in the parsed original.

Every claim carries the words it was read from. Grounding looks for them in
the page text: exactly, then ignoring case, spacing and typographic quotes
and dashes, then as the closest run of words. A claim whose quote cannot be
found is kept for review but marked ungrounded, and never becomes a
proposed obligation on its own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from familyos.extraction.parse import Page, ParsedDocument

FUZZY_MIN_RATIO = 0.85

_TRANSLATE = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                            " ": " ", "•": " "})


@dataclass
class Location:
    page: int
    start: int
    end: int
    boxes: list[list[float]]      # one [x0, y0, x1, y1] per line of the span
    match: str                    # exact | normalized | fuzzy

    def as_dict(self) -> dict:
        return {"page": self.page, "start": self.start, "end": self.end, "boxes": self.boxes, "match": self.match}


def _normalize(text: str) -> tuple[str, list[int]]:
    """Lower-cased text with whitespace runs collapsed to one space, and the
    index in the original of each character kept."""
    out, index, space = [], [], False
    for i, ch in enumerate(text.translate(_TRANSLATE)):
        if ch.isspace():
            if not space and out:
                out.append(" ")
                index.append(i)
            space = True
            continue
        space = False
        out.append(ch.lower())
        index.append(i)
    return "".join(out), index


def locate(doc: ParsedDocument, quote: str, page_hint: int | None = None) -> Location | None:
    quote = (quote or "").strip()
    if len(quote) < 3:
        return None
    order = sorted(doc.pages, key=lambda p: (p.number != page_hint, p.number))
    for page in order:
        at = page.text.find(quote)
        if at >= 0:
            return _location(page, at, at + len(quote), "exact")
    nquote, _ = _normalize(quote)
    for page in order:
        ntext, index = _normalize(page.text)
        at = ntext.find(nquote)
        if at >= 0:
            return _location(page, index[at], index[at + len(nquote) - 1] + 1, "normalized")
    best = None
    for page in order:
        found = _fuzzy(page, nquote)
        if found and (best is None or found[0] > best[0]):
            best = (found[0], page, found[1], found[2])
    if best:
        return _location(best[1], best[2], best[3], "fuzzy")
    return None


def _fuzzy(page: Page, nquote: str) -> tuple[float, int, int] | None:
    """The run of page words most like the quote, if close enough."""
    n = len(nquote.split())
    words = page.words
    if not words or n == 0:
        return None
    qtokens = set(nquote.split())
    hits = [_normalize(page.text[w.start:w.end])[0] in qtokens for w in words]
    best = None
    for size in {max(1, n - 1), n, n + 1}:
        for i in range(0, max(1, len(words) - size + 1)):
            if sum(hits[i:i + size]) < n / 2:
                continue
            chunk = words[i:i + size]
            candidate = _normalize(page.text[chunk[0].start:chunk[-1].end])[0]
            if abs(len(candidate) - len(nquote)) > max(8, len(nquote) // 3):
                continue
            ratio = SequenceMatcher(None, candidate, nquote, autojunk=False).ratio()
            if ratio >= FUZZY_MIN_RATIO and (best is None or ratio > best[0]):
                best = (ratio, chunk[0].start, chunk[-1].end)
    return best


def _location(page: Page, start: int, end: int, match: str) -> Location:
    return Location(page.number, start, end, _boxes(page, start, end), match)


def _boxes(page: Page, start: int, end: int) -> list[list[float]]:
    """The union box of the span's words on each line."""
    lines: list[list[float]] = []
    for w in page.words:
        if w.box is None or w.end <= start or w.start >= end:
            continue
        x0, y0, x1, y1 = w.box
        if lines and abs(lines[-1][1] - y0) < (y1 - y0) * 0.5:
            last = lines[-1]
            last[0], last[1], last[2], last[3] = min(last[0], x0), min(last[1], y0), max(last[2], x1), max(last[3], y1)
        else:
            lines.append([x0, y0, x1, y1])
    return [[round(v, 1) for v in box] for box in lines]


def snippet(doc: ParsedDocument, loc: Location, pad: int = 0) -> str:
    page = next(p for p in doc.pages if p.number == loc.page)
    return re.sub(r"\s+", " ", page.text[max(0, loc.start - pad):loc.end + pad]).strip()
