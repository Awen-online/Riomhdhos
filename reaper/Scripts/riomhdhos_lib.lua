-- riomhdhos_lib.lua
-- Helper library auto-loaded into every remote-console snippet. Everything here is
-- a global, so a snippet can just call it.
--
-- Two things earned their place the hard way on 2026-08-08:
--   setdb()  - discovers a parameter's normalised<->dB mapping by measurement before
--              writing, then verifies the readback. A limiter threshold was set to
--              -60 dB because GetParamEx reports 0..1 for some plugins and real units
--              for others; the result was 60 dB of makeup gain and a very loud hiss
--              through the monitors. Never set a dB value blind again.
--   snap()/restore() - capture track gains and FX enable states before a risky change
--              so there is always a way back that does not depend on remembering.

local M = {}

------------------------------------------------------------------ text
function norm(s) return (tostring(s):upper():gsub("[^A-Z0-9]", "")) end

function tname(tr)
  local ok, n = R.GetSetMediaTrackInfo_String(tr, "P_NAME", "", false)
  return ok and n or "(unnamed)"
end

function hr(c) say(string.rep(c or "-", 66)) end

-- aligned column output: row{"a","b","c"} with widths row.w = {12,8,20}
function row(cols, widths)
  local out = {}
  for i, c in ipairs(cols) do
    local w = (widths and widths[i]) or 14
    local s = tostring(c)
    out[#out+1] = (w < 0) and string.format("%" .. (-w) .. "s", s)
                           or string.format("%-" .. w .. "s", s)
  end
  say(table.concat(out, " "))
end

------------------------------------------------------------------ tracks
function tracks()
  local t = {}
  for i = 0, R.CountTracks(0) - 1 do t[#t+1] = R.GetTrack(0, i) end
  return t
end

-- track("cosmos") / track(3) - name fragment is case and punctuation insensitive
function track(key)
  if type(key) == "number" then return R.GetTrack(0, key) end
  local want = norm(key)
  for _, tr in ipairs(tracks()) do
    if norm(tname(tr)):find(want, 1, true) then return tr end
  end
end

-- the four mood tracks, always in brain order: COSMOS, CAIRN, EIRE, DEEP
function moods()
  local keys, out = { "COSMOS", "CAIRN", "IRE", "DEEP" }, {}
  for _, k in ipairs(keys) do out[#out+1] = track(k) end
  return out
end

function master() return R.GetMasterTrack(0) end

------------------------------------------------------------------ fx
function fxi(tr, frag)
  local want = norm(frag)
  for i = 0, R.TrackFX_GetCount(tr) - 1 do
    local ok, n = R.TrackFX_GetFXName(tr, i, "")
    if ok and norm(n):find(want, 1, true) then return i, n end
  end
end

function fxname(tr, i)
  local _, n = R.TrackFX_GetFXName(tr, i, "")
  return n or "?"
end

function chain(tr)
  local out = {}
  for i = 0, R.TrackFX_GetCount(tr) - 1 do
    out[#out+1] = i .. ":" .. (fxname(tr, i):match("^[^%(]*") or "?")
                  .. (R.TrackFX_GetEnabled(tr, i) and "" or "[OFF]")
  end
  return table.concat(out, " | ")
end

-- parameter index by name; exact match wins, then substring
function pidx(tr, fx, name)
  local want, fallback = norm(name), nil
  for p = 0, R.TrackFX_GetNumParams(tr, fx) - 1 do
    local _, pn = R.TrackFX_GetParamName(tr, fx, p, "")
    if pn then
      local n = norm(pn)
      if n == want then return p, pn end
      if not fallback and n:find(want, 1, true) then fallback = { p, pn } end
    end
  end
  if fallback then return fallback[1], fallback[2] end
end

function pshow(tr, fx, p)
  local _, s = R.TrackFX_GetFormattedParamValue(tr, fx, p, "")
  return s
end

------------------------------------------------------------------ safe dB setting
local function numOf(s)
  return tonumber(tostring(s):match("[-+]?%d+%.?%d*"))
end

-- Measure what a parameter actually reads at two normalised points, so we can map
-- a wanted dB value to a normalised one without assuming the plugin's units.
-- Restores the original value before returning. Set opts.bypass to mute the plugin
-- while probing - essential on anything in the monitoring path.
function probe(tr, fx, p, opts)
  opts = opts or {}
  local was = R.TrackFX_GetParamNormalized(tr, fx, p)
  local wasOn = R.TrackFX_GetEnabled(tr, fx)
  if opts.bypass then R.TrackFX_SetEnabled(tr, fx, false) end

  R.TrackFX_SetParamNormalized(tr, fx, p, 0);   local lo = numOf(pshow(tr, fx, p))
  R.TrackFX_SetParamNormalized(tr, fx, p, 1);   local hi = numOf(pshow(tr, fx, p))
  R.TrackFX_SetParamNormalized(tr, fx, p, was)

  if opts.bypass then R.TrackFX_SetEnabled(tr, fx, wasOn) end
  return lo, hi
end

-- Set a parameter to a real-world value (dB or whatever it reports), verified.
-- Returns ok, reading. On a readback outside tolerance it puts the original back
-- rather than leaving the plugin in a state nobody chose.
function setdb(tr, fx, p, want, opts)
  opts = opts or {}
  local tol = opts.tol or 1.0
  local was = R.TrackFX_GetParamNormalized(tr, fx, p)
  local lo, hi = probe(tr, fx, p, opts)
  if not lo or not hi or hi == lo then
    return false, "could not read a numeric range (lo=" .. tostring(lo) .. " hi=" .. tostring(hi) .. ")"
  end
  local n = math.max(0, math.min(1, (want - lo) / (hi - lo)))
  R.TrackFX_SetParamNormalized(tr, fx, p, n)
  local got = pshow(tr, fx, p)
  local gotn = numOf(got)
  if gotn and math.abs(gotn - want) <= tol then
    return true, got
  end
  R.TrackFX_SetParamNormalized(tr, fx, p, was)
  return false, string.format("wanted %+.2f, got %s (range %.1f..%.1f) - REVERTED", want, tostring(got), lo, hi)
end

------------------------------------------------------------------ kontakt
-- Kontakt exposes each rack slot as a 64-parameter block. Returns the populated ones.
function kslots(tr, maxSlots)
  local fx = fxi(tr, "Kontakt")
  if not fx then return {} end
  local out = {}
  for slot = 0, (maxSlots or 11) do
    local base = slot * 64
    local _, p0 = R.TrackFX_GetParamName(tr, fx, base, "")
    if p0 and p0 ~= "" and not p0:match("^#%d") and not p0:match("^[Pp]ar%s*%d") then
      local vp
      for off = 0, 63 do
        local _, pn = R.TrackFX_GetParamName(tr, fx, base + off, "")
        if pn == "Volume" then vp = base + off; break end
      end
      out[#out+1] = { slot = slot, base = base, fx = fx, first = p0, vol = vp }
    end
  end
  return out
end

------------------------------------------------------------------ midi learn
-- Bind a hardware CC to an FX parameter. REAPER's learn only sees hardware input,
-- so pass the RAW controller CC, never the brain's internal CC20-31.
function learn(tr, fx, p, cc, chan)
  local b1 = 0xB0 + ((chan or 1) - 1)
  R.TrackFX_SetNamedConfigParm(tr, fx, "param." .. p .. ".learn.midi1", tostring(b1))
  R.TrackFX_SetNamedConfigParm(tr, fx, "param." .. p .. ".learn.midi2", tostring(cc))
  local _, v1 = R.TrackFX_GetNamedConfigParm(tr, fx, "param." .. p .. ".learn.midi1")
  local _, v2 = R.TrackFX_GetNamedConfigParm(tr, fx, "param." .. p .. ".learn.midi2")
  return (tostring(v1) == tostring(b1) and tostring(v2) == tostring(cc)), v1 .. "/" .. v2
end

------------------------------------------------------------------ snapshot / rollback
-- Capture everything cheap and reversible: track gains, mutes, and FX enable states.
function snap()
  local s = { tracks = {} }
  local all = tracks()
  all[#all+1] = master()
  for _, tr in ipairs(all) do
    local e = {}
    for f = 0, R.TrackFX_GetCount(tr) - 1 do e[f] = R.TrackFX_GetEnabled(tr, f) end
    s.tracks[#s.tracks+1] = {
      tr   = tr,
      name = tname(tr),
      vol  = R.GetMediaTrackInfo_Value(tr, "D_VOL"),
      mute = R.GetMediaTrackInfo_Value(tr, "B_MUTE"),
      fx   = e,
    }
  end
  return s
end

function restore(s)
  local n = 0
  for _, t in ipairs(s.tracks) do
    if R.ValidatePtr2(0, t.tr, "MediaTrack*") then
      R.SetMediaTrackInfo_Value(t.tr, "D_VOL", t.vol)
      R.SetMediaTrackInfo_Value(t.tr, "B_MUTE", t.mute)
      for f, on in pairs(t.fx) do
        if f < R.TrackFX_GetCount(t.tr) then R.TrackFX_SetEnabled(t.tr, f, on) end
      end
      n = n + 1
    end
  end
  return n
end

function snapshow(s)
  for _, t in ipairs(s.tracks) do
    say(string.format("  %-14s vol=%.4f mute=%d", t.name, t.vol, t.mute))
  end
end

------------------------------------------------------------------ housekeeping
function save() R.Main_OnCommand(40026, 0) end

-- undo("label", function() ... end) - groups everything into one undo step
function undo(label, fn)
  R.Undo_BeginBlock()
  local ok, err = pcall(fn)
  R.Undo_EndBlock(label, -1)
  if not ok then say("ERROR in undo block: " .. tostring(err)) end
  return ok
end

function db(v) return (v and v > 0) and (20 * math.log(v, 10)) or -150 end
function amp(d) return 10 ^ (d / 20) end

return M
