# Rolling DPC / ISR sampler for Riomhdhos.
#
# WHY THIS EXISTS SEPARATELY FROM THE AGENT: the agent is a single-threaded HTTP
# listener. Sampling performance counters properly takes seconds - Get-Counter needs
# a real interval to produce a meaningful rate - so doing it inside a request would
# make every health poll block for that long. This samples continuously and writes a
# rolling log; the agent just reads the tail and summarises. Sampling is decoupled
# from serving.
#
# WHY NOT LATENCYMON: it is a GUI app and this box is headless, so running it means
# RDP - and RDP is itself a heavy DPC source AND hijacks the audio device, so it would
# characterise the RDP session rather than the rig. These counters are lower fidelity
# (no per-driver attribution) but they measure the machine as it actually runs.
param(
  [int]$IntervalSec = 5,
  [string]$OutFile  = "C:\Users\mccul\rig\latency.csv",
  [int]$MaxRows     = 20000          # ~28h at 5s. Trimmed in place, never grows unbounded.
)

$paths = @(
  '\Processor(_Total)\% DPC Time',
  '\Processor(_Total)\% Interrupt Time',
  '\Processor(_Total)\% Processor Time',
  '\System\Processor Queue Length'
)

$dir = Split-Path $OutFile -Parent
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
if (-not (Test-Path $OutFile)) {
  "time,dpc_pct,isr_pct,cpu_pct,queue_len,reaper_cpu_pct" | Set-Content $OutFile -Encoding ascii
}

$prevT = $null; $prevCpu = $null; $tick = 0

while ($true) {
  try {
    $s = (Get-Counter -Counter $paths -ErrorAction Stop).CounterSamples
    $get = {
      param($frag)
      $v = ($s | Where-Object { $_.Path -like "*$frag*" } | Select-Object -First 1).CookedValue
      if ($null -eq $v) { '' } else { [math]::Round($v, 3) }
    }

    # REAPER's CPU as a share of ONE core, from the delta in process CPU seconds.
    # Deliberately not Get-Counter: the process counter name is instance-indexed and
    # renames itself when more than one reaper.exe has ever run.
    $rcpu = ''
    $rp = Get-Process reaper -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($rp) {
      $now = Get-Date
      if ($null -ne $prevT) {
        $dt = ($now - $prevT).TotalSeconds
        if ($dt -gt 0) { $rcpu = [math]::Round((($rp.CPU - $prevCpu) / $dt) * 100, 2) }
      }
      $prevT = $now; $prevCpu = $rp.CPU
    } else { $prevT = $null; $prevCpu = $null }

    # No 'N' format specifiers anywhere here: they insert thousands separators, and a
    # thousands separator is a comma, inside a comma-separated file.
    $line = "{0:yyyy-MM-dd HH:mm:ss},{1},{2},{3},{4},{5}" -f (Get-Date),
      (& $get 'DPC Time'), (& $get 'Interrupt Time'),
      (& $get 'Processor Time'), (& $get 'Processor Queue Length'), $rcpu
    Add-Content -Path $OutFile -Value $line -Encoding ascii
  } catch {
    Add-Content -Path $OutFile -Value ("{0:yyyy-MM-dd HH:mm:ss},,,,," -f (Get-Date)) -Encoding ascii
  }

  # Trim occasionally rather than every tick - rewriting the file is the expensive part,
  # and doing expensive things on a schedule is how a monitor becomes the problem it
  # is supposed to be watching for.
  $tick++
  if ($tick % 720 -eq 0) {
    try {
      $all = Get-Content $OutFile
      if ($all.Count -gt ($MaxRows + 1)) {
        $keep = @($all[0]) + $all[($all.Count - $MaxRows)..($all.Count - 1)]
        Set-Content -Path $OutFile -Value $keep -Encoding ascii
      }
    } catch { }
  }

  Start-Sleep -Seconds $IntervalSec
}
