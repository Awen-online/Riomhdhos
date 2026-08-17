#!/usr/bin/env python3
"""
Audio-reactive visuals: the DRIVE layer.

Three timescales, and the whole design rests on keeping them apart:

    COMPOSE   seconds      an LLM decides the mapping, per mood, cached
    DRIVE     ~50 Hz       this file: audio features -> parameters
    RENDER    16 ms        index.html: shader math on the GPU

The AI never touches the render loop. It builds the instrument; maths plays it.

WHY THE AUDIO IS READ HERE AND NOT TAKEN FROM THE RIG: the rig's `inputenv` JSFX already
computes an envelope, but it publishes into gmem - and gmem is JSFX-only, so it cannot
reach the network without being mirrored into a track parameter first. The actual audio is
already arriving on this machine over the Focusrite for the stream, so analysing it here
skips that problem entirely. The split that falls out is clean:

    discrete state  (which mood)     -> OSC from REAPER
    continuous features (the sound)  -> read straight off the interface

WHY SSE AND NOT WEBSOCKETS: this is one-way, server to page, at a fixed rate. Server-Sent
Events do exactly that over plain HTTP with nothing outside the standard library, and they
reconnect on their own. A websocket would add a dependency to buy nothing.

    python server.py --list
    python server.py --device 8
"""

import argparse
import json
import math
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import sounddevice as sd

HERE = Path(__file__).parent
STATE = {
    "sub": 0.0, "low": 0.0, "mid": 0.0, "high": 0.0,
    "rms": 0.0, "centroid": 0.0, "peak": 0.0,
    "mood": "COSMOS", "t": 0.0,
}
_subs = []           # SSE subscriber queues
_lock = threading.Lock()

# Band edges in Hz. Deliberately musical rather than even: sub is the drone's fundamental
# region, mid is where a guitar and voice live, high is where transients and string noise
# read. Even decades would put most of a folk arrangement in one band.
BANDS = {"sub": (25, 90), "low": (90, 300), "mid": (300, 2000), "high": (2000, 8000)}

# ⚠️ TASTE IS ENFORCED HERE, not left to the shader. Attack fast, release slow, so the
# picture has inertia and feels physical instead of snapping on every transient. Snapping
# is the single thing that makes reactive visuals look cheap - and it is also what makes
# them a photosensitivity risk.
ATTACK, RELEASE = 0.35, 0.04

# Silence thresholds, absolute in linear amplitude. GATE ~ -60 dBFS: below it the input is
# an idle preamp, not a performance. AGC_FLOOR ~ -46 dBFS is where the automatic gain stops
# winding up, so the band between them is quiet playing - which still drives the picture,
# faintly. Both are absolute on purpose: a relative threshold cannot separate quiet playing
# from an empty room, because normalising is exactly what erases that difference.
GATE_PEAK, GATE_RMS, AGC_FLOOR = 1.0e-3, 3.0e-4, 5.0e-3
# Where the figure rests with nothing playing: a calm, open Chladni pattern
# rather than black or maximum complexity. Silence should look like stillness.
REST_CENTROID = 0.22


class Analyser:
    def __init__(self, samplerate, blocksize):
        self.sr = samplerate
        self.n = blocksize
        self.window = np.hanning(blocksize).astype(np.float32)
        self.freqs = np.fft.rfftfreq(blocksize, 1.0 / samplerate)
        self.idx = {k: np.where((self.freqs >= lo) & (self.freqs < hi))[0]
                    for k, (lo, hi) in BANDS.items()}
        self.smooth = {k: 0.0 for k in list(BANDS) + ["rms", "centroid"]}
        # Running loudness reference, so the visuals adapt to the room instead of needing
        # a gain setting. A quiet passage should still drive the picture.
        self.ref = 1e-4

    def _slew(self, key, target):
        cur = self.smooth[key]
        a = ATTACK if target > cur else RELEASE
        self.smooth[key] = cur + (target - cur) * a
        return self.smooth[key]

    def process(self, mono):
        if len(mono) < self.n:
            mono = np.pad(mono, (0, self.n - len(mono)))
        spec = np.abs(np.fft.rfft(mono[:self.n] * self.window))

        rms = float(np.sqrt(np.mean(mono ** 2)))
        peak = float(np.max(np.abs(mono)))

        # ⚠️ GATE FIRST, and this is not optional. An AGC with no gate does exactly what
        # it is built to do when handed silence: it winds the gain up until the noise
        # floor fills the range. Measured on this rig with nothing playing - peak 0.0002,
        # and the analyser reported sub=1.0, rms=0.85, centroid=1.0. A full-intensity
        # light show driven entirely by the hiss of an idle preamp, which is the precise
        # opposite of the intent.
        #
        # The gate is ABSOLUTE, in dBFS, not relative to the running reference - a
        # relative gate cannot tell quiet playing from an empty room, because it has
        # normalised away the only thing that distinguishes them.
        if peak < GATE_PEAK and rms < GATE_RMS:
            for k in self.smooth:
                self.smooth[k] *= (1.0 - RELEASE)   # decay out rather than snap to black
            return {k: round(self.smooth.get(k, 0.0), 4) for k in BANDS} | {
                "rms": round(self.smooth["rms"], 4), "peak": round(peak, 4),
                "centroid": round(self.smooth["centroid"], 4)}

        # Slow AGC so the picture adapts to the room rather than needing a gain setting.
        # The floor sits well ABOVE the gate: between the two is genuinely quiet playing,
        # which should still drive the visuals, just gently.
        self.ref = max(self.ref * 0.9995, rms, AGC_FLOOR)

        out = {}
        for k, ix in self.idx.items():
            e = float(np.mean(spec[ix])) if len(ix) else 0.0
            out[k] = round(min(1.0, self._slew(k, e / (self.ref * len(spec) * 0.25 + 1e-9))), 4)

        out["rms"] = round(min(1.0, self._slew("rms", rms / self.ref)), 4)
        out["peak"] = round(float(np.max(np.abs(mono))), 4)

        # Spectral centroid -> Chladni mode number. This is the physically faithful part:
        # on a real plate a higher driving frequency produces a more complex figure, so
        # mapping brightness-of-spectrum to mode count is what the plate actually does.
        #
        # ⚠️ A CENTROID IS UNDEFINED WITHOUT ENERGY, and this is a correctness point
        # rather than a threshold to tune. The ratio is scale-invariant, so near-silence
        # still yields a confident number - and broadband preamp hiss has a HIGH centroid,
        # so an idle input reported 1.0 and would have rendered maximum mode complexity
        # from an empty room. Gating the bands does not fix it, because the centroid never
        # looks at amplitude at all.
        #
        # So it is blended toward a neutral resting value by how much signal there
        # actually is. With no input it settles at REST_CENTROID - a calm, open figure -
        # and only earns a real reading once something is being played.
        tot = float(np.sum(spec)) + 1e-9
        cen = float(np.sum(self.freqs * spec) / tot) / 4000.0
        conf = min(1.0, max(0.0, (rms - GATE_RMS) / (AGC_FLOOR * 2.0)))
        cen = cen * conf + REST_CENTROID * (1.0 - conf)
        out["centroid"] = round(min(1.0, self._slew("centroid", cen)), 4)
        return out


def audio_thread(device, samplerate, blocksize, channels):
    an = Analyser(samplerate, blocksize)

    def cb(indata, frames, time_info, status):
        mono = indata.mean(axis=1) if indata.ndim > 1 else indata
        feats = an.process(np.asarray(mono, dtype=np.float32))
        with _lock:
            STATE.update(feats)
            STATE["t"] = time.time()
            payload = json.dumps(STATE)
            dead = []
            for q in _subs:
                try:
                    # Drop rather than block. A slow client must never stall the audio
                    # callback - that is a realtime thread and backing it up is how you
                    # get glitches in the thing the visuals are supposed to accompany.
                    q.put_nowait(payload)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                if q in _subs:
                    _subs.remove(q)

    with sd.InputStream(device=device, channels=channels, samplerate=samplerate,
                        blocksize=blocksize, dtype="float32", callback=cb):
        while True:
            time.sleep(1)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/feed"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = queue.Queue(maxsize=4)
            with _lock:
                _subs.append(q)
            try:
                while True:
                    data = q.get()
                    self.wfile.write(f"data: {data}\n\n".encode())
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                with _lock:
                    if q in _subs:
                        _subs.remove(q)
            return

        if self.path.startswith("/api/mood"):
            from urllib.parse import urlparse, parse_qs
            name = (parse_qs(urlparse(self.path).query).get("name") or [""])[0].upper()
            if name:
                with _lock:
                    STATE["mood"] = name
            self._json({"mood": STATE["mood"]})
            return

        if self.path.startswith("/api/state"):
            self._json(STATE); return

        name = "index.html" if self.path in ("/", "/index.html") else self.path.lstrip("/")
        f = HERE / name
        # Contain path traversal: a browser source is a local page, but this listens on
        # 0.0.0.0 so the phone can reach it, and that makes it reachable by anything else
        # on the network too.
        if not f.is_file() or HERE.resolve() not in f.resolve().parents:
            self.send_error(404); return
        body = f.read_bytes()
        ctype = "text/html; charset=utf-8" if f.suffix == ".html" else "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--device", type=int, default=None,
                    help="input device index; defaults to the first Focusrite input")
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--samplerate", type=int, default=44100)
    ap.add_argument("--blocksize", type=int, default=1024)   # ~23 ms at 44.1k -> ~43 Hz
    args = ap.parse_args()

    devs = sd.query_devices()
    if args.list:
        for i, d in enumerate(devs):
            if d["max_input_channels"] > 0:
                print(f"{i:>3}  in={d['max_input_channels']}  {d['name']}")
        return

    dev = args.device
    if dev is None:
        dev = next((i for i, d in enumerate(devs)
                    if d["max_input_channels"] > 0 and "Focusrite" in d["name"]), None)
        if dev is None:
            raise SystemExit("no Focusrite input found - pass --device (see --list)")
    ch = min(2, devs[dev]["max_input_channels"])
    # flush=True, and it is not cosmetic. Redirected to a file, Python buffers stdout, so
    # the log stays EMPTY while the process runs - and an empty log is indistinguishable
    # from "the process never started". That cost real time here: three stale servers were
    # left holding the port, every test hit the OLD code, and two fixes appeared not to
    # work when they had simply never been exercised. The empty log was the signal, and it
    # was unreadable. A startup line that actually appears is the cheapest possible proof
    # that the thing under test is the thing you changed.
    print(f"listening on device {dev}: {devs[dev]['name']}  ({ch} ch)", flush=True)
    print(f"visuals at http://localhost:{args.port}/   -> add as an OBS Browser source",
          flush=True)
    print(f"gate {GATE_PEAK} peak / {GATE_RMS} rms   agc floor {AGC_FLOOR}", flush=True)

    threading.Thread(target=audio_thread,
                     args=(dev, args.samplerate, args.blocksize, ch),
                     daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
