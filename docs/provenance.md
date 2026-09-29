# Code taken from other projects

FamilyOS copies modules from other codebases at a pinned commit rather than
forking them or depending on a shared package. Each entry says where the
code came from and what changed, so later fixes upstream can be ported by
diffing against the pinned version.

## Orbit: `familyos/jobs.py`

- Source: https://github.com/sanjayarun07/orbit, `app/jobs.py` at commit `914c3d9` (28 Sep 2026)
- Owner: same author as FamilyOS; no licence file needed.

Kept as is: the job row (plan, state, evidence, operations, signals), leases
taken and renewed by compare-and-swap, heartbeats, `JobContext` with
`plan`/`step`/`finish_step`/`checkpoint`, the idempotent operation cache
(`call`), `ask`/`approve` pauses, `answer`/`approved`/`expire_approval`,
`cancel`, fingerprints that drop the cache when a handler's assumptions
change, and recurring jobs via `next_run_at`.

Changed:

| Orbit | FamilyOS | Why |
|---|---|---|
| `user_id` (FK `users`), `account_id`, `session_id` | `household_id` (FK `households`, cascade), `member_id` | Households own data; members act. A job with no household is a system job (household erasure). |
| Memory store fallback, `StoreUnavailable`, `run_ephemeral` | Removed | FamilyOS has one store of record. If Postgres is down, requests fail rather than losing work. |
| Schema created from `_TABLE_SQL` at first use | `migrations/0001_foundation.sql` | One migration runner for the whole app. |
| `deliver`, `maintain`, `acknowledge`, `attached`/`delivered` columns, credit charging | Removed | These post a job's answer into an Orbit chat session and bill it. FamilyOS has no chat sessions yet. |
| `guard()` checks `turn_log.is_deleted(user)` | Row existence only | A household's jobs cascade with it, so an erased household's job simply disappears and the guard stops it. |
| Binds TradingView token, receipts owner and evidence envelopes in `run()` | Removed | Orbit-specific. |
| `execution_policy.background(run(row))` | `asyncio.create_task(run(row))` | No execution policy module yet. |
| `settings.job_volatile_max_age_seconds` | `VOLATILE_MAX_AGE_SECONDS` constant | Fewer settings until something needs tuning. |
| Ids as 32-char hex | Canonical UUID strings | Matches every other id in the API. |
| `create()` only on the pool | Optional `conn=` | Lets a change and the job it starts commit together (consent withdrawal and its erasure). |
| `attach(on_event=...)` | No event callback | Nothing streams job progress yet. |
| `scrub_user` | Replaced by `familyos/erasure.py` | Erasure is household- and subject-scoped, and runs as a job itself. |

Style: `jobs.py` keeps Orbit's one-line `a; b; c` statements in `cas()` so the
diff stays small (`E702` is ignored for this file only).
