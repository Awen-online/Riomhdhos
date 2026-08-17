-- Riomhdhos: actual audio levels, reported to the phone.
--
-- WHY THIS EXISTS SEPARATELY FROM health.lua: health answers "is the audio device
-- open". This answers "is audio coming out". They are different claims, and only the
-- second one matters when you are standing on stage wondering why it is silent. A rig
-- can pass every check in health.lua and be completely mute - wrong mood active, input
-- not armed, Kontakt not loaded.

local R = reaper
local out = {}
out[#out+1] = "NONCE=@@NONCE@@"

-- PEAK HOLD, not an instantaneous read. Track_GetPeakInfo returns the level right now,
-- and a single call almost always lands between notes - reporting silence on a rig that
-- is working perfectly. Holding the maximum over a window is the only reading that
-- answers the question honestly.
--
-- Blocking the main thread here does NOT interrupt audio: REAPER's audio runs on its
-- own thread. It briefly freezes the UI, which nobody is looking at on a headless box.
local function peakhold(tr, ms)
  local best0, best1 = 0, 0
  local t0 = R.time_precise()
  while (R.time_precise() - t0) * 1000 < ms do
    local a = R.Track_GetPeakInfo(tr, 0)
    local b = R.Track_GetPeakInfo(tr, 1)
    if a > best0 then best0 = a end
    if b > best1 then best1 = b end
  end
  return best0, best1
end

-- dBFS, with a floor rather than -inf: the phone has to render this, and "-inf" is
-- wider than the column it lands in. -150 reads as silence to anyone and formats.
local function db(v)
  if v <= 0.0000001 then return -150 end
  return math.floor(20 * math.log(v, 10) * 10 + 0.5) / 10
end

local master = R.GetMasterTrack(0)
local mL, mR = peakhold(master, 700)
out[#out+1] = "master_l=" .. db(mL)
out[#out+1] = "master_r=" .. db(mR)
out[#out+1] = "play_state=" .. tostring(R.GetPlayState())

-- Per-track, so a silent master can be traced to a cause rather than just reported.
-- Short window each: the master reading above is the one that needs to catch a
-- transient; these only need to show which track is contributing.
--
-- Packed as L|R|mute|arm|midi|name. NAME LAST, deliberately: a track name may contain a
-- '|' and would then shift every field after it. Last means the parser can take the
-- remainder verbatim and nothing can be corrupted by punctuation someone typed.
local n = 0
for i = 0, R.CountTracks(0) - 1 do
  local tr = R.GetTrack(0, i)
  local ok, name = R.GetSetMediaTrackInfo_String(tr, "P_NAME", "", false)
  if not ok or name == "" then name = "track " .. (i + 1) end
  local l, r = peakhold(tr, 50)

  -- MIDI-ARMED TRACKS CARRY NO AUDIO AND NEVER WILL. The brain, the Push, the LED track
  -- and DRUMS are all armed for MIDI, so "armed but silent" is their permanent correct
  -- state - flagging it would fire a warning on every single read, which teaches you to
  -- ignore warnings. That is worse than having none.
  --
  -- Detected from I_RECINPUT rather than by name: bit 12 (4096) marks a MIDI input. The
  -- README already records what happens when this code assumes a track layout - the
  -- health probe looked for the JSFX on CTRL and reported them absent, because they had
  -- moved. Read the property, never the name.
  local rin  = R.GetMediaTrackInfo_Value(tr, "I_RECINPUT")
  local midi = (rin >= 0 and (rin & 4096) ~= 0) and 1 or 0

  out[#out+1] = string.format("track%d=%s|%s|%d|%d|%d|%s", n, db(l), db(r),
    R.GetMediaTrackInfo_Value(tr, "B_MUTE"),
    R.GetMediaTrackInfo_Value(tr, "I_RECARM"),
    midi, name)
  n = n + 1
end
out[#out+1] = "track_count=" .. n

out[#out+1] = "END=@@NONCE@@"
return table.concat(out, "\n")
