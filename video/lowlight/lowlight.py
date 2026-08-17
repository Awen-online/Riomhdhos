#!/usr/bin/env python3
"""
lowlight - low-light enhancement on a camera, using the known-good preset.

The rig shoots in dim rooms, so this is the filter with the most leverage on how the
picture actually looks. It is the plugin's `enhanceportrait` filter kind - NOT
`background_removal`, despite both shipping in obs-backgroundremoval and both being
selected by a `model_select` path.

WHICH MODEL, and why (all four measured on the Pixel 6 in the real room):

  semantic_guided_llie_180x324   7.7 ms   RECOMMENDED. Cheapest and holds contrast at
                                          high blend - it lifts shadows without lifting
                                          blacks, so the room stays moody.
  zero_dce_180x320               7.7 ms   Similar cost, but goes hazy above ~0.2 blend.
  tbefn_fp32                     9.3 ms   Blows the image to near-WHITE at blend 1.0 and
                                          goes milky by 0.3. Usable only around 0.15.
                                          It is the plugin's DEFAULT, which is a trap.
  uretinex_net_180x320          15.4 ms   Best-looking of the four by a clear margin -
                                          natural skin tone, real shadow recovery - but
                                          it cannot hold 60 fps. See the fps note below.

⚠️ BLEND DOES NOT BUY BACK PERFORMANCE. Measured: render time is the same at blend 0.0 as
at 1.0. The model runs on every frame regardless; blend only mixes how much of its output
is used. So "turn it down to save GPU" does not work - the only way to save the cost is to
disable the filter.

⚠️ Do NOT change model_select during a live stream. That reloads and reallocates the
model, which is the same path that crashed OBS with an access violation in
obs_source_skip_video_filter. Changing `blend` is safe; swapping models is not.

    python lowlight.py                     # apply the preset
    python lowlight.py --blend 0.3         # gentler
    python lowlight.py --model uretinex_net_180x320.onnx --blend 0.5
    python lowlight.py --off / --on        # toggle (safe live)
"""

import argparse
import os
import sys
import time

# Import rather than restate: obsctl owns the credential handling and the logger pinning
# that stops obsws-python printing the websocket password at INFO.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import obsctl  # noqa: E402

FILTER_NAME = "Low light"

# Measured cost of the low-light filter ALONE, on top of the depth-blur filter that is
# already on this source. The blur costs ~6.9 ms by itself.
COSTS_MS = {
    "semantic_guided_llie_180x324.onnx": 7.7,
    "zero_dce_180x320.onnx": 7.7,
    "tbefn_fp32.onnx": 9.3,
    "uretinex_net_180x320.onnx": 15.4,
}
BLUR_MS = 6.9


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="Webcam")
    ap.add_argument("--name", default=FILTER_NAME)
    ap.add_argument("--model", default="semantic_guided_llie_180x324.onnx",
                    help="semantic_guided is cheapest AND holds contrast; tbefn is the "
                         "plugin default and blows out")
    ap.add_argument("--blend", type=float, default=0.45,
                    help="0.45 is visible without going milky. tbefn needs ~0.15.")
    ap.add_argument("--off", action="store_true", help="disable (safe live)")
    ap.add_argument("--on", action="store_true", help="enable (safe live)")
    args = ap.parse_args()

    cl = obsctl.connect(timeout=20)
    existing = [f["filterName"] for f in cl.get_source_filter_list(args.source).filters]

    if args.off or args.on:
        if args.name not in existing:
            sys.exit(f"no '{args.name}' filter on {args.source}")
        cl.set_source_filter_enabled(args.source, args.name, bool(args.on))
        print(f"{args.name}: {'enabled' if args.on else 'disabled'}")
        return

    settings = {
        "model_select": f"models/{args.model}",
        "blend": max(0.0, min(1.0, args.blend)),
        "useGPU": "dml",          # DirectML -> the Radeon, not the CPU
    }

    if args.name in existing:
        cur = cl.get_source_filter(args.source, args.name).filter_settings
        swapping = cur.get("model_select") != settings["model_select"]
        cl.set_source_filter_settings(args.source, args.name, settings, True)
        print(f"updated '{args.name}' on {args.source}")
        if swapping:
            print("  model swapped - leave OBS alone for ~14s while it reallocates")
    else:
        cl.create_source_filter(args.source, args.name, "enhanceportrait", settings)
        print(f"created '{args.name}' on {args.source}")

    cost = COSTS_MS.get(args.model)
    print(f"  model {args.model}")
    print(f"  blend {settings['blend']}")
    if cost:
        total = cost + BLUR_MS
        print(f"  budget: ~{total:.1f} ms/frame with the depth blur "
              f"({total/16.67*100:.0f}% at 60fps, {total/33.33*100:.0f}% at 30fps)")
        if total > 16.67:
            print("  ⚠️ over budget at 60 fps. The camera only delivers 30 fps, so setting")
            print("     OBS output to 30 fps doubles the budget and costs nothing real.")

    time.sleep(8)
    s = cl.get_stats()
    print(f"  measured: render={s.average_frame_render_time:.2f}ms fps={s.active_fps:.2f}")


if __name__ == "__main__":
    main()
