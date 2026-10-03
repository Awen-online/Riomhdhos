<#
.SYNOPSIS
  Repoint the scheduled tasks at a phone's current IP. Needs an elevated shell.

.DESCRIPTION
  The vcam bridge and the dashboard take the phone's address on their command line, so
  when DHCP moves a handset both tasks keep dialling the old number. Riastrad then shows
  NOT CONNECTED, the virtual camera goes green, and the Reset button does nothing because
  there is nothing on the other end to reset.

  This is the stopgap, not the cure - see DISCOVERY.md for why the address should not be
  written down at all. It exists because the tasks are registered in a way that needs
  administrator rights to edit, so the obvious one-line fix is not available from the
  ordinary session where the problem gets noticed.

      .\fix-phone-ip.ps1 -Old 192.168.1.168 -New 192.168.1.169

  -WhatIf shows what would change and edits nothing.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$Old,
    [Parameter(Mandatory = $true)][string]$New
)

$p = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "This needs an elevated PowerShell. Right-click > Run as administrator." -ForegroundColor Red
    exit 1
}

foreach ($pat in @($Old, $New)) {
    if ($pat -notmatch '^\d{1,3}(\.\d{1,3}){3}$') {
        Write-Host "not an IPv4 address: $pat" -ForegroundColor Red; exit 1
    }
}

# Only touch this rig's own tasks, and only ones that actually mention the old address.
$hit = 0
foreach ($t in Get-ScheduledTask | Where-Object { $_.TaskName -like 'Riomhdhos*' }) {
    $act = $t.Actions[0]
    if (-not $act.Arguments -or $act.Arguments -notlike "*$Old*") { continue }
    $hit++
    # ⚠️ NOT $new. PowerShell variable names are CASE-INSENSITIVE, so `$new` and the
    # `$New` parameter are one variable. The first task through this loop overwrote the
    # replacement IP with its own full argument string, and the second task then had that
    # entire string substituted into it in place of the address - producing a command
    # line with dash.py's arguments spliced inside vcambridge's --url. The task failed
    # with "file not found" and the camera went green. It did this silently: both tasks
    # reported as updated, and the damage only showed when the task next tried to start.
    $newArgs = $act.Arguments -replace [regex]::Escape($Old), $New
    # Cheap proof the substitution did what it should: swapping one IP for another can
    # only change the length by the difference between them.
    $delta = [math]::Abs($newArgs.Length - $act.Arguments.Length)
    if ($delta -gt [math]::Abs($New.Length - $Old.Length)) {
        Write-Host ("  {0}: REFUSED - substitution changed the length by {1}, which is " +
                    "not an IP swap. Nothing was written." -f $t.TaskName, $delta) -ForegroundColor Red
        continue
    }
    Write-Host ("  {0}" -f $t.TaskName)
    Write-Host ("    was: {0}" -f $act.Arguments) -ForegroundColor DarkGray
    Write-Host ("    now: {0}" -f $newArgs) -ForegroundColor Green
    if ($PSCmdlet.ShouldProcess($t.TaskName, "repoint $Old -> $New")) {
        $a = if ([string]::IsNullOrEmpty($act.WorkingDirectory)) {
            New-ScheduledTaskAction -Execute $act.Execute -Argument $newArgs
        } else {
            New-ScheduledTaskAction -Execute $act.Execute -Argument $newArgs `
                                    -WorkingDirectory $act.WorkingDirectory
        }
        Set-ScheduledTask -TaskName $t.TaskName -Action $a | Out-Null
        # Bounce it so the new address is used now rather than at the next reboot.
        try { Stop-ScheduledTask -TaskName $t.TaskName -ErrorAction SilentlyContinue } catch {}
        Start-Sleep -Milliseconds 400
        Start-ScheduledTask -TaskName $t.TaskName
        Write-Host "    restarted" -ForegroundColor Green
    }
}

if ($hit -eq 0) {
    Write-Host "no Riomhdhos task mentions $Old - nothing to do" -ForegroundColor Yellow
} else {
    Write-Host ""
    Write-Host "$hit task(s) handled. Check the Cams tab, and the bridge log:"
    Write-Host "    Get-Content C:\Users\mccul\rig\logs\vcam-p8.log -Tail 5"
}
