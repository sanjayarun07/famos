"""Parsing runs where there is nothing worth stealing.

PyMuPDF, Pillow and Tesseract read bytes a stranger emailed us. Until this
existed they read them inside the API process, which holds the database pool
and unwrapped household keys. These tests are about the boundary, not about
parsing -- parse() has its own tests.
"""
import os

import pytest

from familyos.extraction import sandbox
from familyos.settings import settings
from tests.conftest import pdf

NOTICE = b"Sports day is on 14 November 2026. Return the slip by Friday."


async def test_a_notice_is_parsed_in_a_child_process(database):
    doc = await sandbox.parse_isolated(NOTICE, "text/plain")
    assert doc.char_count == len(NOTICE)
    assert "Sports day" in doc.pages[0].text
    assert doc.parser_version


async def test_the_child_is_handed_no_familyos_environment(database):
    """The whole point. Code execution in a parser must not find the master
    key, the database URL, the object store credentials or the inbound
    secret."""
    assert os.environ.get("FAMILYOS_MASTER_KEY"), "the test suite sets one"
    env = sandbox.child_env()
    assert not [k for k in env if k.startswith("FAMILYOS")]
    assert "no_proxy" in env
    # And nothing that happens to hold a secret under another name.
    for leaky in ("AWS_SECRET_ACCESS_KEY", "DATABASE_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        assert leaky not in env


async def test_a_parse_that_hangs_is_killed_and_fails_closed(database, monkeypatch):
    """A timeout must raise, never return an empty document. An empty document
    reads as "this notice says nothing", which is the failure nobody notices."""
    monkeypatch.setattr(settings, "parse_timeout_seconds", 0.05)
    with pytest.raises(sandbox.ParseFailed) as caught:
        await sandbox.parse_isolated(NOTICE, "text/plain")
    assert "longer than" in str(caught.value)


async def test_a_child_that_dies_fails_closed(database, monkeypatch):
    """A segfault in a parser, or the CPU limit firing, is the interesting
    case. It must not be mistaken for an unreadable notice."""
    monkeypatch.setattr(settings, "sandbox_cmd", "/usr/bin/env false")
    with pytest.raises(sandbox.ParseFailed) as caught:
        await sandbox.parse_isolated(NOTICE, "text/plain")
    assert "exited" in str(caught.value)


async def test_an_oversized_answer_is_refused(database, monkeypatch):
    monkeypatch.setattr(settings, "parse_max_output_bytes", 10)
    with pytest.raises(sandbox.ParseFailed) as caught:
        await sandbox.parse_isolated(NOTICE, "text/plain")
    assert "over the" in str(caught.value)


async def test_unreadable_output_fails_closed(database, monkeypatch):
    """Whatever the wrapper is, if what comes back is not a document we do not
    guess at one."""
    monkeypatch.setattr(settings, "sandbox_cmd", "/usr/bin/env echo not-json")
    with pytest.raises(sandbox.ParseFailed):
        await sandbox.parse_isolated(NOTICE, "text/plain")


async def test_the_child_cannot_write_a_file(database):
    """RLIMIT_FSIZE is zero, so a compromised parser cannot drop a payload on
    the host or spill the document to disk."""
    import resource
    import subprocess
    out = subprocess.run(
        [sandbox._command("text/plain")[0], "-I", "-c",
         "import resource, familyos.extraction.sandbox as s; s._limits(256, 10); "
         "print(resource.getrlimit(resource.RLIMIT_FSIZE)[0], resource.getrlimit(resource.RLIMIT_CORE)[0])"],
        capture_output=True, text=True, env={**sandbox.child_env(),
                                             "PYTHONPATH": os.getcwd()}, timeout=60)
    assert out.returncode == 0, out.stderr
    fsize, core = out.stdout.split()
    assert fsize == "0", "the child must not be able to write a file"
    assert core == "0", "a core dump would contain the document"
    assert resource.RLIMIT_FSIZE is not None


async def test_the_boundary_can_be_turned_off_but_says_so(database, monkeypatch):
    """A development escape hatch that announces itself at boot, because an
    unsandboxed parser is the most exposed surface in the system."""
    monkeypatch.setattr(settings, "parse_sandbox", False)
    doc = await sandbox.parse_isolated(NOTICE, "text/plain")
    assert "Sports day" in doc.pages[0].text
    assert "NOT sandboxed" in sandbox.describe()

    monkeypatch.setattr(settings, "parse_sandbox", True)
    assert "sandboxed in a child process" in sandbox.describe()


async def test_a_wrapper_can_be_put_in_front_without_the_caller_knowing(database, monkeypatch):
    """The seam for bubblewrap or a container runtime. /usr/bin/env is a
    harmless stand-in that proves the wrapper is actually used."""
    monkeypatch.setattr(settings, "sandbox_cmd", "/usr/bin/env")
    doc = await sandbox.parse_isolated(NOTICE, "text/plain")
    assert "Sports day" in doc.pages[0].text
    assert sandbox._command("text/plain")[0] == "/usr/bin/env"


async def test_a_real_pdf_round_trips_through_the_boundary(database):
    """Pages, words and boxes survive being serialised out of the child --
    grounding draws its boxes from those words."""
    doc = await sandbox.parse_isolated(pdf("x"), "application/pdf")
    assert doc.media_type == "application/pdf"
    # A minimal PDF has no text; what matters is that the shape came back.
    assert isinstance(doc.pages, list)
    assert isinstance(doc.notes, list)


async def test_extraction_fails_visibly_when_parsing_cannot_be_trusted(family, database, monkeypatch):
    """The integration that matters. If the boundary refuses, the notice is
    left unread and the job says so. It must never be recorded as a notice
    that asked nothing, because that reads to a family as "nothing here for
    you"."""
    from familyos import jobs

    async def refuses(*args, **kwargs):
        raise sandbox.ParseFailed("the parse sandbox exited -11: no output")

    monkeypatch.setattr(sandbox, "parse_isolated", refuses)
    artifact_id = (await family.upload("amma", pdf("sand"))).json()["artifact"]["id"]
    job_id = await database.fetchval(
        "SELECT id::text FROM jobs WHERE kind = 'extract_artifact' AND spec->>'artifact_id' = $1",
        artifact_id)
    assert job_id, "the upload should have queued a reading"

    row = await jobs.attach(job_id)
    assert row["status"] != "succeeded"
    assert "sandbox" in (row.get("error") or "")
    # Nothing recorded, so the notice is unread rather than read as empty.
    assert await database.fetchval("SELECT count(*) FROM extractions") == 0
    assert await database.fetchval("SELECT count(*) FROM claims") == 0
