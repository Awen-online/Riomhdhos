-- Riomhdhos_install_env.lua
-- 1. Puts the input-envelope JSFX FIRST on each live input track and assigns its gmem
--    slot (Zoom [1] -> slot 0, Zoom [2] -> slot 1).
-- 2. Bypasses OneKnob Filter on the mood tracks: the layer mixer now filters per-mood,
--    and leaving both would give one knob two conflicting paths to the same job.
--
-- Bypasses rather than deletes, so nothing you tuned is thrown away and one click
-- undoes the decision.
--
-- Run through the remote console, or Actions -> Load ReaScript.
-- Safe to run repeatedly.

local function norm(s) return (tostring(s):upper():gsub("[^A-Z0-9]", "")) end
local function tname(tr)
  local ok, n = reaper.GetSetMediaTrackInfo_String(tr, "P_NAME", "", false)
  return ok and n or "(unnamed)"
end
local function fxi(tr, frag)
  local want = norm(frag)
  for i = 0, reaper.TrackFX_GetCount(tr) - 1 do
    local ok, n = reaper.TrackFX_GetFXName(tr, i, "")
    if ok and norm(n):find(want, 1, true) then return i, n end
  end
end
local function chain(tr)
  local o = {}
  for i = 0, reaper.TrackFX_GetCount(tr) - 1 do
    local _, n = reaper.TrackFX_GetFXName(tr, i, "")
    o[#o+1] = i .. ":" .. (n:match("^[^%(]*") or "?") .. (reaper.TrackFX_GetEnabled(tr, i) and "" or "[OFF]")
  end
  return table.concat(o, " | ")
end

local out = {}
local function put(s) out[#out+1] = s end

reaper.Undo_BeginBlock()

-- ── inputs ───────────────────────────────────────────────────────────────────
put("=== input envelope followers ===")
local slot = 0
for i = 0, reaper.CountTracks(0) - 1 do
  local tr = reaper.GetTrack(0, i)
  if norm(tname(tr)):find("ZOOM", 1, true) then
    local have = fxi(tr, "input envelope")
    if have then
      put(string.format("  %-10s already present at slot %d", tname(tr), have))
    else
      local idx = reaper.TrackFX_AddByName(tr, "Riomhdhos/inputenv", false, -1)
      if idx and idx >= 0 then
        -- must run FIRST: it should measure the raw input, not the compressed,
        -- reverbed result of the chain below it
        if idx ~= 0 then reaper.TrackFX_CopyToTrack(tr, idx, tr, 0, true); idx = 0 end
        reaper.TrackFX_SetParamNormalized(tr, idx, 0, slot / 7)   -- gmem slot
        put(string.format("  %-10s ADDED at slot 0, gmem slot %d", tname(tr), slot))
      else
        put(string.format("  %-10s FAILED - is the inputenv JSFX deployed?", tname(tr)))
      end
    end
    put("     " .. chain(tr))
    slot = slot + 1
  end
end
put("")

-- ── moods ────────────────────────────────────────────────────────────────────
put("=== retiring the global filter (layermix now filters per-mood) ===")
for _, key in ipairs({ "COSMOS", "CAIRN", "IRE", "DEEP" }) do
  for i = 0, reaper.CountTracks(0) - 1 do
    local tr = reaper.GetTrack(0, i)
    if norm(tname(tr)):find(key, 1, true) then
      local f = fxi(tr, "OneKnob Filter")
      if f then
        if reaper.TrackFX_GetEnabled(tr, f) then
          reaper.TrackFX_SetEnabled(tr, f, false)
          put(string.format("  %-12s OneKnob Filter bypassed (slot %d)", tname(tr), f))
        else
          put(string.format("  %-12s OneKnob Filter already bypassed", tname(tr)))
        end
      else
        put(string.format("  %-12s no OneKnob Filter found", tname(tr)))
      end
      put("     " .. chain(tr))
      break
    end
  end
end

reaper.Undo_EndBlock("Riomhdhos: input envelope + retire global filter", -1)
reaper.Main_OnCommand(40026, 0)
put("")
put("saved")

local text = table.concat(out, "\n")
if say then say(text) else reaper.ShowConsoleMsg(text .. "\n") end
return "install_env done"
