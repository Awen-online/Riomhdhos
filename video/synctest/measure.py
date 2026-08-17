#!/usr/bin/env python3
"""
Measure end-to-end camera latency by photographing a clock.

Open clock.html fullscreen on a monitor, point the camera at it, then run this. It
captures frames through OBS and stamps each with the time of capture; you read the time
shown IN the frame and subtract. The difference is the whole video path - exposure, ISP,
encode, UVC transport, decode, and OBS's buffering.

That number is what the OBS audio Sync Offset is built from:

    sync offset (ms)  =  camera latency  -  audio latency

Audio from the rig arrives over an analog cable into the Focusrite, so its latency is
small (~10-30 ms through the interface and WASAPI) and the camera dominates. The offset
DELAYS the audio to meet the picture, so it is positive.

⚠️ Take several samples. A single frame lands at an arbitrary point in the capture
interval, so one reading carries up to a frame period (33 ms at 30 fps) of quantisation
on top of the real figure. The spread across samples IS the measurement error - if the
readings disagree by more than a frame, something is buffering unevenly and the mean is
not trustworthy.
"""

import argparse
import json
import logging
import os
import sys
import time

try:
    import obsws_python as obsws
except ImportError:
    sys.exit("obsws-python is not installed:  python -m pip install obsws-python")

# obsws-python logs the websocket password in plaintext at INFO. See obsctl.py for the
# full note; capped here too so this file is safe to run with verbose logging on.
for _lg in ("obsws_python", "obsws_python.baseclient", "obsws_python.reqs"):
    logging.getLogger(_lg).setLevel(logging.WARNING)

CONFIG = os.path.join(os.environ.get("APPDATA", ""), "obs-studio",
                      "plugin_config", "obs-websocket", "config.json")


def connect():
    with open(CONFIG, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    pw = cfg.get("server_password") if cfg.get("auth_required", True) else ""
    return obsws.ReqClient(host="localhost", port=cfg.get("server_port", 4455),
                           password=pw, timeout=8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", required=True, help="scene showing the camera")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--gap", type=float, default=1.5)
    args = ap.parse_args()

    cl = connect()
    os.makedirs(args.outdir, exist_ok=True)
    print(f"capturing {args.count} frames from '{args.scene}'\n")

    for i in range(args.count):
        path = os.path.abspath(os.path.join(args.outdir, f"sync{i}.png"))
        # Bracket the call: the frame is grabbed somewhere inside this window, so the
        # midpoint is the best single estimate of "when the picture was as shown" and
        # the width of the bracket is honest measurement error, not noise to hide.
        t0 = time.time()
        cl.save_source_screenshot(args.scene, "png", path, 1280, 720, -1)
        t1 = time.time()
        mid = (t0 + t1) / 2
        lt = time.localtime(mid)
        print(f"  sync{i}.png   captured at {lt.tm_min:02d}:{lt.tm_sec:02d}"
              f".{int(mid % 1 * 1000):03d}   (bracket +/-{(t1 - t0) * 500:.0f} ms)")
        if i < args.count - 1:
            time.sleep(args.gap)

    print("\nRead the clock in each image and subtract:")
    print("    latency = captured_at  -  time_shown_in_frame")
    print("Then in OBS: Advanced Audio Properties -> Sync Offset on the rig's audio")
    print("source, set to (latency - audio latency), positive to delay the audio.")


if __name__ == "__main__":
    main()
