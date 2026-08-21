#!/usr/bin/env python3
"""
rigbridge - let the audio rig drive the visuals.

    Push -> REAPER (RIOMHDHOS) -> OSC/UDP -> LAN -> [this] -> obs-websocket -> OBS

WHY THIS SHAPE: JSFX cannot do network and gmem is JSFX-only, so nothing in the mood
machinery can send a packet itself. What it CAN do is move REAPER state, and REAPER has
an OSC sender built in. So the rig publishes ordinary track state and this process infers
the show state from it. Nothing new has to be invented on the rig side.

⚠️ THE ACTIVE MOOD IS NOT SENT. There is no /mood address and there is no point wishing
for one. `mood_mute` holds every mood track muted except the active one and re-asserts it
every 30 ms, so the active mood is "the one mood track that is not muted". This process
derives it from /track/@/mute. That indirection is the whole trick.

⚠️ TRACKS ARE IDENTIFIED BY NAME, NEVER BY INDEX. REAPER addresses tracks by position,
but the repo has already been bitten once by code that assumed a layout: the health probe
looked for the JSFX on CTRL and reported them absent, because they had moved. Names
arrive on /track/@/name, so this builds the index->name map at runtime and re-derives
everything when it changes. Re-order the project and the bridge follows.

Latency is control-rate, not audio-rate: ~20-60 ms end to end, dominated by REAPER's OSC
update cycle rather than the wire. Correct for scene changes; wrong for anything that
should land on a beat.

  python rigbridge.py --simulate          prove it works with no rig attached
  python rigbridge.py                     run for real
  python rigbridge.py --dry-run           listen and log, touch nothing in OBS
"""

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata

try:
    from pythonosc import dispatcher as osc_dispatcher
    from pythonosc import osc_server, udp_client
except ImportError:
    sys.exit("python-osc is not installed:  python -m pip install python-osc")

try:
    import obsws_python as obsws
except ImportError:
    sys.exit("obsws-python is not installed:  python -m pip install obsws-python")

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "bridge.config.json")
OBS_CONFIG = os.path.join(
    os.environ.get("APPDATA", ""),
    "obs-studio", "plugin_config", "obs-websocket", "config.json",
)

log = logging.getLogger("rigbridge")

# ⚠️ obsws-python LOGS THE PASSWORD AT INFO LEVEL. Its connect path emits
#   "Connecting with parameters: host=... password=gTTdHyPwK3IWwjnW ..."
# which lands in the terminal, in any redirected log file, and in shell scrollback. The
# whole reason this reads the password out of OBS's own config instead of taking it as an
# argument was to keep it off the command line - and the library then prints it anyway.
# Capped at WARNING so its genuine failures still surface.
logging.getLogger("obsws_python").setLevel(logging.WARNING)
logging.getLogger("obsws_python.baseclient").setLevel(logging.WARNING)
logging.getLogger("obsws_python.reqs").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- naming

def norm(name):
    """Fold a track or mood name to something safe to compare.

    ⚠️ ACCENTS ARE STRIPPED ON PURPOSE. This rig has a mood called ÉIRE and that one
    name has now been mangled three separate ways in this project - 'Ã‰IRE' when a UTF-8
    file was read as Latin-1, 'A%IRE' through a terminal that could not render it, and
    plain 'EIRE' wherever someone typed it without the accent. Matching on the exact
    bytes means the projection silently fails to change on one mood in four, which is
    the kind of fault you discover on stage. Compare the accent-free uppercase form and
    all of those land on the same mood.
    """
    if name is None:
        return ""
    decomposed = unicodedata.normalize("NFD", str(name))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.upper().split())


# --------------------------------------------------------------------------- OBS side

class ObsActions:
    """Applies actions to OBS, and stays alive when OBS does not.

    ⚠️ Reconnects rather than exits. OBS crashing or being restarted mid-show must not
    take the bridge with it - the rig keeps playing either way, and a bridge that has to
    be restarted by hand is one more thing to remember at the worst moment.
    """

    def __init__(self, dry_run=False, host="localhost", port=None, password=None):
        self.dry_run = dry_run
        self.host = host
        self._port = port
        self._password = password
        self.client = None
        self._lock = threading.Lock()
        self._last_fail = 0.0
        self._scene_cache = ()
        self._scene_cache_at = 0.0

    def _credentials(self):
        """Read the password OBS generated for itself, so none is ever typed or stored
        here. Same approach as obsctl.py."""
        port, password = self._port, self._password
        if port is None or password is None:
            try:
                with open(OBS_CONFIG, "r", encoding="utf-8") as fh:
                    cfg = json.load(fh)
                port = port or cfg.get("server_port", 4455)
                if password is None:
                    password = cfg.get("server_password") if cfg.get("auth_required", True) else ""
            except OSError:
                port = port or 4455
                password = password or ""
        return port, password

    def connect(self, quiet=False):
        if self.dry_run:
            return True
        with self._lock:
            if self.client is not None:
                return True
            # Back off after a failure: a mood change every few seconds must not become a
            # connection storm while OBS is closed.
            if time.time() - self._last_fail < 5.0:
                return False
            port, password = self._credentials()
            try:
                self.client = obsws.ReqClient(host=self.host, port=port,
                                              password=password, timeout=4)
                v = self.client.get_version()
                log.info("connected to OBS %s (websocket %s)", v.obs_version,
                         v.obs_web_socket_version)
                self._scene_cache_at = 0.0
                return True
            except Exception as exc:
                self.client = None
                self._last_fail = time.time()
                if not quiet:
                    log.warning("OBS not reachable (%s) - will retry", exc)
                return False

    def _drop(self, exc):
        log.warning("OBS call failed (%s) - dropping connection, will reconnect", exc)
        with self._lock:
            self.client = None
        self._last_fail = time.time()

    def scenes(self):
        if self.dry_run or not self.connect(quiet=True):
            return ()
        # Cached briefly. Scene lists change rarely and every action would otherwise pay
        # a round trip to validate a name.
        if time.time() - self._scene_cache_at < 5.0:
            return self._scene_cache
        try:
            self._scene_cache = tuple(s["sceneName"] for s in self.client.get_scene_list().scenes)
            self._scene_cache_at = time.time()
        except Exception as exc:
            self._drop(exc)
            return ()
        return self._scene_cache

    def apply(self, action):
        """Run one action. Unknown or impossible actions are logged, never raised - a bad
        line in a config file must not stop the show."""
        kind = action.get("type")
        if self.dry_run:
            log.info("DRY-RUN would apply: %s", json.dumps(action, ensure_ascii=False))
            return True
        if not self.connect():
            log.warning("skipped %s - no OBS connection", kind)
            return False

        try:
            if kind == "scene":
                name = action["scene"]
                # ⚠️ Checked against the real scene list first. The config will normally
                # name mood scenes before they have been built, and a missing scene should
                # read as "you have not made this yet", not as a stack trace mid-set.
                available = self.scenes()
                if available and name not in available:
                    log.warning("scene %r does not exist in OBS (have: %s)",
                                name, ", ".join(available))
                    return False
                self.client.set_current_program_scene(name)
                log.info("scene -> %s", name)
                return True

            if kind == "visible":
                scene, source = action["scene"], action["source"]
                want = bool(action.get("visible", True))
                item_id = self._find_item(scene, source)
                if item_id is None:
                    log.warning("source %r not found in scene %r", source, scene)
                    return False
                self.client.set_scene_item_enabled(scene, item_id, want)
                log.info("%s/%s visible=%s", scene, source, want)
                return True

            if kind == "filter":
                self.client.set_source_filter_enabled(
                    action["source"], action["filter"], bool(action.get("enabled", True)))
                log.info("filter %s/%s enabled=%s", action["source"], action["filter"],
                         action.get("enabled", True))
                return True

            if kind == "mute":
                self.client.set_input_mute(action["source"], bool(action.get("muted", True)))
                log.info("input %s muted=%s", action["source"], action.get("muted", True))
                return True

            log.warning("unknown action type %r - ignored", kind)
            return False

        except KeyError as exc:
            log.warning("action %s is missing field %s - ignored", kind, exc)
            return False
        except Exception as exc:
            self._drop(exc)
            return False

    def _find_item(self, scene, source):
        try:
            for it in self.client.get_scene_item_list(scene).scene_items:
                if it["sourceName"] == source:
                    return it["sceneItemId"]
        except Exception as exc:
            self._drop(exc)
        return None


# --------------------------------------------------------------------------- rig state

# REAPER addresses tracks by position: /track/3/mute, /track/3/name and so on.
TRACK_RE = re.compile(r"^/track/(\d+)/(.+)$")


class RigState:
    """What the rig has told us so far, and what that implies.

    Deliberately additive: OSC is UDP and messages can be lost or arrive out of order, so
    nothing here assumes a complete picture. An unknown track name is simply unknown until
    REAPER mentions it.
    """

    def __init__(self, mood_names):
        self.mood_keys = [norm(m) for m in mood_names]
        self.names = {}          # track index -> name as sent
        self.muted = {}          # track index -> bool
        self.tempo = None
        self.playing = None
        self._lock = threading.Lock()

    def set_name(self, idx, name):
        with self._lock:
            changed = self.names.get(idx) != name
            self.names[idx] = name
        if changed:
            log.debug("track %d is %r", idx, name)
        return changed

    def set_mute(self, idx, muted):
        with self._lock:
            changed = self.muted.get(idx) != muted
            self.muted[idx] = muted
        return changed

    def active_mood(self):
        """The one mood track that is not muted.

        Returns None when the answer is not yet knowable - during startup before names
        have arrived, or in the brief window where mood_mute has unmuted the new mood but
        not yet muted the old one. Returning None rather than guessing matters: acting on
        a half-applied state would switch the projection to a mood that is about to be
        muted again, and the visible result is a flicker on every change.
        """
        with self._lock:
            names = dict(self.names)
            muted = dict(self.muted)

        unmuted = []
        seen = 0
        for idx, name in names.items():
            key = norm(name)
            if key not in self.mood_keys:
                continue
            if idx not in muted:
                continue                      # nothing heard about this one yet
            seen += 1
            if not muted[idx]:
                unmuted.append((idx, name))

        if seen == 0:
            return None
        if len(unmuted) != 1:
            # Zero means everything is muted; more than one means we caught mood_mute
            # mid-sweep. Both are transient and neither is a mood.
            log.debug("no single unmuted mood (%d of %d known) - holding", len(unmuted), seen)
            return None
        return unmuted[0][1]


# --------------------------------------------------------------------------- the bridge

class Bridge:
    def __init__(self, config, obs, dry_run=False):
        self.config = config
        self.obs = obs
        self.dry_run = dry_run

        self.moods = config.get("moods", {})
        self.mood_index = {norm(k): k for k in self.moods}
        self.addresses = config.get("addresses", {})

        self.state = RigState(self.moods.keys())
        self.current_mood = None
        self._last_switch = 0.0
        self._debounce = float(config.get("options", {}).get("debounce_seconds", 0.25))
        self.unknown = {}
        self.applied = []            # what we did, for --simulate to assert against

    # -- OSC intake ---------------------------------------------------------

    def handle(self, address, *args):
        """One handler for every message.

        A single default handler rather than a table of patterns, because the thing that
        makes an OSC integration debuggable is being able to see what actually arrived -
        including, especially, the messages nothing is listening for.
        """
        log.debug("osc %s %s", address, args)

        if address in self.addresses:
            for action in self.addresses[address]:
                self.obs.apply(action)
                self.applied.append(action)
            return

        m = TRACK_RE.match(address)
        if m:
            idx, leaf = int(m.group(1)), m.group(2)
            if leaf == "name" and args:
                if self.state.set_name(idx, args[0]):
                    self.evaluate()
                return
            if leaf == "mute" and args:
                # REAPER declares this 'b' but a bool may arrive as True/False or as
                # 1.0/0.0 depending on how the pattern config is written. Accept both.
                if self.state.set_mute(idx, bool(args[0])):
                    self.evaluate()
                return

        if address in ("/tempo/raw",) and args:
            self.state.tempo = args[0]
            return
        if address in ("/play", "/stop") and args:
            self.state.playing = (address == "/play") and bool(args[0])
            return

        # Log each unrecognised address ONCE. REAPER is chatty and an unhandled address
        # repeating at the update rate would bury everything worth reading.
        if address not in self.unknown:
            self.unknown[address] = 0
            log.info("unhandled address %s %s (first sighting)", address, args)
        self.unknown[address] += 1

    # -- decisions ----------------------------------------------------------

    def evaluate(self):
        mood = self.state.active_mood()
        if mood is None or norm(mood) == norm(self.current_mood):
            return

        # ⚠️ mood_mute re-asserts the active mood every 30 ms, so mute traffic can arrive
        # in bursts and a mood change passes through a moment where two tracks look
        # unmuted. Debouncing means the projection changes once per mood change, not once
        # per packet.
        now = time.time()
        if now - self._last_switch < self._debounce:
            log.debug("debounced %s", mood)
            return
        self._last_switch = now

        key = self.mood_index.get(norm(mood))
        log.info("MOOD -> %s", mood)
        self.current_mood = mood

        for action in self.moods.get(key, {}).get("actions", []):
            self.obs.apply(action)
            self.applied.append(action)

    # -- running ------------------------------------------------------------

    def serve(self, host, port):
        disp = osc_dispatcher.Dispatcher()
        disp.set_default_handler(self.handle)
        server = osc_server.ThreadingOSCUDPServer((host, port), disp)
        log.info("listening for OSC on %s:%d", host, port)
        log.info("moods: %s", ", ".join(self.moods))
        return server


# --------------------------------------------------------------------------- config

def default_config():
    """Written on first run so there is something concrete to edit.

    The scene names here will not exist in OBS yet. That is deliberate - the bridge warns
    about a missing scene rather than failing, so this file doubles as the list of scenes
    still to build.
    """
    return {
        "osc": {"listen_host": "0.0.0.0", "listen_port": 8000},
        "options": {"debounce_seconds": 0.25},
        "moods": {
            "COSMOS":    {"actions": [{"type": "scene", "scene": "COSMOS"}]},
            "THE CAIRN": {"actions": [{"type": "scene", "scene": "THE CAIRN"}]},
            "ÉIRE":      {"actions": [{"type": "scene", "scene": "EIRE"}]},
            "THE DEEP":  {"actions": [{"type": "scene", "scene": "THE DEEP"}]},
        },
        "addresses": {},
    }


def load_config(path):
    if not os.path.exists(path):
        cfg = default_config()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
        log.info("wrote a starter config to %s", path)
        return cfg
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- simulate

def simulate(bridge, host, port):
    """Prove the whole path with no rig attached.

    The rig's OSC device is not configured yet, so this is the only way to know the
    bridge works before that GUI step happens - and it stays useful afterwards as the
    thing to run when the projection is not changing and you need to know which half is
    at fault.

    Sends the messages REAPER really sends, in the order REAPER really sends them:
    names first, then mute states, then a mood change.
    """
    server = bridge.serve(host, port)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.4)

    client = udp_client.SimpleUDPClient("127.0.0.1", port)
    ok, fail = 0, 0

    def check(label, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  PASS  {label}")
        else:
            fail += 1
            print(f"  FAIL  {label}")

    # The real track layout, as the rig reports it.
    layout = [
        (1, "CTRL (brain)"), (2, "COSMOS"), (3, "THE CAIRN"), (4, "ÉIRE"),
        (5, "THE DEEP"), (6, "REVERB (bus)"), (7, "Zoom [1]"), (8, "Zoom [2]"),
    ]
    print("\n1. track names arrive")
    for idx, name in layout:
        client.send_message(f"/track/{idx}/name", name)
    time.sleep(0.3)
    check("all 8 names learned", len(bridge.state.names) == 8)
    check("no mood chosen yet (no mute state)", bridge.current_mood is None)

    print("\n2. mute states arrive - COSMOS is the live mood")
    for idx in (2, 3, 4, 5):
        client.send_message(f"/track/{idx}/mute", 1.0 if idx != 2 else 0.0)
    time.sleep(0.5)
    check("active mood is COSMOS", norm(bridge.current_mood) == norm("COSMOS"))

    print("\n3. switch to ÉIRE - accented name, the one that has broken three times")
    bridge._last_switch = 0.0                      # skip debounce for the test
    client.send_message("/track/2/mute", 1.0)
    client.send_message("/track/4/mute", 0.0)
    time.sleep(0.5)
    check("active mood is ÉIRE", norm(bridge.current_mood) == norm("ÉIRE"))
    check("matched despite the accent", norm("ÉIRE") == norm("EIRE"))

    print("\n4. a half-applied sweep must NOT switch")
    before = bridge.current_mood
    bridge._last_switch = 0.0
    client.send_message("/track/5/mute", 0.0)      # two unmuted at once
    time.sleep(0.4)
    check("held during ambiguous state", bridge.current_mood == before)
    client.send_message("/track/4/mute", 1.0)      # sweep completes on THE DEEP
    time.sleep(0.4)
    check("settled on THE DEEP", norm(bridge.current_mood) == norm("THE DEEP"))

    print("\n5. a non-mood track must be ignored")
    bridge._last_switch = 0.0
    before = bridge.current_mood
    client.send_message("/track/7/mute", 0.0)      # Zoom [1]
    time.sleep(0.3)
    check("Zoom [1] changed nothing", bridge.current_mood == before)

    print("\n6. unrecognised addresses are logged, not swallowed")
    client.send_message("/nonsense/thing", 1.0)
    time.sleep(0.3)
    check("unknown address recorded", "/nonsense/thing" in bridge.unknown)

    print("\n7. a literal address mapping fires")
    n = len(bridge.applied)
    client.send_message("/riomhdhos/test", 1.0)
    time.sleep(0.3)
    check("mapped address applied an action", len(bridge.applied) > n)

    server.shutdown()
    print(f"\n{ok} passed, {fail} failed")
    return fail == 0


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--simulate", action="store_true",
                    help="send synthetic OSC to ourselves and assert the results")
    ap.add_argument("--dry-run", action="store_true",
                    help="listen and log, but never touch OBS")
    ap.add_argument("--port", type=int, default=None, help="override the listen port")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(args.config)
    host = cfg.get("osc", {}).get("listen_host", "0.0.0.0")
    port = args.port or cfg.get("osc", {}).get("listen_port", 8000)

    if args.simulate:
        # Simulation never touches OBS: the point is to prove the OSC->decision half in
        # isolation, so a failure means the bridge, not the scene collection.
        cfg.setdefault("addresses", {})["/riomhdhos/test"] = [
            {"type": "scene", "scene": "__simulated__"}
        ]
        bridge = Bridge(cfg, ObsActions(dry_run=True), dry_run=True)
        sys.exit(0 if simulate(bridge, "127.0.0.1", port) else 1)

    obs = ObsActions(dry_run=args.dry_run)
    obs.connect()
    bridge = Bridge(cfg, obs, dry_run=args.dry_run)
    server = bridge.serve(host, port)
    log.info("REAPER should send OSC to this machine on port %d", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping")
        server.shutdown()


if __name__ == "__main__":
    # cp1252 is the console default here, and a stray non-ASCII character in a print has
    # now killed three separate tools AT THE MOMENT THEY HAD SOMETHING USEFUL TO SAY.
    # Degrade the character, never the process.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
