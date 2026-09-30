# FamilyOS

A household agent that turns school notices, bills and forms into things
that get done, without losing track of where each fact came from or who is
allowed to see it.

Milestone 1 is the intake and trust layer:

- Households with guardians, adults and children, and private/shared visibility
- One intake path for every channel: upload (share sheet) and a forwarding email address
- Originals stored exactly as they arrived, encrypted per household, deduplicated by hash
- Quarantine for mail from unknown or unauthenticated senders
- Append-only audit trail
- Parental consent records, and erasure of a child's data or the whole household

Milestone 2 is extraction: every accepted notice is read (PDF text, OCR for
photos and scans, email), and the facts in it (dates, deadlines, amounts,
forms, changes to earlier notices) become claims that point back to the
exact place in the original. Actionable claims become proposed tasks and
calendar entries for a member to accept or dismiss. See
[docs/extraction.md](docs/extraction.md), including how to score it against
the test set.

See [docs/architecture.md](docs/architecture.md) for how it fits together,
[docs/reminders.md](docs/reminders.md) for how the family gets told,
[docs/reconciliation.md](docs/reconciliation.md) for revised notices, and
[docs/provenance.md](docs/provenance.md) for code taken from Orbit.

## Run it

```sh
docker compose up -d                 # Postgres + MinIO
cp .env.example .env                 # then set FAMILYOS_MASTER_KEY (and OPENAI_API_KEY)
sudo apt-get install tesseract-ocr   # OCR; brew install tesseract on macOS
pip install -e ".[dev]"
uvicorn familyos.main:app --reload   # applies migrations on start
```

The console is at http://localhost:8000/app/ and the API docs at
http://localhost:8000/docs. The console is served by the API itself, so a
request is same-origin and the member's bearer token goes straight on it: no
CORS rule and no second host to run. Sign in by pasting a token, or create a
household from the front page.

```sh
# Create a household; keep the token it returns.
curl -s localhost:8000/v1/households -H 'content-type: application/json' \
  -d '{"name":"Home","guardian":{"display_name":"Amma","email":"amma@example.com"}}'

# Upload a notice, shared with the household.
curl -s localhost:8000/v1/artifacts -H "Authorization: Bearer $TOKEN" \
  -F file=@notice.pdf -F visibility=shared
```

## Getting notices in today

Most school traffic is WhatsApp and the school's own app, and neither offers a
feed you can subscribe to. Until a WhatsApp business number is verified, these
work now and need nothing external:

- **Paste it.** Long-press the WhatsApp message, Copy, then *Add a notice →
  Paste a message*. Copied text reads better than OCR of a screenshot of it.
- **Share a screenshot.** Images are read with OCR, so a screenshot of the
  school app works; the claim still points at the pixels it came from.
- **From an iPhone, without opening the console.** Shortcuts → new shortcut →
  ⓘ → *Show in Share Sheet*, one **Get Contents of URL** action:
  `POST https://<your host>/v1/artifacts`, header
  `Authorization: Bearer <token>`, request body *Form* with `file` = Shortcut
  Input. It then appears in WhatsApp's share sheet. The token sits in the
  shortcut in plain text, so issue one for that phone and revoke it from
  *Household → Sign out everywhere* if the phone is lost.
- **On Android**, Tasker or MacroDroid can post to the same endpoint on a
  notification, which is the only way to capture something nobody opened. iOS
  has no equivalent and never will.

Forwarding email: point your inbound mail provider (SES, Postmark, Mailgun,
Cloudflare Email Routing) at `POST /v1/inbound/email`, posting the raw message
with the `X-FamilyOS-Webhook-Secret` header and, if available, the envelope
recipient in `X-FamilyOS-Recipient`. Each household's address is shown as
`inbound_address` on `GET /v1/household`.

## Test

Tests need a Postgres database they can wipe:

```sh
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/familyos_test pytest
ruff check .
```

## API (v1)

| Method | Path | Who |
|---|---|---|
| POST | `/v1/households` | anyone: creates a household and its first guardian |
| GET | `/v1/me`, `/v1/household` | members |
| GET | `/v1/sessions`; POST `/v1/signout`, `/v1/members/{id}/signout` | members; a guardian for anyone |
| POST | `/v1/household/members` | guardians |
| POST | `/v1/artifacts` | members: upload |
| GET | `/v1/artifacts`, `/v1/artifacts/{id}`, `/v1/artifacts/{id}/original` | members, visibility applies |
| PATCH | `/v1/artifacts/{id}` | the submitter: change visibility |
| PUT | `/v1/artifacts/{id}/subjects` | members who can see it; children need consent |
| DELETE | `/v1/artifacts/{id}` | the submitter, or a guardian if shared |
| GET | `/v1/quarantine`; POST `/v1/quarantine/{id}/accept`, `/reject` | guardians |
| POST | `/v1/inbound/email` | the mail provider |
| POST, GET | `/v1/consents`; POST `/v1/consents/{id}/withdraw` | guardians |
| POST | `/v1/members/{id}/erase`, `/v1/household/erase` | guardians |
| GET | `/v1/erasures/{id}` | members |
| GET | `/v1/amendments`; POST `/v1/amendments/{id}/decision` | members who can see both notices |
| GET | `/v1/reminders` | members: what they will be told about, and were |
| GET | `/v1/audit` | guardians |
| GET | `/v1/artifacts/{id}/extraction`; POST `/v1/artifacts/{id}/extract` | members, visibility applies |
| GET | `/v1/obligations`; POST `/v1/obligations/{id}/decision` | members, visibility applies |
