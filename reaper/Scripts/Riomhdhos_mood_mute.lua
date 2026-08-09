-- Riomhdhos_mood_mute.lua
-- Keeps exactly one mood track audible: whichever the MiniLab brain has selected.
-- The brain (JSFX on the CTRL track) publishes the active mood on slider1; this watches
-- that and mutes the others. Live inputs (Zoom), CTRL and the REVERB bus are never touched.
--
-- Runs automatically from Scripts/__startup.lua. It waits for the project to finish loading,
-- so it is safe to start before Kontakt is done. Safe to stop at any time: on exit it unmutes
-- every mood track, so a dead script can never leave you silent on stage.

------------------------------------------------------------------ config
-- Seconds to let a departing mood's release tail ring before the hard mute lands.
-- The brain already sends all-notes-off, so the drone decays naturally over this window.
-- Set to 0 for an instant, hard cut.
local MUTE_DELAY = 2.5

-- mood index (as the brain counts them) -> a distinctive fragment of the track's name.
-- Matched case-insensitively after stripping spaces/punctuation/accents, so "IRE" catches
-- "EIRE" and "ÉIRE" alike.
local MOOD_KEYS = { [0] = "COSMOS", [1] = "CAIRN", [2] = "IRE", [3] = "DEEP" }

local POLL   = 0.03 -- seconds between mood checks once running
local RESCAN = 1.0  -- seconds between attempts to find the brain while waiting
local WARN_AFTER = 180 -- seconds of fruitless searching before saying so in the console
------------------------------------------------------------------

local ctrlTr, ctrlFx, moodParam
local moodTracks = {}   -- [moodIndex] = MediaTrack
local pendingMute = {}  -- [moodIndex] = time_precise() deadline
local lastMood
local nextPoll, nextScan, startedAt, warned = 0, 0, nil, false

local function norm(s)
  return (tostring(s):upper():gsub("[^A-Z0-9]", ""))
end

local function trackName(tr)
  local ok, nm = reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", "", false)
  return ok and nm or ""
end

-- Locate the brain JSFX and its mood parameter, wherever the track sits.
local function findBrain()
  for i = 0, reaper.CountTracks(0) - 1 do
    local tr = reaper.GetTrack(0, i)
    for fx = 0, reaper.TrackFX_GetCount(tr) - 1 do
      local ok, fxName = reaper.TrackFX_GetFXName(tr, fx, "")
      if ok and norm(fxName):find("MINILAB") then
        for p = 0, reaper.TrackFX_GetNumParams(tr, fx) - 1 do
          local ok2, pName = reaper.TrackFX_GetParamName(tr, fx, p, "")
          if ok2 and norm(pName):find("MOOD") then
            return tr, fx, p
          end
        end
      end
    end
  end
end

local function findMoodTracks()
  local found = {}
  for i = 0, reaper.CountTracks(0) - 1 do
    local n = norm(trackName(reaper.GetTrack(0, i)))
    for mood, key in pairs(MOOD_KEYS) do
      if not found[mood] and n:find(key, 1, true) then found[mood] = reaper.GetTrack(0, i) end
    end
  end
  return found
end

local function setMute(tr, on)
  if tr and reaper.ValidatePtr2(0, tr, "MediaTrack*") then
    reaper.SetMediaTrackInfo_Value(tr, "B_MUTE", on and 1 or 0)
  end
end

local function unmuteAll()
  for _, tr in pairs(moodTracks) do setMute(tr, false) end
end

-- Unmute the active mood now; schedule (or immediately apply) the mute on the rest.
local function applyMood(mood, instant)
  local now = reaper.time_precise()
  for m, tr in pairs(moodTracks) do
    if m == mood then
      pendingMute[m] = nil
      setMute(tr, false)
    elseif instant or MUTE_DELAY <= 0 then
      pendingMute[m] = nil
      setMute(tr, true)
    elseif pendingMute[m] == nil
       and reaper.GetMediaTrackInfo_Value(tr, "B_MUTE") == 0 then
      pendingMute[m] = now + MUTE_DELAY
    end
  end
end

local function servicePending(now)
  for m, due in pairs(pendingMute) do
    if now >= due then
      pendingMute[m] = nil
      setMute(moodTracks[m], true)
    end
  end
end

local function resolved()
  return ctrlTr ~= nil and reaper.ValidatePtr2(0, ctrlTr, "MediaTrack*")
end

-- Find the brain and the mood tracks. Returns true once both are in hand.
local function tryResolve(now)
  local tr, fx, p = findBrain()
  if not tr then
    if not warned and (now - startedAt) > WARN_AFTER then
      warned = true
      reaper.ShowConsoleMsg(
        "Riomhdhos_mood_mute: still no MiniLab brain JSFX with a 'mood' parameter.\n" ..
        "The JSFX needs the slider1 mood output - reload it on the CTRL track.\n")
    end
    return false
  end
  local tracks = findMoodTracks()
  if not next(tracks) then return false end

  ctrlTr, ctrlFx, moodParam = tr, fx, p
  moodTracks, pendingMute, lastMood, warned = tracks, {}, nil, false
  return true
end

------------------------------------------------------------------ single-instance guard
-- Two copies would fight over the mutes, so a live heartbeat means we bow out.
local hb = tonumber(reaper.GetExtState("Riomhdhos", "moodmute_hb")) or -1
if hb > 0 and (reaper.time_precise() - hb) < 1.0 then return end

-- Generation counter: bump "moodmute_gen" from anywhere and the running loop exits at
-- its next poll, so an edited copy can take over WITHOUT restarting REAPER. The old
-- version had no way to stand down, which meant every fix waited for a restart.
local MY_GEN = (tonumber(reaper.GetExtState("Riomhdhos", "moodmute_gen")) or 0) + 1
reaper.SetExtState("Riomhdhos", "moodmute_gen", tostring(MY_GEN), false)

startedAt = reaper.time_precise()

reaper.atexit(function()
  reaper.DeleteExtState("Riomhdhos", "moodmute_hb", false)
  unmuteAll() -- never leave the rig muted
end)

------------------------------------------------------------------ main loop
local function loop()
  -- stand down if a newer copy has started
  if (tonumber(reaper.GetExtState("Riomhdhos", "moodmute_gen")) or MY_GEN) > MY_GEN then
    unmuteAll()
    return
  end
  local now = reaper.time_precise()
  reaper.SetExtState("Riomhdhos", "moodmute_hb", tostring(now), false)

  if not resolved() then
    -- Either we have not started yet, or the project changed under us. Drop any stale
    -- state and look again; do not touch mutes until we know what we are looking at.
    ctrlTr, moodTracks, pendingMute, lastMood = nil, {}, {}, nil
    if now >= nextScan then
      nextScan = now + RESCAN
      if tryResolve(now) then nextPoll = 0 end
    end
  else
    if now >= nextPoll then
      nextPoll = now + POLL
      local mood = math.floor(reaper.TrackFX_GetParam(ctrlTr, ctrlFx, moodParam) + 0.5)
      if mood ~= lastMood then
        applyMood(mood, lastMood == nil) -- first pass settles instantly, no phantom tail
        lastMood = mood
      else
        -- Re-assert the active mood every poll. Previously this loop only acted on a
        -- CHANGE, so if anything else muted the live track - a stray click, another
        -- script, a project reload - the rig went silent and stayed silent, because
        -- nothing was watching. Observed 2026-08-09: all four moods muted at once
        -- with the brain reporting mood 2, and it never recovered on its own.
        -- Only the ACTIVE mood is touched, so scheduled release tails are undisturbed.
        local tr = moodTracks[mood]
        if tr and reaper.ValidatePtr2(0, tr, "MediaTrack*")
           and pendingMute[mood] == nil
           and reaper.GetMediaTrackInfo_Value(tr, "B_MUTE") ~= 0 then
          setMute(tr, false)
        end
      end
    end
    servicePending(now)
  end

  reaper.defer(loop)
end

loop()
