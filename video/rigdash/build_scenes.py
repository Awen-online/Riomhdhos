#!/usr/bin/env python3
"""
Build the video rig's scene graph. Idempotent - safe to re-run.

    CAM      the camera on its own
    ECHO 1   CAM, delayed and faded          }  nested scenes exist ONLY so each copy
    ECHO 2   CAM, delayed further and faded  }  can carry its OWN filters
    LIVE     CAM + both echoes + the shader overlay
    VISUALS  black + the shader overlay, full screen

⚠️ WHY THE NESTED SCENES: filters attach to a SOURCE, not to a scene item. Adding the
same camera to one scene three times gives three views of one source with one shared
filter chain - so all three would carry the same delay, which is no delay at all. A scene
is itself a source, so wrapping the camera in ECHO 1 and ECHO 2 creates two distinct
filterable objects that both show the same camera. That indirection IS the feature.

⚠️ The camera is still opened ONCE. Nesting scenes does not duplicate the device - which
matters, because a DirectShow camera has exactly one consumer and a second claim would
black out the first.

The echoes are scaled slightly larger and blended with SCREEN, which is what makes a
trail read as motion rather than as a ghost: each copy is a moment ago, slightly closer.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import obsctl  # noqa: E402

CAM_SOURCE = "Webcam"
OVERLAY = "Chladni"
CAM, LIVE, VISUALS = "CAM", "LIVE", "VISUALS"
ECHOES = [
    # (scene name, delay ms, opacity, hue shift, scale)
    ("ECHO 1", 120, 0.55, 12.0, 1.035),
    ("ECHO 2", 260, 0.32, 26.0, 1.075),
]


def ensure_scene(cl, name):
    if name not in [s["sceneName"] for s in cl.get_scene_list().scenes]:
        cl.create_scene(name)
        print(f"  + scene {name}")


def ensure_item(cl, scene, source, index=None):
    """Add source to scene if absent; return the scene item id."""
    items = cl.get_scene_item_list(scene).scene_items
    hit = next((i for i in items if i["sourceName"] == source), None)
    if hit is None:
        r = cl.create_scene_item(scene, source, True)
        print(f"  + {source} -> {scene}")
        iid = r.scene_item_id
    else:
        iid = hit["sceneItemId"]
    if index is not None:
        cl.set_scene_item_index(scene, iid, index)
    return iid


def ensure_filter(cl, source, name, kind, settings):
    have = [f["filterName"] for f in cl.get_source_filter_list(source).filters]
    if name not in have:
        cl.create_source_filter(source, name, kind, settings)
        print(f"  + filter {name} on {source}")
    else:
        # Settings only on an existing filter. Remove+create is the operation that takes
        # OBS's render thread down, and this script is meant to be safe to re-run.
        cl.set_source_filter_settings(source, name, settings, True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-overlay", action="store_true",
                    help="skip the shader overlay (if the dashboard is not running)")
    args = ap.parse_args()
    cl = obsctl.connect(timeout=20)

    sources = [i["inputName"] for i in cl.get_input_list().inputs]
    if CAM_SOURCE not in sources:
        sys.exit(f"no '{CAM_SOURCE}' source - create the camera first")
    have_overlay = OVERLAY in sources and not args.no_overlay

    print("scenes:")
    ensure_scene(cl, CAM)
    ensure_item(cl, CAM, CAM_SOURCE)

    for name, delay, opacity, hue, _scale in ECHOES:
        ensure_scene(cl, name)
        ensure_item(cl, name, CAM)
        # gpu_delay holds frames on the GPU, so the trail costs VRAM rather than CPU -
        # which is the right currency here, since the CPU is carrying realtime audio.
        ensure_filter(cl, name, "delay", "gpu_delay", {"delay_ms": delay})
        # Fade AND shift hue: a trail that is only faded reads as a smear, while a trail
        # that also drifts in colour reads as time.
        ensure_filter(cl, name, "fade", "color_filter_v2",
                      {"opacity": opacity, "hue_shift": hue, "saturation": -0.15})

    ensure_scene(cl, LIVE)
    v = cl.get_video_settings()
    order = [CAM] + [e[0] for e in ECHOES] + ([OVERLAY] if have_overlay else [])
    for idx, src in enumerate(order):
        iid = ensure_item(cl, LIVE, src, index=idx)
        scale = next((e[4] for e in ECHOES if e[0] == src), 1.0)
        if scale != 1.0:
            # Centred zoom: offset by half the overspill so the copy grows about the middle
            # rather than out of the top-left corner.
            cl.set_scene_item_transform(LIVE, iid, {
                "scaleX": scale, "scaleY": scale,
                "positionX": -v.base_width * (scale - 1) / 2,
                "positionY": -v.base_height * (scale - 1) / 2,
            })
        if src in [e[0] for e in ECHOES]:
            cl.set_scene_item_blend_mode(LIVE, iid, "OBS_BLEND_SCREEN")

    ensure_scene(cl, VISUALS)
    if "VIS BG" not in sources:
        cl.create_input(VISUALS, "VIS BG", "color_source_v3",
                        {"color": 0xFF000000, "width": v.base_width,
                         "height": v.base_height}, True)
        print("  + VIS BG")
    ensure_item(cl, VISUALS, "VIS BG", index=0)
    if have_overlay:
        ensure_item(cl, VISUALS, OVERLAY, index=1)

    print("\nLIVE stack (bottom first):")
    for it in sorted(cl.get_scene_item_list(LIVE).scene_items,
                     key=lambda i: i["sceneItemIndex"]):
        bl = cl.get_scene_item_blend_mode(LIVE, it["sceneItemId"]).scene_item_blend_mode
        print(f"  idx{it['sceneItemIndex']}  {it['sourceName']:<12} "
              f"{bl.replace('OBS_BLEND_','').lower()}")


if __name__ == "__main__":
    # cp1252 is the console default here, and a stray non-ASCII character in a print has
    # now killed three separate tools AT THE MOMENT THEY HAD SOMETHING USEFUL TO SAY.
    # Degrade the character, never the process.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
