# What FamilyOS did not see

Every other part of this records what arrived. The alpha's real question is the
other one: **of the notices a family actually got, how many reached the system
at all?**

That cannot be answered from the artifacts. A notice that never arrived leaves
no row behind to count. So a member says so.

```
POST /v1/intake-gaps              "you missed this, and here is where it lived"
GET  /v1/intake-gaps              a guardian sees the household's; others their own
POST /v1/intake-gaps/{id}/arrived point it at the notice that turned up later
GET  /v1/intake-gaps/summary      capture rate, and where the misses lived
```

## Why one column carries three decisions

`lived_where` is the whole point of the table. Each answer means something
different and expensive:

| It lived on | Then |
|---|---|
| `email` | the query or the sender matching is wrong — **the cheapest of these to fix** |
| `whatsapp_group` | forwarding has to get easier; a new rail would not help |
| `whatsapp_direct` | the number is not matched to a member, or they forwarded to the wrong place |
| `school_portal` | **a browser rail would earn its keep** |
| `school_app` | a mobile rail would, if the app runs on a virtual device at all |
| `other_app` | mobile-only, and worth knowing which app before costing anything |
| `sms` | no channel reads these yet |
| `paper` | needs a camera, which needs a native app |
| `word_of_mouth` | nothing can read this — it is the floor on what any system can capture |
| `unknown` | ask again; an unknown here is a question nobody followed up |

So the browser rail, the virtual-phone rail and the native app are all decided
off the same column, and none of them has to be guessed. Each `means` is
carried back in the summary beside its count, so the finding and its
consequence are never separated — a bare table of channel counts invites
everyone to read their preferred conclusion into it.

## A gap is a report, never a notice

It produces no claims, no obligations, no extraction, no artifact, and nothing
downstream reads it.

This line matters. A gap's title is a sentence somebody typed from memory. If
anything downstream consumed it, that sentence would be standing where a quote
from a circular should be — a second source of truth about what a school said,
with no provenance behind it and no way to check it. Everything else in
FamilyOS refuses to record a fact it cannot point back at a line in a
document; this must not be the exception.

It exists to be counted. That is all.

For the same reason the title stays out of the audit trail, exactly as a
filename does: the audit detail carries `lived_where`, `had_date` and
`also_emailed`, and never the family's words.

## Reading the numbers honestly

```json
{ "captured": 3, "missed_reported": 1, "capture_rate": 0.75, ... }
```

**The rate is an upper bound on capture, not a measurement.** A family reports
some of what it misses and never all of it — by definition they cannot report
a notice they still do not know about. So `captured / (captured + missed)` is
the best case. The endpoint says so in a `caveat` field, because a number
travelling without its caveat gets reported upward without it.

The breakdown is the reliable part, and the part that decides anything.

Two deliberate details:

- An empty household's rate is `null`, not `1.0`. A household with no notices
  has captured nothing, not everything, and `1.0` is exactly the kind of wrong
  answer that gets quoted in a board update.
- Attachments are not counted. One email with a circular attached is one
  notice that arrived, not two — otherwise capture improves the more
  attachments a school sends.

Scope is the visibility rule as everywhere else, so two members of one
household can see different numbers. That is the rule working, not a bug.

## How to use it in the alpha

During the hand-held phase, every time a family mentions something FamilyOS
did not have, file it — including the ones that turn out to be email matching
problems, which are the most fixable and the easiest to dismiss as not real
misses.

At the end of the alpha the breakdown answers, with evidence rather than
argument:

- whether to build the browser rail (`school_portal`)
- whether to run the Play Integrity spike at all, let alone fund the mobile
  rail (`school_app`, `other_app`)
- whether a native app is about push notifications or about a camera (`paper`)
- whether forwarding needs to get easier rather than be replaced
  (`whatsapp_group`)
- and how much of the gap is simply `gmail_query` being too narrow (`email`)

If the biggest bar is `email`, the cheapest quarter of work available is
widening a query. If it is `word_of_mouth`, no rail helps and the product has
a ceiling worth knowing about early.
