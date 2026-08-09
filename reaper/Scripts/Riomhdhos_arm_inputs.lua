-- Riomhdhos_arm_inputs.lua
-- Arms the live-input tracks (Zoom [1] / Zoom [2]) with input monitoring on, as soon as the
-- project has finished loading. Run automatically from Scripts/__startup.lua.
--
-- Only arm and monitor are touched. The record-input assignment is left exactly as saved in
-- the project, so this cannot silently re-route your guitar or Juno to the wrong jack.

------------------------------------------------------------------ config
local ARM_KEYS = { "ZOOM" } -- track-name fragments, matched case/punctuation-insensitively
local RESCAN   = 1.0        -- seconds between attempts while the project is still loading
local GIVE_UP  = 600        -- stop looking after this long (Kontakt can take minutes)
------------------------------------------------------------------

local startedAt = reaper.time_precise()
local nextScan  = 0

local function norm(s)
  return (tostring(s):upper():gsub("[^A-Z0-9]", ""))
end

local function trackName(tr)
  local ok, nm = reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", "", false)
  return ok and nm or ""
end

local function armInputs()
  local n = 0
  for i = 0, reaper.CountTracks(0) - 1 do
    local tr = reaper.GetTrack(0, i)
    local nm = norm(trackName(tr))
    for _, key in ipairs(ARM_KEYS) do
      if nm:find(key, 1, true) then
        reaper.SetMediaTrackInfo_Value(tr, "I_RECARM", 1)
        reaper.SetMediaTrackInfo_Value(tr, "I_RECMON", 1) -- monitor on
        n = n + 1
        break
      end
    end
  end
  return n
end

local function loop()
  local now = reaper.time_precise()
  if now >= nextScan then
    nextScan = now + RESCAN
    if armInputs() > 0 then return end -- armed; nothing more to do
    if (now - startedAt) > GIVE_UP then
      reaper.ShowConsoleMsg("Riomhdhos_arm_inputs: no track name matched ARM_KEYS; gave up.\n")
      return
    end
  end
  reaper.defer(loop)
end

loop()
