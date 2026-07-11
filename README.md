# Ríomhdhos — Folk Drone Rig

A live ambient-accompaniment synthesizer for solo folk guitar + storytelling.
Pure Data synthesis engine running **headless on a Raspberry Pi 3** ("folkpi"),
played from an **Arturia MiniLab 3**. Evolving drone beds + triggered
SFX/foley, with dynamics driven by the controller's faders, knobs and pedal.

## The name

**Ríomhdhos** *(Irish)* — pronounced roughly **"REEV-ghuss"** (IPA ≈ /ˌɾˠiːwˈɣɔsˠ/;
the lenited *dh* is a soft guttural, and pronunciation varies by dialect).

A coined compound of two Irish words:

- **ríomh-** — *electronic / digital / computer-* (from *ríomh*, "to count, compute, reckon"; cf. *ríomhaire* = computer, *ríomhphost* = email)
- **dos** — the *drone* / bass-pipe of the bagpipes (also "bush, tuft"; *dos mór* = greater drone, *dos beag* = lesser drone)

In the compound the second element lenites (*dos → dhos*), so **Ríomhdhos**
literally means an **"electronic drone"** — a fitting name for a digital drone
synthesizer with the uilleann pipe's drone in its DNA.
Meanings per Ó Dónaill (teanglann.ie).

## The four worlds

Selected live via the MiniLab's browse encoder (name shown on the MiniLab LCD).
Each world has all four faders mapped to independent layers, gated by the
sustain-pedal swell. All layers self-tested clean (no clipping at full).

| # | World | Layers (Fader 1–4) |
|---|-------|--------------------|
| 0 | 🌌 **COSMOS** | detuned strings / Rhodes / sine pad / breath |
| 1 | 🪨 **THE CAIRN** (Norse / Wardruna) | Box Violin swell / horn-throat / lyre shimmer / wind |
| 2 | ☘️ **ÉIRE** (Irish) | reed drone / Ocean Flute / harp shimmer / misty air |
| 3 | 🌊 **THE DEEP** | dark saw sub / metallic groan / cave-wind / sub-rumble |

## Layout

- `folk-drone.pd` — core synth: 4 voices, tape chorus, `rev3~` reverb, master low-pass.
- `minilab-map.pd` — maps the MiniLab 3 CCs to the engine's Pd send names.
- `rig-nav.pd` — browse encoder scrolls the world list, drives the LCD, broadcasts `keysinst`.
- `combo-synth.pd` / `combo-norse.pd` / `combo-eire.pd` / `combo-deep.pd` — per-world routers.
- `cairn-*.pd`, `eire-*.pd`, `deep-*.pd` — individual layer patches (modular, self-testable).
- `folk-samples.pd` — 8 pads trigger looping SFX WAVs (ch10, notes 36–43).
- `jouhikko-drone.pd` — looped real Jouhikko D-drone layer.
- `start-rig.sh` — boot launcher; auto-detects the UMC202HD audio output index, falls back to onboard.
- `folkdrone.service` — systemd unit; brings the whole rig up ~15s after power, no laptop needed.
- `test-*.pd`, `rendertest.pd`, `lcd-test.pd`, `ledtest.pd`, `fwcheck.pd`, `midimon.pd` — bench/diagnostic patches.

## Hardware

- **Raspberry Pi 3 Model B** — Raspberry Pi OS Lite (64-bit), Pure Data. Ethernet/DHCP on the LAN.
- **Arturia MiniLab 3** — class-compliant USB MIDI. Keys = drone pitch, sustain pedal = master swell,
  4 faders = layer levels, 8 knobs = filter/warmth/reverb/vol, browse encoder = world select. LCD + pad
  LEDs driven from Pd over SysEx (device must be in DAW mode for LCD text).
- **Behringer UMC202HD** — class-compliant audio out (no driver). Onboard 3.5 mm jack is the fallback.
- Live chain: Pi → UMC202HD → Behringer Xenyx 802 → Fishman Loudbox Artist.

## MiniLab 3 MIDI map (current custom program)

- Faders 1–4 → CC 14, 15, 30, 31 → `lvlA/B/C/N`
- Knobs 1–8 → CC 86, 87, 89, 90, 110, 111, 116, 117 (K1 filter, K2 warmth, K3 reverb, K8 vol; K4–7 free)
- Browse encoder → CC 28 (relative, turn) / CC 118 (click)
- Mod strip → CC 1, Pitch strip → pitch bend (program-independent, like keys/pedal)
- Pads → ch10; Bank A 1–8 = notes 36–43, Bank B 1–8 = notes 44–51

> ⚠️ MiniLab CC numbers are **program-dependent** and drift if "Prog" is pressed. Lock one fixed custom
> program in Arturia MIDI Control Center so the CCs never change.

## Deploying to the Pi

Files deploy flat into `/home/ian/` (the paths in `start-rig.sh` and `folkdrone.service` assume that).

```sh
sudo systemctl {status,restart,stop} folkdrone
```

Launch by hand (headless, output-only):

```sh
setsid pd -nogui -alsa -noadc -audiooutdev 0 -channels 2 -r 44100 -audiobuf 60 \
  -midiindev 1 -midioutdev 1 /home/ian/folk-drone.pd /home/ian/minilab-map.pd &
```

> Set your own Pi login/password on deployment — none are stored in this repo.

## Self-test pipeline

Render a patch to WAV on the Pi, pull it to a desktop with numpy/scipy, and analyze RMS / FFT peak /
envelope modulation / clipping to verify correctness before trusting your ears. The harness patches
(`test-*.pd`, `rendertest.pd`) drive the combo via globals and record `catch~ sfx` through `writesf~`
(include a `; pd dsp 1` message or you get an empty WAV).

## Not in this repo

The `folk-samples/` WAV library (field recordings, Jouhikko/Box Violin/Ocean Flute SFZ renders) and
runtime logs are excluded — see `.gitignore`. Sample sources and per-file licenses are tracked separately.
