-- Riomhdhos_remote.lua
-- A remote Lua console for the rig. Runs a defer loop that watches a drop folder and
-- executes whatever Lua lands there, writing the result back to a file that can be
-- read over SSH. Removes the "load it in the Actions list and click Run" round trip.
--
--   C:\Users\mccul\rig\remote\in.lua      <- the code to run
--   C:\Users\mccul\rig\remote\in.ready    <- empty marker: write LAST, triggers the run
--   C:\Users\mccul\rig\remote\out.txt     <- result, rewritten each run
--   C:\Users\mccul\rig\remote\status.txt  <- heartbeat, proves the watcher is alive
--
-- The two-file handshake exists so a half-copied in.lua can never be executed:
-- nothing runs until in.ready appears, so write in.lua first and the marker second.
--
-- Inside a snippet you get:
--   say(...)   append a line to out.txt
--   R          alias for the reaper table
-- and whatever the chunk returns is appended as "-> <value>".
--
-- Every stage is wrapped in pcall. A snippet that errors reports the error and the
-- watcher keeps running - a bad paste can never take the console down with it.
--
-- SECURITY NOTE: this executes arbitrary Lua in REAPER. It is only reachable by
-- writing to a local folder, which already requires an authenticated SSH session -
-- and an SSH session can do strictly more than this can. So it grants no new access,
-- it just saves the GUI trip. Delete this from __startup.lua to disarm it.

local DIR    = "C:\\Users\\mccul\\rig\\remote\\"
local IN     = DIR .. "in.lua"
local READY  = DIR .. "in.ready"
local OUT    = DIR .. "out.txt"
local STATUS = DIR .. "status.txt"
local POLL   = 0.10   -- seconds

------------------------------------------------------------------ single instance
local hb = tonumber(reaper.GetExtState("Riomhdhos", "remote_hb")) or -1
if hb > 0 and (reaper.time_precise() - hb) < 2.0 then return end

------------------------------------------------------------------ helpers
local function readAll(path)
  local f = io.open(path, "r"); if not f then return nil end
  local s = f:read("*a"); f:close(); return s
end

local function writeAll(path, s)
  local f = io.open(path, "w"); if not f then return false end
  f:write(s); f:close(); return true
end

-- every run is also appended here, so an earlier result is never lost to the next one
local HIST = DIR .. "history.log"
local function appendHist(s)
  local f = io.open(HIST, "a"); if not f then return end
  f:write(s .. "\n" .. string.rep("=", 72) .. "\n"); f:close()
end

local function exists(path)
  local f = io.open(path, "r"); if f then f:close(); return true end
  return false
end

local function removeFile(path) os.remove(path) end

local runCount = 0

------------------------------------------------------------------ run one snippet
local function execute(src)
  runCount = runCount + 1
  local out = {}
  local function say(...)
    local parts = {}
    for i = 1, select("#", ...) do parts[#parts+1] = tostring((select(i, ...))) end
    out[#out+1] = table.concat(parts, "\t")
  end

  -- expose helpers as globals for the snippet
  _G.say = say
  _G.R   = reaper

  -- load the shared helper library fresh each run, so editing the library takes
  -- effect immediately without restarting REAPER
  local libPath = reaper.GetResourcePath() .. "/Scripts/riomhdhos_lib.lua"
  local libOk, libErr = pcall(dofile, libPath)
  if not libOk then say("[lib] NOT LOADED: " .. tostring(libErr)) end

  local header = string.format("run #%d   %s   %d bytes",
      runCount, os.date("%Y-%m-%d %H:%M:%S"), #src)

  local function finish(text)
    writeAll(OUT, text)
    appendHist(text)
  end

  local chunk, loadErr = load(src, "remote")
  if not chunk then
    finish(header .. "\nCOMPILE ERROR: " .. tostring(loadErr) .. "\n")
    return
  end

  local ok, ret = pcall(chunk)
  local body = table.concat(out, "\n")
  if not ok then
    finish(header .. "\n" .. body ..
      (#body > 0 and "\n" or "") .. "RUNTIME ERROR: " .. tostring(ret) .. "\n")
  else
    local tail = (ret ~= nil) and ("\n-> " .. tostring(ret)) or ""
    finish(header .. "\n" .. body .. tail .. "\n")
  end
end

------------------------------------------------------------------ loop
reaper.atexit(function()
  reaper.DeleteExtState("Riomhdhos", "remote_hb", false)
  writeAll(STATUS, "stopped " .. os.date("%Y-%m-%d %H:%M:%S") .. "\n")
end)

local nextPoll = 0

local function loop()
  local now = reaper.time_precise()
  reaper.SetExtState("Riomhdhos", "remote_hb", tostring(now), false)

  if now >= nextPoll then
    nextPoll = now + POLL
    -- heartbeat: lets a remote check confirm the console is alive without running anything
    writeAll(STATUS, string.format("alive %s  runs=%d\n",
        os.date("%Y-%m-%d %H:%M:%S"), runCount))

    if exists(READY) then
      local src = readAll(IN)
      -- consume the trigger first, so a snippet that throws cannot loop forever
      removeFile(READY)
      removeFile(IN)
      if src and #src > 0 then
        local ok, err = pcall(execute, src)
        if not ok then
          writeAll(OUT, "WATCHER ERROR: " .. tostring(err) .. "\n")
        end
      else
        writeAll(OUT, "empty or unreadable in.lua\n")
      end
    end
  end

  reaper.defer(loop)
end

writeAll(STATUS, "starting " .. os.date("%Y-%m-%d %H:%M:%S") .. "\n")
loop()
