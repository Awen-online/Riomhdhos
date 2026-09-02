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


def subject_mask(cl, source, shape):
    """Where the PERSON is, from the background-removal matte. None if unavailable.

    ⚠️ THIS IS THE WHOLE POINT OF THE SECOND VERSION. The first measured variance of the
    Laplacian over the ENTIRE frame, which optimises for wherever the most detail is - and
    in this room that is the desk, mic stand, launchpad and keyboard behind the subject.
    The Pixel 8 duly locked at 3.15 m, focused past the person onto the wall, and Ian's
    face was soft. Sharpest frame is not the same thing as sharpest subject.

    The erase filter already knows where the person is, so borrow its matte as the region
    of interest and ignore everything outside it.
    """
    name = next((f["filterName"] for f in cl.get_source_filter_list(source).filters
                 if f["filterKind"] == "background_removal"
                 and "erase" in f["filterName"].lower()), None)
    if not name:
        return None
    was = next(f["filterEnabled"] for f in cl.get_source_filter_list(source).filters
               if f["filterName"] == name)
    try:
        cl.set_source_filter_enabled(source, name, True)
        time.sleep(1.4)
        r = cl.get_source_screenshot(source, "png", shape[1], shape[0], -1)
        im = Image.open(_io.BytesIO(base64.b64decode(
            (getattr(r, "image_data", "") or "").split(",", 1)[-1]))).convert("RGBA")
        alpha = np.asarray(im)[:, :, 3]
        m = alpha > 128
        # A mask covering almost nothing or almost everything tells us nothing useful.
        if m.mean() < 0.03 or m.mean() > 0.9:
            return None
        return m
    except Exception:
        return None
    finally:
        try:
            cl.set_source_filter_enabled(source, name, was)
        except Exception:
            pass


def sharpness(cl, source, roi=None):
    """Variance of the Laplacian, over `roi` if given and the whole frame otherwise."""
    try:
        r = cl.get_source_screenshot(source, "jpg", 640, 360, 92)
    except Exception:
        return None
    data = getattr(r, "image_data", None) or ""
    a = np.asarray(Image.open(_io.BytesIO(base64.b64decode(
        data.split(",", 1)[-1]))).convert("L"), dtype=np.float32)
    w = sliding_window_view(a, (3, 3))
    lap = np.einsum("ijkl,kl->ij", w, LAP)
    if roi is None:
        return float(lap.var())
    # sliding_window_view trims one pixel each side; align the mask to it.
    r2 = roi[1:-1, 1:-1]
    if r2.shape != lap.shape or r2.sum() < 50:
        return float(lap.var())
    return float(lap[r2].var())


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
    # Before anything is disabled: the matte needs the erase filter ON to exist.
    roi = subject_mask(cl, source, (360, 640))
    print(f"  {label}: metering on {'the subject matte' if roi is not None else 'the WHOLE FRAME (no usable matte - it may focus past you)'}")
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
            sc = sharpness(cl, source, roi)
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
            sc = sharpness(cl, source, roi)
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
