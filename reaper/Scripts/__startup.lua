-- Reaper runs this once, automatically, at startup.
-- Riomhdhos rig. All of these wait for the project to load, so running them this early is fine.
local scripts = reaper.GetResourcePath() .. "/Scripts/"

dofile(scripts .. "Riomhdhos_mood_mute.lua")  -- only the selected mood track stays audible
dofile(scripts .. "Riomhdhos_arm_inputs.lua") -- Zoom [1] / Zoom [2] armed + monitoring
dofile(scripts .. "Riomhdhos_bridge.lua")     -- K4-K7 -> the active mood's Kontakt macros
dofile(scripts .. "Riomhdhos_remote.lua")     -- remote Lua console: watches rig\remote\
dofile(scripts .. "Riomhdhos_autosave.lua")  -- SAVE the project; nothing else ever does
