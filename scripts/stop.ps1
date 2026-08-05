<#
.SYNOPSIS
  Stop the DTS MCP gateway.
#>
[CmdletBinding()]
param([switch]$Force)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$stateDirectory = Join-Path $env:LOCALAPPDATA 'DtsMCP'
$pidFile = Join-Path $stateDirectory 'server.pid'

if (-not (Test-Path $pidFile)) {
    Write-Host 'Not running (no PID file).'
    return
}

$serverPid = (Get-Content $pidFile -Raw).Trim()
$process = Get-Process -Id $serverPid -ErrorAction SilentlyContinue
if (-not $process) {
    Write-Host "No process $serverPid; removing stale PID file."
    Remove-Item $pidFile -Force
    return
}

# Graceful first: an in-flight publish should be allowed to finish writing.
$process.CloseMainWindow() | Out-Null
for ($waited = 0; $waited -lt 10; $waited++) {
    Start-Sleep -Seconds 1
    if (-not (Get-Process -Id $serverPid -ErrorAction SilentlyContinue)) { break }
}

if (Get-Process -Id $serverPid -ErrorAction SilentlyContinue) {
    if (-not $Force) {
        Write-Warning "PID $serverPid did not exit. Re-run with -Force to kill it."
        return
    }
    Stop-Process -Id $serverPid -Force
    Write-Host "Killed PID $serverPid."
}
else {
    Write-Host "Stopped PID $serverPid."
}

Remove-Item $pidFile -Force
