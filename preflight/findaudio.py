#!/usr/bin/env python3
"""
Find which Windows input device actually carries the rig.

WHY THIS EXISTS: the audio path from Ríomhdhos to this desktop was ASSUMED to be an analog
cable into the Focusrite, and that assumption was never tested. It shaped where the visuals
server listens and what OBS was told to capture. A machine with a Focusrite, seven
Voicemeeter buses, a webcam mic and an onboard codec has plenty of places for a signal to
be, and exactly one of them is right.

So: open every input in turn, measure it, and let the levels say which.

    python findaudio.py            # 1.5s per device
    python findaudio.py --secs 3   # longer, for sparse playing

⚠️ PLAY SOMETHING WHILE THIS RUNS. Every input reads silence on an idle rig, and that
result is indistinguishable from "nothing is connected".
"""

import argparse
import math

import numpy as np
import sounddevice as sd


def db(x):
    return 20 * math.log10(x) if x > 1e-9 else -120.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=1.5)
    ap.add_argument("--rate", type=int, default=44100)
    ap.add_argument("--all", action="store_true",
                    help="include the WDM/DirectSound duplicates of each device")
    args = ap.parse_args()

    devs = sd.query_devices()
    apis = sd.query_hostapis()
    rows = []
    seen_names = set()

    for i, d in enumerate(devs):
        if d["max_input_channels"] < 1:
            continue
        name = d["name"].strip()
        # Windows lists most devices several times, once per host API. Testing every copy
        # takes minutes and tells you nothing new, so keep the first of each name unless
        # asked otherwise.
        if not args.all and name in seen_names:
            continue
        seen_names.add(name)

        ch = min(2, d["max_input_channels"])
        try:
            rec = sd.rec(int(args.secs * args.rate), samplerate=args.rate,
                         channels=ch, device=i, dtype="float32")
            sd.wait()
            peak = float(np.max(np.abs(rec)))
            rms = float(np.sqrt(np.mean(rec ** 2)))

            # ⚠️ REJECT IMPOSSIBLE VALUES. Float32 audio is bounded by ±1.0, so anything
            # above that is not a loud signal - it is an uninitialised buffer. The
            # Voicemeeter virtual points return exactly that when Voicemeeter is not
            # running: opening them succeeds and hands back garbage, which this tool
            # first reported as "741 dBFS" and listed at the top as the strongest signal
            # on the machine. A scan whose loudest result is nonsense is worse than no
            # scan, because it points confidently at the wrong device.
            if not np.isfinite(peak) or peak > 1.5:
                rows.append((-999.0, -999.0, i, name, apis[d["hostapi"]]["name"],
                             "invalid samples (device not running?)"))
            else:
                rows.append((db(peak), db(rms), i, name, apis[d["hostapi"]]["name"], None))
        except Exception as e:
            rows.append((-999.0, -999.0, i, name, apis[d["hostapi"]]["name"], str(e)[:40]))

    rows.sort(key=lambda r: -r[0])
    print(f"\n{'peak':>8} {'rms':>8}  {'#':>3}  device")
    print("-" * 74)
    for pk, rm, idx, name, api, err in rows:
        if err:
            print(f"{'--':>8} {'--':>8}  {idx:>3}  {name[:44]:<44} ({err})")
        else:
            # -60 dBFS is the line between an idle preamp and something actually arriving.
            mark = "  <== SIGNAL" if pk > -60 else ""
            print(f"{pk:>8.1f} {rm:>8.1f}  {idx:>3}  {name[:44]:<44} [{api[:9]}]{mark}")

    live = [r for r in rows if r[0] > -60]
    print()
    if not live:
        print("Nothing above -60 dBFS on any input.")
        print("Either nothing was playing, or the rig is not reaching this machine at all.")
    else:
        print("Carrying signal:")
        for pk, rm, idx, name, api, _ in live:
            print(f"  device {idx}: {name}   peak {pk:.1f} dBFS")


if __name__ == "__main__":
    main()
