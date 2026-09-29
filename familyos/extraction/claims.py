"""The typed claims an extractor reads out of a notice.

A claim is one fact the notice states, with the words it was read from and
where those words sit in the original. Claims are what the notice says;
obligations (obligations.py) are what the family might do about it.
"""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field

ClaimKind = Literal["event", "deadline", "payment", "form", "amendment", "instruction", "info"]
Requirement = Literal["parent_signature", "form_return", "payment", "parent_attendance", "medical_info", "items_to_bring"]

KINDS: tuple[str, ...] = ClaimKind.__args__  # type: ignore[attr-defined]
REQUIREMENTS: tuple[str, ...] = Requirement.__args__  # type: ignore[attr-defined]


class Amount(BaseModel):
    value: float
    currency: str | None = None     # ISO 4217 where known: INR, USD, GBP, OMR
    text: str | None = None         # as written


class Claim(BaseModel):
    kind: ClaimKind
    title: str
    # event: when it happens; deadline: when it is due; amendment: the new date.
    date: dt.date | None = None
    end_date: dt.date | None = None
    date_text: str | None = None    # as written, when no single date fits ("first week of March")
    time: str | None = None
    place: str | None = None
    amount: Amount | None = None
    applies_to: str | None = None   # class, grade or group the fact is scoped to
    subject_name: str | None = None  # a student named on the notice
    requires: list[Requirement] = Field(default_factory=list)
    optional: bool = False
    uncertain: bool = False
    amends: str | None = None       # amendment: what earlier arrangement it changes
    change: str | None = None       # amendment: what changed
    quote: str                      # the words the claim was read from, verbatim
    page: int | None = None
    location: dict | None = None    # set by grounding: page, start, end, boxes, match
    confidence: float = 0.5

    @property
    def grounded(self) -> bool:
        return self.location is not None


class ExtractorInfo(BaseModel):
    name: str
    model: str | None = None
    prompt_version: str
    parser_version: str


class Extraction(BaseModel):
    extractor: ExtractorInfo
    reference_date: dt.date | None = None
    actionable: bool
    non_actionable_reason: str | None = None
    claims: list[Claim]
    page_count: int
    ocr_pages: int = 0
    notes: list[str] = Field(default_factory=list)
