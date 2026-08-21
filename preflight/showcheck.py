#!/usr/bin/env python3
"""
showcheck - one command that answers "is the whole rig actually ready".

WHY THIS EXISTS: every check in this file corresponds to a failure that was hit for real,
and every one of them REPORTS HEALTHY WHILE BEING BROKEN. That is the unifying property.
An ASIO device can be open with a half-seated cable. An OBS camera source can exist and
have never received a frame. An OSC link can carry 300 messages a second, all of them
noise. Ollama can answer while running 11x slow on the CPU.

So the rule this file is built on: **check function, never existence.** "The device is
open" is not "sound is coming out". "The source exists" is not "frames are arriving".
"Packets are flowing" is not "the right packets are flowing".

Runs from the desktop and reaches everything: the rig over HTTP, OSC over UDP, OBS over
websocket, the phone over ADB, Ollama over HTTP. Read-only - it changes nothing, so it is
safe to run mid-setup or thirty seconds before a set.

    python showcheck.py                # everything
    python showcheck.py --only rig obs # subsets
    python showcheck.py --json         # machine readable
"""

import argparse
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

RIG_HOST = "192.168.1.232"
RIG_PORT = 8765
OSC_PORT = 8000
OLLAMA = "http://localhost:11434"

OK, WARN, BAD, SKIP = "ok", "warn", "bad", "skip"
results = []


def check(group, label, state, detail, fix=""):
    results.append({"group": group, "label": label, "state": state,
                    "detail": detail, "fix": fix})


def rig_token():
    """The agent's token is generated on the rig. Prefer a local copy, fall back to the
    env, and say plainly when neither exists rather than failing with a 401 that reads
    like the rig is down."""
    for p in (os.path.expanduser("~/.riomhdhos-token"),
              os.path.join(os.path.dirname(__file__), "agent.token")):
        if os.path.exists(p):
            return open(p, encoding="utf-8").read().strip()
    return os.environ.get("RIOMHDHOS_TOKEN", "")


# --------------------------------------------------------------------------- rig

def check_rig(token):
    g = "rig"
    if not token:
        check(g, "agent token", SKIP, "no token",
              "Put the agent token in ~/.riomhdhos-token or set RIOMHDHOS_TOKEN.")
        return

    def api(path, timeout=40):
        req = urllib.request.Request(f"http://{RIG_HOST}:{RIG_PORT}{path}",
                                     headers={"Authorization": f"Bearer {token}"})
        return json.load(urllib.request.urlopen(req, timeout=timeout))

    try:
        h = api("/api/health?deep=1")
    except Exception as e:
        check(g, "reachable", BAD, str(e),
              "The rig is not answering. Check it is powered and on the network.")
        return

    check(g, "reachable", OK, f"{h['host']['name']}, up {h['host']['uptimeMin']} min")

    r = h.get("reaper", {})
    if not r.get("running"):
        check(g, "REAPER", BAD, "not running", "Start REAPER from the phone app.")
    elif not r.get("responding"):
        check(g, "REAPER", BAD, "not responding",
              "This is the ASIO exit deadlock. Restart REAPER.")
    else:
        check(g, "REAPER", OK, f"pid {r.get('pid')}, {r.get('memMB')} MB")

    # Buffer size is checked as a VALUE, not merely as present. It silently reverts to the
    # driver default of 512 whenever the interface enumerates on a different USB port -
    # which happened for real, and looked like a latency problem it was not.
    deep = h.get("deep") or {}
    bsize = deep.get("audio_bsize")
    if bsize is None:
        check(g, "buffer", WARN, "unknown", "Deep probe unavailable; is the console running?")
    elif str(bsize) == "128":
        check(g, "buffer", OK, "128 samples (2.9 ms)")
    else:
        check(g, "buffer", WARN, f"{bsize} samples",
              "Expected 128. The UMC reverts to 512 when it enumerates on a different USB "
              "port. Fix in the UMC tray panel, not in REAPER.")

    for name, want in (("audio_out", "ASIO"), ("audio_mode", "ASIO")):
        v = deep.get(name, "")
        if "Remote Audio" in str(v) or "RDP" in str(v):
            check(g, name, BAD, str(v),
                  "REAPER came up inside an RDP session and is not on the real interface.")

    # Controllers: REAPER stays perfectly 'healthy' with every controller unplugged.
    midi = deep.get("midi_in", "")
    if "Ableton Push" in midi and "Ableton Push [disconnected]" not in midi:
        check(g, "Push", OK, "connected")
    else:
        check(g, "Push", BAD, "not connected",
              "No controller. Check the USB cable and hub power.")

    # Levels: the only check that distinguishes "device open" from "sound coming out".
    try:
        lv = api("/api/levels")
        if lv.get("available"):
            if lv.get("masterSilent"):
                check(g, "signal", WARN, "master silent",
                      "Nothing reaching the output. Play something while this runs - "
                      "levels are peak-held over ~700 ms, so silence here with nothing "
                      "playing is normal.")
            else:
                check(g, "signal", OK, f"master {lv['masterL']} / {lv['masterR']} dBFS")
            for t in lv.get("tracks", []):
                if t.get("armed") and t.get("silent") and not t.get("midi"):
                    check(g, f"input: {t['name']}", WARN, "armed but silent",
                          "Nothing arriving. Check the cable is fully seated - a "
                          "half-seated jack presents as a healthy device with no signal.")
        else:
            check(g, "signal", WARN, lv.get("note", "unavailable"), "")
    except Exception as e:
        check(g, "signal", WARN, str(e), "")

    # DPC headroom, which decides whether 128 samples is actually survivable.
    try:
        lat = api("/api/latency?min=30")
        if lat.get("available"):
            dpc = lat.get("dpc") or {}
            mx = dpc.get("max")
            st = OK if mx is not None and mx < 4 else (WARN if mx is not None and mx < 10 else BAD)
            check(g, "DPC headroom", st, f"peak {mx}% over {lat['samples']} samples",
                  "" if st == OK else "A driver is holding the CPU long enough to threaten "
                                      "the 2.9 ms deadline.")
        else:
            check(g, "DPC headroom", SKIP, lat.get("note", ""), "")
    except Exception:
        check(g, "DPC headroom", SKIP, "sampler not reporting", "")


# --------------------------------------------------------------------------- osc

def check_osc(seconds=6):
    """Listening is the only way to tell a configured OSC link from a working one - and,
    more importantly, from a link that is 100% noise.

    This exact failure happened: the drum sequencer free-runs, its 'armed mask' slider
    changes continuously, and REAPER reported every change as the last-touched FX
    parameter. 300 messages/second, forever, with the transport stopped. Track and
    transport feedback never got through. A link that is entirely noise looks alive.
    """
    g = "osc"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", OSC_PORT))
    except OSError as e:
        check(g, "listen", SKIP, f"cannot bind {OSC_PORT}: {e}",
              "Something else holds the port - probably the bridge, which is fine.")
        return
    s.settimeout(0.5)

    def pad4(n): return (n + 3) & ~3

    def rd(b, i):
        j = b.index(b"\x00", i)
        return b[i:j].decode("ascii", "replace"), i + pad4(j - i + 1)

    def walk(b, i, end, out):
        if b[i:i + 8] == b"#bundle\x00":
            i += 16
            while i + 4 <= end:
                (sz,) = struct.unpack(">i", b[i:i + 4]); i += 4
                if sz <= 0 or i + sz > end: break
                walk(b, i, i + sz, out); i += sz
        else:
            a, _ = rd(b, i); out.append(a)

    seen, total = {}, 0
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            data, _ = s.recvfrom(8192)
        except socket.timeout:
            continue
        acc = []
        try: walk(data, 0, len(data), acc)
        except Exception: continue
        for a in acc:
            seen[a] = seen.get(a, 0) + 1
            total += 1
    s.close()

    if not total:
        check(g, "messages", WARN, "nothing received",
              "Either REAPER is not sending, or nothing changed. OSC feedback is "
              "event-driven - move a fader on the rig and re-run.")
        return

    rate = total / seconds
    useful = {a: n for a, n in seen.items() if a.startswith(("/track", "/tempo", "/play", "/stop"))}
    noise = {a: n for a, n in seen.items() if a.startswith("/fxparam") or "last_touched" in a}

    if noise and not useful:
        check(g, "messages", BAD, f"{rate:.0f}/s, ALL noise ({max(noise, key=noise.get)})",
              "The stock Default.ReaperOSC sends FX-parameter feedback and the free-running "
              "sequencer floods it. Select the 'Riomhdhos' pattern config on the OSC device.")
    elif noise:
        check(g, "messages", WARN, f"{rate:.0f}/s, {sum(noise.values())} noise msgs",
              "FX feedback is still enabled; it will crowd out track state under load.")
    else:
        check(g, "messages", OK, f"{rate:.0f}/s, {len(useful)} useful addresses")


# --------------------------------------------------------------------------- obs

def check_obs():
    g = "obs"
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "video"))
        import obsctl
        cl = obsctl.connect(timeout=10)
    except SystemExit as e:
        check(g, "connect", BAD, str(e), "Start OBS, and enable the websocket server.")
        return
    except Exception as e:
        check(g, "connect", BAD, str(e), "")
        return

    v = cl.get_version()
    check(g, "connect", OK, f"OBS {v.obs_version}, websocket {v.obs_web_socket_version}")

    scene = cl.get_scene_list().current_program_scene_name

    # ⚠️ A DirectShow device has exactly ONE consumer. Two OBS sources on one camera is
    # the version of this that is hardest to spot, because both look correctly configured
    # and one of them silently gets nothing.
    dshow = {}
    for i in cl.get_input_list().inputs:
        if i["inputKind"] == "dshow_input":
            st = cl.get_input_settings(i["inputName"]).input_settings
            dev = st.get("video_device_id", "")
            dshow.setdefault(dev, []).append((i["inputName"], st))
    for dev, users in dshow.items():
        if len(users) > 1:
            check(g, "camera contention", BAD,
                  f"{len(users)} sources share one device: " + ", ".join(u[0] for u in users),
                  "Only one can receive frames. Delete the duplicates.")

    # Frames arriving, not merely a source existing. 0x0 means no frame has EVER arrived.
    for it in cl.get_scene_item_list(scene).scene_items:
        nm = it["sourceName"]
        if not any(nm == u[0] for us in dshow.values() for u in us):
            continue
        t = cl.get_scene_item_transform(scene, it["sceneItemId"]).scene_item_transform
        if (t["sourceWidth"], t["sourceHeight"]) == (0, 0):
            check(g, f"camera: {nm}", BAD, "0x0 - no frames",
                  "Configured but never opened the device. Run: obsctl.py cycle "
                  f"\"{nm}\". If that fails, something else holds the camera.")
        else:
            check(g, f"camera: {nm}", OK, f"{t['sourceWidth']:.0f}x{t['sourceHeight']:.0f}")

    # Format pinned, not left on Any. At 1080p the Pixel offers ONLY MJPEG, so Any means
    # the driver may pick a mode that cannot stream - a source that holds the device and
    # shows black.
    for dev, users in dshow.items():
        for nm, st in users:
            if st.get("res_type") == 1 and st.get("video_format") in (None, 0):
                check(g, f"format: {nm}", WARN, "video_format = Any",
                      "Pin it explicitly (MJPEG = 400). 'Any' lets the driver choose, and "
                      "at 1080p an uncompressed choice cannot stream.")


def check_obs_encoders():
    """StreamEncoder and RecEncoder are read from disk, not the API - obs-websocket has no
    request for them. ⚠️ The file is stale while OBS is running, so this reports what OBS
    will use NEXT launch, which is still worth knowing before a show."""
    g = "obs"
    base = os.path.join(os.environ.get("APPDATA", ""), "obs-studio", "basic", "profiles")
    if not os.path.isdir(base):
        return
    prof = sorted(os.listdir(base))[0]
    ini = os.path.join(base, prof, "basic.ini")
    if not os.path.exists(ini):
        return
    enc = {}
    for line in open(ini, encoding="utf-8", errors="replace"):
        for k in ("StreamEncoder", "RecEncoder"):
            if line.startswith(k + "="):
                enc.setdefault(k, line.split("=", 1)[1].strip())
    if enc.get("RecEncoder") == "x264" and enc.get("StreamEncoder") == "amd":
        check(g, "encoders", WARN, "stream=amd, record=x264",
              "Recording on the CPU while streaming on the GPU. Both should be amd - the "
              "GPU's media engine is separate silicon and handles both for free.")
    elif enc:
        check(g, "encoders", OK, ", ".join(f"{k.replace('Encoder','')}={v}" for k, v in enc.items()))


# --------------------------------------------------------------------------- phone

def check_phone():
    g = "phone"
    adb = shutil.which("adb") or os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages",
        "Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe",
        "platform-tools", "adb.exe")
    if not os.path.exists(adb):
        check(g, "adb", SKIP, "not found", "")
        return

    def sh(*a, timeout=15):
        return subprocess.run([adb] + list(a), capture_output=True, text=True,
                              timeout=timeout).stdout.strip()

    devs = [l for l in sh("devices").splitlines()[1:] if "\tdevice" in l]
    if not devs:
        check(g, "connected", WARN, "no authorised device",
              "Plug the phone in; accept the RSA prompt if it appears.")
        return
    serial = devs[0].split("\t")[0]
    check(g, "connected", OK, serial)

    # ⚠️ USB function does NOT survive a replug - Android reverts to charging/ADB, so the
    # camera silently stops being a camera while ADB keeps working and everything looks
    # fine. But DO NOT ask the phone what mode it is in:
    #
    #   svc usb getFunctions  ->  ''        phone says it is not a webcam
    #   sys.usb.config        ->  'adb'
    #   Windows PnP           ->  Camera: Android Webcam    it demonstrably IS one
    #
    # When DeviceAsWebcam owns the gadget it does not go through the legacy USB function
    # properties, so the phone's self-report is simply wrong. Ask the HOST what it can
    # see instead - which is the principle this whole file is built on, and which this
    # check got wrong on the first pass.
    host_sees = _host_usb_roles()
    if "camera" in host_sees:
        check(g, "usb mode", OK, "webcam (host enumerates a Camera device)")
    elif "net" in host_sees:
        check(g, "usb mode", OK, "tethering (host enumerates a network device)")
    else:
        check(g, "usb mode", WARN, "charging/ADB only",
              f"Not a camera or a network link. Fix: adb -s {serial} shell svc usb "
              "setFunctions uvc  - or pick Webcam in the phone's USB notification.")

    bat = sh("-s", serial, "shell", "dumpsys", "battery")
    def field(n):
        return next((l.split(":")[1].strip() for l in bat.splitlines()
                     if l.strip().startswith(n)), "")
    lvl, stat = field("level") or "?", field("status")
    # 1 unknown, 2 charging, 3 discharging, 4 not-charging, 5 full. 3 and 4 are DIFFERENT
    # and the difference is diagnostic: 3 means unplugged and draining, 4 means plugged in
    # and stalled - taking everything the port offers and still not gaining.
    words = {"1": "unknown", "2": "charging", "3": "DISCHARGING",
             "4": "NOT CHARGING (plugged, stalled)", "5": "full"}
    # The root cause when it is 4: measured 500 mA max charge current on this port - USB
    # 2.0 default power, 2.5 W - against a 3-5 W draw for capture plus encode plus USB.
    # It cannot win, so it treads water and tips either side of break-even with load.
    # Over a set that means starting full and hoping, rather than knowing.
    ma = field("Max charging current")
    ma_txt = f", {int(ma)//1000} mA supply" if ma.isdigit() else ""
    state = OK if stat in ("2", "5") else WARN
    check(g, "battery", state, f"{lvl}%, {words.get(stat, stat)}{ma_txt}",
          "" if state == OK else
          "Plugged in but not gaining. A 500 mA port is USB 2.0 default power (2.5 W) "
          "against a 3-5 W draw. Use a powered hub or a USB-C port that negotiates more.")


def _host_usb_roles():
    """What the HOST enumerates for the phone - the only trustworthy statement of what
    mode it is really in."""
    roles = set()
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-PnpDevice -ErrorAction SilentlyContinue | "
             "Where-Object { $_.InstanceId -match 'VID_18D1' -and $_.Status -eq 'OK' } | "
             "ForEach-Object { $_.Class }"],
            capture_output=True, text=True, timeout=25).stdout.lower()
        if "camera" in out: roles.add("camera")
        if "net" in out:    roles.add("net")
    except Exception:
        pass
    return roles


# --------------------------------------------------------------------------- ollama

def _frame_hash(cl, source):
    """Hash one rendered frame. None if the screenshot fails."""
    try:
        r = cl.get_source_screenshot(source, "png", 160, 90, -1)
        return hashlib.md5(r.image_data.encode()).hexdigest()
    except Exception:
        return None


def check_cameras(dash="http://127.0.0.1:8770"):
    """The camera path, from each phone through to a source OBS is actually rendering.

    ⚠️ Every check here asks whether something DELIVERS, never whether it exists. A dshow
    source can hold a device and show nothing; an ffmpeg source can be configured perfectly
    and never open; RigCam can be installed and not running. Existence proves nothing.
    """
    g = "cameras"
    try:
        cam = json.load(urllib.request.urlopen(dash + "/api/camera", timeout=75))
    except Exception as e:
        check(g, "dashboard", BAD, str(e),
              f"The rig dashboard is not answering at {dash}. Start it: "
              "python video/rigdash/dash.py --uvc-serial <wired> --rigcam http://<ip>:8090")
        return
    check(g, "dashboard", OK, dash)

    # ⚠️ The bridge is what makes the WiFi camera usable: measured 414 ms through it against
    # 853 ms through OBS's Media Source. If it stops, that feed goes cold - so check the
    # process exists BEFORE checking the picture, or a dead bridge just looks like a frozen
    # camera and you debug the wrong end.
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or "
             "Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*vcambridge*' } "
             "| Select-Object -First 1 -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=25).stdout.strip()
    except Exception:
        out = ""
    if out.isdigit():
        check(g, "vcam bridge", OK, f"running (pid {out})")
    else:
        check(g, "vcam bridge", WARN, "not running",
              "The WiFi camera's low-latency path is down. Start it: "
              "Start-ScheduledTask -TaskName 'Riomhdhos vcam bridge'  - or run "
              "python video/vcambridge.py by hand.")

    u = cam.get("uvc") or {}
    if u.get("reachable"):
        check(g, "wired camera", OK, f"zoom {u.get('selected') or '?'}x via ADB")
    else:
        check(g, "wired camera", WARN, "not reachable",
              "uvczoom could not read the phone's UI. It needs the phone AWAKE and "
              "UNLOCKED - there is no way round that, it is a UI tap. "
              "adb shell settings put global stay_on_while_plugged_in 3 keeps it awake.")

    r = cam.get("rigcam") or {}
    if r.get("offline"):
        check(g, "wifi camera", WARN, f"offline ({r.get('error')})",
              "RigCam is not answering. It now runs as a foreground service, so it should "
              "survive backgrounding - if it is gone, the app was stopped or the phone left "
              "the network.")
    elif not r.get("streaming"):
        check(g, "wifi camera", WARN, "reachable but not streaming",
              "The server is up but no frames are encoded. CameraX will not open a camera "
              "behind a keyguard - unlock the phone.")
    else:
        e = r.get("encoder") or {}
        check(g, "wifi camera", OK,
              f"{r.get('resolution')} @{r.get('fps')} {e.get('bitrateKbps')} kbps")
        if e.get("lastError"):
            check(g, "wifi encoder", WARN, e["lastError"],
                  "The encoder fell back to its ByteBuffer input path. Harmless, but it "
                  "means getInputImage failed on this device.")

    # And finally: is OBS actually rendering them?
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "video"))
        import obsctl
        cl = obsctl.connect(timeout=15)
        sl = cl.get_scene_list()
        scenes = {s_["sceneName"] for s_ in sl.scenes}
        program = sl.current_program_scene_name
        kinds = {i["inputName"]: i["inputKind"] for i in cl.get_input_list().inputs}
        # (scene, source). BOTH CAMS is the program scene and holds both live cameras, so
        # it is the one place where a failure actually reaches the audience - check them
        # there rather than in the single-camera cut scenes.
        #
        # The WiFi phone is checked as 'Pixel 6 (vcam)' - it reaches OBS through
        # vcambridge and the virtual-camera sink. The old Media Source path was
        # 853 ms against 414 ms and has been removed entirely.
        for scene, src in (("BOTH CAMS", "Pixel 8"),
                           ("BOTH CAMS", "Pixel 6 (vcam)")):
            kind = kinds.get(src, "")
            if scene not in scenes:
                check(g, f"obs {src}", SKIP, f"no scene {scene}")
                continue
            # ⚠️ A DISABLED SCENE ITEM AND A DEAD SOURCE BOTH MEASURE 0x0, and the
            # difference matters enormously: one is a checkbox, the other is a broken
            # capture path. This cost a long detour chasing containers and buffer sizes
            # when the actual cause was an unticked item.
            items = cl.get_scene_item_list(scene).scene_items
            hit = next((i for i in items if i["sourceName"] == src), None)
            if hit is None:
                check(g, f"obs {src}", SKIP, f"not in scene {scene}")
                continue
            if not hit["sceneItemEnabled"]:
                check(g, f"obs {src}", WARN, "scene item is DISABLED",
                      f"It will never open while unticked - a disabled item is never "
                      f"activated. Tick '{src}' in scene '{scene}'.")
                continue
            w, h = obsctl._source_size(cl, scene, src)
            if (w, h) == (0, 0):
                # ⚠️ A media source only RUNS while its scene is the program scene, unlike a
                # dshow camera which runs whenever it is active. Reporting that as a broken
                # capture path sends you hunting for a fault that does not exist - it cost a
                # whole debugging session once, through a source rebuild, a media-restart, a
                # scene-item toggle and an OBS restart.
                if kind == "ffmpeg_source" and scene != program:
                    check(g, f"obs {src}", SKIP,
                          f"idle - '{scene}' is not the program scene",
                          "A media source only runs while its scene is live. Switch to "
                          f"'{scene}' to test it.")
                else:
                    check(g, f"obs {src}", BAD, "0x0 - holding the device, showing nothing",
                          "A source at 0x0 has never received a frame. For the wired camera "
                          "check nothing else claimed it (one DirectShow consumer only); "
                          "for the WiFi one check RigCam is serving.")
                continue

            # ⚠️ A RESOLUTION IS NOT A PICTURE. This check reported `ok 1920x1080` for a
            # source frozen on a stale frame - the torch produced no change in it at all.
            # A stale frame keeps its resolution forever.
            #
            # Two frames a second apart settle it, and sensor noise is what makes it
            # reliable: a live camera never produces byte-identical frames, even pointed at
            # a still room, while a frozen source repeats exactly.
            a = _frame_hash(cl, src)
            time.sleep(1.1)
            b = _frame_hash(cl, src)
            if a and b and a == b:
                check(g, f"obs {src}", BAD, f"{int(w)}x{int(h)} but FROZEN",
                      "Two frames a second apart were byte-identical, which a live camera "
                      "never manages. The source is holding a stale frame: it reports a "
                      "resolution and delivers nothing.")
            else:
                check(g, f"obs {src}", OK, f"{int(w)}x{int(h)} live")
    except Exception as e:
        check(g, "obs sources", WARN, f"{type(e).__name__}: {e}")


def check_ollama():
    g = "ai"
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/ps", timeout=6) as r:
            ps = json.load(r)
    except Exception:
        check(g, "ollama", SKIP, "not running", "")
        return

    models = ps.get("models", [])
    if not models:
        check(g, "ollama", WARN, "running, no model resident",
              "First request will stall ~16 s loading from disk. Warm it before a show.")
        return

    for m in models:
        total, gpu = m.get("size", 0), m.get("size_vram", 0)
        pct = (gpu / total * 100) if total else 0
        # ⚠️ A SINGLE CPU-placed request poisons the resident instance: every later request
        # reuses it and reports 100% CPU indefinitely. It looks like the GPU stopped
        # working. `ollama stop <model>` clears it - it is an instance, not a setting.
        if pct >= 95:
            check(g, f"ollama: {m['name']}", OK, f"{pct:.0f}% GPU, {total/1e9:.1f} GB")
        else:
            check(g, f"ollama: {m['name']}", BAD, f"only {pct:.0f}% on GPU",
                  f"Running on the CPU - roughly 11x slower. Run: ollama stop {m['name']}")
        # Keep-alive: the default 5 minutes guarantees the worst latency exactly when
        # chat resumes after a quiet passage.
        if m.get("expires_at", "").startswith("0001") or "9999" in str(m.get("expires_at", "")):
            check(g, "ollama keep-alive", OK, "resident indefinitely")


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*",
                    choices=["rig", "osc", "obs", "phone", "cameras", "ai"])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--osc-seconds", type=int, default=6)
    args = ap.parse_args()
    want = set(args.only) if args.only else {"rig", "osc", "obs", "phone",
                                             "cameras", "ai"}

    if "rig" in want:   check_rig(rig_token())
    if "osc" in want:   check_osc(args.osc_seconds)
    if "obs" in want:   check_obs(); check_obs_encoders()
    if "phone" in want: check_phone()
    if "cameras" in want: check_cameras()
    if "ai" in want:    check_ollama()

    if args.json:
        print(json.dumps(results, indent=2))
        return

    mark = {OK: "  ok ", WARN: " WARN", BAD: " BAD ", SKIP: " --  "}
    group = None
    for r in results:
        if r["group"] != group:
            group = r["group"]
            print(f"\n{group.upper()}")
        print(f" {mark[r['state']]}  {r['label']:<24} {r['detail']}")
        if r["fix"] and r["state"] in (WARN, BAD):
            for line in _wrap(r["fix"], 74):
                print(f"           {line}")

    bad = sum(1 for r in results if r["state"] == BAD)
    warn = sum(1 for r in results if r["state"] == WARN)
    print(f"\n{'-' * 60}\n{len(results)} checks: {bad} bad, {warn} warn")
    sys.exit(1 if bad else 0)


def _wrap(text, width):
    out, line = [], ""
    for w in text.split():
        if len(line) + len(w) + 1 > width:
            out.append(line); line = w
        else:
            line = f"{line} {w}".strip()
    if line: out.append(line)
    return out


if __name__ == "__main__":
    # cp1252 is the console default here, and a stray non-ASCII character in a print has
    # now killed three separate tools AT THE MOMENT THEY HAD SOMETHING USEFUL TO SAY.
    # Degrade the character, never the process.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
