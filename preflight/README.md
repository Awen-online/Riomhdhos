# Preflight

```
python showcheck.py                 # everything
python showcheck.py --only rig obs  # subsets
python showcheck.py --json          # machine readable
```

Read-only. Changes nothing, so it is safe thirty seconds before a set.

## The principle: check function, never existence

Every check here corresponds to a failure that happened for real, and **every one of them
reports healthy while being broken.** That is the whole reason this file exists.

| Reports | Actually |
|---|---|
| ASIO device open, buffer "ok" | 512 samples — a driver default after a USB port change |
| Audio device open, correct driver | cable half-seated, no signal at all |
| Camera source exists in OBS | `0×0`, never received a frame |
| Camera configured, `res_type` custom | format `Any` — at 1080p the driver may pick a mode that cannot stream |
| Two OBS sources both configured | one silently owns the device; DirectShow allows exactly one consumer |
| OSC link carrying 800 msg/s | 100% noise, zero useful messages |
| Ollama responding | CPU-placed instance, ~11× slow |
| Ollama idle 5 minutes | next request stalls ~16 s reloading from disk |
| Phone plugged in, ADB works | USB reverted to charging on replug — not a camera |
| Recording configured | x264 on the CPU while streaming on the GPU |

"The device is open" is not "sound is coming out". "The source exists" is not "frames are
arriving". "Packets are flowing" is not "the right packets are flowing".

## ⚠️ Never trust a self-report when you can measure the effect

The phone-mode check got this wrong on its first pass and is worth preserving as the
example:

```
svc usb getFunctions  ->  ''                      phone: "I am not a webcam"
sys.usb.config        ->  'adb'
Windows PnP           ->  Camera: Android Webcam   host: "it demonstrably is"
```

When `DeviceAsWebcam` owns the USB gadget it bypasses the legacy USB function properties,
so the phone's own answer is simply wrong. The check now asks the **host** what it
enumerates. Same lesson as everything above, one layer further out — and a reminder that
a diagnostic can violate its own principle without anyone noticing.

## What it does not cover

- **Camera latency / audio sync** — needs `video/synctest/`, which needs the camera
  physically pointed at a screen. Not automatable.
- **Concurrent load.** Each subsystem is checked idle. Whether the LLM holds 53 tok/s
  while OBS encodes and a segmentation filter runs is the real live question and is
  unanswered.
- **`sourceWidth` is last-known, not live.** A camera that delivered frames and then
  vanished can still report its old size. Cross-read it against the phone's USB mode.
