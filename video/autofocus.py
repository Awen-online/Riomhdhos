#!/usr/bin/env python3
"""
autofocus - find the focus distance that is actually sharpest, then LOCK it.

⚠️ WHY LOCK RATHER THAN LEAVE AUTOFOCUS ON. Nothing in RigCam ever set CONTROL_AF_MODE, so
CameraX's default continuous autofocus ran unopposed. On a camera pointed at a person who
moves, that re-racks constantly - visible as focus cycling in recordings, which is what
sent me looking. A locked-off camera at a fixed subject distance has no reason to refocus
at all, and every refocus is a visible artifact.

Same shape as autoexpose.py: measure once, deliberately, write the answer back as a fixed
manual value. The camera adapts to the room; it does not keep adapting during a show.

⚠️ DIOPTRES, NOT METRES. LENS_FOCUS_DISTANCE is 1/metres, so 0.0 is INFINITY and the
maximum is the CLOSEST the lens can focus. Both phones here report 9.52, about 10 cm.

    python autofocus.py --all
    python autofocus.py --all --dry-run
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
from numpy.lib.stride_tricks import sliding_window_view
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import obsctl  # noqa: E402

DEFAULT_CAMS = {
    "Pixel 8": ("http://127.0.0.1:8091", "Pixel 8"),
    "Pixel 6": ("http://192.168.1.50:8090", "Pixel 6 (vcam)"),
}
CALIBRATION = HERE / "focus.json"

# Laplacian: the standard sharpness proxy. Its VARIANCE over the frame peaks at best focus.
LAP = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=np.float32)
SETTLE_S = 1.1


def rigcam(url, path, timeout=8):
    try:
        with urllib.request.urlopen(url.rstrip("/") + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"offline": True, "error": f"{type(e).__name__}: {e}"}


def sharpness(cl, source):
    """Variance of the Laplacian over one frame. Higher is sharper."""
    try:
        r = cl.get_source_screenshot(source, "jpg", 640, 360, 92)
    except Exception:
        return None
    data = getattr(r, "image_data", None) or ""
    a = np.asarray(Image.open(_io.BytesIO(base64.b64decode(
        data.split(",", 1)[-1]))).convert("L"), dtype=np.float32)
    w = sliding_window_view(a, (3, 3))
    return float(np.einsum("ijkl,kl->ij", w, LAP).var())


def set_focus(url, d):
    rigcam(url, "/api/set?" + urllib.parse.urlencode(
        {"manualFocus": "true", "focusDioptres": round(d, 3)}))
    time.sleep(SETTLE_S)


def calibrate(cl, label, url, source, dry_run=False):
    st = rigcam(url, "/api/state")
    if st.get("offline"):
        print(f"  {label}: unreachable ({st.get('error')})"); return None
    if st.get("dormant"):
        print(f"  {label}: dormant - wake it first"); return None
    focus = st.get("focus")
    if not focus:
        print(f"  {label}: this RigCam build has no focus control - reinstall the app")
        return None
    max_d = float(focus.get("maxDioptres") or 0)
    if max_d <= 0:
        print(f"  {label}: fixed-focus lens, nothing to do"); return None

    # ⚠️ MEASURE THE CAMERA, NOT THE COMPOSITE - the same trap autoexpose hit. Background
    # removal deletes most of the detail in the frame, and 'Low light' and colour
    # correction both move local contrast, so a sharpness reading through the filter chain
    # measures the filters. Everything off, restored in the finally block.
    disabled = []
    original = focus.get("dioptres", 0.0)
    try:
        for f in cl.get_source_filter_list(source).filters:
            if f["filterEnabled"]:
                cl.set_source_filter_enabled(source, f["filterName"], False)
                disabled.append(f["filterName"])
        if disabled:
            time.sleep(0.8)

        if dry_run:
            print(f"  {label}: would sweep 0 - {max_d:.2f} dioptres (currently "
                  f"{'manual' if focus.get('manual') else 'AUTO'} at {original})")
            return None

        # Coarse pass across the whole range, then a fine pass around the winner. A single
        # fine sweep would be ~40 lens moves and two minutes of the subject holding still.
        coarse = [i * max_d / 10.0 for i in range(11)]
        best_d, best_s = None, -1.0
        for d in coarse:
            set_focus(url, d)
            sc = sharpness(cl, source)
            if sc is None:
                continue
            print(f"    {d:5.2f} dioptre ({'inf' if d < 0.01 else f'{1/d:5.2f} m':>7})  "
                  f"sharpness {sc:9.0f}")
            if sc > best_s:
                best_d, best_s = d, sc
        if best_d is None:
            print(f"  {label}: could not read the source"); return None

        step = max_d / 10.0
        fine = [best_d + k * step / 3.0 for k in (-2, -1, 1, 2)]
        for d in [x for x in fine if 0 <= x <= max_d]:
            set_focus(url, d)
            sc = sharpness(cl, source)
            if sc is not None and sc > best_s:
                best_d, best_s = d, sc

        set_focus(url, best_d)
        dist = "infinity" if best_d < 0.01 else f"{1/best_d:.2f} m"
        print(f"  {label}: locked at {best_d:.2f} dioptres ({dist}), sharpness {best_s:.0f}")
        return {"label": label, "dioptres": round(best_d, 3), "sharpness": round(best_s, 1)}
    finally:
        for name in disabled:
            try:
                cl.set_source_filter_enabled(source, name, True)
            except Exception:
                print(f"    ⚠️ could not restore filter '{name}' - check it by hand")


def save(results):
    data = {}
    if CALIBRATION.exists():
        try:
            data = json.loads(CALIBRATION.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["_readme"] = [
        "Per-phone focus, measured by autofocus.py. Dioptres = 1/metres, 0 = infinity.",
        "Re-run 'python autofocus.py --all' if a camera moves or the subject distance changes.",
    ]
    phones = data.setdefault("phones", {})
    for r in results:
        phones[r["label"]] = {"dioptres": r["dioptres"], "sharpness": r["sharpness"],
                              "calibrated": time.strftime("%Y-%m-%dT%H:%M:%S")}
    tmp = CALIBRATION.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(CALIBRATION)
    return CALIBRATION


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phone", action="append", default=[], metavar="LABEL=URL")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cams = dict(DEFAULT_CAMS) if (args.all or not args.phone) else {}
    for spec in args.phone:
        label, url = spec.split("=", 1)
        cams[label.strip()] = (url.strip(),
                               DEFAULT_CAMS.get(label.strip(), (None, label.strip()))[1])

    cl = obsctl.connect(timeout=8)
    print("⚠️ hold still - this measures sharpness, and movement reads as blur")
    results = [r for r in (calibrate(cl, l, u, src, args.dry_run)
                           for l, (u, src) in cams.items()) if r]
    if results and not args.dry_run:
        print(f"\n  wrote {save(results).name}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
