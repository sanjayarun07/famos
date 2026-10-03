"""Taking everything with you.

Erasure answers "destroy what you hold about us". This answers the other half:
"give us what you hold about us, in a form we can read and keep". GDPR calls
them Articles 17 and 20; a family calls them leaving.

Two things make this more than a dump.

**It is generated and streamed, never stored.** A saved export would be a
second copy of the household sitting in the object store, unsealed, which the
erasure path would then have to find and chase. Everything here is built in
memory for one request and handed over. Nothing new is at rest afterwards, so
nothing new has to die with the key.

**It is written twice.** `data/*.json` is the structured, machine-readable
form the law asks for and another tool can import. The markdown beside it is
for the family: one page per notice, saying what was read out of it, in what
words, with what confidence, and what anyone decided. That is the same
provenance the console draws, in a form that survives having no console --
and it is readable and correctable by the person it is about, which is the
point of giving it to them at all.

Scope is the visibility rule, as everywhere else: a member exports the notices
they can see, so one adult's private mail does not leave in another's export.
A child's export is different -- children do not sign in, so a guardian asks
for it, and it holds what is held *about* that child rather than what they
could see.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import uuid
import zipfile

from familyos import artifacts, audit
from familyos.db import pool
from familyos.identity import NotAllowed, NotFound, Principal
from familyos.settings import settings

# ----------------------------------------------------------------------------

_VISIBLE = f"SELECT a.* FROM input_artifacts a WHERE {artifacts.VISIBLE_TO_MEMBER}"


def _slug(text: str | None, fallback: str = "notice") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (slug[:60] or fallback)


def _iso(value):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _plain(row) -> dict:
    """A row as JSON can hold it. Bytes never travel in the JSON: the
    originals are files in the zip."""
    return {k: _iso(v) for k, v in dict(row).items() if not isinstance(v, (bytes, bytearray, memoryview))}


def _ext(media_type: str | None, filename: str | None) -> str:
    if filename and "." in filename:
        return "." + filename.rsplit(".", 1)[1][:8]
    return {"application/pdf": ".pdf", "message/rfc822": ".eml", "text/plain": ".txt",
            "image/jpeg": ".jpg", "image/png": ".png", "image/heic": ".heic",
            "image/webp": ".webp"}.get(media_type or "", ".bin")


# ----------------------------------------------------------------------------
# the pages a person reads
# ----------------------------------------------------------------------------

def _readme(what: str, household: dict, when: dt.datetime, counts: dict) -> str:
    return f"""# Your FamilyOS data

{what}

Taken from **{household['name']}** on {when.date().isoformat()}.

- `family.md` -- the household and who is in it.
- `notices/` -- one folder per notice: the original exactly as it arrived, and
  `notice.md` saying what was read out of it.
- `obligations.md` -- everything a notice asked of the family, and what was
  decided.
- `reminders.md` -- what the family was told, and when.
- `audit.md` -- the record of what happened to all of it.
- `data/` -- the same contents as JSON, so another tool can read them.

## How to read a notice page

Every fact FamilyOS recorded carries the words it came from, the page they
were on, and how sure the reader was. Nothing was ever recorded that could not
be pointed back at a line in the notice -- so if a date here is wrong, the
quote beside it tells you whether the notice said something different or
whether it was read wrongly.

Confidence is not a promise. A fact marked uncertain, or read out of a
photograph, or relayed second-hand through someone else, carries less weight,
and the page says which.

## What is not here

Nothing that was never stored: FamilyOS keeps no copy of your mailbox, and a
notice that arrived and was refused was not kept to be exported.

Contains {counts['notices']} notices, {counts['claims']} recorded facts and
{counts['obligations']} things asked of the family.
"""


def _family_md(household: dict, members: list[dict], consents: list[dict]) -> str:
    out = [f"# {household['name']}", "",
           f"Started {_iso(household['created_at'])[:10]}.", "", "## Who is in it", ""]
    for m in members:
        bits = [f"**{m['display_name']}** -- {m['role']}"]
        if m.get("email"):
            bits.append(m["email"])
        if m.get("date_of_birth"):
            bits.append("born " + _iso(m["date_of_birth"])[:10])
        out.append("- " + ", ".join(bits))
    if consents:
        out += ["", "## Consent recorded for a child", "",
                "A child's data is only ever recorded while a guardian's consent stands.", ""]
        for c in consents:
            state = "withdrawn " + _iso(c["withdrawn_at"])[:10] if c.get("withdrawn_at") else "active"
            out.append(f"- {c['subject_name']} -- {c['purpose']}, {state} "
                       f"(given {_iso(c['granted_at'])[:10]}, notice {c['notice_version']})")
    return "\n".join(out) + "\n"


def _notice_md(artifact: dict, claims: list[dict], obligations: list[dict], original: str | None) -> str:
    source = artifact.get("source") or {}
    out = [f"# {artifact.get('filename') or source.get('subject') or 'Notice'}", ""]
    out.append(f"Arrived {_iso(artifact['received_at'])[:16].replace('T', ' ')} "
               f"by {artifact['channel'].replace('_', ' ')}.")
    if source.get("from"):
        out.append(f"From {source['from']}.")
    if source.get("forwarded") or source.get("is_group"):
        out.append("Relayed by somebody rather than sent by the school, so what it says was "
                   "weighed as second-hand.")
    out.append(f"Visible to: {'everyone in the household' if artifact['visibility'] == 'shared' else 'only its sender'}.")
    if original:
        out += ["", f"The original is beside this page, as `{original}`."]

    if not claims:
        out += ["", "Nothing was read out of this notice."]
    else:
        out += ["", "## What was read out of it", ""]
        for c in claims:
            when = _iso(c["date"])[:10] if c.get("date") else (c.get("date_text") or "no date given")
            out.append(f"### {c['title']}")
            out.append("")
            out.append(f"- **{c['kind']}**, {when}"
                       + (f" to {_iso(c['end_date'])[:10]}" if c.get("end_date") else "")
                       + (f", {c['time_text']}" if c.get("time_text") else ""))
            if c.get("place"):
                out.append(f"- Where: {c['place']}")
            if c.get("amount") is not None:
                out.append(f"- Amount: {c.get('currency') or ''} {c['amount']}".strip()
                           + (f" ({c['amount_text']})" if c.get("amount_text") else ""))
            if c.get("applies_to"):
                out.append(f"- Applies to: {c['applies_to']}")
            if c.get("subject_name"):
                out.append(f"- About: {c['subject_name']}")
            if c.get("requires"):
                out.append(f"- Asks for: {', '.join(c['requires'])}")
            if c.get("optional"):
                out.append("- The notice made this optional.")
            if c.get("uncertain"):
                out.append("- Recorded as uncertain: the notice was not definite about this.")
            out.append(f'- Read from, on page {c.get("page") or 1}: "{(c.get("quote") or "").strip()}"')
            out.append(f"- Confidence {c['confidence']:.2f}"
                       + (f", matched to the notice {c['match']}ly" if c.get("match") else
                          ", **not matched to any words in the notice**"))
            out.append("")

    if obligations:
        out += ["## What it asked of the family", ""]
        for o in obligations:
            state = o["status"]
            if o.get("superseded_at"):
                state += ", later superseded"
            due = _iso(o["due_date"])[:10] if o.get("due_date") else "no date"
            out.append(f"- {o['title']} -- {o['action']} by {due} ({state})")
        out.append("")
    return "\n".join(out) + "\n"


def _obligations_md(rows: list[dict], names: dict) -> str:
    out = ["# What notices asked of the family", "",
           "| What | Do | By | Status | Decided by | About |", "|---|---|---|---|---|---|"]
    for o in rows:
        decided = names.get(o.get("decided_by"), "")
        about = names.get(o.get("subject_member_id"), "")
        state = o["status"] + (" (superseded)" if o.get("superseded_at") else "")
        out.append(f"| {o['title']} | {o['action']} | {_iso(o['due_date'])[:10] if o.get('due_date') else '--'} "
                   f"| {state} | {decided} | {about} |")
    return "\n".join(out) + "\n"


def _reminders_md(rows: list[dict], names: dict) -> str:
    if not rows:
        return "# What the family was told\n\nNothing has been sent yet.\n"
    out = ["# What the family was told", "",
           "| What | Who | Why | When | Status |", "|---|---|---|---|---|"]
    for r in rows:
        why = "still undecided" if r["reason"] == "undecided" else "coming up"
        out.append(f"| {r['title']} | {names.get(r['member_id'], '')} | {why} "
                   f"| {_iso(r.get('sent_at') or r['send_after'])[:16].replace('T', ' ')} | {r['status']} |")
    return "\n".join(out) + "\n"


def _audit_md(rows: list[dict], names: dict) -> str:
    out = ["# The record", "",
           "Every change to the household, in order, newest first. The audit trail never holds "
           "a notice's words or a filename -- only what happened, to which record, and who asked.",
           "", "| When | Who | What | Record |", "|---|---|---|---|"]
    for e in rows:
        who = names.get(e.get("actor_member_id")) or e["actor_kind"]
        out.append(f"| {_iso(e['at'])[:19].replace('T', ' ')} | {who} | {e['action']} "
                   f"| {e.get('target_type') or ''} |")
    return "\n".join(out) + "\n"


def _audit_csv(rows: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["at", "actor_kind", "actor_member_id", "action", "target_type", "target_id", "detail"])
    for e in rows:
        writer.writerow([_iso(e["at"]), e["actor_kind"], _iso(e.get("actor_member_id")), e["action"],
                         e.get("target_type"), _iso(e.get("target_id")), json.dumps(e.get("detail") or {})])
    return buffer.getvalue()


# ----------------------------------------------------------------------------
# building it
# ----------------------------------------------------------------------------

async def _household_and_members(conn, household_id: uuid.UUID) -> tuple[dict, list[dict], dict]:
    household = await conn.fetchrow(
        "SELECT id, name, status, created_at FROM households WHERE id = $1", household_id)
    if household is None:
        raise NotFound("household")
    members = await conn.fetch(
        "SELECT id, display_name, role, email, phone, date_of_birth, created_at FROM members "
        "WHERE household_id = $1 ORDER BY created_at", household_id)
    names = {m["id"]: m["display_name"] for m in members}
    return dict(household), [dict(m) for m in members], names


def _write(zf: zipfile.ZipFile, path: str, text: str) -> None:
    zf.writestr(path, text)


def _write_json(zf: zipfile.ZipFile, path: str, payload) -> None:
    zf.writestr(path, json.dumps(payload, indent=2, ensure_ascii=False, default=_iso))


def _build(*, what: str, household: dict, members: list[dict], consents: list[dict],
           notices: list[dict], claims_by: dict, obligations: list[dict], reminders: list[dict],
           events: list[dict], originals: dict, names: dict, when: dt.datetime) -> tuple[bytes, dict]:
    counts = {"notices": len(notices), "claims": sum(len(v) for v in claims_by.values()),
              "obligations": len(obligations), "reminders": len(reminders), "events": len(events),
              "originals": len(originals)}
    buffer = io.BytesIO()
    obligations_by: dict[uuid.UUID, list[dict]] = {}
    for o in obligations:
        obligations_by.setdefault(o["artifact_id"], []).append(o)

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        _write(zf, "README.md", _readme(what, household, when, counts))
        _write(zf, "family.md", _family_md(household, members, consents))

        for i, artifact in enumerate(notices, start=1):
            source = artifact.get("source") or {}
            stem = _slug(artifact.get("filename") or source.get("subject") or artifact["channel"])
            folder = f"notices/{i:04d}-{_iso(artifact['received_at'])[:10]}-{stem}"
            data = originals.get(artifact["id"])
            name = None
            if data is not None:
                name = "original" + _ext(artifact.get("media_type"), artifact.get("filename"))
                zf.writestr(f"{folder}/{name}", data)
            _write(zf, f"{folder}/notice.md",
                   _notice_md(artifact, claims_by.get(artifact["id"], []),
                              obligations_by.get(artifact["id"], []), name))

        _write(zf, "obligations.md", _obligations_md(obligations, names))
        _write(zf, "reminders.md", _reminders_md(reminders, names))
        _write(zf, "audit.md", _audit_md(events, names))

        # The structured copy: what another tool reads, and what the law means
        # by a commonly used machine-readable format.
        _write_json(zf, "data/household.json", {
            "household": _plain(household), "members": [_plain(m) for m in members],
            "consents": [_plain(c) for c in consents], "exported_at": when,
            "export_scope": what})
        _write_json(zf, "data/notices.json", [_plain(a) for a in notices])
        _write_json(zf, "data/claims.json",
                    [_plain(c) for rows in claims_by.values() for c in rows])
        _write_json(zf, "data/obligations.json", [_plain(o) for o in obligations])
        _write_json(zf, "data/reminders.json", [_plain(r) for r in reminders])
        _write_json(zf, "data/audit.json", [_plain(e) for e in events])
        _write(zf, "data/audit.csv", _audit_csv(events))
    return buffer.getvalue(), counts


async def for_member(p: Principal) -> tuple[bytes, str, dict]:
    """Everything this member can see, plus the household they can see it in.

    The visibility rule decides the scope, so an export is not a way around
    it: another adult's private mail is no more exportable than it is
    readable.
    """
    when = dt.datetime.now(dt.UTC)
    async with pool().acquire() as conn:
        household, members, names = await _household_and_members(conn, p.household_id)
        notices = [dict(r) for r in await conn.fetch(
            _VISIBLE + " ORDER BY a.received_at", p.household_id, p.member_id)]
        ids = [a["id"] for a in notices]
        claims = [dict(r) for r in await conn.fetch(
            "SELECT c.* FROM claims c JOIN extractions e ON e.id = c.extraction_id "
            "WHERE c.artifact_id = ANY($1::uuid[]) AND e.superseded_at IS NULL "
            "ORDER BY c.artifact_id, c.ordinal", ids)]
        obligations = [dict(r) for r in await conn.fetch(
            "SELECT * FROM obligations WHERE artifact_id = ANY($1::uuid[]) "
            "ORDER BY due_date NULLS LAST, created_at", ids)]
        reminders = [dict(r) for r in await conn.fetch(
            "SELECT r.*, o.title FROM reminders r JOIN obligations o ON o.id = r.obligation_id "
            "WHERE r.member_id = $1 AND o.artifact_id = ANY($2::uuid[]) ORDER BY r.send_after",
            p.member_id, ids)]
        consents = [dict(r) for r in await conn.fetch(
            "SELECT c.*, m.display_name AS subject_name FROM consent_records c "
            "JOIN members m ON m.id = c.subject_member_id WHERE c.household_id = $1 ORDER BY c.granted_at",
            p.household_id)]
        # Guardians keep the household's record; another adult gets their own
        # actions, not everyone's.
        events = [dict(r) for r in await conn.fetch(
            "SELECT * FROM audit_events WHERE household_id = $1 "
            "AND ($2 OR actor_member_id = $3) ORDER BY at DESC LIMIT 20000",
            p.household_id, p.is_guardian, p.member_id)]

    claims_by: dict[uuid.UUID, list[dict]] = {}
    for c in claims:
        claims_by.setdefault(c["artifact_id"], []).append(c)
    originals = await artifacts.read_many(p.household_id, notices, actor=p,
                                          max_bytes=settings.max_export_bytes)
    data, counts = _build(
        what="Everything FamilyOS holds that you can see.", household=household, members=members,
        consents=consents, notices=notices, claims_by=claims_by, obligations=obligations,
        reminders=reminders, events=events, originals=originals, names=names, when=when)

    await audit.record(p.household_id, "export.created", actor_member_id=p.member_id,
                       target_type="member", target_id=p.member_id,
                       detail={"scope": "member", "bytes": len(data), **counts})
    return data, f"familyos-{_slug(household['name'], 'household')}-{when.date().isoformat()}.zip", counts


async def for_subject(p: Principal, member_id: uuid.UUID) -> tuple[bytes, str, dict]:
    """What is held *about* one child, asked for by a guardian.

    Children do not sign in, so there is no "what they can see" -- there is
    only what names them. This is the counterpart to subject erasure: the same
    scope, handed over instead of destroyed, so a guardian can see exactly
    what erasing would take away.
    """
    if not p.is_guardian:
        raise NotAllowed("only a guardian can export a member's data")
    when = dt.datetime.now(dt.UTC)
    async with pool().acquire() as conn:
        subject = await conn.fetchrow(
            "SELECT * FROM members WHERE id = $1 AND household_id = $2", member_id, p.household_id)
        if subject is None:
            raise NotFound("member")
        if subject["role"] != "child":
            raise NotAllowed("an adult exports their own data; this is for a child")
        household, members, names = await _household_and_members(conn, p.household_id)
        # Named as the subject of a notice, or named inside a claim read out of
        # one. Both are "about this child", and the second is the one a family
        # would not think to look for.
        notices = [dict(r) for r in await conn.fetch(
            "SELECT DISTINCT a.* FROM input_artifacts a WHERE a.household_id = $1 AND ("
            "  EXISTS (SELECT 1 FROM artifact_subjects s WHERE s.artifact_id = a.id AND s.member_id = $2)"
            "  OR EXISTS (SELECT 1 FROM claims c WHERE c.artifact_id = a.id AND c.subject_name IS NOT NULL"
            "             AND c.subject_name = $3)) ORDER BY a.received_at",
            p.household_id, member_id, subject["display_name"])]
        ids = [a["id"] for a in notices]
        claims = [dict(r) for r in await conn.fetch(
            "SELECT c.* FROM claims c JOIN extractions e ON e.id = c.extraction_id "
            "WHERE c.artifact_id = ANY($1::uuid[]) AND e.superseded_at IS NULL "
            "ORDER BY c.artifact_id, c.ordinal", ids)]
        obligations = [dict(r) for r in await conn.fetch(
            "SELECT * FROM obligations WHERE subject_member_id = $1 OR artifact_id = ANY($2::uuid[]) "
            "ORDER BY due_date NULLS LAST, created_at", member_id, ids)]
        consents = [dict(r) for r in await conn.fetch(
            "SELECT c.*, m.display_name AS subject_name FROM consent_records c "
            "JOIN members m ON m.id = c.subject_member_id WHERE c.subject_member_id = $1 ORDER BY c.granted_at",
            member_id)]
        events = [dict(r) for r in await conn.fetch(
            "SELECT * FROM audit_events WHERE household_id = $1 AND (target_id = $2 OR target_id = ANY($3::uuid[])) "
            "ORDER BY at DESC LIMIT 20000", p.household_id, member_id, ids)]

    claims_by: dict[uuid.UUID, list[dict]] = {}
    for c in claims:
        claims_by.setdefault(c["artifact_id"], []).append(c)
    originals = await artifacts.read_many(p.household_id, notices, actor=p,
                                          max_bytes=settings.max_export_bytes)
    data, counts = _build(
        what=f"Everything FamilyOS holds about {subject['display_name']}.",
        household=household, members=[dict(subject)], consents=consents, notices=notices,
        claims_by=claims_by, obligations=obligations, reminders=[], events=events,
        originals=originals, names=names, when=when)

    await audit.record(p.household_id, "export.created", actor_member_id=p.member_id,
                       target_type="member", target_id=member_id,
                       detail={"scope": "subject", "bytes": len(data), **counts})
    return data, f"familyos-{_slug(subject['display_name'], 'child')}-{when.date().isoformat()}.zip", counts


__all__ = ["for_member", "for_subject"]
