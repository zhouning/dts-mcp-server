<#
.SYNOPSIS
  Start the DTS MCP gateway in the background.

.DESCRIPTION
  Idempotent: returns early if a live process already holds the PID file, and
  cleans the file if the process is gone. Progress is appended to launcher.log
  because a hidden scheduled task produces no visible output, so a startup
  failure would otherwise be invisible.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$stateDirectory = Join-Path $env:LOCALAPPDATA 'DtsMCP'
$environmentFile = Join-Path $stateDirectory 'config\server.env'
$pidFile = Join-Path $stateDirectory 'server.pid'
$logFile = Join-Path $stateDirectory 'server.log'
$launcherLog = Join-Path $stateDirectory 'launcher.log'
$python = Join-Path $repositoryRoot '.venv\Scripts\python.exe'

function Write-Launcher([string]$message) {
    $stamp = (Get-Date).ToString('s')
    Add-Content -Path $launcherLog -Value "$stamp $message" -Encoding utf8
}

Write-Launcher 'start requested'

foreach ($required in @($python, $environmentFile)) {
    if (-not (Test-Path $required)) {
        Write-Launcher "missing prerequisite: $required"
        throw "Missing $required. Run .\scripts\bootstrap.ps1 first."
    }
}

if (Test-Path $pidFile) {
    $existingPid = (Get-Content $pidFile -Raw).Trim()
    $running = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
    if ($running) {
        Write-Host "Already running (PID $existingPid)."
        Write-Launcher "already running: $existingPid"
        return
    }
    Remove-Item $pidFile -Force
    Write-Launcher 'removed stale pid file'
}

# Split on the first '=' only: base64 secrets contain '=' padding.
foreach ($line in Get-Content $environmentFile) {
    if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith('#')) { continue }
    $parts = $line.Split('=', 2)
    if ($parts.Count -eq 2) {
        $value = $parts[1].Trim('"')
        Set-Item -Path "env:$($parts[0])" -Value $value
    }
}

Write-Launcher 'launching'
$process = Start-Process -FilePath $python `
    -ArgumentList @('-m', 'dts_mcp_server') `
    -WorkingDirectory $repositoryRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $logFile `
    -RedirectStandardError "$logFile.err" `
    -PassThru

Set-Content -Path $pidFile -Value $process.Id -Encoding ascii
Write-Launcher "started pid $($process.Id)"

Start-Sleep -Seconds 5
if (-not (Get-Process -Id $process.Id -ErrorAction SilentlyContinue)) {
    Write-Launcher 'process exited during startup'
    throw "Gateway exited during startup. See $logFile.err"
}

Write-Host "Started (PID $($process.Id))."
Write-Host "  log: $logFile"
