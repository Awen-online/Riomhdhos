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
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import numpy as np
import sounddevice as sd

HERE = Path(__file__).parent
VISUALS = HERE.parent / "visuals"
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(VISUALS))

import obsctl                                  # noqa: E402  credential handling + logger pinning
from server import Analyser, BANDS             # noqa: E402  the analyser is already tested

SOURCE = "Webcam"
MOODS = ["COSMOS", "THE CAIRN", "ÉIRE", "THE DEEP"]

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
              "intensity": 1.0, "t": 0.0})
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
                with _lock:
                    if "intensity" in body:
                        STATE["intensity"] = max(0.0, min(1.0, float(body["intensity"])))
                    out = {"intensity": STATE["intensity"]}
                self._json(out); return

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
    ap.add_argument("--samplerate", type=int, default=44100)
    ap.add_argument("--blocksize", type=int, default=1024)
    ap.add_argument("--no-audio", action="store_true",
                    help="filter control only, no audio capture")
    args = ap.parse_args()

    global SOURCE
    SOURCE = args.source
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
    print(f"source: {SOURCE}", flush=True)
    for label, ip in addresses(args.port):
        print(f"  {label:<6} dashboard http://{ip}:{args.port}/", flush=True)
    print(f"  OBS browser source -> http://localhost:{args.port}/visuals", flush=True)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
