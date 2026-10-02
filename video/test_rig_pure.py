#!/usr/bin/env python3
"""Pin the pure decision functions in the rig — the ones that classify and parse.

WHY THESE AND NOT OTHERS: of 153 Python functions across this rig, almost all talk to a
phone, a socket, OBS or the filesystem, and testing those means a rig. These few take data
in and return a verdict, and that is where every bug in this system has actually lived -
a wrong verdict is indistinguishable from a right one until something downstream is
visibly wrong, usually mid-set.

Each case below is a real failure this rig has had, or the guard that stops one coming
back. Nothing here is a hypothetical.

    python test_rig_pure.py
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "rigdash"))

import vcambridge as vc                                           # noqa: E402

FAILS = []


def check(label, got, want):
    if got == want:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s\n          got  %r\n          want %r" % (label, got, want))
        FAILS.append(label)


# ====================================================================== vcambridge
def test_vcambridge():
    print("\nvcambridge — transport classification")

    # ⚠️ The USB tunnel rewrites the phone's URL to the local end of an adb forward. Get
    # this wrong and the bridge opens the wrong port, which presents as "phone offline"
    # with the cable plainly in.
    check("to_usb keeps scheme and path",
          vc.to_usb("http://192.168.1.50:8090/stream.h264", 8190),
          "http://127.0.0.1:8190/stream.h264")
    check("to_usb handles a bare host with no path",
          vc.to_usb("http://192.168.1.50:8090", 8190), "http://127.0.0.1:8190")
    check("to_usb leaves a non-URL alone rather than mangling it",
          vc.to_usb("not a url", 8190), "not a url")
    check("to_usb preserves https",
          vc.to_usb("https://10.0.0.2:8090/api/state", 1234),
          "https://127.0.0.1:1234/api/state")

    # host_of feeds find_usb_serial: it is compared against the phone's own wlan0 address,
    # so a wrong answer attaches the bridge to the wrong handset.
    check("host_of strips scheme, port and path",
          vc.host_of("http://192.168.1.168:8090/stream.h264"), "192.168.1.168")
    check("host_of on rubbish is empty, not a crash", vc.host_of("nonsense"), "")


# ====================================================================== dash.py
def test_dash():
    import dash                                                   # noqa: E402
    print("\ndash.py — adb address ranking and handset dedupe")

    # The three shapes a device can arrive as. Ranking decides which row survives dedupe.
    check("a cable outranks host:port", dash._addr_rank("38021FDJH004KS") <
          dash._addr_rank("192.168.1.50:38533"), True)
    check("host:port outranks the mDNS name",
          dash._addr_rank("192.168.1.50:38533") <
          dash._addr_rank("adb-1A06-x._adb-tls-connect._tcp"), True)
    check("a missing serial does not raise", dash._addr_rank(None), 0)

    # ⚠️ THE BUG THIS EXISTS FOR: the Pixel 6 appeared twice - once as host:port and once
    # under adb's mDNS name - and the Phones card listed it as two phones with identical
    # battery readings.
    rows = [
        {"model": "Pixel 6", "serial": "adb-1A06-x._adb-tls-connect._tcp", "hwserial": "1A06"},
        {"model": "Pixel 6", "serial": "192.168.1.50:38533", "hwserial": "1A06"},
        {"model": "Pixel 8", "serial": "192.168.1.168:43141", "hwserial": "3802"},
    ]
    out = dash._dedupe_phones(rows)
    check("one row per handset", len(out), 2)
    check("the more direct address wins",
          [r["serial"] for r in out if r["hwserial"] == "1A06"], ["192.168.1.50:38533"])
    check("adb's ordering is preserved, so the list does not reshuffle",
          [r["model"] for r in out], ["Pixel 6", "Pixel 8"])

    # ⚠️ A phone whose getprop failed has no hwserial. It must keep its own row rather
    # than being silently merged into another handset's.
    nohw = [{"model": "Pixel 6", "serial": "192.168.1.50:1", "hwserial": None},
            {"model": "Pixel 8", "serial": "192.168.1.60:1", "hwserial": None}]
    check("rows with no hwserial are not merged together", len(dash._dedupe_phones(nohw)), 2)

    print("\ndash.py — bridge health")
    F = dash.BRIDGE_FRESH_S
    # ⚠️ ONLY A FRAME REPORT PROVES FRAMES. The retry line is the freshest log on the
    # machine while a phone is switched off, and the old denylist fell through to
    # "feeding" - the retry spam itself was mistaken for health.
    check("retry spam is 'no signal', never 'feeding'",
          dash._bridge_health("no picture yet (phone unreachable, or camera asleep)", 2,
                              "running"), "no signal")
    check("a frame report is feeding",
          dash._bridge_health("  1718 frames, 30.0 fps", 2, "running"), "feeding")
    check("a stall line is failing",
          dash._bridge_health("STALLED: no frame for 45s", 2, "running"), "failing")
    check("nothing written for three reporting intervals is stalled",
          dash._bridge_health("  1718 frames, 30.0 fps", F + 1, "running"), "stalled")
    # ⚠️ AND THE INVERSE: an unreadable task state must not read as stopped. A saturated
    # box made every poll queue, and both bridges showed Stopped while feeding at 25 fps.
    check("an unknown task state is 'unknown', not 'stopped'",
          dash._bridge_health("  1718 frames, 30.0 fps", 2, "unknown"), "unknown")
    check("a task that is not running is stopped",
          dash._bridge_health("  1718 frames, 30.0 fps", 2, "ready"), "stopped")
    check("no age at all is stalled, not feeding",
          dash._bridge_health("  1718 frames, 30.0 fps", None, "running"), "stalled")

    print("\ndash.py — chat health (the two-clock model)")
    now = time.time()
    check("a source quiet but proving life is live",
          dash.chat_health({"state": "live", "last_alive": now - 5}, now), "live")
    # ⚠️ TWO CLOCKS, DELIBERATELY: silence is not death, but no proof of life is.
    check("no proof of life beyond the limit is stalled",
          dash.chat_health({"state": "live", "last_alive": now - dash.CHAT_STALE_S - 1},
                           now), "stalled")
    for st in ("off", "failed", "connecting", "quota", "idle"):
        check("state %-10s passes through untouched" % st,
              dash.chat_health({"state": st, "last_alive": 0}, now), st)

    print("\ndash.py — measured frame rate")
    dash._fps_prev.clear()
    st = {"Pixel 8": {"encoder": {"nalsOut": 1000}}}
    # ⚠️ ONE SAMPLE PROVES NOTHING, and guessing from it is how a panel reports a
    # confident number for a camera running at half rate.
    check("the first poll reports nothing", dash.rigcams_fps(st), {"Pixel 8": None})
    dash._fps_prev["Pixel 8"] = (1000, time.monotonic() - 10.0)
    got = dash.rigcams_fps({"Pixel 8": {"encoder": {"nalsOut": 1300}}})["Pixel 8"]
    check("300 slices over 10 s reads as 30 fps", got, 30.0)
    # ⚠️ A RESTARTED RigCam RESETS THE COUNTER. A negative delta means a new process,
    # not a negative frame rate.
    dash._fps_prev["Pixel 8"] = (99999, time.monotonic() - 10.0)
    check("a counter reset reports nothing rather than a negative rate",
          dash.rigcams_fps({"Pixel 8": {"encoder": {"nalsOut": 5}}})["Pixel 8"], None)
    dash._fps_prev.clear()
    check("an offline phone reports nothing",
          dash.rigcams_fps({"Pixel 6": {"offline": True}})["Pixel 6"], None)
    check("a phone with no encoder block reports nothing",
          dash.rigcams_fps({"Pixel 6": {}})["Pixel 6"], None)


def main():
    test_vcambridge()
    test_dash()
    print()
    if FAILS:
        print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
