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
import os
import queue
import random
import re
import socket
import ssl
import subprocess
import urllib.error
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
# ---------------------------------------------------------------- access token
#
# ⚠️ OPT-IN, NOT OPT-OUT, AND THAT IS A DELIBERATE REVERSAL. This first shipped
# generating a token on startup and requiring it from anywhere but loopback. That is the
# safer default in the abstract and it was the wrong one here: it silently broke the phone
# bookmark that is this dashboard's primary way of being used, mid-session, with no
# warning - and the comment claiming you could "delete the file to disable it" was false,
# because startup recreated it. A security control that breaks the instrument during a set
# will be ripped out at the worst moment, so it does not get to be the default.
#
# No file  -> no auth. Exactly the behaviour this had for its whole life before today.
# A file   -> its contents are required from anything that is not loopback.
#
# Turn it on for the case that actually warrants it - the rig on a venue or hotel network,
# where the LAN is full of strangers:
#     python -c "import secrets;print(secrets.token_urlsafe(18))" > %USERPROFILE%\.riastrad-token
# then restart the task. Delete the file and restart to go back.
TOKEN_FILE = Path.home() / ".riastrad-token"
try:
    TOKEN = TOKEN_FILE.read_text(encoding="utf-8").strip() if TOKEN_FILE.exists() else ""
except Exception:
    TOKEN = ""
    print("WARNING: access token file exists but could not be read; "
          "network access is UNAUTHENTICATED", flush=True)

# ---------------------------------------------------------------- the stream relay
# The relay is deliberately STANDALONE: its own folder, its own scheduled task, its own
# retry loop. Riastrad reads it and can restart it, but never owns it - so the same folder
# runs on a laptop somewhere else with no dashboard at all.
RELAY_DIR = Path(r"C:\Users\mccul\Awen\stream-relay")
RELAY_STATUS = RELAY_DIR / "status"
RELAY_CONNECT = RELAY_DIR / "connect.py"
RELAY_TASK = "Awen stream relay"
MEDIAMTX_API = "http://127.0.0.1:9997"
# ⚠️ A DESTINATION IS GREEN ONLY WHILE BYTES ARE MOVING TO IT. The .state file the relay
# writes is a HINT, not evidence: MediaMTX hard-kills the supervisor when the source goes
# away, so its finally block may never run and .state can sit there reading "running" over
# a dead push. This is the same shape as the bridge bug where retry spam kept a log fresh
# and a switched-off phone reported "feeding". Judge by the progress file: recently written
# AND total_size advancing.
EGRESS_FRESH_S = 5.0

_egress_seen = {}          # name -> (total_size, first time we saw that value)
# ⚠️ CACHED BECAUSE IT IS A SUBPROCESS, NOT BECAUSE IT IS SLOW. `connect.py status --json`
# measures 195 ms, which is almost entirely interpreter startup. At the 2 s poll that is a
# tenth of the dashboard's duty cycle spent launching Python to be told the same thing.
# Setup state only changes when a file is edited or a connect flow is run, so seconds of
# staleness cost nothing - unlike the egress health beside it, which is read every poll.
_plat_cache = {"at": 0.0, "data": []}
_PLAT_TTL = 5.0

_dev_cache = {"at": 0.0, "data": []}
_DEV_TTL = 120.0
# Below this draw, "hours left" is arithmetic on measurement noise. These phones pull
# ~500 mA while streaming, so anything under 50 mA means the battery is effectively
# balanced and no honest runtime can be quoted - see the note in device_info.
RUNTIME_MIN_MA = 50


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
        'echo hwserial=$(getprop ro.serialno);'
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
        # ⚠️ A RUNTIME DIVIDED BY A NEAR-ZERO CURRENT IS NOISE, NOT NEWS. Only exactly 0 was
        # guarded here, so a phone sitting balanced on its charger at -4 mA reported
        # "891.7 h left" - thirty-seven days, for a handset that lasts a couple of hours
        # while streaming. One absurd number is enough to teach you to stop reading the
        # field that exists precisely to shout when the battery is short. Below this
        # threshold the reading is dominated by measurement noise and the division
        # explodes, so say nothing rather than something false.
        if abs(ua) < RUNTIME_MIN_MA * 1000:
            d["hours"] = None
            d["charging"] = None if ua == 0 else ua > 0
        elif ua < 0:
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


def _addr_rank(serial):
    """Lower is better. A plain hardware serial beats host:port beats the mDNS name."""
    s = serial or ""
    if "._tcp" in s:
        return 2                          # adb-<SERIAL>-<tag>._adb-tls-connect._tcp
    if ":" in s:
        return 1                          # 192.168.1.50:38533
    return 0                              # a cable


def _dedupe_phones(rows):
    """One physical handset, one row.

    ⚠️ ONE PHONE CAN APPEAR TWICE IN `adb devices`, which is not obvious until it does.
    Once a phone is paired for wireless debugging, adb's own mDNS auto-connect attaches it
    under its service name AT THE SAME TIME as an explicit `adb connect host:port` holds
    it - two transport ids, two entries, same handset. The Phones card then listed the
    Pixel 6 twice with identical battery readings, which reads as a second phone rather
    than a second route to the first one.

    Keyed on ro.serialno, which is the handset itself and is the same down every route.
    Falls back to the adb address when the prop could not be read, so a phone that fails
    the getprop still gets its row rather than being silently merged into another.
    """
    best = {}
    order = []
    for r in rows:
        key = r.get("hwserial") or ("addr:" + str(r.get("serial")))
        if key not in best:
            best[key] = r
            order.append(key)
        elif _addr_rank(r.get("serial")) < _addr_rank(best[key].get("serial")):
            best[key] = r
    return [best[k] for k in order]


def devices_snapshot():
    now = time.time()
    if now - _dev_cache["at"] < _DEV_TTL and _dev_cache["data"]:
        return _dev_cache["data"]
    data = _dedupe_phones([device_info(x) for x in adb_devices()])
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


_fps_prev = {}                 # label -> (nalsOut, monotonic) from the previous poll


def rigcams_fps(states):
    """Measured frames per second per phone, from the encoder's own output counter.

    ⚠️ THE `fps` FIELD IN /api/state IS THE TARGET, NOT THE RATE. It reads 30 whether the
    camera is delivering 30 or 13, because it is the number handed to the H264 encoder at
    bind time - so a panel showing it would have read a confident "30 fps" through the
    entire week both cameras were running at half that. `nalsOut` is a monotonic count of
    emitted slices, so the delta between two polls over the elapsed time is the real rate.

    Needs two polls to say anything, and says nothing rather than guessing from one. Uses
    a monotonic clock because this subtracts two timestamps, and the wall clock can step.
    """
    out = {}
    now = time.monotonic()
    for label, st in (states or {}).items():
        enc = (st or {}).get("encoder") or {}
        n = enc.get("nalsOut")
        if not isinstance(n, int) or (st or {}).get("offline"):
            _fps_prev.pop(label, None)
            out[label] = None
            continue
        prev = _fps_prev.get(label)
        _fps_prev[label] = (n, now)
        # A restarted RigCam resets the counter, so a negative delta means "new process",
        # not "negative frame rate".
        if not prev or now - prev[1] < 2.0 or n < prev[0]:
            out[label] = None
            continue
        out[label] = round((n - prev[0]) / (now - prev[1]), 1)
    return out


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
    # "source" is the OBS source this bridge ends up in; the reset needs it by name - see
    # the note in bridge_reset about who is holding the sink. It lives on the bridge rather
    # than in a parallel list so that resetting ONE bridge cannot release the other one's
    # sink by picking the wrong index.
    # ⚠️ THE LABEL NAMES THE TRANSPORT, SO IT HAS TO BE TRUE. Both phones now come in over
    # WiFi - the Pixel 8 moved off its USB adb forward on 2026-09-21 and its task points at
    # 192.168.1.166:8090 directly. A label that still said USB would send you to replug a
    # cable that is not part of the path any more, which is the exact wrong-tool failure
    # the device rows were fixed for.
    {"task": "Riomhdhos vcam bridge", "label": "Pixel 6 (WiFi)",
     "log": BRIDGE_LOGS / "vcam-p6.log", "source": "Pixel 6 (vcam)"},
    {"task": "Riomhdhos vcam bridge P8", "label": "Pixel 8 (WiFi)",
     "log": BRIDGE_LOGS / "vcam-p8.log", "source": "Pixel 8"},
]
BRIDGE_FRESH_S = 90        # they report frames every 30 s, so this is three missed reports
BRIDGE_SOURCES = [b["source"] for b in BRIDGES]


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


def _bridge_health(last, age, state):
    """What the last log line SAYS decides health - and it has to say something POSITIVE.

    ⚠️ A FRESH LOG MINUS TWO KNOWN ERROR STRINGS IS NOT HEALTH, and reading it that way is
    what made this panel claim a phone that was switched off was feeding at 25 fps. When
    the handset is unreachable the bridge writes

        no picture yet (phone unreachable, or camera asleep); retrying in 5s

    every five seconds. That is the FRESHEST log on the machine, the scheduled task is
    still 'running', and the line contains neither 'session failed' nor 'STALLED' - so the
    old denylist fell straight through to 'feeding'. The retry spam was itself the thing
    being mistaken for health.

    Only a frame report proves frames. Everything else is named for what it actually is,
    because 'feeding' on a dead camera costs a take and 'no signal' does not.
    """
    # ⚠️ "I COULD NOT ASK" IS NOT "IT IS NOT RUNNING". _task_state returns "unknown" when
    # the schtasks query itself fails or times out, and that was being reported as
    # "stopped" - so when this box was saturated (a closed OBS making every poll queue
    # behind a 4 s connect attempt), both bridges read Stopped on screen while they were
    # feeding at 25 fps. The panel turned the absence of an answer into a confident wrong
    # one, which is the same failure as a dead camera reading "feeding", inverted.
    if state == "unknown":
        return "unknown"
    if state != "running":
        return "stopped"
    if age is None or age >= BRIDGE_FRESH_S:
        # Nothing written in three reporting intervals: alive, blocked, counter frozen.
        return "stalled"
    low = last.lower()
    if "session failed" in low or "stalled" in low:
        return "failing"
    if "no picture yet" in low or "phone unreachable" in low or "camera asleep" in low:
        return "no signal"
    if "stream ended" in low or "error during demuxing" in low or "i/o error" in low:
        return "dropped"
    if " frames," in low:
        return "feeding"
    # Opened the stream, or re-opened the OBS source, but no frame report yet. Honest for
    # the ~30 s between connecting and the first count, and it must not read as feeding.
    if "from http" in low or "feeding '" in low or "re-opened" in low:
        return "starting"
    return "starting"


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
            "source": b["source"],
            "ageS": None if age is None else round(age, 1),
            "health": _bridge_health(last, age, state),
            "last": last[-140:],
        })
    return out


def bridge_find(label):
    """Resolve a bridge by label or task name. Returns None when nothing matches, so a
    stale button in an old tab cannot silently reset the wrong camera."""
    for b in BRIDGES:
        if label in (b["label"], b["task"]):
            return b
    return None


def bridge_reset(only=None):
    """Reset every bridge, or just one.

    ⚠️ ONE CAMERA AT A TIME IS THE POINT. The pair reset drops both pictures for about ten
    seconds, which mid-set costs the shot that is currently live as well as the one that
    was broken. When only the WiFi phone has gone, only the WiFi phone should go dark.
    """
    steps = []
    targets = BRIDGES if only is None else [only]
    if only is None:
        # The sledgehammer, kept deliberately broad: it is also the only path that catches
        # an ffmpeg whose parent bridge already died and left it holding the stream.
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | "
              "Where-Object { $_.CommandLine -like '*vcambridge*' } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; "
              "Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe'\" | "
              "Where-Object { $_.CommandLine -like '*stream.h264*' } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
        killed = "stopped the bridges and their ffmpeg"
    else:
        # Both bridges run the same script with the same '*vcambridge*' command line, so the
        # only thing that tells them apart is the --log path they were started with. Match
        # on that, then take the ffmpeg that bridge actually spawned - by parent pid, not by
        # a URL restated here, which would drift the moment a task is re-pointed.
        tag = only["log"].name
        ps = ("$ps = Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | "
              "Where-Object { $_.CommandLine -like '*" + tag + "*' }; "
              "foreach ($p in $ps) { "
              "Get-CimInstance Win32_Process -Filter \"ParentProcessId=$($p.ProcessId)\" | "
              "Where-Object { $_.Name -eq 'ffmpeg.exe' } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; "
              "Stop-Process -Id $p.ProcessId -Force }")
        killed = "stopped " + only["label"] + " and its ffmpeg"
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
        steps.append(killed)
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
        for src in [t["source"] for t in targets]:
            try:
                obsctl._cycle(cl, src, settle=0.5)
                steps.append("released " + src)
            except Exception:
                steps.append("could not cycle " + src)
    except Exception:
        # OBS closed is a perfectly normal state here - it is often WHY the reset is needed
        steps.append("OBS not reachable - skipped releasing the sources")

    for b in targets:
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


# ---------------------------------------------------------------- the stream relay


def _progress(path):
    """ffmpeg -progress writes repeating key=value blocks; the last values are current."""
    out = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except Exception:
        return {}
    return out


def _egress_health(name, prog_path):
    """feeding / stalled / stopped for one destination, from evidence only."""
    try:
        age = time.time() - prog_path.stat().st_mtime
    except Exception:
        return "stopped", None, 0.0, None
    prog = _progress(prog_path)
    try:
        total = int(prog.get("total_size") or 0)
    except ValueError:
        total = 0
    kbps = None
    m = re.match(r"([\d.]+)", (prog.get("bitrate") or "").strip())
    if m:
        try:
            kbps = round(float(m.group(1)))
        except ValueError:
            pass
    secs = None
    try:
        secs = int(int(prog.get("out_time_us") or 0) / 1_000_000)
    except ValueError:
        pass

    if prog.get("progress") == "end":
        return "stopped", total, age, secs
    if age >= EGRESS_FRESH_S:
        # Written recently enough to be a live process, but not recently enough to be
        # sending anything. Alive and not feeding is exactly the case worth naming.
        return "stalled", total, age, secs

    # Fresh file. Still not enough on its own: ffmpeg rewrites the block on a timer even
    # when the far end has stopped accepting, so require the counter to have MOVED.
    prev = _egress_seen.get(name)
    if prev is None or prev[0] != total:
        _egress_seen[name] = (total, time.time())
        return "feeding", total, age, secs
    held = time.time() - prev[1]
    return ("feeding" if held < EGRESS_FRESH_S else "stalled"), total, age, secs


def platform_status():
    """Every configured platform and how far through setup it is. Never returns a key.

    ⚠️ THE PLATFORM LIST IS THE SPINE OF THIS PANEL, not the pushers. Listing only what is
    currently streaming meant a rig with nothing set up showed an empty box, which answers
    "is anything going out" with a technically-true "no" and tells you nothing about WHY
    or what to do next. A channel you have not registered an app for and a channel that is
    live are both facts about the same channel, and both belong on its row.
    """
    now = time.time()
    if now - _plat_cache["at"] < _PLAT_TTL and _plat_cache["data"]:
        return _plat_cache["data"]
    r = connect_run(["status", "--json"], timeout=15)
    data = []
    if r["ok"] and r["lines"]:
        try:
            data = json.loads("".join(r["lines"])).get("platforms", [])
        except Exception:
            data = []
    _plat_cache.update(at=now, data=data)
    return data


def relay_status():
    """Ingest from MediaMTX, egress from each pusher's own progress file."""
    out = {"ingest": {"ready": False, "readers": 0, "bytes": 0, "error": None},
           "destinations": [], "platforms": platform_status(),
           "task": _task_state(RELAY_TASK)}
    try:
        with urllib.request.urlopen(MEDIAMTX_API + "/v3/paths/get/live", timeout=2) as r:
            d = json.loads(r.read().decode("utf-8"))
        out["ingest"] = {"ready": bool(d.get("ready")),
                         "readers": len(d.get("readers") or []),
                         "bytes": d.get("bytesReceived") or 0, "error": None}
    except Exception as e:
        out["ingest"]["error"] = type(e).__name__

    try:
        progs = sorted(RELAY_STATUS.glob("*.progress"))
    except Exception:
        progs = []
    for p in progs:
        name = p.stem
        health, total, age, secs = _egress_health(name, p)
        # RED HAS TO BE EARNED, THE SAME WAY GREEN DOES. A cold progress file means
        # "stalled" only while something is actually being sent. Between shows nothing
        # publishes, every leftover file is cold, and this panel sat on three red rows
        # permanently - which trains you to ignore red, so a real mid-show stall then
        # reads exactly like the resting state. Not knowing is the one exception: if
        # MediaMTX could not be reached we cannot claim the rig is idle, so the raw
        # verdict stands and the ingest row carries the error.
        if (health == "stalled" and not out["ingest"]["ready"]
                and out["ingest"]["error"] is None):
            health = "idle"
        state = err = ""
        try:
            state = (RELAY_STATUS / f"{name}.state").read_text(encoding="utf-8",
                                                               errors="replace").strip()
        except Exception:
            pass
        try:
            err = (RELAY_STATUS / f"{name}.err").read_text(
                encoding="utf-8", errors="replace").strip()[-160:]
        except Exception:
            pass
        restarts = 0
        m = re.search(r"restarts=(\d+)", state)
        if m:
            restarts = int(m.group(1))
        prog = _progress(p)
        out["destinations"].append({
            "name": name, "health": health, "bytes": total,
            "ageS": None if age is None else round(age, 1),
            "uptimeS": secs, "kbps": None,
            "speed": prog.get("speed"), "restarts": restarts,
            "retrying": state.startswith("retrying"), "last": err,
        })
        try:
            mm = re.match(r"([\d.]+)", (prog.get("bitrate") or "").strip())
            out["destinations"][-1]["kbps"] = round(float(mm.group(1))) if mm else None
        except Exception:
            pass
    return out


PREFS_FILE = Path.home() / ".riastrad-prefs.json"


def prefs():
    """Small persisted UI preferences owned by this dashboard. Unreadable means default,
    never an error - losing a preference must not take the panel down."""
    try:
        if PREFS_FILE.exists():
            d = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def set_pref(key, value):
    """Write one preference, atomically. dash.py restarts constantly during a show and a
    half-written file read on the way back up would look like a corrupt config."""
    d = prefs()
    d[key] = value
    tmp = PREFS_FILE.with_name(PREFS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    os.replace(tmp, PREFS_FILE)
    return d


DEFAULT_THUMB = Path.home() / "Pictures" / "stream-assets" / "cullah-live.png"


def _mtime(path):
    """File mtime as an int, or 0. Used to bust the browser's cache on the preview:
    the thumbnail is regenerated under the SAME filename, so without this the page
    keeps showing the previous image and you approve artwork you are not sending."""
    try:
        return int(os.path.getmtime(path))
    except Exception:
        return 0


def stream_meta():
    """Title, description and thumbnail path, held server-side.

    ⚠️ THESE USED TO LIVE IN localStorage, WHICH IS PER-BROWSER. The panel is driven from
    a phone as often as from this desk, and a title typed at the desk simply was not there
    on the phone - so the fields showed stale test text on one device and nothing on the
    other, and the only way to know which you were looking at was to remember. A default
    that differs by device is worse than no default, because it looks authoritative.

    The thumbnail is YouTube-only and the UI has to say so: Facebook has no API for a live
    video's thumbnail at all, and Twitch does not use one.
    """
    pr = prefs()
    return {"title": pr.get("stream_title", "") or "",
            "desc": pr.get("stream_desc", "") or "",
            "thumb": pr.get("stream_thumb", "") or str(DEFAULT_THUMB),
            "thumb_name": os.path.basename(pr.get("stream_thumb") or str(DEFAULT_THUMB)),
            "thumb_at": _mtime(pr.get("stream_thumb") or str(DEFAULT_THUMB)),
            "thumb_exists": os.path.isfile(pr.get("stream_thumb") or str(DEFAULT_THUMB))}


def set_thumbnail_now():
    """Push the configured image to YouTube through connect.py.

    Riastrad never talks to a platform itself, so this shells out like everything else -
    which also means the OAuth token stays in connect.py's keeping and never comes near
    the dashboard.
    """
    meta = stream_meta()
    path = meta["thumb"]
    if not os.path.isfile(path):
        return {"ok": False, "lines": ["no image at " + path]}
    r = connect_run(["thumbnail", path], timeout=180)
    return r


def obs_record_state():
    """Is OBS recording to disk? Shaped like obs_stream_state - OBS closed is an answer,
    not a fault, and shares the same negative cache so a shut OBS stays instant.

    `armed` is a stored preference and needs no OBS at all, so it is still truthful when
    the rest of this is unknown: it says what WILL happen, not what is happening.
    """
    out = {"armed": bool(prefs().get("record_with_golive"))}
    try:
        st = client().get_record_status()
        out.update(connected=True,
                   active=bool(getattr(st, "output_active", False)),
                   paused=bool(getattr(st, "output_paused", False)),
                   seconds=int((getattr(st, "output_duration", 0) or 0) / 1000),
                   bytes=getattr(st, "output_bytes", None))
    except (Exception, SystemExit):
        out.update(connected=False, active=False)
    return out


def obs_stream_state():
    """Is OBS actually sending? OBS closed is a normal answer, not an error."""
    try:
        cl = client()                  # shared connection + the negative cache above
        st = cl.get_stream_status()
        return {"connected": True, "active": bool(getattr(st, "output_active", False)),
                "congestion": getattr(st, "output_congestion", None),
                "skipped": getattr(st, "output_skipped_frames", None),
                "total": getattr(st, "output_total_frames", None)}
    except (Exception, SystemExit):
        return {"connected": False, "active": False}


def connect_run(args, timeout=90):
    """Run the relay's own connect.py. Riastrad NEVER speaks to a platform itself.

    ⚠️ ALL OF THIS LIVES IN THE RELAY FOLDER ON PURPOSE. connect.py holds the OAuth
    tokens and writes keys.env; keeping it there rather than importing it means the same
    folder still works on a laptop with no dashboard, and Riastrad never handles a key or
    a refresh token - it shells out and reads back the report.

    ⚠️ AND IT IS HEADLESS ONLY. `golive` and `--dry-run` make no browser. The interactive
    `connect.py <platform>` flow is deliberately NOT reachable from here: it opens a
    consent screen on this machine, and the dashboard's whole point is being driven from
    the couch - tapping Connect on a phone and having a Google login appear on a desktop
    in another room is a failure, not a feature. First-time connection is a desk job.
    """
    if not RELAY_CONNECT.exists():
        return {"ok": False, "exit": None, "lines": [f"not found: {RELAY_CONNECT}"]}
    try:
        # ⚠️ DECODE AS UTF-8 EXPLICITLY. text=True uses the locale encoding, which is
        # cp1252 on this box, and connect.py echoes back the title and tags it was given -
        # so a set called "Café" would be reported as "CafÃ©" and read as though the wrong
        # thing had been sent to the platform. This is the one subprocess here that carries
        # text the user typed, so it is the one that has to get this right.
        p = subprocess.run([sys.executable, str(RELAY_CONNECT), *args],
                           cwd=str(RELAY_DIR), capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit": None, "lines": ["timed out talking to the platforms"]}
    except Exception as e:
        return {"ok": False, "exit": None, "lines": [f"{type(e).__name__}: {e}"]}
    lines = [l.strip() for l in ((p.stdout or "") + (p.stderr or "")).splitlines() if l.strip()]
    return {"ok": p.returncode == 0, "exit": p.returncode, "lines": lines[-12:]}


def twitch_categories():
    """Category names Twitch itself has already accepted from this channel.

    ⚠️ THIS IS A MEMORY, NOT A SEARCH, AND THE UI HAS TO SAY SO. Riastrad never speaks to
    a platform (see connect_run), so it cannot ask Twitch what categories exist right now.
    What it can do is read the cache connect.py builds in tokens.json under twitch.games,
    and every name in there is EARNED: twitch_game_id() writes an entry only after
    /helix/games returned an id for that exact string, so each key is a name Twitch
    confirmed at the moment it was used. That is positive evidence, which is the standard
    everything else here is held to, and it is the only reason these may be offered at all.

    ⚠️ AND A MEMORY CAN GO STALE, SO IT STAYS A SUGGESTION AND NEVER A CONSTRAINT. The
    field remains free text. Twitch can retire or rename a category, and a picker that
    quietly offered a name that no longer resolves would fail the whole PATCH and take the
    title down with it - the same split outcome the tag validator exists to prevent. An
    empty list is therefore a perfectly good answer and must read as "nothing remembered
    yet", not as "no such category exists".

    The keys are stored lower-cased by connect.py and are deliberately NOT re-capitalised
    here. Turning "music" into "Music" would be presenting a guess as a fact, and since
    the /helix/games lookup is case-insensitive the guess would buy nothing anyway.

    Reads one small JSON file and returns only the category names out of it. The tokens in
    that file are never read, never held and never returned.
    """
    try:
        raw = json.loads((RELAY_DIR / "tokens.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"names": [], "why": "the relay has no tokens.json yet - sign in at the desk first"}
    except Exception as e:
        # Same rule as chat_conf(): unreadable is not the same as empty, and saying which
        # is the difference between "you have not used one yet" and "this file is broken".
        return {"names": [], "why": "tokens.json unreadable: %s" % (type(e).__name__,)}
    tw = (raw or {}).get("twitch") or {}
    games = tw.get("games") or {}
    if not isinstance(games, dict):
        return {"names": [], "why": "twitch.games in tokens.json is not an object"}
    # Two sources, and the order is the point. What this channel has actually used comes
    # first, because after a few shows the right answer is almost always one of three or
    # four names and burying them under three hundred game titles helps nobody. Twitch's
    # own most-watched list follows, in Twitch's order, so the field can offer real
    # category names rather than only a memory.
    used = sorted(k for k in games if isinstance(k, str) and k.strip())
    top = [n for n in (tw.get("top_categories") or [])
           if isinstance(n, str) and n.strip()]
    seen, names = set(), []
    for n in used + top:
        k = n.strip().lower()
        if k not in seen:
            seen.add(k)
            names.append(n.strip())
    why = ""
    if not names:
        why = ("no categories cached yet - run: python connect.py categories")
    return {"names": names, "used": len(used), "top": len(top),
            # ⚠️ THE AGE TRAVELS WITH THE LIST. Categories get renamed and retired, and a
            # stale name does not fail quietly - it fails the whole PATCH and takes the
            # title down with it. A list offered without saying when it was taken is
            # claiming to be current, which is a claim this cannot make.
            "top_at": tw.get("top_categories_at") or "",
            "why": why}


def set_enabled(name, on):
    """Flip one <NAME>_ENABLED flag in the relay's keys.env. Never reads or writes a key.

    ⚠️ THIS FILE HOLDS THE STREAM KEYS, so it is edited line by line and written
    atomically. Rewriting it from parsed values would risk reformatting or dropping a key
    on a parse quirk, and a truncated write during a crash would lose all of them - to
    arm a channel, which is not a trade worth making. Only the one ENABLED line changes;
    every other byte is passed through untouched.
    """
    name = (name or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]+", name or ""):
        return {"error": "bad platform name"}, 400
    key = f"{name}_ENABLED"
    val = "1" if on else "0"
    path = RELAY_DIR / "keys.env"
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as e:
        return {"error": f"cannot read keys.env: {type(e).__name__}"}, 500

    newline = "\r\n" if "\r\n" in raw else "\n"
    lines, found = raw.splitlines(), False
    for i, l in enumerate(lines):
        if l.strip().startswith(key + "="):
            lines[i] = f"{key}={val}"
            found = True
            break
    if not found:
        # A platform with a key but no ENABLED line is a real state; adding the line is
        # the correct repair rather than an error.
        lines.append(f"{key}={val}")

    tmp = path.with_suffix(".env.tmp")
    try:
        tmp.write_text(newline.join(lines) + newline, encoding="utf-8")
        os.replace(tmp, path)          # atomic on Windows for same-volume replace
    except Exception as e:
        try:
            tmp.unlink()
        except Exception:
            pass
        return {"error": f"cannot write keys.env: {type(e).__name__}"}, 500

    _plat_cache["at"] = 0.0            # the panel must reflect this on the very next poll
    return {"ok": True, "platform": name.lower(), "enabled": bool(on)}, 200


def _mediamtx_pid():
    """PID of the relay process; None if it is not running; False if the question failed.

    Three-valued on purpose. "Could not ask" is not "not running", and the restart button
    must not report success on the strength of a lookup that never answered.
    """
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq mediamtx.exe",
                            "/NH", "/FO", "CSV"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=10, creationflags=NO_WINDOW)
        if r.returncode != 0:
            return False
        for line in r.stdout.splitlines():
            cells = [c.strip().strip('"') for c in line.split('","')]
            if len(cells) >= 2 and cells[0].lower() == "mediamtx.exe":
                try:
                    return int(cells[1])
                except ValueError:
                    return False
        return None
    except Exception:
        return False


def relay_restart():
    """Bounce the relay's task, then prove it actually came back.

    Two separate lies were possible here. schtasks does not raise on failure - it exits
    non-zero and prints to stderr - so the original try/except caught nothing and this
    returned ok:True unconditionally, reporting "restarted" even with no such task.

    Worse, and the reason the PID check exists: even when schtasks succeeds the relay may
    not restart at all. start-relay.bat launches MediaMTX through `start`, which detaches
    it from the task, so /End kills the task's tree and leaves MediaMTX running; the fresh
    instance then cannot bind 1935 and dies. Exit code 0 is absence of error, not evidence
    of a restart. Compare the PID and let only a genuinely new process count as success.
    The real fix is in start-relay.bat, which this session does not own.
    """
    before = _mediamtx_pid()
    steps, ok, missing = [], True, False
    for verb in ("/End", "/Run"):
        try:
            p = subprocess.run(["schtasks", verb, "/TN", RELAY_TASK], capture_output=True,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=30, creationflags=NO_WINDOW)
        except Exception as e:
            steps.append(f"{verb} failed: {type(e).__name__}")
            ok = False
            continue
        if p.returncode == 0:
            steps.append(f"{verb} ok")
            continue
        said = [l.strip() for l in
                ((p.stderr or "").splitlines() + (p.stdout or "").splitlines())
                if l.strip()]
        said = said[-1] if said else f"exit {p.returncode}"
        low = said.lower()
        # Ending a task that was not running is a no-op, not a failure.
        if verb == "/End" and ("is not running" in low or "not currently running" in low):
            steps.append("/End: was not running")
            continue
        if "cannot find the file" in low or "does not exist" in low:
            missing = True
        steps.append(f"{verb}: {said}")
        ok = False

    _egress_seen.clear()

    if not ok:
        err = (f'no scheduled task named "{RELAY_TASK}" is registered' if missing
               else f'could not bounce the "{RELAY_TASK}" task')
        return {"ok": False, "steps": steps, "error": err}

    # schtasks was happy. That is not the same as the relay having restarted.
    after = _mediamtx_pid()
    for _ in range(12):
        if isinstance(after, int) and after != before:
            break
        threading.Event().wait(0.5)
        after = _mediamtx_pid()

    if after is False:
        steps.append("⚠ could not check whether MediaMTX came back")
        return {"ok": False, "steps": steps,
                "error": "task bounced, but the relay could not be verified"}
    if after is None:
        steps.append("⚠ MediaMTX is not running after the restart")
        return {"ok": False, "steps": steps, "error": "the relay did not come back"}
    if after == before:
        steps.append(f"⚠ MediaMTX is still pid {before}, the same process as before - "
                     "start-relay.bat launches it through `start`, which detaches it from "
                     "the task, so /End cannot kill it")
        return {"ok": False, "steps": steps, "error": "the relay did not actually restart"}

    steps.append(f"MediaMTX restarted, pid {after}")
    return {"ok": True, "steps": steps}


# ---------------------------------------------------------------- chat (read-only)
# ⚠️ CHAT DOES NOT RIDE /feed, AND THAT IS DELIBERATE. That pipe is a 20 Hz broadcast of
# the whole STATE dict through a queue.Queue(maxsize=4) that drops rather than blocks,
# which is exactly right for animation frames and is silent data loss for chat. Chat is
# pulled from /api/chat with a monotonic cursor instead: nothing is dropped, and a phone
# that slept through a song catches up on wake instead of showing a hole it cannot see.
#
# ⚠️ READ-ONLY STRUCTURALLY, NOT BY POLICY. Nothing here can post, reply, moderate or
# authenticate. Twitch is read as justinfan<random>, the anonymous login the server hands
# out for free - no token, no account, no OAuth app - so there is no credential in this
# path to leak and nothing here that can act as the user. Sending would mean a Google
# OAuth app with a sensitive scope and a 200-replies-a-day ceiling; it was considered and
# deliberately left out.
#
# ⚠️ A QUIET CHAT AND A DEAD SOCKET LOOK IDENTICAL, so every source carries two clocks:
# when it last carried a message, and when it last PROVED it was alive. Only the second
# earns green. Twitch only PINGs every few minutes, far too slow to notice a hung socket,
# so this sends its own PING whenever the read goes quiet and counts the bytes coming back
# as the proof. An empty pane is a lie that looks like a quiet audience, so a source with
# nothing behind it says so in words.

CHAT_CONF = Path.home() / ".riastrad-chat.json"
CHAT_KEEP = 300            # messages held in memory; oldest fall off
CHAT_STALE_S = 150.0       # no proof of life for this long and the source is not green
CHAT_PING_S = 60.0         # how long a quiet read waits before we prod the server

# Every platform the rig can stream to gets a row, including the ones with nothing behind
# them. The reason is written out rather than implied by an empty list.
CHAT_SOURCES = [("twitch", "Twitch"), ("youtube", "YouTube"), ("facebook", "Facebook"),
                ("x", "X"), ("tiktok", "TikTok"), ("instagram", "Instagram")]
CHAT_WHYNOT = {
    "x":         "not wired yet - Livestream API access is form-gated",
    "tiktok":    "no live-chat API exists at all - nothing to connect to",
    "instagram": "needs a public webhook URL Meta can reach - awkward from this box",
}

_chat_lock = threading.Lock()
_chat_msgs = []            # newest last
_chat_seq = 0
_chat_src = {}             # name -> {state, detail, last_msg, last_alive}


_chat_conf_err = ""


def chat_conf():
    """Read the chat config.

    An absent file means nothing is configured, which is a legitimate resting state. A
    file that is present but unparseable is NOT the same thing and must not read as one:
    a human hand-edits this to paste keys in, and one trailing comma would otherwise
    present as "no channel configured" and send you looking at the wrong thing while the
    real fault is a character you can see.
    """
    global _chat_conf_err
    try:
        if not CHAT_CONF.exists():
            _chat_conf_err = ""
            return {}
        c = json.loads(CHAT_CONF.read_text(encoding="utf-8"))
        if not isinstance(c, dict):
            _chat_conf_err = CHAT_CONF.name + " is not a JSON object"
            return {}
        _chat_conf_err = ""
        return c
    except Exception as e:
        _chat_conf_err = "%s: %s" % (CHAT_CONF.name, e)
        return {}


def _chat_rec(name):
    return _chat_src.setdefault(name, {"state": "off", "detail": "",
                                       "last_msg": 0.0, "last_alive": 0.0})


def _chat_mark(name, state, detail="", alive=False):
    with _chat_lock:
        r = _chat_rec(name)
        r["state"], r["detail"] = state, detail
        if alive:
            r["last_alive"] = time.time()


def _chat_add(name, user, text, color=""):
    global _chat_seq
    if not text:
        return
    now = time.time()
    with _chat_lock:
        _chat_seq += 1
        _chat_msgs.append({"seq": _chat_seq, "src": name, "user": user[:40],
                           "text": text[:500], "color": color[:7], "t": now})
        if len(_chat_msgs) > CHAT_KEEP:
            del _chat_msgs[:len(_chat_msgs) - CHAT_KEEP]
        r = _chat_rec(name)
        r["last_msg"] = r["last_alive"] = now


def _irc_tags(chunk):
    """Parse an IRCv3 tag string. The escaping is a real spec, not ad hoc: a literal
    semicolon or space cannot appear raw inside a tag value."""
    out = {}
    for part in chunk.split(";"):
        k, _, v = part.partition("=")
        if k:
            out[k] = (v.replace(r"\s", " ").replace(r"\:", ";")
                       .replace(r"\r", "").replace(r"\n", "").replace("\\\\", "\\"))
    return out


def irc_privmsg(line):
    """Pull (display name, text, colour) out of one tagged IRC line, or None if it is not
    a chat message. Split out of the reader deliberately: inside the socket loop this was
    untestable without a live channel talking, which meant the parse would have shipped on
    inspection alone."""
    tags, rest = {}, line
    if line.startswith("@"):
        head, _, rest = line.partition(" ")
        tags = _irc_tags(head[1:])
    if " PRIVMSG #" not in rest:
        return None
    who = rest.split("!", 1)[0].lstrip(":")
    body = rest.split(" PRIVMSG #", 1)[1]
    body = body.split(" :", 1)[1] if " :" in body else ""
    return (tags.get("display-name") or who, body, tags.get("color") or "")


def twitch_chat_thread():
    """Read one Twitch channel's chat anonymously over TLS IRC.

    Verified against the live server before this shipped: the CAP REQ for tags and
    commands is ACKed, 001 arrives, JOIN and ROOMSTATE follow, all as justinfan<random>
    with no credential of any kind. Tags carry the display name and colour, so rendering
    needs no second lookup and no API app.

    Note the socket timeout is a heartbeat, not a failure. Twitch stays silent for minutes
    in a quiet channel, so a read timing out means "prod it"; only a failed write or a
    closed socket means "reconnect".
    """
    backoff = 2.0
    while True:
        chan = str(chat_conf().get("twitch_channel") or "").strip().lstrip("#").lower()
        if not chan:
            _chat_mark("twitch", "failed" if _chat_conf_err else "off",
                       _chat_conf_err or ("no twitch_channel in " + CHAT_CONF.name))
            threading.Event().wait(10)
            continue
        sock = None
        try:
            _chat_mark("twitch", "connecting", "#" + chan)
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(
                socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=15),
                server_hostname="irc.chat.twitch.tv")
            sock.settimeout(CHAT_PING_S)
            nick = "justinfan%d" % random.randint(10000, 99999)
            for line in ("CAP REQ :twitch.tv/tags twitch.tv/commands",
                         "NICK " + nick, "JOIN #" + chan):
                sock.sendall((line + "\r\n").encode("utf-8"))
            buf = ""
            backoff = 2.0
            while True:
                try:
                    data = sock.recv(8192)
                except socket.timeout:
                    # WATCH FOR A CHANGED CHANNEL. The config is only read where this
                    # loop reconnects, so editing twitch_channel did nothing at all
                    # until the socket happened to drop - which on a healthy link is
                    # never. That is the worst shape of wrong: the panel says reading,
                    # and truthfully, while the chat on screen belongs to someone else.
                    # I shipped exactly that bug by guessing the channel name.
                    want = str(chat_conf().get('twitch_channel') or '')
                    if want.strip().lstrip('#').lower() != chan:
                        raise OSError('channel changed in ' + CHAT_CONF.name)
                    sock.sendall(b"PING :riastrad\r\n")   # heartbeat; a PONG proves it
                    continue
                if not data:
                    raise OSError("closed by Twitch")
                _chat_mark("twitch", "live", "#" + chan, alive=True)
                buf += data.decode("utf-8", "replace")
                while "\r\n" in buf:
                    line, buf = buf.split("\r\n", 1)
                    if not line:
                        continue
                    if line.startswith("PING"):
                        sock.sendall(("PONG" + line[4:] + "\r\n").encode("utf-8"))
                        continue
                    got = irc_privmsg(line)
                    if got:
                        _chat_add("twitch", got[0], got[1], got[2])
        except Exception as e:
            _chat_mark("twitch", "failed", ("%s: %s" % (type(e).__name__, e))[:120])
        finally:
            try:
                if sock:
                    sock.close()
            except Exception:
                pass
        threading.Event().wait(backoff)
        backoff = min(backoff * 2, 60.0)


YT_API = "https://www.googleapis.com/youtube/v3/"
YT_MIN_POLL_S = 5.0        # floor under whatever interval the server asks for
YT_IDLE_S = 20.0           # how often to re-check for a broadcast while nothing is live


def _yt_get(key, path, **params):
    """One YouTube Data API call. Returns (data, None) or (None, (reason, message)).

    The reason is Google's own machine-readable code - quotaExceeded, forbidden,
    liveChatEnded - and is kept separate from the prose because the caller has to branch
    on it. Collapsing both into a string was tempting and would have made "out of quota"
    indistinguishable from "the broadcast ended", which are opposite situations: one is
    our fault and lasts until midnight Pacific, the other is normal and lasts seconds.
    """
    params["key"] = key
    url = YT_API + path + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(body)["error"]
            reason = ((err.get("errors") or [{}])[0].get("reason") or "").strip()
            msg = (err.get("message") or "")[:120]
        except Exception:
            reason, msg = "http%d" % e.code, body[:120]
        return None, (reason or ("http%d" % e.code), msg)
    except Exception as e:
        return None, ("network", "%s: %s" % (type(e).__name__, e))


def _yt_pushing():
    """Is the relay actually sending to YouTube right now?

    ⚠️ THIS GATES THE BROADCAST LOOKUP, AND IT HAS TO. search.list lives in its own
    hundred-calls-a-day bucket, so a timer that hunted for a broadcast around the clock
    would burn the whole allowance before breakfast and then fail for the rest of the day.
    Gating on the push costs one lookup per show - and it is also simply true, because
    there is no broadcast of ours to find while nothing is being sent to one.

    The cost is that a broadcast started some other way, not through this relay, will not
    be found. Set youtube_video_id in the config to read that one directly.
    """
    try:
        f = RELAY_STATUS / "youtube.progress"
        return f.exists() and (time.time() - f.stat().st_mtime) < EGRESS_FRESH_S * 3
    except Exception:
        return False


YT_VERIFY_S = 600.0
_yt_checked = {"at": 0.0, "ok": None, "why": ""}


def _yt_verify(key, cid):
    """Prove the key still works during the long stretches when nothing is live.

    ⚠️ WITHOUT THIS, A DEAD KEY AND A QUIET EVENING ARE THE SAME PICTURE. The broadcast
    lookup is gated on the relay pushing, so between shows nothing calls the API at all
    and the row sits on "waiting for a broadcast" - truthfully, and identically, whether
    the key is fine, revoked, or attached to a project whose quota is zero. This rig has
    now been bitten by exactly that: the quota on the old project was not exhausted, it
    was zero, and nothing said so until someone went looking.

    One unit every ten minutes, against ten thousand a day, buys the difference.
    """
    now = time.time()
    if now - _yt_checked["at"] < YT_VERIFY_S and _yt_checked["ok"] is not None:
        return _yt_checked["ok"], _yt_checked["why"]
    d, err = _yt_get(key, "channels", part="id", id=cid)
    _yt_checked["at"] = now
    if err:
        _yt_checked["ok"], _yt_checked["why"] = False, _yt_state(err)[1]
    elif not (d.get("items") or []):
        _yt_checked["ok"] = False
        _yt_checked["why"] = "the key works but youtube_channel_id matches no channel"
    else:
        _yt_checked["ok"], _yt_checked["why"] = True, ""
    return _yt_checked["ok"], _yt_checked["why"]


def _yt_find_chat(key, conf):
    """Resolve a live chat id. Returns (chat_id, title, None) or (None, None, (state, detail))."""
    vid = str(conf.get("youtube_video_id") or "").strip()
    cid = str(conf.get("youtube_channel_id") or "").strip()
    if not vid:
        if not _yt_pushing():
            # Idle is the normal resting state, but only say so once the key has been
            # shown to work - otherwise this is the friendliest possible way to hide a
            # credential that stopped working weeks ago.
            ok, why = _yt_verify(key, cid)
            if ok is False:
                return None, None, ("failed", why)
            return None, None, ("idle", "key checks out - waiting for a broadcast")
        d, err = _yt_get(key, "search", part="id", channelId=cid,
                         eventType="live", type="video", maxResults=1)
        if err:
            return None, None, _yt_state(err)
        items = d.get("items") or []
        if not items:
            return None, None, ("idle", "pushing to YouTube, but no live broadcast found yet")
        vid = items[0]["id"]["videoId"]

    d, err = _yt_get(key, "videos", part="liveStreamingDetails,snippet", id=vid)
    if err:
        return None, None, _yt_state(err)
    items = d.get("items") or []
    if not items:
        return None, None, ("failed", "video %s not found or not public" % vid)
    det = items[0].get("liveStreamingDetails") or {}
    chat = det.get("activeLiveChatId")
    title = (items[0].get("snippet") or {}).get("title", "")
    if not chat:
        # A real and common state: the broadcast exists but chat is off, members-only, or
        # it is not actually live. Say which rather than looking broken.
        return None, None, ("idle", "broadcast found but it has no active chat")
    return chat, title, None


def _yt_state(err):
    """Turn an API error into a state the panel can show honestly."""
    reason, msg = err
    if reason == "quotaExceeded":
        return ("quota", "daily quota spent - refills at midnight Pacific")
    if reason in ("liveChatEnded", "liveChatNotFound"):
        return ("idle", "the broadcast chat has ended")
    if reason in ("keyInvalid", "badRequest"):
        return ("failed", "the API key was rejected: " + msg)
    if reason == "forbidden":
        return ("failed", "forbidden - chat may be disabled or members-only")
    if reason == "network":
        return ("failed", msg)
    return ("failed", "%s: %s" % (reason, msg))


YT_SEEN_KEEP = 1000        # message ids remembered, so a replayed page cannot duplicate


def _yt_display(it):
    """Turn one liveChat item into (author, text), or (author, "") if it is silent.

    ⚠️ A PAID MESSAGE MUST NOT VANISH, AND READING displayMessage ALONE NEARLY LOSES ONE.
    Google's own stream_list.proto says "at the moment only messages of type TOMBSTONE and
    CHAT_ENDED_EVENT are silent", so Super Chats, Super Stickers and every membership event
    do carry displayMessage and none of them were being dropped outright. The real fault is
    subtler: displayMessage for a Super Chat is the comment, with nothing about the money,
    so a fiver rendered as an ordinary line of chat. Someone paid to be seen. The amount
    goes in front, built from superChatDetails rather than trusted to the display string.

    ⚠️ AND AN UNKNOWN TYPE GETS NAMED, NOT DROPPED. The type enum grows - giftEvent and
    pollEvent both postdate the first version of this reader - and the old code discarded
    anything without displayMessage without trace. A line reading "[giftEvent]" is ugly;
    a silent hole in the feed during a show is worse, because nothing on screen says the
    reader met something it did not understand.
    """
    sn = it.get("snippet") or {}
    au = it.get("authorDetails") or {}
    who = au.get("displayName") or "someone"
    typ = str(sn.get("type") or "")
    text = str(sn.get("displayMessage") or "")

    if typ == "superChatEvent":
        d = sn.get("superChatDetails") or {}
        # amountDisplayString is already localised by the server ("$1.00", "1,50$").
        amt = str(d.get("amountDisplayString") or "")
        note = str(d.get("userComment") or "")
        text = (amt + " " + note).strip() or amt or text
    elif typ == "superStickerEvent":
        d = sn.get("superStickerDetails") or {}
        amt = str(d.get("amountDisplayString") or "")
        # Super Stickers carry no user comment at all - altText is the only text there is,
        # and the sticker image is deliberately not available through the API.
        alt = str((d.get("superStickerMetadata") or {}).get("altText") or "sticker")
        text = (amt + " [" + alt + "]").strip()
    elif not text and typ not in ("tombstone", "chatEndedEvent"):
        text = "[" + typ + "]" if typ else ""
    return who, text


def youtube_chat_thread():
    """Read YouTube live chat with nothing but an API key.

    ⚠️ AN API KEY IS ENOUGH HERE, AND ONLY BECAUSE THIS IS READ-ONLY ON A PUBLIC STREAM.
    No OAuth app, no consent screen, no verification, no token to refresh. Posting a single
    reply would cost all of that plus a sensitive scope, which is the whole reason sending
    was left out. An unlisted or private broadcast is invisible to a key - it would need
    the OAuth path - so the panel says "no broadcast found" rather than pretending.

    Quota, checked against Google's quota calculator on 2026-09-27: liveChatMessages.list
    costs ONE unit against a pool of 10,000 a day. The widely repeated figure of five is a
    fossil - it was correct under the per-part pricing Google retired in April 2019, where
    a read cost 1 and each part added 2, making part=snippet,authorDetails exactly 5. At
    one unit, 10,000 polls a day is a poll every 8.6 s sustained around the clock, and a
    four-hour show could poll every 1.4 s and still fit. The current interval is nowhere
    near the wall.

    ⚠️ pollingIntervalMillis IS A FLOOR, BUT "SLOWER IS SAFE" IS AN ASSUMPTION, NOT A
    PROMISE. Google says only that it is how long the client "should wait before polling
    again", and documents rateLimitExceeded for requests sent "more frequently than
    YouTube's refresh rates" - so polling faster is a documented error and clamping upward
    cannot earn one. What Google does NOT document, in either direction, is whether a
    client that polls far slower than the chat's message rate can lose messages off the
    back of a server-side buffer. Nothing here should come to depend on slow polling being
    lossless. YT_MIN_POLL_S is a floor under the server's number, not a target.
    """
    chat_id = title = page = None
    quiet_until = 0.0
    yt_seen, yt_order = set(), []
    while True:
        now = time.time()
        if now < quiet_until:
            threading.Event().wait(min(2.0, quiet_until - now))
            continue

        conf = chat_conf()
        key = str(conf.get("youtube_api_key") or "").strip()
        if not key:
            _chat_mark("youtube", "off",
                       _chat_conf_err or ("no youtube_api_key in " + CHAT_CONF.name))
            quiet_until = time.time() + 15
            continue

        if not chat_id:
            chat_id, title, problem = _yt_find_chat(key, conf)
            if problem:
                _chat_mark("youtube", problem[0], problem[1])
                # Out of quota is a wall, not a hiccup: retrying every few seconds for
                # hours would spend the moment it refills and log nothing useful.
                quiet_until = time.time() + (300.0 if problem[0] == "quota" else YT_IDLE_S)
                continue
            page = None
            _chat_mark("youtube", "live", title or "live", alive=True)

        d, err = _yt_get(key, "liveChat/messages", liveChatId=chat_id,
                         part="snippet,authorDetails", maxResults=200,
                         **({"pageToken": page} if page else {}))
        if err:
            state, detail = _yt_state(err)
            _chat_mark("youtube", state, detail)
            if state in ("idle", "failed"):
                chat_id = None          # make the next pass rediscover
            quiet_until = time.time() + (300.0 if state == "quota" else YT_IDLE_S)
            continue

        # Bytes came back, so the link is proven alive even when nobody has spoken.
        _chat_mark("youtube", "live", title or "live", alive=True)

        # ⚠️ ADVANCE THE CURSOR ONLY ON A REAL TOKEN. pageToken is a resume point, not an
        # index into a fixed collection - Google's wording is that the API "will resume
        # sending messages from the point where you left off". So re-sending the previous
        # token asks the server to resume from BEFORE the messages just consumed, and the
        # next response redelivers them; a client doing that on every pass can wedge itself
        # re-reading one window for the length of a show. `page = d.get("nextPageToken")
        # or page` was precisely that. Google documents no fallback and does not promise
        # the token is always present, so when it is missing the only safe move is to hold
        # the cursor still and let de-duplication absorb the overlap.
        tok_next = d.get("nextPageToken")
        if tok_next:
            page = tok_next

        for it in d.get("items") or []:
            mid = str(it.get("id") or "")
            typ = str((it.get("snippet") or {}).get("type") or "")
            # ⚠️ giftEvent IS THE ONE TYPE THAT MAY ARRIVE TWICE UNDER ONE ID. Google warns
            # that for gift events "the same ID may be reused to update the combo count",
            # so de-duplicating it by id would show the first gift of a combo and silently
            # swallow every update after it.
            if mid and typ != "giftEvent":
                if mid in yt_seen:
                    continue
                yt_seen.add(mid)
                yt_order.append(mid)
                while len(yt_order) > YT_SEEN_KEEP:
                    yt_seen.discard(yt_order.pop(0))
            who, txt = _yt_display(it)
            if txt:
                _chat_add("youtube", who, txt)

        # ⚠️ offlineAt MEANS IT HAS ALREADY HAPPENED, not that it is scheduled to. Google:
        # "The date and time when the underlying livestream went offline. This property is
        # only present if the stream is already offline." It is also the RELIABLE end-of-
        # show signal, where chatEndedEvent is not - that one "is not sent for live chats
        # on a channel's default broadcast". Note this check deliberately runs AFTER the
        # items above are drained: the final payload carries both the last words spoken
        # and the notice that it is over, and bailing out first would drop them.
        if d.get("offlineAt"):
            chat_id = None
            _chat_mark("youtube", "idle", "the broadcast has ended")
            quiet_until = time.time() + YT_IDLE_S
            continue

        wait = (d.get("pollingIntervalMillis") or 0) / 1000.0
        quiet_until = time.time() + max(YT_MIN_POLL_S, wait)


# ⚠️ THE TOKEN LIVES IN SOMEONE ELSE'S FILE, AND IT IS RE-READ EVERY PASS. connect.py owns
# tokens.json and rewrites facebook.live_video_id every time a broadcast is created. That
# file was rewritten underneath this reader while this reader was being written, which is
# not a curiosity - it is the normal case, because going live is precisely what changes
# that id. Reading either value once at thread start would leave the panel happily green
# on a broadcast that ended hours ago.
#
# ⚠️ THE TOKEN MUST NEVER REACH THE PANEL, A LOG LINE OR AN EXCEPTION. Every string that
# can escape this section goes through _fb_scrub first. Graph does not normally echo the
# token back inside an error message, but "normally" is not a guarantee worth putting a
# Page credential behind, and these detail lines are rendered on a dashboard that gets
# screen-shared. The token is never formatted, never logged, never returned.

FB_TOKENS = RELAY_DIR / "tokens.json"      # connect.py's file; read here, never written
FB_GRAPH = "https://graph.facebook.com/v21.0"
FB_STREAM = "https://streaming-graph.facebook.com"
FB_POLL_S = 4.0            # comment poll interval while a broadcast is live
FB_IDLE_S = 20.0           # re-check interval while nothing is live
FB_SEEN_KEEP = 800         # comment ids remembered, so a re-poll cannot duplicate
FB_SSE_QUIET_S = 70.0      # a stream silent this long has stopped proving anything
FB_SSE_RETRY_S = 1800.0    # how long to stop attempting SSE after it refuses us

# Graph's own words for a live video's status field. Only LIVE means there is anything to
# read; the rest are ordinary resting states and must not wear red.
FB_STATUS_WORD = {
    "LIVE_STOPPED": "the broadcast has ended",
    "VOD": "the broadcast has ended and become a recording",
    "PROCESSING": "the broadcast is still processing",
    "UNPUBLISHED": "a live video is waiting, unpublished",
    "SCHEDULED_UNPUBLISHED": "a broadcast is scheduled but not started",
    "SCHEDULED_LIVE": "a broadcast is scheduled but not started",
    "SCHEDULED_EXPIRED": "the scheduled broadcast expired without starting",
    "PREVIEW": "the broadcast is in preview, not public yet",
}


def _fb_scrub(text, tok):
    """Remove a token from anything about to be displayed. Both raw and percent-encoded,
    because the value travels in a query string and could come back either way."""
    if not tok or not text:
        return text
    text = text.replace(tok, "<token>")
    quoted = urllib.parse.quote(tok, safe="")
    if quoted != tok:
        text = text.replace(quoted, "<token>")
    return text


def fb_conf():
    """Page token and live video id out of connect.py's tokens.json, read fresh.

    Returns (token, video_id, problem) where problem is (state, detail) or None.

    The three "nothing to do" cases are deliberately kept apart, because they send you to
    three different places: no file at all, a file with no token, and a token with no
    broadcast. Only the middle one is anyone's fault. A file that exists but will not
    parse is a fourth case and is NOT a resting state - same reasoning as chat_conf().
    """
    try:
        if not FB_TOKENS.exists():
            return "", "", ("off", "no tokens.json at " + str(FB_TOKENS))
        raw = json.loads(FB_TOKENS.read_text(encoding="utf-8"))
        fb = (raw or {}).get("facebook") or {}
    except Exception as e:
        return "", "", ("failed", "tokens.json unreadable: %s: %s" % (type(e).__name__, e))
    tok = str(fb.get("page_token") or "").strip()
    vid = str(fb.get("live_video_id") or "").strip()
    if not tok:
        return "", "", ("off", "no facebook.page_token - run: python connect.py facebook")
    if not vid:
        return tok, "", ("off", "no live video yet - nothing to read until you go live")
    return tok, vid, None


def _fb_get(tok, path, timeout=20, **params):
    """One Graph API GET. Returns (data, None) or (None, (code, subcode, message)).

    ⚠️ GET-ONLY IS THE READ-ONLY GUARANTEE, AND IT IS STRUCTURAL. urlopen() called with no
    data argument cannot issue a POST or a DELETE, and this module contains no other HTTP
    call. The Page token in hand is perfectly capable of commenting, replying, hiding and
    banning - the protection is not that this code chooses not to, it is that this code
    contains no way to. Adding a `data=` argument here is the single edit that would break
    that, which is why the guarantee is written down next to the function rather than in a
    design document nobody opens.

    The code and subcode are kept separate from the prose because the caller branches on
    them: 190 (token rejected) and 100/33 (the object is gone) are opposite situations and
    collapsing them into one string was exactly the mistake worth avoiding.
    """
    params["access_token"] = tok
    url = FB_GRAPH + path + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace")), None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(body)["error"]
            return None, (err.get("code"), err.get("error_subcode"),
                          _fb_scrub(str(err.get("message") or "")[:160], tok))
        except Exception:
            # Not a Graph JSON error. The streaming host answers with an HTML page, and
            # so does an edge behind a load balancer having a bad day.
            return None, ("http%d" % e.code, None, _fb_scrub(body[:160], tok))
    except Exception as e:
        return None, ("network", None, _fb_scrub("%s: %s" % (type(e).__name__, e), tok))


def _fb_state(err):
    """Turn a Graph error into a state the panel can show honestly.

    ⚠️ 190 IS THE ONE THAT MUST NEVER LOOK LIKE A QUIET CHAT. A Page token dies from an
    expiry, a password change, a permissions change or a straight revocation, and it is by
    some distance the most likely real-world failure of this reader. If it were allowed to
    fall through to a generic "failed" the panel would show red with no instruction, and
    the fix - one command - would not be on screen. Verified live on 2026-09-27: a
    deliberately malformed token returns HTTP 400, type OAuthException, code 190.
    """
    code, sub, msg = err
    if code == 190:
        # The subcode says WHY, and the why changes what he has to go and do: a token that
        # merely expired is a re-run, a Page role that was removed is a trip to Facebook.
        return ("failed", {
            463: "the Page token expired - re-run: python connect.py facebook",
            467: "the Page token was revoked - re-run: python connect.py facebook",
            460: "the password changed, so the token died - re-run: python connect.py facebook",
            492: "this token's user no longer has a role on the Page - fix that first",
        }.get(sub, "the Page token was rejected - re-run: python connect.py facebook"))
    if code == 102:
        return ("failed", "the Page session is invalid - re-run: python connect.py facebook")
    if code in (10, 200, 299):
        return ("failed", "permission denied - the token is missing pages_read_engagement")
    if code in (4, 17, 32, 613):
        # A wall we hit rather than broke, same as YouTube's quota: not red.
        return ("quota", "Graph rate limit reached - backing off")
    if code == 100 and sub == 33:
        # Verified live on 2026-09-27 against a nonexistent id: code 100, subcode 33.
        # ⚠️ NAME THE BUTTON, NOT THE COMMAND. This used to read "tokens.json points at a
        # live video that no longer exists" - exactly true, and useless at a glance mid-set,
        # because it describes an internal file and implies a fault where there is none.
        # A resting state should say what it is waiting for in terms of something the
        # person can actually press.
        return ("idle", "no broadcast yet - one is created when you go live")
    if code == 100 and "nonexisting field" in (msg or ""):
        # ⚠️ THE SAME DISAPPEARANCE WEARS TWO DIFFERENT ERRORS. Asking for a dead video's
        # fields gives 100/33, but asking for its /comments edge gives a bare 100 reading
        # "Tried accessing nonexisting field (comments)" with no subcode at all - observed
        # on 2026-09-27 when a broadcast ended between the status check and the next poll.
        # Both mean the broadcast is gone, which is the ordinary end of every show, so
        # neither may show up red.
        return ("idle", "the broadcast ended - its comments are no longer readable")
    if code == "network":
        return ("failed", msg)
    return ("failed", "graph %s: %s" % (code, msg))


# ⚠️ THE DOCUMENTED STREAM DOES NOT WORK ON THIS PAGE, SO THE POLL IS NOT A FALLBACK, IT
# IS THE PATH. Meta documents Server-Sent Events at streaming-graph.facebook.com/{id}/
# live_comments (/docs/live-video-api/interact-with-viewers). On 2026-09-27, against a
# video whose status field read LIVE, with the same Page token Graph had accepted for /me
# in the same second, that host returned HTTP 400 and a generic HTML error page - not a
# Graph JSON error, so there is no machine-readable reason to act on - for every
# documented shape tried: bare, with comment_rate=one_per_two_seconds, with
# comment_rate=ten_per_second, with flat fields, with from{} expansion, with an explicit
# Accept: text/event-stream, and against the permalink's video id instead of the live
# video id. The likeliest explanation is an access-level gate on the streaming host, since
# this app holds only Standard Access, but Meta does not say so and I will not claim it.
#
# ⚠️ AND THE SPEC FOR THIS ENDPOINT IS NO LONGER PUBLISHED, so do not go looking for it to
# check these parameters. Meta deleted the whole docs/graph-api/server-sent-events tree -
# including the live_comments page that carried the comment_rate and fields tables - and it
# now 301s to /docs/live-video-api, which describes the endpoint in prose and documents no
# parameters at all. The node reference for Live Video itself 404s too, having gone dark
# around the v21.0 boundary. The parameter details relied on here come from Meta's own
# archived pages (Wayback, captures of 2023-09-05 and 2024-05-21), cross-checked against
# the v21.0 through v24.0 changelogs, which record no change to any of it. That means the
# usual early warning - a doc diff - does not exist for this call, and the only signal of a
# break will be this reader failing.
#
# So: the stream is still attempted, because if it starts working it is strictly better
# than polling, and the panel NAMES the transport actually carrying the chat. Falling back
# silently would have meant "reading" on screen with no way to tell which of two very
# different mechanisms was behind it. After a refusal the attempt is parked for half an
# hour rather than retried every reconnect, because a request that has failed identically
# eight times in a row is not diagnosis, it is noise.
_fb_sse = {"off_until": 0.0, "why": ""}


def _fb_emit(c, seen, seen_q, quiet=False):
    """Record one comment and, unless priming, show it. True if it was new.

    ⚠️ THE FIRST POLL IS PRIMED SILENTLY. /comments hands back the most recent fifty
    comments whether or not this reader has seen them, and _chat_add stamps everything
    with the time it arrives here - so replaying that page on startup would drop an hour
    of old conversation into the bottom of a live feed wearing fresh timestamps. That is
    not a cosmetic problem: it is the panel stating, in the only way it states anything,
    that those words were just said.
    """
    cid = str(c.get("id") or "")
    if not cid or cid in seen:
        return False
    seen.add(cid)
    seen_q.append(cid)
    while len(seen_q) > FB_SEEN_KEEP:
        seen.discard(seen_q.pop(0))
    if quiet:
        return True
    # `from` is not guaranteed. Meta has progressively restricted commenter identity on
    # Page content for apps without Page Public Content Access, so an anonymous-looking
    # comment is an expected shape here, not a parse failure.
    who = ((c.get("from") or {}).get("name") or "").strip() or "someone on Facebook"
    _fb_text = str(c.get("message") or "")
    if _fb_text:
        _chat_add("facebook", who, _fb_text)
    return True


def _fb_sse_try(tok, vid, title, seen, seen_q):
    """Attempt the SSE comment stream and drain it. Returns True if it carried us.

    A read timeout is not a failure here, it is the absence of proof: the stream is
    supposed to keep talking, and if it has not produced a byte in FB_SSE_QUIET_S then
    whatever is on the other end has stopped demonstrating that it is there. Dropping back
    to the status check re-proves the link cheaply rather than sitting on a socket that
    may already be dead.
    """
    if time.time() < _fb_sse["off_until"]:
        return False
    # ⚠️ comment_rate IS A SAMPLING RATE, NOT A PACING HINT, AND THE OBVIOUS VALUE IS THE
    # WRONG ONE. Meta documents three: one_per_two_seconds, ten_per_second and
    # one_hundred_per_second, and of the first it says only "up to one comment will be
    # delivered per two seconds", with "comments prioritized based on quality so if the
    # comment rate exceeds the requested rate then you will receive the higher quality
    # comments". That is lossy sampling with no notification - a busy minute would silently
    # drop most of the chat and the panel would show a calm, plausible, incomplete feed,
    # which is the exact failure this whole section is built to prevent. Meta calls
    # one_hundred_per_second "sufficient to receive every comment even on extremely popular
    # broadcasts", so completeness is picked over tidiness.
    url = FB_STREAM + "/" + vid + "/live_comments?" + urllib.parse.urlencode({
        "access_token": tok, "comment_rate": "one_hundred_per_second",
        "fields": "from{name,id},message,created_time"})
    r = None
    try:
        r = urllib.request.urlopen(url, timeout=FB_SSE_QUIET_S)
        _chat_mark("facebook", "live", (title or "live") + " - streaming", alive=True)
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            # Any byte at all counts, a ":" keepalive comment included. Something on the
            # far end is still choosing to talk to us, and that is the whole definition of
            # proof of life - it is deliberately not the same test as "a comment arrived".
            _chat_mark("facebook", "live", (title or "live") + " - streaming", alive=True)
            if not line.startswith("data:"):
                continue
            try:
                _fb_emit(json.loads(line[5:].strip()), seen, seen_q)
            except Exception:
                continue
        return True
    except Exception as e:
        # ⚠️ THE STREAMING HOST DOES NOT SPEAK GRAPH. It answers with a bare HTTP status
        # and an HTML page, not the JSON envelope with code 190 that graph.facebook.com
        # returns - so a dead token shows up here as a plain 401 and _fb_state would never
        # see it. Branch on the status directly, and let a 401 say what it means; the
        # status check on the Graph host will confirm it a second later anyway.
        why = _fb_scrub("%s" % (type(e).__name__,), tok)
        if isinstance(e, urllib.error.HTTPError):
            why = {401: "HTTP 401, the token was refused",
                   400: "HTTP 400", 500: "HTTP 500"}.get(e.code, "HTTP %d" % e.code)
        _fb_sse["off_until"] = time.time() + FB_SSE_RETRY_S
        _fb_sse["why"] = why
        return False
    finally:
        try:
            if r:
                r.close()
        except Exception:
            pass


def facebook_chat_thread():
    """Read comments on the Page's current Facebook live video. Reads only.

    Nothing here authenticates a human, posts, replies, hides or bans; see _fb_get. The
    Page token is borrowed from connect.py's tokens.json, which is never written to and
    never echoed.
    """
    seen, seen_q = set(), []
    primed = ""            # the video id whose backlog has already been absorbed
    while True:
        tok, vid, problem = fb_conf()
        if problem:
            _chat_mark("facebook", problem[0], problem[1])
            threading.Event().wait(15)
            continue

        # Is there actually a broadcast to read? This call doubles as the liveness proof:
        # Graph answering about THIS video with THIS token, just now, is positive evidence
        # that every part of the path works, and it is the only evidence a silent audience
        # will ever generate.
        d, err = _fb_get(tok, "/" + vid, fields="status,title")
        if err:
            state, detail = _fb_state(err)
            _chat_mark("facebook", state, detail)
            threading.Event().wait(300.0 if state == "quota" else FB_IDLE_S)
            continue

        status = str(d.get("status") or "").upper()
        title = str(d.get("title") or "")
        if status != "LIVE":
            _chat_mark("facebook", "idle",
                       FB_STATUS_WORD.get(status, "broadcast status " + (status or "unknown")),
                       alive=True)
            primed = ""          # a new broadcast gets its backlog absorbed again
            threading.Event().wait(FB_IDLE_S)
            continue

        if _fb_sse_try(tok, vid, title, seen, seen_q):
            continue             # the stream ended; re-check status and reconnect

        note = "polling"
        if _fb_sse["why"]:
            note += " - the SSE stream refused us (%s)" % _fb_sse["why"]
        # ⚠️ live_filter=no_filter IS NOT OPTIONAL. This edge defaults to
        # filter_low_quality, which drops comments Meta judges low quality without saying
        # it did - so the default setting quietly hides part of the audience and the panel
        # would have no way to know. reverse_chronological is Meta's own documented best
        # practice for polling a live video.
        d, err = _fb_get(tok, "/" + vid + "/comments", order="reverse_chronological",
                         live_filter="no_filter", limit=50,
                         fields="id,message,created_time,from{name,id}")
        if err:
            state, detail = _fb_state(err)
            _chat_mark("facebook", state, detail)
            threading.Event().wait(300.0 if state == "quota" else FB_IDLE_S)
            continue

        _chat_mark("facebook", "live", (title or "live") + " - " + note, alive=True)
        quiet = (primed != vid)
        # reverse_chronological is newest-first, so walk it backwards to add in the order
        # the words were actually said.
        for c in reversed(d.get("data") or []):
            _fb_emit(c, seen, seen_q, quiet=quiet)
        primed = vid
        threading.Event().wait(FB_POLL_S)


def chat_health(rec, now):
    """Green is the one answer that has to be earned. Anything not positively proven alive
    within CHAT_STALE_S reads as stalled, however recently it was working."""
    if rec["state"] in ("off", "failed", "connecting", "quota", "idle"):
        return rec["state"]
    if now - rec["last_alive"] > CHAT_STALE_S:
        return "stalled"
    return "live"


def chat_status(since=0):
    now = time.time()
    with _chat_lock:
        msgs = [m for m in _chat_msgs if m["seq"] > since]
        seq = _chat_seq
        out = []
        for name, label in CHAT_SOURCES:
            rec = _chat_src.get(name)
            if rec is None:
                out.append({"name": name, "label": label, "health": "none",
                            "detail": CHAT_WHYNOT.get(name, "not connected"),
                            "quiet_s": None})
                continue
            out.append({"name": name, "label": label,
                        "health": chat_health(rec, now), "detail": rec["detail"],
                        "quiet_s": (round(now - rec["last_msg"], 1)
                                    if rec["last_msg"] else None)})
    return {"seq": seq, "msgs": msgs, "sources": out}


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
_cl_fail_until = 0.0
OBS_RETRY_AFTER_S = 6.0
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
    need a restart of a process meant to be left running through a show.

    ⚠️ A CLOSED OBS MUST FAIL INSTANTLY, NOT SLOWLY. obsctl.connect takes 4.1 s to give up
    when nothing is listening - it tries ::1 and then 127.0.0.1, so the timeout argument
    does not cap it - and this function holds _clock the whole time, so every caller
    queues behind it. With the 1 Hz state poll and the 2 s stream poll both arriving, the
    dashboard fell 8 s behind and the browser reported "Failed to fetch" while the rig
    itself was completely healthy. Closing OBS is a normal thing to do between sets; it
    must not take the panel down with it.

    So a failure is remembered briefly and the next attempts are refused locally. The
    window is short enough that reopening OBS is picked up within seconds.
    """
    global _cl, _cl_fail_until
    with _clock:
        try:
            _cl.get_version()
            return _LockedClient(_cl)
        except Exception:
            pass
        if time.time() < _cl_fail_until:
            raise ConnectionError("OBS not reachable (retry suppressed)")
        try:
            _cl = obsctl.connect(timeout=10)
        except (Exception, SystemExit):
            _cl_fail_until = time.time() + OBS_RETRY_AFTER_S
            raise
        _cl_fail_until = 0.0
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


def page_build():
    """A short stamp identifying the dash.html currently on disk.

    Shown in the panel so "am I looking at the new code?" has an answer that is not
    "probably". Reading a changelog while running the previous build is a specific and
    expensive kind of confusion - it sends you to debug a fix you are not running.
    """
    try:
        st = PAGE.stat()
        return time.strftime("%H:%M:%S", time.localtime(st.st_mtime))
    except Exception:
        return "?"


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
        """Serve a file, and tell the browser exactly how stale it may let it get.

        ⚠️ THIS SENT NO CACHE HEADERS AT ALL, which is not the same as sending
        "do not cache". With no Cache-Control, no ETag and no Last-Modified, a browser
        falls back to a heuristic of its own choosing and may reuse the copy it has
        without asking. dash.html is re-read from disk on every request precisely so an
        edit is live on refresh - the whole point of serving it this way - and that
        promise was being quietly handed to the browser's guesswork. "Just refresh"
        was advice with a silent failure mode, and the failure mode is the worst kind:
        you are looking at old code while reading a changelog that says otherwise.

        no-cache does NOT mean "never store" - it means "store it, but revalidate before
        reusing it". Paired with an ETag, an unchanged page costs a 304 and no body, so
        this is nearly free on a LAN and correct when the file has moved.
        """
        if not path.is_file():
            self.send_error(404); return
        st = path.stat()
        # mtime and size: cheap, and together they move whenever an edit lands. A hash
        # would be stricter and would mean reading the file twice on every poll.
        tag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
        if self.headers.get("If-None-Match") == tag:
            self.send_response(304)
            self.send_header("ETag", tag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        b = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("ETag", tag)
        self._maybe_set_cookie()
        self.end_headers()
        self.wfile.write(b)

    def _obs_or_none(self):
        """The OBS client, or None. Callers report the outage instead of crashing."""
        try:
            return client()
        except (Exception, SystemExit):
            return None

    # ---------------------------------------------------------------- access control
    #
    # ⚠️ THIS SERVER WAS OPEN TO THE WHOLE NETWORK AND HAD NO AUTH OF ANY KIND. Measured,
    # not theorised: the rig box at .232 fetched the entire dashboard over the LAN, and the
    # only matches for "auth" in this file were the words "author" and "authoritative" in
    # comments. Fourteen POST routes were reachable by anything that could route here -
    # including /api/power, which puts both phones to sleep, and /api/scene, which changes
    # what the audience is looking at. This box is also on a tailnet.
    #
    # ⚠️ BUT LOOPBACK-ONLY IS THE WRONG FIX, because "open on the phone" is the first line
    # of this file's own docstring. The dashboard is MEANT to be reachable from the couch.
    # So: loopback stays unauthenticated (local tools, the OBS browser source, curl), and
    # everything arriving over the network needs the token.
    #
    # The phone visits http://<ip>:8770/?t=<token> ONCE; that sets a cookie and the
    # bookmark works from then on. A header is also accepted for scripts.
    def _authed(self):
        peer = (self.client_address or ("",))[0]
        if peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            return True
        if not TOKEN:
            return True                      # no token file => auth disabled deliberately
        if self.headers.get("X-Riastrad-Token") == TOKEN:
            return True
        if ("t=" + TOKEN) in (urlparse(self.path).query or ""):
            return True
        cookie = self.headers.get("Cookie") or ""
        return ("riastrad=" + TOKEN) in cookie

    def _deny(self):
        # 403, not 401: a browser basic-auth prompt cannot be satisfied by a token in a URL
        # and would just trap the user in a dialog with nothing useful to type.
        body = (b"Riastrad: not authorised from this address.\n"
                b"Open it once as  http://<this-host>:8770/?t=<token>\n"
                b"The token is in  " + str(TOKEN_FILE).encode() + b"\n")
        self.send_response(403)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _maybe_set_cookie(self):
        """Turn a one-off ?t=<token> into a sticky cookie so the phone bookmark works."""
        if TOKEN and ("t=" + TOKEN) in (urlparse(self.path).query or ""):
            # No Secure flag: this is plain HTTP on a LAN by design. HttpOnly and SameSite
            # still keep it out of page scripts and off cross-site requests.
            self.send_header("Set-Cookie",
                             f"riastrad={TOKEN}; Path=/; Max-Age=31536000; "
                             f"HttpOnly; SameSite=Lax")

    def do_GET(self):
        if not self._authed():
            self._deny(); return
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

            if p == "/api/chat":
                # Cheap by construction - a cursor slice of an in-memory list. The readers
                # are threads of their own, so this never waits on a network round trip
                # and is safe on the 1 Hz poll tier.
                try:
                    since = int((parse_qs(urlparse(self.path).query)
                                 .get("since") or ["0"])[0])
                except ValueError:
                    since = 0
                self._json(chat_status(since)); return

            if p == "/api/stream":
                # Cheap and entirely local - a 2 s-capped loopback call plus a few small
                # file reads - so this one IS safe on the poll tier, unlike /api/camera.
                s = relay_status()
                s["obs"] = obs_stream_state()
                s["record"] = obs_record_state()
                s["meta"] = stream_meta()
                s["twitch_categories"] = twitch_categories()
                self._json(s); return

            if p == "/thumb":
                # ⚠️ SERVES THE CONFIGURED PATH AND NOTHING ELSE. The filename never comes
                # from the request, so there is no traversal to defend against - the only
                # reachable file is whatever stream_thumb points at, which only this
                # dashboard writes. A ?v= cache-buster rides along on the <img> src and is
                # ignored here.
                tp = stream_meta()["thumb"]
                if not os.path.isfile(tp):
                    self.send_error(404); return
                ext = os.path.splitext(tp)[1].lower()
                self._file(Path(tp), {".png": "image/png", ".jpg": "image/jpeg",
                                      ".jpeg": "image/jpeg", ".webp": "image/webp"}
                           .get(ext, "application/octet-stream"))
                return
            if p == "/api/camera":
                # One read of every phone, shared by the state block and the fps delta, so
                # the two cannot disagree about what they were looking at.
                _rc = rigcams_state()
                # On demand only. See the note by RIGCAM above.
                uvc = uvc_call("--state")
                self._json({
                    "rigcam": rigcam_call("/api/state"),
                    "rigcams": _rc,
                    "rigcamFps": rigcams_fps(_rc),
                    # Where each phone actually is. The WiFi phone is not on adb, so this is
                    # the only address the panel can show for it - and 'no address at all'
                    # reads as 'we lost it' rather than 'it is deliberately over HTTP'.
                    "rigcamUrls": dict(RIGCAMS),
                    "uvc": {"reachable": uvc["ok"], "selected": uvc_selected(uvc["out"]),
                            "zooms": list(UVC_ZOOMS), "detail": uvc["out"]},
                    "build": page_build(),
                    "devices": devices_snapshot(),
                    "wiredSerial": UVC_SERIAL,
                    "power": power_call("status", timeout=60),
                    "bridges": bridge_status(),
                }); return

            if p == "/api/scenepresets":
                cl = self._obs_or_none()
                scenes = []
                cur = None
                prev = None
                studio = False
                if cl:
                    try:
                        sl = cl.get_scene_list()
                        scenes = [x["sceneName"] for x in sl.scenes]
                        cur = sl.current_program_scene_name
                        # ⚠️ PREVIEW ONLY EXISTS IN STUDIO MODE, and OBS omits the field
                        # entirely when it is off - so this is None rather than equal to
                        # program, and the panel must not draw a preview marker that would
                        # be a lie about a mode you are not in.
                        studio = cl.get_studio_mode_enabled().studio_mode_enabled
                        prev = getattr(sl, "current_preview_scene_name", None) if studio else None
                    except Exception:
                        pass
                self._json({
                    "presets": load_presets(),
                    "obsScenes": scenes,
                    "currentScene": cur,
                    "previewScene": prev,
                    "studio": studio,
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
        if not self._authed():
            self._deny(); return
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
                # ⚠️ "preview" AND "program" ARE NOT THE SAME BUTTON WITH A FLAG.
                # In studio mode, setting program cuts to air instantly with no
                # transition and no second look - which is the one thing studio mode
                # exists to prevent. So the panel only ever sends "preview" while studio
                # mode is on, and "take" is the deliberate separate act.
                if act == "studio":
                    client().set_studio_mode_enabled(bool(body.get("on")))
                    self._json({"ok": True, "studio": bool(body.get("on"))}); return
                if act == "preview":
                    scene = body.get("scene")
                    if not scene:
                        self._json({"error": "scene required"}, 400); return
                    client().set_current_preview_scene(scene)
                    self._json({"ok": True, "preview": scene}); return
                if act == "program":
                    scene = body.get("scene")
                    if not scene:
                        self._json({"error": "scene required"}, 400); return
                    client().set_current_program_scene(scene)
                    self._json({"ok": True, "program": scene}); return
                if act == "take":
                    client().trigger_studio_mode_transition()
                    self._json({"ok": True}); return
                if act == "capture":
                    scene = body.get("scene") or client().get_current_program_scene().scene_name
                    self._json({"scene": scene, "entry": capture_preset(scene),
                                "presets": load_presets()}); return
                if act == "apply":
                    scene = body.get("scene") or client().get_current_program_scene().scene_name
                    self._json({"scene": scene, "steps": apply_preset(scene)}); return
                self._json({"error": "action must be save, capture, apply, enable, "
                                     "studio, preview, program or take"}, 400)
                return

            if p == "/api/stream":
                # Above the OBS guard for relay_restart, which is pure schtasks and must
                # work with OBS closed - the same rule /api/power already follows.
                act = body.get("action")
                if act == "relay_restart":
                    self._json(relay_restart()); return

                if act == "enable":
                    payload, code = set_enabled(body.get("platform"), bool(body.get("on")))
                    self._json(payload, code); return

                if act == "preflight":
                    # Creates nothing. Refreshes tokens and does one cheap read per
                    # platform, so "armed" can be checked without littering the channel
                    # with empty broadcasts.
                    self._json(connect_run(["golive", "--dry-run"], timeout=60)); return

                if act == "golive":
                    # ⚠️ TITLES BEFORE OBS, AND OBS ONLY IF THEY WORKED. golive writes a
                    # fresh FACEBOOK_KEY when it creates a live video, and push.ps1 reads
                    # keys.env when the relay's supervisor starts - which is when OBS
                    # begins sending. Start OBS first and Facebook gets last set's key.
                    #
                    # A failure here also STOPS the launch by default. Finding out the
                    # broadcast was never created is survivable before you are sending and
                    # expensive afterwards - but `force` exists so a titling failure can
                    # never trap you off-air mid-set.
                    # An empty title skips connect.py altogether - that is the path for
                    # a hand-pasted key with no app registered, and it must work.
                    title = (body.get("title") or "").strip()
                    res = {"ok": True, "exit": 0, "lines": []}
                    if title:
                        a = ["golive", "--title", title]
                        if (body.get("desc") or "").strip():
                            a += ["--desc", body["desc"].strip()]
                        if body.get("privacy") in ("public", "unlisted", "private"):
                            a += ["--privacy", body["privacy"]]
                        # Twitch-only extras. Passed through untouched; connect.py owns
                        # what each platform does with them, and rejects what it cannot use.
                        if (body.get("category") or "").strip():
                            a += ["--category", body["category"].strip()]
                        if (body.get("tags") or "").strip():
                            a += ["--tags", body["tags"].strip()]
                        res = connect_run(a, timeout=120)
                    if not res["ok"] and not body.get("force"):
                        res["started"] = False
                        self._json(res); return
                    try:
                        cl = obsctl.connect(timeout=4)
                        cl.start_stream()
                        res["started"] = True
                        # ⚠️ A FAILED RECORDING MUST NOT SILENTLY NOT HAPPEN. The whole
                        # point of arming it is that you are not watching this panel once
                        # the set starts, so if the disk is full or OBS refuses, that has
                        # to be in the report. It does NOT fail the broadcast, though -
                        # losing the archive is bad, losing the show is worse.
                        if prefs().get("record_with_golive"):
                            try:
                                cl.start_record()
                                res["lines"] = res["lines"] + ["recording: started"]
                            except Exception as e:
                                res["lines"] = res["lines"] + [
                                    f"recording: FAILED, {type(e).__name__} - "
                                    f"the stream is live but nothing is being saved"]
                    except (Exception, SystemExit) as e:
                        res["started"] = False
                        res["lines"] = res["lines"] + [f"OBS: FAILED, {type(e).__name__}"]
                        res["ok"] = False
                    self._json(res); return
                if act == "set_meta":
                    # Pure preference write; touches no platform.
                    try:
                        for k, pref in (("title", "stream_title"), ("desc", "stream_desc"),
                                        ("thumb", "stream_thumb")):
                            if k in body:
                                set_pref(pref, str(body.get(k) or "")[:400])
                    except Exception as e:
                        self._json({"error": "could not save: %s" % type(e).__name__}, 500)
                        return
                    self._json({"ok": True, "meta": stream_meta()}); return
                if act == "thumbnail":
                    self._json(set_thumbnail_now()); return
                if act == "rec_arm":
                    # Pure preference write - touches no OBS, so it works with OBS shut.
                    on = bool(body.get("on"))
                    try:
                        set_pref("record_with_golive", on)
                    except Exception as e:
                        self._json({"error": f"could not save: {type(e).__name__}"}, 500)
                        return
                    self._json({"ok": True, "armed": on}); return
                if act in ("rec_start", "rec_stop"):
                    try:
                        cl = obsctl.connect(timeout=4)
                        if act == "rec_start":
                            cl.start_record()
                        else:
                            cl.stop_record()
                    except (Exception, SystemExit) as e:
                        self._json({"error": f"OBS not reachable: {type(e).__name__}"}, 503)
                        return
                    self._json({"ok": True, "action": act}); return
                if act in ("obs_start", "obs_stop"):
                    try:
                        cl = obsctl.connect(timeout=4)
                        if act == "obs_start":
                            cl.start_stream()
                            if prefs().get("record_with_golive"):
                                try:
                                    cl.start_record()
                                except Exception:
                                    pass
                        else:
                            cl.stop_stream()
                            # Symmetry: if arming it started the recording, stopping the
                            # broadcast ends it. Leaving a recording running after the set
                            # fills a disk overnight and nobody notices until it is full.
                            if prefs().get("record_with_golive"):
                                try:
                                    cl.stop_record()
                                except Exception:
                                    pass
                    except (Exception, SystemExit) as e:
                        self._json({"error": f"OBS not reachable: {type(e).__name__}"}, 503)
                        return
                    self._json({"ok": True, "action": act}); return
                self._json({"error": f"unknown action {act!r}"}, 400); return

            if p == "/api/bridges":
                if body.get("action") != "reset":
                    self._json({"error": "action must be reset"}, 400); return
                # No "bridge" key means both, which is what the old callers sent.
                want = body.get("bridge")
                if want is None:
                    self._json(bridge_reset()); return
                one = bridge_find(want)
                if one is None:
                    self._json({"error": f"unknown bridge {want!r}"}, 400); return
                self._json(bridge_reset(one)); return

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
    threading.Thread(target=twitch_chat_thread, daemon=True).start()
    threading.Thread(target=youtube_chat_thread, daemon=True).start()
    threading.Thread(target=facebook_chat_thread, daemon=True).start()
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
        # Loopback needs no token; anything else does, so print the URL that actually
        # works from the couch rather than one that 403s.
        q = "" if ip in ("127.0.0.1", "localhost") or not TOKEN else f"?t={TOKEN}"
        print(f"  {label:<6} dashboard http://{ip}:{args.port}/{q}", flush=True)
    # Say which mode this is, every time. "Is it locked right now" must never be a thing
    # you work out by getting a 403 on the couch.
    print(f"  auth  {'ON  - token required off-box (' + str(TOKEN_FILE) + ')' if TOKEN else 'off - open on the LAN, as before'}",
          flush=True)
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
