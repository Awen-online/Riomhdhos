<#
.SYNOPSIS
  Register the rig dashboard as a scheduled task so it survives a reboot.

.DESCRIPTION
  dash.py has always been hand-started, which means it is gone after any restart - and this
  desktop has crashed 21 times. The one control surface for the whole video rig should not
  depend on somebody remembering to launch it.

  Runs at logon, in the user's own session. NOT as SYSTEM: the dashboard talks to OBS over
  a websocket and reads the password out of OBS's per-user config, so a SYSTEM task would
  find neither.

  ⚠️ THE TASK DELIBERATELY DOES NOT RESTART ON FAILURE BY DEFAULT.
  dash.py opens a continuous audio input stream on the Focusrite, and that driver is the
  leading suspect in this machine's `0x139` bugchecks. A crash-loop reopening it
  unattended is the last thing this box needs. Pass -RestartOnFailure only once the BSOD
  investigation is closed, or -NoAudio to remove the exposure entirely.

.PARAMETER WiredSerial
  adb serial of the USB phone. Required when two phones are attached: without it the
  wired-camera controls pick whichever device adb lists first, which sent them to the
  WiFi phone and killed its stream.

.EXAMPLE
  .\install-dash-task.ps1 -WiredSerial 38021FDJH004KS -RigCam http://192.168.1.234:8090
.EXAMPLE
  .\install-dash-task.ps1 -Uninstall
#>
[CmdletBinding()]
param(
  [string]$WiredSerial,
  [string]$RigCam = "http://127.0.0.1:8090",
  [string]$Source = "Pixel 8",
  [switch]$NoAudio,
  [switch]$RestartOnFailure,
  [switch]$Uninstall
)

$TaskName = "Riomhdhos dashboard"

if ($Uninstall) {
  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    "removed scheduled task '$TaskName'"
  } else { "no task named '$TaskName'" }
  return
}

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dash = Join-Path $here "dash.py"
if (-not (Test-Path $dash)) { throw "dash.py not found next to this script ($dash)" }

$python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
if (-not $python) { throw "no python on PATH" }
# pythonw keeps it windowless; python.exe would leave a console open on the desktop all day.

$argList = @("`"$dash`"", "--source", "`"$Source`"", "--rigcam", $RigCam)
if ($WiredSerial) { $argList += @("--uvc-serial", $WiredSerial) }
else { Write-Warning "No -WiredSerial given. With two phones attached the wired-camera controls will refuse to act rather than guess." }
if ($NoAudio) { $argList += "--no-audio" }

$action  = New-ScheduledTaskAction -Execute $python -Argument ($argList -join " ") -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Delay so OBS and the audio devices are up first; the dashboard fails fast on no OBS.
$trigger.Delay = "PT45S"

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
if ($RestartOnFailure) {
  $settings.RestartCount = 3
  $settings.RestartInterval = "PT2M"
  Write-Warning "Restart-on-failure enabled: a crash loop will keep reopening the Focusrite stream."
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Settings $settings -Description "Riomhdhos video rig dashboard (port 8770)" -Force | Out-Null

"registered '$TaskName'"
"  python : $python"
"  args   : $($argList -join ' ')"
"  runs   : at logon as $env:USERNAME, 45s delay"
""
"Start it now with:  Start-ScheduledTask -TaskName '$TaskName'"
"⚠️ Stop any dash.py you already have running first - two instances fight over port 8770,"
"   and the loser exits silently while the winner serves stale code. Check the hash the"
"   server prints against /api/state's 'code' field if the dashboard ever looks out of date."
