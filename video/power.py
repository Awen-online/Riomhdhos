#!/usr/bin/env python3
"""
power - put the phones to sleep so they recharge, and wake them for a show.

⚠️ WHY THIS EXISTS: both phones DRAIN while acting as cameras, and neither port negotiates
more than 500 mA. Measured with OBS closed and nothing consuming either feed:

    Pixel 8   14%   -214 mA    2.6 h left
    Pixel 6   43%   -574 mA    3.1 h left

Closing OBS barely helped, because the load is not OBS - it is the phones themselves:
their screens held awake, their camera pipelines running, and the encoder on the WiFi one.
A camera that cannot survive a set is not a camera, so this turns all of that off in one
action and turns it back on in another.

    python power.py sleep     # let them recharge
    python power.py show      # back to streaming
    python power.py status

WHAT SLEEP ACTUALLY DOES, in the order that matters:

  1. stops the vcam bridge, so nothing pulls the WiFi stream and keeps its encoder alive
  2. asks every configured phone to sleep over HTTP, then force-stops RigCam on the ones
     adb can see
  3. turns High Quality mode OFF on the wired phone - it disables power optimisation and
     measured -232 mA -> -404 mA when it was switched on
  4. clears stay_on_while_plugged_in, which was pinned to 3 for ADB work and is why both
     screens have been lit continuously
  5. puts both screens to sleep now rather than waiting for a timeout

⚠️ STEP 2 USED TO BE ADB ONLY, AND THAT MISSED THE PHONE IT MATTERED MOST FOR. The loop
walks `adb devices`; the WiFi phone is deliberately not on adb, so every "sleep" stopped its
bridge and left its camera and encoder running - the phone measured at -574 mA, drained all
night with nothing consuming the stream. RigCam now serves /api/sleep and /api/wake, which
is a channel that phone actually has. Pass it with --phone, the same LABEL=URL the dashboard
uses; an unreachable phone is reported and skipped rather than failing the whole command.

⚠️ Step 4 needs the phone UNLOCKED, because it is a UI tap on DeviceAsWebcam - there is no
API for it. If the phone is locked it is skipped and reported, rather than failing the
whole operation: the other four steps are worth having on their own.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# WARNING: WINDOWS SPAWNS A CONSOLE WINDOW FOR EVERY CHILD CONSOLE PROCESS. adb.exe and
# powershell.exe are console applications, so each call flashed a black terminal on the
# desktop - and opening the Cams tab fires several at once, which is unusable during a
# show. CREATE_NO_WINDOW keeps them headless; it does not exist off Windows, hence getattr.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


ADB = r"C:\Users\mccul\Android\Sdk\platform-tools\adb.exe"
UVCZOOM = Path(__file__).with_name("uvczoom.py")
# One bridge per phone: Windows has a single OBS Virtual Camera, so the second feeds Unity
# Capture instead. Both have to stop, or the phone whose bridge is still pulling keeps its
# encoder alive and never actually sleeps.
BRIDGE_TASKS = ("Riomhdhos vcam bridge", "Riomhdhos vcam bridge P8")
RIGCAM = "online.awen.rigcam"


def rigcam(url, path, timeout=8):
    """GET one of RigCam's control endpoints. Returns a short status string.

    ⚠️ NEVER RAISES. A phone that is off, asleep at the OS level or on a different network
    must not take the whole sleep down with it - the bridges still need stopping and the
    other phone still needs putting down. Report and carry on.
    """
    try:
        with urllib.request.urlopen(url.rstrip("/") + path, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
        if "note" in body:
            return body["note"]
        if "error" in body:
            return "refused: " + str(body["error"])
        return "dormant" if body.get("dormant") else "awake"
    except urllib.error.HTTPError as e:
        # ⚠️ 404 IS NOT "UNREACHABLE", IT IS AN OLD BUILD - the phone answered, it just has
        # no /api/sleep. Worth saying plainly, because a phone that cannot be updated
        # without a USB cable can sit on the old APK for a long time and this is the only
        # place that difference shows up.
        if e.code == 404:
            return "not supported - RigCam predates /api/sleep, reinstall over USB"
        return f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return f"unreachable ({getattr(e, 'reason', e)})"
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def sh(*args, timeout=25):
    try:
        return subprocess.run([ADB, *args], capture_output=True, text=True,
                              timeout=timeout, creationflags=NO_WINDOW).stdout.strip()
    except Exception as e:
        return f"<{type(e).__name__}>"


def devices():
    out = sh("devices")
    return [l.split()[0] for l in out.splitlines()[1:]
            if l.strip() and l.split()[-1] == "device"]


def model(serial):
    return sh("-s", serial, "shell", "getprop", "ro.product.model") or serial


def unlocked(serial):
    return "isKeyguardShowing=false" in sh("-s", serial, "shell", "dumpsys", "window")


def task(action, name):
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"{action}-ScheduledTask -TaskName '{name}' "
                        f"-ErrorAction SilentlyContinue"],
                       capture_output=True, timeout=30, creationflags=NO_WINDOW)
        return True
    except Exception:
        return False


def hq_state(serial):
    """'on', 'off', or None if it cannot be read (locked phone, no UI)."""
    try:
        r = subprocess.run([sys.executable, str(UVCZOOM), "--serial", serial, "--state"],
                           capture_output=True, text=True, timeout=45, creationflags=NO_WINDOW).stdout
    except Exception:
        return None
    if "Switch High Quality off" in r:
        return "on"            # the button offers to switch it OFF, so it is ON
    if "Switch High Quality on" in r:
        return "off"
    return None


def hq_off(serial):
    if not unlocked(serial):
        return "skipped (phone locked - HQ is a UI tap)"
    if hq_state(serial) != "on":
        return "already off"
    subprocess.run([sys.executable, str(UVCZOOM), "--serial", serial, "--hq"],
                   capture_output=True, timeout=45, creationflags=NO_WINDOW)
    return "turned off"


def sleep_mode(wired_serial=None, phones=None):
    log = []
    for t in BRIDGE_TASKS:
        log.append((t.replace("Riomhdhos ", ""), "stopped" if task("Stop", t) else "could not stop"))

    # ⚠️ HTTP BEFORE ADB, AND OVER EVERY PHONE - this is the half that reaches the WiFi one.
    # For a phone adb can also see this is redundant (the force-stop below is stronger), and
    # redundant is fine: it costs one request and keeps the two paths from diverging.
    for label, url in (phones or {}).items():
        log.append((label, "camera released: " + rigcam(url, "/api/sleep")))

    for s in devices():
        m = model(s)
        # ⚠️ BOTH PHONES RUN RIGCAM NOW. This used to stop RigCam on the WiFi phone only and
        # tap High Quality off on the wired one, because the wired one reached OBS through
        # DeviceAsWebcam. It does not any more - it runs RigCam over an adb forward - so
        # skipping it left its camera and encoder running through every "sleep", which is
        # exactly the drain this command exists to stop, on the phone that runs out first.
        sh("-s", s, "shell", "am", "force-stop", RIGCAM)
        log.append((m, "RigCam stopped"))
        if s == wired_serial and unlocked(s):
            # Harmless if DeviceAsWebcam is not in use, and worth doing in case it is.
            st = hq_off(s)
            if st and st != "already off":
                log.append((m, "high quality: " + st))
        # ⚠️ THIS is the one that has been costing the most. stay_on_while_plugged_in was
        # pinned to 3 so ADB work would not be interrupted by the lockscreen, and it has
        # been holding both displays lit ever since.
        sh("-s", s, "shell", "settings", "put", "global", "stay_on_while_plugged_in", "0")
        sh("-s", s, "shell", "input", "keyevent", "KEYCODE_SLEEP")
        log.append((m, "screen released and asleep"))
    return log


def show_mode(wired_serial=None, phones=None):
    log = []
    for s in devices():
        m = model(s)
        sh("-s", s, "shell", "settings", "put", "global", "stay_on_while_plugged_in", "3")
        sh("-s", s, "shell", "input", "keyevent", "KEYCODE_WAKEUP")
        log.append((m, "awake, screen held on"))
        sh("-s", s, "shell", "am", "start", "-n", f"{RIGCAM}/.MainActivity")
        log.append((m, "RigCam started"))
    # ⚠️ WAKE OVER HTTP TOO, AND AFTER `am start`. An adb phone was force-stopped by sleep,
    # so it comes back fresh and awake and this is a harmless no-op. The WiFi phone was only
    # made dormant - its app never stopped - so `am start` cannot reach it and this call is
    # the only thing that reopens its camera.
    time.sleep(3)
    for label, url in (phones or {}).items():
        log.append((label, "camera reopened: " + rigcam(url, "/api/wake")))

    # ⚠️ The bridge last, once RigCam has something to serve - it retries anyway, but
    # starting it into a dead stream just burns a reconnect cycle.
    for t in BRIDGE_TASKS:
        log.append((t.replace("Riomhdhos ", ""), "started" if task("Start", t) else "could not start"))
    # ⚠️ High Quality is deliberately NOT restored. It costs ~170 mA and the phone that
    # carries it is the one that runs out first; turning it back on should be a decision,
    # not a side effect.
    log.append(("note", "high quality left OFF - re-enable from the Cams tab if wanted"))
    return log


def status(phones=None):
    """What each phone is actually doing, over whichever channel can say.

    ⚠️ THE TWO CHANNELS ANSWER DIFFERENT QUESTIONS AND NEITHER IS ENOUGH. adb knows the
    things only the OS knows - stay_on_while_plugged_in, whether the screen is lit, whether
    the process exists - but the WiFi phone is deliberately not on adb, so it used to be
    absent from this list entirely, which reads exactly like "fine". HTTP knows whether the
    CAMERA is running, which is the thing that actually drains the battery, and it is the
    only channel the WiFi phone has. So: adb facts first, then let HTTP fill in the camera.
    """
    rows = []
    for s_ in devices():
        stay = sh("-s", s_, "shell", "settings", "get", "global",
                  "stay_on_while_plugged_in")
        awake = "Awake" in sh("-s", s_, "shell", "dumpsys", "power")
        rig = bool(sh("-s", s_, "shell", "pidof", RIGCAM))
        rows.append({"serial": s_, "model": model(s_), "stayOn": stay,
                     "screenAwake": awake, "rigcam": rig,
                     "camera": "running" if rig else "stopped"})

    for label, url in (phones or {}).items():
        st = rigcam(url, "/api/state")
        row = next((r for r in rows if r["model"] == label), None)
        if row is None:
            rows.append({"serial": "-", "model": label, "stayOn": "-",
                         "screenAwake": None, "rigcam": st in ("awake", "dormant"),
                         "camera": st})
        elif st in ("awake", "dormant"):
            # ⚠️ ONLY overwrite with a REAL answer. A force-stopped app refuses the
            # connection, and replacing adb's plain "stopped" with the socket error was
            # strictly worse - it also threw away stay_on and the screen state.
            row["camera"] = st
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["sleep", "show", "status"])
    ap.add_argument("--wired-serial", default=None,
                    help="the USB phone; it keeps its webcam, the other runs RigCam")
    ap.add_argument("--phone", action="append", default=[], metavar="LABEL=URL",
                    help="a RigCam phone to sleep/wake over HTTP, repeatable. "
                         "Same LABEL=URL the dashboard takes. Required for any phone that "
                         "is not on adb - without it that phone is never actually slept.")
    args = ap.parse_args()

    phones = {}
    for spec in args.phone:
        if "=" not in spec:
            ap.error(f"--phone wants LABEL=URL, got {spec!r}")
        label, url = spec.split("=", 1)
        phones[label.strip()] = url.strip().rstrip("/")

    if args.mode == "status":
        for r in status(phones):
            screen = "-" if r["screenAwake"] is None else                      ("awake" if r["screenAwake"] else "asleep")
            print(f"  {r['model']:<9} stay_on={r['stayOn']:<4} "
                  f"screen={screen:<6} camera={r['camera']}")
        return
    fn = sleep_mode if args.mode == "sleep" else show_mode
    for who, what in fn(args.wired_serial, phones):
        print(f"  {who:<12} {what}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
