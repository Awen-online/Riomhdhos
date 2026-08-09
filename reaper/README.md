# Riomhdhos — Reaper rig

The Reaper half of the rig, mirrored from the live machine
(`%APPDATA%\REAPER\Effects\Riomhdhos\` and `%APPDATA%\REAPER\Scripts\`).
The Pd patches in the parent directory are the earlier Raspberry Pi rig.

## Concept

Four "moods" — COSMOS, THE CAIRN, ÉIRE, THE DEEP — each a Reaper track running
Kontakt. Mood selection is **MIDI channel re-routing**: a JSFX brain on a control
track re-channels everything to `mood+1`, and each mood track receives only its own
channel. Only the selected mood is audible.

## Effects/Riomhdhos

| File | Role |
|---|---|
| `minilab-brain` | The brain. Mood select, re-channelling, latching pedal → mood advance, MiniLab LCD, publishes state to sliders and gmem. |
| `layermix` | Per mood track, straight after Kontakt. Mixes 4 Kontakt outputs to stereo, per-mood lowpass, layer on/off, ducks against the live input. |
| `inputenv` | First on each live input track. Measures playing dynamics and publishes them via gmem. Passes audio untouched. |
| `pushbrain` | Ableton Push 1 input: mood select, layer toggles, scale-locked note grid, encoders. |
| `pushled` | Push 1 LEDs and 4×68 display. Separate track from `pushbrain` — see below. |
| `midispy`, `pushmap`, `pushlight` | Diagnostics: capture what a controller emits, map it, light it. |
| `minilab-lcd` | Earlier standalone LCD experiment. |

## Scripts

`__startup.lua` runs `mood_mute` (exclusive mood muting), `arm_inputs`, `bridge`
(K4–K7 → the active mood's Kontakt macros) and `remote` (a Lua console driven over
SSH). `riomhdhos_lib.lua` is the helper library that console loads. The `add_*` /
`learn_*` scripts are one-shot installers.

## Three things that are not obvious

**Kontakt does not pass MIDI downstream.** A JSFX placed after Kontakt never sees
CC20–31. Control positions therefore travel through **gmem**, not MIDI. Values are
stored `+1` so a stored `0` means "never touched" — otherwise an untouched control
reads as a fader at zero and mutes the track on load.

**Reaper's FX-parameter MIDI learn only sees hardware input.** It cannot see CCs the
brain invents inside the track stream, so anything learned that way is global across
all four moods. Per-mood control has to go through a JSFX (audio) or the Lua bridge
(other plugins' parameters).

**`pushbrain` and `pushled` must be on separate tracks.** Lighting a Push pad is a
note-on on channel 1, and the `pushbrain` track's output feeds the control track — so
lighting from there would play notes on COSMOS. Two tracks, two destinations.

## Push layout

```
row 7      mood select x4                      (rest dark)
row 6      dark
rows 0-5   col 0   layer 1-4 on/off
           col 1   dark, reserved
           cols 2-7  6x6 scale-locked note grid
buttons    6 columns: COSMOS CAIRN EIRE DEEP Zoom[1] Zoom[2]
           upper CC102-109 = MUTE, lower CC20-27 = ARM, fully lit or fully dark
encoders   CC71-78 -> the MiniLab's own knob CCs;  CC79 (9th) -> master volume
octave     CC54 down / CC55 up, published to gmem so the LED colouring follows
```

⚠️ The Push 1 display is **four separate 17-character segments**, not one 68-character
strip. Breaks fall at 17, 34 and 51. Column offsets are 0, 9 | 17, 26 | 34, 43 | 51, 60
so no label straddles a break — laying them out every 8 characters renders `REVERB` as
`R EVERB`.

⚠️ Muting a mood does **not** use the track mute: `mood_mute` owns that and re-asserts
the active mood every 30 ms. Moods mute through a slider on their layer mixer, which is
the one thing the Lua bridge can set — it can neither read nor write gmem.

## Mood selection needs no Kontakt setup

Each mood receives only its own MIDI channel, but the send **remaps the channel to 1 on
delivery** (`I_MIDIFLAGS = channel + 32`). Kontakt instruments default to channel 1, so
every mood plays with **no per-instrument configuration at all**, and any instrument
loaded later works immediately.

## gmem map

```
0, 1        live input envelopes (guitar, second input)
8, 9        envelope peak hold
63          active mood + 1
64 + m*16 + 0..3    faders 1-4      (value+1, 0 = never touched)
64 + m*16 + 4..11   knobs K1-K8     (value+1, 0 = never touched)
64 + m*16 + 12..15  layer 1-4 muted (1 = silent)
62          octave offset + 1   (pushbrain -> pushled)
63          active mood + 1
1000+       MIDI spy log
```
