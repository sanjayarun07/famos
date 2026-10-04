# Parsing where there is nothing worth stealing

`parse()` runs PyMuPDF, Pillow and Tesseract over bytes a stranger sent us.
Those are large C libraries with a long history of memory-safety bugs, and the
bytes arrive **by email** — anyone who learns a household's inbound address can
post a crafted PDF.

Until this existed, that ran inside the API process, which holds the asyncpg
pool and, moment to moment, unwrapped household keys.

`extraction/service.py` already wrapped the call in `asyncio.to_thread`, and
that is the part that made the exposure easy to miss: **a thread is latency
isolation, not security isolation.** It keeps parsing off the event loop and
shares the address space, the file descriptors, the database pool and the keys.

## What now happens

```
bytes ──▶ python -I -m familyos.extraction.sandbox <media_type> <mem> <cpu>
            │   stdin:  the document
            │   stdout: one JSON ParsedDocument
            │   env:    no FAMILYOS_ variable at all
            │   rlimits: address space, CPU, FSIZE 0, CORE 0, NOFILE 64
            ▼
          ParsedDocument
```

The media type goes on argv and the bytes go on stdin, so no part of a
document ever appears in a process listing.

## What it stops

- **Reading our secrets.** The child's environment keeps only `PATH`, locale,
  `TMPDIR`, `TESSDATA_PREFIX` and the Python path. No master key, no database
  URL, no object-store credentials, no inbound webhook secret. There is nothing
  in there to take.
- **Reaching Postgres or the object store.** No pool, no credentials, no
  handle.
- **Writing to disk.** `RLIMIT_FSIZE` is zero, so a compromised parser cannot
  drop a payload or spill the document.
- **Dumping core.** `RLIMIT_CORE` is zero — a core file would contain the
  document.
- **Running away.** Address space and CPU are capped, and the parent kills the
  child after `parse_timeout_seconds`.

## What it does not stop

- **A kernel escape.**
- **Outbound network from the child.**

Neither is reachable through the three parsers today, and both want a stronger
boundary than a process. `FAMILYOS_SANDBOX_CMD` is the seam:

```sh
FAMILYOS_SANDBOX_CMD="bwrap --unshare-net --unshare-pid --die-with-parent --ro-bind / /"
```

A container runtime goes in the same place. The caller never knows.

## It fails closed

A crash, a timeout, or output that will not parse raises `ParseFailed`, and the
extraction job fails. The notice is left **unread**, which is visible in the
job row and in the console.

The alternative — treating a crashed parser as an empty document — would record
the notice as one that asked nothing of the family. That reads to a parent as
*"nothing here for you"*, and it is the failure nobody would ever notice. This
is the one behaviour in the module worth not changing.

Note that a *malformed but gracefully rejected* document is different: PyMuPDF
refusing a broken PDF yields a `ParsedDocument` with a note and no text, which
is correct and produces no claims.

## Configuration

```sh
FAMILYOS_PARSE_SANDBOX=true          # off only for development; the boot log says so
FAMILYOS_PARSE_TIMEOUT_SECONDS=90
FAMILYOS_PARSE_MEMORY_MB=1024
FAMILYOS_PARSE_CPU_SECONDS=120
FAMILYOS_PARSE_MAX_OUTPUT_BYTES=67108864
FAMILYOS_SANDBOX_CMD=                # the wrapper, if any
```

Every boot prints which of these is in force, next to the line saying what
reads notices. An unsandboxed parser is the most exposed surface in the system,
so it announces itself rather than being discovered.
