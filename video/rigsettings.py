#!/usr/bin/env python3
"""rigsettings - put both phones on the SAME explicit camera settings, and prove it.

WHY THIS EXISTS. RigCam resets to its built-in defaults every time it restarts, and it
restarts on every phone reboot, every crash and every "the app got killed overnight". So
the rig quietly comes back at 720p on auto exposure - and auto exposure is the failure
mode that matters, because two cameras deciding their own exposure DRIFT APART over a set
and a cut between them jumps. Matching them by eye afterwards was tried and could not do
it: black level came within 5, mid-tones stayed 152 against 98.

The fix is not to lock auto (that only freezes whatever each camera happened to land on,
from two different starting points) but to give both the SAME NUMBERS, so they match by
construction.

    python rigsettings.py --check                    what is set right now
    python rigsettings.py --apply                    push the baseline to both
    python rigsettings.py --apply --wait 300         retry while the phones come up
    python rigsettings.py --apply --start-rigcam     start the app first if it is down

⚠️ NOT wired into vcambridge on purpose. The bridge reconnects whenever the stream drops,
and re-applying settings there would clobber a zoom or an EV change made from Ríastrad
seconds earlier - a control surface that silently undoes you is worse than one that does
nothing. This runs at startup, and when you ask for it.
"""
import argparse
import json
import sys
import time
import subprocess
import urllib.parse
import urllib.request

ADB = r"C:\Users\mccul\Android\Sdk\platform-tools\adb.exe"

# The show baseline. Explicit on BOTH phones, or they are not matched - see the module
# docstring. Shutter is sent in nanoseconds because that is what the sensor takes:
# 16666666 ns = 1/60 s, which is the slowest shutter that still stops motion on a hand.
BASELINE = {
    "resolution": "1920x1080",
    "fps": "30",
    # ⚠️ BITS per second on the way IN, kbps on the way OUT. /api/set coerces this into
    # 500_000..20_000_000 and /api/state reports it as `bitrateKbps` - so sending the 16000
    # that the state field shows silently gets you the 500 kbps FLOOR, at 1080p, and the
    # picture just looks bad with every field reporting success.
    "bitrate": "16000000",
    "iso": "800",
    "shutterNs": "16666666",
    "manualExposure": "true",
    "wbR": "1.8",
    "wbG": "1.0",
    "wbB": "1.9",
    "manualWb": "true",
}

# What /api/state calls each of the above, so a write can be read back and checked rather
# than assumed. A settings call that reports success proves the REQUEST was accepted, not
# that the camera honoured it - RigCam itself warns that a rejected shutter is dropped
# silently, taking the whole request with it.
VERIFY = {
    "resolution": lambda s: s.get("resolution"),
    "fps": lambda s: str(s.get("fps")),
    "bitrate": lambda s: str(int(s.get("encoder", {}).get("bitrateKbps", 0)) * 1000),
    "iso": lambda s: str(s.get("manual", {}).get("iso")),
    "shutterNs": lambda s: str(s.get("manual", {}).get("shutterNs")),
    "manualExposure": lambda s: str(s.get("manual", {}).get("exposure")).lower(),
    "wbR": lambda s: str(s.get("manual", {}).get("wbR")),
    "wbG": lambda s: str(s.get("manual", {}).get("wbG")),
    "wbB": lambda s: str(s.get("manual", {}).get("wbB")),
    "manualWb": lambda s: str(s.get("manual", {}).get("wb")).lower(),
}


def get_state(url, timeout=6):
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/state", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def apply(url, settings, timeout=15):
    q = urllib.parse.urlencode(settings)
    with urllib.request.urlopen(url.rstrip("/") + "/api/set?" + q, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def start_rigcam(serial):
    """Launch the app over adb. It starts fine behind the keyguard now that its
    permissions are granted, so this does not need anyone to unlock the phone."""
    if not serial:
        return "no serial configured"
    subprocess.run([ADB, "-s", serial, "shell", "am", "start", "-n",
                    "online.awen.rigcam/.MainActivity"],
                   capture_output=True, text=True, timeout=30)
    return "am start sent"


def close_enough(want, got):
    """Floats come back as 1.8 or 1.80 depending on the field, so compare numerically
    where both sides parse as numbers and textually otherwise."""
    if got is None:
        return False
    try:
        return abs(float(want) - float(got)) < 1e-6
    except (TypeError, ValueError):
        return str(want).lower() == str(got).lower()


def check(label, url):
    s = get_state(url)
    if s is None:
        print(f"  {label:10s} NOT ANSWERING at {url}")
        return None
    bad = []
    for k, want in BASELINE.items():
        got = VERIFY[k](s)
        if not close_enough(want, got):
            bad.append(f"{k}={got} (want {want})")
    res = s.get("resolution")
    m = s.get("manual", {})
    mode = "manual" if (m.get("exposure") and m.get("wb")) else "AUTO exp/WB"
    print(f"  {label:10s} {res} @{s.get('fps')} {mode}"
          + ("" if not bad else "\n" + "".join(f"      drift: {b}\n" for b in bad)).rstrip())
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phone", action="append", default=[],
                    metavar="LABEL=URL", help='repeatable, e.g. --phone "Pixel 6=http://192.168.1.50:8090"')
    ap.add_argument("--serial", action="append", default=[],
                    metavar="LABEL=SERIAL", help="adb serial per phone, for --start-rigcam")
    ap.add_argument("--apply", action="store_true", help="push the baseline (default is --check)")
    ap.add_argument("--check", action="store_true", help="report only")
    ap.add_argument("--start-rigcam", action="store_true",
                    help="if a phone is not answering, start the app over adb first")
    ap.add_argument("--wait", type=int, default=0,
                    help="seconds to keep retrying while a phone is still coming up")
    args = ap.parse_args()

    phones = []
    for p in args.phone:
        if "=" not in p:
            sys.exit(f"--phone wants LABEL=URL, got {p!r}")
        label, url = p.split("=", 1)
        phones.append((label.strip(), url.strip()))
    if not phones:
        # the rig's own two, so the scheduled task needs no arguments
        phones = [("Pixel 6", "http://192.168.1.50:8090"),
                  ("Pixel 8", "http://127.0.0.1:8091")]

    serials = dict(s.split("=", 1) for s in args.serial if "=" in s)
    if not serials:
        serials = {"Pixel 6": "192.168.1.50:5555", "Pixel 8": "38021FDJH004KS"}

    if args.check and not args.apply:
        print("current:")
        drift = [check(l, u) for l, u in phones]
        return 0 if all(d == [] for d in drift) else 1

    deadline = time.time() + args.wait
    failed = []
    for label, url in phones:
        while True:
            if get_state(url) is not None:
                break
            if args.start_rigcam:
                print(f"  {label}: not answering - {start_rigcam(serials.get(label))}")
                time.sleep(8)
                if get_state(url) is not None:
                    break
            if time.time() >= deadline:
                print(f"  {label:10s} NOT ANSWERING at {url} - skipped")
                failed.append(label)
                break
            time.sleep(5)
        else:
            continue
        if label in failed:
            continue
        r = apply(url, BASELINE)
        print(f"  {label:10s} applied {len(r.get('applied', []))} settings")

    # ⚠️ Read it back. RigCam drops a whole request if one value is out of range for the
    # sensor, and the two phones do NOT have the same ranges (Pixel 6 ISO 44-11377,
    # Pixel 8 21-10666), so "it worked on one" proves nothing about the other.
    time.sleep(4)
    print("verified:")
    drift = [check(l, u) for l, u in phones]
    ok = all(d == [] for d in drift) and not failed
    print("matched and on baseline" if ok else "NOT on baseline - see the drift above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
