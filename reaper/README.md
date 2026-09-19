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
| `pushbrain` | Ableton Push 1 input: mood select, layer toggles, scale-locked note grid, encoders, the DRUMS step grid and its preset bank. |
| `pushled` | Push 1 LEDs and 4×68 display. Separate track from `pushbrain` — see below. |
| `drumseq` | First on the DRUMS track. 8 voices × 8 steps, free-running against the project tempo, reading its pattern out of gmem. Generates the notes it feeds. |
| `midispy`, `pushmap`, `pushlight` | Diagnostics: capture what a controller emits, map it, light it. |
| `minilab-lcd` | Earlier standalone LCD experiment. |

## Scripts

`__startup.lua` runs `mood_mute` (exclusive mood muting), `arm_inputs`, `bridge`
(K4–K7 → the active mood's Kontakt macros) and `remote` (a Lua console driven over
SSH). `riomhdhos_lib.lua` is the helper library that console loads. The `add_*` /
`learn_*` scripts are one-shot installers, as is `build_moods.lua` — it creates one
Kontakt child track per instrument under each mood bus and carries the intended
instrument list for all four moods. `Riomhdhos_build_drums.lua` is the same kind of
thing for the DRUMS track: `drumseq` at the head of the chain and eight
ReaSamplOmatic5000 behind it, one per voice. All of them are safe to re-run.

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

## Push layout — four views

`NOTE` (CC50) and `SESSION` (CC51) select a view directly; they are not a toggle.
`SCALES` (CC58) and `ACCENT` (CC57) are toggles, for reasons given below. `RECORD`
(CC86) is a second door into DRUMS. The mode lives in `gmem[61]` so the two plugins
cannot disagree about what is on screen.

The grid defaults to **D major** — the rig's home key, where most of the repertoire sits
and what the pipes are pitched in. It used to default to C minor, which put the grid in
the wrong key on every fresh instance and left the low B off the bottom row entirely.

⚠️ A JSFX slider default applies only to instances added **afterwards**. The live
project stores its own values in the `.rpp`, so changing the default never moves a grid
that is already running — that takes writing the parameter itself.

```
NOTE view      all 64 pads play a scale-locked grid. Nothing else lit.
               No mood row, no arm/mute - the panel is only the instrument.

SESSION view   4x4 instrument grid: COLUMN = mood, ROW = instrument slot
                 lit  = loaded and audible      dim  = loaded, toggled off
                 dark = empty slot              cols 5-8 dark
               mood select on the top row
               arm/mute rows lit (upper CC102-109 red = ARM,
                                  lower CC20-27 white = MUTE, both binary)

DRUMS view     8x8 step sequencer: ROW = voice, COLUMN = step, voice 1 on TOP
               lower button row CC20-27 = the eight preset beats, loaded one lit
               CC102 run/stop  CC103 clear  CC36-43 step division 1/4..1/32T
               ACCENT again toggles ACCENT EDIT - same pads, accent map

EVERY view     PLAY (CC85)   = panic, all-notes-off on every mood channel
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
and a loop that kept the last match counted the wrong plugin's parameters. Matching now
excludes the mixer explicitly, by `LAYERMIXER`.

⚠️ **Renaming the JSFX did not fix that, and cannot.** `TrackFX_GetFXName` returns the
name stored in the project **when the FX was added**, not the plugin's current `desc:`.
The mixer's `desc:` has said "4 instrument layers" for some time, yet every instantiated
copy still reports "4 Kontakt layers -> stereo" and still collides with a `KONTAKT`
search. A rename only takes effect on FX added afterwards, so a name change is never a
fix for a matching bug - the explicit exclusion is what actually holds.

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

## Muting an instrument — two routes, one gate

A green pad drives both, because neither alone covers every case:

- **instrument volume** (via the Lua bridge) — works today with no routing, but only
  for libraries publishing a usable volume. COSMOS, THE CAIRN and THE DEEP do; the
  Uilleann Pipes library on ÉIRE does not.
- **gmem layer mute** (in `layermix`) — works for ANY instrument whatever it publishes,
  but only once Kontakt's outputs are split (inst 1 → st.1, 2 → st.2 …).

⚠️ The layer mutes are **gated behind the `Kontakt outputs` switch** on each mixer,
default off. Applying them unsplit is actively wrong: everything arrives on layer 1, so
muting instrument 1 takes the whole mood with it and muting instrument 2 mutes silence.
Split the outputs, then set that switch to SPLIT per mood.

⚠️ Muted state is **derived by reading the parameter**, never cached. A remembered flag
desyncs the moment anything else touches the value — a manual restore, a reload — and
then a press "restores" a volume that was never muted, so nothing happens and the pad
looks dead. Previous levels live in ExtState so they survive reloads.

⚠️ Kontakt's **formatted parameter values lag one write behind**. Compare normalised
values, or listen. Two separate diagnoses were sent the wrong way by trusting the text.

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

⚠️ **Do not assume a library publishes nothing because it has no `Volume`.** Every
library names its own controls, and only one of the three in this rig calls the level
"Volume". Read the parameter names before concluding an instrument is unreachable:

```
Play Series        Volume                    one master
Cutoff/Vol family  Vol A, Vol B              two layers, no master
Uilleann Pipes     Rel Vol, Drone Vol        chanter + drone, no master
```

ÉIRE was written off as "publishes nothing usable" and slated for a manual host-automation
assignment on that basis. It publishes `Rel Vol` and `Drone Vol`, and needed **no Kontakt
setup at all** — only a matcher that recognises volume names by **shape** (a token equal
to `vol`/`volume`, at most two words) rather than from a hardcoded list. Every match in
the slot is driven together: muting the chanter alone leaves the drone sounding.

A parameter genuinely publishing nothing shows as `#000`, `#001`… — *that* is the
signature of an unassigned slot, and the only case needing the manual fix below.

Fix, once per instrument, only if the names really are `#000`-style: in that mood's
Kontakt, **Classic view → Browser → Automation → Host Automation**, drag the
instrument's Volume onto a slot. The Automation tab does **not** exist in Kontakt 7/8's
redesigned browser; it is Classic view only.

⚠️ Kontakt exposes **no per-instrument mute** to the host. Muting is implemented as
"drive that instrument's volume parameter(s) to zero and remember the previous value".

## Mood selection needs no Kontakt setup

Each mood receives only its own MIDI channel, but the send **remaps the channel to 1 on
delivery** (`I_MIDIFLAGS = channel + 32`). Kontakt instruments default to channel 1, so
every mood plays with **no per-instrument configuration at all**, and any instrument
loaded later works immediately.

## The DRUMS track

`ACCENT` (CC57) opens it. ⚠️ **That CC is an inference, not a measurement.** Four of its
neighbours are verified by working code in `pushbrain` — Octave Down 54, Octave Up 55,
`REPEAT` 56, `SCALES` 58, which the source records as "the button above REPEAT". That
fixes the block as

```
SCALES 58   USER  59
REPEAT 56   ACCENT 57?
OCT-   54   OCT+  55
```

so the button above Octave Up and right of Repeat is CC57. It lives on a **slider** at
both ends (`pushbrain` slider44, `pushled` slider40) rather than as a constant, so a
wrong guess is a knob turn. Confirm it by putting `pushmap` or `midispy` on the Push
track and reading `last cc` while pressing Accent.

`ACCENT` both selects and toggles, which `NOTE` and `SESSION` deliberately do not. From
anywhere else it lands you in DRUMS, full stop. Pressed again *while already in DRUMS*
it toggles **accent edit**: the same 64 pads then write the accent map instead of the
step map, and the button blinks (Push LED mode 5) because that is the only thing on the
panel saying which array a press will land in. The button is printed ACCENT, and a panel
whose labels are true is worth more than an unbroken "views never toggle" rule — the
same trade `SCALES` already makes.

⚠️ **`drumseq` generates its own notes and its own clock.** Nothing upstream feeds it,
and it counts samples against the project tempo rather than rolling the transport,
because the timeline has media items on it. It is therefore audible with nothing armed,
nothing recording and the edit cursor untouched.

⚠️ **The instrument is ReaSamplOmatic5000, not Kontakt.** `drumseq` plays eight
*consecutive* notes (36–43 by default) and no drum library maps that way — GM scatters a
kit and every Kontakt library rearranges it again. Eight RS5k instances, one per voice
with its note range pinned to a single note, put the layout here instead of negotiating
it. RS5k is also stock, so it cannot go missing or stall on an authorisation mid-set,
and eight separate instances publish eight identical named parameter sets — none of the
instrument-boundary detection the moods need. Set **Obey note-offs off** on each, or
`drumseq`'s gate chops the cymbal to the length of a step. Kontakt still earns a place
for big cinematic hits (`D:\KONTAKT\Spitfire Audio HZ01 Hans Zimmer Percussion…`) on a
*second* drums track — note that Old Tape Drums is already spoken for by THE CAIRN.

⚠️ **gmem is not saved with the project.** A pattern typed in by hand is gone the next
time REAPER loads. That, not convenience, is why the eight preset beats exist in
`pushbrain` — the bank is the only pattern that survives a reload. Each preset is one
64-character string, row per voice, `.` silent `x` hit `X` accented, so the beat is
legible as a beat in the source.

⚠️ **Editing a step while stopped used to light nothing.** `pushled` decided a repaint
was due by watching the playhead, and `drumseq` parks that at −1 on every block when it
is not running — so it never changed and a whole pattern could be typed in blind.
`gmem[2105]` is a pattern edit counter and is what triggers the repaint now. The
playhead is handled separately and repaints **only the two columns that changed**: a
full redraw is ~513 bytes and at 1/8 and 120 BPM that is ~2 kB/s against MIDI 1.0's
3125 B/s ceiling, with the pads and LCD sharing the same port.

⚠️ **The lower button row was dark in this view and still acted.** `redraw_seq` blanks
every button CC and never relit CC20–27, but the arm/mute handler is not view-scoped, so
pressing one there muted a track from a button the panel was presenting as unassigned.
The row is now the preset bank, which fixes both halves at once.

## gmem map

```
0, 1        live input envelopes (guitar, second input)
8, 9        envelope peak hold
44..48      scale published by pushbrain: root+1, scale+1, row step,
            layout+1, chromatic step
49, 50      held pitch-class mask + 1, lowest held note + 1  (chord naming)
52          swing + 1
53          repeat/hold engaged
54..57      note event counter, notes held, last note, its velocity
58..60      parameter readout: counter, value, label id + 2
61          view          0 NOTE  1 MIXER  2 DRUMS  3 SCALES
62          octave offset + 1   (pushbrain -> pushled)
63          active mood + 1     (minilab-brain -> pushled, bridge)
64 + m*16 + 0..3    faders 1-4      (value+1, 0 = never touched)
64 + m*16 + 4..11   knobs K1-K8     (value+1, 0 = never touched)
64 + m*16 + 12..15  layer 1-4 muted (1 = silent)
1000+       MIDI spy log

-- DRUMS.  pushbrain writes, drumseq and pushled read.
2000 + v*8 + s      step on        (v = voice 0-7, s = step 0-7)
2100        current step, -1 while stopped
2101        1 = running
2102        step division index   0 = 1/4 ... 7 = 1/32T
2103        1 = accent edit (pads write accents, not steps)
2104        preset loaded + 1     (0 = hand-edited, no preset lit)
2105        pattern edit counter  (see above - the repaint trigger)
2200 + v*8 + s      accent on      (drumseq picks "Accent velocity" for these)
```
