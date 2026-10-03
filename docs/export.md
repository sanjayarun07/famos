# Taking everything with you

Erasure answers *destroy what you hold about us*. Export answers the other
half: *give us what you hold about us, in a form we can read and keep*. The
law calls them Articles 17 and 20. A family calls them leaving.

```
GET /v1/export                      the member's own, as a zip
GET /v1/members/{id}/export         what is held about one child, for a guardian
```

## Two decisions worth stating

**It is generated and streamed, never stored.** A saved export would be a
second copy of the household sitting in the object store, unsealed, which the
erasure path would then have to find and chase — and a backup of it would
outlive the household key that was supposed to make erasure final. So an
export is built in memory for one request and handed over. Nothing new is at
rest afterwards, so nothing new has to die with the key. The response carries
`cache-control: no-store` for the same reason.

The cost of that choice is a ceiling: `FAMILYOS_MAX_EXPORT_BYTES` (200 MB by
default) is checked while the originals are being read, so an oversized
request is refused with `413` before anything is built rather than after. A
household large enough to hit it needs the streaming version, which this is
not yet.

**It is written twice.** `data/*.json` is the structured, machine-readable
form the law asks for and another tool can import. The markdown beside it is
for the family.

## What is in it

```
README.md                  what this is and how to read it
family.md                  the household, who is in it, consent for any child
notices/0001-<date>-<slug>/
    original.pdf           exactly the bytes that arrived
    notice.md              what was read out of it, and what it asked
obligations.md             everything asked of the family, and what was decided
reminders.md               what the family was told, and when
audit.md                   the record, newest first
data/household.json        the same contents, structured
data/notices.json
data/claims.json
data/obligations.json
data/reminders.json
data/audit.json
data/audit.csv
```

A notice page carries, for every fact: the words it was read from, the page
they were on, the confidence, and whether the quote was matched to the notice
at all. An ungrounded fact says so in those words — **not matched to any words
in the notice** — because that is the one a family most needs flagged. A
notice that was relayed by somebody rather than sent by the school says that
too, since what it states was weighed as second-hand.

This is the same provenance the console draws, in a form that survives having
no console. It is readable and correctable by the person it is about, which is
most of the point of handing it over: if a date here is wrong, the quote beside
it says whether the notice said something different or whether it was read
wrongly.

Only the current reading is exported. One extraction per notice is current; an
export says what FamilyOS holds now, not every reading it ever made.

## Scope

The visibility rule, as everywhere else. **An export is not a way around it**:
a private notice leaves in its sender's export and in nobody else's, not even
a guardian's. One adult's private mail is no more exportable than it is
readable.

The audit trail is the household's record for a guardian and the member's own
actions for anyone else.

A child's export is different, because children do not sign in and so there is
no "what they can see" — only what names them. A guardian asks for it, and it
holds the notices where the child is a subject, the claims that name them,
their obligations, and the consent that made recording any of it lawful. It is
the exact counterpart to subject erasure: the same scope, handed over instead
of destroyed, so a guardian can see what erasing would take away before they
ask for it.

An adult is never exported as a subject. They ask for their own.

## What it costs the record

Reading fifty originals to answer one request for an export is one deliberate
act, so it is recorded as one: a single `artifact.originals_read` saying how
many and how large, and a single `export.created` with the scope and the
counts. Fifty separate `artifact.original_read` rows would describe the same
event less truthfully.

As everywhere, the audit detail holds counts and ids — never a filename and
never a notice's words.
