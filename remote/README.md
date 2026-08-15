# Phone control for Ríomhdhos

A small HTTP agent on the rig, and a PWA on the phone. Lets you check the rig's health,
restart REAPER, and shut the box down without opening RDP — which matters because
**opening RDP is itself what hangs REAPER** (see `project_riomhdhos_rdp_hangs_reaper`
in the memory notes).

```
phone (PWA, added to home screen)
   │  http + bearer token
   ▼
riomhdhos-agent.ps1        ── kills / launches REAPER, shuts the box down
   │  drops in.lua, reads out.txt
   ▼
Riomhdhos_remote.lua       ── already running inside REAPER
   │
   ▼
health.lua                 ── audio device, JSFX canaries, moods, drums, tempo
```

## Why it is shaped this way

**The agent is PowerShell** so the rig needs nothing installed. A dependency you have to
reinstall is a dependency you will discover missing at a venue.

**Deep health goes through the existing remote console** rather than a second channel.
`Riomhdhos_remote.lua` already watches `rig\remote\` and can call any ReaperScript API,
so the agent drops a snippet and reads the answer. The handshake carries a nonce, because
`out.txt` is rewritten by every run and "a file exists" does not prove whose run wrote it.

**The app must open when the rig is off.** That is exactly when you want the power-on
button, and exactly when the server that serves the app is unreachable. A service worker
caches the shell so the app still launches and shows an offline screen with the button on it.

**Power-on does not go through the agent, and cannot.** When the box is off there is
nothing to answer. It is a smart plug plus BIOS restore-on-AC, called directly from the
phone. Wake-on-LAN was rejected: it needs Ethernet on the same subnet and S3 sleep rather
than a real shutdown, neither of which holds on a phone hotspot.

**Shutdown kills REAPER first, always, with no way to skip it.** Windows waits for
applications on shutdown, and REAPER holding an ASIO device can deadlock on exit. A stall
leaves a headless box half down with the network already gone — recoverable only at the
physical power button. The lesson lives in code rather than in a note someone has to
remember.

## Install (on RIOMHDHOS, elevated)

```powershell
cd <repo>\remote\agent
.\install-agent.ps1 -ReaperTask "<name of the task that launches REAPER>"
```

Omit `-ReaperTask` and it will list candidates; without it the Start/Restart REAPER
buttons cannot work, because launching REAPER has to go through the task that puts it in
session 1 with the real audio device.

The installer registers a SYSTEM task at startup, opens TCP 8765 on **both** the Private
and Public firewall profiles (a phone hotspot is usually classified Public), starts the
agent, and prints a URL of the form:

```
http://192.168.1.232:8765/?token=<48 hex chars>
```

Open that on the phone once — the token is saved and stripped from the URL — then use
Chrome's **Add to Home Screen**.

## Settings in the app

| Setting | When you need it |
|---|---|
| Rig address | Only when the rig moves, e.g. onto the phone hotspot with a different address. Blank = wherever the app was served from. |
| Token | Filled automatically by the `?token=` URL. |
| Smart plug ON url | The plug's local HTTP endpoint, e.g. `http://192.168.1.50/relay/0?turn=on` |

The plug must have a **local HTTP API with CORS** — Shelly or Tasmota. TP-Link Kasa uses a
proprietary TCP protocol a browser cannot speak, and cloud-only plugs are useless on a
hotspot with no internet.

## API

All `/api/*` routes need `Authorization: Bearer <token>` (or `?token=`).

| Route | Method | Does |
|---|---|---|
| `/api/whoami` | GET | Rig name and agent version, nothing else. Exists for discovery sweeps. |
| `/api/health` | GET | Full report. `?deep=0` skips the REAPER round trip. |
| `/api/reaper/restart` | POST | Kill, then relaunch via the scheduled task |
| `/api/reaper/stop` | POST | Force kill |
| `/api/reaper/start` | POST | Launch via the scheduled task |
| `/api/system/shutdown` | POST | Kill REAPER, then shut down |
| `/api/system/reboot` | POST | Kill REAPER, then reboot |
| `/api/audio/devices` | GET | ASIO drivers, which is selected, whether its hardware is attached |
| `/api/audio/device?name=…` | POST | Switch interface: save, kill REAPER, edit `reaper.ini`, relaunch |

## Switching audio interface

The rig has two: the **UMC 202HD** (2 in) for normal use and the **Zoom H6essential**
(6 in) for shows with more instruments to record. ASIO drives one interface at a time, so
this is a toggle, not a mix.

REAPER keeps the choice in `reaper.ini` under `[audioconfig]`:

```ini
asio_driver_name="UMC ASIO Driver"
```

with per-driver channel ranges remembered separately in `[asiochan]`, so REAPER restores
the right channel count on its own when the driver changes.

The switch is restart-level and the ordering is what makes it safe: **save → kill →
edit → relaunch.** REAPER holds audio config in memory and rewrites `reaper.ini` on exit,
so an edit under a running REAPER is simply overwritten. Killing first means a force-kill,
which does *not* write the ini back, so the edit survives — and the save beforehand is
what stops that costing unsaved work.

Finding `reaper.ini` is harder than it looks, because the agent runs as SYSTEM and
`$env:APPDATA` is SYSTEM's own profile. It resolves by evidence instead: the running
REAPER's process owner first, then any `reaper.ini` on disk newest-first, then SYSTEM's
APPDATA as a last resort. `Win32_ComputerSystem.UserName` was tried and abandoned — it
reports the *console* user and returns empty the moment RDP displaces it, which is exactly
when someone is likely to be changing audio settings.

## Finding the rig on a phone hotspot

**MAC addresses cannot help with this.** They are link-layer, they do not route, and a
browser cannot see them. On the home LAN the rig is at a known address; on a hotspot
Android hands it a fresh lease on a different subnet and nothing in the page can know which.

So the app asks. **Find rig on this network** sweeps candidate subnets — the common
Android and Windows hotspot ranges first, then the usual home ranges — hitting
`/api/whoami` on each address and saving the first that answers. That endpoint exists
purely for this: a sweep is up to 254 requests per subnet, and pointing that at the full
health endpoint would hammer the machine it is trying to find.

Known addresses are tried before any sweep, and the result is saved, so the scan is a
one-time cost per network rather than something you wait for on every launch.

## What has and has not been tested

**Verified on the rig, 2026-08-14.** `health.lua` runs correctly inside REAPER; every
`GetAudioDeviceInfo` attribute name was right first time. The agent is installed, answers
over the network, and reports the real ASIO device, both JSFX canaries, all four moods,
the drum chain and tempo.

One bug found and fixed in the process: the first version looked for `pushbrain` and
`pushled` on the CTRL track and reported them **absent**. They actually live on their own
`PUSH` and `PUSH LED` tracks. The probe now searches every track by FX name instead of
assuming a layout — a health check that hardcodes the layout will lie the next time the
layout moves, and it had moved already.

The REAPER launch task did not exist at all; REAPER had been started by hand from
Explorer. `install-reaper-task.ps1` now creates it, and an Interactive-principal task was
confirmed to land in **session 1 as the console user** — the property that decides whether
REAPER comes up on the real ASIO device or comes up deaf.

Verified earlier on CUCHULAINN against a harness faking REAPER and the console: static
serving, 401 without a token, 404 on unknown routes, path traversal rejected, the full
nonce handshake and report parse, and the "not responding" and "Remote Audio" warnings.

**Still untested:**

- **Start / Restart REAPER end to end.** The launch mechanism is proven, but the buttons
  themselves have not been fired: the project was dirty at the time and a restart would
  have discarded unsaved changes. Press Restart once with a saved project to close this out.
- **Power-on.** No smart plug exists yet.
