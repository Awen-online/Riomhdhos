# Registers the rolling DPC/ISR sampler as a scheduled task.
#
# WHY A TASK AND NOT JUST Start-Process: a process launched from an SSH session is a
# child of that session and dies when the session closes. The first attempt logged
# three samples and stopped for exactly that reason. A task is owned by the service,
# not by whoever started it.
#
# Runs as SYSTEM at boot so latency history exists BEFORE anyone thinks to look for it.
# The whole point is catching an intermittent DPC spike, and an intermittent fault is
# never happening at the moment you decide to start measuring.
param(
  [string]$Script   = 'C:\Users\mccul\rig\phone\agent\latency-sampler.ps1',
  [string]$TaskName = 'Riomhdhos Latency Sampler'
)

if (-not (Test-Path $Script)) { throw "sampler not found at $Script" }

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $Script)

$trigger = New-ScheduledTaskTrigger -AtStartup

# SYSTEM: the counters are machine-wide and need no desktop. Unlike the REAPER task,
# this deliberately does NOT want an interactive session - it must run on a headless
# box with nobody logged in, which is exactly how this rig lives.
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

# RestartCount/Interval matter more than they look: if the sampler ever dies, silent
# absence of data reads identically to "no problems found".
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero)

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Principal $principal -Settings $settings -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 12

$t = Get-ScheduledTask -TaskName $TaskName
Write-Output ("task '{0}' state: {1}" -f $TaskName, $t.State)
$csv = 'C:\Users\mccul\rig\latency.csv'
if (Test-Path $csv) {
  $n = (Get-Content $csv | Measure-Object -Line).Lines
  Write-Output ("latency.csv rows: {0}" -f $n)
  Get-Content $csv -Tail 3 | ForEach-Object { Write-Output ("  " + $_) }
}
