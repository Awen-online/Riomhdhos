# Video

Phone cameras into OBS, on a machine that is **not** the audio rig.

⚠️ **Nothing here is wired to the show yet.** `obsctl.py` works and is tested; the
REAPER→OBS bridge that would make moods drive scenes is not written.

## Why this lives in the Riomhdhos repo

The point of the video half is that **the moods drive it** — selecting THE DEEP on the
Push should change the projection. That bridge touches the JSFX/gmem side and the OBS side
at once, so a separate repo would mean no atomic change across it and two clones to keep in
step at a venue. Video is not a neighbouring project; it is the same instrument.

## Why video never runs on Ríomhdhos

Measured 2026-08-16, at a matched ~1.4 MB/s into the rig: **Ethernet ingest adds +12% DPC,
USB tethering adds +107%.** At 128 samples the audio graph has 2.9 ms to finish, every
2.9 ms, and a driver DPC that blocks for 3 ms is an audible click in the PA.

The link quality runs the *other* way — USB tether measured 0% loss and 2 ms p99 against
WiFi's 3% loss and 108 ms p99 — but a fat receive buffer is cheap and an audio dropout in
front of an audience is not. So: cameras wired to a video machine, rig left alone.

The other reason is not a number. **RDP hangs REAPER on that box**, so the one action you
would take to fix a video problem mid-show is also the one that kills the audio.

## obsctl.py

```
python obsctl.py status
python obsctl.py devices --device "Android Webcam"
python obsctl.py build-cam --device "Android Webcam" --scene CAM --replace
python obsctl.py cycle "Webcam" --scene Pixel6
python obsctl.py shot --scene Pixel6 --out frame.png
```

Reads OBS's own generated password from `%APPDATA%\obs-studio\plugin_config\obs-websocket\`,
so no credential is ever typed on a command line. Requires **Tools → WebSocket Server
Settings → Enable** once; that cannot be done by editing `config.json` while OBS is
running, because OBS rewrites it on exit — the same trap as `reaper.ini`.

## Three things that cost an hour each to find

**A DirectShow device has exactly one consumer.** A browser tab, a stray ffmpeg, or a
second OBS source on the same camera silently wins it and everything else gets a source
that claims the device and shows nothing. Two sources on one camera is the version of this
that looks most like a bug and is hardest to spot.

**The dshow property lists cascade, and all three steps are needed:**

```
video_device_id  ->  the resolution list populates
res_type = 1     ->  custom mode, so a format is even a choice
resolution       ->  the FORMAT list populates, filtered to that mode
```

Ask earlier and you get only `Any`, which is not a format but the absence of one. Measured
on the Pixel 6: at 1920x1080 and 1280x720 the **only** format offered is MJPEG (enum 400);
YUY2 appears only at 640x480 and below, because uncompressed 1080p is ~2 Gbit/s and no UVC
link carries it. So `Any` at 1080p means the driver picks something that cannot stream.

**Settings alone do nothing to a running source.** dshow negotiates its mode when it opens
the device; writing settings afterwards updates stored values and leaves the live capture
untouched. The source sits at **0x0**, having never received a frame. `build-cam` cycles
activation for this reason, and `cycle` exists as a standalone recovery — it is the first
thing to try when a camera shows nothing.

Source size is the only honest test. A source *existing* proves nothing; `0x0` means no
frame has ever arrived.

## The phone as a camera

The Pixel 6 runs GrapheneOS, which ships `com.android.DeviceAsWebcam`. Switching USB mode
to **Webcam** makes it a standard UVC device — no app, no network, no streaming stack.

⚠️ USB mode does **not** survive unplugging; Android returns to charging/ADB. `svc usb
setFunctions uvc` over ADB sets it back, and ADB keeps working alongside webcam mode, so
this is automatable from the host.

Because `DeviceAsWebcam` is a system app it uses Google's own camera pipeline, so the image
is better than a third-party Camera2 app would get. The tradeoff is that exposure and white
balance are automatic — the argument for a custom app is *locked* settings matched across
two cameras, not image quality. Test whether auto-exposure actually drifts under fixed
stage lighting before assuming the custom app is needed.
