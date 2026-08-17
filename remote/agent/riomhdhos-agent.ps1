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
$LevelsLua = Join-Path $Here 'levels.lua'

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

$script:AudioCache     = $null
$script:AudioCacheTime = [datetime]::MinValue

function Get-AudioInventory {
  # OS-level audio picture, gathered from Windows rather than from REAPER. This is the
  # half REAPER cannot tell you: an ASIO driver can be installed and registered while the
  # interface it drives is unplugged, and REAPER will simply report no device without
  # ever explaining why.
  #
  # Cached for 30s. Get-PnpDevice is slow enough that running it on every poll would make
  # the phone's ten-second refresh a measurable load on a machine whose whole job is to
  # not glitch. Hardware does not appear and disappear faster than this anyway - and when
  # it does, that is the one case worth waiting half a minute to be sure about.
  if ($script:AudioCache -and ((Get-Date) - $script:AudioCacheTime).TotalSeconds -lt 30) {
    return $script:AudioCache
  }

  $inv = @{ asioDrivers = @(); devices = @(); errors = @() }

  # ASIO drivers are a plain registry list. Both views are read because a 32-bit driver
  # registers under WOW6432Node and would otherwise be invisible to a 64-bit host.
  foreach ($root in 'HKLM:\SOFTWARE\ASIO', 'HKLM:\SOFTWARE\WOW6432Node\ASIO') {
    try {
      if (Test-Path $root) {
        foreach ($k in Get-ChildItem $root -ErrorAction Stop) {
          $desc = (Get-ItemProperty $k.PSPath -ErrorAction SilentlyContinue).Description
          $inv.asioDrivers += @{
            name = $k.PSChildName
            description = if ($desc) { $desc } else { $k.PSChildName }
            bits = if ($root -like '*WOW6432Node*') { 32 } else { 64 }
          }
        }
      }
    } catch { $inv.errors += "asio registry ${root}: $($_.Exception.Message)" }
  }

  # Physical sound hardware and, critically, whether Windows thinks it is healthy. A
  # device in Error state is the difference between "REAPER is misconfigured" and
  # "the interface fell off the USB bus", which are fixed in completely different ways.
  # Deduplicated by name: Windows lists a multi-interface USB device once per interface,
  # so the Zoom shows up twice and would otherwise read as two separate faults. Best
  # status wins, because "present on one interface" means the hardware is there.
  try {
    $seen = @{}
    foreach ($d in (Get-PnpDevice -Class Media -ErrorAction Stop)) {
      $n = $d.FriendlyName
      if ($seen.ContainsKey($n) -and $seen[$n] -eq 'OK') { continue }
      $seen[$n] = $d.Status
    }
    foreach ($n in $seen.Keys) {
      $inv.devices += @{
        name    = $n
        # OK = connected and working. Unknown = driver installed but the hardware is not
        # currently attached. Error/Degraded = attached and broken. These are three
        # different situations with three different fixes; collapsing them loses the fix.
        status  = $seen[$n]
        present = ($seen[$n] -eq 'OK')
      }
    }
  } catch { $inv.errors += "pnp media: $($_.Exception.Message)" }

  $script:AudioCache     = $inv
  $script:AudioCacheTime = Get-Date
  return $inv
}

function Get-AudioChecks {
  # Each check is a plain statement with a fix attached. The point is that the phone
  # should tell you what to DO, not make you infer it from a field you have to remember
  # the correct value of.
  param($Deep, $Inventory, $Reaper)

  $checks = @()
  function chk($label, $state, $detail, $fix) {
    @{ label = $label; state = $state; detail = $detail; fix = $fix }
  }

  # 1. is the hardware attached, and is it healthy? Three states, not two.
  $broken = @($Inventory.devices | Where-Object { $_.status -eq 'Error' -or $_.status -eq 'Degraded' })
  $absent = @($Inventory.devices | Where-Object { $_.status -ne 'OK' -and $_.status -ne 'Error' -and $_.status -ne 'Degraded' })
  $live   = @($Inventory.devices | Where-Object { $_.status -eq 'OK' })

  if ($Inventory.devices.Count -eq 0) {
    $checks += chk 'Sound hardware' 'warn' 'Windows reported no media devices' 'Could not enumerate; check the agent log.'
  } elseif ($broken.Count -gt 0) {
    $checks += chk 'Sound hardware' 'bad' (($broken | ForEach-Object { "$($_.name) [$($_.status)]" }) -join '; ') `
                  'Attached but faulted. Reseat the USB cable, then reboot the rig.'
  } elseif ($absent.Count -gt 0) {
    $checks += chk 'Sound hardware' 'warn' `
                  ("$($live.Count) connected; not connected: " + (($absent | ForEach-Object { $_.name }) -join ', ')) `
                  'Driver installed but the hardware is not plugged in. Fine unless you need that device.'
  } else {
    $checks += chk 'Sound hardware' 'ok' "$($live.Count) device(s) connected" ''
  }

  # 2. is an ASIO driver even installed. Deduplicated by name: a driver registers under
  # both the 64- and 32-bit hives and listing both reads as four drivers, not two.
  $asioNames = @($Inventory.asioDrivers | ForEach-Object { $_.description } | Select-Object -Unique)
  if ($asioNames.Count -eq 0) {
    $checks += chk 'ASIO driver' 'bad' 'No ASIO driver registered' 'Install the interface driver; REAPER cannot use ASIO without one.'
  } else {
    $checks += chk 'ASIO driver' 'ok' ($asioNames -join '; ') ''
  }

  if (-not $Reaper.running) {
    $checks += chk 'REAPER' 'warn' 'not running' 'Press Start REAPER.'
    return $checks
  }

  # 3. did REAPER actually open a device
  $out = if ($Deep) { $Deep['audio_out'] } else { $null }
  if (-not $out -or $out -eq '?' -or $out -eq '') {
    $checks += chk 'Audio device' 'bad' 'REAPER has no audio device open' `
                  'The driver failed to open - usually the interface is unplugged, or another app grabbed it exclusively.'
  } elseif ($out -match 'Remote Audio|RDP') {
    $checks += chk 'Audio device' 'bad' $out `
                  'REAPER was started inside an RDP session. Disconnect RDP, then Restart REAPER.'
  } else {
    $checks += chk 'Audio device' 'ok' $out ''
  }

  # 4. ASIO vs anything else
  $mode = if ($Deep) { $Deep['audio_mode'] } else { $null }
  if ($mode -and $mode -ne 'ASIO') {
    $checks += chk 'Driver mode' 'warn' $mode 'Not ASIO. Latency will be poor; switch in REAPER audio preferences.'
  } elseif ($mode) {
    $checks += chk 'Driver mode' 'ok' $mode ''
  }

  # 5. buffer size - large buffers are audible as lag when playing live
  $bsize = if ($Deep) { [int]($Deep['audio_bsize']) } else { 0 }
  if ($bsize -gt 512) {
    $checks += chk 'Buffer' 'warn' "$bsize samples" 'Large enough to feel as lag when playing. Reduce it in the driver panel.'
  } elseif ($bsize -gt 0) {
    $checks += chk 'Buffer' 'ok' "$bsize samples" ''
  }

  return $checks
}

$script:ReaperIni = Join-Path $env:APPDATA 'REAPER\reaper.ini'

function Get-ReaperIniPath {
  # The agent runs as SYSTEM, so $env:APPDATA is SYSTEM's own profile and never the one
  # REAPER reads. Finding the right file is done by evidence, in descending order of how
  # much the evidence proves:
  #
  #   1. the running REAPER's own process owner  - proof, if REAPER is up
  #   2. any reaper.ini on disk, newest first    - works with REAPER stopped
  #   3. SYSTEM's own APPDATA                    - last resort, almost certainly wrong
  #
  # Win32_ComputerSystem.UserName was tried first and abandoned: it reports the CONSOLE
  # user specifically, and returns empty the moment an RDP session displaces the console -
  # which is exactly when someone is likely to be fiddling with audio settings.
  if ($script:ReaperIni -and (Test-Path $script:ReaperIni)) { return $script:ReaperIni }

  $proc = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
          Where-Object { $_.Name -eq 'reaper.exe' } | Select-Object -First 1
  if ($proc) {
    try {
      $owner = Invoke-CimMethod -InputObject $proc -MethodName GetOwner -ErrorAction Stop
      if ($owner.User) {
        $p = "C:\Users\$($owner.User)\AppData\Roaming\REAPER\reaper.ini"
        if (Test-Path $p) { $script:ReaperIni = $p; return $p }
      }
    } catch { }
  }

  $found = Get-ChildItem 'C:\Users\*\AppData\Roaming\REAPER\reaper.ini' -ErrorAction SilentlyContinue |
           Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if ($found) { $script:ReaperIni = $found.FullName; return $found.FullName }

  if (Test-Path $script:ReaperIni) { return $script:ReaperIni }
  return $null
}

function Get-Levels {
  # "The audio device is open" and "audio is coming out" are different claims. Every
  # other check in this agent answers the first. This answers the second - the one that
  # matters thirty seconds before a set starts.
  $proc = Get-ReaperProcess
  if (-not $proc)            { return @{ available = $false; note = 'REAPER is not running.' } }
  if (-not (Get-ConsoleHeartbeat).alive) {
    return @{ available = $false; note = 'REAPER is up but the remote console is not running - no levels available.' }
  }

  # Longer timeout than the health probe: levels.lua deliberately spends ~700 ms holding
  # peaks on the master plus 50 ms per track, so it cannot answer inside health's 6 s on
  # a project with many tracks.
  $raw = Invoke-RigLua -Source (Get-Content $LevelsLua -Raw) -TimeoutSec 20
  if (-not $raw) { return @{ available = $false; note = 'Level probe timed out - the console did not answer.' } }

  $kv = ConvertFrom-KvReport $raw
  $tracks = @()
  for ($i = 0; $i -lt [int]($kv['track_count']); $i++) {
    $row = $kv["track$i"]
    if (-not $row) { continue }
    # Split with a cap of 6 so the name - which is LAST and may itself contain '|' -
    # arrives whole in the final element rather than shifting the fields after it.
    $p = $row -split '\|', 6
    if ($p.Count -lt 6) { continue }
    $tracks += @{
      l = [double]$p[0]; r = [double]$p[1]
      muted = ($p[2] -eq '1'); armed = ($p[3] -eq '1'); midi = ($p[4] -eq '1')
      name = $p[5]
      # -150 is the floor levels.lua substitutes for -inf, so anything at or below it is
      # silence. Compared as a number rather than a string: "-inf" does not sort.
      silent = ([double]$p[0] -le -149 -and [double]$p[1] -le -149)
    }
  }

  $mL = [double]$kv['master_l']; $mR = [double]$kv['master_r']
  $masterSilent = ($mL -le -149 -and $mR -le -149)

  $checks = @()
  $checks += if ($masterSilent) {
    @{ label='Master output'; state='warn'; detail='silent'; fix='Nothing is reaching the output. Check the active mood is unmuted and that something is actually being played.' }
  } elseif ($mL -gt -1 -or $mR -gt -1) {
    @{ label='Master output'; state='bad'; detail=("peak {0} / {1} dBFS" -f $mL, $mR); fix='Clipping. Pull the master or the mood level down.' }
  } else {
    @{ label='Master output'; state='ok'; detail=("peak {0} / {1} dBFS" -f $mL, $mR); fix='' }
  }

  # An armed AUDIO input at digital silence is a dead cable or a dead interface input, and
  # it is invisible from every other check the agent makes.
  #
  # ⚠️ MIDI-armed tracks are excluded, and this is not a detail. The brain, the Push, the
  # LED track and DRUMS are all armed for MIDI and carry no audio, so they sit at -inf
  # permanently and correctly. Warning on them fires on every single read - and a
  # diagnostic that always warns is one you stop reading, which is worse than none.
  foreach ($t in ($tracks | Where-Object { $_.armed -and $_.silent -and -not $_.midi })) {
    $checks += @{ label=('Input: ' + $t.name); state='warn'; detail='armed but silent'
                  fix='Nothing is arriving on this input. Check the cable, the gain, and that the right physical input is assigned.' }
  }

  @{
    available    = $true
    masterL      = $mL
    masterR      = $mR
    masterSilent = $masterSilent
    playState    = $kv['play_state']
    tracks       = $tracks
    checks       = $checks
  }
}

function Get-Latency {
  # Summarises the rolling log written by latency-sampler.ps1. The agent does NOT sample
  # here: Get-Counter needs a real interval to produce a meaningful rate, so sampling
  # inside a request would block every health poll for seconds.
  #
  # WHY THIS MATTERS ON THIS BOX: at 128 samples / 44100 Hz the whole REAPER graph has to
  # finish inside 2.9 ms, every 2.9 ms. A single DPC that blocks for 3 ms is an audible
  # click in the PA. Average CPU tells you nothing about that - a machine at 10% average
  # can still miss a deadline every few minutes. What matters is the TAIL.
  param([int]$Minutes = 30)

  $csv = 'C:\Users\mccul\rig\latency.csv'
  if (-not (Test-Path $csv)) {
    return @{ available = $false; note = 'sampler not running - no latency log on disk' }
  }

  try { $rows = Import-Csv $csv -ErrorAction Stop } catch {
    return @{ available = $false; note = "could not read latency log: $($_.Exception.Message)" }
  }

  $cut = (Get-Date).AddMinutes(-$Minutes)
  # -as rather than [datetime]::TryParse: TryParse needs a TYPED [ref] target, and
  # passing [ref] to an untyped $null makes PowerShell unable to resolve the overload.
  # -as yields $null on a bad parse, which is the same test with none of that.
  $win = @($rows | Where-Object {
    $ts = $_.time -as [datetime]
    $ts -and $ts -ge $cut
  })
  if ($win.Count -lt 3) {
    return @{ available = $false; note = "only $($win.Count) samples in the last $Minutes min - let it run longer" }
  }

  # p95 rather than mean. The mean of a latency distribution hides exactly the events
  # that cause dropouts; the tail is the entire story.
  $stat = {
    param($field)
    $v = @($win | ForEach-Object { $_.$field } | Where-Object { $_ -ne '' -and $null -ne $_ } | ForEach-Object { [double]$_ })
    if ($v.Count -lt 2) { return $null }
    $sorted = $v | Sort-Object
    [pscustomobject]@{
      avg = [math]::Round(($v | Measure-Object -Average).Average, 3)
      p95 = [math]::Round($sorted[[int][math]::Floor($sorted.Count * 0.95)], 3)
      max = [math]::Round(($v | Measure-Object -Maximum).Maximum, 3)
    }
  }

  $dpc    = & $stat 'dpc_pct'
  $isr    = & $stat 'isr_pct'
  $cpu    = & $stat 'cpu_pct'
  $queue  = & $stat 'queue_len'
  $reaper = & $stat 'reaper_cpu_pct'

  # Thresholds are deliberately about the PEAK, not the average, for the reason above.
  $checks = @()
  if ($dpc) {
    $checks += if ($dpc.max -ge 10) {
      @{ label='DPC time'; state='bad';  detail="peak $($dpc.max)% (avg $($dpc.avg)%)"; fix='A driver is holding the CPU long enough to miss the audio deadline. Run LatencyMon at the console to name it - the usual offenders are network, GPU and power management.' }
    } elseif ($dpc.max -ge 4) {
      @{ label='DPC time'; state='warn'; detail="peak $($dpc.max)% (avg $($dpc.avg)%)"; fix='Headroom is thinner than ideal at 128 samples. Fine now, worth watching if you add load.' }
    } else {
      @{ label='DPC time'; state='ok';   detail="peak $($dpc.max)% (avg $($dpc.avg)%)"; fix='' }
    }
  }
  if ($queue) {
    $checks += if ($queue.max -ge 4) {
      @{ label='CPU queue'; state='warn'; detail="peak $($queue.max) threads waiting"; fix='Threads are queueing for CPU. Something is competing with the audio thread.' }
    } else {
      @{ label='CPU queue'; state='ok';   detail="peak $($queue.max)"; fix='' }
    }
  }
  if ($reaper) {
    $checks += if ($reaper.p95 -ge 85) {
      @{ label='REAPER CPU'; state='warn'; detail="p95 $($reaper.p95)% of one core"; fix='The audio thread is close to saturating its core. Raise the buffer or thin the plugin chain before adding anything.' }
    } else {
      @{ label='REAPER CPU'; state='ok';   detail="p95 $($reaper.p95)% of one core"; fix='' }
    }
  }

  @{
    available   = $true
    windowMin   = $Minutes
    samples     = $win.Count
    firstSample = $win[0].time
    lastSample  = $win[-1].time
    dpc         = $dpc
    isr         = $isr
    cpu         = $cpu
    queue       = $queue
    reaper      = $reaper
    checks      = $checks
  }
}

function Get-AudioDevices {
  # What REAPER could be pointed at, which one it is pointed at, and whether the hardware
  # is actually attached. The last part is the one that saves a trip to the rig: selecting
  # a driver whose interface is unplugged leaves REAPER with no audio at all.
  $ini = Get-ReaperIniPath
  $current = $null
  if ($ini) {
    $line = Select-String -Path $ini -Pattern '^\s*asio_driver_name\s*=' -ErrorAction SilentlyContinue |
            Select-Object -First 1
    if ($line -and $line.Line -match '=\s*"?([^"]+)"?\s*$') { $current = $Matches[1].Trim() }
  }

  $inv = Get-AudioInventory
  $names = @($inv.asioDrivers | ForEach-Object { $_.description } | Select-Object -Unique)

  $devices = @()
  foreach ($n in $names) {
    # Heuristic, and deliberately loose: match the driver's first word against the
    # hardware list ("UMC ASIO Driver" -> "BEHRINGER UMC 202HD 192k"). Vendors do not
    # use the same string in both places, so an exact match would report everything absent.
    $keyword = ($n -split '\s+')[0]
    $hw = @($inv.devices | Where-Object { $_.name -like "*$keyword*" })
    $devices += @{
      name    = $n
      current = ($n -eq $current)
      present = [bool](@($hw | Where-Object { $_.status -eq 'OK' }).Count)
      hardware = (($hw | ForEach-Object { $_.name }) -join '; ')
    }
  }
  return @{ current = $current; devices = $devices; ini = $ini }
}

function Set-AudioDevice {
  # Switching ASIO drivers is a restart-level change: REAPER holds its audio config in
  # memory and rewrites reaper.ini on exit, so editing the file under a running REAPER
  # would simply be overwritten. The order here is what makes it safe -
  #   save -> kill -> edit -> relaunch
  # Killing before editing matters twice over: a force-killed REAPER does not write the
  # ini back, so our edit survives, and the save beforehand is what stops that costing
  # you unsaved work.
  param([string]$Name)

  $ini = Get-ReaperIniPath
  if (-not $ini) { return @{ ok = $false; message = 'reaper.ini not found' } }

  $avail = (Get-AudioDevices).devices
  $match = @($avail | Where-Object { $_.name -eq $Name })
  if (-not $match.Count) {
    return @{ ok = $false; message = "unknown driver '$Name'"; available = @($avail | ForEach-Object { $_.name }) }
  }

  $steps = @()

  # 1. save, so the kill in step 2 cannot lose work
  $proc = Get-ReaperProcess
  if ($proc -and (Get-ConsoleHeartbeat).alive) {
    $saved = Invoke-RigLua -Source "say(`"NONCE=@@NONCE@@`")`nR.Main_OnCommand(40026, 0)`nsay(`"saved=`" .. tostring(R.IsProjectDirty(0)))`nsay(`"END=@@NONCE@@`")" -TimeoutSec 10
    $steps += if ($saved) { 'project saved' } else { 'save did not confirm' }
  }

  # 2. stop
  if ($proc) { $steps += (Stop-Reaper).message }

  # 3. edit
  try {
    $lines = Get-Content $ini
    $hit = $false
    $out = foreach ($l in $lines) {
      if ($l -match '^\s*asio_driver_name\s*=') { $hit = $true; "asio_driver_name=`"$Name`"" }
      else { $l }
    }
    if (-not $hit) { return @{ ok = $false; message = 'asio_driver_name not present in reaper.ini'; steps = $steps } }
    Set-Content -Path $ini -Value $out -Encoding ascii
    $steps += "reaper.ini -> $Name"
  } catch {
    return @{ ok = $false; message = "ini write failed: $($_.Exception.Message)"; steps = $steps }
  }

  # 4. relaunch
  $started = Start-Reaper
  $steps += $started.message

  return @{
    ok = $started.ok
    message = "switched to $Name"
    warning = if (-not $match[0].present) { "$Name selected but its hardware is not connected - REAPER will come up with no audio device." } else { $null }
    steps = $steps
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
        # -Encoding UTF8, because track names are not ASCII: ÉIRE came back as "Ã‰IRE"
        # when this used the default, which is UTF-8 bytes decoded as Latin-1. REAPER
        # writes UTF-8; PowerShell 5.1's Get-Content does not assume it.
        $text = Get-Content $OutFile -Raw -Encoding UTF8 -ErrorAction Stop
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

  # CIM can come back empty in the seconds after the agent starts at boot. Every field
  # derived from it is guarded, because a health endpoint that throws is worse than one
  # reporting an unknown uptime - the phone shows "rig unreachable" and you go looking
  # for a network fault that isn't there.
  $os   = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
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
      bootedAt  = if ($os) { $os.LastBootUpTime.ToString('yyyy-MM-dd HH:mm:ss') } else { $null }
      uptimeMin = if ($os) { [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalMinutes) } else { $null }
      freeMemMB = if ($os) { [math]::Round($os.FreePhysicalMemory / 1024) } else { $null }
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
    audio   = Get-AudioInventory
    checks  = @()
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

  $health.checks = @(Get-AudioChecks -Deep $health.deep -Inventory $health.audio -Reaper $health.reaper)

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
        '^/api/whoami$' {
          # Deliberately trivial: this is what the phone's "Find rig" scan hits, once per
          # candidate address across a whole subnet. Anything that touches PnP, REAPER or
          # the console here would turn a discovery sweep into a denial of service against
          # the machine we are trying to find.
          Send-Json -Response $res -Object @{
            rig     = $env:COMPUTERNAME
            agent   = $AGENT_VERSION
            port    = $Port
          }
        }
        '^/api/health$' {
          $deep = -not ($req.QueryString['deep'] -eq '0')
          Send-Json -Response $res -Object (Get-Health -Deep $deep)
        }
        '^/api/levels$' {
          Send-Json -Response $res -Object (Get-Levels)
        }
        '^/api/latency$' {
          $mins = 30
          if ($req.QueryString['min']) { [int]::TryParse($req.QueryString['min'], [ref]$mins) | Out-Null }
          if ($mins -lt 1 -or $mins -gt 1440) { $mins = 30 }
          Send-Json -Response $res -Object (Get-Latency -Minutes $mins)
        }
        '^/api/audio/devices$' {
          Send-Json -Response $res -Object (Get-AudioDevices)
        }
        '^/api/audio/device$' {
          if ($verb -ne 'POST') { Send-Json -Response $res -Object @{ error = 'POST only' } -Status 405; break }
          $name = $req.QueryString['name']
          if (-not $name) { Send-Json -Response $res -Object @{ error = 'name required' } -Status 400; break }
          Write-Log "audio device -> $name (from $($req.RemoteEndPoint.Address))"
          Send-Json -Response $res -Object (Set-AudioDevice -Name $name)
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
