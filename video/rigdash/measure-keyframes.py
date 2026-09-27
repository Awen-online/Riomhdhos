"""Measure the real keyframe interval off the local relay, while a stream is running.

This settles a question no config file can answer. OBS is in Simple output mode, which
does not expose keyframe interval anywhere - not in basic.ini, not in the UI - so the only
way to know is to look at the bytes. It matters because -c copy sends byte-identical video
to every destination, so this single number has to satisfy all of them at once: YouTube,
Twitch, Facebook and Rumble all want 2 seconds, and Facebook documents a hard "do not
exceed 4".

Reads from MediaMTX's local RTMP, so it costs the platforms nothing and adds one more
reader to a stream that is already running.
"""
import json
import subprocess
import sys

URL = "rtmp://127.0.0.1:1935/live"
SECONDS = 12

cmd = [
    "ffprobe", "-v", "error",
    "-select_streams", "v:0",
    "-show_entries", "frame=pts_time,key_frame",
    "-of", "json",
    "-read_intervals", "%%+{}".format(SECONDS),
    URL,
]

print("reading %s for ~%ds ..." % (URL, SECONDS))
try:
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=SECONDS + 45)
except FileNotFoundError:
    sys.exit("ffprobe not found on PATH")
except subprocess.TimeoutExpired:
    sys.exit("ffprobe timed out - is OBS actually publishing to the relay?")

if out.returncode != 0:
    sys.exit("ffprobe failed (is anything streaming?):\n" + (out.stderr or "")[:400])

try:
    frames = json.loads(out.stdout or "{}").get("frames", [])
except Exception as e:
    sys.exit("could not parse ffprobe output: %s" % e)

keys = []
for f in frames:
    if str(f.get("key_frame")) == "1":
        t = f.get("pts_time")
        if t is not None:
            try:
                keys.append(float(t))
            except ValueError:
                pass

print("  frames seen      : %d" % len(frames))
print("  keyframes seen   : %d" % len(keys))
if len(keys) < 2:
    sys.exit("  not enough keyframes in the window - try a longer sample")

gaps = [round(b - a, 3) for a, b in zip(keys, keys[1:])]
avg = sum(gaps) / len(gaps)
print("  gaps (s)         : %s" % gaps)
print("  average interval : %.2f s" % avg)
print()
for name, limit, want in (("YouTube", 4.0, 2.0), ("Twitch", 4.0, 2.0),
                          ("Facebook", 4.0, 2.0), ("Rumble", 4.0, 2.0)):
    verdict = "OK" if avg <= limit else "OVER the documented maximum"
    near = "" if abs(avg - want) < 0.35 else "  (wants %.0fs)" % want
    print("  %-9s max %.0fs -> %s%s" % (name, limit, verdict, near))
