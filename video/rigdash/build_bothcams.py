import sys, time
sys.path.insert(0, r"C:\Users\mccul\Riomhdhos\video")
import obsctl

SCENE = "BOTH CAMS"
WIRED, WIFI = "Pixel 8", "Pixel 6 (WiFi)"

cl = obsctl.connect(timeout=25)
v = cl.get_video_settings()
CW, CH = v.base_width, v.base_height

if SCENE not in [s["sceneName"] for s in cl.get_scene_list().scenes]:
    cl.create_scene(SCENE)
    print(f"created scene {SCENE}")

def item(source, index):
    items = cl.get_scene_item_list(SCENE).scene_items
    hit = next((i for i in items if i["sourceName"] == source), None)
    if hit:
        iid = hit["sceneItemId"]
    else:
        iid = cl.create_scene_item(SCENE, source, True).scene_item_id
        print(f"  + {source}")
    cl.set_scene_item_index(SCENE, iid, index)
    cl.set_scene_item_enabled(SCENE, iid, True)
    return iid

# ⚠️ BOTH ITEMS MUST STAY ENABLED. A disabled scene item is never activated, so the source
# behind it goes cold - which is the whole reason this scene exists. Anything that "cuts"
# by hiding an item defeats the point.
wired = item(WIRED, 0)          # bottom: full frame
wifi = item(WIFI, 1)            # top: picture-in-picture

# Full frame for the wired camera.
cl.set_scene_item_transform(SCENE, wired, {
    "boundsType": "OBS_BOUNDS_SCALE_INNER", "boundsWidth": float(CW),
    "boundsHeight": float(CH), "positionX": 0.0, "positionY": 0.0,
    "alignment": 5})

# PiP for the WiFi camera: a third of the width, bottom-right, with a margin.
pw, ph = CW / 3.0, CH / 3.0
margin = CW * 0.025
cl.set_scene_item_transform(SCENE, wifi, {
    "boundsType": "OBS_BOUNDS_SCALE_INNER", "boundsWidth": pw, "boundsHeight": ph,
    "positionX": CW - pw - margin, "positionY": CH - ph - margin,
    "alignment": 5})

print(f"canvas {CW}x{CH}: {WIRED} full frame, {WIFI} PiP bottom-right")

cl.set_current_program_scene(SCENE)
time.sleep(8)
print("\nboth sources, with this scene live:")
for src in (WIRED, WIFI):
    t = obsctl._source_size(cl, SCENE, src)
    print(f"  {src:<16} {int(t[0])}x{int(t[1])}")
