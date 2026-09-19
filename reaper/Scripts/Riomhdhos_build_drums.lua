-- Riomhdhos_build_drums.lua
-- Builds the DRUMS track: the `drumseq` JSFX first, then eight ReaSamplOmatic5000
-- instances behind it, one per voice, each pinned to a single MIDI note.
--
-- Run: Actions -> Show action list -> ReaScript: Load... -> pick this file -> Run.
-- SAFE TO RUN REPEATEDLY. Every step checks first and skips what already exists, so a
-- re-run after fixing a path fills in only what is missing.
--
-- Writes a full transcript to LOGFILE every run, the same way `Riomhdhos_add_fx.lua`
-- does, so an error is recoverable even if the console window is lost.
--
--------------------------------------------------------------------------------
-- ⚠️ WHY ReaSamplOmatic5000 AND NOT KONTAKT
--
-- `drumseq` plays EIGHT CONSECUTIVE NOTES - voice v fires `Voice 1 note + v`, so the
-- default is 36..43 and nothing else. No sampled drum library maps that way. General
-- MIDI scatters a kit (36 kick, 38 snare, 42 closed hat, 46 open hat, 45/47/48 toms,
-- 49 crash) and every Kontakt library then arranges itself on top of that. One RS5k per
-- voice with its note range pinned to a single note means the LAYOUT IS CHOSEN HERE
-- rather than negotiated with whatever library happens to be loaded.
--
-- Everything the rig has learned about Kontakt argues the same way, and all of it is
-- written down in reaper/README.md:
--   * Kontakt assigns host-automation IDs consecutively as instruments publish them,
--     and the count varies by library (Play Series 64, Uilleann Pipes 40) - so nothing
--     can address a voice by index.
--   * Its state chunk is compressed - 557 KB with no readable strings - so a script
--     cannot tell what is loaded.
--   * It exposes no per-instrument mute, and its formatted parameter values lag one
--     write behind.
-- Eight RS5k instances have none of that: eight identical, named, host-visible
-- parameter sets, so the Lua bridge can reach any voice with no boundary detection at
-- all, and eight separate Volume parameters give a drum mixer for free without the
-- Kontakt output-splitting step that is still gated behind a switch on each layer mixer.
--
-- RS5k is also STOCK. It is in the plugin list of any REAPER install, so it cannot go
-- missing, time out an authorisation or stall a headless machine mid-set the way a
-- licensed sampler can. Verified present on this machine in
-- %APPDATA%\REAPER\reaper-vstplugins64.ini:
--     reasamplomatic.dll=...,ReaSamplOmatic5000 (Cockos)!!!VSTi
--
-- ⚠️ NOT A PURE DATA PATCH EITHER, despite the repo being full of them. reaper/README.md
-- states it plainly: "The Pd patches in the parent directory are the earlier Raspberry
-- Pi rig." Pd has no path to gmem, which is how the whole Push surface talks to itself,
-- so a Pd drum machine would be a second rig running beside this one rather than part
-- of it.
--
-- Kontakt still has a place here, just not as the primary: D:\KONTAKT holds
-- "Spitfire Audio HZ01 Hans Zimmer Percussion London Ensembles" for big cinematic hits.
-- Put that on a SECOND drums track fed by its own `drumseq` if you want it - and note
-- that "Wavesfactory - Old Tape Drums" is already assigned to THE CAIRN in
-- build_moods.lua, so using it here would load it twice.
--------------------------------------------------------------------------------

------------------------------------------------------------------ config
local TRACK_NAME = "DRUMS"
local LOGFILE    = "C:\\Users\\mccul\\rig\\build_drums_log.txt"

-- ⚠️ THIS PATH IS MACHINE-SPECIFIC AND IS NOT ASSUMED TO EXIST. It was read off this
-- workstation's D: drive; `build_moods.lua` points at C:\KONTAKT, so the live rig box
-- evidently lays its libraries out differently. Every file below is checked before it
-- is used, and a missing one is reported by name rather than silently loading nothing.
local KIT_ROOT = "D:\\Sample Library\\kits\\TR808"

-- Voice 1 is the TOP row of pads, matching the instrument rack, the mixer view and the
-- sequencer grid - "top" means the same thing everywhere on this panel. That puts the
-- kick at the top rather than at the bottom where a drum machine would normally put it;
-- one consistent reading order is worth more than one familiar exception.
--
-- `note` must stay CONSECUTIVE and must start at `drumseq`'s "Voice 1 note" slider
-- (default 36). Changing one without the other silently detunes the whole kit.
--
-- Other kits verified present on this disk, if the 808 is the wrong flavour for a
-- folk-drone set - swap KIT_ROOT and the `file` fields together:
--   D:\Sample Library\kits\Roland_TR606      606bass/606snare/606chat/606ohat/606cymbal
--                                            (also ships "acc" variants of every voice)
--   D:\Sample Library\kits\TR909             BT*/CLOP* style names, less obvious
--   D:\Sample Library\kits\EN1783\1646 kicks taiko.wav, taiko (lo).wav, taiko2.wav
--   D:\Sample Library\musicradar-retro-drum-machine-samples\Drums Hits
local VOICES = {
  { note = 36, name = "KICK",   file = "BD\\BD5050.WAV" },
  { note = 37, name = "SNARE",  file = "SD\\SD5050.WAV" },
  { note = 38, name = "HAT",    file = "CH\\CH.WAV"     },
  { note = 39, name = "OPENHAT",file = "OH\\OH50.WAV"   },
  { note = 40, name = "CLAP",   file = "CP\\CP.WAV"     },
  { note = 41, name = "RIM",    file = "RS\\RS.WAV"     },
  { note = 42, name = "COWBELL",file = "CB\\CB.WAV"     },
  { note = 43, name = "CYMBAL", file = "CY\\CY5050.WAV" },
}

-- TrackFX_AddByName has no single spelling that works for both a JSFX and a VST across
-- builds, so the known ones are tried in order and the one that worked is reported -
-- the same approach Riomhdhos_add_fx.lua takes with send destinations.
local SEQ_NAMES = { "Riomhdhos/drumseq", "drumseq", "JS: Riomhdhos drum sequencer" }
local RS5K_NAME = "ReaSamplOmatic5000"
------------------------------------------------------------------

local log = {}
local function say(s)
  log[#log+1] = tostring(s)
  -- flush every line, so even a hard crash leaves the transcript behind
  local f = io.open(LOGFILE, "w")
  if f then f:write(table.concat(log, "\n") .. "\n"); f:close() end
end

local function norm(s) return (tostring(s):upper():gsub("[^A-Z0-9]", "")) end

local function trackName(tr)
  local ok, nm = reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", "", false)
  return ok and nm or "(unnamed)"
end

local function fileExists(p)
  local f = io.open(p, "rb")
  if f then f:close(); return true end
  return false
end

say("=== Riomhdhos_build_drums ===")
say("REAPER " .. tostring(reaper.GetAppVersion()))
say("TrackFX_SetNamedConfigParm available : " .. tostring(reaper.TrackFX_SetNamedConfigParm ~= nil))
say("TrackFX_GetNamedConfigParm available : " .. tostring(reaper.TrackFX_GetNamedConfigParm ~= nil))
say("Kit root : " .. KIT_ROOT)
say("")

------------------------------------------------------------------ sample check first
-- Checked BEFORE anything is created. Building eight empty samplers and only then
-- discovering the kit path is wrong leaves a mess to unpick by hand; finding out first
-- costs nothing and the track is still buildable with the files filled in later.
local missing = 0
for _, v in ipairs(VOICES) do
  local p = KIT_ROOT .. "\\" .. v.file
  if fileExists(p) then
    say(string.format("  ok     %-8s %s", v.name, p))
  else
    say(string.format("  MISSING %-8s %s", v.name, p))
    missing = missing + 1
  end
end
say("")
if missing > 0 then
  say(missing .. " of " .. #VOICES .. " samples not found. The track and the samplers are")
  say("still built - an RS5k with no sample is silent, not broken - but fix KIT_ROOT")
  say("or the per-voice paths above and re-run to fill them in.")
  say("")
end

------------------------------------------------------------------ the track
-- ⚠️ `Riomhdhos_mood_mute.lua` re-asserts the active mood's mute every 30 ms, but it
-- only ever touches tracks whose name matches COSMOS / CAIRN / IRE / DEEP. "DRUMS"
-- matches none of them, so the drums keep playing across a mood change - which is the
-- point of a drum track on a rig where everything else is exclusive.
local drums
for i = 0, reaper.CountTracks(0) - 1 do
  local tr = reaper.GetTrack(0, i)
  if norm(trackName(tr)) == norm(TRACK_NAME) then drums = tr; break end
end

reaper.Undo_BeginBlock()
reaper.PreventUIRefresh(1)

if drums then
  say("Track    : found existing '" .. trackName(drums) .. "'")
else
  local at = reaper.CountTracks(0)
  reaper.InsertTrackAtIndex(at, false)
  drums = reaper.GetTrack(0, at)
  reaper.GetSetMediaTrackInfo_String(drums, "P_NAME", TRACK_NAME, true)
  -- a colour of its own: the four moods are already spoken for in build_moods.lua
  reaper.SetTrackColor(drums, reaper.ColorToNative(190, 150, 90) | 0x1000000)
  say("Track    : created '" .. TRACK_NAME .. "' at index " .. at)
end

------------------------------------------------------------------ helpers
local function findFX(tr, needle)
  local want = norm(needle)
  for i = 0, reaper.TrackFX_GetCount(tr) - 1 do
    local ok, nm = reaper.TrackFX_GetFXName(tr, i, "")
    if ok and norm(nm):find(want, 1, true) then return i end
  end
end

local function addFirstThatWorks(tr, names, pos)
  for _, n in ipairs(names) do
    local ok, idx = pcall(reaper.TrackFX_AddByName, tr, n, false, pos or -1)
    if ok and idx and idx >= 0 then
      say("           added as '" .. n .. "' -> fx index " .. idx)
      return idx
    end
  end
  return -1
end

-- ⚠️ PARAMETERS ARE FOUND BY NAME, NEVER BY INDEX. This is the same rule
-- Riomhdhos_bridge.lua follows, and for the same reason: a plugin's parameter order is
-- its own business and an index that is right today is right by luck.
-- The match is on SHAPE - the normalised name merely has to CONTAIN the fragment - so
-- "Note range start" and "Note range start (semitones)" both hit. Every name actually
-- published is logged for the first voice, so if RS5k ever renames one the transcript
-- says what it is now rather than leaving a silent no-op.
local function setParamByName(tr, fx, fragment, value)
  local want = norm(fragment)
  for p = 0, reaper.TrackFX_GetNumParams(tr, fx) - 1 do
    local ok, pn = reaper.TrackFX_GetParamName(tr, fx, p, "")
    if ok and norm(pn):find(want, 1, true) then
      reaper.TrackFX_SetParamNormalized(tr, fx, p, value)
      return true, pn
    end
  end
  return false
end

------------------------------------------------------------------ the sequencer, first
-- `drumseq` MUST sit ahead of the samplers: it generates the notes they play, and a
-- JSFX only ever feeds what is downstream of it in the chain.
say("")
if findFX(drums, "drum sequencer") or findFX(drums, "drumseq") then
  say("drumseq  : already on the track, left alone")
else
  say("drumseq  : adding at the head of the chain")
  if addFirstThatWorks(drums, SEQ_NAMES, 0) < 0 then
    say("           FAILED - none of " .. table.concat(SEQ_NAMES, " / ") .. " matched.")
    say("           Add it by hand (FX browser -> JS -> Riomhdhos -> drumseq) at slot 1.")
  end
end

------------------------------------------------------------------ eight samplers
say("")
say("Voices   : one ReaSamplOmatic5000 per voice, note range pinned to a single note")
local built, dumped = 0, false
for i, v in ipairs(VOICES) do
  -- An existing RS5k is identified by the note it is already pinned to rather than by
  -- position, so re-running after inserting something by hand cannot double up.
  local have
  for fx = 0, reaper.TrackFX_GetCount(drums) - 1 do
    local ok, nm = reaper.TrackFX_GetFXName(drums, fx, "")
    if ok and norm(nm):find("REASAMPLOMATIC", 1, true) then
      local okr, rt = reaper.TrackFX_GetNamedConfigParm(drums, fx, "renamed_name")
      if okr and rt == (TRACK_NAME .. " " .. v.name) then have = fx; break end
    end
  end

  if have then
    say(string.format("  skip   %-8s note %d, already present at fx %d", v.name, v.note, have))
  else
    local fx = addFirstThatWorks(drums, { RS5K_NAME }, -1)
    if fx < 0 then
      say(string.format("  FAIL   %-8s ReaSamplOmatic5000 not in the plugin list", v.name))
    else
      -- name it, so the chain reads as a kit rather than as eight identical rows
      reaper.TrackFX_SetNamedConfigParm(drums, fx, "renamed_name", TRACK_NAME .. " " .. v.name)

      -- log the real parameter list once, so the names below are evidence and not memory
      if not dumped then
        dumped = true
        say("           RS5k publishes these parameters:")
        for p = 0, reaper.TrackFX_GetNumParams(drums, fx) - 1 do
          local okp, pn = reaper.TrackFX_GetParamName(drums, fx, p, "")
          if okp then say(string.format("             %2d  %s", p, pn)) end
        end
      end

      -- the sample. FILE0 then DONE is RS5k's documented named-config route; the result
      -- is READ BACK rather than assumed, because a silent failure here looks exactly
      -- like a missing file and the two want different fixes.
      local path = KIT_ROOT .. "\\" .. v.file
      if fileExists(path) then
        reaper.TrackFX_SetNamedConfigParm(drums, fx, "FILE0", path)
        reaper.TrackFX_SetNamedConfigParm(drums, fx, "DONE", "")
        local okf, got = reaper.TrackFX_GetNamedConfigParm(drums, fx, "FILE0")
        if okf and got and got ~= "" then
          say(string.format("  ADD    %-8s note %d  <- %s", v.name, v.note, got))
        else
          say(string.format("  ADD    %-8s note %d  but FILE0 read back EMPTY - load", v.name, v.note))
          say("           " .. path .. " by hand into this RS5k.")
        end
      else
        say(string.format("  ADD    %-8s note %d  (no sample: %s)", v.name, v.note, path))
      end

      -- Note range start and end BOTH set to this voice's note, so the sampler answers
      -- to exactly one note and nothing else. Normalised 0..1 over the 0..127 range.
      local n = v.note / 127
      local okS = setParamByName(drums, fx, "noterangestart", n)
      local okE = setParamByName(drums, fx, "noterangeend",   n)
      if not (okS and okE) then
        say(string.format("           WARNING %-8s note range not set (start=%s end=%s)",
                          v.name, tostring(okS), tostring(okE)))
        say("           Set 'Note range' by hand to " .. v.note .. " .. " .. v.note ..
            " - see the parameter list logged above for the real names.")
      end

      -- ⚠️ ONE-SHOTS MUST IGNORE NOTE-OFFS. `drumseq` closes every gate after "Gate
      -- length (% of a step)" and cuts anything still sounding at the next step edge -
      -- sensible for a sustaining voice, fatal for a cymbal, which would be chopped to
      -- the length of a step. With this off the sample always plays to its end and the
      -- gate slider stops mattering for this kit.
      local okO = setParamByName(drums, fx, "obeynoteoff", 0)
      if not okO then
        say("           WARNING " .. v.name .. " 'Obey note-offs' not found - if the")
        say("           samples sound truncated, turn it off by hand in each RS5k.")
      end
      built = built + 1
    end
  end
end

reaper.PreventUIRefresh(-1)
reaper.TrackList_AdjustWindows(false)
reaper.UpdateArrange()
reaper.Undo_EndBlock("Build the DRUMS track", -1)

say("")
say(string.format("=== done: %d sampler voices added ===", built))
say("")
say("On the Push: ACCENT (the button above Octave Up) opens DRUMS.")
say("  pads              row = voice, column = step; voice 1 is the TOP row")
say("  CC102 / CC103     run-stop / clear")
say("  CC36-43           step division, 1/4 .. 1/32T")
say("  lower button row  the eight preset beats")
say("  ACCENT again      accent edit - the same pads then write the accent map")
say("")
say("⚠️ gmem is not saved with the project, so a hand-typed pattern does not survive a")
say("reload. The preset bank in `pushbrain` is what comes back.")

reaper.ShowConsoleMsg(table.concat(log, "\n") .. "\n")
