#!/usr/bin/env python3
"""
vcambridge - decode the phone's stream here and present it to OBS as a WEBCAM.

⚠️ WHY THIS EXISTS, AND WHY IT IS NOT ABOUT THE NETWORK. Measured, by decoding the phone's
stream straight off the wire with ffmpeg and OBS not involved at all:

    camera -> encode -> WiFi -> decode      80 ms
    the same stream measured inside OBS    ~790 ms

The phone and the network account for a tenth of the delay. OBS's **Media Source** adds the
rest, and its own knobs do not touch it: default 787 ms, `buffering_mb 0` 780 ms, and
`nobuffer flags=low_delay` made it slightly WORSE at 824 ms. Six transport-side hypotheses
were tested and disproved before this - USB instead of WiFi (identical), MPEG-TS with real
timestamps (331 ms worse), a shallower client queue, the encoder's own low-latency keys.
None of them mattered, because none of them were the problem.

So this bypasses Media Source entirely. ffmpeg decodes here, the frames go into the
**OBS Virtual Camera** DirectShow sink, and OBS reads it as an ordinary webcam - the same
class of source as the wired Pixel 8, which does not suffer this buffering.

This is the architecture DroidCam uses, and it is why DroidCam feels responsive: their PC
client decodes and feeds a virtual camera driver rather than handing a URL to a player.

    python vcambridge.py                          # defaults to the Pixel 6 over WiFi
    python vcambridge.py --url http://127.0.0.1:8091/stream.h264    # over adb forward
    python vcambridge.py --size 1920x1080

Then in OBS: add a **Video Capture Device** and pick **OBS Virtual Camera**.

⚠️ The OBS Virtual Camera sink has ONE producer. Stop OBS's own "Start Virtual Camera"
before running this, or they will fight over it.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request

import numpy as np
import pyvirtualcam


def probe_size(api):
    """Ask the phone what it is sending, so we do not guess and letterbox."""
    try:
        with urllib.request.urlopen(api, timeout=4) as r:
            d = json.loads(r.read().decode())
        m = re.match(r"(\d+)x(\d+)", str(d.get("resolution", "")))
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://192.168.1.234:8090/stream.h264")
    ap.add_argument("--api", default="http://192.168.1.234:8090/api/state")
    ap.add_argument("--size", default=None, help="WxH; probed from the phone if omitted")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--backend", default="obs")
    args = ap.parse_args()

    ff = shutil.which("ffmpeg") or r"C:\Users\mccul\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"

    if args.size:
        w, h = (int(x) for x in args.size.lower().split("x"))
    else:
        got = probe_size(args.api)
        if not got:
            sys.exit("could not probe the stream size - is RigCam running? Pass --size.")
        w, h = got
    print(f"stream {w}x{h} from {args.url}")

    # ⚠️ EVERY FLAG HERE IS ABOUT NOT ACCUMULATING FRAMES. `-fflags nobuffer` and
    # `-flags low_delay` stop the demuxer and decoder holding frames back; a tiny probesize
    # keeps startup short. The output is raw video on a pipe, so nothing downstream can
    # buffer either - we read exactly one frame's worth and hand it straight over.
    cmd = [ff, "-hide_banner", "-loglevel", "error",
           "-fflags", "nobuffer", "-flags", "low_delay",
           "-f", "h264", "-i", args.url,
           "-vf", f"scale={w}:{h}",
           "-pix_fmt", "rgb24", "-f", "rawvideo", "-fps_mode", "passthrough", "-"]
    # ⚠️ Do NOT add `-probesize 32 -analyzeduration 0` here. They shave startup but leave
    # ffmpeg unable to estimate the frame rate ("not enough frames to estimate rate"), and
    # they buy nothing once running - the latency this tool targets is downstream, in OBS.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, bufsize=0)

    frame_bytes = w * h * 3
    shown = 0
    t0 = time.time()
    last_report = t0

    try:
        with pyvirtualcam.Camera(width=w, height=h, fps=args.fps,
                                 backend=args.backend,
                                 fmt=pyvirtualcam.PixelFormat.RGB) as cam:
            print(f"feeding '{cam.device}'  -  add a Video Capture Device in OBS and pick it")
            while True:
                # ⚠️ A PIPE READ RETURNS WHAT IS AVAILABLE, NOT WHAT YOU ASKED FOR. With an
                # unbuffered pipe the first read came back short, which looked exactly like
                # the stream ending - the bridge exited immediately and blamed ffmpeg. Loop
                # until a whole frame is in hand.
                chunks, got = [], 0
                while got < frame_bytes:
                    part = proc.stdout.read(frame_bytes - got)
                    if not part:
                        break
                    chunks.append(part)
                    got += len(part)
                if got < frame_bytes:
                    err = proc.stderr.read(400).decode("utf-8", "replace").strip()
                    print("stream ended" + (f": {err}" if err else ""))
                    break
                buf = b"".join(chunks)
                # ⚠️ NO sleep_until_next_frame() HERE. That paces output to a nominal rate,
                # which is right for a generated source and wrong for a live one: it would
                # reintroduce exactly the queue this tool exists to remove. Send each frame
                # the moment it decodes.
                cam.send(np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3))
                shown += 1
                now = time.time()
                if now - last_report >= 5:
                    print(f"  {shown} frames, {shown / (now - t0):.1f} fps")
                    last_report = now
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        proc.terminate()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
