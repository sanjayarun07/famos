"""From claims to proposed obligations: what the family might do about a notice.

Nothing here is decided for the family. Each obligation starts `proposed`
and a member accepts or dismisses it. Two kinds:

- task: something to do (sign and return a form, pay, attend, apply).
- calendar: a dated event to know about (a trip day, a holiday, an exam).

Only grounded claims (their quote was found in the original) become
obligations; an ungrounded claim stays visible for review and nothing more.
Nothing ever pays: a payment task is a reminder, never an instruction.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from familyos.extraction.claims import Claim


@dataclass
class ProposedObligation:
    claim_index: int
    kind: str                    # task | calendar
    action: str                  # sign | pay | attend | submit | prepare | note
    title: str
    due_date: dt.date | None
    end_date: dt.date | None
    due_time: str | None
    optional: bool


def _action(claim: Claim) -> str:
    req = set(claim.requires)
    if claim.kind == "form" or "parent_signature" in req or "form_return" in req:
        return "sign"
    if claim.kind == "payment" or "payment" in req:
        return "pay"
    if "parent_attendance" in req:
        return "attend"
    if "items_to_bring" in req:
        return "prepare"
    return "submit" if claim.kind == "deadline" else "note"


def propose(claims: list[Claim], reference_date: dt.date | None = None) -> list[ProposedObligation]:
    out: list[ProposedObligation] = []
    # A form whose return date is also stated as a deadline gets one task, from the deadline.
    deadline_forms = any(c.kind == "deadline" and ({"form_return", "parent_signature"} & set(c.requires)) for c in claims)
    for i, c in enumerate(claims):
        if not c.grounded:
            continue
        if c.kind == "deadline":
            out.append(ProposedObligation(i, "task", _action(c), c.title, c.date, None, c.time, c.optional))
        elif c.kind == "form":
            if deadline_forms:
                continue
            out.append(ProposedObligation(i, "task", "sign", c.title, c.date, None, None, c.optional))
        elif c.kind == "payment":
            out.append(ProposedObligation(i, "task", "pay", c.title, c.date, None, None, c.optional))
        elif c.kind == "event":
            if "parent_attendance" in c.requires:
                out.append(ProposedObligation(i, "task", "attend", c.title, c.date, c.end_date, c.time, c.optional))
            elif c.date is not None and not c.uncertain:
                out.append(ProposedObligation(i, "calendar", "note", c.title, c.date, c.end_date, c.time, c.optional))
        elif c.kind == "amendment" and c.date is not None:
            out.append(ProposedObligation(i, "calendar", "note", c.title, c.date, c.end_date, c.time, c.optional))
    if reference_date is not None:
        # A notice read long after it was issued should not propose work for days already gone.
        out = [o for o in out if o.due_date is None or (o.end_date or o.due_date) >= reference_date - dt.timedelta(days=1)]
    return out
