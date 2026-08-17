# rigbridge — the moods drive the visuals

```
Push -> REAPER (RIOMHDHOS) -> OSC/UDP -> LAN -> rigbridge -> obs-websocket -> OBS
```

Selecting THE DEEP on the Push changes the projection. That is the whole purpose.

## Why it infers rather than listens

**There is no `/mood` message, and there cannot be one.** JSFX cannot do network and gmem
is JSFX-only, so nothing in the mood machinery can send a packet. What it *can* do is move
REAPER state, and REAPER has an OSC sender built in.

So the rig publishes ordinary track state and the bridge derives the show state from it.
`mood_mute` holds every mood track muted except the active one, re-asserting every 30 ms —
therefore **the active mood is the one mood track that is not muted.** Nothing new has to
be built on the rig side.

⚠️ **Tracks are matched by name, never by index.** REAPER addresses tracks by position,
but this repo has already been bitten by code that assumed a layout — the health probe
looked for the JSFX on CTRL and reported them absent because they had moved. Names arrive
on `/track/@/name`, so the bridge learns the index→name map at runtime. Re-order the
project and it follows.

⚠️ **Accents are stripped before matching.** `ÉIRE` has been mangled three separate ways
in this project — `Ã‰IRE` (UTF-8 read as Latin-1), `A%IRE` (terminal), and plain `EIRE`
(typed without the accent). Matching exact bytes means the projection silently fails to
change on one mood in four, which is a fault you find on stage. All those forms now fold
together.

⚠️ **Ambiguous states are held, not acted on.** During a mood change there is a moment
where two tracks look unmuted. Switching on that produces a visible flicker every time, so
`active_mood()` returns nothing unless exactly one mood is unmuted.

## Setup

**On the rig (GUI, one time):** Options → Preferences → Control/OSC/web → Add → **OSC
(Open Sound Control)**

- Mode: **Configure device IP + local port**
- Device IP: `192.168.1.76` (this desktop), port `8000`

⚠️ REAPER's default OSC config sends only a **bank of 8 tracks** (`DEVICE_TRACK_COUNT 8`).
The moods sit at tracks 2–5 so they fit — but raise the track count in the device dialog
if the project ever grows or is re-ordered, or moods could fall outside the bank and the
bridge would simply never hear about them.

**On this desktop:**

```
python rigbridge.py --simulate     prove it works with no rig attached
python rigbridge.py --dry-run      listen and log, touch nothing in OBS
python rigbridge.py                run for real
python test_live_obs.py            end-to-end against real OBS
```

## Config

`bridge.config.json`, written automatically on first run:

```json
{
  "osc": { "listen_host": "0.0.0.0", "listen_port": 8000 },
  "options": { "debounce_seconds": 0.25 },
  "moods": {
    "COSMOS":    { "actions": [ { "type": "scene", "scene": "COSMOS" } ] },
    "THE CAIRN": { "actions": [ { "type": "scene", "scene": "THE CAIRN" } ] },
    "ÉIRE":      { "actions": [ { "type": "scene", "scene": "EIRE" } ] },
    "THE DEEP":  { "actions": [ { "type": "scene", "scene": "THE DEEP" } ] }
  },
  "addresses": {}
}
```

Each mood holds a list of actions, so a mood can switch a scene *and* toggle sources:

| Action | Fields |
|---|---|
| `scene` | `scene` |
| `visible` | `scene`, `source`, `visible` |
| `filter` | `source`, `filter`, `enabled` |
| `mute` | `source`, `muted` |

`addresses` maps a literal OSC address to the same actions, for anything not mood-derived.

**The scene names in the default config do not exist in OBS yet.** That is deliberate —
a missing scene logs a warning and is skipped, so this file doubles as the list of scenes
still to build.

## Latency

~20–60 ms end to end, dominated by REAPER's OSC update cycle rather than the wire
(sub-millisecond on the LAN). **Control-rate, not audio-rate.** Correct for scene changes
and envelope-driven blending; wrong for anything that should land on a beat.

## Tested

`--simulate` — 10 checks, all passing: name learning, mood derivation, the accented name,
ambiguous-sweep holding, non-mood tracks ignored, unknown addresses logged, literal address
mappings.

`test_live_obs.py` — 4 checks against **real OBS**: three actual scene switches driven by
synthetic OSC, plus surviving a missing scene. Creates `__rigbridge_test_*` scenes,
restores the scene that was live, and removes what it made.

**The only untested link is REAPER's OSC sender**, because configuring it is a pending GUI
step on the rig. Everything downstream of that is proven.

## Traps found building this

⚠️ **obsws-python logs the websocket password at INFO level.** Its connect path prints
`Connecting with parameters: ... password=<plaintext>`, which lands in the terminal, any
redirected log file, and shell scrollback. The whole reason both this and `obsctl.py` read
the password out of OBS's own config was to keep it off the command line — and the library
prints it anyway. `rigbridge.py` caps that logger at WARNING.

⚠️ **OBS dropping must not take the bridge with it.** `ObsActions` reconnects with a
back-off rather than exiting: the rig keeps playing whatever OBS does, and a bridge that
needs restarting by hand is one more thing to remember at the worst possible moment.
