# WhatsApp intake

Most school communication in India happens in WhatsApp -- the class group, the
school's broadcast, and the parents' own group -- and in the school's app. Not
email. This is the channel that follows the traffic.

## What is and is not possible

Meta's Cloud API **cannot read a parent's groups**, and no API ever will:
reading someone's personal message stream is not on offer at any price. What it
gives is messages sent *to* a business number.

So the shape is forwarding. A parent long-presses, picks the household's
FamilyOS number, and a morning's class-group traffic moves in about four taps.
Not automatic -- but forwarding is the one habit parents already have, and it
works identically on both phones, which notification capture does not.

## Identity, and why it is better than email

| | email | WhatsApp |
|---|---|---|
| who sent it | a `From:` header | a number Meta verified before the webhook fired |
| spoofable | yes -- hence `authserv_id`, DKIM alignment, quarantine | no |
| matched against | `members.email` | `members.phone` |

`intake/email.py` carries an apparatus for deciding whether a sender is really
who they claim. None of it is needed here.

## Routing

An email carries the household's own address, so the **recipient** places it.
Every WhatsApp message arrives at one business number, so the **sender** has to:
a member's phone number is what puts a message in a household.

Two consequences:

- A number belonging to no member is **refused and stored nowhere**. There is no
  household to quarantine it into, the way there is for mail sent to a
  household's address from a stranger.
- A number in two households places nothing. Guessing which family a school
  notice belongs to is not a guess worth making.

The webhook answers `202` either way: Meta retries anything that is not 2xx, and
a message we cannot place will never become placeable.

## What is stored

The message text becomes an artifact; each attachment becomes a child of it,
exactly as an email's attachments do. The text is kept even when the message
only carried a file, because the words around a forwarded photo are usually
where the date is.

`source` records the sender's number and profile name, Meta's message id, and
whether Meta marked it **forwarded** -- which a school notice almost always is,
and which matters: a forward from the parents' own group is second-hand.

Dedup is on Meta's message id, so a retried delivery stores nothing new.

## Reminders back the same way

`reminders.py` gains a `whatsapp` channel. Email is checked on Sundays;
WhatsApp is checked at traffic lights. Set `FAMILYOS_REMINDER_CHANNEL=whatsapp`.

## Settings

| | |
|---|---|
| `FAMILYOS_WHATSAPP_APP_SECRET` | signs every delivery; **unset means nothing is accepted** |
| `FAMILYOS_WHATSAPP_VERIFY_TOKEN` | the one-time subscription handshake |
| `FAMILYOS_WHATSAPP_ACCESS_TOKEN` | fetching media, and sending reminders |
| `FAMILYOS_WHATSAPP_PHONE_NUMBER_ID` | the business number reminders are sent from |
| `FAMILYOS_WHATSAPP_API_BASE` | `https://graph.facebook.com/v21.0` |

Point Meta's webhook at `POST /v1/inbound/whatsapp`. The `GET` on the same path
answers the subscription challenge.

## Reading a group, in development only

The Cloud API cannot read a class group, which makes realistic traffic hard to
come by while building. `POST /v1/inbound/whatsapp/bridge` accepts relays from
an unofficial client for that purpose -- off by default, and meant to stay a
development tool rather than become a feature. The reasons, and what it costs
the linked account, are in [wa-bridge.md](wa-bridge.md).

## Not yet

- **The same notice forwarded by two parents** makes two artifacts, two
  extractions and two sets of reminders. Blob storage already dedups the bytes;
  what is missing is recognising that two artifacts say the same thing. This is
  the cost of a parents' group and it needs solving.
- **Second-hand claims are not marked as such.** `source.forwarded` is recorded
  but nothing downstream reads it. A date heard from another parent is not the
  same as a date on the school's circular, and a claim's confidence should say
  so.
- Group messages cannot be read, only forwarded ones. That is Meta's line, not
  an implementation gap.
