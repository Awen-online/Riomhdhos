<#
.SYNOPSIS
  Register the virtual-camera bridge as a scheduled task so it survives a reboot.

.DESCRIPTION
  vcambridge.py decodes the phone's H.264 stream on this PC and feeds it into the OBS
  Virtual Camera sink, which OBS reads as an ordinary webcam. Measured, that path is
  414 ms against 853 ms for OBS's own Media Source - so the bridge is not an optimisation,
  it is how the WiFi camera becomes usable.

  Which makes it a single point of failure: if this process stops, the virtual camera goes
  cold and that feed is simply gone. Exactly the problem the foreground service fixed on the
  phone side, and it deserves the same treatment here.

  Runs at logon in the user's own session - NOT as SYSTEM, which has no access to the
  desktop's DirectShow devices.

  ⚠️ RESTART ON FAILURE IS ON BY DEFAULT HERE, unlike the dashboard task. The dashboard
  holds a Focusrite audio stream and a crash loop there would hammer the driver suspected in
  this machine's bugchecks. This bridge touches no audio hardware - it reads a socket and
  writes a virtual camera - so restarting it is cheap and losing it is expensive.

.PARAMETER Url
  The phone's stream. Use the adb-forward address if the phone is wired for development.

.EXAMPLE
  .\install-vcam-task.ps1
.EXAMPLE
  .\install-vcam-task.ps1 -Url http://127.0.0.1:8091/stream.h264
.EXAMPLE
  .\install-vcam-task.ps1 -Uninstall
#>
[CmdletBinding()]
param(
  [string]$Url = "http://192.168.1.234:8090/stream.h264",
  [string]$Api = "http://192.168.1.234:8090/api/state",
  [string]$Size,
  [switch]$NoRestartOnFailure,
  [switch]$Uninstall
)

$TaskName = "Riomhdhos vcam bridge"

if ($Uninstall) {
  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    "removed scheduled task '$TaskName'"
  } else { "no task named '$TaskName'" }
  return
}

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "vcambridge.py"
if (-not (Test-Path $script)) { throw "vcambridge.py not found next to this script" }

$python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $python) { $python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
if (-not $python) { throw "no python on PATH" }

$argList = @("`"$script`"", "--url", $Url, "--api", $Api)
if ($Size) { $argList += @("--size", $Size) }

$action  = New-ScheduledTaskAction -Execute $python -Argument ($argList -join " ") -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Later than the dashboard's 45 s: the phone has to be on the network and serving before
# there is anything to decode.
$trigger.Delay = "PT90S"

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
if (-not $NoRestartOnFailure) {
  # The phone may not be up yet at logon, and the bridge exits when the stream ends.
  $settings.RestartCount = 999
  $settings.RestartInterval = "PT1M"
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Settings $settings -Description "Riomhdhos WiFi camera -> OBS Virtual Camera bridge" -Force | Out-Null

"registered '$TaskName'"
"  python : $python"
"  args   : $($argList -join ' ')"
"  runs   : at logon as $env:USERNAME, 90s delay, retry every minute"
""
"Start it now with:  Start-ScheduledTask -TaskName '$TaskName'"
"⚠️ The OBS Virtual Camera sink takes ONE producer. Stop any vcambridge you already have"
"   running, and do not press OBS's own 'Start Virtual Camera' while this is feeding it."
"Check it is actually delivering with:  python preflight\showcheck.py --only cameras"
