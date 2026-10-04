# Doing something about a notice

The alpha has an action rail, and it is deliberately the half of acting that
needs **no browser, no credential belonging to anybody else, and no automation
of a service that could object.**

Two kinds:

- **reply** — answer the school that wrote. The commonest thing a notice asks
  for is *"let us know by Friday"*, and answering it is an email.
- **calendar** — a dated thing as an `.ics`, which a mail client offers to add.
  No calendar write scope, so no new permission from anyone.

```
POST /v1/obligations/{id}/actions   {kind, body}  -> proposed, with a fingerprint
POST /v1/actions/{id}/approve       {params_sha256}
POST /v1/actions/{id}/cancel
GET  /v1/actions
```

## The recipient is derived, never supplied

**There is no recipient parameter, at any layer.** A caller sends an obligation
id and a message; the address is computed from the artifact's stored `source`.

That is the design and not an omission. A sender that accepts an arbitrary
address is an exfiltration primitive wearing a useful hat — and note that
*"the agent may send one email"* bounds the **volume**, not the destination.
One email to an attacker's address is a complete exfiltration, so the control
has to be on where it goes.

`reply_address()` decides, and it refuses more often than it accepts:

| The notice arrived | Reply goes to |
|---|---|
| Polled from a member's own mailbox (`gmail`) | the school — it wrote *to* them |
| Forwarded, sender is **not** a member | the school — it wrote to the household address |
| Forwarded, sender **is** one of our members | **nobody.** Replying would mail the family back |
| WhatsApp | **nobody.** A phone number is not an address, and a group message was written by another parent who did not ask to be answered |

## The approval is bound to the exact words

`params_sha256` covers recipient, subject and body. Approving echoes it back,
and the server **recomputes** it from the stored row before comparing.

That recomputation is the whole control, and the first version of this got it
wrong: comparing the caller's echo against the stored hash column proves
nothing, because the column was written when the action was proposed and does
not change when the content does. A test caught it. Two checks now:

1. **recomputed vs stored** — has the action changed since it was proposed?
2. **echoed vs recomputed** — is the caller approving what is actually there?

So an approval is an approval *of something* rather than a flag, and nothing
can change between showing and sending. It also makes a *delta* computable,
which is what an approval prompt should show rather than re-presenting the
whole action — approval is a control that degrades with use, and a family that
gets more than about three a week will rubber-stamp them.

## What cannot be done at all

No payment. No purchase. No browsing. The `kind` check constraint **is** the
deny-list, so widening it is a migration somebody writes and reviews rather
than a flag somebody flips.

A superseded obligation is never answered: if a later notice changed the date,
answering the old one is worse than not answering.

## Sending

Claim-then-send with `FOR UPDATE SKIP LOCKED`, the same shape as the reminder
sweep and for the same reason — at-least-once is the best anyone can do over
somebody else's network, and a duplicate should need a crash inside a window of
milliseconds rather than a restart. `claimed_at` lets an abandoned claim be
taken back after `CLAIM_TTL`.

One live action per kind per obligation, by unique index: proposing a reply
twice gives one reply to decide about, not two to send.

## The receipt

Three audit events — `action.proposed`, `action.approved`, `action.sent` —
carrying the kind, the obligation, who approved it, and **the recipient's
domain but never the address or the words.** The same rule that keeps a
notice's text out of the audit trail: enough to see where something went,
never enough to reconstruct what was said.

```sh
FAMILYOS_ACTIONS_ENABLED=false   # off by default
FAMILYOS_ACTION_MAX_ATTEMPTS=3
FAMILYOS_ACTION_SWEEP_SECONDS=60
```

Needs `FAMILYOS_SMTP_HOST`. Enabled without it, the boot log says
`MISCONFIGURED` rather than failing at the socket when a family first approves
something.
