<#
.SYNOPSIS
  The four maintenance jobs on this rig that need an elevated shell. Run once.

.DESCRIPTION
  Each of these was attempted from the ordinary session and refused for lack of
  elevation. They are collected here so they take one UAC prompt between them rather
  than four separate hunts through Settings.

  Right-click PowerShell > Run as administrator, then:
      cd C:\Users\mccul\Riomhdhos\video\rigdash
      .\elevated-housekeeping.ps1

  Nothing here touches the stream path - no OBS, no relay, no camera bridges. It is safe
  to run while the rig is idle and safe to run twice; every step checks before acting.
  -WhatIf shows what each would do and changes nothing.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param()

$GB = 1073741824

function Need-Admin {
    $p = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host "This needs an elevated PowerShell. Right-click > Run as administrator." -ForegroundColor Red
        exit 1
    }
}
Need-Admin

$free0 = (Get-PSDrive C).Free
Write-Host ("C: free before: {0:N1} GB" -f ($free0 / $GB))
Write-Host ""

# ---------------------------------------------------------------- 1. network profile
# Ethernet is classified Public, which is wrong for a home LAN: it disables network
# discovery and is the kind of thing Windows silently re-evaluates after a blip, which
# would cut the phone off from the dashboard with no obvious cause. Not currently
# blocking anything - the Python firewall rules cover the Public profile - so this is
# removing a latent failure, not fixing an active one.
$prof = Get-NetConnectionProfile -InterfaceAlias Ethernet -ErrorAction SilentlyContinue
if ($prof -and $prof.NetworkCategory -ne 'Private') {
    if ($PSCmdlet.ShouldProcess("Ethernet", "set NetworkCategory to Private")) {
        Set-NetConnectionProfile -InterfaceAlias Ethernet -NetworkCategory Private
        Write-Host "  network profile -> Private" -ForegroundColor Green
    }
} else {
    Write-Host "  network profile already Private, skipped"
}

# ---------------------------------------------------------------- 2. adobeTemp
# Three installer extraction directories (ETR*.tmp), newest from 4 September. These are
# Adobe's unpacked installers, NOT installed software - every Adobe app lives in
# Program Files and is untouched by this. Owned by BUILTIN\Administrators, which is why
# the ordinary session cannot remove them.
if (Test-Path 'C:\adobeTemp') {
    $sz = (Get-ChildItem 'C:\adobeTemp' -Recurse -File -Force -ErrorAction SilentlyContinue |
           Measure-Object Length -Sum).Sum
    Write-Host ("  adobeTemp: {0:N2} GB" -f ($sz / $GB))
    # ⚠️ An install in flight would be using these. Nothing has written here since
    # September, but check rather than assume - a half-deleted installer payload is a
    # worse problem than the disk space.
    $recent = Get-ChildItem 'C:\adobeTemp' -Recurse -File -Force -ErrorAction SilentlyContinue |
              Where-Object { $_.LastWriteTime -gt (Get-Date).AddHours(-2) }
    if ($recent) {
        Write-Host "  SKIPPED - files written in the last 2 hours; an install may be running" -ForegroundColor Yellow
    } elseif ($PSCmdlet.ShouldProcess('C:\adobeTemp', 'delete recursively')) {
        Remove-Item -LiteralPath 'C:\adobeTemp' -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path 'C:\adobeTemp') { Write-Host "  adobeTemp: partially removed (some files locked)" -ForegroundColor Yellow }
        else { Write-Host "  adobeTemp removed" -ForegroundColor Green }
    }
} else {
    Write-Host "  adobeTemp already gone, skipped"
}

# ---------------------------------------------------------------- 3. component store
# WinSxS apparent size is ~20 GB but it is hardlink-heavy, so that figure overstates real
# usage badly. How much this reclaims genuinely cannot be known in advance; the analyse
# step below reports it rather than guessing. Takes several minutes.
if ($PSCmdlet.ShouldProcess('WinSxS', 'analyse then clean the component store')) {
    Write-Host "  analysing the component store (slow)..."
    & DISM /Online /Cleanup-Image /AnalyzeComponentStore | Select-String -Pattern 'Reclaimable|Recommended' |
        ForEach-Object { Write-Host ("    " + $_.Line.Trim()) }
    Write-Host "  cleaning..."
    & DISM /Online /Cleanup-Image /StartComponentCleanup | Select-String -Pattern 'completed|error' |
        ForEach-Object { Write-Host ("    " + $_.Line.Trim()) }
}

# ---------------------------------------------------------------- 4. hibernation
# ⚠️ NOT DONE AUTOMATICALLY, AND THAT IS DELIBERATE. hiberfil.sys is 12.77 GB - the single
# biggest remaining win - but turning it off also disables Fast Startup, which changes how
# this machine boots every day thereafter. That is a behaviour change, not housekeeping,
# and it is Ian's call rather than a script's.
$hib = 'C:\hiberfil.sys'
$hibSz = if (Test-Path $hib) { (Get-Item $hib -Force).Length } else { 0 }
if ($hibSz -gt 0) {
    Write-Host ""
    Write-Host ("  hiberfil.sys is {0:N2} GB and was NOT removed." -f ($hibSz / $GB))
    Write-Host "  It is the largest remaining win, but 'powercfg /h off' also kills Fast"
    Write-Host "  Startup. Run it yourself if you want that trade:"
    Write-Host "      powercfg /h off" -ForegroundColor Cyan
}

Write-Host ""
$free1 = (Get-PSDrive C).Free
$c = Get-PSDrive C
Write-Host ("C: free after : {0:N1} GB  ({1:N2}%)" -f ($free1 / $GB), (100 * $c.Free / ($c.Free + $c.Used)))
Write-Host ("reclaimed     : {0:N2} GB" -f (($free1 - $free0) / $GB))
