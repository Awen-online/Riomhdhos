--[[
  build_moods.lua  —  Riomhdhos folk-drone rig
  Builds one Kontakt track per instrument under each mood folder bus.
  ADDITIVE + SAFE: creates new child tracks only; never deletes or edits existing tracks/FX.
  Each child gets: a fresh empty Kontakt 7, and a MIDI receive from CTRL(brain) copying
  that mood's existing MIDI routing (so mood-switching keeps working). Audio flows
  child -> mood folder bus -> (existing REVERB send + master). Load the .nki into each
  Kontakt from the pick-list printed at the end.

  Run once:  Actions > Show action list > ReaScript: Load... > pick this file > Run.
  Re-running is blocked by a guard so it can't duplicate tracks.
]]--

-- channel = the MIDI channel the brain routes this mood on (from the existing project:
-- Cosmos=1, Cairn=2, Éire=3, Deep=4). Used as a guaranteed fallback for MIDI routing.
local MOODS = {
  { parent = "COSMOS",     channel = 1, color = {150,120,225},  -- violet
    instruments = { "Mellotron", "Symphony Strings", "Vintage Strings", "VOXOS Choir", "Symphobia Lumina", "Ambient Minimalism" } },
  { parent = "THE CAIRN",  channel = 2, color = { 90,140,180},  -- cold steel blue (Norse)
    instruments = { "Meditation Ritual", "SHORTNOISE", "Old Tape Drums", "Dark Strings", "Choir (Arva)" } },
  { parent = "ÉIRE",       channel = 3, color = { 90,180,120},  -- green
    instruments = { "Uilleann Pipes", "Whistler", "Free World", "Mellotron Flute", "String Bed" } },
  { parent = "THE DEEP",   channel = 4, color = { 70,150,160},  -- deep teal
    instruments = { "Fraktale", "SHORTNOISE (Deep)", "Risenge", "Analog Tape Synth", "Whispers & Clock" } },
}

local BRAIN  = "CTRL (brain)"

local function log(s) reaper.ShowConsoleMsg(s .. "\n") end

local function findTrackByName(name)
  local n = reaper.CountTracks(0)
  for i = 0, n-1 do
    local tr = reaper.GetTrack(0, i)
    local _, tn = reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", "", false)
    if tn == name then return tr end
  end
  return nil
end

local function trackIndex(tr)  -- 0-based
  return math.floor(reaper.GetMediaTrackInfo_Value(tr, "IP_TRACKNUMBER")) - 1
end

-- Read the MIDI-routing flags of the brain's existing send to a given mood parent.
local function brainMidiFlagsTo(brain, parent)
  local ns = reaper.GetTrackNumSends(brain, 0)  -- 0 = track sends from brain
  for s = 0, ns-1 do
    local dest = reaper.GetTrackSendInfo_Value(brain, 0, s, "P_DESTTRACK")
    if dest == parent then
      return reaper.GetTrackSendInfo_Value(brain, 0, s, "I_MIDIFLAGS")
    end
  end
  return nil
end

reaper.Undo_BeginBlock()
reaper.PreventUIRefresh(1)

-- guard against double-run
if reaper.GetExtState("riomhdhos", "moods_built") == "1" then
  reaper.PreventUIRefresh(-1)
  reaper.ShowMessageBox("Mood tracks were already built by this script (guard flag set).\n"..
    "If you deleted them and want to rebuild, run:\n  reaper.SetExtState('riomhdhos','moods_built','',true)\nfirst, or just add tracks manually.", "build_moods", 0)
  return
end

local brain = findTrackByName(BRAIN)
if not brain then
  reaper.PreventUIRefresh(-1)
  reaper.ShowMessageBox("Could not find the '"..BRAIN.."' track. Aborting.", "build_moods", 0)
  return
end

log("=== build_moods: creating instrument tracks ===")
local created = 0
local guide = {}

for _, mood in ipairs(MOODS) do
  local parent = findTrackByName(mood.parent)
  if not parent then
    log("!! mood parent not found, skipping: " .. mood.parent)
  else
    -- prefer copying the mood's existing MIDI routing; fall back to its known channel
    local flags = brainMidiFlagsTo(brain, parent) or mood.channel
    local col   = reaper.ColorToNative(mood.color[1], mood.color[2], mood.color[3]) | 0x1000000
    reaper.SetTrackColor(parent, col)                      -- tint the bus to match
    local pidx = trackIndex(parent)

    table.insert(guide, "")
    table.insert(guide, "-- " .. mood.parent .. " --")

    for j, instr in ipairs(mood.instruments) do
      local at = pidx + j                                  -- insert right after parent, in order
      reaper.InsertTrackAtIndex(at, false)
      local child = reaper.GetTrack(0, at)
      reaper.GetSetMediaTrackInfo_String(child, "P_NAME", instr, true)
      reaper.SetTrackColor(child, col)
      reaper.SetMediaTrackInfo_Value(child, "I_FOLDERDEPTH", 0)

      -- fresh empty Kontakt 7 (VST3i)
      local fx = reaper.TrackFX_AddByName(child, "Kontakt 7", false, -1)
      if fx < 0 then
        table.insert(guide, "   [" .. instr .. "]  (Kontakt add FAILED — add it manually)")
      else
        table.insert(guide, "   [" .. instr .. "]  Kontakt ready — load its .nki")
      end

      -- MIDI receive from brain, mirroring this mood's routing; audio disabled (MIDI only)
      local si = reaper.CreateTrackSend(brain, child)
      reaper.SetTrackSendInfo_Value(brain, 0, si, "I_SRCCHAN", -1)         -- no audio
      if flags then
        reaper.SetTrackSendInfo_Value(brain, 0, si, "I_MIDIFLAGS", flags)
      end
      created = created + 1
    end

    -- turn the mood track into a folder that closes on its last new child
    reaper.SetMediaTrackInfo_Value(parent, "I_FOLDERDEPTH", 1)
    local lastChild = reaper.GetTrack(0, pidx + #mood.instruments)
    reaper.SetMediaTrackInfo_Value(lastChild, "I_FOLDERDEPTH", -1)
  end
end

reaper.SetExtState("riomhdhos", "moods_built", "1", true)
reaper.PreventUIRefresh(-1)
reaper.TrackList_AdjustWindows(false)
reaper.UpdateArrange()
reaper.Undo_EndBlock("Build mood instrument tracks", -1)

log(string.format("=== done: %d instrument tracks created ===", created))
log("Load these .nki files into each Kontakt (Kontakt Files browser -> C:\\KONTAKT):")
for _, line in ipairs(guide) do log(line) end

reaper.ShowMessageBox(
  created .. " instrument tracks created across the 4 mood folders.\n\n" ..
  "Each has an empty Kontakt 7. Open each Kontakt and load its instrument from\n" ..
  "C:\\KONTAKT (Files tab). See the ReaScript console for the per-track pick-list.\n\n" ..
  "Then save the project (Ctrl+S).", "build_moods — done", 0)
