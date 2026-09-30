# FamilyOS architecture: milestone 1

This milestone builds the intake and trust layer that everything else
depends on: who is in the household, who may see what, and a record of
every document that arrived, stored exactly as it arrived. No model runs
yet. The full plan and the reasoning behind it is in the architecture
review: https://claude.ai/artifact/1q9VVgPorjYGFuoZFTfYiU

## Components

| Module | What it does |
|---|---|
| `identity.py` | Households, members (guardian, adult, child) and sign-in tokens. |
| `intake/gateway.py` | The four-step trust gateway every channel goes through. |
| `intake/email.py` | Forwarding-email adapter: parses the raw message, reads the provider's sender verdict, finds attachments. |
| `intake/sniff.py` | Decides file type from bytes, not from the sender's label. |
| `artifacts.py` | Immutable originals with hash dedup, the InputArtifact envelope, visibility, quarantine, deletion. |
| `crypto.py`, `blobstore.py` | Per-household envelope encryption; S3-compatible or local object storage. |
| `consent.py` | Verifiable parental consent records for children's data. |
| `erasure.py` | Deletion hooks: subject erasure and household erasure, run as durable jobs. |
| `audit.py` | Append-only audit events. |
| `jobs.py` | Durable job engine copied from Orbit (see `provenance.md`). |

## The trust gateway

Every item, from any channel, goes through the same four steps before it is
stored:

1. **Receive.** The channel adapter turns what arrived into an `InboundEnvelope`.
2. **Identify.** A signed-in member for uploads. For email, the From address
   must belong to a member of the household the message was sent to, and the
   receiving provider must have authenticated it (DMARC pass, or DKIM pass for
   the From domain exactly). The verdict is read only from the
   `Authentication-Results` header whose authserv-id is
   `FAMILYOS_INBOUND_AUTHSERV_ID`: anyone can write that header, and the id is
   what tells our provider's verdict from the sender's own. Unset, nothing
   authenticates and every message waits in quarantine.
3. **Decide.** Accept, quarantine or reject. Mail from an unknown or
   unauthenticated sender is quarantined: stored, but invisible until a
   guardian accepts it and says whose it is. Rejected items (empty, too large,
   an unsupported type, an unknown household address) are not stored.
4. **Scope.** Everything starts private to the member who sent it, unless they
   chose shared.

## Visibility

v1 has two levels, `private` and `shared`. The rule lives in one SQL
fragment, `artifacts.VISIBLE_TO_MEMBER`, used inside every query:

> a member sees an accepted artifact of their household when it is shared, or when they submitted it.

Guardians get no special view of other members' private items. Children do
not sign in. Attachments always have their email's visibility. Delegated use
("Dad is busy at 4pm" without saying why) is deliberately left for later.

## Originals

- Bytes are stored once per household, keyed by SHA-256 (`blobs`). A second
  member sending the same file gets their own envelope over the same blob.
- The same member sending the same bytes again, or the provider delivering the
  same Message-ID twice, gets back the artifact made the first time.
- Blobs are sealed with AES-GCM using the household's own data key before they
  reach the object store. Object keys are random ids, never hashes or names.
- Every read of an original is audited and re-checked against its hash.

## Consent and erasure (DPDP Act 2023)

- A guardian records consent for each child, with the notice version and how
  the guardian was verified. Until then nothing can name that child as its
  subject (`409 consent_required`).
- Withdrawing consent starts, in the same transaction, a job that erases every
  artifact naming the child and any original no longer referenced. The consent
  record stays, as evidence that consent existed and ended.
- Erasing a child member does the same and removes the member.
- Erasing the household stops sign-in and intake at once, then a job destroys
  the household key (so every copy of the originals, including backups, is
  unreadable), deletes the objects, then deletes the rows and the household's
  audit trail. `erasure_log` keeps what was done, without personal data.

## Audit

`audit_events` rejects UPDATE and DELETE with a trigger; only a household
erasure can purge it. Events carry ids, counts, sizes and hashes, never
filenames, subjects or text.

## Known limits of this milestone

- Sign-in is a bearer token issued when a member is added. Phone OTP or email
  magic link replaces `identity._issue_token` without changing anything that
  consumes a `Principal`.
- A token lasts `FAMILYOS_TOKEN_LIFETIME_DAYS` (30) and slides forward while it
  is used, at most once an hour, so an active session stays signed in and an
  abandoned one ends. Signing out ends the token the request arrived on and
  leaves the member's other devices alone; a guardian can end every token a
  member holds, which is the answer to a lost phone. What is still missing is a
  way to get a *new* token without a guardian adding you again.
- Member email addresses are trusted as the guardian typed them; there is no
  verification step yet.
- If a request fails after its sealed object was uploaded but before its row
  committed, the object is left behind. It is unreadable without the household
  key and is destroyed with it; a sweeper can come later.
- Redis is not used yet. It arrives with rate limiting and caching, and will
  hold nothing that must survive a restart.

## Milestone 2: extraction

Claims with source spans, proposed obligations and the scorer: see
[extraction.md](extraction.md).

## Next

Reconciliation of revised notices (an amendment claim updating the
obligations of the notice it amends), the review card, reminders on
`jobs.py`, and matching `applies_to` to a child's class.
