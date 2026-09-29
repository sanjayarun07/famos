"""Extraction: parsing, grounding, claims, proposed obligations, the job, and the scorer."""
import datetime as dt
import io
import json
import shutil
import uuid
from types import SimpleNamespace

import pymupdf
import pytest

from familyos import jobs
from familyos.extraction import evaluate, pipeline
from familyos.extraction.claims import Claim
from familyos.extraction.dates import find_dates
from familyos.extraction.ground import locate
from familyos.extraction.llm import OUTPUT_SCHEMA, PROMPT_VERSION, ClaudeExtractor, build_request
from familyos.extraction.obligations import propose
from familyos.extraction.parse import parse
from familyos.extraction.rules import RulesExtractor
from familyos.settings import settings

Y = dt.date.today().year + 5     # far enough ahead that nothing is in the past
TRIP = [
    "Circular: Field Trip, Classes I & II",
    f"Grade I and II students are going on an educational trip to Fort Warangal on Saturday, 29th October {Y}.",
    f"Kindly submit the duly filled consent form to the class teacher by Wednesday, 26th October {Y}.",
    "Participants contribute Rs. 150/- towards transport.",
    "Parent signature: ____________________",
]


def make_pdf(lines: list[str]) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for line in lines:
        page.insert_text((50, y), line, fontsize=9)
        y += 20
    return doc.tobytes()


# ----------------------------------------------------------------------------
# pure parts
# ----------------------------------------------------------------------------

def test_dates_as_notices_write_them():
    ref = dt.date(2026, 2, 25)
    found = find_dates("Session Break: 1st March to 8th March 2026. Consent by Saturday, 28th February.", ref)
    assert [f.date for f in found] == [dt.date(2026, 3, 1), dt.date(2026, 3, 8), dt.date(2026, 2, 28)]
    assert [f.date for f in find_dates("Date: 25/02/2026", ref)] == [dt.date(2026, 2, 25)]
    # US notices write month first.
    assert [f.date for f in find_dates("depart 5/29, back 5/31", dt.date(2019, 4, 1), us_numeric=True)] == \
        [dt.date(2019, 5, 29), dt.date(2019, 5, 31)]


def test_pdf_words_keep_their_place_and_quotes_are_grounded():
    doc = parse(make_pdf(TRIP), "application/pdf")
    assert len(doc.pages) == 1 and doc.pages[0].source == "text"
    exact = locate(doc, f"by Wednesday, 26th October {Y}")
    assert exact.match == "exact" and exact.page == 1
    assert doc.pages[0].text[exact.start:exact.end] == f"by Wednesday, 26th October {Y}"
    x0, y0, x1, y1 = exact.boxes[0]
    assert 50 <= x0 < x1 and 72 + 40 - 12 < y1 < 72 + 40 + 5      # on the third line
    assert locate(doc, f"BY  wednesday,\n26th October {Y}").match == "normalized"
    assert locate(doc, "submit the duly filed consent form to the class teacher").match == "fuzzy"
    assert locate(doc, "a sentence the notice never says anywhere") is None


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")
def test_a_photo_is_read_with_ocr():
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1400, 200), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=40)
    draw.text((20, 60), "Annual Day: Saturday, 28th March 2026", fill="black", font=font)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    doc = parse(buf.getvalue(), "image/png")
    assert doc.pages[0].source == "ocr" and "28th March 2026" in doc.pages[0].text
    loc = locate(doc, "Saturday, 28th March 2026")
    assert loc is not None and loc.boxes and loc.boxes[0][0] > 100


def test_obligations_come_only_from_grounded_claims():
    doc = parse(make_pdf(TRIP), "application/pdf")
    claims = [
        Claim(kind="event", title="Trip to Fort Warangal", date=dt.date(Y, 10, 29),
              quote=f"educational trip to Fort Warangal on Saturday, 29th October {Y}."),
        Claim(kind="deadline", title="Return consent form", date=dt.date(Y, 10, 26), requires=["form_return"],
              quote=f"Kindly submit the duly filled consent form to the class teacher by Wednesday, 26th October {Y}."),
        Claim(kind="form", title="Consent form", requires=["parent_signature"], quote="Parent signature:"),
        Claim(kind="payment", title="Invented fee", quote="Please pay Rs 9999 immediately", confidence=0.8),
    ]
    x = pipeline.finish(doc, claims, pipeline.info(RulesExtractor(), doc), dt.date(Y, 10, 20))
    assert [c.grounded for c in x.claims] == [True, True, True, False]
    assert x.claims[3].confidence == 0.4
    assert x.actionable
    proposed = propose(x.claims, dt.date(Y, 10, 20))
    # The form is covered by the deadline; the ungrounded payment proposes nothing.
    assert [(o.kind, o.action, o.due_date) for o in proposed] == [
        ("calendar", "note", dt.date(Y, 10, 29)), ("task", "sign", dt.date(Y, 10, 26))]
    # Read long after, nothing is proposed for days already past.
    assert propose(x.claims, dt.date(Y + 1, 1, 1)) == []


# ----------------------------------------------------------------------------
# the model-based extractor, with a stand-in for the API
# ----------------------------------------------------------------------------

class FakeClient:
    def __init__(self, answer: dict):
        self.answer = answer
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **request):
        self.calls.append(request)
        answer = self.answer

        class Stream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get_final_message(self):
                return SimpleNamespace(stop_reason="end_turn", model=request["model"],
                                       content=[SimpleNamespace(type="text", text=json.dumps(answer))],
                                       usage=SimpleNamespace(input_tokens=1000, output_tokens=200))
        return Stream()


def model_answer() -> dict:
    blank = {k: None for k in ("date", "end_date", "date_text", "time", "place", "amount", "applies_to", "subject_name",
                               "amends", "change")}
    return {"actionable": True, "non_actionable_reason": None, "claims": [
        {**blank, "kind": "event", "title": "Trip to Fort Warangal", "date": f"{Y}-10-29", "applies_to": "Grade I, II",
         "requires": [], "optional": False, "uncertain": False, "page": 1, "confidence": 0.95,
         "quote": f"going on an educational trip to Fort Warangal on Saturday, 29th October {Y}."},
        {**blank, "kind": "deadline", "title": "Return the signed consent form", "date": f"{Y}-10-26",
         "requires": ["form_return", "parent_signature"], "optional": False, "uncertain": False, "page": 1,
         "confidence": 0.9, "quote": f"Kindly submit the duly filled consent form to the class teacher by Wednesday, 26th October {Y}."},
        {**blank, "kind": "payment", "title": "Transport contribution", "amount": {"value": 150, "currency": "INR",
         "text": "Rs. 150/-"}, "requires": ["payment"], "optional": False, "uncertain": False, "page": 1,
         "confidence": 0.9, "quote": "Participants contribute Rs. 150/- towards transport."},
    ]}


def test_the_request_is_versioned_and_schema_constrained():
    doc = parse(make_pdf(TRIP), "application/pdf")
    request = build_request(doc, dt.date(Y, 10, 20), model="claude-opus-5-5", effort="medium")
    assert request["model"] == "claude-opus-5-5"
    assert request["output_config"]["format"] == {"type": "json_schema", "schema": OUTPUT_SCHEMA}
    assert f"{Y}-10-20" in request["system"] and '<page number="1">' in request["messages"][0]["content"]
    assert "Fort Warangal" in request["messages"][0]["content"]


async def test_claude_answers_are_grounded():
    doc = parse(make_pdf(TRIP), "application/pdf")
    client = FakeClient(model_answer())
    extractor = ClaudeExtractor(api_key=None, model="claude-opus-5-5", client=client)
    x = await pipeline.extract(doc, dt.date(Y, 10, 20), extractor)
    assert x.extractor.name == "claude" and x.extractor.prompt_version == PROMPT_VERSION
    assert x.extractor.model == "claude-opus-5-5"
    assert all(c.grounded for c in x.claims)
    assert x.claims[2].amount.value == 150 and x.claims[2].amount.currency == "INR"
    assert client.calls[0]["fallbacks"] == "default"


# ----------------------------------------------------------------------------
# the job after intake
# ----------------------------------------------------------------------------

async def _run_extraction(database, artifact_id):
    job_id = await database.fetchval("SELECT id::text FROM jobs WHERE kind = 'extract_artifact' AND spec->>'artifact_id' = $1 "
                                     "ORDER BY created_at DESC LIMIT 1", artifact_id)
    assert job_id, "no extraction job was queued"
    return await jobs.attach(job_id)


async def test_an_upload_is_read_and_proposes_tasks(family, database, monkeypatch):
    monkeypatch.setattr(settings, "extractor", "rules")
    r = await family.upload("amma", make_pdf(TRIP), visibility="shared")
    artifact_id = r.json()["artifact"]["id"]
    pending = (await family.client.get(f"/v1/artifacts/{artifact_id}/extraction", headers=family.h("amma"))).json()
    assert pending["extraction"] is None and pending["job_status"] == "queued"

    done = await _run_extraction(database, artifact_id)
    assert done["status"] == "succeeded", done
    body = (await family.client.get(f"/v1/artifacts/{artifact_id}/extraction", headers=family.h("appa"))).json()
    x = body["extraction"]
    assert x["extractor"] == "rules" and x["actionable"] and x["page_count"] == 1
    deadline = next(c for c in x["claims"] if c["kind"] == "deadline")
    assert deadline["date"] == f"{Y}-10-26" and deadline["location"]["match"] == "exact" and deadline["location"]["boxes"]

    tasks = (await family.client.get("/v1/obligations?status=", headers=family.h("appa"))).json()
    assert tasks, "nothing was proposed"
    assert all(t["status"] == "proposed" for t in tasks)
    r = await family.client.post(f"/v1/obligations/{tasks[0]['id']}/decision", headers=family.h("appa"),
                                 json={"status": "accepted"})
    assert r.status_code == 200 and r.json()["status"] == "accepted" and r.json()["decided_by"] == family.appa["id"]
    actions = [e["action"] for e in (await family.client.get("/v1/audit", headers=family.h("amma"))).json()]
    assert "extraction.completed" in actions and "obligation.accepted" in actions

    # Re-reading keeps the decision and replaces only what is still proposed.
    r = await family.client.post(f"/v1/artifacts/{artifact_id}/extract", headers=family.h("amma"))
    assert r.status_code == 202
    await _run_extraction(database, artifact_id)
    again = (await family.client.get("/v1/obligations?status=", headers=family.h("appa"))).json()
    assert sum(t["status"] == "accepted" for t in again) == 1
    assert len(again) == len(tasks)
    assert await database.fetchval("SELECT count(*) FROM extractions WHERE artifact_id = $1 AND superseded_at IS NULL",
                                   uuid.UUID(artifact_id)) == 1


async def test_private_notices_propose_tasks_only_to_their_sender(family, database, monkeypatch):
    monkeypatch.setattr(settings, "extractor", "rules")
    artifact_id = (await family.upload("amma", make_pdf(TRIP))).json()["artifact"]["id"]
    await _run_extraction(database, artifact_id)
    assert (await family.client.get("/v1/obligations", headers=family.h("amma"))).json()
    assert (await family.client.get("/v1/obligations", headers=family.h("appa"))).json() == []
    assert (await family.client.get(f"/v1/artifacts/{artifact_id}/extraction", headers=family.h("appa"))).status_code == 404
    task = (await family.client.get("/v1/obligations", headers=family.h("amma"))).json()[0]
    r = await family.client.post(f"/v1/obligations/{task['id']}/decision", headers=family.h("appa"), json={"status": "dismissed"})
    assert r.status_code == 404


async def test_a_resumed_job_does_not_pay_for_the_model_twice_and_keeps_no_copy(family, database, monkeypatch):
    client = FakeClient(model_answer())
    extractor = ClaudeExtractor(api_key=None, model="claude-opus-5-5", client=client)
    monkeypatch.setattr(pipeline, "make_extractor", lambda name=None: extractor)
    from familyos.extraction import service

    real_save = service.save
    crashed = []

    async def save_once_crashing(*args, **kwargs):
        if not crashed:
            crashed.append(True)
            raise RuntimeError("database went away")
        return await real_save(*args, **kwargs)

    monkeypatch.setattr(service, "save", save_once_crashing)
    artifact_id = (await family.upload("amma", make_pdf(TRIP), visibility="shared")).json()["artifact"]["id"]
    first = await _run_extraction(database, artifact_id)
    assert first["status"] == "queued" and "database went away" in first["error"]
    assert len(first["operations"]) == 1          # the answer is cached while the job is unfinished
    second = await _run_extraction(database, artifact_id)
    assert second["status"] == "succeeded", second
    assert len(client.calls) == 1
    assert second["operations"] == {}             # and gone once the claims are stored
    x = (await family.client.get(f"/v1/artifacts/{artifact_id}/extraction", headers=family.h("amma"))).json()["extraction"]
    assert x["extractor"] == "claude" and x["model"] == "claude-opus-5-5" and len(x["claims"]) == 3
    pay = next(c for c in x["claims"] if c["kind"] == "payment")
    assert pay["amount"] == 150 and pay["currency"] == "INR"
    tasks = (await family.client.get("/v1/obligations", headers=family.h("amma"))).json()
    assert sorted((t["kind"], t["action"]) for t in tasks) == [("calendar", "note"), ("task", "pay"), ("task", "sign")]


async def test_erasing_a_child_erases_what_was_read_about_them(family, database, monkeypatch):
    monkeypatch.setattr(settings, "extractor", "rules")
    consent = await family.consent(family.older)
    r = await family.upload("amma", make_pdf(TRIP), visibility="shared", subjects=[family.older])
    artifact_id = r.json()["artifact"]["id"]
    await _run_extraction(database, artifact_id)
    tasks = (await family.client.get("/v1/obligations", headers=family.h("amma"))).json()
    assert tasks and all(t["subject_member_id"] == family.older["id"] for t in tasks)

    r = await family.client.post(f"/v1/consents/{consent['id']}/withdraw", headers=family.h("amma"))
    assert r.status_code == 202, r.text
    job_id = await database.fetchval("SELECT job_id::text FROM erasure_log WHERE id = $1", uuid.UUID(r.json()["id"]))
    assert (await jobs.attach(job_id))["status"] == "succeeded"
    for table in ("extractions", "claims", "obligations"):
        assert await database.fetchval(f"SELECT count(*) FROM {table}") == 0, table
    assert await database.fetchval("SELECT count(*) FROM jobs WHERE kind = 'extract_artifact'") == 0


async def test_quarantined_mail_is_not_read_until_a_guardian_accepts_it(family, database, monkeypatch):
    monkeypatch.setattr(settings, "extractor", "rules")
    from tests.test_email import message, post

    r = await post(family.client, message("stranger@school.example", family.inbound,
                                          attachments=(("trip.pdf", make_pdf(TRIP)),)))
    assert r.status_code == 202
    assert await database.fetchval("SELECT count(*) FROM jobs WHERE kind = 'extract_artifact'") == 0
    quarantined = (await family.client.get("/v1/quarantine", headers=family.h("amma"))).json()[0]
    r = await family.client.post(f"/v1/quarantine/{quarantined['id']}/accept", headers=family.h("amma"),
                                 json={"member_id": family.amma["id"], "visibility": "shared"})
    assert r.status_code == 200, r.text
    # The email body and its attachment are each read.
    assert await database.fetchval("SELECT count(*) FROM jobs WHERE kind = 'extract_artifact'") == 2
    attachment_id = str(quarantined["children"][0])
    assert (await _run_extraction(database, attachment_id))["status"] == "succeeded"
    x = (await family.client.get(f"/v1/artifacts/{attachment_id}/extraction", headers=family.h("appa"))).json()["extraction"]
    assert any(c["kind"] == "deadline" for c in x["claims"])


# ----------------------------------------------------------------------------
# the scorer
# ----------------------------------------------------------------------------

async def test_the_scorer_matches_claims_to_labels(tmp_path):
    (tmp_path / "originals").mkdir()
    (tmp_path / "originals" / "t-1.pdf").write_bytes(make_pdf(TRIP))
    row = {"id": "t-1", "issued": f"{Y}-10-20", "original_path": "originals/t-1.pdf", "expect_no": ["event date"],
           "expected": [
               {"kind": "event", "title": "Educational trip to Fort Warangal", "date": f"{Y}-10-29"},
               {"kind": "deadline", "title": "Return signed consent form", "due": f"{Y}-10-26", "form_required": True},
               {"kind": "event", "title": "Sports day", "date": f"{Y}-12-01"},
               {"kind": "instruction", "text": "Wear uniform"}]}
    (tmp_path / "notices.jsonl").write_text(json.dumps(row) + "\n")
    summary, results, meta = await evaluate.run(tmp_path, "rules", None, None)
    r = results[0]
    assert r["expected_scored"] == 3 and r["detection"]["event"] == [1, 2] and r["detection"]["deadline"] == [1, 1]
    assert r["missed"] == ["Sports day"] and r["expected_unscored_kinds"] == ["instruction"]
    assert r["expect_no_violations"] == ["event date"]
    assert summary["recall"]["all"][:2] == [2, 3] and summary["fields"]["form"] == [1, 1, 1.0]
    assert meta["extractor"] == "rules" and "Facts found" in evaluate.render(summary, results, meta)
