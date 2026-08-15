# install-reaper-task.ps1
# Creates the scheduled task the agent uses to launch REAPER. Run once, elevated, on the rig.
#
# WHY A TASK AND NOT JUST Start-Process
# The agent runs as SYSTEM in session 0. A process it starts directly lands in session 0
# too, where there is no audio device and no desktop - REAPER would come up deaf and
# invisible. A task with an Interactive principal runs in the logged-on user's session
# instead, which on this box is the auto-logged-on console session 1, holding the real
# ASIO device. This is the same reason you must never relaunch REAPER from inside an RDP
# session: what matters is not who starts it but which session it lands in.
#
# ON-DEMAND ONLY, DELIBERATELY
# No trigger is attached, so nothing about the rig's boot behaviour changes. Powering the
# box on gets you a booted machine; the phone's "Start REAPER" then brings the rig up.
# Add -AtLogon if you would rather it come up by itself.

[CmdletBinding()]
param(
  [string] $TaskName = 'Riomhdhos REAPER',
  [string] $ReaperExe = 'C:\Program Files\REAPER (x64)\reaper.exe',
  [string] $Project   = 'C:\Users\mccul\rig\Riomhdhos-brain.rpp',
  [string] $RunAsUser = '',        # defaults to the console user
  [switch] $AtLogon                # attach a logon trigger so the rig starts itself
)

$ErrorActionPreference = 'Stop'

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal $id).IsInRole('Administrators')) {
  throw 'Run this elevated.'
}
if (-not (Test-Path $ReaperExe)) { throw "REAPER not found at $ReaperExe" }
if (-not (Test-Path $Project))   { throw "Project not found at $Project" }

# Resolve the interactive user by asking the machine who is actually at the console,
# rather than assuming an account name. The rig auto-logs-on, so this is stable, and a
# wrong answer here is the difference between REAPER opening on the desk and opening
# nowhere at all.
if (-not $RunAsUser) {
  $consoleUser = (Get-CimInstance Win32_ComputerSystem).UserName
  if (-not $consoleUser) { throw 'Nobody is logged on at the console; cannot resolve the interactive user.' }
  $RunAsUser = $consoleUser
}
Write-Host "Interactive user: $RunAsUser" -ForegroundColor Green

$action = New-ScheduledTaskAction -Execute $ReaperExe -Argument "`"$Project`"" `
            -WorkingDirectory (Split-Path $ReaperExe)
$principal = New-ScheduledTaskPrincipal -UserId $RunAsUser -LogonType Interactive -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
               -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

$params = @{
  TaskName  = $TaskName
  Action    = $action
  Principal = $principal
  Settings  = $settings
}
if ($AtLogon) { $params.Trigger = New-ScheduledTaskTrigger -AtLogOn -User $RunAsUser }

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask @params | Out-Null

Write-Host "Registered '$TaskName'" -ForegroundColor Green
Write-Host "  exe     : $ReaperExe"
Write-Host "  project : $Project"
Write-Host "  trigger : $(if ($AtLogon) { 'at logon' } else { 'on demand only' })"
Write-Host ''
Write-Host "Wire the agent to it with:  install-agent.ps1 -ReaperTask `"$TaskName`"" -ForegroundColor Cyan
