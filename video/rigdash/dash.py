#!/usr/bin/env python3
"""
rigdash - one dashboard for the whole video side, on one port.

Replaces the separate tuner (8770) and visuals server (8780). Two processes on two ports
meant two things to start, two to forget, and two to leave stale - and stale servers
holding a port already cost real time tonight by making every test hit old code.

    /            dashboard          open on the phone
    /visuals     the WebGL page     point an OBS Browser source here
    /feed        SSE audio features consumed by /visuals
    /api/...     filter + mood control

⚠️ WHAT IS SAFE TO CHANGE LIVE, established by testing, not assumption:

    filter PARAMETERS      SAFE   6 rapid changes, no fault
    enable / disable       SAFE   4 toggles, no fault
    remove + create        CRASHES OBS - access violation in obs_source_skip_video_filter
    model_select           same allocation path as create; treat as unsafe

So this exposes parameters and toggles only, enforced by an ALLOWLIST per filter kind.
A denylist would fail open, and the failure here is not a wrong setting - it is OBS going
down mid-show.

    python dash.py                 # auto-picks the Focusrite input
    python dash.py --device 8
"""

import argparse
import json
import queue
import subprocess
import urllib.parse
import urllib.request
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import base64
import io

import numpy as np
import sounddevice as sd

HERE = Path(__file__).parent
VISUALS = HERE.parent / "visuals"
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(VISUALS))

import obsctl                                  # noqa: E402  credential handling + logger pinning
from server import Analyser, BANDS             # noqa: E402  the analyser is already tested

SOURCE = "Webcam"

# ---------------------------------------------------------------------------------------
# The two camera back ends.
#
# ⚠️ THEY ARE NOT INTERCHANGEABLE, and the panel says so rather than pretending otherwise.
#   WIRED (Pixel 8, UVC)  - the host gets NO camera control over UVC at all (see
#                           video/camctl.py: zoom/focus unsupported, exposure min==max), so
#                           the only route is tapping the phone's own DeviceAsWebcam UI over
#                           ADB. Three fixed zoom presets, ~2 s per action, and it needs the
#                           phone unlocked.
#   RIGCAM (WiFi)         - our own app, full Camera2 control: continuous zoom, EV, and the
#                           AE/AWB locks that let two cameras cut together.
#
# ⚠️ NEITHER IS POLLED FROM THE 1 Hz LOOP. `uvczoom --state` runs a uiautomator dump and
# costs ~2 s; putting that in the status poll would stall the whole dashboard once a second.
# Camera state is fetched on demand, when the panel is opened or refreshed.
# ---------------------------------------------------------------------------------------
RIGCAM = "http://127.0.0.1:8090"       # via `adb forward tcp:8090 tcp:8090`, or a LAN IP
UVCZOOM = HERE.parent / "uvczoom.py"
UVC_ZOOMS = ("0.5", "1.0", "2.0")
# ⚠️ The wired phone is named explicitly. Two phones are attached in normal use and the
# wired-camera controls MUST NOT land on the WiFi one - doing so launches DeviceAsWebcam
# over RigCam and kills its stream.
UVC_SERIAL = None


def rigcam_call(path, timeout=2.5):
    """Talk to the RigCam app. Never raises - an unreachable phone is a normal state."""
    try:
        with urllib.request.urlopen(RIGCAM + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"offline": True, "error": type(e).__name__}


def uvc_call(*args, timeout=30):
    """Run uvczoom.py. Reuses the tested tool rather than restating its ADB handling."""
    if not UVCZOOM.exists():
        return {"ok": False, "out": "uvczoom.py not found"}
    try:
        cmd = [sys.executable, str(UVCZOOM), *args]
        if UVC_SERIAL:
            cmd += ["--serial", UVC_SERIAL]
        p = subprocess.run(cmd,
                           capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or p.stderr or "").strip()
        return {"ok": p.returncode == 0, "out": out[-400:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "out": "timed out - is the phone awake and unlocked?"}
    except Exception as e:
        return {"ok": False, "out": f"{type(e).__name__}: {e}"}


def uvc_selected(text):
    """Pull the active zoom out of `uvczoom --state` output, or None."""
    for line in (text or "").splitlines():
        if "currently selected zoom" in line:
            return line.split(":")[-1].strip()
    return None
CODE_HASH = "?"
MOODS = ["COSMOS", "THE CAIRN", "ÉIRE", "THE DEEP"]

# Index order MUST match PATTERN_NAMES in visuals/index.html - the wire format is the
# integer, so a mismatch silently plays the wrong pattern rather than erroring.
PATTERNS = ["chladni", "moire", "rings", "lissajous", "flow", "cells", "grid", "spiral"]

# OBS composites the overlay against the video on the GPU. This is the whole reason the
# shader does not need the camera as a texture: a DirectShow device has one consumer, and
# blend modes get the same result without taking it from OBS.
#   SCREEN    lines brighten the video, never darken it - the safe default over faces
#   ADDITIVE  hotter, blows out over bright areas
#   MULTIPLY  lines darken - reads as ink or shadow on the image
#   LIGHTEN/DARKEN  per-channel max/min, harder edged
BLEND_MODES = ["OBS_BLEND_NORMAL", "OBS_BLEND_SCREEN", "OBS_BLEND_ADDITIVE",
               "OBS_BLEND_MULTIPLY", "OBS_BLEND_LIGHTEN", "OBS_BLEND_DARKEN"]
OVERLAY_SOURCE = "Chladni"

# The echo rig, built by build_scenes.py. Each entry is a nested scene that exists purely
# so it can carry its own delay - filters attach to sources, so three copies of the camera
# in one scene would share one filter chain and therefore one delay, which is none.
ECHO_SCENES = ["ECHO 1", "ECHO 2"]
ECHO_BASE = {"ECHO 1": (120, 0.55), "ECHO 2": (260, 0.32)}   # (delay ms, opacity at 100%)
LIVE_SCENE = "LIVE"

# Presets are the one thing a VJ actually needs mid-set: recalling a whole LOOK in one tap
# rather than dialling six sliders while playing. Stored beside this file as plain JSON so
# they survive a restart and can be read, edited or version-controlled by hand.
PRESET_FILE = Path(__file__).parent / "presets.json"
PRESET_KEYS = ["patternA", "patternB", "xfade", "kaleido", "complexity",
               "intensity", "echo", "echoTime", "vReact", "mood"]

# Per filter KIND, the parameters that may be written and their ranges. Anything not here
# is silently dropped - notably model_select, which is what makes a filter reload its
# model and is the operation that took OBS down.
ALLOWED = {
    "background_removal": {
        "blur_background":  (0, 20, 1, "Blur strength"),
        "blur_focus_point": (0.0, 1.0, 0.01, "Focus point"),
        "blur_focus_depth": (0.0, 1.0, 0.01, "Sharp band"),
    },
    "enhanceportrait": {
        "blend": (0.0, 1.0, 0.01, "Amount"),
    },
}

STATE = {k: 0.0 for k in BANDS}
# `intensity` scales the overlay's alpha in the page. Kept HERE rather than as an OBS
# filter because it must be adjustable while the overlay is live, and OBS's own opacity
# lives in a colour-correction filter - adding or removing filters is the operation that
# crashes the render thread. A number the page already receives costs nothing.
STATE.update({"rms": 0.0, "centroid": 0.0, "peak": 0.0, "mood": "COSMOS",
              "intensity": 1.0, "t": 0.0,
              # Two decks and a crossfader, as a DJ would expect. Mixing happens in FIELD
              # space inside the shader, so the figure bends from one pattern into the
              # other rather than dissolving - a transition, not a cut.
              "patternA": 0, "patternB": 1, "xfade": 0.0,
              "kaleido": 0.0, "complexity": 1.0,
              # Video features. The camera itself drives the pattern, not just the audio.
              "vBright": 0.0, "vMotion": 0.0, "vDetail": 0.0, "vReact": 0.0,
              "echo": 0.0, "echoTime": 1.0})
_subs, _lock = [], threading.Lock()
_cl = None
_clock = threading.Lock()


def client():
    """One shared OBS connection, reconnected on demand - a dropped websocket must not
    need a restart of a process meant to be left running through a show."""
    global _cl
    with _clock:
        try:
            _cl.get_version()
        except Exception:
            _cl = obsctl.connect(timeout=10)
        return _cl


def audio_thread(device, samplerate, blocksize, channels):
    an = Analyser(samplerate, blocksize)

    def cb(indata, frames, tinfo, status):
        mono = indata.mean(axis=1) if indata.ndim > 1 else indata
        feats = an.process(np.asarray(mono, dtype=np.float32))
        with _lock:
            STATE.update(feats)
            STATE["t"] = time.time()
            payload = json.dumps(STATE)
            dead = []
            for q in _subs:
                try:
                    q.put_nowait(payload)      # drop, never block: this is a realtime thread
                except queue.Full:
                    dead.append(q)
            for q in dead:
                _subs.remove(q)

    with sd.InputStream(device=device, channels=channels, samplerate=samplerate,
                        blocksize=blocksize, dtype="float32", callback=cb):
        while True:
            time.sleep(1)


def video_thread(source, hz=5.0, w=160, h=90):
    """Read the CAMERA and turn it into drive parameters.

    ⚠️ THE FRAMES COME FROM OBS, NOT FROM THE DEVICE. A DirectShow camera has exactly one
    consumer; opening it here would take it from OBS and black out the stream. obs-websocket
    hands back what OBS has already decoded, so this is a free ride on work already done -
    no second claim on the device, and it keeps working whatever the camera is.

    Deliberately slow and tiny: 5 Hz at 160x90. These features drive slow, wide gestures -
    how much is moving, how bright the room is - and none of that needs frame rate. A full
    resolution grab at 30 Hz would cost more than everything else in this process combined.
    """
    from PIL import Image
    prev = None
    period = 1.0 / hz
    while True:
        t0 = time.time()
        try:
            r = client().get_source_screenshot(source, "jpg", w, h, 40)
            raw = r.image_data.split(",", 1)[-1]         # strip the data: URI prefix
            im = Image.open(io.BytesIO(base64.b64decode(raw))).convert("L")
            f = np.asarray(im, dtype=np.float32) / 255.0

            bright = float(f.mean())
            # Local contrast, which tracks how much STRUCTURE is in frame rather than how
            # light it is - a bright empty wall and a busy dim shelf differ here and not in
            # brightness.
            detail = float(f.std())
            motion = 0.0 if prev is None else float(np.abs(f - prev).mean())
            prev = f

            with _lock:
                # Motion is scaled hard because inter-frame difference at 5 Hz is small -
                # a person moving normally lands around 0.02-0.06 raw.
                STATE["vBright"] = round(min(1.0, bright * 1.6), 4)
                STATE["vDetail"] = round(min(1.0, detail * 3.0), 4)
                STATE["vMotion"] = round(min(1.0, motion * 12.0), 4)
        except Exception:
            # A missing source or a mid-restart OBS must not kill the thread - the audio
            # side keeps working and video features simply stop updating.
            pass
        time.sleep(max(0.0, period - (time.time() - t0)))


def load_presets():
    try:
        return json.loads(PRESET_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_presets(d):
    PRESET_FILE.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def apply_preset(cl, preset):
    """Apply a stored look. Echo and blend go through their own paths because they live in
    OBS rather than in STATE - a preset that only restored the shader would silently leave
    the trails and blend mode from whatever was on screen before."""
    with _lock:
        for k in PRESET_KEYS:
            if k in preset:
                STATE[k] = preset[k]
        amt, tscale = STATE.get("echo", 0.0), STATE.get("echoTime", 1.0)
    for name in ECHO_SCENES:
        if name not in [x["sceneName"] for x in cl.get_scene_list().scenes]:
            continue
        base_ms, base_op = ECHO_BASE[name]
        cl.set_source_filter_settings(name, "delay", {"delay_ms": int(base_ms * tscale)}, True)
        cl.set_source_filter_settings(name, "fade", {"opacity": base_op * amt}, True)
        try:
            item = next((i for i in cl.get_scene_item_list(LIVE_SCENE).scene_items
                         if i["sourceName"] == name), None)
            if item:
                cl.set_scene_item_enabled(LIVE_SCENE, item["sceneItemId"], amt > 0.01)
        except Exception:
            pass
    blend = preset.get("blend")
    if blend in BLEND_MODES:
        scene = cl.get_scene_list().current_program_scene_name
        item = next((i for i in cl.get_scene_item_list(scene).scene_items
                     if i["sourceName"] == OVERLAY_SOURCE), None)
        if item:
            cl.set_scene_item_blend_mode(scene, item["sceneItemId"], blend)


def _current_blend(cl, scene):
    """Blend mode is a property of the scene ITEM, not the source - the same overlay can
    screen in one scene and multiply in another, which is a feature worth not flattening."""
    try:
        item = next((i for i in cl.get_scene_item_list(scene).scene_items
                     if i["sourceName"] == OVERLAY_SOURCE), None)
        if not item:
            return None
        return cl.get_scene_item_blend_mode(scene, item["sceneItemId"]).scene_item_blend_mode
    except Exception:
        return None


def filter_state():
    cl = client()
    out = []
    for f in cl.get_source_filter_list(SOURCE).filters:
        kind = f["filterKind"]
        if kind not in ALLOWED:
            continue
        s = cl.get_source_filter(SOURCE, f["filterName"]).filter_settings
        params = []
        for key, (lo, hi, step, label) in ALLOWED[kind].items():
            params.append({"key": key, "label": label, "min": lo, "max": hi,
                           "step": step, "value": s.get(key, lo)})
        # model_select is shown but NOT editable - knowing which model is loaded matters
        # for judging the picture, while changing it is the unsafe operation.
        out.append({"name": f["filterName"], "kind": kind, "enabled": f["filterEnabled"],
                    "model": str(s.get("model_select", "")).split("/")[-1], "params": params})
    return out


PAGE = (HERE / "dash.html")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _file(self, path, ctype="text/html; charset=utf-8"):
        if not path.is_file():
            self.send_error(404); return
        b = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = urlparse(self.path).path
        try:
            if p == "/feed":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                q = queue.Queue(maxsize=4)
                with _lock:
                    _subs.append(q)
                try:
                    while True:
                        self.wfile.write(f"data: {q.get()}\n\n".encode())
                        self.wfile.flush()
                except Exception:
                    pass
                finally:
                    with _lock:
                        if q in _subs:
                            _subs.remove(q)
                return

            if p == "/api/camera":
                # On demand only. See the note by RIGCAM above.
                uvc = uvc_call("--state")
                self._json({
                    "rigcam": rigcam_call("/api/state"),
                    "uvc": {"reachable": uvc["ok"], "selected": uvc_selected(uvc["out"]),
                            "zooms": list(UVC_ZOOMS), "detail": uvc["out"]},
                }); return

            if p == "/api/state":
                with _lock:
                    audio = dict(STATE)
                cl = client()
                sl = cl.get_scene_list()
                st = cl.get_stats()
                v = cl.get_video_settings()
                fps = v.fps_numerator / max(1, v.fps_denominator)
                # Render time against budget is the number that decides whether another
                # filter or overlay can be afforded. Two inference filters plus a browser
                # source is most of a 33 ms frame, and nothing else on the machine says so.
                self._json({
                    "audio": audio, "filters": filter_state(),
                    "moods": MOODS, "source": SOURCE,
                    "scenes": [s["sceneName"] for s in sl.scenes],
                    "currentScene": sl.current_program_scene_name,
                    "code": CODE_HASH,
                    "presets": sorted(load_presets().keys()),
                    "patterns": PATTERNS,
                    "blendModes": BLEND_MODES,
                    "blend": _current_blend(cl, sl.current_program_scene_name),
                    "health": {
                        "fps": round(fps, 2),
                        "budgetMs": round(1000.0 / fps, 2),
                        "renderMs": round(st.average_frame_render_time, 2),
                        "skipped": st.render_skipped_frames,
                        "missed": st.output_skipped_frames,
                        "cpu": round(st.cpu_usage, 1),
                    }})
            elif p == "/visuals":
                self._file(VISUALS / "index.html")
            elif p in ("/", "/index.html"):
                self._file(PAGE)
            else:
                self.send_error(404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            cl = client()

            if p == "/api/mood":
                name = str(body.get("mood", "")).upper()
                if name in [m.upper() for m in MOODS]:
                    with _lock:
                        STATE["mood"] = next(m for m in MOODS if m.upper() == name)
                self._json({"mood": STATE["mood"]}); return

            if p == "/api/scene":
                name = body.get("scene")
                if name in [s["sceneName"] for s in cl.get_scene_list().scenes]:
                    cl.set_current_program_scene(name)
                self._json({"currentScene": cl.get_scene_list().current_program_scene_name})
                return

            if p == "/api/visuals":
                # Clamped on the way in. These reach a shader running in the live output;
                # a stray value out of range is a visible fault on a projector.
                lim = {"intensity": (0.0, 1.0), "xfade": (0.0, 1.0),
                       "kaleido": (0.0, 16.0), "complexity": (0.25, 3.0)}
                with _lock:
                    for k, (lo, hi) in lim.items():
                        if k in body:
                            STATE[k] = max(lo, min(hi, float(body[k])))
                    if "vReact" in body:
                        STATE["vReact"] = max(0.0, min(1.0, float(body["vReact"])))
                    for k in ("patternA", "patternB"):
                        if k in body:
                            STATE[k] = max(0, min(len(PATTERNS) - 1, int(body[k])))
                    out = {k: STATE[k] for k in
                           ("intensity", "xfade", "kaleido", "complexity",
                            "patternA", "patternB", "vReact")}
                self._json(out); return

            if p == "/api/preset":
                name = str(body.get("name", "")).strip()[:32]
                act = body.get("action", "recall")
                ps = load_presets()
                if act == "save" and name:
                    with _lock:
                        snap = {k: STATE.get(k) for k in PRESET_KEYS}
                    snap["blend"] = _current_blend(cl, cl.get_scene_list().current_program_scene_name)
                    ps[name] = snap
                    save_presets(ps)
                elif act == "delete" and name in ps:
                    del ps[name]; save_presets(ps)
                elif act == "recall" and name in ps:
                    apply_preset(cl, ps[name])
                self._json({"presets": sorted(ps.keys()), "applied": name if act == "recall" else None})
                return

            if p == "/api/echo":
                with _lock:
                    if "echo" in body:
                        STATE["echo"] = max(0.0, min(1.0, float(body["echo"])))
                    if "echoTime" in body:
                        STATE["echoTime"] = max(0.25, min(3.0, float(body["echoTime"])))
                    amt, tscale = STATE["echo"], STATE["echoTime"]
                for name in ECHO_SCENES:
                    if name not in [x["sceneName"] for x in cl.get_scene_list().scenes]:
                        continue
                    base_ms, base_op = ECHO_BASE[name]
                    cl.set_source_filter_settings(name, "delay",
                                                  {"delay_ms": int(base_ms * tscale)}, True)
                    cl.set_source_filter_settings(name, "fade",
                                                  {"opacity": base_op * amt}, True)
                    # Disable outright at zero. An opacity-0 layer still costs a full
                    # delayed copy of the camera in VRAM and a composite pass; switching
                    # it off actually reclaims that.
                    try:
                        item = next((i for i in cl.get_scene_item_list(LIVE_SCENE).scene_items
                                     if i["sourceName"] == name), None)
                        if item:
                            cl.set_scene_item_enabled(LIVE_SCENE, item["sceneItemId"], amt > 0.01)
                    except Exception:
                        pass
                self._json({"echo": STATE["echo"], "echoTime": STATE["echoTime"]}); return

            if p == "/api/camera":
                target = body.get("target")
                if target == "uvc":
                    args = []
                    if body.get("zoom") in UVC_ZOOMS:
                        args = [body["zoom"]]
                    elif body.get("lens") == "front":
                        args = ["--front"]
                    elif body.get("lens") == "back":
                        args = ["--back"]
                    elif body.get("hq"):
                        args = ["--hq"]
                    if not args:
                        self._json({"error": "nothing to do"}, 400); return
                    r = uvc_call(*args)
                    self._json({"uvc": r}, 200 if r["ok"] else 502); return

                if target == "rigcam":
                    # Allowlist: only these reach the phone, and each is range-clamped there.
                    q = {}
                    for k in ("zoom", "linearZoom", "ev", "aeLock", "awbLock", "torch",
                              "bitrate"):
                        if k in body and body[k] is not None:
                            v = body[k]
                            q[k] = str(v).lower() if isinstance(v, bool) else str(v)
                    if not q:
                        self._json({"error": "nothing to do"}, 400); return
                    self._json({"rigcam": rigcam_call(
                        "/api/set?" + urllib.parse.urlencode(q))}); return

                self._json({"error": "target must be uvc or rigcam"}, 400); return

            if p == "/api/blend":
                mode = body.get("mode")
                if mode not in BLEND_MODES:
                    self._json({"error": "unknown blend mode"}, 400); return
                scene = cl.get_scene_list().current_program_scene_name
                item = next((i for i in cl.get_scene_item_list(scene).scene_items
                             if i["sourceName"] == OVERLAY_SOURCE), None)
                if not item:
                    self._json({"error": f"{OVERLAY_SOURCE} not in {scene}"}, 404); return
                cl.set_scene_item_blend_mode(scene, item["sceneItemId"], mode)
                self._json({"blend": mode, "scene": scene}); return

            if p == "/api/toggle":
                name = body.get("filter")
                cur = next((f for f in filter_state() if f["name"] == name), None)
                if not cur:
                    self._json({"error": "no such filter"}, 404); return
                cl.set_source_filter_enabled(SOURCE, name, not cur["enabled"])
                self._json({"filters": filter_state()}); return

            if p == "/api/set":
                name = body.get("filter")
                cur = next((f for f in filter_state() if f["name"] == name), None)
                if not cur:
                    self._json({"error": "no such filter"}, 404); return
                allow = ALLOWED[cur["kind"]]
                out = {}
                for k, v in (body.get("params") or {}).items():
                    if k not in allow:          # allowlist: fails closed by construction
                        continue
                    lo, hi, step, _ = allow[k]
                    val = max(lo, min(hi, float(v)))
                    out[k] = int(val) if isinstance(step, int) else val
                if out:
                    cl.set_source_filter_settings(SOURCE, name, out, True)
                self._json({"applied": out}); return

            self.send_error(404)
        except Exception as e:
            self._json({"error": str(e)}, 500)


def addresses(port):
    """Every address the phone might use, most-likely first.

    NOT gethostbyname(gethostname()): with a VPN up that returns the tunnel address, which
    nothing on the LAN can reach.

    ⚠️ Nor is the route trick sufficient on its own. Connecting a UDP socket toward
    192.168.1.1 still returned 10.2.0.2 here, because ProtonVPN installs a default route at
    metric 0 and captures the lookup - so the address that looked most authoritative was
    the one guaranteed not to work. Labelling that "lan" is worse than not labelling it:
    a confident wrong answer costs more than an unranked list.

    So classify by what the address IS, and say plainly when one cannot be reached from
    the LAN.
    """
    import socket
    ips, seen = [], set()

    def add(ip):
        if ip and ip not in seen:
            seen.add(ip); ips.append(ip)

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.1.1", 9)); add(s.getsockname()[0]); s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except Exception:
        pass

    def label(ip):
        if ip.startswith("192.168."):            return "LAN"      # what the phone wants
        if ip.startswith("100."):                return "tailscale"
        if ip.startswith(("10.", "172.16.")):    return "vpn?"     # tunnel, not the LAN
        if ip.startswith("169.254."):            return "link-local"
        return "other"

    rank = {"LAN": 0, "tailscale": 1, "other": 2, "vpn?": 3, "link-local": 4}
    out = sorted(((label(ip), ip) for ip in ips), key=lambda t: rank.get(t[0], 9))
    return out or [("local", "127.0.0.1")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--device", type=int, default=None)
    ap.add_argument("--source", default="Webcam")
    ap.add_argument("--uvc-serial", default=None,
                    help="adb serial of the WIRED phone (required when two are attached)")
    ap.add_argument("--rigcam", default="http://127.0.0.1:8090",
                    help="RigCam base URL (adb forward gives 127.0.0.1:8090)")
    ap.add_argument("--samplerate", type=int, default=44100)
    ap.add_argument("--blocksize", type=int, default=1024)
    ap.add_argument("--no-audio", action="store_true",
                    help="filter control only, no audio capture")
    args = ap.parse_args()

    global SOURCE, RIGCAM, UVC_SERIAL
    SOURCE = args.source
    RIGCAM = args.rigcam
    UVC_SERIAL = args.uvc_serial
    client()                                    # fail now, not on the phone's first tap

    if not args.no_audio:
        devs = sd.query_devices()
        dev = args.device
        if dev is None:
            dev = next((i for i, d in enumerate(devs)
                        if d["max_input_channels"] > 0 and "Focusrite" in d["name"]), None)
        if dev is None:
            print("no Focusrite input found - running without audio", flush=True)
        else:
            ch = min(2, devs[dev]["max_input_channels"])
            print(f"audio: device {dev} {devs[dev]['name']} ({ch} ch)", flush=True)
            threading.Thread(target=audio_thread,
                             args=(dev, args.samplerate, args.blocksize, ch),
                             daemon=True).start()

    # flush=True throughout: redirected to a file Python buffers stdout, and an empty log
    # is indistinguishable from "never started" - which is exactly how three stale servers
    # went unnoticed while every test hit old code.
    threading.Thread(target=video_thread, args=(SOURCE,), daemon=True).start()
    # A CONTENT HASH OF THIS FILE, printed at startup and served at /api/state.
    #
    # Stale servers holding the port have now cost real time THREE times in one session:
    # each time the code on disk was correct, the process was old, and every test silently
    # exercised the previous version - so correct fixes looked broken and I went hunting
    # for bugs that were not there. "Is the thing under test the thing I changed" is not a
    # question you should have to answer by inference. Compare the hash.
    import hashlib
    global CODE_HASH
    CODE_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:8]
    print(f"source: {SOURCE}  (video features at 5 Hz)  code {CODE_HASH}", flush=True)
    for label, ip in addresses(args.port):
        print(f"  {label:<6} dashboard http://{ip}:{args.port}/", flush=True)
    print(f"  OBS browser source -> http://localhost:{args.port}/visuals", flush=True)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
