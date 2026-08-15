# install-agent.ps1
# Registers the agent as a boot-time scheduled task, opens the firewall, and prints the
# token and the URL to point the phone at. Run once, elevated, on RIOMHDHOS.
#
# WHY SYSTEM AND AT BOOT, NOT AT LOGON
# The rig is headless and nobody logs into it. An agent that waits for a logon is an
# agent that is not there when you arrive at a venue and the box has rebooted. Running
# as SYSTEM from boot also means it is up BEFORE REAPER, so it can report on a REAPER
# that failed to start - which is exactly the failure you most want reported.
#
# The agent still cannot start REAPER by itself: launching into session 1 with the real
# audio device is what the existing REAPER scheduled task is for. The agent triggers
# that task rather than trying to replace it.

[CmdletBinding()]
param(
  [int]    $Port       = 8765,
  [string] $TaskName   = 'Riomhdhos Agent',
  [string] $ReaperTask = '',
  [string] $RemoteDir  = 'C:\Users\mccul\rig\remote'
)

$ErrorActionPreference = 'Stop'

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal $id).IsInRole('Administrators')) {
  throw 'Run this elevated. The agent binds a port and registers a SYSTEM task; neither works otherwise.'
}

$Here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script = Join-Path $Here 'riomhdhos-agent.ps1'
if (-not (Test-Path $Script)) { throw "agent script not found at $Script" }

# ---------------------------------------------------------------- find REAPER's task
# Guessing this wrong means the "Start REAPER" button silently does nothing, so list
# the candidates and make the operator choose rather than picking one hopefully.
if (-not $ReaperTask) {
  $candidates = @(Get-ScheduledTask -ErrorAction SilentlyContinue |
    Where-Object { $_.TaskName -match 'reaper|riomhdhos|rig' } |
    Select-Object -ExpandProperty TaskName)
  if ($candidates.Count -eq 1) {
    $ReaperTask = $candidates[0]
    Write-Host "Using REAPER launch task: $ReaperTask" -ForegroundColor Green
  } else {
    Write-Host 'Could not pick a REAPER launch task automatically. Candidates:' -ForegroundColor Yellow
    $candidates | ForEach-Object { Write-Host "   $_" }
    Write-Host 'Re-run with -ReaperTask "<name>" to wire the Start/Restart buttons.' -ForegroundColor Yellow
  }
}

# ---------------------------------------------------------------- firewall
# Both profiles on purpose: a phone hotspot is usually classified Public, and a rule
# that only covers Private is a rule that works at home and fails at the gig.
$ruleName = "Riomhdhos Agent $Port"
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort $Port -Profile Private,Public | Out-Null
Write-Host "Firewall: opened TCP $Port (Private + Public)" -ForegroundColor Green

# ---------------------------------------------------------------- scheduled task
$argList = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`" -Port $Port -RemoteDir `"$RemoteDir`""
if ($ReaperTask) { $argList += " -ReaperTask `"$ReaperTask`"" }

$action    = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argList
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
# RestartCount/Interval matter more than they look: if the agent dies you lose the only
# remote channel to a headless box, and nobody is there to notice.
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
               -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
               -ExecutionTimeLimit ([TimeSpan]::Zero)

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Principal $principal -Settings $settings | Out-Null
Write-Host "Scheduled task '$TaskName' registered (at startup, SYSTEM)" -ForegroundColor Green

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3

# ---------------------------------------------------------------- report
$tokenFile = Join-Path $RemoteDir 'agent.token'
$token = if (Test-Path $tokenFile) { (Get-Content $tokenFile -Raw).Trim() } else { '(not generated yet - check the log)' }

$addrs = Get-NetIPAddress -AddressFamily IPv4 |
  Where-Object { $_.IPAddress -ne '127.0.0.1' -and $_.IPAddress -notlike '169.254.*' }

Write-Host ''
Write-Host 'Open one of these on the phone, then Add to Home Screen:' -ForegroundColor Cyan
foreach ($a in $addrs) {
  Write-Host ("   http://{0}:{1}/?token={2}    [{3}]" -f $a.IPAddress, $Port, $token, $a.InterfaceAlias)
}
Write-Host ''
Write-Host "Token file: $tokenFile"
Write-Host "Log file:   $(Join-Path $RemoteDir 'agent.log')"
Write-Host ''
Write-Host 'The ?token= form saves it into the app so you never type it again.' -ForegroundColor DarkGray
