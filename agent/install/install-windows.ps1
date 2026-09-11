<#
.SYNOPSIS
  Install the Avalon Monitor agent on Windows as a Scheduled Task that runs at boot (as SYSTEM).

.EXAMPLE
  # In an elevated PowerShell, from the copied agent\ folder:
  Set-ExecutionPolicy -Scope Process Bypass
  .\install\install-windows.ps1 -Server http://avalon:8787 -Token avm_xxx [-Name desktop-c] [-Interval 5]

  .\install\install-windows.ps1 -Uninstall

.NOTES
  Requires Python 3.8+ (python.org installer or `winget install Python.Python.3.12`).
  For CPU temperature and AMD/Intel GPU stats, run LibreHardwareMonitor with its
  web server enabled and add AVM_LHM_URL=http://localhost:8085/data.json to agent.conf.
#>
[CmdletBinding()]
param(
  [string]$Server,
  [string]$Token,
  [string]$Name = "",
  [int]$Interval = 5,
  [switch]$Uninstall,
  [switch]$Upgrade
)
$ErrorActionPreference = "Stop"
$TaskName = "AvalonMonitorAgent"
$AppDir = Join-Path $env:ProgramData "AvalonAgent"
$SrcDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw "Run this from an elevated (Administrator) PowerShell."
}

if ($Uninstall) {
  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  }
  if (Test-Path $AppDir) { Remove-Item -Recurse -Force $AppDir }
  Write-Host "Avalon agent removed."
  return
}
if (-not $Upgrade -and (-not $Server -or -not $Token)) { throw "-Server and -Token are required (or -Upgrade)." }

# --- locate python
$py = $null
foreach ($cand in @("py -3", "python", "python3")) {
  try {
    $v = & cmd /c "$cand -c `"import sys;print(sys.executable)`"" 2>$null
    if ($LASTEXITCODE -eq 0 -and $v) { $py = $v.Trim(); break }
  } catch {}
}
if (-not $py) { throw "Python 3 not found. Install it (winget install Python.Python.3.12) and re-run." }
Write-Host "==> using $py"

New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
Copy-Item (Join-Path $SrcDir "avalon_agent.py") (Join-Path $AppDir "avalon_agent.py") -Force

$venv = Join-Path $AppDir "venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
  Write-Host "==> creating venv"
  & $py -m venv $venv
}
$vpy = Join-Path $venv "Scripts\python.exe"
$vpyw = Join-Path $venv "Scripts\pythonw.exe"
& $vpy -m pip install --quiet --upgrade pip
& $vpy -m pip install --quiet -r (Join-Path $SrcDir "requirements.txt")

$conf = Join-Path $AppDir "agent.conf"
if (-not $Upgrade) {
  $lines = @("AVM_SERVER_URL=$Server", "AVM_TOKEN=$Token", "AVM_INTERVAL=$Interval", "AVM_PROCESSES=8")
  if ($Name) { $lines += "AVM_HOSTNAME=$Name" }
  $lines += "# AVM_LHM_URL=http://localhost:8085/data.json"
  Set-Content -Path $conf -Value $lines -Encoding ASCII
  # config holds the token: restrict to Administrators + SYSTEM
  icacls $conf /inheritance:r /grant:r "SYSTEM:F" /grant:r "Administrators:F" | Out-Null
}

Write-Host "==> checking connectivity"
& $vpy (Join-Path $AppDir "avalon_agent.py") --config $conf --check
if ($LASTEXITCODE -ne 0) { Write-Warning "hub check failed - the task will keep retrying once the config is fixed." }

Write-Host "==> registering scheduled task $TaskName"
$action = New-ScheduledTaskAction -Execute $vpyw -Argument "`"$AppDir\avalon_agent.py`" --config `"$conf`"" -WorkingDirectory $AppDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
  Set-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
} else {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Avalon Monitor agent" | Out-Null
}
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2
Write-Host ("Task state: " + (Get-ScheduledTask -TaskName $TaskName).State)
Write-Host "Done. Config: $conf   Logs: run `"$vpy $AppDir\avalon_agent.py --config $conf`" interactively to see output."
