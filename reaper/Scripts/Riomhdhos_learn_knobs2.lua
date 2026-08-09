-- Riomhdhos_learn_knobs2.lua
-- Second pass: K3 (reverb) and K8 (mood volume).
--
-- Both stay inside FX parameters, which we now know are scriptable, instead of
-- send/track volume which would need a different mechanism:
--   K3 / CC89  -> IR-L wet-dry on the REVERB bus. ONE binding, global. Harmless,
--                 because only one mood is ever audible.
--   K8 / CC117 -> Kramer Tape "Playback Level" on each mood track: a gain stage
--                 already in the chain, so no new plugin is needed.
--
-- Run: Actions -> "New action..." -> "Load ReaScript..." -> this file -> Run.
-- Re-running is harmless. Logs to C:\Users\mccul\rig\learn_log2.txt.

------------------------------------------------------------------ config
local MOOD_KEYS = { "COSMOS", "CAIRN", "IRE", "DEEP" }
local REVERB_KEY = "REVERB"
local MIDI_CHAN = 1
local LOGFILE   = "C:\\Users\\mccul\\rig\\learn_log2.txt"

-- K8 -> mood volume, on every mood track
local K8 = { label = "K8 volume", cc = 117, fx = "Kramer Tape",
             paramHints = { "PLAYBACKLEVEL", "OUTPUT", "PLAYBACK" } }

-- K3 -> reverb amount, once on the bus
local K3 = { label = "K3 reverb", cc = 89, fx = "IR-L",
             paramHints = { "WETDRY", "DRYWET", "WET", "MIX", "REVERB", "DIRECT" } }
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

local function findFX(tr, needle)
  local want = norm(needle)
  for i = 0, reaper.TrackFX_GetCount(tr) - 1 do
    local ok, nm = reaper.TrackFX_GetFXName(tr, i, "")
    if ok and norm(nm):find(want, 1, true) then return i, nm end
  end
end

local function paramNames(tr, fx)
  local n = reaper.TrackFX_GetNumParams(tr, fx)
  local names = {}
  for p = 0, n - 1 do
    local ok, pn = reaper.TrackFX_GetParamName(tr, fx, p, "")
    names[p] = ok and pn or "?"
  end
  return names, n
end

local function findParam(names, n, hints)
  for _, hint in ipairs(hints) do
    for p = 0, n - 1 do
      if norm(names[p]):find(hint, 1, true) then return p, names[p] end
    end
  end
end

local function setLearn(tr, fx, param, cc)
  local b1 = 0xB0 + (MIDI_CHAN - 1)
  reaper.TrackFX_SetNamedConfigParm(tr, fx, "param."..param..".learn.midi1", tostring(b1))
  reaper.TrackFX_SetNamedConfigParm(tr, fx, "param."..param..".learn.midi2", tostring(cc))
  local r1, v1 = reaper.TrackFX_GetNamedConfigParm(tr, fx, "param."..param..".learn.midi1")
  local r2, v2 = reaper.TrackFX_GetNamedConfigParm(tr, fx, "param."..param..".learn.midi2")
  return (r1 and v1 or "?"), (r2 and v2 or "?")
end

-- Bind `spec` on one track. `dump` = print the full param list first.
local function bind(tr, spec, dump)
  local tn = trackName(tr)
  local fxi, fxn = findFX(tr, spec.fx)
  if not fxi then
    say(string.format("  %-11s FX '%s' not on '%s' - skipped", spec.label, spec.fx, tn))
    return
  end
  local names, count = paramNames(tr, fxi)
  if dump then
    say(string.format("  [%s has %d params]", fxn, count))
    for i = 0, math.min(count - 1, 19) do
      say(string.format("      %2d  %s", i, names[i]))
    end
    if count > 20 then say("      ... (" .. (count - 20) .. " more)") end
  end
  local p, pn = findParam(names, count, spec.paramHints)
  if not p then
    say(string.format("  %-11s no param matched %s on '%s' - ASSIGN BY HAND",
        spec.label, table.concat(spec.paramHints, "/"), tn))
    return
  end
  local rb1, rb2 = setLearn(tr, fxi, p, spec.cc)
  say(string.format("  %-11s CC%-3d -> param %2d '%s'   readback=%s/%s",
      spec.label, spec.cc, p, pn, rb1, rb2))
end

------------------------------------------------------------------
say("=== Riomhdhos_learn_knobs2 ===")
say("REAPER " .. tostring(reaper.GetAppVersion()))
say("")

local moodTracks, reverbBus = {}, nil
for i = 0, reaper.CountTracks(0) - 1 do
  local tr = reaper.GetTrack(0, i)
  local n = norm(trackName(tr))
  if not reverbBus and n:find(REVERB_KEY, 1, true) then reverbBus = tr end
  for _, k in ipairs(MOOD_KEYS) do
    if n:find(k, 1, true) then moodTracks[#moodTracks+1] = tr; break end
  end
end

say("Mood tracks: " .. #moodTracks)
say("Reverb bus : " .. (reverbBus and ("'" .. trackName(reverbBus) .. "'") or "NOT FOUND"))
say("")

reaper.Undo_BeginBlock()

say("K8 -> mood volume (Kramer Tape playback level):")
local first = true
for _, tr in ipairs(moodTracks) do
  say("  --- " .. trackName(tr))
  bind(tr, K8, first)
  first = false
end
say("")

if reverbBus then
  say("K3 -> reverb amount (IR-L on the bus, one global binding):")
  bind(reverbBus, K3, true)
else
  say("No REVERB bus - K3 not bound.")
end
say("")

reaper.Undo_EndBlock("Riomhdhos: bind K3 + K8", -1)

say("readback should show 176/<cc>. If it shows '?' the binding did not take.")
say("Then Ctrl+S - these live in the .rpp and are lost on a crash otherwise.")
say("=== end ===")

reaper.ShowConsoleMsg(table.concat(log, "\n") .. "\n")
