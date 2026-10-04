# Contract sketches for rails we have not built

Offline proofs that a deferred rail's **safety contract** can be written down
and tested before any of it is deployed. No production I/O, no model calls, no
browser, no device — transport is injected.

The point is to find out whether a boundary is expressible, cheaply, while the
decision to build the thing is still open.

## `cross_app.py` — the browser rail's refusals

Arrived from review rather than from the main build. It implements three
controls from the threat model that were otherwise only designed:

- **`approved_url`** — HTTPS only, no bare IP addresses, no credentials in the
  URL, and the host must be inside the grant for that source.
- **`_page`** — *a redirect is a new source, not an implicit grant.* The final
  URL is re-checked against the allowlist and the hostname must not have
  changed. That is the redirect-enforcement row of the threat table, as code.
- **`evidence_capture`** — a page becomes a **review candidate**, never
  silently an artifact: hashed, `visibility: private`,
  `status: awaiting_review`, and refused outright if the page was truncated or
  empty.

That last one is better than the design it came from. The main architecture
said *capture the page and seal it as an artifact*; this says capture it, hash
it, and hold it for a person. For a rail whose whole risk is reading
attacker-controlled content, a human gate before anything enters the record is
the right default.

`require_owner` enforces that a source belongs to the acting member, which is
the household rule the deferred grants table will generalise.

```sh
python -m pytest tests/test_cross_app_poc.py
```

## What a sketch here is not

It is not an integration, a dependency, or evidence that a rail works. It
proves a contract is expressible. Whether the rail is built at all is decided
by `intake_gaps` — see [docs/capture.md](../docs/capture.md).
