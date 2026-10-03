# tools

Not part of FamilyOS. Things used to decide whether to build something.

## `app_spike.py` — does it run on a virtualised Android?

The gate in front of any plan to reach mobile-only services (a school app, an
apartment app, a regional service) by driving a cloud Android device with a GUI
agent.

Play Integrity reports a device verdict to the app. An emulator cannot produce
`MEETS_DEVICE_INTEGRITY` — no verified boot, no hardware-backed attestation —
and forging one breaks Play's terms. An app that enforces it refuses, and then
no amount of agent accuracy matters. So this is worth half a day *before* the
month.

```sh
# 1. An AVD from a GOOGLE PLAY system image (not AOSP: without Play Services
#    there is nothing to attest and the run proves nothing). API 34+.
#    On Apple Silicon use arm64-v8a — closer to a real device, so a failure
#    there is conclusive.
python tools/app_spike.py --device        # check what you are on

# 2. Install a free "Play Integrity API Checker" from the Play Store and run
#    it by hand. Expect MEETS_BASIC_INTEGRITY at best and DEVICE to fail.
#    That confirms the emulator is a faithful negative control before you
#    blame any individual app.

# 3. The real list. Copy the example and replace every line.
cp tools/apps.example.txt apps.txt
python tools/app_spike.py apps.txt --open-play   # install pass, by hand
python tools/app_spike.py apps.txt --out spike-2026-10-03
```

Writes `report.md`, `results.csv`, `device.csv` and a screenshot per app.

### What it decides, and what it does not

It automates what a machine can be trusted with: resolve the launcher activity,
start it, wait, see whether the process is alive and in front, keep the
screenshot and any integrity-shaped log lines.

**It does not decide whether an app blocked you.** A block almost always looks
like a dialog *inside* a running app — "this device isn't supported" — which is
indistinguishable from a login screen without looking. The script narrows ten
apps to "these came up, now read the screenshots". Signing in, reading content
and attempting a payment are columns a person fills in.

### Scoring

Score **read-capable** — installs, launches, signs in, shows content. That is
what intake needs; paying through an app is a later and much harder question,
and many apps that block payment will still let you read.

| Of ten apps | Then |
|---|---|
| 5+ not read-capable | the rail is dead, and you spent half a day |
| 2 or fewer | fund the full POC |
| in between | scope the POC to the passers, against a smaller prize |

### What a pass is still not

- **Local ≠ hosted.** A hosted ARM fleet runs images a provider may have
  flagged. A local pass is necessary, not sufficient.
- **It says nothing about detection.** An app can allow the device and still
  flag the interaction pattern.
- **It says nothing about terms.** An app that works may still prohibit
  automated access.
- **It expires.** A pass today can be a fail at the next release — which is
  the argument for reaching the long tail this way rather than building the
  product on it.

Use throwaway test accounts, one per app. Never a real family's credentials,
and never a parent's account.

## `contrast.py` — do the themes hold?

The console is dark by default with a light alternative, and every colour in
`app.css` and `app.js` goes through a token — there is not one hex literal
outside the two palette blocks. That is what makes a second theme a list of
values rather than a hunt.

It is also what makes it checkable. This computes the WCAG contrast ratio for
every text-on-background pair the console actually uses, in both themes, and
fails if any falls below its floor (4.5:1 for text, 3:1 for faint).

```sh
python tools/contrast.py
```

A dark theme is easy to write and easy to get wrong in a way that only shows
up on somebody else's screen, which is the part a screenshot would not tell
you anyway. It caught `--faint` at 2.97:1 on a card.

Two values are deliberately **not** themed, and the tool prints them so the
exception stays visible:

- `--page` is white in both themes. A notice is a photograph of paper, and
  pdf.js paints only the page's content — it leaves the canvas transparent
  where the PDF declares no background, so without a white page the dark
  stage shows through and a notice becomes black text on near-black.
- `--amber-glow`, the quote highlight, lies on that page. Dimming it for dark
  would fade the highlight out of the one thing it exists to point at.
