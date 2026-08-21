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

    # ⚠️ SELF-HEALING, and it is not optional. The first version probed the size once,
    # opened one ffmpeg, and exited when the stream ended - CLEANLY, with status 0. So when
    # the phone changed resolution (which makes RigCam drop its clients ON PURPOSE, so they
    # re-probe), the bridge quit, the scheduled task saw a success and never restarted it,
    # and the WiFi camera went dark with no error anywhere. A component the show depends on
    # must reconnect by itself.
    fixed = None
    if args.size:
        fixed = tuple(int(x) for x in args.size.lower().split("x"))

    session = 0
    while True:
        size = fixed or probe_size(args.api)
        if not size:
            print("phone not answering; retrying in 5s", flush=True)
            time.sleep(5)
            continue
        w, h = size
        session += 1
        print(f"[{session}] {w}x{h} from {args.url}", flush=True)

        # ⚠️ Every flag here is about not accumulating frames. `-fflags nobuffer` and
        # `-flags low_delay` stop the demuxer and decoder holding frames back. Do NOT add
        # `-probesize 32 -analyzeduration 0`: they leave ffmpeg unable to estimate the frame
        # rate and buy nothing, because the latency this tool targets is downstream in OBS.
        cmd = [ff, "-hide_banner", "-loglevel", "error",
               "-fflags", "nobuffer", "-flags", "low_delay",
               "-f", "h264", "-i", args.url,
               "-vf", f"scale={w}:{h}",
               "-pix_fmt", "rgb24", "-f", "rawvideo", "-fps_mode", "passthrough", "-"]
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
                print(f"    feeding '{cam.device}'", flush=True)
                while True:
                    # ⚠️ A pipe read returns what is AVAILABLE, not what you asked for. The
                    # first short read once looked exactly like the stream ending.
                    chunks, got = [], 0
                    while got < frame_bytes:
                        part = proc.stdout.read(frame_bytes - got)
                        if not part:
                            break
                        chunks.append(part)
                        got += len(part)
                    if got < frame_bytes:
                        err = proc.stderr.read(300).decode("utf-8", "replace").strip()
                        print(f"    stream ended{': ' + err if err else ''}", flush=True)
                        break
                    # No sleep_until_next_frame(): pacing to a nominal rate would
                    # reintroduce exactly the queue this tool exists to remove.
                    cam.send(np.frombuffer(b"".join(chunks),
                                           dtype=np.uint8).reshape(h, w, 3))
                    shown += 1
                    now = time.time()
                    if now - last_report >= 30:
                        print(f"    {shown} frames, {shown / (now - t0):.1f} fps", flush=True)
                        last_report = now
        except KeyboardInterrupt:
            proc.terminate()
            print("stopping")
            return
        except Exception as e:
            print(f"    session failed: {type(e).__name__}: {e}", flush=True)
        finally:
            try:
                proc.terminate()
            except Exception:
                pass

        # Re-probe on the next pass: the resolution may be exactly why this one ended.
        time.sleep(2)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
