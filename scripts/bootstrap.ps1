<#
.SYNOPSIS
  Prepare this host to serve the DTS MCP gateway: secrets, address, certificates.

.DESCRIPTION
  Idempotent. Re-running preserves the bearer token and signing key, because
  rewriting them would invalidate every configured client and every already
  issued artifact URL. If the host's LAN address has changed since the last run,
  only that line is rewritten and certificate reissue is escalated
  automatically — a certificate whose SAN no longer matches the dialled address
  fails at connect time with an error that looks nothing like its cause.

.PARAMETER RotateSecrets
  Replace the bearer token and signing key. Every client must be reconfigured.

.PARAMETER RotateCertificates
  Reissue the CA and server certificate. Clients must re-import the CA.
#>
[CmdletBinding()]
param(
    [switch]$RotateSecrets,
    [switch]$RotateCertificates
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$stateDirectory = Join-Path $env:LOCALAPPDATA 'DtsMCP'
$configDirectory = Join-Path $stateDirectory 'config'
$certificateDirectory = Join-Path $stateDirectory 'certs'
$environmentFile = Join-Path $configDirectory 'server.env'
$python = Join-Path $repositoryRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    throw "Virtualenv not found at $python. Create it with: python -m venv .venv; .venv\Scripts\python.exe -m pip install -e '.[dev]'"
}

foreach ($directory in @($stateDirectory, $configDirectory, $certificateDirectory)) {
    if (-not (Test-Path $directory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
}

# --- address -----------------------------------------------------------------
# Single source of truth: the certificate SAN, the published base URL and the
# firewall scope all derive from this one value.
$serverIp = & $python -c @"
import sys
sys.path.insert(0, r'$repositoryRoot\src')
from dts_mcp_server.host_address import resolve_server_ip
print(resolve_server_ip())
"@
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($serverIp)) {
    throw 'Could not determine this host LAN address. Set DTS_MCP_SERVER_IP and retry.'
}
$serverIp = $serverIp.Trim()
Write-Host "Server address: $serverIp"

# --- secrets -----------------------------------------------------------------
function New-Secret {
    $bytes = [byte[]]::new(48)
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    return [Convert]::ToBase64String($bytes)
}

$utf8NoBom = [Text.UTF8Encoding]::new($false)

if ((-not (Test-Path $environmentFile)) -or $RotateSecrets) {
    if ($RotateSecrets -and (Test-Path $environmentFile)) {
        Write-Warning 'Rotating secrets. Every client must be reconfigured.'
    }
    $lines = @(
        "DTS_MCP_BEARER_TOKEN=$(New-Secret)"
        "DTS_MCP_ARTIFACT_SIGNING_KEY=$(New-Secret)"
        "DTS_MCP_SERVER_IP=$serverIp"
        "DTS_MCP_BIND_PORT=8770"
    )
    [IO.File]::WriteAllLines($environmentFile, $lines, $utf8NoBom)
    $RotateCertificates = $true
    Write-Host "Wrote $environmentFile"
}
else {
    # Rewrite only the address line. Rewriting the file wholesale would discard
    # the secrets and break every client.
    $existing = [IO.File]::ReadAllLines($environmentFile)
    $updated = $false
    for ($i = 0; $i -lt $existing.Count; $i++) {
        if ($existing[$i] -like 'DTS_MCP_SERVER_IP=*') {
            $current = ($existing[$i] -split '=', 2)[1]
            if ($current -ne $serverIp) {
                Write-Host "Address changed: $current -> $serverIp"
                $existing[$i] = "DTS_MCP_SERVER_IP=$serverIp"
                $updated = $true
            }
        }
    }
    if ($updated) {
        [IO.File]::WriteAllLines($environmentFile, $existing, $utf8NoBom)
        $RotateCertificates = $true
        Write-Host 'Certificates will be reissued for the new address.'
    }
    else {
        Write-Host 'Existing configuration is current; secrets preserved.'
    }
}

# Owner-only, inheritance stripped: these files are the whole auth scheme.
& icacls.exe $configDirectory /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" | Out-Null

# --- certificates ------------------------------------------------------------
$arguments = @(
    '-m', 'dts_mcp_server.tls_bootstrap',
    '--output-dir', $certificateDirectory,
    '--ip', $serverIp,
    '--repository-root', $repositoryRoot
)
if ($RotateCertificates) { $arguments += '--rotate' }

Push-Location $repositoryRoot
try {
    $caPath = & $python @arguments
    if ($LASTEXITCODE -ne 0) { throw 'Certificate generation failed.' }
}
finally {
    Pop-Location
}
& icacls.exe $certificateDirectory /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" | Out-Null

Write-Host ''
Write-Host 'Bootstrap complete.'
Write-Host "  CA certificate: $caPath"
Write-Host "  Endpoint:       https://${serverIp}:8770/mcp"
Write-Host ''
Write-Host 'Next steps:'
Write-Host '  1. .\scripts\install_scheduled_task.ps1   (autostart + firewall)'
Write-Host '  2. Copy the CA certificate to the client and run configure-macos.sh'
Write-Host "  3. Read the bearer token from $environmentFile"
