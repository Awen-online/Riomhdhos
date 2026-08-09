-- Riomhdhos_bridge.lua
-- Points knobs K4-K7 at the ACTIVE mood's Kontakt macros.
--
-- WHY THIS EXISTS: neither of the other two mechanisms can do this job.
--   * REAPER's FX-parameter learn only sees hardware MIDI, so a binding made there is
--     GLOBAL - one knob moves all four moods at once, with no per-mood recall.
--   * A JSFX sees the brain's per-mood CCs but can only affect its own DSP; it cannot
--     reach into Kontakt and move a parameter.
-- A Lua defer loop can do both: read the brain's published state, and write to any
-- plugin. ~30 ms of latency, which is invisible on a texture knob and would be
-- unacceptable on a volume fader - hence volume stays in the JSFX.
--
-- Runs automatically from __startup.lua. Writes nothing unless a knob actually moves,
-- so it will not fight you for control of a parameter you are editing by hand.

------------------------------------------------------------------ config
-- Which Kontakt slot-0 parameter each knob drives. Play Series instruments put their
-- five macro FX at params 0-5; param 0 is skipped because K1 already filters and
-- param 5 is usually Reverb, which K3 handles globally on the bus.
local KNOB_PARAM = { [2] = 1, [3] = 2, [4] = 3, [5] = 4 }  -- brain slider -> Kontakt param

local MOOD_KEYS  = { [0]="COSMOS", [1]="CAIRN", [2]="IRE", [3]="DEEP" }
local POLL       = 0.03
local RESCAN     = 1.0
------------------------------------------------------------------

local ctrlTr, ctrlFx, moodParam
local sliderParam = {}     -- brain slider index -> its FX param index
local moodTracks  = {}
local lastSent    = {}     -- [sliderIdx] = last value we wrote, so we only write on change
local lastMood
local nextPoll, nextScan, startedAt = 0, 0, nil

local function norm(s) return (tostring(s):upper():gsub("[^A-Z0-9]", "")) end

local function trackName(tr)
  local ok, nm = reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", "", false)
  return ok and nm or ""
end

-- Find the brain and note which FX param each of its sliders is.
local function findBrain()
  for i = 0, reaper.CountTracks(0) - 1 do
    local tr = reaper.GetTrack(0, i)
    for fx = 0, reaper.TrackFX_GetCount(tr) - 1 do
      local ok, fxName = reaper.TrackFX_GetFXName(tr, fx, "")
      if ok and norm(fxName):find("MINILAB") then
        local moodP, sliders = nil, {}
        for p = 0, reaper.TrackFX_GetNumParams(tr, fx) - 1 do
          local ok2, pn = reaper.TrackFX_GetParamName(tr, fx, p, "")
          if ok2 then
            local n = norm(pn)
            if n:find("MOOD") then moodP = p end
            for s = 2, 5 do
              if n == ("K" .. (s + 2)) then sliders[s] = p end
            end
          end
        end
        if moodP then return tr, fx, moodP, sliders end
      end
    end
  end
end

local function findMoodTracks()
  local found = {}
  for i = 0, reaper.CountTracks(0) - 1 do
    local tr = reaper.GetTrack(0, i)
    local n = norm(trackName(tr))
    for mood, key in pairs(MOOD_KEYS) do
      if not found[mood] and n:find(key, 1, true) then found[mood] = tr end
    end
  end
  return found
end

local function kontaktOf(tr)
  for i = 0, reaper.TrackFX_GetCount(tr) - 1 do
    local ok, n = reaper.TrackFX_GetFXName(tr, i, "")
    if ok and norm(n):find("KONTAKT") then return i end
  end
end

local function resolved()
  return ctrlTr ~= nil and reaper.ValidatePtr2(0, ctrlTr, "MediaTrack*")
end

------------------------------------------------------------------ single instance
local hb = tonumber(reaper.GetExtState("Riomhdhos", "bridge_hb")) or -1
if hb > 0 and (reaper.time_precise() - hb) < 1.0 then return end
startedAt = reaper.time_precise()

reaper.atexit(function()
  reaper.DeleteExtState("Riomhdhos", "bridge_hb", false)
end)

------------------------------------------------------------------ main loop
local function loop()
  local now = reaper.time_precise()
  reaper.SetExtState("Riomhdhos", "bridge_hb", tostring(now), false)

  if not resolved() then
    ctrlTr, moodTracks, sliderParam, lastSent = nil, {}, {}, {}
    if now >= nextScan then
      nextScan = now + RESCAN
      local tr, fx, mp, sl = findBrain()
      if tr then
        local mt = findMoodTracks()
        if next(mt) then
          ctrlTr, ctrlFx, moodParam, sliderParam = tr, fx, mp, sl
          moodTracks, lastSent, lastMood = mt, {}, nil
        end
      end
    end
  elseif now >= nextPoll then
    nextPoll = now + POLL

    local mood = math.floor(reaper.TrackFX_GetParam(ctrlTr, ctrlFx, moodParam) + 0.5)
    -- a mood change invalidates every cached value: the same knob now addresses a
    -- different instrument, so everything must be re-sent once
    if mood ~= lastMood then lastSent = {}; lastMood = mood end

    local tr = moodTracks[mood]
    if tr and reaper.ValidatePtr2(0, tr, "MediaTrack*") then
      local kfx = kontaktOf(tr)
      if kfx then
        for sIdx, kParam in pairs(KNOB_PARAM) do
          local p = sliderParam[sIdx]
          if p then
            local v = reaper.TrackFX_GetParam(ctrlTr, ctrlFx, p)   -- 0..127 from the brain
            if lastSent[sIdx] ~= v then
              lastSent[sIdx] = v
              if kParam < reaper.TrackFX_GetNumParams(tr, kfx) then
                reaper.TrackFX_SetParamNormalized(tr, kfx, kParam, math.max(0, math.min(1, v / 127)))
              end
            end
          end
        end
      end
    end
  end

  reaper.defer(loop)
end

loop()
