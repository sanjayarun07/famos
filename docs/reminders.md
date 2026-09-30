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
