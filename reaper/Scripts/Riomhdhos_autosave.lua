-- Riomhdhos_autosave.lua
-- Save the PROJECT FILE periodically, because nothing else ever does.
--
-- ⚠️ WHY THIS EXISTS. On 2026-08-27 the rig came up missing THE CAIRN's second Kontakt and
-- both Wrongtools instruments in it. Nothing had crashed and nothing was corrupt: the
-- project file on disk was TEN DAYS OLD. This box auto-starts REAPER with the project and
-- nobody ever presses Ctrl+S, so every restart quietly reverted to whatever was last saved
-- by hand, and everything since - a plugin added, an instrument loaded, a level set - went
-- with it. It reads as "the rig lost my work" rather than "the rig never saved it".
--
-- REAPER's own autosave was on and did nothing to help: it writes timestamped copies into
-- rig\AutoSaves\ and never touches the file that gets loaded. Those copies are what the
-- restore came from, and they are worth keeping - but a backup you have to know about is
-- not the same as the project being saved.
--
-- ⚠️ SAVING IS NOT FREE ON THIS BOX. The audio graph has 2.9 ms per block at 128 samples,
-- and serialising two Kontakt instances is megabytes of plugin state. So this saves only
-- when the project is DIRTY, never while the transport is rolling, and never more often
-- than the interval below. If a save ever coincides with an audible click, raise INTERVAL
-- or set the ExtState switch to 0 - do not remove the script and go back to never saving.
--
--   disable at runtime:  ExtState Riomhdhos/autosave = "0"
--   re-enable:                                        "1" (or delete the key)

local INTERVAL = 300          -- seconds between saves, when there is something to save
local SETTLE   = 20           -- seconds of no further edits before saving, so a save never
                              -- lands in the middle of someone dragging a fader

local last_save = reaper.time_precise()
local dirty_since = nil

local function enabled()
  return reaper.GetExtState("Riomhdhos", "autosave") ~= "0"
end

local mygen

local function tick()
  -- superseded by a newer copy of this script? then stop deferring and let it take over
  if (tonumber(reaper.GetExtState("Riomhdhos", "autosave_gen")) or 0) ~= mygen then return end
  local ok, err = pcall(function()
    local now = reaper.time_precise()
    local dirty = reaper.IsProjectDirty(0) == 1

    if not dirty then
      dirty_since = nil
    elseif not dirty_since then
      dirty_since = now
    end

    if enabled() and dirty and dirty_since
       and (now - last_save) >= INTERVAL
       and (now - dirty_since) >= SETTLE
       and reaper.GetPlayState() == 0 then
      reaper.Main_SaveProject(0, false)
      last_save = now
      dirty_since = nil
      -- A save that happens silently is indistinguishable from one that never runs, and
      -- this script exists because of exactly that gap. Leave a trail.
      reaper.SetExtState("Riomhdhos", "autosave_last", os.date("%Y-%m-%d %H:%M:%S"), false)
      reaper.SetExtState("Riomhdhos", "autosave_count",
        tostring((tonumber(reaper.GetExtState("Riomhdhos", "autosave_count")) or 0) + 1), false)
    end
  end)
  -- ⚠️ An uncaught error here raises a MODAL dialog, and a modal dialog stalls every defer
  -- script in the session - the mood watchdog and the remote console included. Log and
  -- carry on; never raise.
  if not ok then
    reaper.SetExtState("Riomhdhos", "autosave_err", tostring(err), false)
  end
  reaper.defer(tick)
end

-- Single instance: re-running this file replaces the old loop rather than stacking a
-- second one, the same generation trick the bridge uses.
local gen = (tonumber(reaper.GetExtState("Riomhdhos", "autosave_gen")) or 0) + 1
reaper.SetExtState("Riomhdhos", "autosave_gen", tostring(gen), false)
mygen = gen

tick()
