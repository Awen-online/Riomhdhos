-- health.lua
-- Deep health probe, run inside REAPER by Riomhdhos_remote.lua. The agent drops this
-- into rig\remote\in.lua, touches in.ready, and reads the answer back out of out.txt.
--
-- WHY A NONCE
-- out.txt is rewritten by every run, so "a file exists" proves nothing about WHOSE run
-- wrote it. The agent substitutes a fresh nonce into @@NONCE@@ before each drop and
-- refuses any out.txt that doesn't carry it. Without this the agent would happily
-- report the previous probe's numbers as current - the exact class of bug that made
-- the console look broken for an afternoon in August.
--
-- OUTPUT FORMAT
-- Flat key=value lines, one per line. Deliberately not JSON: this is assembled by
-- string concatenation inside REAPER's Lua, and a missing brace in a quoting edge case
-- would cost more than the parse on the agent side saves.
--
-- Everything is defensive. A probe that throws must still return the rest of the
-- report, because "the audio device is wrong" and "the DRUMS track vanished" are
-- exactly the situations where you most need the other half of the picture.

say("NONCE=@@NONCE@@")

local function kv(k, v) say(k .. "=" .. tostring(v)) end

-- run a probe, and turn any error into a value rather than losing the whole report
local function try(k, fn)
  local ok, v = pcall(fn)
  kv(k, ok and v or ("ERR " .. tostring(v)))
end

------------------------------------------------------------------ reaper itself
try("reaper_version", function() return R.GetAppVersion() end)
try("project", function()
  local _, fn = R.EnumProjects(-1, "")
  return (fn ~= "" and fn:match("[^\\/]+$")) or "(unsaved)"
end)
-- 0 stopped, 1 playing, 2 paused, 4 recording. Should be 0: the rig is rhythmic via
-- drumseq's own sample clock, never the transport, because the timeline has media on it.
try("play_state", function() return R.GetPlayState() end)

------------------------------------------------------------------ audio device
-- This is the check that matters after an RDP session. If REAPER was relaunched while
-- RDP was connected it binds to the redirected "Remote Audio" device and looks entirely
-- healthy while making no sound at the desk.
local function adi(attr)
  local ok, s = R.GetAudioDeviceInfo(attr, "")
  return (ok and s ~= "" ) and s or "?"
end
try("audio_mode",  function() return adi("MODE")      end)
try("audio_out",   function() return adi("IDENT_OUT") end)
try("audio_in",    function() return adi("IDENT_IN")  end)
try("audio_srate", function() return adi("SRATE")     end)
try("audio_bsize", function() return adi("BSIZE")     end)

------------------------------------------------------------------ MIDI surfaces
-- Named rather than counted: "2 inputs" tells you nothing, "Push is missing" tells you
-- why nothing on the controller responds.
try("midi_in", function()
  local out = {}
  for i = 0, R.GetNumMIDIInputs() - 1 do
    local present, nm = R.GetMIDIInputName(i, "")
    if nm and nm ~= "" then
      out[#out+1] = nm .. (present and "" or " [disconnected]")
    end
  end
  return #out > 0 and table.concat(out, " | ") or "(none)"
end)
try("midi_out", function()
  local out = {}
  for i = 0, R.GetNumMIDIOutputs() - 1 do
    local present, nm = R.GetMIDIOutputName(i, "")
    if nm and nm ~= "" then
      out[#out+1] = nm .. (present and "" or " [disconnected]")
    end
  end
  return #out > 0 and table.concat(out, " | ") or "(none)"
end)

------------------------------------------------------------------ JSFX canaries
-- A JSFX that fails to compile still reports its full parameter list through the API,
-- so presence proves nothing. The -alive slider is set to 1 in @init; only a running
-- effect can have done that. 0 means loaded-but-dead, which looks identical from
-- REAPER's FX list and is the failure mode worth flying a flag for.
local ctrl = track("CTRL")
if not ctrl then
  kv("ctrl_track", "ABSENT")
else
  kv("ctrl_track", "ok")
  for _, nm in ipairs({ "pushbrain", "pushled" }) do
    try(nm, function()
      local fx = fxi(ctrl, nm)
      if not fx then return "absent" end
      local p = pidx(ctrl, fx, "alive")
      if not p then return "no-canary" end
      return R.TrackFX_GetParam(ctrl, fx, p)
    end)
  end
end

------------------------------------------------------------------ moods
-- The active mood is derived from mute state rather than read from a slider, because
-- Riomhdhos_mood_mute.lua keeps exactly one mood audible. That makes the answer a fact
-- about what you can actually hear, not a claim about what some control thinks.
-- moods() returns them in brain order and matches "IRE" so the accent in EIRE can't bite.
try("moods", function()
  local names = { "COSMOS", "CAIRN", "EIRE", "DEEP" }
  local out, active = {}, "?"
  local ms = moods()
  for i, tr in ipairs(ms) do
    if not tr then
      out[#out+1] = names[i] .. ":absent"
    else
      local muted = R.GetMediaTrackInfo_Value(tr, "B_MUTE") > 0.5
      if not muted then active = names[i] end
      out[#out+1] = names[i] .. (muted and ":muted" or ":ACTIVE")
    end
  end
  kv("mood_active", active)
  return table.concat(out, " | ")
end)

------------------------------------------------------------------ drums
-- drumseq must sit FIRST, ahead of the instrument, because it generates the notes it
-- feeds. If something reordered the chain the sequencer goes silent while still
-- appearing to run, so the position is part of the health answer, not just presence.
local drums = track("DRUMS")
if not drums then
  kv("drums", "ABSENT")
else
  try("drums_chain", function() return chain(drums) end)
  try("drums_seq", function()
    local fx = fxi(drums, "drum")
    if not fx then return "drumseq absent" end
    if fx ~= 0 then return "drumseq at slot " .. fx .. " - MUST BE 0" end
    local p = pidx(drums, fx, "Transport")
    local run = p and R.TrackFX_GetParam(drums, fx, p) or -1
    return "ok, transport=" .. ((run and run > 0.5) and "RUNNING" or "stopped")
  end)
end

------------------------------------------------------------------ tempo
try("tempo", function() return string.format("%.2f", R.Master_GetTempo()) end)

say("END=@@NONCE@@")
