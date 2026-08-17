#!/usr/bin/env python3
"""
End-to-end test: synthetic OSC in one end, a real OBS scene change out the other.

--simulate proves the OSC and decision half. This proves the half that actually matters
on stage - that a mood change reaches obs-websocket and OBS acts on it. The two together
mean the only untested link is REAPER's OSC sender, which is a GUI step on the rig.

Creates its own distinctively-named scenes, restores whatever scene was live, and removes
what it made. It must leave the scene collection exactly as it found it: this runs against
the real OBS the show uses, not a scratch profile.
"""

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rigbridge                                            # noqa: E402
from pythonosc import udp_client                            # noqa: E402

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")

PREFIX = "__rigbridge_test_"
SCENES = {"COSMOS": PREFIX + "COSMOS", "ÉIRE": PREFIX + "EIRE", "THE DEEP": PREFIX + "DEEP"}
PORT = 8011

obs = rigbridge.ObsActions()
if not obs.connect():
    sys.exit("OBS is not reachable - start OBS and enable the websocket server")
cl = obs.client

original = cl.get_scene_list().current_program_scene_name
print(f"current scene before test: {original}")
made = []
ok = fail = 0


def check(label, cond):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {label}")
    else:
        fail += 1
        print(f"  FAIL  {label}")


try:
    for scene in SCENES.values():
        if scene not in [s["sceneName"] for s in cl.get_scene_list().scenes]:
            cl.create_scene(scene)
            made.append(scene)
    print(f"created test scenes: {', '.join(made)}")

    cfg = {
        "osc": {"listen_host": "127.0.0.1", "listen_port": PORT},
        "options": {"debounce_seconds": 0.0},
        "moods": {m: {"actions": [{"type": "scene", "scene": s}]} for m, s in SCENES.items()},
        "addresses": {},
    }
    bridge = rigbridge.Bridge(cfg, obs)
    server = bridge.serve("127.0.0.1", PORT)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.4)

    client = udp_client.SimpleUDPClient("127.0.0.1", PORT)
    for idx, name in [(2, "COSMOS"), (3, "THE CAIRN"), (4, "ÉIRE"), (5, "THE DEEP")]:
        client.send_message(f"/track/{idx}/name", name)
    time.sleep(0.3)

    print("\n1. COSMOS becomes the live mood")
    for idx in (2, 3, 4, 5):
        client.send_message(f"/track/{idx}/mute", 0.0 if idx == 2 else 1.0)
    time.sleep(1.2)
    check("OBS switched to the COSMOS scene",
          cl.get_scene_list().current_program_scene_name == SCENES["COSMOS"])

    print("\n2. switch to ÉIRE")
    client.send_message("/track/2/mute", 1.0)
    client.send_message("/track/4/mute", 0.0)
    time.sleep(1.2)
    check("OBS switched to the EIRE scene",
          cl.get_scene_list().current_program_scene_name == SCENES["ÉIRE"])

    print("\n3. switch to THE DEEP")
    client.send_message("/track/4/mute", 1.0)
    client.send_message("/track/5/mute", 0.0)
    time.sleep(1.2)
    check("OBS switched to the DEEP scene",
          cl.get_scene_list().current_program_scene_name == SCENES["THE DEEP"])

    print("\n4. a mood pointing at a scene that does not exist must not crash")
    bridge.obs.apply({"type": "scene", "scene": "__does_not_exist__"})
    check("survived a missing scene", True)

    server.shutdown()

finally:
    # Restore first, remove second: a scene cannot be removed while it is the program
    # scene, and leaving the show pointed at a test scene would be worse than any failure
    # this test could report.
    try:
        cl.set_current_program_scene(original)
        time.sleep(0.4)
        print(f"\nrestored scene: {original}")
    except Exception as exc:
        print(f"\n⚠️  could not restore {original}: {exc}")
    for scene in made:
        try:
            cl.remove_scene(scene)
        except Exception as exc:
            print(f"⚠️  could not remove {scene}: {exc}")
    if made:
        print(f"removed test scenes: {', '.join(made)}")

print(f"\n{ok} passed, {fail} failed")
sys.exit(0 if fail == 0 else 1)
