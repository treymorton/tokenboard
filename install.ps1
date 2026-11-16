<#
TokenBoard installer - Windows. Clone, run this, open a browser.
No admin, no pip. Registers a Task Scheduler logon task for auto-start.

Usage (PowerShell, from the repo folder):
  powershell -ExecutionPolicy Bypass -File .\install.ps1
  .\install.ps1 -Port 8100 -BindHost 127.0.0.1
  .\install.ps1 -LlamaUrl http://127.0.0.1:8080     # pin llama-server
  .\install.ps1 -HubUrl http://10.0.1.5:8101        # report to a hub
  .\install.ps1 -NoTask                             # start once, no autostart
#>
param(
  [int]$Port = 8100,
  [string]$BoxName = "",
  [string]$LlamaUrl = "",
  [string]$HubUrl = "",
  [string]$BindHost = "127.0.0.1",   # loopback: no Windows Firewall prompt
  [switch]$NoTask
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Say($m)  { Write-Host "`n== $m" -ForegroundColor Green }
function Warn($m) { Write-Host "   ! $m" -ForegroundColor Yellow }

Say "TokenBoard install (Windows)"

# ---------------------------------------------------------------- python
$pyExe = $null
foreach ($cand in @("py", "python", "python3")) {
  $cmd = Get-Command $cand -ErrorAction SilentlyContinue
  if (-not $cmd) { continue }
  try {
    if ($cand -eq "py") {
      $resolved = (& py -3 -c "import sys;print(sys.executable)") 2>$null
    } else {
      $resolved = (& $cand -c "import sys;print(sys.executable)") 2>$null
    }
    if (-not $resolved) { continue }
    $resolved = $resolved.Trim()
    & $resolved -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>$null
    if ($LASTEXITCODE -eq 0) { $pyExe = $resolved; break }
  } catch { }
}
if (-not $pyExe) {
  throw "Python 3.10+ not found. Install from https://python.org and tick 'Add python.exe to PATH', then re-run."
}
$pyVer = (& $pyExe -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')").Trim()
Write-Host "   python $pyVer OK ($pyExe) - stdlib only, nothing to pip install"

# pythonw = same folder, no console window (what the logon task runs)
$pywExe = Join-Path (Split-Path $pyExe) "pythonw.exe"
if (-not (Test-Path $pywExe)) { $pywExe = $pyExe }

# ---------------------------------------------------------------- config
Say "Configuration"
if (-not $BoxName) { $BoxName = $env:COMPUTERNAME }
if (Test-Path config.json) {
  Warn "config.json already exists - leaving it untouched"
} else {
  $cfg = Get-Content config.example.json -Raw | ConvertFrom-Json
  $cfg.box_name   = $BoxName
  $cfg.port       = $Port
  $cfg.bind_host  = $BindHost
  $cfg.llama_url  = $LlamaUrl
  $cfg.hub_url    = $HubUrl
  $cfg | ConvertTo-Json | Set-Content config.json -Encoding UTF8
  Write-Host "   wrote config.json (box '$BoxName', port $Port, bind $BindHost)"
}
Write-Host "   token counting: llama-server.exe with --metrics is auto-discovered"
Write-Host "   (process name must contain 'llama'); pin one with -LlamaUrl or the"
Write-Host "   llama_url setting in config.json. See README 'Windows' for LM Studio"
Write-Host "   and Ollama capabilities."

# ---------------------------------------------------------------- prices
Say "Frontier prices"
& $pyExe scripts\refresh_prices.py 2>$null
if ($LASTEXITCODE -ne 0) {
  Warn "live price fetch failed (offline?) - keeping the bundled snapshot"
  Warn "in prices.json; re-run later: python scripts\refresh_prices.py"
} else {
  Write-Host "   live prices fetched and written to prices.json"
}

# ---------------------------------------------------------------- start now
$root = (Get-Location).Path
function Test-Live {
  try {
    $r = Invoke-RestMethod "http://127.0.0.1:$Port/api/state" -TimeoutSec 3
    return ($null -ne $r)
  } catch { return $false }
}

Say "Starting TokenBoard"
if (Test-Live) {
  Warn "something already answers on :$Port - not starting a second copy"
} else {
  Start-Process -FilePath $pywExe -ArgumentList "`"$root\tokenboard.py`"" `
    -WorkingDirectory $root -WindowStyle Hidden
}

# ---------------------------------------------------------------- autostart
if (-not $NoTask) {
  Say "Auto-start (Task Scheduler, current user, no admin)"
  $action  = New-ScheduledTaskAction -Execute $pywExe `
    -Argument "`"$root\tokenboard.py`"" -WorkingDirectory $root
  $trigger = New-ScheduledTaskTrigger -AtLogOn
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
  Register-ScheduledTask -TaskName "TokenBoard Box" -Action $action `
    -Trigger $trigger -Settings $settings -Force | Out-Null
  Write-Host "   'TokenBoard Box' will start at every logon"
}

# ---------------------------------------------------------------- health
Say "Health check"
$ok = $false
1..15 | ForEach-Object {
  if (-not $ok) { if (Test-Live) { $script:ok = $true } else { Start-Sleep 2 } }
}
if (-not $ok) { throw "dashboard did not come up on :$Port - check tokenboard.py output: run 'pythonw tokenboard.py' manually or see README troubleshooting" }

$lanIp = try {
  $s = New-Object System.Net.Sockets.UdpClient
  $s.Connect("8.8.8.8", 80); ($s.localendpoint).Address; $s.Close()
} catch { "127.0.0.1" }

Write-Host "`n== TokenBoard is live" -ForegroundColor Green
Write-Host "   this machine :  http://localhost:$Port"
if ($BindHost -ne "127.0.0.1") {
  Write-Host "   on the LAN   :  http://${lanIp}:$Port  (firewall: allow python when prompted)"
} else {
  Write-Host "   bound to loopback - set bind_host to 0.0.0.0 in config.json to share on the LAN"
}
Write-Host "`n   stop now     :  Stop-Process -Name pythonw   (or Task Manager)"
Write-Host "   remove task  :  Unregister-ScheduledTask -TaskName 'TokenBoard Box' -Confirm:`$false"
