#!/usr/bin/env python3
"""Does this app run on a virtualised Android at all?

The gate in front of any plan to reach mobile-only services by driving a cloud
Android device. Play Integrity reports a device verdict to the app, and an
emulator cannot produce MEETS_DEVICE_INTEGRITY -- it has no verified boot and
no hardware-backed attestation, and forging one breaks Play's terms. An app
that enforces it refuses, and then no amount of GUI-agent accuracy matters.

So before funding that work: take the real apps, put them on a stock emulator,
and find out which ones come up.

What this does and does not decide
----------------------------------
It automates the part a machine can be trusted with -- resolve the launcher
activity, start it, wait, see whether the process is alive and in front, and
keep the screenshot and any integrity-shaped log lines as evidence.

It does **not** decide whether an app blocked you. A block almost always looks
like a dialog *inside* a running app ("this device isn't supported", "rooted
device detected"), which from here is indistinguishable from a login screen.
The script's job is to narrow ten apps down to "these came up, now go and look
at the screenshots". Signing in, reading content and attempting a payment are
columns a person fills in.

It also proves less than it appears to. A local pass is necessary, not
sufficient: a hosted ARM fleet runs images a provider may have flagged. It
says nothing about whether automation is *detected* once you are inside, and
nothing at all about whether the app's terms permit any of this. And it
expires -- a pass today can be a fail at the next release, which is the
argument for reaching the long tail this way rather than building on it.

Usage
-----
    # one package per line; "pkg  Some label" or "pkg,Some label" also works
    python tools/app_spike.py apps.txt
    python tools/app_spike.py apps.txt --out spike-2026-10-03
    python tools/app_spike.py apps.txt --open-play      # install pass, by hand
    python tools/app_spike.py --device                  # just describe the device

Needs `adb` on PATH and exactly one device attached (or -s). Nothing is
installed, signed into or paid for by this script.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import pathlib
import re
import subprocess
import sys
import time

# Log lines worth keeping when an app comes up and then does nothing useful.
# Not a verdict -- a pointer at where to look.
INTEREST = re.compile(
    r"integrity|attest|safetynet|droidguard|rootbeer|\bsu\b|magisk|"
    r"emulator|qemu|goldfish|ranchu|device not supported|unsupported device",
    re.I)
FATAL = re.compile(r"FATAL EXCEPTION|ANR in|Force finishing activity")

VERDICTS = {
    "running": "came up and stayed up -- read the screenshot",
    "not_foreground": "process alive but something else is in front",
    "crashed": "died on launch",
    "launch_failed": "no launcher activity, or am start refused",
    "not_installed": "not on the device",
}


def adb(serial: str | None, *args: str, binary: bool = False, timeout: int = 60):
    cmd = ["adb"] + (["-s", serial] if serial else []) + list(args)
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError:
        sys.exit("adb is not on PATH. Install Android platform-tools.")
    except subprocess.TimeoutExpired:
        return b"" if binary else ""
    if binary:
        return done.stdout
    return (done.stdout + done.stderr).decode("utf-8", "replace").strip()


def devices() -> list[str]:
    out = adb(None, "devices")
    found = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            found.append(parts[0])
    return found


def describe(serial: str | None) -> dict:
    """What it ran on. Recorded in the report, because "it worked" means
    nothing without saying on what."""
    props = ("ro.product.model", "ro.product.manufacturer", "ro.build.version.release",
             "ro.build.version.sdk", "ro.product.cpu.abi", "ro.build.fingerprint",
             "ro.kernel.qemu", "ro.boot.verifiedbootstate", "ro.build.tags")
    about = {p: adb(serial, "shell", "getprop", p) for p in props}
    about["has_play_store"] = "yes" if adb(serial, "shell", "pm", "path", "com.android.vending") else "NO"
    fingerprint = (about.get("ro.build.fingerprint") or "").lower()
    about["looks_emulated"] = "yes" if (
        about.get("ro.kernel.qemu") == "1"
        or any(k in fingerprint for k in ("generic", "emulator", "sdk_gphone", "ranchu", "goldfish"))
    ) else "no"
    return about


def read_list(path: pathlib.Path) -> list[tuple[str, str]]:
    apps = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        package, _, label = (line.replace(",", " ", 1)).partition(" ")
        apps.append((package.strip(), label.strip() or package.strip()))
    return apps


def installed(serial: str | None, package: str) -> bool:
    return bool(adb(serial, "shell", "pm", "path", package).startswith("package:"))


def launcher(serial: str | None, package: str) -> str | None:
    out = adb(serial, "shell", "cmd", "package", "resolve-activity", "--brief", package)
    for line in reversed(out.splitlines()):
        if "/" in line and not line.lower().startswith("priority"):
            return line.strip()
    return None


def foreground(serial: str | None) -> str:
    out = adb(serial, "shell", "dumpsys", "activity", "activities")
    for pattern in (r"mResumedActivity.*\{.*? (\S+/\S+)", r"topResumedActivity.*\{.*? (\S+/\S+)"):
        found = re.search(pattern, out)
        if found:
            return found.group(1)
    window = adb(serial, "shell", "dumpsys", "window")
    found = re.search(r"mCurrentFocus=Window\{.*? (\S+/\S+)", window)
    return found.group(1) if found else ""


def probe(serial: str | None, package: str, label: str, *, settle: int, shots: pathlib.Path) -> dict:
    row = {"package": package, "label": label, "verdict": "", "detail": "",
           "foreground": "", "screenshot": "", "log": ""}
    if not installed(serial, package):
        row["verdict"] = "not_installed"
        return row

    component = launcher(serial, package)
    adb(serial, "shell", "am", "force-stop", package)
    adb(serial, "logcat", "-c")
    if component is None:
        row["verdict"] = "launch_failed"
        row["detail"] = "no launcher activity"
        return row

    started = adb(serial, "shell", "am", "start", "-W", "-n", component, timeout=90)
    if "Error" in started or "Exception" in started:
        row["verdict"] = "launch_failed"
        row["detail"] = started.splitlines()[0][:160] if started else "am start said nothing"
        return row

    time.sleep(settle)
    alive = bool(adb(serial, "shell", "pidof", package))
    front = foreground(serial)
    row["foreground"] = front

    shot = shots / f"{package}.png"
    image = adb(serial, "exec-out", "screencap", "-p", binary=True, timeout=90)
    if image:
        shot.write_bytes(image)
        row["screenshot"] = shot.name

    log = adb(serial, "logcat", "-d", "-v", "brief", timeout=90)
    keep = [ln for ln in log.splitlines() if (INTEREST.search(ln) or FATAL.search(ln)) and len(ln) < 400]
    row["log"] = " | ".join(keep[-6:])

    if not alive or any(FATAL.search(ln) and package in ln for ln in log.splitlines()):
        row["verdict"] = "crashed"
    elif front.startswith(package + "/"):
        row["verdict"] = "running"
    else:
        row["verdict"] = "not_foreground"
    return row


def report(rows: list[dict], about: dict, out: pathlib.Path) -> str:
    counts = {v: sum(1 for r in rows if r["verdict"] == v) for v in VERDICTS}
    came_up = counts["running"]
    lines = [
        "# Does it run on a virtualised Android?", "",
        f"{len(rows)} apps, {dt.datetime.now().isoformat(timespec='seconds')}.", "",
        "## What it ran on", "",
        f"- {about.get('ro.product.manufacturer')} {about.get('ro.product.model')}, "
        f"Android {about.get('ro.build.version.release')} (SDK {about.get('ro.build.version.sdk')}), "
        f"{about.get('ro.product.cpu.abi')}",
        f"- Play Store present: {about['has_play_store']}",
        f"- Looks emulated: {about['looks_emulated']}  "
        f"(verifiedbootstate={about.get('ro.boot.verifiedbootstate') or 'unset'}, "
        f"build tags={about.get('ro.build.tags') or 'unset'})",
        f"- Fingerprint: `{about.get('ro.build.fingerprint')}`", "",
    ]
    if about["has_play_store"] == "NO":
        lines += ["> **This image has no Play Store, so there is nothing to attest and this run proves "
                  "nothing.** Rebuild the AVD from a *Google Play* system image.", ""]
    if about["looks_emulated"] == "no":
        lines += ["> **This does not look like an emulator.** If it is a real phone, every pass below is "
                  "expected and says nothing about a virtual device.", ""]

    lines += ["## Which came up", "",
              "| App | Package | Verdict | In front | Shot | Log pointers |",
              "|---|---|---|---|---|---|"]
    order = list(VERDICTS)
    for r in sorted(rows, key=lambda r: order.index(r["verdict"]) if r["verdict"] in order else 9):
        lines.append(f"| {r['label']} | `{r['package']}` | **{r['verdict']}** | `{r['foreground'] or '--'}` "
                     f"| {r['screenshot'] or '--'} | {(r['log'] or '')[:120].replace('|', '/') or '--'} |")

    lines += ["", "## Now do this by hand", "",
              f"{came_up} of {len(rows)} came up. The script cannot tell a login screen from "
              "\"this device isn't supported\", so open each screenshot in `" + out.name + "/shots/` "
              "and fill in the rest:", "",
              "| App | Signed in | Content readable | Payment works | Read-capable | Act-capable |",
              "|---|---|---|---|---|---|"]
    for r in rows:
        if r["verdict"] in ("running", "not_foreground"):
            lines.append(f"| {r['label']} | | | | | |")

    lines += ["", "## Reading the result", "",
              "Score **read-capable** -- installs, launches, signs in, shows content. That is what "
              "intake needs; paying through an app is a later and much harder question.", "",
              "- 5 or more of 10 not read-capable: the rail is dead, and you spent half a day.",
              "- 2 or fewer: fund the full POC.",
              "- In between: scope the POC to the apps that passed, and price it against a smaller prize.", "",
              "And remember what this does not say: a local pass is not a pass on a hosted fleet, "
              "nothing here tests whether automation is detected once you are inside, and nothing here "
              "tests whether the app's terms allow it.", ""]
    for v, meaning in VERDICTS.items():
        lines.append(f"- `{v}` -- {meaning}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("apps", nargs="?", type=pathlib.Path, help="file of package names, one per line")
    parser.add_argument("-s", "--serial", help="adb device serial, if more than one is attached")
    parser.add_argument("--out", type=pathlib.Path, help="where to write the report (default: app-spike-<date>)")
    parser.add_argument("--settle", type=int, default=8, help="seconds to wait after launch (default 8)")
    parser.add_argument("--open-play", action="store_true",
                        help="open the Play listing for each app not installed, then stop")
    parser.add_argument("--device", action="store_true", help="describe the attached device and stop")
    args = parser.parse_args()

    attached = devices()
    serial = args.serial
    if serial is None:
        if not attached:
            return print("No device. Start an emulator built from a *Google Play* system image, "
                         "then run `adb devices`.") or 1
        if len(attached) > 1:
            return print(f"More than one device: {', '.join(attached)}. Pick one with -s.") or 1
        serial = attached[0]

    about = describe(serial)
    if args.device:
        for key, value in about.items():
            print(f"{key:32} {value}")
        return 0
    if args.apps is None:
        parser.error("give a file of package names, or --device")
    if not args.apps.is_file():
        return print(f"{args.apps} is not a file.") or 1

    apps = read_list(args.apps)
    if not apps:
        return print(f"{args.apps} has no package names in it.") or 1

    if args.open_play:
        missing = [(p, label) for p, label in apps if not installed(serial, p)]
        if not missing:
            print("All of them are already installed.")
            return 0
        print(f"{len(missing)} not installed. Opening each Play listing -- install it, then press Enter.\n")
        for package, label in missing:
            adb(serial, "shell", "am", "start", "-a", "android.intent.action.VIEW",
                "-d", "market://details?id=" + package)
            input(f"  {label} ({package}) -- Enter when done, or skip: ")
        return 0

    out = args.out or pathlib.Path(f"app-spike-{dt.date.today().isoformat()}")
    shots = out / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    if about["has_play_store"] == "NO":
        print("! This image has no Play Store, so there is nothing to attest. "
              "Rebuild the AVD from a Google Play system image.\n")

    rows = []
    for i, (package, label) in enumerate(apps, start=1):
        print(f"[{i}/{len(apps)}] {label} ({package}) ... ", end="", flush=True)
        row = probe(serial, package, label, settle=args.settle, shots=shots)
        print(row["verdict"] + (f" -- {row['detail']}" if row["detail"] else ""))
        rows.append(row)
        adb(serial, "shell", "am", "force-stop", package)

    with (out / "results.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (out / "report.md").write_text(report(rows, about, out))
    (out / "device.csv").write_text("\n".join(f"{k},{v}" for k, v in about.items()) + "\n")

    came_up = sum(1 for r in rows if r["verdict"] == "running")
    print(f"\n{came_up} of {len(rows)} came up. Report: {out / 'report.md'}")
    print(f"Screenshots: {shots}  <- the actual evidence; the script cannot read them for you.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
