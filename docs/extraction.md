# Extraction (milestone 2)

Extraction reads an accepted notice and lists the facts a family needs:
dates, deadlines, amounts, forms to sign, changes to earlier notices. Each
fact is a **claim** that carries the words it was read from and where those
words sit in the original. Actionable claims become **proposed obligations**
that a member accepts or dismisses. Nothing is decided for the family, and
nothing ever pays.

English only for now.

## Pipeline

```
accepted artifact
  -> parse        familyos/extraction/parse.py     page text + a box per word
  -> extract      llm.py (Claude) or rules.py      typed claims, each with a verbatim quote
  -> ground       ground.py                        find the quote: page, character span, boxes
  -> decide       pipeline.py                      actionable or not, and why
  -> propose      obligations.py                   tasks and calendar entries, all "proposed"
  -> store        service.py                       extractions, claims, obligations tables
```

It runs as a durable job (`extract_artifact`, on `jobs.py`) queued in the
same transaction that accepts an artifact: an upload, an authenticated
email and each of its attachments, or a quarantined item once a guardian
accepts it. Quarantined mail is never read before that.

### Parsing

- **PDF**: the text layer via PyMuPDF. A page with almost no text but an
  image (a scan) is rendered at 200 dpi and read with OCR.
- **Images** (photos, WhatsApp forwards): Tesseract OCR, English.
- **Email**: subject and plain-text body (HTML stripped when there is no
  plain part). Attachments are artifacts of their own and are read separately.

Page text is rebuilt from words, so every word keeps its character span and
its box: PDF points from the top left, or image pixels.

### Extractors

- **claude** (`llm.py`): the model gets the page text, not the file, so
  every quote it returns can be looked up in the text we store. The answer
  is constrained to a JSON schema with structured outputs. Default model
  `claude-opus-5-5` at effort `medium`; refused requests fall back to
  another model server-side. The prompt is versioned (`PROMPT_VERSION`);
  change it whenever the prompt or schema changes.
- **rules** (`rules.py`): regular expressions over lines with dates and
  amounts, no network. The baseline the model is scored against, and the
  fallback when no API key is configured.

`FAMILYOS_EXTRACTOR=auto` (the default) uses Claude when
`FAMILYOS_ANTHROPIC_API_KEY` is set, and the rules otherwise.

### Claims

`kind` is one of `event`, `deadline`, `payment`, `form`, `amendment`,
`instruction`, `info`. A claim has `date`/`end_date` (ISO) or, when the
notice gives no exact date, `date_text` with `uncertain` set; `time`,
`place`, `amount` (value, currency, text as written), `applies_to` (the
class or group it is scoped to), `subject_name` (only when a student is
named), `requires` (`parent_signature`, `form_return`, `payment`,
`parent_attendance`, `medical_info`, `items_to_bring`), `optional`, and for
amendments `amends`/`change`. Every claim records the extractor, model,
prompt version and parser version on its extraction, and a confidence.

### Grounding

The quote is looked up exactly, then ignoring case, spacing and typographic
quotes and dashes, then as the closest run of words (at least 85% similar).
The claim stores the page, the character span in that page's text, one box
per line, and how it matched. A claim whose quote is not found is kept,
visible for review, with its confidence halved, and **never becomes an
obligation**.

### Obligations

| Claim | Proposed as |
|---|---|
| deadline | task: sign, pay or submit, due on its date |
| form (no return date given) | task: sign (dropped when a deadline already covers the form) |
| payment | task: pay (a reminder only) |
| event a parent must attend | task: attend |
| other dated, certain event; dated amendment | calendar entry |

Nothing is proposed for dates already past when the notice was read. When
the artifact names exactly one subject (a child), the obligation is for
that child. Re-reading an artifact supersedes its extraction and replaces
obligations still `proposed`; accepted and dismissed ones stay.

### Privacy

- Claims and obligations cascade with their artifact, so deleting a notice,
  withdrawing a child's consent, or erasing the household erases them.
- The model's answer is cached in the job only while the job runs (so a
  resume does not pay twice) and cleared once the claims are stored.
  Subject erasure also deletes extraction jobs for the erased artifacts.
- Audit events for extraction carry counts and versions, never text.

## API

| Method | Path | Who |
|---|---|---|
| GET | `/v1/artifacts/{id}/extraction` | members who can see the artifact: claims with locations, or the job's status |
| POST | `/v1/artifacts/{id}/extract` | members who can see it: read it again |
| GET | `/v1/obligations?status=proposed` | members: obligations from artifacts they can see (`status=` for all) |
| POST | `/v1/obligations/{id}/decision` | members who can see it: `{"status": "accepted" \| "dismissed"}` |

## Scoring

The test set is `evalset/` (from PR 2): `notices.jsonl` with the facts each
notice should produce, and the originals.

```sh
python -m familyos.extraction.evaluate --extractor rules
FAMILYOS_ANTHROPIC_API_KEY=... python -m familyos.extraction.evaluate --extractor claude --cache .extraction-cache \
    --out report.json --markdown report.md
```

With `--cache`, model answers are stored per notice, model, effort, prompt
and parser version, so re-scoring after labels change makes no new calls.
"Today" is pinned to each notice's issue date.

What it measures:

- **Recall**: a labelled event, deadline, form, payment or amendment counts
  as found when a claim of a compatible kind has the labelled date or shares
  at least 30% of the label's title words. Label kinds such as
  `instruction` or `info` are not scored yet.
- **Precision**: the share of claims matched to a labelled fact. Labels
  are not exhaustive for long circulars (en-001, en-003), so this is a floor.
- **Field accuracy** on matched pairs: date and end date (only on pairs that
  also match by title, so a date is never scored against itself), amount,
  and the flags (parent attendance, form, payment, optional), plus
  `no_invented_date` for facts the label says have no exact date.
- **Actionable** per notice, **expect_no** violations (things a notice must
  not produce), and **grounding** rate.
