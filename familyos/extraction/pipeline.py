"""The extraction pipeline: parsed notice in, grounded claims out.

    parse (parse.py) -> extractor (rules.py or llm.py) -> ground (ground.py)
    -> actionable or not -> proposed obligations (obligations.py)

The database and the job live in job.py; this module is pure so the scorer
can run it on the test set without a database.
"""
from __future__ import annotations

import datetime as dt

from familyos.extraction import ground
from familyos.extraction.claims import Claim, Extraction, ExtractorInfo
from familyos.extraction.llm import ClaudeExtractor, ModelExtractor, OpenAIExtractor, split_model
from familyos.extraction.parse import ParsedDocument
from familyos.extraction.rules import RulesExtractor
from familyos.settings import settings

UNGROUNDED_PENALTY = 0.5
TASK_KINDS = {"deadline", "form", "payment"}


def make_extractor(name: str | None = None):
    """`rules`, `model` (the provider of FAMILYOS_EXTRACTION_MODEL), or `auto`:
    the model when its provider's API key is set, the rules otherwise.
    `claude` and `openai` pick that provider with its default model."""
    name = name or settings.extractor
    model = settings.extraction_model
    if name in ("claude", "anthropic") and split_model(model)[0] != "anthropic":
        model = "anthropic/claude-opus-5-5"
    elif name == "openai" and split_model(model)[0] != "openai":
        model = "openai/gpt-5.6-sol"
    provider = split_model(model)[0]
    keys = {"anthropic": settings.anthropic_api_key, "openai": settings.openai_api_key}
    if name == "auto":
        name = "model" if keys.get(provider) else "rules"
    if name == "rules":
        return RulesExtractor()
    if provider == "anthropic":
        return ClaudeExtractor(api_key=keys["anthropic"] or None, model=model, effort=settings.extraction_effort)
    if provider == "openai":
        return OpenAIExtractor(api_key=keys["openai"] or None, model=model, effort=settings.extraction_effort)
    raise ValueError(f"unknown model provider {provider!r} in {model!r}")


def info(extractor, doc: ParsedDocument, model: str | None = None) -> ExtractorInfo:
    return ExtractorInfo(name=extractor.name, model=model or extractor.model, prompt_version=extractor.prompt_version,
                         parser_version=doc.parser_version)


def finish(doc: ParsedDocument, claims: list[Claim], extractor_info: ExtractorInfo, reference_date: dt.date | None, *,
           actionable: bool | None = None, reason: str | None = None) -> Extraction:
    """Ground every claim, then decide whether the notice asks anything of the family."""
    grounded = []
    for c in claims:
        loc = ground.locate(doc, c.quote, c.page)
        c = c.model_copy(update={"location": loc.as_dict() if loc else None, "page": loc.page if loc else c.page,
                                 "confidence": c.confidence if loc else round(c.confidence * UNGROUNDED_PENALTY, 3)})
        grounded.append(c)
    asks = any(c.grounded and (c.kind in TASK_KINDS or "parent_attendance" in c.requires) for c in grounded)
    if actionable is None:
        actionable = asks or any(c.grounded and c.kind in ("event", "amendment") and c.date for c in grounded)
        reason = None if actionable else ("no text could be read" if doc.char_count == 0 else "no dated events or requests")
    notes = list(doc.notes)
    if doc.char_count == 0:
        notes.append("no_text")
    return Extraction(extractor=extractor_info, reference_date=reference_date, actionable=actionable,
                      non_actionable_reason=None if actionable else reason, claims=grounded,
                      page_count=len(doc.pages), ocr_pages=doc.ocr_pages, notes=notes)


async def extract(doc: ParsedDocument, reference_date: dt.date | None, extractor=None) -> Extraction:
    extractor = extractor or make_extractor()
    if isinstance(extractor, ModelExtractor):
        if doc.char_count == 0:
            return finish(doc, [], info(extractor, doc), reference_date)
        response = await extractor.call(doc, reference_date)
        return from_model_response(doc, response, extractor, reference_date)
    claims = await extractor.run(doc, reference_date)
    return finish(doc, claims, info(extractor, doc), reference_date)


def from_model_response(doc: ParsedDocument, response: dict, extractor: ModelExtractor,
                        reference_date: dt.date | None) -> Extraction:
    answer = response["answer"]
    return finish(doc, ModelExtractor.claims_from(answer), info(extractor, doc, response.get("model")), reference_date,
                  actionable=bool(answer.get("actionable")), reason=answer.get("non_actionable_reason"))
