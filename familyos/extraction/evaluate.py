"""Score an extractor against the labelled test set.

    python -m familyos.extraction.evaluate --evalset evalset --extractor rules
    python -m familyos.extraction.evaluate --evalset evalset --extractor model --cache .extraction-cache

Each line of notices.jsonl is one notice with its expected facts. The
scorer runs the same pipeline intake uses (parse, extract, ground) on each
original, matches predicted claims to expected ones, and reports:

- detection: how many expected facts of each kind were found (recall), and
  how many predicted claims matched something expected (precision);
- field accuracy on matched pairs: date, end date, amount, and the flags
  (parent must attend, form to sign, payment, optional);
- whether the notice was correctly called actionable or not;
- `expect_no` violations: things a notice must not produce (a deadline on a
  form that has none, a cost on a blank template);
- grounding: the share of claims whose quote was found in the original.

Labels change while they are being reviewed, so the scorer is meant to be
re-run. With --cache, model answers are stored per notice, model and prompt
version, and re-scoring after a label fix makes no new model calls.

"Today" is pinned to each notice's issue date, since many notices are old.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from familyos.extraction import pipeline
from familyos.extraction.claims import Claim, Extraction
from familyos.extraction.dates import amounts_in_label, iso_dates
from familyos.extraction.llm import ModelExtractor
from familyos.extraction.obligations import propose
from familyos.extraction.parse import parse

# Label kinds that are scored, and the predicted kinds that can satisfy each.
SCORED = {
    "event": {"event"},
    "deadline": {"deadline", "form", "payment"},
    "form": {"form", "deadline"},
    "payment": {"payment", "deadline"},
    "charge": {"payment", "deadline"},
    "amendment": {"amendment", "event"},
}
KIND_GROUP = {"charge": "payment"}
ACTIONABLE_LABEL_KINDS = {"event", "deadline", "form", "payment", "charge", "amendment", "recurring_deadline"}
MEDIA_TYPES = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".eml": "message/rfc822", ".txt": "text/plain"}
STOP = set("the a an of to for and on in at by with from is are be will your our all any this that".split())


def reference_date(issued) -> dt.date | None:
    s = str(issued or "")
    for fmt, pattern in (("%Y-%m-%d", r"^\d{4}-\d{2}-\d{2}$"), ("%Y-%m", r"^\d{4}-\d{2}$"), ("%Y", r"^\d{4}$")):
        if re.match(pattern, s):
            try:
                return dt.datetime.strptime(s, fmt).date()
            except ValueError:
                break
    if m := re.match(r"^(\d{4})-\d{2}$", s):      # an academic year such as 2025-26
        return dt.date(int(m.group(1)), 6, 1)
    return None


def _tokens(text) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", str(text or "").lower()) if t not in STOP and len(t) > 1}


def _overlap(expected: dict, claim: Claim) -> float:
    e = _tokens(" ".join(str(expected.get(k) or "") for k in ("title", "text", "amends", "change")))
    p = _tokens(" ".join(str(x or "") for x in (claim.title, claim.change, claim.amends, claim.quote)))
    return len(e & p) / len(e) if e else 0.0


def expected_dates(item: dict) -> list[dt.date]:
    kind = item["kind"]
    if kind == "amendment":
        return iso_dates(item.get("change"))
    return iso_dates(item.get("due") if kind in ("deadline", "form", "payment", "charge") else item.get("date"))


def _pair_score(item: dict, claim: Claim) -> float | None:
    if claim.kind not in SCORED[item["kind"]]:
        return None
    if item["kind"] == "deadline" and claim.kind != "deadline" and not claim.date:
        return None
    dates = expected_dates(item)
    overlap = _overlap(item, claim)
    date_hit = bool(dates) and claim.date in dates
    if dates and not date_hit and overlap < 0.3:
        return None
    if not dates and overlap < 0.25:
        return None
    # Prefer the same kind when both would do.
    same = 0.5 if claim.kind == KIND_GROUP.get(item["kind"], item["kind"]) else 0.0
    return 2.0 * date_hit + overlap + same


def match(expected: list[dict], claims: list[Claim]) -> list[tuple[int, int]]:
    """Greedy one-to-one matching of scored labels to claims."""
    pairs = []
    for i, item in enumerate(expected):
        if item["kind"] not in SCORED:
            continue
        for j, c in enumerate(claims):
            s = _pair_score(item, c)
            if s is not None:
                pairs.append((s, i, j))
    used_i, used_j, out = set(), set(), []
    for _, i, j in sorted(pairs, reverse=True):
        if i not in used_i and j not in used_j:
            used_i.add(i)
            used_j.add(j)
            out.append((i, j))
    return out


def field_checks(item: dict, c: Claim) -> dict[str, bool]:
    """Which fields of a matched pair were right. Only fields the label speaks
    to are checked. Dates are checked only on pairs that also match by title,
    since a pair matched on its date alone would score its own date as right."""
    checks: dict[str, bool] = {}
    kind = item["kind"]
    dates = expected_dates(item)
    raw = item.get("change") if kind == "amendment" else item.get("due" if kind in ("deadline", "form", "payment", "charge") else "date")
    if (kind != "amendment" or dates) and _overlap(item, c) >= 0.3:
        if dates:
            checks["date"] = c.date == dates[0]
            ranged = isinstance(raw, str) and re.search(r"\d{4}-\d{2}-\d{2}\s+to\s+\d{4}-\d{2}-\d{2}", raw)
            if ranged and len(dates) >= 2:
                checks["end_date"] = c.end_date == dates[1]
        elif raw is not None or item.get("uncertain"):
            # The label says there is no exact date: the claim must not invent one.
            checks["no_invented_date"] = c.date is None or c.uncertain
    numbers = amounts_in_label(item.get("amount"))
    if numbers:
        checks["amount"] = c.amount is not None and any(abs(c.amount.value - n) < 0.01 for n in numbers)
    req = set(c.requires)
    if item.get("requires_parent"):
        checks["parent_attendance"] = "parent_attendance" in req
    if item.get("form_required") or item.get("requires_parent_signature") or kind == "form":
        checks["form"] = c.kind == "form" or bool(req & {"form_return", "parent_signature"})
    if item.get("payment") or kind in ("payment", "charge"):
        checks["payment"] = c.kind == "payment" or "payment" in req
    if "optional" in item:
        checks["optional"] = c.optional == bool(item["optional"])
    return checks


def expect_no_violations(expect_no: list[str], extraction: Extraction, obligations) -> list[str]:
    claims = [c for c in extraction.claims if c.grounded]
    out = []
    for rule in expect_no or []:
        r = rule.lower()
        if "deadline" in r:
            hit = any(c.kind == "deadline" and (("payment" in r and "payment" in c.requires) or "payment" not in r) for c in claims)
        elif r in ("event", "event date"):
            hit = any(c.kind == "event" and c.date for c in claims)
        elif r == "new date":
            hit = any(c.kind in ("amendment", "event") and c.date for c in claims)
        elif r in ("cost", "amount", "payment"):
            hit = any(c.amount is not None or c.kind == "payment" or "payment" in c.requires for c in claims)
        elif r == "form":
            hit = any(c.kind == "form" for c in claims)
        elif r == "parent obligation":
            hit = any(o.kind == "task" for o in obligations)
        else:
            continue
        if hit:
            out.append(rule)
    return out


def score_notice(row: dict, extraction: Extraction) -> dict:
    expected = row.get("expected") or []
    claims = extraction.claims
    pairs = match(expected, claims)
    obligations = propose(claims, extraction.reference_date)
    fields = defaultdict(lambda: [0, 0])
    matched = []
    for i, j in pairs:
        checks = field_checks(expected[i], claims[j])
        for k, ok in checks.items():
            fields[k][0] += ok
            fields[k][1] += 1
        matched.append({"expected": i, "claim": j, "checks": checks})
    matched_i = {i for i, _ in pairs}
    matched_j = {j for _, j in pairs}
    detection = defaultdict(lambda: [0, 0])
    for i, item in enumerate(expected):
        if item["kind"] in SCORED:
            k = KIND_GROUP.get(item["kind"], item["kind"])
            detection[k][0] += i in matched_i
            detection[k][1] += 1
    expected_actionable = any(e["kind"] in ACTIONABLE_LABEL_KINDS for e in expected)
    return {
        "id": row["id"],
        "doc_type": row.get("doc_type"),
        "label_status": row.get("label_status"),
        "reference_date": extraction.reference_date.isoformat() if extraction.reference_date else None,
        "expected_scored": sum(1 for e in expected if e["kind"] in SCORED),
        "expected_unscored_kinds": sorted({e["kind"] for e in expected if e["kind"] not in SCORED}),
        "claims": len(claims),
        "claims_matched": len(matched_j),
        "claims_grounded": sum(1 for c in claims if c.grounded),
        "grounding_match": dict(Counter((c.location or {}).get("match", "none") for c in claims)),
        "detection": {k: v for k, v in detection.items()},
        "fields": {k: v for k, v in fields.items()},
        "actionable": {"expected": expected_actionable, "predicted": extraction.actionable},
        "expect_no_violations": expect_no_violations(row.get("expect_no") or [], extraction, obligations),
        "obligations": len(obligations),
        "missed": [expected[i].get("title") or expected[i].get("text") or expected[i].get("amends")
                   for i, e in enumerate(expected) if e["kind"] in SCORED and i not in matched_i],
        "ocr_pages": extraction.ocr_pages,
        "notes": extraction.notes,
        "matched": matched,
    }


def summarize(results: list[dict]) -> dict:
    det = defaultdict(lambda: [0, 0])
    fields = defaultdict(lambda: [0, 0])
    for r in results:
        for k, (a, b) in r["detection"].items():
            det[k][0] += a
            det[k][1] += b
        for k, (a, b) in r["fields"].items():
            fields[k][0] += a
            fields[k][1] += b
    claims = sum(r["claims"] for r in results)
    act = [r["actionable"] for r in results]
    ratio = lambda a, b: round(a / b, 3) if b else None  # noqa: E731
    found = sum(v[0] for v in det.values())
    total = sum(v[1] for v in det.values())
    return {
        "notices": len(results),
        "recall": {"all": [found, total, ratio(found, total)], **{k: [a, b, ratio(a, b)] for k, (a, b) in sorted(det.items())}},
        "precision": [sum(r["claims_matched"] for r in results), claims, ratio(sum(r["claims_matched"] for r in results), claims)],
        "fields": {k: [a, b, ratio(a, b)] for k, (a, b) in sorted(fields.items())},
        "actionable_accuracy": [sum(a["expected"] == a["predicted"] for a in act), len(act),
                                ratio(sum(a["expected"] == a["predicted"] for a in act), len(act))],
        "expect_no": [sum(1 for r in results if r["expect_no_violations"]),
                      sum(1 for r in results if r.get("has_expect_no"))],
        "grounding": [sum(r["claims_grounded"] for r in results), claims, ratio(sum(r["claims_grounded"] for r in results), claims)],
    }


def render(summary: dict, results: list[dict], meta: dict) -> str:
    pct = lambda t: "n/a" if t[2] is None else f"{t[2] * 100:.0f}% ({t[0]}/{t[1]})"  # noqa: E731
    lines = [f"# Extraction score: {meta['extractor']}", "",
             f"Test set: {meta['evalset']} ({summary['notices']} notices, labels sha256 {meta['labels_sha256'][:12]})",
             f"Model: {meta.get('model') or 'none'}; prompt {meta['prompt_version']}; parser {meta['parser_version']}", "",
             "| Measure | Score |", "|---|---|",
             f"| Facts found (recall) | {pct(summary['recall']['all'])} |"]
    for k in ("event", "deadline", "form", "payment", "amendment"):
        if k in summary["recall"]:
            lines.append(f"| - {k} | {pct(summary['recall'][k])} |")
    lines += [f"| Claims that match a labelled fact (precision) | {pct(summary['precision'])} |",
              f"| Actionable or not, per notice | {pct(summary['actionable_accuracy'])} |",
              f"| Notices with an expect_no violation | {summary['expect_no'][0]} of {summary['expect_no'][1]} |",
              f"| Claims grounded in the original | {pct(summary['grounding'])} |", "",
              "Field accuracy on matched facts:", "", "| Field | Correct |", "|---|---|"]
    for k, v in summary["fields"].items():
        lines.append(f"| {k} | {pct(v)} |")
    lines += ["", "| Notice | Found | Claims | Matched | Actionable (exp/pred) | expect_no violated |", "|---|---|---|---|---|---|"]
    for r in results:
        found = sum(v[0] for v in r["detection"].values())
        lines.append(f"| {r['id']} | {found}/{r['expected_scored']} | {r['claims']} | {r['claims_matched']} | "
                     f"{'yes' if r['actionable']['expected'] else 'no'}/{'yes' if r['actionable']['predicted'] else 'no'} | "
                     f"{', '.join(r['expect_no_violations']) or '-'} |")
    return "\n".join(lines) + "\n"


async def run(evalset: Path, extractor_name: str, cache: Path | None, ids: set[str] | None) -> tuple[dict, list[dict], dict]:
    labels_path = evalset / "notices.jsonl"
    raw_labels = labels_path.read_bytes()
    rows = [json.loads(line) for line in raw_labels.decode().splitlines() if line.strip()]
    extractor = pipeline.make_extractor(extractor_name)
    results = []
    for row in rows:
        if ids and row["id"] not in ids:
            continue
        if not row.get("original_path"):
            print(f"{row['id']}: no original, skipped", file=sys.stderr)
            continue
        path = evalset / row["original_path"]
        data = path.read_bytes()
        doc = parse(data, MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"))
        ref = reference_date(row.get("issued"))
        if isinstance(extractor, ModelExtractor):
            response = None
            key = hashlib.sha256(f"{hashlib.sha256(data).hexdigest()}:{extractor.model}:{extractor.effort}:"
                                 f"{extractor.prompt_version}:{doc.parser_version}:{ref}".encode()).hexdigest()[:24]
            cached = cache / f"{row['id']}-{key}.json" if cache else None
            if cached and cached.exists():
                response = json.loads(cached.read_text())
            elif doc.char_count:
                response = await extractor.call(doc, ref)
                if cached:
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    cached.write_text(json.dumps(response, indent=1))
            extraction = (pipeline.from_model_response(doc, response, extractor, ref) if response
                          else pipeline.finish(doc, [], pipeline.info(extractor, doc), ref))
        else:
            extraction = await pipeline.extract(doc, ref, extractor)
        result = score_notice(row, extraction)
        result["has_expect_no"] = bool(row.get("expect_no"))
        results.append(result)
        print(f"{row['id']}: {sum(v[0] for v in result['detection'].values())}/{result['expected_scored']} found, "
              f"{result['claims']} claims", file=sys.stderr)
    meta = {"extractor": extractor.name, "model": extractor.model, "prompt_version": extractor.prompt_version,
            "parser_version": "parse-v1", "evalset": str(evalset), "labels_sha256": hashlib.sha256(raw_labels).hexdigest(),
            "run_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
    return summarize(results), results, meta


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--evalset", type=Path, default=Path("evalset"))
    ap.add_argument("--extractor", default="auto", choices=["auto", "model", "claude", "openai", "rules"])
    ap.add_argument("--cache", type=Path, default=None, help="directory for cached model answers")
    ap.add_argument("--ids", default=None, help="comma-separated notice ids")
    ap.add_argument("--out", type=Path, default=None, help="write the full JSON report here")
    ap.add_argument("--markdown", type=Path, default=None, help="write the summary as Markdown here")
    args = ap.parse_args(argv)
    summary, results, meta = asyncio.run(run(args.evalset, args.extractor, args.cache,
                                             set(args.ids.split(",")) if args.ids else None))
    text = render(summary, results, meta)
    print(text)
    if args.out:
        args.out.write_text(json.dumps({"meta": meta, "summary": summary, "notices": results}, indent=1, default=str))
    if args.markdown:
        args.markdown.write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
