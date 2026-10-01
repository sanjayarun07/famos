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


EXTRACTORS = ("rules", "model", "claude", "anthropic", "openai")


def make_extractor(name: str | None = None):
    """`rules` reads notices here and sends nothing anywhere. `model` uses the
    provider of FAMILYOS_EXTRACTION_MODEL; `claude` and `openai` pick that
    provider with its default model.

    There is deliberately no mode that decides for you. Sending a household's
    notices to a model provider is a choice somebody makes, so it is named in
    the configuration and never inferred from an API key happening to be in the
    environment. A model asked for without its key is a configuration error and
    says so, rather than quietly reading the notice some other way."""
    name = name or settings.extractor
    if name not in EXTRACTORS:
        raise ValueError(f"FAMILYOS_EXTRACTOR must be one of {', '.join(EXTRACTORS)}, not {name!r}")
    if name == "rules":
        return RulesExtractor()

    model = settings.extraction_model
    if name in ("claude", "anthropic") and split_model(model)[0] != "anthropic":
        model = "anthropic/claude-opus-5-5"
    elif name == "openai" and split_model(model)[0] != "openai":
        model = "openai/gpt-5.6-sol"
    provider = split_model(model)[0]
    keys = {"anthropic": settings.anthropic_api_key, "openai": settings.openai_api_key}
    if provider not in keys:
        raise ValueError(f"unknown model provider {provider!r} in {model!r}")
    if not keys[provider]:
        raise ValueError(
            f"FAMILYOS_EXTRACTOR={name} asks for {model}, but no {provider} API key is set. "
            f"Set it, or use FAMILYOS_EXTRACTOR=rules to read notices without sending them anywhere.")
    if provider == "anthropic":
        return ClaudeExtractor(api_key=keys["anthropic"], model=model, effort=settings.extraction_effort)
    return OpenAIExtractor(api_key=keys["openai"], model=model, effort=settings.extraction_effort)


def describe() -> str:
    """One line for the log at startup: what reads notices, and where their
    text goes."""
    name = settings.extractor
    if name == "rules":
        return "extraction: rules (offline) - notice text stays in this deployment"
    try:
        extractor = make_extractor()
    except ValueError as exc:
        return f"extraction: MISCONFIGURED - {exc}"
    return (f"extraction: {extractor.name} model {extractor.model} - the full text of every accepted notice, "
            f"names included, is sent to this provider")


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
                      page_count=len(doc.pages), ocr_pages=doc.ocr_pages, scripts=doc.scripts, notes=notes)


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
