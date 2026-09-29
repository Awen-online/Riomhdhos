#!/usr/bin/env python3
"""
vcambridge - decode the phone's stream here and present it to OBS as a WEBCAM.

⚠️ WHY THIS EXISTS, AND WHY IT IS NOT ABOUT THE NETWORK. Measured, by decoding the phone's
stream straight off the wire with ffmpeg and OBS not involved at all:

    camera -> encode -> WiFi -> decode      80 ms
    the same stream measured inside OBS    ~790 ms

The phone and the network account for a tenth of the delay. OBS's **Media Source** adds the
rest, and its own knobs do not touch it: default 787 ms, `buffering_mb 0` 780 ms, and
`nobuffer flags=low_delay` made it slightly WORSE at 824 ms. Six transport-side hypotheses
were tested and disproved before this - USB instead of WiFi (identical), MPEG-TS with real
timestamps (331 ms worse), a shallower client queue, the encoder's own low-latency keys.
None of them mattered, because none of them were the problem.

So this bypasses Media Source entirely. ffmpeg decodes here, the frames go into the
**OBS Virtual Camera** DirectShow sink, and OBS reads it as an ordinary webcam - the same
class of source as the wired Pixel 8, which does not suffer this buffering.

This is the architecture DroidCam uses, and it is why DroidCam feels responsive: their PC
client decodes and feeds a virtual camera driver rather than handing a URL to a player.

    python vcambridge.py                          # defaults to the Pixel 6 over WiFi
    python vcambridge.py --url http://127.0.0.1:8091/stream.h264    # over adb forward
    python vcambridge.py --size 1920x1080

Then in OBS: add a **Video Capture Device** and pick **OBS Virtual Camera**.

⚠️ The OBS Virtual Camera sink has ONE producer. Stop OBS's own "Start Virtual Camera"
before running this, or they will fight over it.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

import numpy as np
import pyvirtualcam

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))


def match_obs_source(w, h, device, name=None):
    """Point the OBS capture source at the size we are actually sending.

    WARNING: A DSHOW SOURCE PINNED TO THE WRONG RESOLUTION SHOWS BLACK, SILENTLY. res_type=1
    means a custom resolution, so the source asks the device for exactly that and gets
    nothing when the device offers something else. Observed: RigCam at 720p, the source still
    pinned to 1920x1080 from an earlier session, bridge healthy and feeding, OBS black - and
    nothing anywhere reported an error. Changing the phone's resolution is a normal thing to
    do from the Cams tab, so this has to follow it rather than wait to be noticed mid-show.

    Never raises: OBS being closed is a normal state for this process.
    """
    try:
        # Check the port before obsctl does. obsctl prints a full websocket traceback and then
        # sys.exit()s when OBS is absent, and OBS being absent is normal here - the bridge is
        # meant to be up before OBS is. A refused connect is one syscall; a traceback in the
        # log of a service that is working fine is a false alarm someone has to rule out.
        import socket
        with socket.socket() as probe:
            probe.settimeout(1.0)
            if probe.connect_ex(("127.0.0.1", 4455)) != 0:
                return "OBS not running"
        import obsctl
        c = obsctl.connect(timeout=5)
        want = f"{w}x{h}"
        for inp in c.get_input_list(None).inputs:
            if inp["inputKind"] != "dshow_input":
                continue
            cur = c.get_input_settings(inp["inputName"]).input_settings
            dev = str(cur.get("video_device_id", ""))
            # Match on the sink THIS bridge is feeding, not on a hard-coded name - there are
            # now two of them, and healing the other phone's source would be worse than doing
            # nothing. `device` is what pyvirtualcam reports it actually opened.
            if device.split(" #")[0] not in dev:
                continue
            if name and inp["inputName"] != name:
                continue
            # WARNING: A FULL DEVICE RE-OPEN, EVERY TIME - AND A SCENE-ITEM TOGGLE IS NOT ONE.
            # If OBS opened this source while the virtual camera had no producer (which is
            # normal: the task starts the bridge 90 s after logon, and OBS is often up first)
            # the source stays BLACK even once frames arrive. Disabling and re-enabling the
            # scene item does not clear it - measured, still black - because the underlying
            # device was never closed. Clearing video_device_id closes it; restoring opens it
            # fresh. The property cascade matters: device first, THEN res_type, then
            # resolution, or the resolution is applied to a device that is not open yet.
            time.sleep(0.5)
            c.set_input_settings(inp["inputName"], {"video_device_id": ""}, True)
            time.sleep(2)
            c.set_input_settings(inp["inputName"], {"video_device_id": dev}, True)
            time.sleep(2)
            c.set_input_settings(inp["inputName"], {"res_type": 1, "resolution": want}, True)
            return f"'{inp['inputName']}' re-opened at {want}"
        return f"no OBS source bound to '{device}'"
    # WARNING: SystemExit IS NOT AN Exception. obsctl.connect() calls sys.exit() when OBS is
    # not running, and sys.exit raises SystemExit, which inherits from BaseException - it goes
    # straight through `except Exception` and unwinds the thread with a traceback. OBS being
    # closed is a NORMAL state for this process: the bridge exists to be running before OBS
    # is. Third time this has bitten in this project.
    except (Exception, SystemExit) as e:
        return f"OBS not updated ({type(e).__name__})"


ADB = r"C:\Users\mccul\Android\Sdk\platform-tools\adb.exe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def adb_forward(serial, url, remote=8090):
    """Point a local port at the phone's server over USB. Never raises."""
    m = re.search(r"://127\.0\.0\.1:(\d+)", url)
    if not m:
        return "url is not a localhost forward; nothing to do"
    local = m.group(1)
    try:
        r = subprocess.run([ADB, "-s", serial, "forward",
                            f"tcp:{local}", f"tcp:{remote}"],
                           capture_output=True, text=True, timeout=20,
                           creationflags=NO_WINDOW)
        return f"tcp:{local} -> tcp:{remote}" if r.returncode == 0 else r.stderr.strip()[:60]
    except Exception as e:
        return f"failed ({type(e).__name__})"


def adb_state(serial):
    """"device", "unauthorized", "offline", or "" when adb has never heard of it.

    Three-valued on purpose, because "unauthorized" is the one the person can fix: the
    cable is in and the phone is awake, it just has an un-tapped "Allow USB debugging?"
    dialog on screen. Collapsing that into "no USB" would send someone to check a cable
    that is already fine.
    """
    try:
        r = subprocess.run([ADB, "-s", serial, "get-state"], capture_output=True,
                           text=True, timeout=10, creationflags=NO_WINDOW)
        out = (r.stdout or "").strip()
        if out:
            return out
        err = (r.stderr or "").lower()
        return "unauthorized" if "unauthorized" in err else ""
    except Exception:
        return ""


_serial_cache = {}          # host -> (serial_or_None, looked_at)
_SERIAL_TTL = 30.0


def adb_devices():
    """[(serial, state)] from `adb devices`. Never raises; [] when adb is absent."""
    try:
        r = subprocess.run([ADB, "devices"], capture_output=True, text=True,
                           timeout=10, creationflags=NO_WINDOW)
    except Exception:
        return []
    out = []
    for line in (r.stdout or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and not parts[0].startswith("emulator-"):
            out.append((parts[0], parts[1]))
    return out


def phone_wifi_ip(serial):
    """The phone's own wlan0 address, asked over the cable. "" if it cannot be read."""
    try:
        r = subprocess.run([ADB, "-s", serial, "shell", "ip", "-f", "inet", "addr",
                            "show", "wlan0"], capture_output=True, text=True,
                           timeout=10, creationflags=NO_WINDOW)
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", r.stdout or "")
        return m.group(1) if m else ""
    except Exception:
        return ""


def find_usb_serial(host):
    """Serial of an attached phone whose own WiFi address is `host`, or None.

    ⚠️ MATCHED BY IP, NOT CONFIGURED. Asking each attached phone what its own wlan0
    address is, and comparing that to the host this bridge already streams from, makes
    plugging the cable in the entire configuration - no serial in the scheduled task, no
    second place to keep in sync, and two phones on two bridges sort themselves out
    because each matches only its own address.

    The alternative was hardcoding a serial per task, which is also where this would have
    gone wrong quietly: a task edited for one phone and then copied for the other would
    have both bridges tunnelling to the same handset, and the picture would look fine.

    Cached briefly. The reconnect loop can spin every five seconds while a phone is
    asleep, and that is no reason to shell out to adb twice a second.
    """
    now = time.time()
    hit = _serial_cache.get(host)
    if hit and now - hit[1] < _SERIAL_TTL:
        return hit[0]
    found = None
    for serial, state in adb_devices():
        if state != "device":
            continue                      # unauthorized/offline: cannot be asked
        if phone_wifi_ip(serial) == host:
            found = serial
            break
    _serial_cache[host] = (found, now)
    return found


def host_of(url):
    m = re.match(r"^https?://([^:/]+)", url)
    return m.group(1) if m else ""


def to_usb(url, port):
    """Point a phone URL at the local end of an adb forward, keeping scheme and path."""
    m = re.match(r"^(https?://)[^/]+(/.*)?$", url)
    return url if not m else "%s127.0.0.1:%d%s" % (m.group(1), port, m.group(2) or "")


def pick_transport(serial, url, api, port):
    """Prefer USB when the cable is actually usable, and fall back to WiFi otherwise.

    ⚠️ DECIDED EVERY RECONNECT PASS, NOT ONCE AT STARTUP. The cable gets plugged in
    mid-session and pulled out mid-session, and a bridge that chose its transport once
    would keep trying a tunnel that no longer exists - which presents as the phone being
    off, because a dead forward refuses the connection exactly like an absent phone.

    ⚠️ AND THE CHOICE IS ANNOUNCED. USB is not obviously faster here - the measurement in
    the header puts camera-to-decode at 80 ms over WiFi, with the remaining ~710 ms
    downstream in OBS - so the reason to prefer it is stability, not speed. That makes a
    silent fallback to WiFi genuinely costly: the picture keeps working and the thing you
    switched to USB to avoid is quietly back. Whichever is in use gets printed.
    """
    if not serial:
        # Nothing configured, so go looking. This is the path that makes "plug it in and
        # it switches" true without anyone editing a scheduled task.
        serial = find_usb_serial(host_of(url))
        if not serial:
            pending = [s for s, st in adb_devices() if st == "unauthorized"]
            if pending:
                # Deliberately hedged. An unauthorized device cannot be asked for its
                # IP, so we genuinely do not know whether it is this bridge's phone -
                # and naming it as though we did would send someone to the wrong handset.
                return url, api, ("WiFi (a phone is on USB with an unanswered "
                                  '"Allow USB debugging?" prompt - cannot tell if it is '
                                  "this one until that is tapped)"), None
            return url, api, "WiFi (no USB)", None
    state = adb_state(serial)
    if state == "device":
        u, a = to_usb(url, port), to_usb(api, port)
        fwd = adb_forward(serial, u)
        if "->" in fwd:
            return u, a, "USB", serial
        return url, api, "WiFi (adb forward failed: %s)" % fwd, None
    if state == "unauthorized":
        return url, api, 'WiFi (USB cable is in, but "Allow USB debugging?" is unanswered on the phone)', None
    if state:
        return url, api, "WiFi (adb says %s)" % state, None
    return url, api, "WiFi (no USB)", None


def unlocked_functions(dumpsys_text):
    """Parse `screen_unlocked_functions` out of `dumpsys usb`. Pure, so it can be tested
    without a phone attached - which matters, see the warning in no_webcam_handover."""
    m = re.search(r"screen_unlocked_functions=(\S+)", dumpsys_text or "")
    return (m.group(1) if m else "0").strip()


def no_webcam_handover(serial):
    """Stop the phone switching USB to webcam mode, which steals the camera from RigCam.

    WARNING: THIS IS THE FAILURE THAT LOOKS LIKE A DEAD CAMERA AND ISN'T. Android's
    `screen_unlocked_functions` is the "Default USB configuration" developer option. Set to
    0x80 (UVC) it flips USB into webcam mode every time the screen unlocks, which starts
    com.android.DeviceAsWebcam - a PRIVILEGED system app. RigCam then loses the camera to it
    the moment it is backgrounded:

        EVICT device 0 client held by online.awen.rigcam
          - Evicted by com.android.DeviceAsWebcam

    and reports `streaming: true` with an empty lastError while nalsOut stops advancing. A
    silent freeze, with the phone insisting it is fine.

    WARNING: ONLY EVER CLEAR THIS, NEVER SET IT. `svc usb setScreenUnlockedFunctions <value>`
    renegotiates the USB gadget and DROPPED THE PHONE OFF USB ENTIRELY - gone from adb and
    from Windows device enumeration, recoverable only by rebooting the handset. Clearing it
    (blank argument) was measured NOT to disturb the live connection. That asymmetry is the
    whole reason this function takes no value argument: there is no safe reason for this
    program to ever set one.

    WARNING: AND DO NOT "FIX" THE HANDOVER BY DISABLING DeviceAsWebcam. `pm disable-user` on
    it took USB data down the same way, because the gadget still pointed at a webcam function
    whose provider had just been removed.

    Checked every reconnect because it is a user-facing toggle: one tap in developer options
    puts it back, and it may not survive a reboot.
    """
    try:
        d = subprocess.run([ADB, "-s", serial, "shell", "dumpsys", "usb"],
                           capture_output=True, text=True, timeout=20,
                           creationflags=NO_WINDOW).stdout
        cur = unlocked_functions(d)
        if cur in ("0", "0x0", ""):
            return "ok (no webcam handover)"
        subprocess.run([ADB, "-s", serial, "shell", "svc", "usb",
                        "setScreenUnlockedFunctions"],
                       capture_output=True, text=True, timeout=20,
                       creationflags=NO_WINDOW)
        return f"cleared screen_unlocked_functions={cur} (was handing the camera away)"
    except Exception as e:
        return f"not checked ({type(e).__name__})"


def probe_size(api):
    """Ask the phone what it is sending, so we do not guess and letterbox.

    ⚠️ A DORMANT CAMERA REPORTS `0x0`, AND `(0, 0)` IS A TRUTHY TUPLE. That is how a
    reconnect landing during `/api/sleep` got all the way through to ffmpeg: the caller's
    `if not size` guard passed, the bridge opened a session at 0x0, and `frame_bytes` came
    out zero - so `readinto` returned instantly forever and the log read
    `8761852 frames, 292053.0 fps` while OBS was handed nothing. A camera that is asleep is
    not a camera that is broken and not a camera that is ready; the only correct answer is
    "no size yet", which the existing retry loop already knows how to wait out.
    """
    try:
        with urllib.request.urlopen(api, timeout=4) as r:
            d = json.loads(r.read().decode())
        m = re.match(r"(\d+)x(\d+)", str(d.get("resolution", "")))
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            if w > 0 and h > 0:
                return w, h
    except Exception:
        pass
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://192.168.1.234:8090/stream.h264")
    ap.add_argument("--api", default="http://192.168.1.234:8090/api/state")
    ap.add_argument("--size", default=None, help="WxH; probed from the phone if omitted")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--backend", default="obs")
    ap.add_argument("--obs-source", default=None,
                    help="capture source to keep in step; found by device if omitted")
    ap.add_argument("--adb-serial", default=None,
                    help="phone serial; re-establishes the adb forward each reconnect")
    ap.add_argument("--usb-port", type=int, default=8190, metavar="PORT",
                    help="local port for the adb forward when the phone is on USB; the "
                         "stream and the state API share one tunnel")
    ap.add_argument("--no-obs-match", action="store_true",
                    help="do not touch OBS settings")
    # ⚠️ A scheduled task runs this with pythonw and NO console, so everything it prints -
    # including "phone not answering" and the frame counters that prove it is alive - goes
    # nowhere. That is how a stalled bridge looked identical to a healthy one and had to be
    # re-run by hand to find out which it was. Give the task a log.
    ap.add_argument("--log", default=None, metavar="PATH",
                    help="append output here as well as stdout (for the scheduled task)")
    # WARNING: THE FAILURE THIS EXISTS FOR. When the sink goes away under a running session
    # - OBS closed, the machine slept - `cam.send()` does not raise, it BLOCKS. The process
    # stays alive, the task stays "Running", the frame counter freezes, and the newest log
    # line is an hour old. Nothing restarts it because, as far as Windows is concerned,
    # nothing has failed. Exiting is the honest move: the task retries every minute, so a
    # stall then heals itself in under one.
    ap.add_argument("--stall-exit", type=float, default=45, metavar="SEC",
                    help="exit if no frame is sent for this long while a session is live "
                         "(the scheduled task then restarts us); 0 disables")
    args = ap.parse_args()

    # Shared with the watchdog thread. `at` is when a frame last reached the sink, and
    # `live` says a session is supposed to be running - so waiting for a phone that is off
    # is never mistaken for a stall.
    beat = {"at": time.time(), "live": False, "frames": 0}

    if args.log:
        os.makedirs(os.path.dirname(os.path.abspath(args.log)), exist_ok=True)
        class _Tee:
            """Timestamped, line-buffered, and it never lets a logging failure kill the
            bridge: the show does not stop because a disk is full."""
            def __init__(self, stream, path):
                self.stream, self.path = stream, path
            def write(self, text):
                try:
                    if self.stream: self.stream.write(text)
                except Exception:
                    pass
                try:
                    with open(self.path, "a", encoding="utf-8") as f:
                        for line in text.splitlines(True):
                            if line.strip():
                                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line)
                            else:
                                f.write(line)
                except Exception:
                    pass
            def flush(self):
                try:
                    if self.stream: self.stream.flush()
                except Exception:
                    pass
        sys.stdout = _Tee(sys.stdout, args.log)
        sys.stderr = _Tee(sys.stderr, args.log)

    # WARNING: THE PIXEL FORMAT IS DICTATED BY THE SINK, NOT BY PREFERENCE. The OBS Virtual
    # Camera takes NV12, which is why this tool uses it: 1.5 bytes per pixel instead of 3,
    # half the memory traffic, and no colour conversion in ffmpeg. Unity Capture does NOT
    # offer NV12 - `ffmpeg -list_options` shows bgr24 and nothing else - and sending NV12 to
    # it does not fail, it produces a BLACK source with no error anywhere. bgr24 costs
    # 83 MB/s at 720p30 against 41, affordable at 720p; at 1080p watch the CPU, because a
    # pipeline at capacity queues rather than drops and the queue IS the latency.
    if args.stall_exit > 0:
        def _watchdog():
            while True:
                time.sleep(5)
                if beat["live"] and time.time() - beat["at"] > args.stall_exit:
                    print("    STALLED: no frame for %.0fs at %d frames - exiting so the "
                          "task restarts us" % (args.stall_exit, beat["frames"]), flush=True)
                    # os._exit, not sys.exit: the main thread is blocked inside the sink and
                    # will never unwind. A clean shutdown that cannot happen is not clean.
                    os._exit(3)
        threading.Thread(target=_watchdog, daemon=True).start()

    if args.backend == "unitycapture":
        pix, bpp, fmt = "bgr24", 6, pyvirtualcam.PixelFormat.BGR
    else:
        pix, bpp, fmt = "nv12", 3, pyvirtualcam.PixelFormat.NV12

    ff = shutil.which("ffmpeg") or r"C:\Users\mccul\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe"

    # ⚠️ SELF-HEALING, and it is not optional. The first version probed the size once,
    # opened one ffmpeg, and exited when the stream ended - CLEANLY, with status 0. So when
    # the phone changed resolution (which makes RigCam drop its clients ON PURPOSE, so they
    # re-probe), the bridge quit, the scheduled task saw a success and never restarted it,
    # and the WiFi camera went dark with no error anywhere. A component the show depends on
    # must reconnect by itself.
    fixed = None
    if args.size:
        fixed = tuple(int(x) for x in args.size.lower().split("x"))

    session = 0
    while True:
        # ⚠️ RE-ESTABLISH THE ADB FORWARD EVERY PASS, not once at startup. A forward belongs to
        # an adb connection: it dies when the cable is touched, when the phone reboots, and
        # when the adb server restarts - and once it is gone the URL simply refuses, which
        # looks exactly like the phone being off. `adb forward` is idempotent and cheap, so
        # the reconnect loop that already exists for the stream heals the tunnel too.
        url, api, how, usb_serial = pick_transport(args.adb_serial, args.url, args.api,
                                                   args.usb_port)
        # ⚠️ ALWAYS PRINT THE REASON, not only when a serial was configured. The whole
        # point of auto-discovery is that nobody configures anything - so gating the
        # explanation on a flag nobody sets means the one line that says WHY it is on
        # WiFi, and what to tap to change that, never appears. The session line below
        # shows the transport; this shows the reason it was chosen.
        print(f"    transport:   {how}", flush=True)
        if how == "USB":
            # Stop the phone handing itself over to UVC webcam mode when the cable goes
            # in - that would take RigCam's HTTP server down with it, and the dashboard's
            # whole Phones panel reads that server.
            print(f"    usb mode:    {no_webcam_handover(usb_serial)}", flush=True)
        size = fixed or probe_size(api)
        if not size:
            # Covers both cases honestly: the phone may be unreachable, or reachable and
            # deliberately asleep. Neither is a fault, and both are cured by waiting.
            print("no picture yet (phone unreachable, or camera asleep); retrying in 5s",
                  flush=True)
            time.sleep(5)
            continue
        w, h = size
        session += 1
        print(f"[{session}] {w}x{h} from {url} over {how.split(' (')[0]}", flush=True)


        # ⚠️ Every flag here is about not accumulating frames. `-fflags nobuffer` and
        # `-flags low_delay` stop the demuxer and decoder holding frames back. Do NOT add
        # `-probesize 32 -analyzeduration 0`: they leave ffmpeg unable to estimate the frame
        # rate and buy nothing, because the latency this tool targets is downstream in OBS.
        # ⚠️ -nostdin IS NOT OPTIONAL HERE. ffmpeg reads stdin for keyboard commands and treats
        # EOF as quit. This process is spawned without a console - by the scheduled task, and
        # by anything that runs it non-interactively - so its stdin is a closed or null handle
        # and ffmpeg can exit the moment it reads one. It exits CLEANLY and SILENTLY: status 0,
        # nothing on stderr, and the bridge simply reported "stream ended" a second after
        # starting, over and over, which reads as a network fault rather than a self-inflicted
        # one. stdin is pinned to DEVNULL as well so the handle is never inherited at all.
        cmd = [ff, "-hide_banner", "-nostdin", "-loglevel", "error",
               "-fflags", "nobuffer", "-flags", "low_delay",
               "-f", "h264", "-i", url,
               # ⚠️ NV12, NOT rgb24. RGB is 3 bytes per pixel; at 1080p30 that is 6.2 MB a
               # frame and 186 MB/s through a Python loop, which pegged the CPU and stalled
               # the pipe - TCP then back-pressured the phone and its frames were dropped
               # mid-GOP, which is what "pixelating and breaking" actually was. NV12 is
               # 1.5 bytes per pixel: 93 MB/s, half the work, and it is what the virtual
               # camera wants anyway, so ffmpeg skips a colour conversion too.
               "-pix_fmt", pix, "-f", "rawvideo", "-fps_mode", "passthrough", "-"]
        # ⚠️ NO `-vf scale`. The size comes from probing the phone, so the filter was always
        # scaling WxH to WxH - swscale still runs, still touches every pixel, and at 1080p30
        # that cost enough to push the bridge to ~80% of a core. A pipeline running at
        # capacity does not drop frames, it QUEUES them: measured 1494 ms at 1080p against
        # 428 ms at 720p. The latency was the backlog in front of a decoder that could not
        # quite keep up.
        # ⚠️ CREATE_NO_WINDOW. The bridge runs under pythonw, which has no console of its
        # own, so every console child Windows starts gets a BRAND NEW terminal window - and
        # with Windows Terminal as the default host that is a visible empty black window,
        # not a flicker. ffmpeg is respawned on every stream end (a few times an hour, more
        # when the phone hiccups), so the desktop slowly filled with empty terminals titled
        # ffmpeg.EXE. Every other subprocess in this rig already carries this flag; this one
        # was missed because it is a Popen rather than a run().
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0,
                                creationflags=NO_WINDOW)
        frame_bytes = w * h * bpp // 2
        # ⚠️ ONE BUFFER, REUSED, FILLED IN PLACE. The previous loop accumulated a list of
        # chunks and joined them, so every frame was copied two or three times: at 1080p30
        # that is ~3.1 MB a frame and roughly 280 MB/s of pointless memcpy, which pegged the
        # bridge at ~98% of a core. A pipeline at capacity queues rather than drops, and the
        # queue IS the latency - measured 1494 ms at 1080p against 428 ms at 720p.
        buf = bytearray(frame_bytes)
        view = memoryview(buf)
        # WARNING: THE SHAPE MATTERS, NOT JUST THE BYTE COUNT. pyvirtualcam takes NV12 as a
        # flat buffer but BGR as (h, w, 3), and passing the flat one raises
        # "unexpected frame shape: (2764800,) != (720, 1280, 3)" on every single frame - the
        # session dies and reconnects forever. reshape() on a frombuffer view is free; it is
        # still a view onto the same buffer, so the readinto-into-one-buffer property holds.
        arr = np.frombuffer(buf, dtype=np.uint8)   # a view, not a copy
        if pix == "bgr24":
            arr = arr.reshape(h, w, 3)
        shown = 0
        t0 = time.time()
        last_report = t0

        try:
            with pyvirtualcam.Camera(width=w, height=h, fps=args.fps,
                                     backend=args.backend,
                                     fmt=fmt) as cam:
                print(f"    feeding '{cam.device}'", flush=True)
                beat["at"] = time.time(); beat["live"] = True
                while True:
                    # ⚠️ A pipe read returns what is AVAILABLE, not what you asked for. The
                    # first short read once looked exactly like the stream ending.
                    got = 0
                    while got < frame_bytes:
                        n = proc.stdout.readinto(view[got:])
                        if not n:
                            break
                        got += n
                    if got < frame_bytes:
                        err = proc.stderr.read(300).decode("utf-8", "replace").strip()
                        print(f"    stream ended{': ' + err if err else ''}", flush=True)
                        break
                    # No sleep_until_next_frame(): pacing to a nominal rate would
                    # reintroduce exactly the queue this tool exists to remove.
                    cam.send(arr)
                    shown += 1
                    beat["at"] = time.time(); beat["frames"] = shown
                    # ⚠️ RE-OPEN THE OBS SOURCE ONLY ONCE FRAMES ARE ACTUALLY FLOWING. Doing it
                    # at session start re-opens a device that still has no producer, which is
                    # the very state that leaves the source black - measured, black either
                    # way. 30 frames is about a second of real video, so by here the sink has
                    # been fed and a fresh open finds a live device. On a thread because the
                    # re-open sleeps several seconds and the feed must not stall behind it.
                    if shown == 30 and not args.no_obs_match:
                        threading.Thread(
                            target=lambda: print(f"    OBS source: "
                                                 f"{match_obs_source(w, h, cam.device, args.obs_source)}",
                                                 flush=True),
                            daemon=True).start()
                    now = time.time()
                    if now - last_report >= 30:
                        print(f"    {shown} frames, {shown / (now - t0):.1f} fps", flush=True)
                        last_report = now
        except KeyboardInterrupt:
            beat["live"] = False
            proc.terminate()
            print("stopping")
            return
        except Exception as e:
            print(f"    session failed: {type(e).__name__}: {e}", flush=True)
        finally:
            beat["live"] = False
            try:
                proc.terminate()
            except Exception:
                pass

        # Re-probe on the next pass: the resolution may be exactly why this one ended.
        time.sleep(2)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
