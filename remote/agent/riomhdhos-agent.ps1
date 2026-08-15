# riomhdhos-agent.ps1
# A small authenticated HTTP service that lets the phone drive the rig: report health,
# restart REAPER, shut the box down. Serves the PWA from ..\www as well, so the phone
# needs nothing installed.
#
# WHY POWERSHELL AND NOT PYTHON/GO
# It has to survive a bare Windows reinstall of the rig with no toolchain and no
# network. PowerShell and System.Net.HttpListener are already on the box. Nothing to
# install means nothing to forget to reinstall at 6pm in a venue.
#
# WHY IT CANNOT POWER THE MACHINE ON
# When the box is off there is no agent to answer. Power-on is a smart plug plus BIOS
# restore-on-AC, driven from the phone directly. That asymmetry is inherent, not a gap.
#
# THE SHUTDOWN ENDPOINT KILLS REAPER FIRST, ON PURPOSE
# REAPER holding an ASIO device can deadlock on exit (see the RDP lesson in the rig
# notes). Windows shutdown waits on it, and a stall leaves a headless box half down
# with the network already gone - recoverable only at the physical power button. So
# the agent force-kills REAPER before asking for shutdown, every time. The lesson
# belongs in code, not in a note somebody has to remember.

[CmdletBinding()]
param(
  [int]    $Port      = 8765,
  # '+' binds every interface and needs elevation. 'localhost' binds only the loopback
  # and does not, which is what makes the agent testable on a workstation without
  # granting it the run of the machine.
  [string] $Bind      = '+',
  [string] $RemoteDir = "C:\Users\mccul\rig\remote",
  [string] $ReaperTask = "",          # scheduled task that launches REAPER into session 1
  [switch] $Console                    # log to stdout as well as the log file
)

$ErrorActionPreference = 'Stop'
$AGENT_VERSION = '1.0.0'

$Here    = Split-Path -Parent $MyInvocation.MyCommand.Path
$WwwDir  = Join-Path (Split-Path -Parent $Here) 'www'
$HealthLua = Join-Path $Here 'health.lua'

$InFile     = Join-Path $RemoteDir 'in.lua'
$ReadyFile  = Join-Path $RemoteDir 'in.ready'
$OutFile    = Join-Path $RemoteDir 'out.txt'
$StatusFile = Join-Path $RemoteDir 'status.txt'
$TokenFile  = Join-Path $RemoteDir 'agent.token'
$LogFile    = Join-Path $RemoteDir 'agent.log'

# ---------------------------------------------------------------- logging
function Write-Log {
  param([string]$Message)
  $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
  try { Add-Content -Path $LogFile -Value $line -Encoding ascii } catch {}
  if ($Console) { Write-Output $line }
}

# ---------------------------------------------------------------- token
# Generated once and stored beside the console files. The phone holds a copy; anything
# without it gets 401. This is a shared secret on a private network, not a login system
# - it exists so that "on the same wifi" is not by itself enough to power off the rig.
if (-not (Test-Path $RemoteDir)) { New-Item -Path $RemoteDir -ItemType Directory -Force | Out-Null }
if (-not (Test-Path $TokenFile)) {
  $bytes = New-Object byte[] 24
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $tok = ([System.BitConverter]::ToString($bytes) -replace '-','').ToLower()
  Set-Content -Path $TokenFile -Value $tok -Encoding ascii
  Write-Log "generated new token at $TokenFile"
}
$TOKEN = (Get-Content $TokenFile -Raw).Trim()

# ---------------------------------------------------------------- rig facts
function Get-ReaperProcess {
  return Get-Process -Name reaper -ErrorAction SilentlyContinue | Select-Object -First 1
}

function Get-ConsoleHeartbeat {
  # Riomhdhos_remote.lua rewrites status.txt every 100ms. If it is stale the console is
  # not running, and a deep probe would just time out - so we check this before dropping
  # a snippet rather than making the phone wait six seconds to learn nothing.
  if (-not (Test-Path $StatusFile)) { return @{ alive = $false; text = 'no status file' } }
  $age = (Get-Date) - (Get-Item $StatusFile).LastWriteTime
  return @{
    alive   = ($age.TotalSeconds -lt 5)
    ageSec  = [math]::Round($age.TotalSeconds, 1)
    text    = (Get-Content $StatusFile -Raw).Trim()
  }
}

function Invoke-RigLua {
  # Drop a snippet for Riomhdhos_remote.lua and wait for the matching answer.
  param([string]$Source, [int]$TimeoutSec = 6)

  $nonce = [guid]::NewGuid().ToString('N').Substring(0, 12)
  $src   = $Source -replace '@@NONCE@@', $nonce

  # Order matters and has bitten before: clear any stale trigger, write the code, and
  # only then create the marker. The watcher fires on in.ready, so a marker that
  # outlives its snippet makes it execute an empty or half-written file.
  Remove-Item -Path $ReadyFile -Force -ErrorAction SilentlyContinue
  Remove-Item -Path $OutFile   -Force -ErrorAction SilentlyContinue
  Set-Content -Path $InFile -Value $src -Encoding ascii
  New-Item -Path $ReadyFile -ItemType File -Force | Out-Null

  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  while ((Get-Date) -lt $deadline) {
    if (Test-Path $OutFile) {
      try {
        $text = Get-Content $OutFile -Raw -ErrorAction Stop
        # Require BOTH markers: the trailing one proves we did not read a partially
        # flushed file and lose the tail of the report.
        if ($text -match "NONCE=$nonce" -and $text -match "END=$nonce") { return $text }
      } catch { }
    }
    Start-Sleep -Milliseconds 100
  }
  return $null
}

function ConvertFrom-KvReport {
  param([string]$Text)
  $h = @{}
  if (-not $Text) { return $h }
  foreach ($line in ($Text -split "`r?`n")) {
    if ($line -match '^([A-Za-z0-9_]+)=(.*)$') { $h[$Matches[1]] = $Matches[2] }
  }
  $h.Remove('NONCE') | Out-Null
  $h.Remove('END')   | Out-Null
  return $h
}

function Get-Health {
  param([bool]$Deep = $true)

  $os   = Get-CimInstance Win32_OperatingSystem
  $proc = Get-ReaperProcess
  $hb   = Get-ConsoleHeartbeat

  # Every interface, because the hotspot question ("which address is it on right now?")
  # is exactly what you need answered when the app cannot find the rig.
  $nets = @()
  foreach ($ip in (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                   Where-Object { $_.IPAddress -ne '127.0.0.1' })) {
    $nets += @{ iface = $ip.InterfaceAlias; address = $ip.IPAddress }
  }

  $health = @{
    agent = @{
      version = $AGENT_VERSION
      port    = $Port
      time    = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    }
    host = @{
      name      = $env:COMPUTERNAME
      bootedAt  = $os.LastBootUpTime.ToString('yyyy-MM-dd HH:mm:ss')
      uptimeMin = [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalMinutes)
      freeMemMB = [math]::Round($os.FreePhysicalMemory / 1024)
      networks  = $nets
    }
    reaper = @{
      running    = [bool]$proc
      pid        = if ($proc) { $proc.Id } else { $null }
      responding = if ($proc) { $proc.Responding } else { $false }
      memMB      = if ($proc) { [math]::Round($proc.WorkingSet64 / 1MB) } else { $null }
    }
    console = $hb
    deep    = $null
    notes   = @()
  }

  # A REAPER that is running but not responding is the ASIO deadlock. Say so plainly
  # rather than making someone infer it from a boolean.
  if ($proc -and -not $proc.Responding) {
    $health.notes += 'REAPER is not responding - likely the ASIO exit deadlock. Restart it.'
  }
  if (-not $proc) {
    $health.notes += 'REAPER is not running. The box is up; only the rig is down.'
  }

  if ($Deep -and $proc -and $hb.alive) {
    $raw = Invoke-RigLua -Source (Get-Content $HealthLua -Raw) -TimeoutSec 6
    if ($raw) {
      $health.deep = ConvertFrom-KvReport $raw
      $out = $health.deep['audio_out']
      if ($out -and $out -match 'Remote Audio|RDP') {
        $health.notes += "Audio device is '$out' - REAPER was started inside an RDP session and is not on the real interface."
      }
    } else {
      $health.notes += 'Deep probe timed out - the remote console did not answer.'
    }
  } elseif ($Deep -and $proc -and -not $hb.alive) {
    $health.notes += 'REAPER is up but the remote console is not running; no deep health available.'
  }

  return $health
}

# ---------------------------------------------------------------- actions
function Stop-Reaper {
  $proc = Get-ReaperProcess
  if (-not $proc) { return @{ ok = $true; message = 'REAPER was not running' } }
  Stop-Process -Id $proc.Id -Force
  Start-Sleep -Milliseconds 800
  $still = Get-ReaperProcess
  return @{ ok = (-not $still); message = if ($still) { 'kill did not take' } else { "killed pid $($proc.Id)" } }
}

function Start-Reaper {
  # Launching must go through the scheduled task: it is what puts REAPER in session 1
  # with the real audio device. Starting the exe directly from here would land it in
  # whatever session the agent happens to be in, which is the Remote Audio trap again.
  if (-not $ReaperTask) {
    return @{ ok = $false; message = 'no ReaperTask configured - set it in agent.json' }
  }
  $null = schtasks /run /tn $ReaperTask 2>&1
  if ($LASTEXITCODE -ne 0) {
    return @{ ok = $false; message = "schtasks /run /tn $ReaperTask failed with $LASTEXITCODE" }
  }
  return @{ ok = $true; message = "launched via task '$ReaperTask'" }
}

function Restart-Reaper {
  $stopped = Stop-Reaper
  Start-Sleep -Milliseconds 1200
  $started = Start-Reaper
  return @{ ok = $started.ok; stop = $stopped; start = $started }
}

function Invoke-Shutdown {
  param([switch]$Reboot)
  # REAPER first - see the header. Non-negotiable, and deliberately not optional
  # through the API, because the one time it gets skipped is the time it hangs.
  $killed = Stop-Reaper
  Write-Log "shutdown requested (reboot=$Reboot); reaper: $($killed.message)"
  if ($Reboot) { shutdown /r /t 5 /c "Riomhdhos agent" | Out-Null }
  else         { shutdown /s /t 5 /c "Riomhdhos agent" | Out-Null }
  return @{ ok = $true; reaper = $killed; message = if ($Reboot) { 'rebooting in 5s' } else { 'shutting down in 5s' } }
}

# ---------------------------------------------------------------- http plumbing
$MIME = @{
  '.html' = 'text/html; charset=utf-8'
  '.js'   = 'application/javascript; charset=utf-8'
  '.css'  = 'text/css; charset=utf-8'
  '.json' = 'application/json; charset=utf-8'
  '.webmanifest' = 'application/manifest+json; charset=utf-8'
  '.png'  = 'image/png'
  '.svg'  = 'image/svg+xml'
  '.ico'  = 'image/x-icon'
}

function Send-Bytes {
  param($Response, [byte[]]$Bytes, [string]$ContentType, [int]$Status = 200)
  $Response.StatusCode  = $Status
  $Response.ContentType = $ContentType
  $Response.ContentLength64 = $Bytes.Length
  try {
    $Response.OutputStream.Write($Bytes, 0, $Bytes.Length)
  } catch { }
  $Response.OutputStream.Close()
}

function Add-Cors {
  # The PWA is normally same-origin with the agent, so this is not needed for the usual
  # case. It matters when the rig has moved - onto the phone hotspot, say - and the
  # installed app is still hosted from its old address while pointing at the new one.
  # Allowing any origin is safe only because every /api/ route demands the bearer token;
  # being able to reach the agent is not the same as being able to use it.
  param($Response)
  $Response.Headers['Access-Control-Allow-Origin']  = '*'
  $Response.Headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type'
  $Response.Headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
}

function Send-Json {
  param($Response, $Object, [int]$Status = 200)
  $json = $Object | ConvertTo-Json -Depth 8 -Compress
  Send-Bytes -Response $Response -Bytes ([Text.Encoding]::UTF8.GetBytes($json)) `
             -ContentType 'application/json; charset=utf-8' -Status $Status
}

function Send-Static {
  param($Response, [string]$RelPath)
  if ($RelPath -eq '/' -or $RelPath -eq '') { $RelPath = '/index.html' }
  # Reject anything that tries to climb out of www\. The agent runs elevated; a path
  # traversal here would serve any file on the box to anyone on the wifi.
  $clean = $RelPath.TrimStart('/').Replace('/', '\')
  if ($clean -match '\.\.') {
    Send-Json -Response $Response -Object @{ error = 'bad path' } -Status 400; return
  }
  $full = Join-Path $WwwDir $clean
  if (-not (Test-Path $full -PathType Leaf)) {
    Send-Json -Response $Response -Object @{ error = 'not found'; path = $RelPath } -Status 404; return
  }
  $ext  = [IO.Path]::GetExtension($full).ToLower()
  $type = if ($MIME.ContainsKey($ext)) { $MIME[$ext] } else { 'application/octet-stream' }
  Send-Bytes -Response $Response -Bytes ([IO.File]::ReadAllBytes($full)) -ContentType $type
}

function Test-Auth {
  param($Request)
  $hdr = $Request.Headers['Authorization']
  if ($hdr -and $hdr -match '^Bearer\s+(.+)$') { if ($Matches[1].Trim() -eq $TOKEN) { return $true } }
  $q = $Request.QueryString['token']
  if ($q -and $q -eq $TOKEN) { return $true }
  return $false
}

# ---------------------------------------------------------------- serve
$listener = New-Object System.Net.HttpListener
$listener.Prefixes.Add("http://${Bind}:$Port/")
try {
  $listener.Start()
} catch {
  Write-Log "FATAL: could not bind http://${Bind}:$Port/ - $($_.Exception.Message)"
  Write-Log "If this is an access error, the agent must run elevated (or add a urlacl)."
  throw
}

Write-Log "agent $AGENT_VERSION listening on port $Port; www=$WwwDir; token=$TokenFile"

while ($listener.IsListening) {
  $ctx = $null
  try { $ctx = $listener.GetContext() } catch { break }
  if (-not $ctx) { continue }

  $req  = $ctx.Request
  $res  = $ctx.Response
  $path = $req.Url.AbsolutePath
  $verb = $req.HttpMethod

  try {
    if ($path -like '/api/*') {
      Add-Cors -Response $res
      # Preflight carries no Authorization header by definition, so it must be answered
      # before the auth check or every cross-origin call fails at the first hurdle.
      if ($verb -eq 'OPTIONS') {
        Send-Bytes -Response $res -Bytes ([byte[]]@()) -ContentType 'text/plain' -Status 204
        continue
      }
      if (-not (Test-Auth -Request $req)) {
        Write-Log "401 $verb $path from $($req.RemoteEndPoint.Address)"
        Send-Json -Response $res -Object @{ error = 'unauthorized' } -Status 401
        continue
      }

      switch -Regex ($path) {
        '^/api/health$' {
          $deep = -not ($req.QueryString['deep'] -eq '0')
          Send-Json -Response $res -Object (Get-Health -Deep $deep)
        }
        '^/api/reaper/restart$' {
          if ($verb -ne 'POST') { Send-Json -Response $res -Object @{ error = 'POST only' } -Status 405; break }
          Write-Log "reaper restart from $($req.RemoteEndPoint.Address)"
          Send-Json -Response $res -Object (Restart-Reaper)
        }
        '^/api/reaper/stop$' {
          if ($verb -ne 'POST') { Send-Json -Response $res -Object @{ error = 'POST only' } -Status 405; break }
          Write-Log "reaper stop from $($req.RemoteEndPoint.Address)"
          Send-Json -Response $res -Object (Stop-Reaper)
        }
        '^/api/reaper/start$' {
          if ($verb -ne 'POST') { Send-Json -Response $res -Object @{ error = 'POST only' } -Status 405; break }
          Write-Log "reaper start from $($req.RemoteEndPoint.Address)"
          Send-Json -Response $res -Object (Start-Reaper)
        }
        '^/api/system/shutdown$' {
          if ($verb -ne 'POST') { Send-Json -Response $res -Object @{ error = 'POST only' } -Status 405; break }
          Send-Json -Response $res -Object (Invoke-Shutdown)
        }
        '^/api/system/reboot$' {
          if ($verb -ne 'POST') { Send-Json -Response $res -Object @{ error = 'POST only' } -Status 405; break }
          Send-Json -Response $res -Object (Invoke-Shutdown -Reboot)
        }
        default {
          Send-Json -Response $res -Object @{ error = 'unknown endpoint'; path = $path } -Status 404
        }
      }
    } else {
      Send-Static -Response $res -RelPath $path
    }
  } catch {
    Write-Log "ERROR $verb $path : $($_.Exception.Message)"
    try { Send-Json -Response $res -Object @{ error = $_.Exception.Message } -Status 500 } catch {}
  }
}

Write-Log 'agent stopped'
