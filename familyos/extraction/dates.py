"""Dates and amounts as notices write them.

Used by the rule-based extractor and by the scorer. Numeric dates are read
day first (Indian and UK notices) unless that is impossible; a date with no
year takes the year that puts it closest after the notice was issued.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"])}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9

_MON = r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_DAY = r"(?P<day>[0-3]?\d)(?:st|nd|rd|th)?"
_YEAR = r"(?P<year>(?:19|20)\d{2}|'\d{2})"
_WEEKDAY = r"(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day,?\s+)?"

DATE_PATTERNS = [
    re.compile(r"\b(?P<y>(?:19|20)\d{2})-(?P<m>[01]\d)-(?P<d>[0-3]\d)\b"),
    re.compile(rf"\b{_WEEKDAY}{_DAY}\s*(?:of\s+)?{_MON},?\s*{_YEAR}?\b", re.I),
    re.compile(rf"\b{_WEEKDAY}{_MON}\s+{_DAY},?\s*{_YEAR}?\b", re.I),
    re.compile(r"\b(?P<a>[0-3]?\d)[/.-](?P<b>[01]?\d)[/.-](?P<c>(?:19|20)?\d{2})\b"),
    re.compile(r"\b(?P<a>[01]?\d)/(?P<b>[0-3]?\d)\b(?![/.-]\d)"),      # 5/29, month first, US style
]

RANGE_JOIN = re.compile(r"^\s*(?:to|till|until|through|-|–|—|and)\s*$", re.I)


@dataclass
class FoundDate:
    date: dt.date
    start: int
    end: int
    text: str
    has_year: bool


def _year_for(month: int, day: int, reference: dt.date | None) -> int | None:
    if reference is None:
        return None
    for year in (reference.year, reference.year + 1, reference.year - 1):
        try:
            candidate = dt.date(year, month, day)
        except ValueError:
            continue
        if -60 <= (candidate - reference).days <= 300:
            return year
    return reference.year


def find_dates(text: str, reference: dt.date | None = None, *, us_numeric: bool = False) -> list[FoundDate]:
    found: list[FoundDate] = []
    taken: list[tuple[int, int]] = []
    for index, pattern in enumerate(DATE_PATTERNS):
        for m in pattern.finditer(text):
            if any(s < m.end() and m.start() < e for s, e in taken):
                continue
            g = m.groupdict()
            has_year = True
            try:
                if index == 0:
                    y, mo, d = int(g["y"]), int(g["m"]), int(g["d"])
                elif index in (1, 2):
                    mo, d = MONTHS[g["mon"].lower().rstrip(".")[:4 if g["mon"].lower().startswith("sept") else 3]], int(g["day"])
                    if g.get("year"):
                        y = int(g["year"][1:]) + 2000 if g["year"].startswith("'") else int(g["year"])
                    else:
                        has_year = False
                        y = _year_for(mo, d, reference)
                        if y is None:
                            continue
                elif index == 3:
                    a, b, c = int(g["a"]), int(g["b"]), int(g["c"])
                    y = c + 2000 if c < 100 else c
                    d, mo = (b, a) if (us_numeric and a <= 12) or b > 12 else (a, b)
                else:
                    if not us_numeric:
                        continue
                    mo, d = int(g["a"]), int(g["b"])
                    has_year = False
                    y = _year_for(mo, d, reference)
                    if y is None:
                        continue
                value = dt.date(y, mo, d)
            except (ValueError, KeyError):
                continue
            found.append(FoundDate(value, m.start(), m.end(), m.group(0), has_year))
            taken.append((m.start(), m.end()))
    found.sort(key=lambda f: f.start)
    # "1st March to 8th March 2026": the first date borrows the second's year.
    for a, b in zip(found, found[1:], strict=False):
        if not a.has_year and b.has_year and RANGE_JOIN.match(text[a.end:b.start]):
            try:
                a.date = a.date.replace(year=b.date.year)
            except ValueError:
                pass
    return found


def date_ranges(found: list[FoundDate], text: str) -> list[tuple[FoundDate, FoundDate | None]]:
    """Pair dates joined by "to", "till", "-" into ranges."""
    out, i = [], 0
    while i < len(found):
        a = found[i]
        if i + 1 < len(found) and RANGE_JOIN.match(text[a.end:found[i + 1].start]) and found[i + 1].date >= a.date:
            out.append((a, found[i + 1]))
            i += 2
        else:
            out.append((a, None))
            i += 1
    return out


def iso_dates(value) -> list[dt.date]:
    """Every ISO date in a label value (a string, a list, or None)."""
    if value is None:
        return []
    if isinstance(value, list):
        return [d for v in value for d in iso_dates(v)]
    out = []
    for m in re.finditer(r"(?:19|20)\d{2}-[01]\d-[0-3]\d", str(value)):
        try:
            out.append(dt.date.fromisoformat(m.group(0)))
        except ValueError:
            pass
    return out


# ----------------------------------------------------------------------------
# amounts
# ----------------------------------------------------------------------------

_CURRENCY = {"rs": "INR", "inr": "INR", "₹": "INR", "$": "USD", "usd": "USD", "£": "GBP", "gbp": "GBP",
             "r.o": "OMR", "ro": "OMR", "omr": "OMR", "€": "EUR", "eur": "EUR"}
AMOUNT = re.compile(
    r"(?P<cur>rs\.?|inr|₹|\$|usd|£|gbp|r\.\s?o\.?|omr|€|eur)\s?(?P<num>\d[\d,]*(?:\.\d{1,2})?)(?:\s?/-)?"
    r"|(?P<num2>\d[\d,]*(?:\.\d{1,2})?)\s?(?P<cur2>rupees|inr|usd|gbp|omr|dollars|pounds)\b", re.I)


@dataclass
class FoundAmount:
    value: float
    currency: str | None
    start: int
    end: int
    text: str


def find_amounts(text: str) -> list[FoundAmount]:
    out = []
    for m in AMOUNT.finditer(text):
        num = m.group("num") or m.group("num2")
        cur = (m.group("cur") or m.group("cur2") or "").lower().replace(" ", "").rstrip(".")
        cur = {"rupees": "INR", "dollars": "USD", "pounds": "GBP"}.get(cur) or _CURRENCY.get(cur)
        try:
            value = float(num.replace(",", ""))
        except ValueError:
            continue
        out.append(FoundAmount(value, cur, m.start(), m.end(), m.group(0).strip()))
    return out


def amounts_in_label(value) -> list[float]:
    """Numbers in a label amount such as "Rs 1400" or "USD 300 (first payment of USD 665 total)"."""
    if value in (None, ""):
        return []
    return [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", str(value))]
