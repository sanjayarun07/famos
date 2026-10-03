# Connecting a parent's Gmail

Forwarding works, and it will keep working. But it asks the family to notice a
notice and act on it, which is the thing they came here because they are bad
at. Reading the mailbox the school already writes to removes that step.

## One scope, and why

`gmail.readonly`, plus `openid` and `userinfo.email` so the console can say
which mailbox is connected. Nothing that can modify, send or label.

`gmail.readonly` is a **restricted** scope. Google requires a CASA Tier 2
security assessment before an app may ask the public for it: roughly 4–8 weeks,
$540–1,800 a year, re-validated annually.

**You do not have to wait for it to start.** An OAuth client whose consent
screen is in **Testing** may ask up to **100 named test users** for restricted
scopes with no verification at all. An alpha of 20–30 families is well inside
that. Nothing in this code differs between the two cases; only the Google-side
client does.

### The catch, and it is a real one

A client in Testing with an External user type issues **refresh tokens that
expire after seven days**. So every connected mailbox stops being readable
weekly until the app is verified, and each parent has to reconnect.

A mailbox that silently stops being read is the worst failure this product can
have: the family believes nothing is being missed, and notices are arriving
nowhere. So it is not silent. The poller marks the connection
`needs_reconnect`, stops asking, writes `mailbox.needs_reconnect` to the audit
trail, and the **daily brief says so, second only to something already
missed**.

Plan around it: forwarding is the alpha's primary channel and Gmail is the
secondary, CASA is filed on day one because it is the longest-lead item on the
roadmap, and weekly reconnect friction is itself a thing worth measuring.

## What you need from Google

1. A Google Cloud project.
2. **APIs & Services → Library → Gmail API → Enable.**
3. **OAuth consent screen**: User type **External**, publishing status
   **Testing**. Add each alpha parent's Google address under **Test users**
   (100 maximum). Add the `gmail.readonly` scope.
4. **Credentials → Create credentials → OAuth client ID**, type **Web
   application**. Under *Authorised redirect URIs* add exactly the value of
   `FAMILYOS_GOOGLE_REDIRECT_URI` — Google matches it character for character.
5. Put the two values in the environment:

```sh
FAMILYOS_GOOGLE_CLIENT_ID=....apps.googleusercontent.com
FAMILYOS_GOOGLE_CLIENT_SECRET=...
FAMILYOS_GOOGLE_REDIRECT_URI=http://localhost:8000/v1/google/callback
FAMILYOS_GMAIL_ENABLED=true
```

Until the first two are set, connecting answers plainly that there is no OAuth
client rather than redirecting to a Google error page, and the boot log says
Gmail intake is off.

## The flow

```
POST /v1/google/mailboxes/authorize   ->  { url, state }
        the member opens `url` and consents at Google
GET  /v1/google/callback?code=&state= ->  redirect back into the console
GET  /v1/google/mailboxes             ->  what is connected, and whether it works
POST /v1/google/mailboxes/{id}/disconnect
```

The callback carries no FamilyOS token — it is a browser redirect from Google —
so a single-use `google_auth_states` row is the only thing tying that consent
to a member. A state we did not issue is refused, which is what stops somebody
else's consent being attached to this household. PKCE is used as well, so an
intercepted code is not enough on its own.

## What is stored

The refresh token is the whole of the access, so it is **sealed with the
household's data key** — the same key the originals are sealed with, bound to
the member by the AAD. Two things follow:

- **Erasing a household ends its access to every mailbox it had connected.**
  Erasure destroys the key first, so the token stops being readable before
  anything is deleted. Nothing has to remember to go and revoke anything.
- A database dump on its own is not access. It takes the master key too.

Access tokens are never stored. Each poll refreshes one, uses it and drops it.
Disconnecting overwrites the sealed token with empty bytes and tells Google.

There is no endpoint, for anybody, that returns a token. A guardian cannot see
another member's mailbox at all: a mailbox is more private than any one notice
out of it.

## What it reads

A query, not an inbox:

```sh
FAMILYOS_GMAIL_QUERY='has:attachment OR subject:(school OR circular OR notice OR fee OR permission)'
FAMILYOS_GMAIL_BACKFILL_DAYS=30
FAMILYOS_GMAIL_MAX_PER_POLL=25
FAMILYOS_GMAIL_POLL_SECONDS=300
```

Narrow on purpose. Mail that does not match is mail FamilyOS never sees, which
is a privacy property before it is a cost one. Widen it only on evidence that
notices are being missed — the alpha's capture rate is exactly that evidence.

The first pass reaches back `gmail_backfill_days` and walks pages until Gmail
runs out. After that each poll asks for what arrived since the last one, with
an hour of overlap so a late delivery or clock skew cannot drop a message.
Overlap is free: intake refuses a Gmail id it has stored before.

## Who the mail belongs to

The sender is the school, not the member — matching on sender, the way
forwarded mail does, would refuse nearly every notice. So this mirrors the
WhatsApp bridge instead: the member whose mailbox it is **received** it, so it
is stored for them, privately, and the school's address goes in `source` the
way an email's From does. There is no quarantine lane, because a message we
cannot attribute is a message we would not have been given.

The whole RFC 822 message is stored as the original, so the stored copy is the
mail itself — headers, `Authentication-Results` and all — and not our rendering
of it. Attachments become children, exactly as they do for forwarded mail: the
circular is nearly always the PDF, and the covering note is nearly always where
the date is.
