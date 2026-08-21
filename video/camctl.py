#!/usr/bin/env python3
"""
camctl - read and set a UVC camera's hardware controls from the host.

WHY THIS EXISTS: OBS's DirectShow input exposes only the *stream* settings - resolution,
format, frame interval, colour space. It has no zoom, focus, exposure or white balance,
so the rig dashboard could not reach them either. Those live on a different pair of COM
interfaces that OBS simply does not surface:

    IAMCameraControl   pan, tilt, roll, ZOOM, exposure, iris, focus
    IAMVideoProcAmp    brightness, contrast, hue, saturation, sharpness, gamma,
                       white balance, backlight compensation, gain

Whether a given camera implements ANY of them is up to its firmware. A UVC device is
allowed to support none. So the first job of this tool is to ask, not to assume:

    python camctl.py probe                     # what does this camera actually support?
    python camctl.py get --device "Android"    # current values
    python camctl.py set --zoom 200
    python camctl.py set --exposure -6 --manual

⚠️ A DirectShow device has exactly ONE consumer. OBS holds the camera whenever its source
is active, and binding here will fail while it does. Pass --release to have this tool
deactivate the OBS source, do its work, and switch it back on afterwards.
"""

import argparse
import sys
from ctypes import POINTER, c_long

from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

# ---------------------------------------------------------------------------------------
# The two interfaces OBS does not expose. Declared by hand because they are not in any
# type library comtypes can import - the method ORDER below is the vtable order and must
# not be rearranged.
# ---------------------------------------------------------------------------------------

_RANGE = [
    COMMETHOD([], HRESULT, "GetRange",
              (["in"], c_long, "Property"),
              (["out"], POINTER(c_long), "pMin"),
              (["out"], POINTER(c_long), "pMax"),
              (["out"], POINTER(c_long), "pSteppingDelta"),
              (["out"], POINTER(c_long), "pDefault"),
              (["out"], POINTER(c_long), "pCapsFlags")),
    COMMETHOD([], HRESULT, "Set",
              (["in"], c_long, "Property"),
              (["in"], c_long, "lValue"),
              (["in"], c_long, "Flags")),
    COMMETHOD([], HRESULT, "Get",
              (["in"], c_long, "Property"),
              (["out"], POINTER(c_long), "lValue"),
              (["out"], POINTER(c_long), "Flags")),
]


class IAMCameraControl(IUnknown):
    _iid_ = GUID("{C6E13370-30AC-11D0-A18C-00A0C9118956}")
    _methods_ = _RANGE


class IAMVideoProcAmp(IUnknown):
    _iid_ = GUID("{C6E13360-30AC-11D0-A18C-00A0C9118956}")
    _methods_ = _RANGE


# Property enums, from the DirectShow headers.
CAMERA_PROPS = {
    "pan": 0, "tilt": 1, "roll": 2, "zoom": 3,
    "exposure": 4, "iris": 5, "focus": 6,
}
PROCAMP_PROPS = {
    "brightness": 0, "contrast": 1, "hue": 2, "saturation": 3, "sharpness": 4,
    "gamma": 5, "colorenable": 6, "whitebalance": 7, "backlight": 8, "gain": 9,
}

# CameraControlFlags / VideoProcAmpFlags
FLAG_AUTO, FLAG_MANUAL = 0x0001, 0x0002


def _flag_name(f):
    bits = []
    if f & FLAG_AUTO:
        bits.append("auto")
    if f & FLAG_MANUAL:
        bits.append("manual")
    return "+".join(bits) or str(f)


# ---------------------------------------------------------------------------------------


def bind(device_substring):
    """Bind the first video device whose name contains `device_substring`.

    Returns (camera_control_or_None, proc_amp_or_None, device_label). The graph object is
    kept alive on the returned filter, otherwise COM tears the device down immediately.
    """
    from pygrabber.dshow_graph import FilterGraph

    graph = FilterGraph()
    names = graph.get_input_devices()
    hits = [i for i, n in enumerate(names) if device_substring.lower() in n.lower()]
    if not hits:
        sys.exit(f"no video device matching '{device_substring}'. Seen: " + ", ".join(names))
    idx = hits[0]
    graph.add_video_input_device(idx)
    flt = graph.get_input_device()

    def qi(iface):
        try:
            return flt.instance.QueryInterface(iface)
        except Exception:
            return None

    cc, pa = qi(IAMCameraControl), qi(IAMVideoProcAmp)
    # Stash the graph so it is not garbage collected while the interfaces are in use.
    for obj in (cc, pa):
        if obj is not None:
            obj._keepalive = graph
    return cc, pa, names[idx]


def _probe_group(obj, props, title):
    print(f"\n{title}")
    if obj is None:
        print("   interface NOT implemented by this device")
        return {}
    found = {}
    for name, prop in props.items():
        try:
            mn, mx, step, dflt, caps = obj.GetRange(prop)
        except Exception:
            print(f"   {name:<14} unsupported")
            continue
        try:
            val, flags = obj.Get(prop)
            cur = f"now={val} ({_flag_name(flags)})"
        except Exception:
            cur = "now=?"
        print(f"   {name:<14} {mn}..{mx} step {step}  default {dflt}  "
              f"[{_flag_name(caps)}]  {cur}")
        found[name] = (mn, mx, step, dflt, caps)
    if not found:
        print("   (interface present but no property is supported)")
    return found


def obs_source(active, source):
    """Toggle an OBS source so it releases / retakes the DirectShow device."""
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import obsctl
    cl = obsctl.connect(timeout=15)
    cl.set_input_settings(source, {"active": active}, True)
    return cl


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["probe", "get", "set"])
    ap.add_argument("--device", default="Android Webcam")
    ap.add_argument("--release", metavar="OBS_SOURCE", default=None,
                    help="deactivate this OBS source first, reactivate after")
    for p in list(CAMERA_PROPS) + list(PROCAMP_PROPS):
        ap.add_argument(f"--{p}", type=int, default=None)
    ap.add_argument("--manual", action="store_true", help="set values as manual, not auto")
    args = ap.parse_args()

    if args.release:
        obs_source(False, args.release)
        print(f"released '{args.release}' in OBS")
        import time
        time.sleep(1.5)

    try:
        cc, pa, label = bind(args.device)
        print(f"device          {label}")

        if args.cmd in ("probe", "get"):
            _probe_group(cc, CAMERA_PROPS, "IAMCameraControl  (zoom / focus / exposure)")
            _probe_group(pa, PROCAMP_PROPS, "IAMVideoProcAmp   (brightness / white balance)")
        else:
            flags = FLAG_MANUAL if args.manual else FLAG_AUTO
            wrote = 0
            for group, props in ((cc, CAMERA_PROPS), (pa, PROCAMP_PROPS)):
                if group is None:
                    continue
                for name, prop in props.items():
                    want = getattr(args, name)
                    if want is None:
                        continue
                    try:
                        group.Set(prop, want, flags)
                        print(f"   {name:<14} = {want} ({_flag_name(flags)})")
                        wrote += 1
                    except Exception as e:
                        print(f"   {name:<14} FAILED: {type(e).__name__}")
            if not wrote:
                print("   nothing to set - pass e.g. --zoom 200")
    finally:
        if args.release:
            obs_source(True, args.release)
            print(f"reactivated '{args.release}' in OBS")


if __name__ == "__main__":
    # cp1252 is the console default here, and a stray non-ASCII character in a print has
    # now killed three separate tools AT THE MOMENT THEY HAD SOMETHING USEFUL TO SAY.
    # Degrade the character, never the process.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
