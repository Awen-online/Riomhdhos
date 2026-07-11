#!/bin/bash
# Folk-drone rig launcher.
# Loads patches from this script's own directory (the git working tree), so the
# rig runs directly from the repo checkout wherever it lives. Sample assets are
# referenced by absolute path (/home/ian/samples, /home/ian/sfz) and are NOT in
# the repo — keep WorkingDirectory=/home/ian in the service unit.
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
# Auto-detect the UMC202HD's Pd audio-output device index each boot (USB
# enumeration order is not stable). Fall back to the onboard jack (device 0)
# if the interface isn't present.
IDX=$(timeout 4 pd -nogui -alsa -listdev 2>&1 \
  | awk '/audio output devices:/{f=1;next} /MIDI/{f=0} f && /UMC202HD/ && /plug-in/{print $1}' \
  | tr -d '.' | head -1)
[ -z "$IDX" ] && IDX=0
logger -t folkdrone "Pd audio output device index = $IDX (patches from $DIR)"
exec /usr/bin/pd -nogui -alsa -noadc -audiooutdev "$IDX" -channels 2 -r 44100 -audiobuf 60 \
  -midiindev 1 -midioutdev 1 \
  "$DIR"/folk-drone.pd "$DIR"/minilab-map.pd "$DIR"/combo-synth.pd "$DIR"/combo-norse.pd "$DIR"/combo-eire.pd "$DIR"/combo-deep.pd "$DIR"/cairn-horn.pd "$DIR"/eire-flute.pd "$DIR"/deep-groan.pd "$DIR"/cairn-lyre.pd "$DIR"/cairn-air.pd "$DIR"/eire-harp.pd "$DIR"/eire-air.pd "$DIR"/deep-wind.pd "$DIR"/deep-rumble.pd "$DIR"/rig-nav.pd "$DIR"/lcd.pd
