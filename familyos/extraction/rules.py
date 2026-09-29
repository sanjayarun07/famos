"""A rule-based extractor: no model, no network.

It exists as the offline baseline the model-based extractor is scored
against, and as the fallback when no model is configured. It reads each
line that carries a date or an amount and guesses the claim's kind from
cue words. It does not understand scope, amendments or context.
"""
from __future__ import annotations

import datetime as dt
import re

from familyos.extraction.claims import Amount, Claim
from familyos.extraction.dates import date_ranges, find_amounts, find_dates
from familyos.extraction.parse import ParsedDocument

NAME = "rules"
PROMPT_VERSION = "rules-v1"

DEADLINE_CUE = re.compile(r"\b(by|before|no later than|not later than|last date|latest by|due|deadline|on or before|"
                          r"submit|return(?:ed)?|send back|closes?)\b", re.I)
PAYMENT_CUE = re.compile(r"\b(fees?|pay(?:ment|able)?|dues|contribut\w*|charges?|cost|deposit|amount)\b", re.I)
FORM_CUE = re.compile(r"\b(consent|permission|slip|form|reply)\b", re.I)
SIGN_CUE = re.compile(r"\b(signature|signed|sign)\b|_{6,}", re.I)
AMEND_CUE = re.compile(r"\b(postponed|rescheduled|revised|cancelled|canceled|extended|preponed|changed|instead of)\b", re.I)
PARENT_CUE = re.compile(r"\b(parent[- ]teacher|ptm|parents? (?:are|is) (?:requested|invited|required) to (?:attend|meet|come))\b", re.I)
OPTIONAL_CUE = re.compile(r"\b(optional|if you wish|if interested|voluntary)\b", re.I)
ISSUE_DATE = re.compile(r"^\s*(?:date[d]?\s*[:\-]?)?\s*\S*\s*$|^\s*dated?\b", re.I)
US_HINT = re.compile(r"\$\s?\d|\b(?:school district|middle school|elementary|grade \d|PTA|[A-Z]{2} \d{5})\b")


class RulesExtractor:
    name = NAME
    model = None
    prompt_version = PROMPT_VERSION

    async def run(self, doc: ParsedDocument, reference_date: dt.date | None) -> list[Claim]:
        text_all = "\n".join(p.text for p in doc.pages)
        us = bool(US_HINT.search(text_all))
        claims: list[Claim] = []
        signed_form = bool(SIGN_CUE.search(text_all)) and bool(FORM_CUE.search(text_all))
        for page in doc.pages:
            lines = [ln for ln in page.text.split("\n")]
            for i, line in enumerate(lines):
                if not line.strip():
                    continue
                context = line
                # A short line ("Date: 28th March 2026") takes its heading from the line before.
                if len(line.strip()) < 45 and i > 0 and lines[i - 1].strip():
                    context = lines[i - 1].strip() + " " + line.strip()
                previous = lines[i - 1] if i > 0 else ""
                claims.extend(self._line(line, context, previous, page.number, reference_date, us))
        if signed_form:
            heading = next((ln.strip() for p in doc.pages for ln in p.text.split("\n")
                            if FORM_CUE.search(ln) and 3 < len(ln.strip()) < 90), None)
            if heading and not any(c.kind == "deadline" and "form_return" in c.requires for c in claims):
                claims.append(Claim(kind="form", title=_title(heading), quote=heading.strip(), confidence=0.4,
                                    requires=["parent_signature", "form_return"], optional=bool(OPTIONAL_CUE.search(text_all))))
        return _dedupe(claims)

    def _line(self, line: str, context: str, previous: str, page: int, reference: dt.date | None,
              us: bool) -> list[Claim]:
        out: list[Claim] = []
        dates = find_dates(line, reference, us_numeric=us)
        amounts = find_amounts(line)
        if not dates and not amounts:
            return out
        requires: list = []
        if FORM_CUE.search(context):
            requires.append("form_return")
        if PAYMENT_CUE.search(context) or amounts:
            requires.append("payment")
        if PARENT_CUE.search(context):
            requires.append("parent_attendance")
        optional = bool(OPTIONAL_CUE.search(context))
        amount = None
        if amounts and (PAYMENT_CUE.search(context) or dates):
            a = amounts[0]
            amount = Amount(value=a.value, currency=a.currency, text=a.text)
        for first, last in date_ranges(dates, line):
            before = (previous[-40:] + " " + line[:first.start])[-60:]
            if ISSUE_DATE.match(line) and first.date == reference:
                continue        # the notice's own date line
            if AMEND_CUE.search(context):
                kind = "amendment"
            elif DEADLINE_CUE.search(before):
                kind = "deadline"
            else:
                kind = "event"
            reqs = [r for r in requires if kind != "event" or r == "parent_attendance"]
            out.append(Claim(
                kind=kind, title=_title(context, exclude=[first.text] + ([last.text] if last else [])),
                date=first.date, end_date=last.date if last else None, quote=line.strip(), page=page,
                requires=reqs, optional=optional, confidence=0.35,
                amount=amount if kind == "deadline" and "payment" in reqs else None,
                change=_title(context) if kind == "amendment" else None))
        if amounts and not dates and PAYMENT_CUE.search(context):
            a = amounts[0]
            out.append(Claim(kind="payment", title=_title(context, exclude=[a.text]), quote=line.strip(), page=page,
                             amount=Amount(value=a.value, currency=a.currency, text=a.text), requires=["payment"],
                             optional=optional, confidence=0.3))
        return out


def _title(text: str, exclude: list[str] = ()) -> str:
    for piece in exclude:
        text = text.replace(piece, " ")
    text = re.sub(r"^[\s•\-\d.)]+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ,:;-–")
    return (text[:97] + "...") if len(text) > 100 else (text or "Untitled")


def _dedupe(claims: list[Claim]) -> list[Claim]:
    seen, out = set(), []
    for c in claims:
        key = (c.kind, c.date, c.end_date, c.title.lower()[:40])
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out
