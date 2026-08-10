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

## Push layout — two views

`NOTE` (CC50) and `SESSION` (CC51) select a view directly; they are not a toggle.
The mode lives in `gmem[61]` so the two plugins cannot disagree about what is on
screen.

```
NOTE view      all 64 pads play a scale-locked grid. Nothing else lit.
               No mood row, no arm/mute - the panel is only the instrument.

SESSION view   4x4 instrument grid: COLUMN = mood, ROW = instrument slot
                 lit  = loaded and audible      dim  = loaded, toggled off
                 dark = empty slot              cols 5-8 dark
               mood select on the top row
               arm/mute rows lit (upper CC102-109 red = ARM,
                                  lower CC20-27 white = MUTE, both binary)

BOTH views     PLAY (CC85)   = panic, all-notes-off on every mood channel
               REPEAT (CC56) = HOLD - a true note latch, lit while engaged
               octave CC54 down / CC55 up, dark at the limits
               encoders CC71-78 -> the MiniLab's own knob CCs (same in both views)
               CC79 (9th encoder) -> master volume, capped at unity
               touch strip -> modulation CC1
```

Display: line 1 encoder names (or mood names in SESSION) · line 2 mood + played note
`C#4 v104 +2` · line 3 the parameter last moved, clearing after 2.5 s · line 4
wordmark left, current view right.

⚠️ Volume bars were tried in SESSION view and removed: volume is already covered by
the knobs and the mute buttons, and a single bottom-row pad press silenced a live
input. Pads show **instruments**, not levels.

⚠️ Do not substring-match plugin names loosely. The layer mixer described itself as
"4 Kontakt layers", so a search for `KONTAKT` matched it as well as the instrument -
and a loop that kept the last match counted the wrong plugin's parameters. It is now
named "4 instrument layers", and matching excludes the mixer explicitly.

⚠️ The Push 1 display is **four separate 17-character segments**, not one 68-character
strip. Breaks fall at 17, 34 and 51. Column offsets are 0, 9 | 17, 26 | 34, 43 | 51, 60
so no label straddles a break — laying them out every 8 characters renders `REVERB` as
`R EVERB`.

⚠️ Muting a mood does **not** use the track mute: `mood_mute` owns that and re-asserts
the active mood every 30 ms. Moods mute through a slider on their layer mixer, which is
the one thing the Lua bridge can set — it can neither read nor write gmem.

## ⚠️ Kontakt does NOT use fixed 64-parameter blocks

Host automation IDs are assigned **consecutively as each instrument publishes them**,
and the count varies by library — Play Series publishes 64, the Uilleann Pipes library
publishes 40. A `slot * 64` model is right only by coincidence, and when it is wrong it
reaches into the *next* instrument's parameters.

Instrument boundaries are found by the **first parameter's name repeating**: every
instrument of a given library starts with the same control (`Cutoff`, `Solo`, `Noise`),
so each repeat marks a new instrument.

```
COSMOS  'Cutoff' at 0, 64      CAIRN  'Cutoff' at 0, 64
EIRE    'Solo'   at 0, 40      DEEP   'Noise'  at 0
```

## LED convention — three states, not two

```
dark  = does nothing here / unassigned
dim   = available, not engaged
lit   = engaged
```

Arm and mute are deliberately binary, because they answer a yes/no question about a
track. Anything that is a MODE (Repeat/hold, the octave buttons at their limits) uses
dim rather than dark, so "available" never reads as "unassigned".

⚠️ Push button LEDs are **fixed colours by position** — upper row red, lower white.
Only off/dim/lit/blink are selectable, never hue. The only bi-colour buttons are the
time-division ones at **CC36-43** (red/green), which is where a selected value can be
shown by colour when sequencing lands.

## ⚠️ An instrument is only visible if it publishes host automation

The Push can only see, count or mute an instrument that exposes named parameters to
the host. Kontakt's rack is opaque and its state chunk is compressed (557 KB with no
readable strings), so there is no other way to know an instrument is loaded.

- **Play Series** libraries publish automatically — `Volume` at slot base + 7.
- The **Cutoff / Vol A / Vol B family** publishes too, but has **no single `Volume`** —
  it exposes `Vol A` and `Vol B` for its two layers, and both must be driven together
  or muting leaves half the instrument sounding.
- **Plain Kontakt libraries publish nothing.** Uilleann Pipes on ÉIRE shows every
  parameter as `#000`, `#001`… and is therefore invisible: no pad, no mute.

Fix, once per instrument: in that mood's Kontakt, **Browser → Automation → Host
Automation**, drag the instrument's Volume onto a slot.

⚠️ Kontakt exposes **no per-instrument mute** to the host. Muting is implemented as
"drive that instrument's volume parameter(s) to zero and remember the previous value".

## Mood selection needs no Kontakt setup

Each mood receives only its own MIDI channel, but the send **remaps the channel to 1 on
delivery** (`I_MIDIFLAGS = channel + 32`). Kontakt instruments default to channel 1, so
every mood plays with **no per-instrument configuration at all**, and any instrument
loaded later works immediately.

## gmem map

```
0, 1        live input envelopes (guitar, second input)
8, 9        envelope peak hold
62          octave offset + 1   (pushbrain -> pushled)
63          active mood + 1     (minilab-brain -> pushled, bridge)
64 + m*16 + 0..3    faders 1-4      (value+1, 0 = never touched)
64 + m*16 + 4..11   knobs K1-K8     (value+1, 0 = never touched)
64 + m*16 + 12..15  layer 1-4 muted (1 = silent)
1000+       MIDI spy log
```
