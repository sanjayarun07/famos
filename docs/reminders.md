# Reminders (milestone 3, part one)

Intake stores a notice, extraction reads obligations out of it, and until this
the story stopped: a dated thing sat in a list and nobody was ever told. The
README promises notices become "things that get done"; this is the part that
does the telling.

One recurring job, `send_reminders`, on `jobs.py` — whose `next_run_at` had
been there unused since it came over from Orbit.

## Two reasons

| reason | when | why |
|---|---|---|
| `due` | an **accepted** obligation is approaching its date | the family agreed to it |
| `undecided` | a **proposed** obligation nobody has accepted or dismissed, and its date is near | the one that actually gets missed |

A proposal that is decided, and an obligation that is dismissed, have their
pending reminders cancelled on the next sweep. Nothing nags about something
already dealt with.

## Who is told

The same rule as every read (`artifacts.VISIBLE_TO_MEMBER`), applied to the
obligation's artifact:

> a shared notice reminds the adults and guardians of the household; a private
> one reminds only the member who sent it.

Never a child. Children do not sign in, and an obligation's `subject_member_id`
is who it is *about*, not who is told.

## Sending twice is impossible

One row per `(obligation, member, reason, lead_days)` with a unique index. The
sweep inserts with `ON CONFLICT DO NOTHING` and only sends rows still
`pending`, so it can run as often as it likes: a row that exists is not made
again, and a row already `sent` is not sent again. A send that fails records
its error and is retried on the next sweep until
`FAMILYOS_REMINDER_MAX_ATTEMPTS`, then stops as `failed`.

## Channels

- **`log`** (the default) writes the reminder to the application log and the
  audit trail. It needs nothing configured, which is why it is the default.
- **`email`** sends over SMTP. Set `FAMILYOS_SMTP_HOST` and the rest; without
  a host the channel refuses rather than pretending to send.

Adding one is a function in `reminders.CHANNELS` taking `(row, subject, body)`.

## What a reminder says

What and when, and who it is for — never the notice's own words. The record is
a link away, and the audit event carries ids, counts and the channel, never
text.

```
Subject: Return the trip consent form needs signing
         Return the trip consent form is due tomorrow.
         For: Meera
         From a notice received 2026-09-22.
```

## Settings

| | |
|---|---|
| `FAMILYOS_REMINDERS_ENABLED` | `true` |
| `FAMILYOS_REMINDER_LEAD_DAYS` | `7,1,0` — days before the date; `0` is on the day |
| `FAMILYOS_REMINDER_SWEEP_SECONDS` | `3600`, floored at 60 |
| `FAMILYOS_REMINDER_CHANNEL` | `log` or `email` |
| `FAMILYOS_REMINDER_MAX_ATTEMPTS` | `5` |

## API

| Method | Path | Who |
|---|---|---|
| GET | `/v1/reminders` | members: what they will be told, and were |

## Reconciliation

A notice that revises an earlier one links to it (`familyos/reconcile.py`),
and confirming that link supersedes the older obligations so they stop
reminding. A reminder for an obligation with an **unconfirmed** link still goes
out, and says a later notice may have changed it: being reminded about
something already settled is a nuisance, not being reminded about something
that still stands is the harm.

## Not yet

- Quiet hours, and one digest instead of several separate reminders.
- A member choosing their own lead times, or opting out.

## Sent once

A reminder used to be selected, sent, then marked sent. Between the send and
the mark there was a window: two workers could pick up the same row, and a
crash after delivery would deliver again on the next sweep. One row per
(obligation, member, reason, lead) stops a duplicate being *scheduled*, which
is not the same as stopping it being *sent*.

Now a row is claimed first -- moved to `sending` in the same statement that
selects it, `FOR UPDATE SKIP LOCKED` so a second worker steps over it rather
than queueing behind it -- and only then handed to the channel. `claimed_at` is
what lets a claim be taken back: a worker that dies mid-send would otherwise
leave the row in `sending` for ever, and never sent at all is worse than sent
twice. `CLAIM_TTL` is ten minutes.

This is at-least-once, and it cannot be anything else while the send is a
network call to somebody else. What it does guarantee is that a duplicate needs
a crash inside a window of milliseconds, rather than a second worker or an
ordinary restart.

## Visibility is decided twice

Scheduling asks who may see an obligation *now*. A row made yesterday was
answered with yesterday's visibility, and a notice can be made private
afterwards -- so three things cancel a pending row: the obligation was decided,
a later notice superseded it, or the member may no longer see the notice.

The last one is why delivery checks again rather than trusting the sweep. A
reminder is a sentence about a notice; sending it to somebody who can no longer
open that notice leaks the notice. The cancel sweep keeps the queue tidy;
delivery is the guarantee, because delivery is the moment it matters.

Reading follows the same rule. `GET /v1/reminders` returns not "the rows made
for this member" but "the rows this member may still be shown" -- a notice made
private takes its reminders out of everyone else's list, the already-sent ones
included. The rows stay for the audit trail; they stop being readable.
