#!/usr/bin/env python3
"""
uvczoom - drive the phone's DeviceAsWebcam controls from the host, over ADB.

WHY THIS EXISTS: the USB webcam path gives the host NO camera control at all. `camctl.py`
proved it - `IAMCameraControl` reports zoom, focus, pan and tilt as unsupported, and the
three properties that answer have ranges whose min equals max. And RigCam cannot help here,
because RigCam and the UVC webcam service both want the camera and Android allows one
consumer. So for the WIRED camera, poking its own UI is the only route there is.

⚠️ THE COORDINATES ARE RESOLVED AT RUNTIME, NEVER HARDCODED. `uiautomator dump` returns
every node with its resource-id and bounds, so this looks the control up by id and taps the
centre of wherever it currently is. Hardcoded pixels are what makes UI automation rot: they
break on a layout change, a different phone, or a rotation, and they fail SILENTLY by
tapping whatever moved into that spot instead.

    python uvczoom.py --state          # what the UI currently offers, and what is selected
    python uvczoom.py 0.5              # ULTRAWIDE (125.8 deg on the Pixel 8)
    python uvczoom.py 1.0 / 2.0
    python uvczoom.py --front / --back
    python uvczoom.py --hq             # toggle High Quality mode (raises a warning; see below)

⚠️ REQUIRES THE PHONE UNLOCKED AND AWAKE. There is no way around this - it is a UI tap.
`adb shell settings put global stay_on_while_plugged_in 3` keeps it awake on the cable
(revert with 0); the initial unlock still has to be done by hand.
"""

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# WARNING: WINDOWS SPAWNS A CONSOLE WINDOW FOR EVERY CHILD CONSOLE PROCESS. adb.exe and
# powershell.exe are console applications, so each call flashed a black terminal on the
# desktop - and opening the Cams tab fires several at once, which is unusable during a
# show. CREATE_NO_WINDOW keeps them headless; it does not exist off Windows, hence getattr.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


ADB = r"C:\Users\mccul\Android\Sdk\platform-tools\adb.exe"
PREVIEW = "com.android.DeviceAsWebcam/com.android.deviceaswebcam.DeviceAsWebcamPreview"

# resource-id suffix -> what it does
ZOOM_IDS = {"0.5": "zoom_ui_toggle_option_low",
            "1.0": "zoom_ui_toggle_option_middle",
            "2.0": "zoom_ui_toggle_option_high"}


def adb(*args, serial=None):
    cmd = [ADB] + (["-s", serial] if serial else []) + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW).stdout


def only_device():
    """The single attached device, or an error.

    ⚠️ NEVER SILENTLY PICK THE FIRST. With both phones plugged in this returned whichever
    adb happened to list first, and the dashboard's *wired camera* controls drove the WRONG
    PHONE - launching DeviceAsWebcam on the WiFi camera, which stole the camera from RigCam
    and took its stream down. Guessing was worse than failing, because it failed somewhere
    else entirely.
    """
    out = adb("devices")
    devs = [l.split()[0] for l in out.splitlines()[1:]
            if l.strip() and l.split()[-1] == "device"]
    if not devs:
        sys.exit("no adb device. Is USB debugging on and the cable in?")
    if len(devs) > 1:
        sys.exit("more than one device attached (" + ", ".join(devs) +
                 ") - pass --serial to say which. Refusing to guess.")
    return devs[0]


def dump(serial):
    """The current view hierarchy as {resource-id-suffix: (node-attrs, (cx, cy))}."""
    adb("shell", "uiautomator", "dump", "/sdcard/ui.xml", serial=serial)
    tmp = Path(tempfile.gettempdir()) / "uvczoom_ui.xml"
    adb("pull", "/sdcard/ui.xml", str(tmp), serial=serial)
    if not tmp.exists():
        sys.exit("uiautomator dump failed - is the screen on and unlocked?")
    xml = tmp.read_text(encoding="utf-8", errors="replace")

    nodes = {}
    for tag in re.findall(r"<node[^>]*>", xml):
        def g(k):
            m = re.search(k + r'="([^"]*)"', tag)
            return m.group(1) if m else ""
        rid = g("resource-id").split("/")[-1]
        if not rid:
            continue
        b = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", g("bounds"))
        if not b:
            continue
        x1, y1, x2, y2 = (int(v) for v in b.groups())
        nodes[rid] = ({"text": g("text"), "desc": g("content-desc"),
                       "clickable": g("clickable"), "bounds": g("bounds")},
                      ((x1 + x2) // 2, (y1 + y2) // 2))
    return nodes


def ensure_preview(serial):
    """Bring the webcam preview up, and clear the immersive-mode cling if it appears."""
    if "isKeyguardShowing=false" not in adb("shell", "dumpsys", "window", serial=serial):
        sys.exit("the phone is locked - unlock it first (this is a UI tap, there is no "
                 "way around that)")
    adb("shell", "am", "start", "-n", PREVIEW, serial=serial)
    nodes = dump(serial)
    # ⚠️ First launch shows a full-screen "Got it" cling that covers the whole UI. Without
    # dismissing it every lookup below finds nothing and the tool looks broken.
    if "ok" in nodes and "immersive_cling_title" in nodes:
        adb("shell", "input", "tap", *map(str, nodes["ok"][1]), serial=serial)
        nodes = dump(serial)
    # ⚠️ Toggling High Quality raises a confirmation dialog which then covers the whole UI,
    # so every later lookup finds nothing. Acknowledge it, but deliberately do NOT tick
    # "don't show again" - the warning is real (it disables power optimisation, and on the
    # Pixel 8 drain measured -232 mA -> -404 mA), and a human should keep seeing it.
    if "hq_warning_ack_button" in nodes:
        adb("shell", "input", "tap", *map(str, nodes["hq_warning_ack_button"][1]),
            serial=serial)
        nodes = dump(serial)
    return nodes


def tap(serial, nodes, rid, label):
    if rid not in nodes:
        sys.exit(f"'{rid}' is not on screen. Present: {', '.join(sorted(nodes))}")
    x, y = nodes[rid][1]
    adb("shell", "input", "tap", str(x), str(y), serial=serial)
    print(f"tapped {label}  ({rid} at {x},{y})")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("zoom", nargs="?", choices=list(ZOOM_IDS),
                    help="0.5 selects the ultrawide")
    ap.add_argument("--front", action="store_true")
    ap.add_argument("--back", action="store_true")
    ap.add_argument("--hq", action="store_true", help="toggle High Quality mode")
    ap.add_argument("--state", action="store_true")
    ap.add_argument("--serial", default=None)
    args = ap.parse_args()

    serial = args.serial or only_device()
    nodes = ensure_preview(serial)

    if args.state or not (args.zoom or args.front or args.back or args.hq):
        print(f"device {serial}\ncontrols on screen:")
        for rid, (attrs, centre) in sorted(nodes.items()):
            if attrs["clickable"] == "true" or attrs["text"] or attrs["desc"]:
                label = attrs["text"] or attrs["desc"]
                print(f"  {rid:<30} {label[:30]:<30} at {centre}")
        sel = nodes.get("zoom_ui_toggle_btn_selected")
        if sel:
            # The selector pill sits over whichever option is active, so whichever zoom
            # option shares its centre-x is the one currently selected.
            sx = sel[1][0]
            for z, rid in ZOOM_IDS.items():
                if rid in nodes and abs(nodes[rid][1][0] - sx) < 20:
                    print(f"\ncurrently selected zoom: {z}")
        return

    if args.zoom:
        tap(serial, nodes, ZOOM_IDS[args.zoom],
            f"zoom {args.zoom}" + ("  (ULTRAWIDE)" if args.zoom == "0.5" else ""))
    if args.front or args.back:
        tap(serial, nodes, "toggle_camera_button",
            "front camera" if args.front else "back camera")
    if args.hq:
        tap(serial, nodes, "high_quality_button", "high quality toggle")


if __name__ == "__main__":
    # cp1252 is the console default here, and a stray non-ASCII character in a print has
    # now killed three separate tools AT THE MOMENT THEY HAD SOMETHING USEFUL TO SAY.
    # Degrade the character, never the process.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
