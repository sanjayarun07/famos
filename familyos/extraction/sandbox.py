"""Parsing, in a process that holds nothing worth stealing.

`parse()` runs PyMuPDF, Pillow and Tesseract over bytes a stranger sent us.
Those are large C libraries with a long history of memory-safety bugs, and the
bytes arrive by email -- anyone who learns a household's inbound address can
post a crafted PDF. Until now that ran inside the API process, which holds the
database pool and, moment to moment, unwrapped household keys. `to_thread`
kept it off the event loop, which is latency isolation and not security
isolation: a thread shares the address space it was spawned from.

So parsing moves out. The child is this module run as `python -m`: bytes in on
stdin, JSON out on stdout, and an environment with **no FAMILYOS_ variables at
all**. Code execution in there cannot read the master key, cannot find the
database, and cannot write a file -- the three things that would turn a parser
bug into a breach.

What this stops, and what it does not
-------------------------------------
Stops: reading our secrets, reaching Postgres or the object store, writing to
disk, dumping core (a core file would contain the document), running away with
memory or CPU, and hanging the worker.

Does not stop: a kernel escape, or outbound network from the child. Neither is
reachable through the three parsers today, and both want a stronger boundary
than a process -- `bubblewrap --unshare-net` is the cheap next step and a
microVM the thorough one. `SANDBOX_CMD` exists so either can be put in front
of this without touching the caller.

Fails closed. A crash, a timeout, or output that will not parse means the
extraction fails and the notice is left unread, which is visible. The
alternative -- treating a crashed parser as an empty document -- would read as
"this notice says nothing", which is the failure we would never notice.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import resource
import sys

from familyos.extraction.parse import Page, ParsedDocument, Word, parse
from familyos.settings import settings

logger = logging.getLogger(__name__)

# The child is handed the media type on argv and the bytes on stdin, so no
# part of the document ever appears in a process listing.
MODULE = "familyos.extraction.sandbox"


class ParseFailed(RuntimeError):
    """The sandbox did not return a document. Never treated as an empty one."""


# ----------------------------------------------------------------------------
# the child
# ----------------------------------------------------------------------------

def _limits(memory_mb: int, cpu_seconds: int) -> None:
    """Applied in the child before it imports a parser or reads a byte."""
    soft = memory_mb * 1024 * 1024
    for what, limit in (
        (resource.RLIMIT_AS, soft),          # address space
        (resource.RLIMIT_CPU, cpu_seconds),  # SIGXCPU, then SIGKILL
        (resource.RLIMIT_FSIZE, 0),          # cannot write a file of any size
        (resource.RLIMIT_CORE, 0),           # a core dump would hold the document
        (resource.RLIMIT_NOFILE, 64),
    ):
        try:
            resource.setrlimit(what, (limit, limit))
        except (ValueError, OSError):  # noqa: PERF203 - a refused limit is not fatal
            logger.warning("could not set rlimit %s in the parse sandbox", what)


def _as_json(doc: ParsedDocument) -> dict:
    return {
        "media_type": doc.media_type,
        "notes": list(doc.notes),
        "parser_version": doc.parser_version,
        "pages": [{"number": p.number, "text": p.text, "width": p.width, "height": p.height,
                   "source": p.source,
                   # A word is a character span into the page text plus an
                   # optional box, which is what grounding draws from.
                   "words": [[w.start, w.end, list(w.box) if w.box else None] for w in p.words]}
                  for p in doc.pages],
    }


def _from_json(raw: dict) -> ParsedDocument:
    pages = [Page(number=p["number"], text=p["text"], width=p.get("width"), height=p.get("height"),
                  source=p.get("source", "text"),
                  words=[Word(start=w[0], end=w[1], box=tuple(w[2]) if w[2] else None)
                         for w in p.get("words") or []])
             for p in raw.get("pages") or []]
    return ParsedDocument(media_type=raw["media_type"], pages=pages,
                          notes=list(raw.get("notes") or []),
                          parser_version=raw.get("parser_version", ""))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 3:
        print("usage: python -m familyos.extraction.sandbox <media_type> <memory_mb> <cpu_seconds>",
              file=sys.stderr)
        return 2
    media_type, memory_mb, cpu_seconds = argv[0], int(argv[1]), int(argv[2])
    _limits(memory_mb, cpu_seconds)
    data = sys.stdin.buffer.read()
    doc = parse(data, media_type)
    sys.stdout.write(json.dumps(_as_json(doc)))
    sys.stdout.flush()
    return 0


# ----------------------------------------------------------------------------
# the parent
# ----------------------------------------------------------------------------

def child_env() -> dict[str, str]:
    """What the child is allowed to know.

    Everything is dropped except the few variables a parser genuinely needs.
    No FAMILYOS_ variable survives, so the child has no master key, no database
    URL, no object-store credentials and no inbound secret -- there is nothing
    in there for a compromised parser to take.
    """
    keep = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR",
            "TESSDATA_PREFIX", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    # Parsers that look for a network proxy should find a dead one rather than
    # the host's.
    env["no_proxy"] = "*"
    return env


def _command(media_type: str) -> list[str]:
    """The child, optionally wrapped. FAMILYOS_SANDBOX_CMD is the seam where a
    stronger boundary goes -- `bwrap --unshare-net --unshare-pid --ro-bind / /`
    or a container runtime -- without the caller knowing."""
    inner = [sys.executable, "-I", "-m", MODULE, media_type,
             str(settings.parse_memory_mb), str(settings.parse_cpu_seconds)]
    wrapper = (settings.sandbox_cmd or "").strip()
    return (wrapper.split() + inner) if wrapper else inner


async def parse_isolated(data: bytes, media_type: str) -> ParsedDocument:
    """Parse in a child process. Raises ParseFailed rather than returning an
    empty document, because an empty document reads as "this notice says
    nothing" and that is the failure nobody would notice."""
    if not settings.parse_sandbox:
        # Development escape hatch. The boot log says when it is off.
        return await asyncio.to_thread(parse, data, media_type)

    proc = await asyncio.create_subprocess_exec(
        *_command(media_type),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=child_env(),
        cwd=os.getcwd(),
        close_fds=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(data), timeout=settings.parse_timeout_seconds)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise ParseFailed(f"parsing {media_type} took longer than "
                          f"{settings.parse_timeout_seconds}s and was killed") from None

    if proc.returncode != 0:
        detail = (err or b"")[-400:].decode("utf-8", "replace").strip()
        # A negative return code is a signal: a segfault in a parser, or the
        # CPU limit firing. Both are the interesting case.
        raise ParseFailed(f"the parse sandbox exited {proc.returncode}: {detail or 'no output'}")
    if len(out) > settings.parse_max_output_bytes:
        raise ParseFailed(f"the parse sandbox returned {len(out)} bytes, over the "
                          f"{settings.parse_max_output_bytes} limit")
    try:
        return _from_json(json.loads(out))
    except (ValueError, KeyError, TypeError) as exc:
        raise ParseFailed(f"the parse sandbox returned something unreadable: {exc}") from exc


def describe() -> str:
    if not settings.parse_sandbox:
        return ("Parsing is NOT sandboxed (FAMILYOS_PARSE_SANDBOX=false): PDFs and images are "
                "parsed in this process, which holds the database pool and household keys.")
    where = (settings.sandbox_cmd or "").strip()
    return (f"Parsing is sandboxed in a child process with no FAMILYOS_ environment, "
            f"{settings.parse_memory_mb}MB and {settings.parse_cpu_seconds}s CPU, "
            f"killed after {settings.parse_timeout_seconds}s"
            + (f", wrapped in: {where}" if where else ", unwrapped (no network namespace)"))


if __name__ == "__main__":
    raise SystemExit(main())
