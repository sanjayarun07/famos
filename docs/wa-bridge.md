# Reading a WhatsApp group during development

Meta's Cloud API cannot read a class group. During development that is awkward:
the channel most school communication actually travels on is the one you cannot
get realistic traffic from.

Unofficial clients can — OpenWA, wa-automate, whatsapp-web.js, Baileys. They
link as a companion device and relay what they see. `POST
/v1/inbound/whatsapp/bridge` accepts that relay.

## Read this before turning it on

Those clients **break WhatsApp's terms of service**. The consequences land on
the linked account, not on the server:

- **The number gets banned.** Not hypothetically — it is the normal outcome,
  sooner or later. In India a WhatsApp number is an identity: bank OTPs, work,
  family, and the class group you were trying to read.
- **A companion session reads everything.** Every chat, every group, every
  contact, every file on that account. Not only the school ones.
- **It breaks without warning** when the protocol changes.

So: **a throwaway number, in a group you created for testing.** Not a parent's
real account, and not a real class group — the other families in it have not
agreed to any of this, and their children are in those messages.

This is why the bridge is off by default, answers `404` rather than `401` when
disabled (it should not admit to existing), and is kept out of the OpenAPI
schema. It is a development tool. It is not a product feature, and shipping it
as one would mean charging users their WhatsApp accounts for a convenience.

For real households the forwarding flow is the answer: four taps, no risk, and
Meta has already verified the sender.

## Turning it on

```sh
FAMILYOS_WHATSAPP_BRIDGE_ENABLED=true
FAMILYOS_WHATSAPP_BRIDGE_SECRET=<a long random string>
```

Then give a member the linked number, as a guardian would:

```sh
curl -s localhost:8000/v1/household/members -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"display_name":"Test Parent","role":"adult","email":"t@example.com","phone":"919876543210"}'
```

## The payload

The contract is ours, not any client's — map the client's webhook onto this in
a few lines rather than teaching FamilyOS a third-party shape that changes
without notice.

```json
{
  "linked_phone": "919876543210",
  "id":           "3EB0ABC",
  "timestamp":    1790000000,
  "is_group":     true,
  "chat_name":    "Class II Parents",
  "author":       "919999999999",
  "author_name":  "Priya",
  "text":         "Sports day moved to 14 November 2026",
  "media": {
    "mimetype":    "application/pdf",
    "filename":    "circular.pdf",
    "data_base64": "..."
  }
}
```

`linked_phone` and `id` are required. The bytes ride along in `data_base64`, so
nothing calls out to Meta for media.

```sh
curl -s localhost:8000/v1/inbound/whatsapp/bridge \
  -H "X-FamilyOS-Bridge-Secret: $FAMILYOS_WHATSAPP_BRIDGE_SECRET" \
  -H 'content-type: application/json' -d @message.json
```

## Who a group message belongs to

A group message is not *from* a member. It is from whichever parent typed it,
and most of them are strangers to the household. Treating the author as the
sender would refuse nearly everything.

So it mirrors email: the member whose account is linked **received** it, and so
it is stored for them. The author, the group name and the client's id go into
`source`, the way an email's From and Message-ID do.

A group message is marked `forwarded`, which means claims read out of it carry
the hearsay penalty. That is correct — what the group relays is somebody's
retelling, and the school's own circular should outweigh it.

## A relay, in about thirty lines

Whatever client you use, it comes down to this:

```js
// Map your client's message event onto the contract above and POST it.
client.onMessage(async (m) => {
  const media = m.hasMedia
    ? { mimetype: m.mimetype, filename: m.filename,
        data_base64: await client.downloadMedia(m) }   // base64, no data: prefix
    : undefined;

  await fetch(`${FAMILYOS}/v1/inbound/whatsapp/bridge`, {
    method: "POST",
    headers: { "content-type": "application/json",
               "X-FamilyOS-Bridge-Secret": SECRET },
    body: JSON.stringify({
      linked_phone: LINKED_NUMBER,          // the number this client is logged in as
      id: m.id,
      timestamp: m.timestamp,
      is_group: m.isGroupMsg,
      chat_name: m.chat?.name,
      author: (m.author || m.from || "").split("@")[0],
      author_name: m.sender?.pushname,
      text: m.body,
      media,
    }),
  });
});
```

Replaying the same `id` stores nothing new, so a relay that restarts and
re-sends its backlog is safe.
