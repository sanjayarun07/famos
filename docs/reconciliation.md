# Reconciliation (milestone 3, part two)

A school sends "Annual day, revised timings" and the earlier notice is still
sitting there with its own date. Before this, both reminded and nothing said
they were the same thing.

Extraction already reads an `amendment` claim out of the new notice, saying in
the notice's own words what it changes (`claims.amends`) and what is now true
(`claims.change`). What was missing was the link from that claim to the
**artifact** it is talking about.

## The link is proposed, never applied on its own

Matching two notices by their words is a guess, and the cost of guessing wrong
is lopsided:

- A wrong link **stops** the family being reminded about a deadline that still
  stands. They miss it, and nothing tells them.
- A missing link means they are reminded **twice** about something already
  settled. Annoying, and obvious the moment they read it.

So a member confirms it, the same way quarantine needs a guardian and a
proposed obligation needs accepting. Until then **both notices still remind** —
and the reminder for the older one says a later notice looks like it changes
this and nobody has confirmed that yet.

## Matching

No model. An amendment claim's words are compared with the titles of earlier
claims in the same household:

1. Words are lowercased, split, and stripped of the ones every notice uses —
   `circular`, `dear`, `parents`, `revised`, `rescheduled`, `school`, and so
   on. A notice saying "Revised circular: the annual day" is about `annual
   day`; the rest is packaging.
2. The score is the overlap of the amendment's words (its own title plus what
   it says it amends) against the candidate's title, as a share of the two sets
   together.
3. `+0.15` when both are scoped to the same group (`applies_to`), `+0.05` when
   the candidate is a dated event or deadline.
4. Below `MIN_SCORE` (0.42), or fewer than two shared words, proposes nothing.

Deliberately conservative: a near miss should propose nothing rather than
guess. The stored row keeps the `score` and a plain-words `matched_on` ("3
shared words, same group, dated") so a person deciding can see why.

## Confirming

| | |
|---|---|
| **confirmed** | the older claim's obligations get `superseded_at` and `superseded_by_artifact_id`; their pending reminders are cancelled, and the sweep will not make more |
| **rejected** | both notices stand, unchanged |

A superseded obligation is not deleted. It keeps its row, so "what happened to
that?" has an answer.

## Who may decide

Only a member who can see **both** notices. Confirming says these two things
are the same, which needs sight of both, so a private notice cannot be linked
away by someone who was never shown it. The list and the decision endpoint use
the same visibility rule as everything else, applied twice.

## In the console

**Revisions** (`/app/#/revisions`) shows each proposed link as the two notices
side by side -- what the newer one says, what it replaces, the match score and
the words it matched on -- with Confirm and Reject. The nav carries a count, and
a superseded task stays on Today struck through, linking to the notice that
replaced it, rather than quietly disappearing.

## API

| Method | Path | Who |
|---|---|---|
| GET | `/v1/amendments?status=proposed` | members who can see both notices |
| POST | `/v1/amendments/{id}/decision` | the same; `{"status": "confirmed" \| "rejected"}` |

## The same notice twice

A parents' group makes this the normal case: three people relay one circular
and the family is told three times about one consent form.

Storage has always deduplicated the **bytes** -- one blob however many members
send it -- but each artifact proposed its own obligations. Now, when the very
same bytes have already arrived, the later copy's obligations are superseded by
the first and their pending reminders cancelled.

This one needs nobody's confirmation, unlike an amendment. Matching two notices
by their words is a guess; matching them by SHA-256 is not. Both artifacts stay:
two parents did send it, and who told you is worth keeping.

Still a guess, and so still unsolved: the same notice **re-photographed** or
re-encoded by someone else. Different bytes, same circular. That needs the
claim-similarity matcher above pointed at "these are the same notice" rather
than "this one amends that one".

## Heard, not issued

A message a parent forwarded from their own group is second-hand. It is usually
the fastest way a family hears anything -- and it is still somebody's retelling,
so a claim read out of it carries `HEARSAY_PENALTY` (0.8) on its confidence, the
same shape as `pipeline.UNGROUNDED_PENALTY`. Kept and shown, trusted less.

It still becomes a task. Held less confidently is not the same as ignored.

## Not yet

- One amendment is linked to one earlier claim, the best match. A notice that
  revises several things at once proposes one link per amendment claim, which
  is usually right but not always.
- Nothing re-runs matching when a *later* notice arrives that would have been a
  better candidate for an already-decided link.
- The model is never asked. It read the amendment; it could also be asked which
  notice it means, with the candidates in front of it.

## Re-reading a notice somebody already answered

Extraction is not final. A better model, a fixed prompt or a second pass can
read the same circular differently -- and by then somebody may have accepted
what the first pass said.

Deleting the still-proposed obligations is safe: nobody answered them. An
**accepted** one is different. The decision is a person's, not ours to drop.
But leaving it untouched is not safe either: if the new reading moves the date,
or no longer finds the claim at all, the accepted row goes on naming a date the
notice no longer gives -- and goes on reminding about it, which is the one thing
all of this is for.

So neither. An accepted obligation the new reading no longer supports is
**superseded, by the same notice**. Superseded means reminders stop (the
scheduler skips it, and the sweep cancels what is already pending), the row
stays where the family can see what became of it, and the corrected date
arrives beside it as a fresh proposal to accept.

What a person decided is recorded. What the notice says now is what the family
is reminded of. A reading that agrees with the decision changes nothing.
