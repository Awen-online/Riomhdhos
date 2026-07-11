#!/bin/bash
# Folk-drone rig launcher.
# Auto-detect the UMC202HD's Pd audio-output device index each boot (USB
# enumeration order is not stable). Fall back to the onboard jack (device 0)
# if the interface isn't present.
IDX=$(timeout 4 pd -nogui -alsa -listdev 2>&1 \
  | awk '/audio output devices:/{f=1;next} /MIDI/{f=0} f && /UMC202HD/ && /plug-in/{print $1}' \
  | tr -d '.' | head -1)
[ -z "$IDX" ] && IDX=0
logger -t folkdrone "Pd audio output device index = $IDX"
exec /usr/bin/pd -nogui -alsa -noadc -audiooutdev "$IDX" -channels 2 -r 44100 -audiobuf 60 \
  -midiindev 1 -midioutdev 1 \
  /home/ian/folk-drone.pd /home/ian/minilab-map.pd /home/ian/combo-synth.pd /home/ian/combo-norse.pd /home/ian/combo-eire.pd /home/ian/combo-deep.pd /home/ian/cairn-horn.pd /home/ian/eire-flute.pd /home/ian/deep-groan.pd /home/ian/cairn-lyre.pd /home/ian/cairn-air.pd /home/ian/eire-harp.pd /home/ian/eire-air.pd /home/ian/deep-wind.pd /home/ian/deep-rumble.pd /home/ian/rig-nav.pd
