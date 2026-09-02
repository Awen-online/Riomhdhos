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

# WARNING: WINDOWS SPAWNS A CONSOLE WINDOW FOR EVERY CHILD CONSOLE PROCESS. adb.exe and
# powershell.exe are console applications, so each call flashed a black terminal on the
# desktop - and opening the Cams tab fires several at once, which is unusable during a
# show. CREATE_NO_WINDOW keeps them headless; it does not exist off Windows, hence getattr.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


SOURCE = "Webcam"
# The wired camera and the virtual-camera bridge for the WiFi one. Both are dshow sources,
# so both take the same filters.
# The WiFi phone reaches OBS through vcambridge and the OBS Virtual Camera sink, not
# through a Media Source - measured 414 ms against 853 ms, and the Media Source path
# has been removed entirely rather than left as a tempting dead end.
CAM_SOURCES = ["Pixel 8", "Pixel 6 (vcam)"]

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
# ⚠️ BOTH PHONES RUN RIGCAM NOW. The Pixel 8 used to reach OBS through GrapheneOS
# DeviceAsWebcam over UVC, which offered zoom presets and a High Quality toggle and NOTHING
# else - no exposure, no white balance. That is why the two cameras could not be matched:
# grading in OBS got black level within 5 but left mid-tones at 152 against 98. With both on
# RigCam they take the SAME explicit ISO, shutter and WB gains, so they match by construction
# and cannot drift apart mid-set. Keyed by label because the UI has to say which phone it is
# about, and the two are physically hard to tell apart on a dark stage.
RIGCAMS = {}                           # {"Pixel 6": url, "Pixel 8": url}
UVCZOOM = HERE.parent / "uvczoom.py"
POWER = HERE.parent / "power.py"
UVC_ZOOMS = ("0.5", "1.0", "2.0")
# ⚠️ The wired phone is named explicitly. Two phones are attached in normal use and the
# wired-camera controls MUST NOT land on the WiFi one - doing so launches DeviceAsWebcam
# over RigCam and kills its stream.
UVC_SERIAL = None
ADB = r"C:\Users\mccul\Android\Sdk\platform-tools\adb.exe"

# ⚠️ CACHED. Each phone costs an adb round trip, and this is identity information that
# changes on the timescale of an OS update, not a song.
_dev_cache = {"at": 0.0, "data": []}
_DEV_TTL = 120.0


def adb_devices():
    try:
        out = subprocess.run([ADB, "devices"], capture_output=True, text=True,
                             timeout=10, creationflags=NO_WINDOW).stdout
    except Exception:
        return []
    return [l.split()[0] for l in out.splitlines()[1:]
            if l.strip() and l.split()[-1] == "device"]


def device_info(serial):
    """Identity + health for one phone, in one shell round trip."""
    script = (
        'echo model=$(getprop ro.product.model);'
        'echo device=$(getprop ro.product.device);'
        'echo android=$(getprop ro.build.version.release);'
        'echo build=$(getprop ro.build.display.id);'
        'echo patch=$(getprop ro.build.version.security_patch);'
        'echo verifiedboot=$(getprop ro.boot.verifiedbootstate);'
        'echo usb=$(getprop sys.usb.config);'
        'echo ip=$(ip -f inet addr show wlan0 2>/dev/null | grep -o "inet [0-9.]*" | head -1 | cut -d" " -f2);'
        'echo level=$(dumpsys battery | grep -m1 "  level:" | tr -dc "0-9");'
        'echo status=$(dumpsys battery | grep -m1 "  status:" | tr -dc "0-9");'
        'echo tempc=$(dumpsys battery | grep -m1 "  temperature:" | tr -dc "0-9");'
        'echo maxcur=$(dumpsys battery | grep -m1 "Max charging current" | tr -dc "0-9");'
        'echo now=$(cat /sys/class/power_supply/battery/current_now 2>/dev/null);'
        'echo cycles=$(cat /sys/class/power_supply/battery/cycle_count 2>/dev/null);'
        'echo full=$(cat /sys/class/power_supply/battery/charge_full 2>/dev/null);'
        'echo design=$(cat /sys/class/power_supply/battery/charge_full_design 2>/dev/null);'
    )
    d = {"serial": serial}
    try:
        out = subprocess.run([ADB, "-s", serial, "shell", script],
                             capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW).stdout
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    except Exception as e:
        d["error"] = type(e).__name__
    # Health as a percentage of DESIGN capacity - the number that matters on a used phone,
    # and one no settings screen shows.
    try:
        d["healthPct"] = round(100 * int(d["full"]) / int(d["design"]))
    except Exception:
        d["healthPct"] = None
    # ⚠️ RUNTIME, NOT JUST PERCENTAGE. A phone at 45% is fine or nearly dead depending on
    # what it is drawing, and these draw a LOT while streaming - measured -505 mA with the
    # screen held awake. "45%" alone has told nobody anything useful; "4.0 h left" has.
    try:
        d["tempC"] = round(int(d["tempc"]) / 10.0, 1)
    except Exception:
        d["tempC"] = None
    try:
        ua = int(d["now"])                       # microamps; negative means discharging
        d["mA"] = round(ua / 1000.0)
        mah_now = int(d["full"]) / 1000.0 * int(d["level"]) / 100.0
        if ua < 0:
            d["hours"] = round(mah_now / (abs(ua) / 1000.0), 1)
            d["charging"] = False
        elif ua > 0:
            headroom = int(d["full"]) / 1000.0 - mah_now
            d["hours"] = round(headroom / (ua / 1000.0), 1)
            d["charging"] = True
        else:
            d["hours"], d["charging"] = None, None
    except Exception:
        d["mA"], d["hours"], d["charging"] = None, None, None
    try:
        d["supplyMA"] = round(int(d["maxcur"]) / 1000)
    except Exception:
        d["supplyMA"] = None
    d["role"] = "wired" if serial == UVC_SERIAL else "wifi"
    return d


def devices_snapshot():
    now = time.time()
    if now - _dev_cache["at"] < _DEV_TTL and _dev_cache["data"]:
        return _dev_cache["data"]
    data = [device_info(x) for x in adb_devices()]
    # WARNING: A PHONE THAT IS DOWN MUST STILL APPEAR. This list used to be exactly what adb
    # could see, so a configured phone that dropped off USB simply VANISHED from the Phones
    # card - which reads as "never set up" rather than "this one is broken", and sends you
    # looking for a configuration bug instead of a cable. Anything named with --phone that
    # adb cannot see gets a placeholder row instead of silence.
    seen = {d.get("model") for d in data}
    for label in RIGCAMS:
        if label not in seen:
            data.append({"model": label, "serial": None, "absent": True})
    _dev_cache.update(at=now, data=data)
    return data


def rigcam_call(path, timeout=2.5, base=None):
    """Talk to a RigCam app. Never raises - an unreachable phone is a normal state."""
    try:
        with urllib.request.urlopen((base or RIGCAM) + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"offline": True, "error": type(e).__name__}


def rigcams_state():
    """Every phone at once. Sequential is fine: each call is a 2.5 s ceiling on localhost or
    the LAN, and this endpoint is already on-demand rather than in the 1 Hz poll."""
    return {label: rigcam_call("/api/state", base=url) for label, url in RIGCAMS.items()}


def uvc_call(*args, timeout=30):
    """Run uvczoom.py. Reuses the tested tool rather than restating its ADB handling."""
    if not UVCZOOM.exists():
        return {"ok": False, "out": "uvczoom.py not found"}
    try:
        cmd = [sys.executable, str(UVCZOOM), *args]
        if UVC_SERIAL:
            cmd += ["--serial", UVC_SERIAL]
        p = subprocess.run(cmd,
                           capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
        out = (p.stdout or p.stderr or "").strip()
        return {"ok": p.returncode == 0, "out": out[-400:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "out": "timed out - is the phone awake and unlocked?"}
    except Exception as e:
        return {"ok": False, "out": f"{type(e).__name__}: {e}"}


def power_call(mode, timeout=120):
    """sleep / show / status, via video/power.py."""
    if not POWER.exists():
        return {"ok": False, "out": "power.py not found"}
    cmd = [sys.executable, str(POWER), mode]
    # Ask for structured output on status so the browser reads FIELDS, never prose.
    if mode == "status":
        cmd += ["--json"]
    if UVC_SERIAL:
        cmd += ["--wired-serial", UVC_SERIAL]
    # ⚠️ PASS THE PHONES. power.py sleeps an adb phone by force-stopping RigCam and a
    # non-adb one over HTTP, and it cannot do the second without being told the URL. The
    # dashboard already has them; not forwarding them is what made the Sleep button leave
    # the WiFi phone streaming to nobody.
    for label, url in RIGCAMS.items():
        cmd += ["--phone", f"{label}={url}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
        out = (r.stdout or r.stderr or "").strip()
        result = {"ok": r.returncode == 0, "out": out[-800:]}
        if mode == "status" and r.returncode == 0:
            try:
                result["rows"] = json.loads(out)
            except Exception as e:
                # Report the failure rather than silently falling back to no rows - the
                # last time this data path degraded quietly the UI confidently lied.
                result["rowsError"] = f"{type(e).__name__}: {e}"
        return result
    except subprocess.TimeoutExpired:
        return {"ok": False, "out": "timed out - is a phone locked or unplugged?"}
    except Exception as e:
        return {"ok": False, "out": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- the vcam bridges
# WARNING: A BRIDGE THAT IS "RUNNING" PROVES NOTHING. When the sink goes away underneath
# one - OBS closed, the machine slept - it blocks inside cam.send() with the process still
# alive, the task still Running and the newest log line an hour old. Windows sees nothing
# wrong, so nothing restarts it. Health here is therefore FRESHNESS, read from the bridge's
# own log, and the reset KILLS FIRST and asks the task second: a stalled bridge is blocked
# in a C call and will not honour a polite stop.
BRIDGE_LOGS = Path(r"C:\Users\mccul\rig\logs")
BRIDGES = [
    {"task": "Riomhdhos vcam bridge", "label": "Pixel 6 (WiFi)", "log": BRIDGE_LOGS / "vcam-p6.log"},
    {"task": "Riomhdhos vcam bridge P8", "label": "Pixel 8 (USB)", "log": BRIDGE_LOGS / "vcam-p8.log"},
]
BRIDGE_FRESH_S = 90        # they report frames every 30 s, so this is three missed reports
# The OBS source each bridge ends up in. The reset needs these by name - see the note in
# bridge_reset about who is holding the sink.
BRIDGE_SOURCES = ["Pixel 6 (vcam)", "Pixel 8"]


def _task_state(task):
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", task, "/FO", "LIST"],
                           capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW)
        for line in r.stdout.splitlines():
            if line.strip().lower().startswith("status:"):
                return line.split(":", 1)[1].strip().lower()
    except Exception:
        pass
    return "unknown"


def _last_line(path):
    try:
        lines = [l.rstrip() for l in
                 path.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
        return lines[-1] if lines else ""
    except Exception:
        return ""


def bridge_status():
    out = []
    for b in BRIDGES:
        try:
            age = time.time() - b["log"].stat().st_mtime
        except Exception:
            age = None
        state = _task_state(b["task"])
        last = _last_line(b["log"])
        out.append({
            "task": b["task"], "label": b["label"], "state": state,
            "ageS": None if age is None else round(age, 1),
            # feeding, stalled, or not running at all - three states, because "stalled" is
            # the one that used to be invisible
            # ⚠️ Freshness alone is not health: a bridge failing to open its sink writes an
            # error every two seconds, which is the freshest log on the machine. What the
            # last line SAYS decides it.
            "health": (("failing" if ("session failed" in last or "STALLED" in last)
                        else "feeding")
                       if (age is not None and age < BRIDGE_FRESH_S and state == "running")
                       else "stalled" if state == "running" else "stopped"),
            "last": last[-140:],
        })
    return out


def bridge_reset():
    steps = []
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | "
          "Where-Object { $_.CommandLine -like '*vcambridge*' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; "
          "Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe'\" | "
          "Where-Object { $_.CommandLine -like '*stream.h264*' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
        steps.append("stopped the bridges and their ffmpeg")
    except Exception as e:
        steps.append(f"kill failed: {type(e).__name__}")
    # ⚠️ AND NOW LET GO OF THE SINK. A producer killed outright does not release the OBS
    # Virtual Camera cleanly while a CONSUMER still holds it open - and OBS's own dshow
    # source is exactly that consumer. The restarted bridge then retries every two seconds
    # with "virtual camera output could not be started" and never gets in. Cycling the
    # source drops OBS's handle for a moment, which is all the new producer needs.
    # Measured: the bridge failed 30 times in a row, then caught the sink on the first
    # attempt after a cycle.
    try:
        import obsctl
        cl = obsctl.connect(timeout=4)
        for src in BRIDGE_SOURCES:
            try:
                obsctl._cycle(cl, src, settle=0.5)
                steps.append("released " + src)
            except Exception:
                steps.append("could not cycle " + src)
    except Exception:
        # OBS closed is a perfectly normal state here - it is often WHY the reset is needed
        steps.append("OBS not reachable - skipped releasing the sources")

    for b in BRIDGES:
        for verb in ("/End", "/Run"):
            try:
                subprocess.run(["schtasks", verb, "/TN", b["task"]], capture_output=True,
                               text=True, timeout=30, creationflags=NO_WINDOW)
            except Exception:
                pass
        steps.append("restarted " + b["label"])
    # Deliberately does NOT wait for the sinks to come back. Each bridge re-opens its OBS
    # source once frames are actually flowing, which takes several seconds, and a request
    # that blocks that long reads as a hung dashboard. The status rows tell the truth a
    # moment later.
    return {"ok": True, "steps": steps}


# ---------------------------------------------------------------- phone settings reset
# The baseline is imported from rigsettings.py rather than restated here. Two copies of
# "what the cameras should be set to" is exactly the drift this whole thing exists to stop.
sys.path.insert(0, str(HERE.parent))
try:
    from rigsettings import BASELINE as RIG_BASELINE, VERIFY as RIG_VERIFY, close_enough as rig_close
except Exception as _e:
    RIG_BASELINE, RIG_VERIFY, rig_close = {}, {}, None


def phone_reset(label):
    """Put one phone back on the show baseline, and READ IT BACK. RigCam drops a whole
    request when a single value is out of range for that sensor, and the two phones do not
    have the same ranges - so an `applied` list is not evidence."""
    url = RIGCAMS.get(label)
    if not url:
        return {"error": f"unknown phone {label!r}"}, 400
    if not RIG_BASELINE or rig_close is None:
        return {"error": "rigsettings.py could not be imported"}, 500
    applied = rigcam_call("/api/set?" + urllib.parse.urlencode(RIG_BASELINE), base=url, timeout=20)
    if applied.get("offline"):
        return {"error": "phone is not answering", "detail": applied}, 502
    time.sleep(3)                      # a resolution change rebinds the capture session
    st = rigcam_call("/api/state", base=url, timeout=10)
    drift = []
    for k, want in RIG_BASELINE.items():
        got = None
        try:
            got = RIG_VERIFY[k](st)
        except Exception:
            pass
        if not rig_close(want, got):
            drift.append(f"{k}={got} (want {want})")
    return {"phone": label, "applied": applied.get("applied", []), "drift": drift,
            "ok": not drift}, (200 if not drift else 502)


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
# ⚠️ RE-ENTRANT, AND IT GUARDS EVERY REQUEST - NOT JUST RECONNECTION.
#
# obsws-python's ReqClient is NOT thread-safe: it writes a request to the websocket and
# then reads the NEXT response off it. Two threads in flight at once therefore hand each
# other's replies back. This server is a ThreadingHTTPServer, so it always had some
# exposure, but the 1 Hz scene-preset watcher made it constant - a second caller issuing
# a request every single second.
#
# The symptom was beautifully specific: after adding that watcher, scene presets applied
# ZOOM but not FILTERS. Zoom is HTTP straight to the phone and never touches this socket;
# filters go through OBS, and their replies were being consumed by the watcher's
# GetCurrentProgramScene. The same crossing produced a 500 reading
# "GetSourceScreenshotDataclass has no attribute filter_settings" - a filter request that
# was handed a screenshot's response - which was written off as transient. It was not.
_clock = threading.RLock()


class _LockedClient:
    """Serialises every call onto the shared ReqClient.

    A proxy rather than a lock at each call site: there are dozens of call sites, and the
    one that gets forgotten is the one that corrupts a response during a show.
    """

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def call(*a, **kw):
            with _clock:
                return attr(*a, **kw)

        return call


def client():
    """One shared OBS connection, reconnected on demand - a dropped websocket must not
    need a restart of a process meant to be left running through a show."""
    global _cl
    with _clock:
        try:
            _cl.get_version()
        except Exception:
            _cl = obsctl.connect(timeout=10)
        return _LockedClient(_cl)


def publish(feats=None):
    """Stamp the clock and push STATE to every /feed subscriber."""
    with _lock:
        if feats:
            STATE.update(feats)
        STATE["t"] = time.time()
        payload = json.dumps(STATE)
        dead = []
        for q in _subs:
            try:
                q.put_nowait(payload)      # drop, never block: this may be a realtime thread
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subs.remove(q)


def clock_thread(hz=20):
    """Drive the visuals when there is no audio.

    ⚠️ THE ANIMATION CLOCK USED TO LIVE INSIDE THE AUDIO CALLBACK. STATE["t"] - which is
    what the shader animates against - was only ever stamped when a sample buffer arrived,
    so running with --no-audio left /feed emitting NOTHING and every generative visual
    frozen at black. That is not hypothetical: disabling the Focusrite for the BSOD
    elimination test silently took the pre-show visuals down with it, and nothing reported
    it, because the page still served and the source still had a resolution.

    Audio should MODULATE the visuals, never be the thing that makes them move. This ticker
    only fills in when the audio thread is not publishing, so with audio present it costs
    nothing and changes nothing.
    """
    period = 1.0 / hz
    while True:
        time.sleep(period)
        with _lock:
            quiet = time.time() - STATE.get("t", 0) > 0.5
        if quiet:
            publish()


def audio_thread(device, samplerate, blocksize, channels):
    an = Analyser(samplerate, blocksize)

    def cb(indata, frames, tinfo, status):
        mono = indata.mean(axis=1) if indata.ndim > 1 else indata
        publish(an.process(np.asarray(mono, dtype=np.float32)))

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


# ---------------------------------------------------------------- scene presets
# ⚠️ WHY THIS IS NOT JUST "PUT FILTERS ON THE SCENE". OBS filters belong to the SOURCE,
# and both cameras are one input shared by every scene - so a filter toggled in BOTH CAMS
# is toggled in BROWSER too. Duplicating the source is not available either: DirectShow
# allows exactly one consumer per device. And zoom is not an OBS property at all, it is a
# setting on the phone reached over HTTP. One watcher covers both.
PRESETS = HERE.parent / "scenepresets.json"
PRESET_LOG = Path(r"C:\Users\mccul\rig\logs\scenepresets.log")
_presets_cache = {"mtime": 0.0, "data": None}


def load_presets():
    """Re-read on mtime change, so hand edits apply without restarting the dashboard."""
    try:
        m = PRESETS.stat().st_mtime
    except OSError:
        return {"enabled": False, "scenes": {}}
    if _presets_cache["data"] is None or m != _presets_cache["mtime"]:
        try:
            with open(PRESETS, encoding="utf-8") as fh:
                data = json.load(fh)
            data.setdefault("enabled", True)
            data.setdefault("scenes", {})
            _presets_cache.update(mtime=m, data=data)
        except Exception as e:
            print(f"scenepresets: unreadable ({e}); presets disabled", flush=True)
            return {"enabled": False, "scenes": {}}
    return _presets_cache["data"]


def save_presets(data):
    data.setdefault("enabled", True)
    data.setdefault("scenes", {})
    tmp = PRESETS.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    tmp.replace(PRESETS)              # atomic: never leave a half-written config
    _presets_cache["data"] = None
    return load_presets()


def obs_source_for(phone):
    """'Pixel 6' -> 'Pixel 6 (vcam)'. The OBS source name and the phone label differ."""
    for n in CAM_SOURCES:
        if n == phone or n.startswith(phone):
            return n
    return None


def apply_preset(scene):
    """Apply one scene's preset. Returns a list of human-readable steps taken.

    ⚠️ NEVER RAISES PAST THE CALLER. This runs from a watcher thread during a live scene
    change; a phone that is asleep or an OBS mid-restart must not kill the watcher and
    must not stop the OTHER camera being set.
    """
    preset = (load_presets().get("scenes") or {}).get(scene)
    if not preset:
        return []
    steps = []
    for phone, want in preset.items():
        src = obs_source_for(phone)
        for name, on in (want.get("filters") or {}).items():
            if src is None:
                steps.append(f"{phone}: no OBS source")
                continue
            try:
                client().set_source_filter_enabled(src, name, bool(on))
                steps.append(f"{src}: {name} {'on' if on else 'off'}")
            except Exception as e:
                steps.append(f"{src}: {name} failed ({type(e).__name__})")
        if "zoom" in want:
            url = RIGCAMS.get(phone)
            if not url:
                steps.append(f"{phone}: no rigcam url")
            else:
                try:
                    # ⚠️ Zoom is a round trip to the phone and takes ~1-2 s to settle. A
                    # preset suits a deliberate scene change, not a fast cut.
                    # rigcam_call takes a path, not params - build the query.
                    q = urllib.parse.urlencode({"zoom": want["zoom"]})
                    r = rigcam_call("/api/set?" + q, base=url)
                    if isinstance(r, dict) and r.get("offline"):
                        steps.append(f"{phone}: zoom unreachable ({r.get('error')})")
                    else:
                        steps.append(f"{phone}: zoom {want['zoom']}")
                except Exception as e:
                    steps.append(f"{phone}: zoom failed ({type(e).__name__})")
    return steps


def capture_preset(scene):
    """Snapshot what the cameras are doing RIGHT NOW into this scene's preset.

    The useful way to author these: get the look right in OBS and on the phones by hand,
    then press Capture. Hand-writing zoom ratios and filter names is how they drift.
    """
    data = load_presets()
    entry = {}
    for phone, url in RIGCAMS.items():
        src = obs_source_for(phone)
        one = {}
        st = rigcam_call("/api/state", base=url)
        if isinstance(st, dict) and not st.get("offline") and st.get("zoom"):
            one["zoom"] = round(float(st["zoom"].get("ratio", 1.0)), 3)
        if src:
            try:
                fl = client().get_source_filter_list(src).filters
                one["filters"] = {f["filterName"]: bool(f["filterEnabled"]) for f in fl
                                  if f["filterKind"] in ALLOWED}
            except Exception:
                pass
        if one:
            entry[phone] = one
    data.setdefault("scenes", {})[scene] = entry
    save_presets(data)
    return entry


def scene_preset_thread(poll_s=1.0):
    """Watch the PROGRAM scene and apply presets on change.

    ⚠️ POLLED, NOT EVENT-SUBSCRIBED, deliberately. An EventClient is a second websocket
    with its own reconnect story, and this rig restarts OBS regularly. A 1 Hz poll inside
    a try/except reconnects for free and cannot wedge.

    ⚠️ AND IT DOES NOT FIRE ON THE FIRST OBSERVATION. Applying a preset at startup would
    silently overwrite whatever was set up by hand before the dashboard came up. The first
    scene it sees is recorded, not acted on.
    """
    last = None
    while True:
        try:
            cfg = load_presets()
            cur = client().get_current_program_scene().scene_name
            if last is None:
                last = cur                      # record, do not apply
            elif cur != last:
                last = cur
                if cfg.get("enabled"):
                    steps = apply_preset(cur)
                    if steps:
                        # ⚠️ TO A FILE, NOT stdout. This runs under pythonw from a
                        # scheduled task, which has nowhere to print - so a preset that
                        # silently failed left no trace anywhere. Same lesson as the
                        # bridges' --log.
                        line = (time.strftime("%Y-%m-%d %H:%M:%S") +
                                f"  {cur}: " + "; ".join(steps))
                        print(line, flush=True)
                        try:
                            PRESET_LOG.parent.mkdir(parents=True, exist_ok=True)
                            with open(PRESET_LOG, "a", encoding="utf-8") as fh:
                                fh.write(line + "\n")
                        except Exception:
                            pass
        except Exception:
            # OBS closed or mid-restart. Try again next tick.
            pass
        time.sleep(poll_s)


def camera_sources():
    """Every camera that exists and can carry filters.

    ⚠️ Both cameras deserve the same treatment. The panel used to act on one hardcoded
    source, so the WiFi camera could not be blurred, lit or colour-corrected at all - and
    the two feeds cut together badly precisely because only one of them was ever graded.
    """
    cl = client()
    have = {i["inputName"] for i in cl.get_input_list().inputs}
    return [n for n in CAM_SOURCES if n in have]


def filter_state(source=None):
    cl = client()
    source = source or SOURCE
    out = []
    for f in cl.get_source_filter_list(source).filters:
        kind = f["filterKind"]
        if kind not in ALLOWED:
            continue
        s = cl.get_source_filter(source, f["filterName"]).filter_settings
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

    def _obs_or_none(self):
        """The OBS client, or None. Callers report the outage instead of crashing."""
        try:
            return client()
        except (Exception, SystemExit):
            return None

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
                    "rigcams": rigcams_state(),
                    "uvc": {"reachable": uvc["ok"], "selected": uvc_selected(uvc["out"]),
                            "zooms": list(UVC_ZOOMS), "detail": uvc["out"]},
                    "devices": devices_snapshot(),
                    "wiredSerial": UVC_SERIAL,
                    "power": power_call("status", timeout=60),
                    "bridges": bridge_status(),
                }); return

            if p == "/api/scenepresets":
                cl = self._obs_or_none()
                scenes = []
                cur = None
                if cl:
                    try:
                        sl = cl.get_scene_list()
                        scenes = [x["sceneName"] for x in sl.scenes]
                        cur = sl.current_program_scene_name
                    except Exception:
                        pass
                self._json({
                    "presets": load_presets(),
                    "obsScenes": scenes,
                    "currentScene": cur,
                    "phones": list(RIGCAMS.keys()),
                }); return

            if p == "/api/state":
                with _lock:
                    audio = dict(STATE)
                cl = self._obs_or_none()
                if cl is None:
                    # Everything that does not need OBS still answers.
                    self._json({"audio": audio, "obs": False, "code": CODE_HASH,
                                "source": SOURCE, "scenes": [], "moods": MOODS,
                                "cameraFilters": {}, "filters": [],
                                "error": "OBS is not running"})
                    return
                sl = cl.get_scene_list()
                st = cl.get_stats()
                v = cl.get_video_settings()
                fps = v.fps_numerator / max(1, v.fps_denominator)
                # Render time against budget is the number that decides whether another
                # filter or overlay can be afforded. Two inference filters plus a browser
                # source is most of a 33 ms frame, and nothing else on the machine says so.
                self._json({
                    "audio": audio, "filters": filter_state(),
                    "cameraFilters": {n: filter_state(n) for n in camera_sources()},
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

            # ⚠️ ROUTES THAT NEED NO OBS ARE HANDLED BEFORE CONNECTING TO IT. This used to
            # be an unconditional `cl = client()` right here, so with OBS closed EVERY post
            # died - including /api/power, whose whole job is putting the phones to sleep
            # and has nothing to do with OBS. The connection was refused, SystemExit escaped
            # the handler, and the client got no response at all: not an error, silence.
            #
            # ⚠️ AND IT IS NOT JUST /api/power. That fix moved ONE route above the guard and
            # left /api/camera below it, so with OBS closed every camera tap came back 503
            # "OBS is not running" - for a request whose entire path is dashboard -> phone.
            # Framing a shot before opening OBS is the normal order of work, so this is the
            # case that matters. Anything that does not touch OBS belongs above the guard.
            if p == "/api/power":
                mode = body.get("mode")
                # 'normal' is what the button says; 'show' is what power.py has always
                # called it. Accept both rather than leave a label that does not match the
                # wire - that mismatch is exactly where a stale client starts 400ing.
                if mode == "normal":
                    mode = "show"
                if mode not in ("sleep", "show"):
                    self._json({"error": "mode must be sleep or normal"}, 400); return
                r = power_call(mode)
                _dev_cache["at"] = 0          # battery figures are about to change a lot
                self._json({"mode": mode, "result": r}, 200 if r["ok"] else 502); return

            # Neither of these touches OBS, so both sit ABOVE the guard - and the bridge
            # reset especially, since the state it repairs is usually "OBS was just
            # restarted and the bridges are still feeding a sink that went away".
            if p == "/api/scenepresets":
                act = body.get("action")
                if act == "enable":
                    data = load_presets()
                    data["enabled"] = bool(body.get("enabled", True))
                    self._json({"presets": save_presets(data)}); return
                if act == "save":
                    data = load_presets()
                    scene, entry = body.get("scene"), body.get("entry")
                    if not scene:
                        self._json({"error": "scene required"}, 400); return
                    if entry is None:
                        data.get("scenes", {}).pop(scene, None)
                    else:
                        data.setdefault("scenes", {})[scene] = entry
                    self._json({"presets": save_presets(data)}); return
                # capture and apply both need OBS.
                if not self._obs_or_none():
                    self._json({"error": "OBS is not running", "obs": False}, 503); return
                if act == "capture":
                    scene = body.get("scene") or client().get_current_program_scene().scene_name
                    self._json({"scene": scene, "entry": capture_preset(scene),
                                "presets": load_presets()}); return
                if act == "apply":
                    scene = body.get("scene") or client().get_current_program_scene().scene_name
                    self._json({"scene": scene, "steps": apply_preset(scene)}); return
                self._json({"error": "action must be save, capture, apply or enable"}, 400)
                return

            if p == "/api/bridges":
                if body.get("action") != "reset":
                    self._json({"error": "action must be reset"}, 400); return
                self._json(bridge_reset()); return

            if p == "/api/phonereset":
                payload, code = phone_reset(body.get("phone"))
                self._json(payload, code); return

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
                    # Which phone. Unknown label is an error, NOT a silent fall-back to the
                    # default one - sending an exposure change to the wrong camera mid-set is
                    # worse than doing nothing, and far harder to notice.
                    phone = body.get("phone")
                    if phone is not None and phone not in RIGCAMS:
                        self._json({"error": f"unknown phone '{phone}'"}, 400); return
                    base = RIGCAMS.get(phone) if phone else None
                    # Allowlist: only these reach the phone, and each is range-clamped there.
                    q = {}
                    for k in ("zoom", "linearZoom", "ev", "aeLock", "awbLock", "torch",
                              "bitrate", "fps", "facing", "resolution",
                              # Manual sensor and colour. These exist so BOTH cameras can be
                              # given the same explicit numbers instead of two auto algorithms
                              # drifting apart mid-set - grading in OBS got black level within
                              # 5 but could not close a mid-tone gap of 152 against 98.
                              "iso", "shutter", "shutterNs", "manualExposure",
                              "wbR", "wbG", "wbB", "manualWb",
                              "faceTrack", "stabilize"):
                        if k in body and body[k] is not None:
                            v = body[k]
                            q[k] = str(v).lower() if isinstance(v, bool) else str(v)
                    if not q:
                        self._json({"error": "nothing to do"}, 400); return
                    self._json({"phone": phone, "rigcam": rigcam_call(
                        "/api/set?" + urllib.parse.urlencode(q), base=base)}); return

                self._json({"error": "target must be uvc or rigcam"}, 400); return

            cl = self._obs_or_none()
            if cl is None:
                self._json({"error": "OBS is not running", "obs": False}, 503); return

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
                src = body.get("source") or SOURCE
                cur = next((f for f in filter_state(src) if f["name"] == name), None)
                if not cur:
                    self._json({"error": f"no filter '{name}' on '{src}'"}, 404); return
                cl.set_source_filter_enabled(src, name, not cur["enabled"])
                self._json({"source": src, "filters": filter_state(src)}); return

            if p == "/api/set":
                name = body.get("filter")
                src = body.get("source") or SOURCE
                cur = next((f for f in filter_state(src) if f["name"] == name), None)
                if not cur:
                    self._json({"error": f"no filter '{name}' on '{src}'"}, 404); return
                allow = ALLOWED[cur["kind"]]
                out = {}
                for k, v in (body.get("params") or {}).items():
                    if k not in allow:          # allowlist: fails closed by construction
                        continue
                    lo, hi, step, _ = allow[k]
                    val = max(lo, min(hi, float(v)))
                    out[k] = int(val) if isinstance(step, int) else val
                if out:
                    cl.set_source_filter_settings(src, name, out, True)
                self._json({"source": src, "applied": out}); return

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
    ap.add_argument("--phone", action="append", default=[], metavar="LABEL=URL",
                    help="a RigCam phone, repeatable: --phone \"Pixel 6=http://...:8090\". "
                         "The label is what the Cams tab shows, so use the phone's name.")
    ap.add_argument("--samplerate", type=int, default=44100)
    ap.add_argument("--blocksize", type=int, default=1024)
    ap.add_argument("--no-audio", action="store_true",
                    help="filter control only, no audio capture")
    args = ap.parse_args()

    threading.Thread(target=clock_thread, daemon=True).start()

    global SOURCE, RIGCAM, UVC_SERIAL, RIGCAMS
    SOURCE = args.source
    RIGCAM = args.rigcam
    UVC_SERIAL = args.uvc_serial
    for spec in args.phone:
        label, _, url = spec.partition("=")
        if not url:
            sys.exit(f"--phone wants LABEL=URL, got {spec!r}")
        RIGCAMS[label.strip()] = url.strip().rstrip("/")
    # One phone given the old way still works, so nothing that already runs has to change.
    if not RIGCAMS:
        RIGCAMS["Phone"] = RIGCAM
    # ⚠️ DO NOT DIE IF OBS IS CLOSED. This used to be a hard `client()` at startup - "fail
    # now, not on the phone's first tap" - which meant closing OBS took the whole dashboard
    # down with it, including the Phones card, the battery readings and the camera controls,
    # none of which need OBS at all. A control surface that vanishes when one of the things
    # it controls is shut is worse than one that reports the outage.
    # ⚠️ (Exception, SystemExit), NOT just Exception. obsctl.connect() reports a missing
    # OBS by calling sys.exit() with a friendly message - and SystemExit inherits from
    # BaseException, so `except Exception` sails straight past it and the process still
    # dies. The guard looked right and did nothing.
    try:
        client()
        print("OBS: connected", flush=True)
    except (Exception, SystemExit) as e:
        print(f"OBS: NOT connected ({type(e).__name__}) - serving everything else; "
              "scene and filter controls will report it", flush=True)

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
    threading.Thread(target=scene_preset_thread, daemon=True).start()
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
    # cp1252 is the console default here, and a stray non-ASCII character in a print has
    # now killed three separate tools AT THE MOMENT THEY HAD SOMETHING USEFUL TO SAY.
    # Degrade the character, never the process.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
