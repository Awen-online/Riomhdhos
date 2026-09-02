#!/usr/bin/env python3
"""
autoexpose - set ISO from what the cameras are ACTUALLY seeing, then lock it.

⚠️ WHY NOT JUST TURN AUTO-EXPOSURE ON. Because that is the failure mode this rig was
built to avoid, and it has already happened once: found on 2026-09-02 with the Pixel 8
locked at ISO 800 and the Pixel 6 quietly running on auto. Two cameras deciding their own
exposure DRIFT APART over a set, so a cut between them jumps in brightness - measured p50
152 against 98 before manual mode was introduced. Auto also hunts: a performer moving in
front of a light makes the whole frame breathe.

So this is auto-exposure run ONCE, deliberately, with the result written back as a fixed
manual value. The camera adapts to the room; it does not keep adapting during the show.

⚠️ AND IT CONVERGES ON PICTURE BRIGHTNESS, NOT ON IDENTICAL NUMBERS. The rig's rule has
been "give both phones the same ISO so they match by construction", which is right when
they face the same light and wrong when they do not - identical settings on two cameras
pointed at different parts of a room give two different pictures. Both phones are driven
to the same TARGET LUMA instead, and the ISOs they land on are reported so a large
divergence is visible rather than hidden.

Shutter is never touched: 1/60 is the slowest that still stops motion on a hand, and
trading it away for light would buy blur. EV and white balance are left alone.

    python autoexpose.py --phone "Pixel 8=http://127.0.0.1:8091" --source "Pixel 8"
    python autoexpose.py --all --dry-run
"""
import argparse
import base64
import io as _io
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import obsctl  # noqa: E402

# Phone label -> (rigcam base url, OBS source name)
DEFAULT_CAMS = {
    "Pixel 8": ("http://127.0.0.1:8091", "Pixel 8"),
    "Pixel 6": ("http://192.168.1.50:8090", "Pixel 6 (vcam)"),
}

# ⚠️ 110, NOT 128. Mid-grey is 128 on a test card, but a lit face on a darker stage
# averages lower, and driving the MEAN to 128 blows the highlights on skin. 110 keeps
# headroom. Raise it for a bright room, lower it for a moody one.
TARGET_LUMA = 110.0
TOLERANCE = 6.0
MAX_STEPS = 12
SETTLE_S = 1.4          # the sensor needs a beat, and OBS averages its own frames

# ⚠️ EVERY filter comes off to measure, not just the obvious one.
#
# The first version disabled background removal only, on the grounds that an erased
# background makes the mean meaningless. That is true and it was not enough: the Pixel 6
# also carries "Low light" (enhanceportrait), which LIFTS the image - so as this loop cut
# ISO from 400 to 44, a factor of nine, the measured luma moved 183 -> 175 and the loop
# happily drove the sensor to its floor chasing a number the filter was holding up.
# Colour correction moves gamma too.
#
# So: measure the CAMERA, not the composite. Anything in the chain is off while the
# reading is taken, and everything is restored afterwards.
MEASURE_WITH_FILTERS_OFF = True


def rigcam(url, path, timeout=6):
    try:
        with urllib.request.urlopen(url.rstrip("/") + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"offline": True, "error": f"{type(e).__name__}: {e}"}


def mean_luma(cl, source):
    """Mean luma of one frame of an OBS source, 0-255, or None."""
    try:
        r = cl.get_source_screenshot(source, "jpg", 480, 270, 70)
    except Exception as e:
        print(f"    screenshot failed: {type(e).__name__}: {e}")
        return None
    data = getattr(r, "image_data", None) or ""
    if "," in data:
        data = data.split(",", 1)[1]
    try:
        img = Image.open(_io.BytesIO(base64.b64decode(data))).convert("L")
    except Exception as e:
        print(f"    could not decode frame: {e}")
        return None
    return float(np.asarray(img, dtype=np.float32).mean())


def calibrate(cl, label, url, source, target, dry_run=False):
    st = rigcam(url, "/api/state")
    if st.get("offline"):
        print(f"  {label}: unreachable ({st.get('error')})")
        return None
    if st.get("dormant"):
        print(f"  {label}: camera is dormant - wake it first")
        return None

    man = st.get("manual") or {}
    iso = int(man.get("iso") or 400)
    iso_min = int(man.get("isoMin") or 50)
    iso_max = int(man.get("isoMax") or 3200)
    shutter = int(man.get("shutterNs") or 16_666_666)

    # Everything off while measuring - see MEASURE_WITH_FILTERS_OFF above for why this is
    # every filter and not just the obvious one. Restored in the finally block even if this
    # throws: leaving a filter off after a calibration would be a silent change to the look.
    disabled = []
    try:
        for f in cl.get_source_filter_list(source).filters:
            if f["filterEnabled"]:
                cl.set_source_filter_enabled(source, f["filterName"], False)
                disabled.append(f["filterName"])
        if disabled:
            print(f"  {label}: measuring with {', '.join(disabled)} off")
            time.sleep(0.8)

        # Manual mode with the CURRENT iso, so the loop starts from a known state rather
        # than from whatever auto-exposure happened to be doing.
        if not dry_run:
            rigcam(url, "/api/set?" + urllib.parse.urlencode(
                {"manualExposure": "true", "iso": iso, "shutterNs": shutter}))
            time.sleep(SETTLE_S)

        for step in range(1, MAX_STEPS + 1):
            luma = mean_luma(cl, source)
            if luma is None:
                return None
            print(f"    step {step}: iso {iso:<5} luma {luma:6.1f}", end="")
            if abs(luma - target) <= TOLERANCE:
                print("  <- in range")
                break
            # Exposure is close to linear in ISO, so scale rather than step. Damped to
            # 0.8 because the sensor's response is not perfectly linear near the ends and
            # an undamped jump oscillates.
            factor = (target / max(luma, 1.0)) ** 0.8
            new_iso = int(max(iso_min, min(iso_max, round(iso * factor))))
            if new_iso == iso:
                print("  <- at a limit, cannot go further")
                break
            print(f"  -> iso {new_iso}")
            iso = new_iso
            if dry_run:
                break
            rigcam(url, "/api/set?" + urllib.parse.urlencode(
                {"manualExposure": "true", "iso": iso, "shutterNs": shutter}))
            time.sleep(SETTLE_S)
    finally:
        for name in disabled:
            try:
                cl.set_source_filter_enabled(source, name, True)
            except Exception:
                print(f"    ⚠️ could not restore filter '{name}' - check it by hand")

    final = rigcam(url, "/api/state").get("manual", {})
    print(f"  {label}: iso {final.get('iso')} @ 1/{round(1e9 / max(1, shutter))}, "
          f"manual={final.get('exposure')}")
    return {"label": label, "iso": final.get("iso"), "shutterNs": shutter}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phone", action="append", default=[], metavar="LABEL=URL")
    ap.add_argument("--source", action="append", default=[], metavar="LABEL=OBSSOURCE")
    ap.add_argument("--all", action="store_true", help="both phones, using the defaults")
    ap.add_argument("--target", type=float, default=TARGET_LUMA,
                    help=f"mean luma to aim for, 0-255 (default {TARGET_LUMA:.0f})")
    ap.add_argument("--dry-run", action="store_true", help="measure and propose, change nothing")
    args = ap.parse_args()

    cams = dict(DEFAULT_CAMS) if (args.all or not args.phone) else {}
    for spec in args.phone:
        label, url = spec.split("=", 1)
        cams[label.strip()] = (url.strip(), DEFAULT_CAMS.get(label.strip(), (None, label.strip()))[1])
    for spec in args.source:
        label, src = spec.split("=", 1)
        if label.strip() in cams:
            cams[label.strip()] = (cams[label.strip()][0], src.strip())

    cl = obsctl.connect(timeout=8)
    print(f"target mean luma {args.target:.0f}"
          + ("   (dry run - nothing will be changed)" if args.dry_run else ""))
    results = [r for r in (
        calibrate(cl, label, url, src, args.target, args.dry_run)
        for label, (url, src) in cams.items()) if r]

    if len(results) == 2:
        a, b = results
        try:
            ratio = max(int(a["iso"]), int(b["iso"])) / max(1, min(int(a["iso"]), int(b["iso"])))
            if ratio > 2.0:
                print(f"\n⚠️ {a['label']} and {b['label']} landed more than a stop apart "
                      f"({a['iso']} vs {b['iso']}). They are lit differently - the pictures "
                      f"match now, but check they still match if either camera is moved.")
        except Exception:
            pass


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
