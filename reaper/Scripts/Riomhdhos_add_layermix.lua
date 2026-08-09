-- Riomhdhos_add_layermix.lua
-- Installs the layer mixer on each mood track:
--   1. widens the track to 8 channels (Kontakt inst 1-4 -> output pairs 1-4)
--   2. inserts the layermix JSFX immediately AFTER Kontakt, before filter/tape
--
-- The mixer must sit before the filter and tape, because those are stereo and would
-- only ever see channels 1-2. It reads CC20-23 out of the track MIDI stream itself,
-- so there is no MIDI learn to do and nothing to lose in a crash.
--
-- Run: Actions -> "New action..." -> "Load ReaScript..." -> this file -> Run.
-- SAFE TO RUN REPEATEDLY. Logs to C:\Users\mccul\rig\layermix_log.txt.
--
-- STILL YOURS TO DO AFTERWARDS, inside each mood's Kontakt:
--   Outputs page -> preset to a multi-stereo config (creates st.1 .. st.4+)
--   then set each instrument's Output: inst 1 -> st.1, 2 -> st.2, 3 -> st.3, 4 -> st.4

------------------------------------------------------------------ config
local MOOD_KEYS = { "COSMOS", "CAIRN", "IRE", "DEEP" }
local JSFX      = "Riomhdhos/layermix"   -- path under the Effects folder
local CHANS     = 8
local LOGFILE   = "C:\\Users\\mccul\\rig\\layermix_log.txt"
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

say("=== Riomhdhos_add_layermix ===")
say("REAPER " .. tostring(reaper.GetAppVersion()))
say("")

local moods = {}
for i = 0, reaper.CountTracks(0) - 1 do
  local tr = reaper.GetTrack(0, i)
  local n = norm(trackName(tr))
  for _, k in ipairs(MOOD_KEYS) do
    if n:find(k, 1, true) then moods[#moods+1] = tr; break end
  end
end
say("Mood tracks: " .. #moods)
say("")

if #moods == 0 then
  say("No mood tracks matched. Nothing done.")
  reaper.ShowConsoleMsg(table.concat(log, "\n") .. "\n")
  return
end

reaper.Undo_BeginBlock()
reaper.PreventUIRefresh(1)

for _, tr in ipairs(moods) do
  local tn = trackName(tr)
  say("--- " .. tn)

  -- 1. channel count
  local had = reaper.GetMediaTrackInfo_Value(tr, "I_NCHAN")
  if had < CHANS then
    reaper.SetMediaTrackInfo_Value(tr, "I_NCHAN", CHANS)
    say(string.format("  channels %d -> %d", had, CHANS))
  else
    say(string.format("  channels already %d", had))
  end

  -- 2. the mixer itself
  local mix = findFX(tr, "layermix")
  if mix then
    say(string.format("  layermix already present at slot %d", mix))
  else
    local idx = reaper.TrackFX_AddByName(tr, JSFX, false, -1)
    if idx and idx >= 0 then
      say(string.format("  ADD    layermix at slot %d", idx))
      mix = idx
    else
      say("  ERROR  could not add " .. JSFX .. " - is the JSFX deployed?")
    end
  end

  -- 3. position it directly after Kontakt (only if we have a mixer to position)
  if mix then
    local kon = findFX(tr, "Kontakt")
    if kon then
      local want = kon + 1
      if mix ~= want then
        -- CopyToTrack with ismove=true reorders within the same track
        reaper.TrackFX_CopyToTrack(tr, mix, tr, want, true)
        say(string.format("  MOVE   layermix %d -> %d (Kontakt is at %d)", mix, want, kon))
      else
        say(string.format("  order  already correct (Kontakt %d, layermix %d)", kon, mix))
      end
    else
      say("  WARN   no Kontakt found on this track - left the mixer where it landed")
    end

    -- report the resulting chain so the order is verifiable
    local chain = {}
    for i = 0, reaper.TrackFX_GetCount(tr) - 1 do
      local ok, nm = reaper.TrackFX_GetFXName(tr, i, "")
      chain[#chain+1] = string.format("%d:%s", i, (nm or "?"):gsub("^%a+3?i?: ", ""):match("^[^%(]*"))
    end
    say("  chain  " .. table.concat(chain, "  |  "))
  end

  say("")
end

reaper.PreventUIRefresh(-1)
reaper.TrackList_AdjustWindows(false)
reaper.UpdateArrange()
reaper.Undo_EndBlock("Riomhdhos: add layer mixer", -1)

say("NEXT - inside each mood's Kontakt (this part cannot be scripted):")
say("  Outputs page -> preset to a multi-stereo config, then per instrument set")
say("  Output: inst1 -> st.1, inst2 -> st.2, inst3 -> st.3, inst4 -> st.4")
say("Until that is done, layer 1 works and layers 2-4 are silent - which is the")
say("correct, safe failure mode, not a bug.")
say("Then Ctrl+S.")
say("=== end ===")

reaper.ShowConsoleMsg(table.concat(log, "\n") .. "\n")
