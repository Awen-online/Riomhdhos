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

-- Push column -> track. Columns 5 and 6 are the live inputs, which is the only place
-- ARM means anything. MUTE is deliberately NOT offered on the mood tracks:
-- Riomhdhos_mood_mute.lua owns those and re-asserts the active mood every 30 ms, so a
-- Push mute there would be silently undone within a frame - worse than not having it.
local COLUMN_TRACK = { "COSMOS", "CAIRN", "IRE", "DEEP", "ZOOM[1]", "ZOOM[2]" }
local MOOD_COLUMNS = { [1]=true, [2]=true, [3]=true, [4]=true }
local POLL       = 0.03
local RESCAN     = 1.0
------------------------------------------------------------------

local pushTr, pushFx, btnParam, cntParam, mstParam, mixColP, mixValP, mixCntP
local lastMixCount = -1
-- Kontakt exposes no per-instrument mute to the host, only each slot's Volume. So a
-- "mute" is: drive that instrument's volume to silence and remember where it was.
-- Keyed mood*4+slot -> the normalised volume it had before muting.
local instPrevVol = {}

local ctrlTr, ctrlFx, moodParam
local sliderParam = {}     -- brain slider index -> its FX param index
local moodTracks  = {}
local lastSent    = {}     -- [sliderIdx] = last value we wrote, so we only write on change
local lastMood
local nextPoll, nextScan, startedAt = 0, 0, nil

local function norm(s) return (tostring(s):upper():gsub("[^A-Z0-9]", "")) end

-- NB: these must live AFTER norm() is declared. They were originally placed with the
-- other locals near the top, where `norm` was still a nil global - Lua resolves a
-- local only from its declaration onward. Pressing a pad threw "attempt to call a nil
-- value", REAPER opened a modal error dialog, and a modal dialog stalls EVERY defer
-- script - the remote console included.
-- Kontakt lays each rack slot out as a 64-parameter block, but the volume is NOT
-- always at the same offset - EIRE's instrument has "Chord" where the Play Series
-- ones have "Volume". Always find it by NAME.
-- Instruments do not agree on what "the volume" is. Play Series ones expose a single
-- `Volume`; the family used on THE CAIRN and ELIRE exposes `Vol A` and `Vol B` for its
-- two layers and no master at all. So collect every volume-ish parameter in the slot
-- and drive them together - muting only the first would leave half the instrument
-- sounding.
local function slotVolumeParams(tr, kfx, slot)
  local base, exact, pairAB = slot * 64, nil, {}
  for off = 0, 63 do
    local ok, pn = reaper.TrackFX_GetParamName(tr, kfx, base + off, "")
    if ok and pn and pn ~= "" then
      if pn == "Volume" then exact = base + off end
      if pn == "Vol A" or pn == "Vol B" then pairAB[#pairAB+1] = base + off end
    end
  end
  if exact then return { exact } end
  if #pairAB > 0 then return pairAB end
  return {}
end

local function kontaktOfStrict(tr)
  for fx = 0, reaper.TrackFX_GetCount(tr) - 1 do
    local ok, nm = reaper.TrackFX_GetFXName(tr, fx, "")
    if ok and norm(nm):find("KONTAKT") and not norm(nm):find("LAYERMIXER") then return fx end
  end
end
local lastMaster = -1
local lastBtnCount = -1

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

-- Never let a control surface write silence. On 2026-08-09 both live inputs and two
-- mood tracks were found at D_VOL = 0 after stray pad presses, twice, and each time it
-- presented as "the rig stopped making sound" rather than as an obvious cause. Mute
-- already exists as a visible, reversible control; a volume of exactly zero is almost
-- always an accident.
local MIN_SURFACE_VOL = 0.02          -- about -34 dB

local function setTrackVolSafe(tr, v)
  reaper.SetMediaTrackInfo_Value(tr, "D_VOL", math.max(MIN_SURFACE_VOL, v))
end

local function trackByFragment(frag)
  local want = norm(frag)
  for i = 0, reaper.CountTracks(0) - 1 do
    local tr = reaper.GetTrack(0, i)
    if norm(trackName(tr)):find(want, 1, true) then return tr end
  end
end

-- find the Push brain and the two sliders it publishes button presses on
local function findPush()
  for i = 0, reaper.CountTracks(0) - 1 do
    local tr = reaper.GetTrack(0, i)
    for fx = 0, reaper.TrackFX_GetCount(tr) - 1 do
      local ok, n = reaper.TrackFX_GetFXName(tr, fx, "")
      if ok and norm(n):find("PUSHBRAIN") then
        local b, c
        for pp = 0, reaper.TrackFX_GetNumParams(tr, fx) - 1 do
          local ok2, pn = reaper.TrackFX_GetParamName(tr, fx, pp, "")
          if ok2 then
            if norm(pn) == "LASTBUTTON" then b = pp end
            if norm(pn) == "BUTTONPRESSES" then c = pp end
            if norm(pn) == "MASTERENCODER" then mstParam = pp end
            if norm(pn) == "MIXERCOLUMNTOUCHED" then mixColP = pp end
            if norm(pn) == "MIXERVALUE" then mixValP = pp end
            if norm(pn) == "MIXEREVENTS" then mixCntP = pp end
          end
        end
        if b and c then return tr, fx, b, c end
      end
    end
  end
end

-- act on one button press.
-- PHYSICAL ROWS: row 0 = upper (CC102-109) = ARM, row 1 = lower (CC20-27) = MUTE.
-- The Push's button LEDs are fixed colours by position: the upper row is red, the
-- lower row white/grey. Arm on red, mute on white, which is what those rows look like.
local function handleButton(code)
  if code < 1 then return end
  local idx  = code - 1
  local row  = math.floor(idx / 8)
  local col  = (idx % 8) + 1
  local name = COLUMN_TRACK[col]
  if not name then return end
  local tr = trackByFragment(name)
  if not tr then return end

  if row == 0 then
    local now = reaper.GetMediaTrackInfo_Value(tr, "I_RECARM")
    reaper.SetMediaTrackInfo_Value(tr, "I_RECARM", now > 0.5 and 0 or 1)
  elseif MOOD_COLUMNS[col] then
    -- a mood is muted through its layer mixer, NOT the track mute: mood_mute owns
    -- B_MUTE and re-asserts the active mood every 30 ms, so a track mute here would
    -- be undone within a frame.
    for fx = 0, reaper.TrackFX_GetCount(tr) - 1 do
      local ok, n = reaper.TrackFX_GetFXName(tr, fx, "")
      if ok and norm(n):find("LAYERMIXER") then
        local np = reaper.TrackFX_GetNumParams(tr, fx)
        if np > 12 then
          local now = reaper.TrackFX_GetParam(tr, fx, 12)
          reaper.TrackFX_SetParam(tr, fx, 12, now > 0.5 and 0 or 1)
        end
      end
    end
  else
    local now = reaper.GetMediaTrackInfo_Value(tr, "B_MUTE")
    reaper.SetMediaTrackInfo_Value(tr, "B_MUTE", now > 0.5 and 0 or 1)
  end
end

-- publish arm/mute state so pushled can light the buttons
local function publishStates()
  local led, ledFx
  for i = 0, reaper.CountTracks(0) - 1 do
    local tr = reaper.GetTrack(0, i)
    for fx = 0, reaper.TrackFX_GetCount(tr) - 1 do
      local ok, n = reaper.TrackFX_GetFXName(tr, fx, "")
      if ok and norm(n):find("PUSHLEDS") then led, ledFx = tr, fx end
    end
  end
  if not led then return end
  local armed, muted = 0, 0
  for col, name in ipairs(COLUMN_TRACK) do
    local tr = trackByFragment(name)
    if tr then
      if reaper.GetMediaTrackInfo_Value(tr, "I_RECARM") > 0.5 then armed = armed + 2^(col-1) end
      -- The light shows whether the track is AUDIBLE, not merely whether the user
      -- muted it. A mood can be silent for two unrelated reasons: mood_mute has it
      -- muted for not being the active world, or it was muted from the Push. Showing
      -- only the second made three of four moods look live when they were silent.
      -- Consequence worth knowing: three mood lights will always be on, because only
      -- one mood is ever audible.
      local isMuted = reaper.GetMediaTrackInfo_Value(tr, "B_MUTE") > 0.5
      if MOOD_COLUMNS[col] and not isMuted then
        for fx = 0, reaper.TrackFX_GetCount(tr) - 1 do
          local ok, n = reaper.TrackFX_GetFXName(tr, fx, "")
          if ok and norm(n):find("LAYERMIXER") and reaper.TrackFX_GetNumParams(tr, fx) > 12 then
            isMuted = reaper.TrackFX_GetParam(tr, fx, 12) > 0.5
          end
        end
      end
      if isMuted then muted = muted + 2^(col-1) end
    end
  end
  local np = reaper.TrackFX_GetNumParams(led, ledFx)
  if np > 12 then reaper.TrackFX_SetParam(led, ledFx, 11, armed) end
  if np > 13 then reaper.TrackFX_SetParam(led, ledFx, 12, muted) end
  -- How many Kontakt slots each mood actually has loaded. Kontakt exposes each rack
  -- slot as a 64-parameter block, so a populated slot has a real name at its base
  -- index and an empty one does not. Lets the Push show empty slots as dark rather
  -- than lit-but-silent.
  for col = 1, 4 do
    local tr = trackByFragment(COLUMN_TRACK[col])
    local idx = 15 + col
    if tr and np > idx then
      -- Match the INSTRUMENT, not merely anything mentioning Kontakt. The layer
      -- mixer's own description said "4 Kontakt layers", so a substring match found
      -- it too - and because this loop kept the last match rather than the first, it
      -- counted the mixer's parameters instead of the instrument's.
      local kfx, n = nil, 0
      for fx = 0, reaper.TrackFX_GetCount(tr) - 1 do
        local ok, nm = reaper.TrackFX_GetFXName(tr, fx, "")
        if ok and norm(nm):find("KONTAKT") and not norm(nm):find("LAYERMIXER") then
          kfx = fx
          break
        end
      end
      if kfx then
        for slot = 0, 3 do
          local ok2, pn = reaper.TrackFX_GetParamName(tr, kfx, slot * 64, "")
          if ok2 and pn ~= "" and not pn:match("^#%d") and not pn:match("^[Pp]ar%s*%d") then
            n = n + 1
          end
        end
      end
      reaper.TrackFX_SetParam(led, ledFx, idx, n)
    end
  end

  -- which instruments are currently muted, one bit per mood*4+slot
  local imask = 0
  for k, _ in pairs(instPrevVol) do imask = imask + 2^k end
  if np > 24 then reaper.TrackFX_SetParam(led, ledFx, 24, imask) end
end

------------------------------------------------------------------ single instance
local hb = tonumber(reaper.GetExtState("Riomhdhos", "bridge_hb")) or -1
if hb > 0 and (reaper.time_precise() - hb) < 1.0 then return end
startedAt = reaper.time_precise()

reaper.atexit(function()
  reaper.DeleteExtState("Riomhdhos", "bridge_hb", false)
end)

------------------------------------------------------------------ main loop
-- Everything the loop does is wrapped. An uncaught error in a defer script raises a
-- MODAL dialog, and a modal dialog stalls EVERY defer script in the session - on
-- 2026-08-09 one nil call here took down the mood watchdog and the remote console with
-- it, leaving no way to diagnose remotely. A logged failure that keeps running is
-- strictly better than a correct-looking script that can halt the rig.
local errCount, lastErr = 0, ""

local function body()
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

    -- Push buttons: act only when the counter moves, so a held button fires once
    if not pushTr or not reaper.ValidatePtr2(0, pushTr, "MediaTrack*") then
      pushTr, pushFx, btnParam, cntParam = findPush()
      lastBtnCount = -1
    end
    if pushTr then
      local c = reaper.TrackFX_GetParam(pushTr, pushFx, cntParam)
      if lastBtnCount >= 0 and c ~= lastBtnCount then
        handleButton(math.floor(reaper.TrackFX_GetParam(pushTr, pushFx, btnParam) + 0.5))
      end
      lastBtnCount = c

      -- 9th knob -> master volume. Square law so the travel feels even, capped at
      -- unity: this is a performance level control, not somewhere to add gain.
      if mstParam then
        local v = reaper.TrackFX_GetParam(pushTr, pushFx, mstParam)
        if v ~= lastMaster then
          lastMaster = v
          local f = v / 127
          reaper.SetMediaTrackInfo_Value(reaper.GetMasterTrack(0), "D_VOL", f * f)
        end
      end

      -- MIXER VIEW: a pad press or encoder move sets that column's track volume.
      -- Square law, capped at unity - same curve as the master, and this sits above a
      -- limiter, so there is nothing to gain by allowing more than 0 dB.
      if mixCntP then
        local mc = reaper.TrackFX_GetParam(pushTr, pushFx, mixCntP)
        if lastMixCount >= 0 and mc ~= lastMixCount then
          local code = math.floor(reaper.TrackFX_GetParam(pushTr, pushFx, mixColP) + 0.5)
          if code >= 1 and code <= 16 then
            local idx  = code - 1
            local mood = math.floor(idx / 4)
            local slot = idx % 4
            local tr   = trackByFragment(COLUMN_TRACK[mood + 1])
            local kfx  = tr and kontaktOfStrict(tr)
            if kfx then
              local vps = slotVolumeParams(tr, kfx, slot)
              if #vps > 0 then
                local key = idx
                local nowMuted
                if instPrevVol[key] then
                  for _, e in ipairs(instPrevVol[key]) do
                    reaper.TrackFX_SetParamNormalized(tr, kfx, e.p, e.v)
                  end
                  instPrevVol[key] = nil
                  nowMuted = 0
                else
                  local saved = {}
                  for _, vp in ipairs(vps) do
                    saved[#saved+1] = { p = vp, v = reaper.TrackFX_GetParamNormalized(tr, kfx, vp) }
                    reaper.TrackFX_SetParamNormalized(tr, kfx, vp, 0)
                  end
                  instPrevVol[key] = saved
                  nowMuted = 1
                end
                -- announce it on the Push display
                local led2, ledFx2
                for i2 = 0, reaper.CountTracks(0) - 1 do
                  local t2 = reaper.GetTrack(0, i2)
                  for f2 = 0, reaper.TrackFX_GetCount(t2) - 1 do
                    local ok3, n3 = reaper.TrackFX_GetFXName(t2, f2, "")
                    if ok3 and norm(n3):find("PUSHLEDS") then led2, ledFx2 = t2, f2 end
                  end
                end
                if led2 and reaper.TrackFX_GetNumParams(led2, ledFx2) > 27 then
                  reaper.TrackFX_SetParam(led2, ledFx2, 25, code)
                  reaper.TrackFX_SetParam(led2, ledFx2, 26, nowMuted)
                  reaper.TrackFX_SetParam(led2, ledFx2, 27,
                    reaper.TrackFX_GetParam(led2, ledFx2, 27) + 1)
                end
              end
            end
          end
        end
        lastMixCount = mc
      end

      publishStates()
    end

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

end

local function loop()
  local ok, err = pcall(body)
  if not ok then
    errCount = errCount + 1
    if err ~= lastErr then
      lastErr = err
      reaper.SetExtState("Riomhdhos", "bridge_err", tostring(err), false)
    end
    reaper.SetExtState("Riomhdhos", "bridge_errcount", tostring(errCount), false)
  end
  reaper.defer(loop)
end

loop()
