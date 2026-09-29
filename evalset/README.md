# FamilyOS extraction test set (English)

Public school notices collected from the web, with the facts a correct
extractor should produce. Each line of `notices.jsonl` is one notice: its
`source_url`, why it is in the set, the facts to extract (`expected`), what it
must not produce (`expect_no`), and the `traps` it tests. The downloaded
original sits in `originals/` at `original_path`, with its `original_sha256`.

## Contents (28 notices, 27 originals)

- **en-001 to en-008**: the seed set, started 2026-09-29 in the cloud project.
  Originals downloaded on 2026-09-29. en-006 (Bard HSEC consent form) now
  returns 404 with no archive copy, so it has no original; en-022 is the
  replacement US consent form.
- **en-009 to en-028**: added 2026-09-29 from web search: PTM circulars, UK
  and US consent forms, trip letters with costs and several deadlines, a
  newsletter, a revised exam circular paired with its original (en-015 and
  en-016), a continuation notice, a postponement with no new date, a fee
  schedule with tiered amounts, a standing lunch policy, an optional health
  consent, and one image notice (en-020, JPEG, needs OCR).

## Label status

Read `label_status` on each row.

- Checked against the original: en-001 (dated items), en-002, en-004, en-005, en-007.
- en-003 was spot-checked and extended (winter holiday extension, new
  timings). It is a 27-page compilation with Hindi copies, so its list is a
  subset.
- en-008 and en-009 to en-028 were drafted by Claude from the original's
  text and still need a person to check them.

## Known limits

- English only. en-003 carries Hindi duplicates of English notices.
- Public notices skew towards formal PDFs. Real family intake is mostly
  WhatsApp photos and forwards, which still need collecting (with consent).
- Several notices are years old (2013 to 2021). Their dates are in the past,
  so tests must pin "today" to the issue date, not the run date.
- No real fee bill or invoice for a named student was found in public; the
  fee cases are reminders, schedules and a billed charge (en-019).
