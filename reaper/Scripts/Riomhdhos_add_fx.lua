-- Riomhdhos_add_fx.lua
-- Phase 2 + 3: per-mood effects chain on each mood track, one shared convolution
-- reverb on the REVERB bus.
--
-- Run: Actions -> Show action list -> (already imported) -> Run.
-- SAFE TO RUN REPEATEDLY. Every step checks first and skips what already exists.
--
-- Writes a full transcript to C:\Users\mccul\rig\add_fx_log.txt every run, so an
-- error is recoverable even if the console window is lost. Each phase is wrapped
-- separately: a failure in one does not prevent the others from running.

------------------------------------------------------------------ config
local MOOD_FX = {
  "OneKnob Filter Stereo (Waves)",   -- K1 / CC24 -> one knob sweeps LP -> HP
  "Kramer Tape Stereo (Waves)",      -- K2 / CC25 -> tape warmth + gentle flutter
}
local BUS_FX     = "IR-L efficient Stereo (Waves)"
local MOOD_KEYS  = { "COSMOS", "CAIRN", "IRE", "DEEP" }
local REVERB_KEY = "REVERB"
local MAKE_SENDS = true
local LOGFILE    = "C:\\Users\\mccul\\rig\\add_fx_log.txt"
------------------------------------------------------------------

local log = {}
local function say(s)
  log[#log+1] = tostring(s)
  -- flush every line, so even a hard crash leaves the transcript behind
  local f = io.open(LOGFILE, "w")
  if f then f:write(table.concat(log, "\n") .. "\n"); f:close() end
end

say("=== Riomhdhos_add_fx ===")
say("REAPER " .. tostring(reaper.GetAppVersion()))

local function norm(s) return (tostring(s):upper():gsub("[^A-Z0-9]", "")) end

local function trackName(tr)
  local ok, nm = reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", "", false)
  return ok and nm or "(unnamed)"
end

local function short(name) return (name:match("^[^%(]*") or name):gsub("%s+$", "") end

local function findFX(tr, needle)
  local want = norm(needle)
  for i = 0, reaper.TrackFX_GetCount(tr) - 1 do
    local ok, nm = reaper.TrackFX_GetFXName(tr, i, "")
    if ok and norm(nm):find(want, 1, true) then return i end
  end
end

local function ensureFX(tr, name)
  local tn = trackName(tr)
  if findFX(tr, name) then
    say(string.format("  skip   %-20s already on '%s'", short(name), tn)); return
  end
  local ok, idx = pcall(reaper.TrackFX_AddByName, tr, name, false, -1)
  if not ok then
    say(string.format("  ERROR  %-20s AddByName threw: %s", short(name), tostring(idx))); return
  end
  if idx and idx >= 0 then
    say(string.format("  ADD    %-20s -> '%s'", short(name), tn))
  else
    say(string.format("  FAIL   %-20s not found in plugin list (track '%s')", short(name), tn))
  end
end

------------------------------------------------------------------ locate tracks
local moodTracks, reverbBus = {}, nil
for i = 0, reaper.CountTracks(0) - 1 do
  local tr = reaper.GetTrack(0, i)
  local n  = norm(trackName(tr))
  if not reverbBus and n:find(REVERB_KEY, 1, true) then reverbBus = tr end
  for _, key in ipairs(MOOD_KEYS) do
    if n:find(key, 1, true) then moodTracks[#moodTracks+1] = tr; break end
  end
end

say("")
say("Tracks in project : " .. reaper.CountTracks(0))
say("Mood tracks found : " .. #moodTracks)
for _, tr in ipairs(moodTracks) do say("    - " .. trackName(tr)) end
say("Reverb bus        : " .. (reverbBus and ("'" .. trackName(reverbBus) .. "'") or "NOT FOUND"))
say("")

if #moodTracks == 0 then
  say("No mood tracks matched " .. table.concat(MOOD_KEYS, "/") .. " - nothing to do.")
  reaper.ShowConsoleMsg(table.concat(log, "\n") .. "\n")
  return
end

reaper.Undo_BeginBlock()
reaper.PreventUIRefresh(1)

------------------------------------------------------------------ phase 2
local ok2, err2 = pcall(function()
  say("PHASE 2 - per-mood inserts (K1 filter = CC24, K2 warmth = CC25):")
  for _, tr in ipairs(moodTracks) do
    for _, fx in ipairs(MOOD_FX) do ensureFX(tr, fx) end
  end
end)
if not ok2 then say("  PHASE 2 FAILED: " .. tostring(err2)) end
say("")

------------------------------------------------------------------ phase 3a
local ok3, err3 = pcall(function()
  if not reverbBus then
    say("PHASE 3 - skipped, no REVERB bus found."); return
  end
  say("PHASE 3 - shared convolution reverb:")
  ensureFX(reverbBus, BUS_FX)
end)
if not ok3 then say("  PHASE 3 FAILED: " .. tostring(err3)) end
say("")

------------------------------------------------------------------ phase 3b (sends)
-- Reading a send's destination has no single reliable native call across builds,
-- so try the known ones in order and report which worked. If none do, we report
-- the sends we can see and create nothing, rather than risk duplicates.
local function sendDest(src, i)
  local ok, d = pcall(reaper.GetSetTrackSendInfo, src, 0, i, "P_DESTTRACK", nil)
  if ok and d then return d, "GetSetTrackSendInfo" end
  ok, d = pcall(reaper.GetTrackSendInfo_Value, src, 0, i, "P_DESTTRACK")
  if ok and d then return d, "GetTrackSendInfo_Value" end
  return nil, "unavailable"
end

local ok4, err4 = pcall(function()
  if not (reverbBus and MAKE_SENDS) then return end
  say("PHASE 3b - reverb sends (K3 = CC26 rides these):")
  for _, tr in ipairs(moodTracks) do
    local n = reaper.GetTrackNumSends(tr, 0)
    local found, how = false, "n/a"
    for i = 0, n - 1 do
      local d; d, how = sendDest(tr, i)
      if d and d == reverbBus then found = true; break end
    end
    if found then
      say(string.format("  skip   '%s' already sends to the bus (via %s)", trackName(tr), how))
    elseif how == "unavailable" and n > 0 then
      say(string.format("  HOLD   '%s' has %d send(s) but destination is unreadable - "
        .. "not creating one, wire it by hand to avoid a duplicate", trackName(tr), n))
    else
      reaper.CreateTrackSend(tr, reverbBus)
      say(string.format("  ADD    send '%s' -> reverb bus", trackName(tr)))
    end
  end
end)
if not ok4 then say("  PHASE 3b FAILED: " .. tostring(err4)) end
say("")

reaper.PreventUIRefresh(-1)
reaper.TrackList_AdjustWindows(false)
reaper.UpdateArrange()
reaper.Undo_EndBlock("Riomhdhos: add mood FX + reverb bus", -1)

------------------------------------------------------------------ report
say("NEXT, by hand - use REAPER's MIDI learn, not Kontakt's (stores in the .rpp):")
say("  right-click the FX parameter -> 'Set MIDI/OSC learn' -> move the knob")
say("    K1 -> CC24  filter        K2 -> CC25  tape drive")
say("    K3 -> CC26  reverb send   K8 -> CC31  mood volume")
say("  Then Ctrl+S.")
say("=== end ===")

reaper.ShowConsoleMsg(table.concat(log, "\n") .. "\n")
