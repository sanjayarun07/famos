"""The model-based extractor: Claude reads the notice and returns typed claims.

The model sees the parsed page text, not the file, so every quote it
returns can be looked up in the same text we store (grounding). The answer
is constrained to a JSON schema with structured outputs. The prompt is
versioned: change PROMPT_VERSION whenever the prompt or schema changes, so
stored claims say which instructions produced them.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from familyos.extraction.claims import KINDS, REQUIREMENTS, Claim
from familyos.extraction.parse import ParsedDocument

logger = logging.getLogger(__name__)

NAME = "claude"
PROMPT_VERSION = "extract-v1"
# Refused requests are retried on a fallback model chosen by the API.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_INPUT_CHARS = 400_000

SYSTEM = """You read notices that schools and other institutions send to families (circulars, trip letters, consent forms, fee reminders, newsletters) and list the facts a parent needs, as typed claims.

Rules:
- One claim per fact. A circular that lists ten dated items gives ten claims. Keep each claim scoped: if a fact applies to one class or group, say so in applies_to (e.g. "Class IX", "KG II", "Year 11 Media students", "all").
- kind:
  - event: something that happens on a date or over dates (a trip, an exam, a holiday or closure, a meeting, a celebration).
  - deadline: something the family must do by a date (return a form, pay a fee, submit an application). date is the due date.
  - payment: money the family must pay, or a charge they will be billed, when no due date is given (a due date makes it a deadline with requires including "payment" and the amount set).
  - form: a form, slip or consent the parent must fill or sign, when the notice gives no return date (with a return date, make it a deadline with requires "form_return").
  - amendment: a change to an earlier arrangement (postponed, rescheduled, revised timings, extended holiday). amends says what it changes, change says what is now true, date is the new date if one is given.
  - instruction: what to bring, wear or do that has no date of its own.
  - info: anything else a parent should know that asks nothing of them.
- Dates: write ISO dates (YYYY-MM-DD). The notice was issued on or about {reference_date}; use it to resolve dates with no year and words like "this Saturday". Read numeric dates the way the notice's country writes them (day first in India and the UK, month first in the US). For a range, set date and end_date. When the notice gives no exact date ("first week of March", "to be announced", "date will be communicated"), leave date null, put the words in date_text, and set uncertain to true. Never invent a date. If a printed date is clearly a typo, give the corrected date and set uncertain to true.
- Amounts: value as a number, currency as an ISO code (INR, USD, GBP, OMR, EUR), text as written. Account numbers, bank details and minimum-transfer rules are not amounts to pay.
- requires lists what the family must do: parent_signature, form_return, payment, parent_attendance (a parent must come, e.g. a parent-teacher meeting), medical_info, items_to_bring.
- optional: true when the notice makes it the family's choice (opt-in lockers, optional vaccination, a chaperone slip, open days for prospective families).
- subject_name: only when the notice names a specific student. Blank lines to fill in are not names.
- A blank template (every date and name is a blank line) states no dates, costs or deadlines: give only the form claim.
- A notice addressed to schools or staff rather than families asks nothing of a parent: set actionable false and explain why in non_actionable_reason. Also set actionable false when the notice only informs.
- quote: copy the words the claim comes from exactly as they appear in the text, character for character, one sentence or line, at most 200 characters. page: the page the quote is on.
- confidence: from 0 to 1, how sure you are the claim is right as stated.
"""

USER = """Notice text, page by page:

{pages}

List the claims in this notice."""


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


CLAIM_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "title": {"type": "string"},
        "date": _nullable({"type": "string"}),
        "end_date": _nullable({"type": "string"}),
        "date_text": _nullable({"type": "string"}),
        "time": _nullable({"type": "string"}),
        "place": _nullable({"type": "string"}),
        "amount": _nullable({
            "type": "object",
            "properties": {"value": {"type": "number"}, "currency": _nullable({"type": "string"}), "text": _nullable({"type": "string"})},
            "required": ["value", "currency", "text"], "additionalProperties": False}),
        "applies_to": _nullable({"type": "string"}),
        "subject_name": _nullable({"type": "string"}),
        "requires": {"type": "array", "items": {"type": "string", "enum": list(REQUIREMENTS)}},
        "optional": {"type": "boolean"},
        "uncertain": {"type": "boolean"},
        "amends": _nullable({"type": "string"}),
        "change": _nullable({"type": "string"}),
        "quote": {"type": "string"},
        "page": {"type": "integer"},
        "confidence": {"type": "number"},
    },
    "additionalProperties": False,
}
CLAIM_SCHEMA["required"] = list(CLAIM_SCHEMA["properties"])

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "actionable": {"type": "boolean"},
        "non_actionable_reason": _nullable({"type": "string"}),
        "claims": {"type": "array", "items": CLAIM_SCHEMA},
    },
    "required": ["actionable", "non_actionable_reason", "claims"],
    "additionalProperties": False,
}


def render_pages(doc: ParsedDocument) -> str:
    return "\n\n".join(f'<page number="{p.number}">\n{p.text}\n</page>' for p in doc.pages)


def build_request(doc: ParsedDocument, reference_date: dt.date | None, *, model: str, effort: str) -> dict:
    pages = render_pages(doc)
    if len(pages) > MAX_INPUT_CHARS:
        raise ValueError(f"notice text is {len(pages)} characters; the limit is {MAX_INPUT_CHARS}")
    return {
        "model": model,
        "max_tokens": 32000,
        "system": SYSTEM.replace("{reference_date}", reference_date.isoformat() if reference_date else "an unknown date"),
        "messages": [{"role": "user", "content": USER.replace("{pages}", pages)}],
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    }


class ClaudeExtractor:
    name = NAME
    prompt_version = PROMPT_VERSION

    def __init__(self, *, api_key: str | None, model: str, effort: str = "medium", client=None):
        self.model = model
        self.effort = effort
        self._client = client
        self._api_key = api_key

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.AsyncAnthropic(api_key=self._api_key, max_retries=3)
        return self._client

    async def call(self, doc: ParsedDocument, reference_date: dt.date | None) -> dict:
        """One model call; returns the parsed JSON answer and usage. This is
        the part a job caches, so a resumed job never pays for it twice."""
        request = build_request(doc, reference_date, model=self.model, effort=self.effort)
        async with self._get_client().beta.messages.stream(**request, betas=[FALLBACK_BETA], fallbacks="default") as stream:
            message = await stream.get_final_message()
        if message.stop_reason == "refusal":
            raise RuntimeError("the model declined to read this notice")
        if message.stop_reason == "max_tokens":
            raise RuntimeError("the model's answer was cut off")
        text = next((b.text for b in message.content if b.type == "text"), None)
        if text is None:
            raise RuntimeError("the model returned no answer")
        usage = message.usage
        return {"answer": json.loads(text), "model": message.model,
                "usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}}

    @staticmethod
    def claims_from(answer: dict) -> list[Claim]:
        claims = []
        for raw in answer.get("claims") or []:
            try:
                claims.append(Claim.model_validate({k: v for k, v in raw.items() if v is not None or k in ("date",)}))
            except ValueError:
                logger.warning("dropped a claim that did not validate: kind=%s", raw.get("kind"))
        return claims
