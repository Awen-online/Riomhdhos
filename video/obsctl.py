#!/usr/bin/env python3
"""
obsctl - drive OBS from the command line over obs-websocket.

WHY: building scenes by clicking is not repeatable, and the settings that matter most
here are the ones the GUI negotiates for you and gets wrong. A dshow camera added with
"Device Default" negotiates whatever format the driver advertises first, which for the
Pixel is a combination that claims the device and delivers no frames - a black source
that looks broken but is only mis-negotiated. Setting resolution and format explicitly
is the fix, and doing it from a script means it is the same every time.

The password is read from OBS's own config rather than passed on the command line, so it
never lands in shell history.

  python obsctl.py status
  python obsctl.py devices
  python obsctl.py build-cam --device "Android Webcam"
  python obsctl.py shot --scene CAM --out frame.png
"""

import argparse
import json
import logging
import os
import sys

try:
    import obsws_python as obsws
except ImportError:
    sys.exit("obsws-python is not installed:  python -m pip install obsws-python")

# ⚠️ obsws-python LOGS THE PASSWORD IN PLAINTEXT AT INFO LEVEL. Its connect path emits
#   "Connecting with parameters: host='localhost' port=4455 password='...'"
# The whole reason this reads the password out of OBS's own config rather than taking it
# as an argument is to keep it out of shell history - and the library then prints it to
# stdout anyway, into scrollback and any redirected log file.
#
# Latent rather than active here, because nothing in this file raises the root log level.
# That is exactly why it is worth pinning: the leak arrives the day some other script
# imports this and turns on INFO logging, and nothing about that change looks dangerous.
# Capped at WARNING so genuine library failures still surface.
for _lg in ("obsws_python", "obsws_python.baseclient", "obsws_python.reqs"):
    logging.getLogger(_lg).setLevel(logging.WARNING)

CONFIG = os.path.join(
    os.environ.get("APPDATA", ""),
    "obs-studio", "plugin_config", "obs-websocket", "config.json",
)


def connect(timeout=5):
    """Connect using the credentials OBS generated for itself."""
    if not os.path.exists(CONFIG):
        sys.exit("no obs-websocket config found - open OBS once, then enable the server")
    with open(CONFIG, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    port = cfg.get("server_port", 4455)
    # auth_required off means the password is ignored by the server; sending one anyway
    # is harmless, but sending none when it IS required fails with a bare handshake error.
    password = cfg.get("server_password") if cfg.get("auth_required", True) else ""

    # ALWAYS TRY THE CONNECTION, whatever the config claims. server_enabled is written
    # when the settings dialog is OK'd, so it can disagree with reality in both
    # directions - and refusing to connect because a file says so, while the server is
    # right there listening, is a failure invented by the tool. The config is consulted
    # only to explain a failure that already happened.
    try:
        return obsws.ReqClient(host="localhost", port=port, password=password, timeout=timeout)
    except Exception as exc:
        hint = ""
        if not cfg.get("server_enabled"):
            hint = ("\n  Config says the server is disabled. In OBS:\n"
                    "    Tools -> WebSocket Server Settings -> tick 'Enable WebSocket Server'.\n"
                    "  Editing config.json instead does not work while OBS is running - it\n"
                    "  rewrites that file on exit and the edit is lost.")
        elif not any(p.name() == "obs64.exe" for p in _procs()):
            hint = "\n  OBS does not appear to be running. Start it and try again."
        sys.exit(f"could not connect to OBS on port {port}: {exc}{hint}")


def _procs():
    try:
        import psutil
        return psutil.process_iter()
    except Exception:
        return []


def cmd_status(_args):
    cl = connect()
    v = cl.get_version()
    print(f"OBS            {v.obs_version}")
    print(f"websocket      {v.obs_web_socket_version} (rpc {v.rpc_version})")
    print(f"platform       {v.platform_description}")

    scenes = cl.get_scene_list()
    current = getattr(scenes, "current_program_scene_name", None)
    print(f"\nscenes ({len(scenes.scenes)}), current = {current}")
    for s in scenes.scenes:
        print(f"  {s['sceneName']}")

    inputs = cl.get_input_list()
    print(f"\ninputs ({len(inputs.inputs)})")
    for i in inputs.inputs:
        print(f"  {i['inputName']:<28} {i['inputKind']}")

    rec = cl.get_record_status()
    print(f"\nrecording      {'ACTIVE' if rec.output_active else 'idle'}")


def _cycle(cl, source, settle=5.0):
    """Deactivate and reactivate a dshow source so it re-opens the device.

    This is the step that makes a settings change actually take. Without it the new
    resolution and format are stored and ignored.
    """
    import time
    cl.set_input_settings(source, {"active": False}, True)
    time.sleep(1.5)
    cl.set_input_settings(source, {"active": True}, True)
    time.sleep(settle)          # dshow needs a moment to hand over the first frame


def _source_size(cl, scene, source):
    """Actual delivered size, which is 0x0 until a frame has arrived. This is the only
    honest test of 'is it working' - the source existing proves nothing."""
    for it in cl.get_scene_item_list(scene).scene_items:
        if it["sourceName"] == source:
            t = cl.get_scene_item_transform(scene, it["sceneItemId"]).scene_item_transform
            return (t["sourceWidth"], t["sourceHeight"])
    return (0, 0)


def _list_items(cl, input_name, prop):
    """Property lists can only be read from an input that exists, so callers create a
    throwaway one first. Returns [(label, value)]."""
    r = cl.get_input_properties_list_property_items(input_name, prop)
    return [(it["itemName"], it["itemValue"]) for it in r.property_items]


def cmd_devices(args):
    """Enumerate real capture devices and their formats.

    Needs a temporary dshow input because obs-websocket can only read property lists off
    a live input - there is no way to ask a *kind* what it supports.
    """
    cl = connect()
    tmp_scene, tmp_input = "__obsctl_probe", "__obsctl_probe_cam"
    made_scene = False
    try:
        if tmp_scene not in [s["sceneName"] for s in cl.get_scene_list().scenes]:
            cl.create_scene(tmp_scene)
            made_scene = True
        cl.create_input(tmp_scene, tmp_input, "dshow_input", {}, False)

        devices = _list_items(cl, tmp_input, "video_device_id")
        print("video devices:")
        for label, value in devices:
            print(f"  {label}")
            if args.verbose:
                print(f"      id: {value}")

        # ⚠️ THE PROPERTY LISTS CASCADE, and both steps are required:
        #   1. select a device  -> resolutions appear
        #   2. res_type = 1     -> formats appear
        # Skip step 2 and the format list is just "Any", because in device-default mode
        # the format is the driver's business and OBS does not offer a choice. "Any" is
        # not a format - it is the absence of one, and letting the driver pick is exactly
        # what produces a source that holds the device and shows black.
        want = args.device.lower() if args.device else None
        for label, dev_id in devices:
            if want and want not in label.lower():
                continue
            cl.set_input_settings(tmp_input, {"video_device_id": dev_id, "res_type": 1}, True)
            print(f"\n{label}")
            res = [r for r, _ in _list_items(cl, tmp_input, "resolution")]
            if not res:
                print("  (no custom modes offered)")
                continue
            # Formats are per-resolution, not per-device: 1080p may offer only MJPEG
            # while 640x480 also offers uncompressed. Printing one list per mode is the
            # only honest way to show that.
            for r in res:
                cl.set_input_settings(tmp_input, {"video_device_id": dev_id,
                                                  "res_type": 1, "resolution": r}, True)
                fmts = [f"{fl}({fv})" for fl, fv in _list_items(cl, tmp_input, "video_format")
                        if fl != "Any"]
                print(f"  {r:<12} {', '.join(fmts) if fmts else '-'}")
    finally:
        try:
            cl.remove_input(tmp_input)
        except Exception:
            pass
        if made_scene:
            try:
                cl.remove_scene(tmp_scene)
            except Exception:
                pass


def cmd_build_cam(args):
    """Create a scene holding one camera, configured explicitly.

    Every value here is looked up from the device at runtime rather than hardcoded. The
    format enum in particular is an internal libdshowcapture number - guessing it is how
    you end up with a source that claims the device and shows black.
    """
    cl = connect()
    scene, source = args.scene, args.name or args.device

    scenes = [s["sceneName"] for s in cl.get_scene_list().scenes]
    if scene not in scenes:
        cl.create_scene(scene)
        print(f"created scene   {scene}")

    existing = [i["inputName"] for i in cl.get_input_list().inputs]
    if source in existing:
        if not args.replace:
            sys.exit(f"input '{source}' already exists - pass --replace to rebuild it")
        cl.remove_input(source)
        print(f"removed old     {source}")

    cl.create_input(scene, source, "dshow_input", {}, True)

    devices = _list_items(cl, source, "video_device_id")
    match = [(l, v) for l, v in devices if args.device.lower() in l.lower()]
    if not match:
        cl.remove_input(source)
        sys.exit(f"no video device matching '{args.device}'. Seen: "
                 + ", ".join(l for l, _ in devices))
    label, device_id = match[0]

    w, h = (int(x) for x in args.resolution.lower().split("x"))

    # ⚠️ THE PROPERTY LISTS CASCADE, and ALL THREE steps are required before the format
    # list means anything:
    #   1. video_device_id      -> the resolution list populates
    #   2. res_type = 1         -> custom mode, so a format is even a choice
    #   3. resolution           -> the FORMAT list populates, filtered to that mode
    # Ask earlier and you get only "Any", which is not a format but the absence of one.
    # Measured on the Pixel: at 1920x1080 the ONLY format offered is MJPEG - uncompressed
    # YUY2 is ~2 Gbit/s at that size and no UVC link carries it. So "Any" at 1080p means
    # the driver picks something that cannot stream, which is the black source exactly.
    cl.set_input_settings(source, {
        "video_device_id": device_id,
        "res_type": 1,
        "resolution": f"{w}x{h}",
    }, True)

    formats = dict(_list_items(cl, source, "video_format"))
    fmt = formats.get(args.format)
    if fmt is None:
        cl.remove_input(source)
        sys.exit(f"format '{args.format}' not offered by {label}. Seen: " + ", ".join(formats))

    settings = {
        "video_device_id": device_id,
        "res_type": 1,                       # 1 = custom. 0 lets the driver decide - the
                                             # decision that produces the black source.
        "resolution": f"{w}x{h}",
        "frame_interval": int(round(10_000_000 / args.fps)),   # 100ns units
        "video_format": fmt,
        "active": True,
    }
    cl.set_input_settings(source, settings, True)

    # ⚠️ SETTINGS ALONE DO NOTHING TO A RUNNING SOURCE. dshow negotiates its mode when it
    # opens the device; writing new settings afterwards updates the stored values and
    # leaves the live capture exactly as it was. A source configured but never cycled
    # sits at 0x0 having never received a frame - which renders as nothing and reads as
    # "my settings did not work" rather than "the device was never reopened".
    _cycle(cl, source)

    t = _source_size(cl, scene, source)
    if t == (0, 0):
        print("⚠️  source is still 0x0 - it is not receiving frames. Check that nothing "
              "else holds the camera:\n    a DirectShow device has exactly ONE consumer, "
              "and a browser tab,\n    ffmpeg, or a second OBS source on the same device "
              "will silently win it.")
    else:
        print(f"delivering      {t[0]:.0f}x{t[1]:.0f}")

    print(f"device          {label}")
    print(f"format          {args.format} (enum {fmt})")
    print(f"mode            {w}x{h} @ {args.fps}")
    print(f"source          {source}  in scene  {scene}")

    if args.switch:
        cl.set_current_program_scene(scene)
        print(f"switched to     {scene}")


def cmd_cycle(args):
    """The single most useful recovery action here: a camera that shows nothing usually
    holds valid settings and a stale capture. Cycling re-opens the device."""
    cl = connect()
    _cycle(cl, args.source)
    scene = args.scene or cl.get_scene_list().current_program_scene_name
    w, h = _source_size(cl, scene, args.source)
    print(f"{args.source}: {'not delivering (0x0)' if (w, h) == (0, 0) else f'{w:.0f}x{h:.0f}'}")


def cmd_focal_blur(args):
    """Depth-of-field blur on a camera, from a monocular depth estimate.

    NOT the person-segmentation models the plugin ships with - those are PEOPLE detectors,
    so with nobody in frame every pixel is "background" and the whole picture just goes
    soft. Depth estimation works on any scene, which is what makes this usable on a shot
    of instruments as well as on a person.

    The three settings that actually decide the look, all of which were wrong on the first
    attempts:

      blur_focus_depth  the WIDTH of the sharp band. 0.20 kept only the nearest things
                        sharp and softened the keyboard; 0.45 holds foreground through
                        mid-distance - and that band is where a person stands, so walking
                        into frame lands inside the sharp zone.
      blur_background   strength. 2-3 is invisible for depth blur; it needs 7-9. This is
                        the setting most likely to make you think the filter is broken.
      enable_threshold  MUST be off. On, it forces a soft depth gradient into a hard
                        keep/cut decision - a cutout, not a blur.

    ⚠️ Do NOT run this during a live stream. Reinitialising the filter while the render
    thread is inside it crashes OBS - observed, with an access violation in
    obs_source_skip_video_filter. This is a rehearsal operation.
    """
    cl = connect()
    existing = [f["filterName"] for f in cl.get_source_filter_list(args.source).filters]
    settings = {
        "model_select":      "models/tcmonodepth_tcsmallnet_192x320.onnx",
        "enable_focal_blur": True,
        "enable_threshold":  False,
        "blur_background":   args.strength,
        "blur_focus_point":  args.focus,
        "blur_focus_depth":  args.depth,
        "temporal_smooth_factor": 0.9,   # stops the depth mask crawling between frames
        "useGPU":            "dml",      # DirectML -> the Radeon, not the CPU
    }
    if args.name in existing:
        cl.set_source_filter_settings(args.source, args.name, settings, True)
        print(f"updated '{args.name}' on {args.source}")
    else:
        cl.create_source_filter(args.source, args.name, "background_removal", settings)
        print(f"created '{args.name}' on {args.source}")
    print(f"  strength {args.strength}   focus {args.focus}   depth {args.depth}")
    print("  give it ~8s to settle before judging it")


def cmd_shot(args):
    """Save a frame. Proves the source is actually delivering pixels, which 'the source
    exists' does not."""
    cl = connect()
    out = os.path.abspath(args.out)
    # -1 for "native size" is rejected: the protocol validates width/height against a
    # minimum of 8 rather than treating a sentinel as "unset". Pass real numbers.
    cl.save_source_screenshot(args.scene, args.out.rsplit(".", 1)[-1], out,
                              args.width, args.height, -1)
    print(f"wrote {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="connection, scenes, inputs, recording state").set_defaults(fn=cmd_status)

    c = sub.add_parser("cycle", help="re-open a camera's device (fixes a source stuck at 0x0)")
    c.add_argument("source")
    c.add_argument("--scene", default=None, help="scene to measure the result in")
    c.set_defaults(fn=cmd_cycle)

    d = sub.add_parser("devices", help="list capture devices and their formats")
    d.add_argument("--device", default=None,
                   help="only probe devices matching this substring (probing is slow)")
    d.add_argument("-v", "--verbose", action="store_true", help="show full device ids")
    d.set_defaults(fn=cmd_devices)

    b = sub.add_parser("build-cam", help="create a scene with one explicitly configured camera")
    b.add_argument("--device", default="Android Webcam", help="substring of the device name")
    b.add_argument("--scene", default="CAM")
    b.add_argument("--name", default=None, help="source name (defaults to the device)")
    b.add_argument("--resolution", default="1920x1080")
    b.add_argument("--fps", type=float, default=30)
    b.add_argument("--format", default="MJPEG",
                   help="MJPEG for anything above 640x480 - YUY2 is uncompressed and "
                        "1080p will not fit down a UVC link")
    b.add_argument("--replace", action="store_true")
    b.add_argument("--switch", action="store_true", help="make this the program scene")
    b.set_defaults(fn=cmd_build_cam)

    fb = sub.add_parser("focal-blur", help="depth-of-field blur on a camera (known-good preset)")
    fb.add_argument("--source", default="Webcam")
    fb.add_argument("--name", default="Focal blur")
    fb.add_argument("--strength", type=int, default=7, help="7-9; below ~5 is invisible")
    fb.add_argument("--focus", type=float, default=0.22, help="centre of the sharp band")
    fb.add_argument("--depth", type=float, default=0.45,
                    help="width of the sharp band; 0.45 keeps a person and the keyboard sharp")
    fb.set_defaults(fn=cmd_focal_blur)

    s = sub.add_parser("shot", help="save a screenshot of a scene")
    s.add_argument("--scene", default="CAM")
    s.add_argument("--out", default="shot.png")
    s.add_argument("--width", type=int, default=1280)
    s.add_argument("--height", type=int, default=720)
    s.set_defaults(fn=cmd_shot)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
