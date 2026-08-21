#!/usr/bin/env python3
"""
synclatency - measure how far each camera lags reality, and how far they lag EACH OTHER.

WHY THIS EXISTS: the two cameras reach OBS by completely different routes - one is direct
UVC over USB, the other is camera -> hardware H.264 -> HTTP -> ffmpeg -> OBS. There is no
reason for those to arrive together, and cutting between two feeds that are offset from one
another is the kind of fault you discover during a show rather than before one.

METHOD: fire the phone's torch and watch for the step in brightness.

    ⚠️ THIS NEEDS NO CLOCK ON SCREEN AND NOTHING AIMED BY HAND, which is the whole point -
    the obvious method (point a camera at a millisecond clock) needs a person holding a
    phone, so it never actually gets run. A torch is a light source the rig already
    controls, and both cameras are looking at the same room.

    The torch is on ONE phone, so the light reaches the other camera as reflected room
    light: expect a smaller step there, hence the separate --threshold-b.

⚠️ WHAT THIS MEASURES, AND WHAT IT DOES NOT. Every sample is an OBS screenshot round trip,
so the result includes that round trip and is quantised by it. The tool measures and
reports its own sampling period; treat any single number as +/- that. The DIFFERENCE
between the two cameras is far more trustworthy than either absolute, because both are
sampled through the same overhead - and the difference is the number that decides whether
a cut between them is safe.

    python synclatency.py                       # both cameras, 3 flashes
    python synclatency.py --runs 5
    python synclatency.py --torch-host http://192.168.1.234:8090
"""

import argparse
import base64
import io as _io
import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import obsctl  # noqa: E402


def torch(host, on):
    url = f"{host}/api/set?torch={'true' if on else 'false'}"
    try:
        with urllib.request.urlopen(url, timeout=4) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def luma(cl, source, width=160):
    """Mean brightness of one source, and the wall-clock time the sample completed."""
    r = cl.get_source_screenshot(source, "png", width, 90, -1)
    t = time.perf_counter()
    data = r.image_data
    if "," in data:
        data = data.split(",", 1)[1]
    im = Image.open(_io.BytesIO(base64.b64decode(data))).convert("L")
    return float(np.asarray(im, dtype=np.float32).mean()), t


def measure(cl, sources, host, runs, settle, thresholds, width):
    results = {s: [] for s in sources}
    periods = []

    for run in range(runs):
        torch(host, False)
        time.sleep(settle)

        # Baseline: what the room looks like unlit, per source.
        base = {}
        for s in sources:
            vals = [luma(cl, s, width)[0] for _ in range(4)]
            base[s] = statistics.median(vals)

        pending = set(sources)
        found = {}
        torch(host, True)
        t0 = time.perf_counter()

        # Sample every source in turn until each one shows the step, or we give up.
        last_t = t0
        while pending and time.perf_counter() - t0 < 6.0:
            for s in list(pending):
                v, t = luma(cl, s, width)
                periods.append(t - last_t)
                last_t = t
                if v - base[s] >= thresholds[s]:
                    found[s] = t - t0
                    pending.discard(s)
        torch(host, False)

        for s in sources:
            if s in found:
                results[s].append(found[s])
                print(f"  run {run+1}: {s:<16} {found[s]*1000:7.0f} ms "
                      f"(base {base[s]:.1f})")
            else:
                print(f"  run {run+1}: {s:<16} no step seen "
                      f"(base {base[s]:.1f}, threshold {thresholds[s]})")
        time.sleep(settle)

    return results, periods


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wired", default="Pixel 8")
    ap.add_argument("--wifi", default="Pixel 6 (vcam)")
    ap.add_argument("--torch-host", default="http://192.168.1.234:8090",
                    help="RigCam base URL of the phone whose torch fires")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--settle", type=float, default=1.5)
    ap.add_argument("--width", type=int, default=160)
    ap.add_argument("--threshold", type=float, default=6.0,
                    help="luma step for the phone holding the torch")
    ap.add_argument("--threshold-b", type=float, default=2.0,
                    help="luma step for the other camera, which only sees reflected light")
    args = ap.parse_args()

    cl = obsctl.connect(timeout=20)

    # The torch phone is the WiFi one by default, so it gets the larger threshold.
    thresholds = {args.wifi: args.threshold, args.wired: args.threshold_b}
    sources = [s for s in (args.wifi, args.wired)
               if s in [i["inputName"] for i in cl.get_input_list().inputs]]
    if not sources:
        sys.exit("neither camera source exists in OBS")

    print(f"sources: {', '.join(sources)}")
    print(f"torch:   {args.torch_host}\n")
    results, periods = measure(cl, sources, args.torch_host, args.runs,
                               args.settle, thresholds, args.width)

    period_ms = statistics.median(periods) * 1000 if periods else float("nan")
    print(f"\nsampling period (the precision floor): {period_ms:.0f} ms per sample")
    print("-" * 58)
    med = {}
    for s in sources:
        if results[s]:
            med[s] = statistics.median(results[s]) * 1000
            print(f"  {s:<18} {med[s]:7.0f} ms   (n={len(results[s])})")
        else:
            print(f"  {s:<18}    no reading")

    if len(med) == 2:
        a, b = sources[0], sources[1]
        delta = med[a] - med[b]
        print("-" * 58)
        print(f"  DIFFERENCE {a} - {b}: {delta:+.0f} ms")
        print()
        # A frame at 30 fps is 33 ms; a cut is visibly wrong somewhere around 2-3 frames.
        if abs(delta) < 40:
            print("  Under ~1 frame apart: cutting between them is safe as-is.")
        elif abs(delta) < 120:
            print("  A few frames apart. Noticeable on a hard cut; correct it with a\n"
                  "  Render Delay filter on whichever source is EARLIER.")
        else:
            print(f"  WARNING: {abs(delta):.0f} ms apart - clearly visible on a cut. Add a Render\n"
                  f"  Delay filter of ~{abs(delta):.0f} ms to "
                  f"'{a if delta < 0 else b}' to line them up.")
        print("\n  Then align BOTH against audio: whatever the slower camera measures is\n"
              "  roughly the offset to apply to the audio track so sound and picture agree.")


if __name__ == "__main__":
    # cp1252 is the console default here, and a stray non-ASCII character in a print has
    # now killed three separate tools AT THE MOMENT THEY HAD SOMETHING USEFUL TO SAY.
    # Degrade the character, never the process.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
