-- Riomhdhos_learn_knobs.lua
-- Assign MIDI CCs to the per-mood FX parameters programmatically, so the knob
-- mapping does not have to be clicked in 8+ times by hand.
--
-- Run: Actions -> "New action..." dropdown -> "Load ReaScript..." -> this file -> Run.
-- Re-running is harmless: it overwrites the same bindings with the same values.
--
-- Writes everything it sees and does to C:\Users\mccul\rig\learn_log.txt.
--
-- WHY THE HARDWARE CCs AND NOT CC24-31:
--   The brain invents CC24-31 *inside* the track MIDI stream. Kontakt sees those
--   because it reads track MIDI. REAPER's own FX-parameter learn listens to
--   hardware input instead, where only the MiniLab's real CCs exist. So track FX
--   get bound to the raw knob CCs. Consequence: a knob moves that parameter on
--   ALL four mood tracks at once. That is fine audibly (only one mood sounds) but
--   means no per-mood recall on a switch.
--   This script TESTS that assumption and logs the result either way.

------------------------------------------------------------------ config
-- knob -> { hardware CCs (program A, program B), which FX, which param to hunt for }
local ASSIGN = {
  { label = "K1 filter", cc = 86, ccAlt = 74,
    fx = "OneKnob Filter", paramHints = { "FILTER", "FREQ", "CUTOFF", "ONEKNOB" } },
  { label = "K2 warmth", cc = 87, ccAlt = 71,
    fx = "Kramer Tape",   paramHints = { "DRIVE", "FLUX", "GAIN", "SATURAT" } },
}

local MOOD_KEYS = { "COSMOS", "CAIRN", "IRE", "DEEP" }
local MIDI_CHAN = 1          -- MiniLab knobs arrive on channel 1
local LOGFILE   = "C:\\Users\\mccul\\rig\\learn_log.txt"
local DRY_RUN   = false      -- true = report only, change nothing
------------------------------------------------------------------

local log = {}
local function say(s)
  log[#log+1] = tostring(s)
  local f = io.open(LOGFILE, "w")
  if f then f:write(table.concat(log, "\n") .. "\n"); f:close() end
end

local function norm(s) return (tostring(s):upper():gsub("[^A-Z0-9]", "")) end
local function trackName(tr)
  local ok, nm = reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", "", false)
  return ok and nm or "(unnamed)"
end

say("=== Riomhdhos_learn_knobs ===")
say("REAPER " .. tostring(reaper.GetAppVersion()) .. (DRY_RUN and "   [DRY RUN]" or ""))
say("")

-- Does this build support the learn config parms at all?
local apiOK = (reaper.TrackFX_SetNamedConfigParm ~= nil)
             and (reaper.TrackFX_GetNamedConfigParm ~= nil)
say("TrackFX_SetNamedConfigParm available : " .. tostring(reaper.TrackFX_SetNamedConfigParm ~= nil))
say("TrackFX_GetNamedConfigParm available : " .. tostring(reaper.TrackFX_GetNamedConfigParm ~= nil))
if not apiOK then
  say("")
  say("This build cannot set MIDI learn from a script. Assign by hand:")
  say("  right-click the parameter -> 'Set MIDI/OSC learn' -> move the knob.")
  reaper.ShowConsoleMsg(table.concat(log, "\n") .. "\n")
  return
end
say("")

local function findFX(tr, needle)
  local want = norm(needle)
  for i = 0, reaper.TrackFX_GetCount(tr) - 1 do
    local ok, nm = reaper.TrackFX_GetFXName(tr, i, "")
    if ok and norm(nm):find(want, 1, true) then return i, nm end
  end
end

-- Pick the parameter whose name matches a hint. Returns index, name.
local function findParam(tr, fx, hints)
  local n = reaper.TrackFX_GetNumParams(tr, fx)
  local names = {}
  for p = 0, n - 1 do
    local ok, pn = reaper.TrackFX_GetParamName(tr, fx, p, "")
    names[p] = ok and pn or "?"
  end
  for _, hint in ipairs(hints) do
    for p = 0, n - 1 do
      if norm(names[p]):find(hint, 1, true) then return p, names[p], names, n end
    end
  end
  return nil, nil, names, n
end

local function setLearn(tr, fx, param, cc)
  local b1 = 0xB0 + (MIDI_CHAN - 1)
  local ok1 = reaper.TrackFX_SetNamedConfigParm(tr, fx, "param."..param..".learn.midi1", tostring(b1))
  local ok2 = reaper.TrackFX_SetNamedConfigParm(tr, fx, "param."..param..".learn.midi2", tostring(cc))
  -- read back
  local r1, v1 = reaper.TrackFX_GetNamedConfigParm(tr, fx, "param."..param..".learn.midi1")
  local r2, v2 = reaper.TrackFX_GetNamedConfigParm(tr, fx, "param."..param..".learn.midi2")
  return ok1, ok2, (r1 and v1 or "?"), (r2 and v2 or "?")
end

------------------------------------------------------------------ walk the moods
local moodTracks = {}
for i = 0, reaper.CountTracks(0) - 1 do
  local tr = reaper.GetTrack(0, i)
  local n = norm(trackName(tr))
  for _, k in ipairs(MOOD_KEYS) do
    if n:find(k, 1, true) then moodTracks[#moodTracks+1] = tr; break end
  end
end
say("Mood tracks: " .. #moodTracks)
say("")

if #moodTracks == 0 then
  say("No mood tracks matched. Nothing done.")
  reaper.ShowConsoleMsg(table.concat(log, "\n") .. "\n")
  return
end

reaper.Undo_BeginBlock()

local dumped = {}
for _, tr in ipairs(moodTracks) do
  local tn = trackName(tr)
  say("--- " .. tn .. " ---")
  for _, a in ipairs(ASSIGN) do
    local fxi, fxn = findFX(tr, a.fx)
    if not fxi then
      say(string.format("  %-10s FX '%s' not on this track - skipped", a.label, a.fx))
    else
      local p, pn, names, count = findParam(tr, fxi, a.paramHints)
      -- dump the full param list once per plugin, so a bad guess is fixable next run
      if not dumped[a.fx] then
        dumped[a.fx] = true
        say(string.format("  [%s has %d params]", fxn, count))
        for i = 0, math.min(count - 1, 24) do
          say(string.format("      %2d  %s", i, names[i]))
        end
        if count > 25 then say("      ... (" .. (count - 25) .. " more)") end
      end
      if not p then
        say(string.format("  %-10s no param matched %s - ASSIGN BY HAND",
            a.label, table.concat(a.paramHints, "/")))
      elseif DRY_RUN then
        say(string.format("  %-10s would bind CC%d -> param %d '%s'", a.label, a.cc, p, pn))
      else
        local ok1, ok2, rb1, rb2 = setLearn(tr, fxi, p, a.cc)
        say(string.format("  %-10s CC%-3d -> param %2d '%s'   set=%s/%s  readback=%s/%s",
            a.label, a.cc, p, pn, tostring(ok1), tostring(ok2), tostring(rb1), tostring(rb2)))
      end
    end
  end
  say("")
end

reaper.Undo_EndBlock("Riomhdhos: bind knob CCs to mood FX", -1)

say("If readback shows the values back, the binding took - move K1/K2 and listen.")
say("If it shows '?' or empty, this build ignores scripted learn and it must be")
say("done by hand: right-click the param -> 'Set MIDI/OSC learn' -> move the knob.")
say("")
say("NOT scriptable either way: the Kontakt volume learns (CC20-23 -> instrument")
say("slots). Those live inside Kontakt's own state. Do them by hand, then")
say("Files -> Save Multi As -> rig\\multis\\<mood>.nkm so they survive a crash.")
say("=== end ===")

reaper.ShowConsoleMsg(table.concat(log, "\n") .. "\n")
