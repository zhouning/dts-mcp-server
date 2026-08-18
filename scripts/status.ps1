<#
.SYNOPSIS
  Report gateway state: process, port, certificate validity, endpoint health.

.DESCRIPTION
  The certificate check is the one worth reading: an expired leaf, or one whose
  SAN no longer matches this host's current address, produces a client-side
  failure that looks like a network problem.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
Set-StrictMode -Version Latest

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$stateDirectory = Join-Path $env:LOCALAPPDATA 'DtsMCP'
$pidFile = Join-Path $stateDirectory 'server.pid'
$environmentFile = Join-Path $stateDirectory 'config\server.env'
$serverCertificate = Join-Path $stateDirectory 'certs\server.crt'
$python = Join-Path $repositoryRoot '.venv\Scripts\python.exe'

Write-Host 'DTS MCP gateway status'
Write-Host '----------------------'

# --- process -----------------------------------------------------------------
if (Test-Path $pidFile) {
    $serverPid = (Get-Content $pidFile -Raw).Trim()
    if (Get-Process -Id $serverPid -ErrorAction SilentlyContinue) {
        Write-Host "process    : running (PID $serverPid)"
    }
    else {
        Write-Host "process    : NOT running (stale PID file: $serverPid)"
    }
}
else {
    Write-Host 'process    : not running'
}

# --- configured address ------------------------------------------------------
$configuredIp = $null
$port = '8770'
if (Test-Path $environmentFile) {
    foreach ($line in Get-Content $environmentFile) {
        $parts = $line.Split('=', 2)
        if ($parts.Count -ne 2) { continue }
        switch ($parts[0]) {
            'DTS_MCP_SERVER_IP' { $configuredIp = $parts[1].Trim('"') }
            'DTS_MCP_BIND_PORT' { $port = $parts[1].Trim('"') }
        }
    }
    Write-Host "configured : ${configuredIp}:${port}"
}
else {
    Write-Host 'configured : MISSING (run bootstrap.ps1)'
}

# --- current address, and drift ---------------------------------------------
if (Test-Path $python) {
    $currentIp = (& $python -c @"
import sys
sys.path.insert(0, r'$repositoryRoot\src')
from dts_mcp_server.host_address import resolve_server_ip
print(resolve_server_ip())
"@ 2>$null)
    if ($currentIp) {
        $currentIp = $currentIp.Trim()
        Write-Host "detected   : $currentIp"
        if ($configuredIp -and $currentIp -ne $configuredIp) {
            Write-Warning "Address drift: certificate is bound to $configuredIp but this host is now $currentIp. Re-run bootstrap.ps1 to reissue, then re-import the CA on clients."
        }
    }
}

# --- listener ----------------------------------------------------------------
$listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
Write-Host ("port       : " + $(if ($listening) { "listening on $port" } else { "NOT listening on $port" }))

# --- certificate -------------------------------------------------------------
if (Test-Path $serverCertificate) {
    $certificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new($serverCertificate)
    $daysLeft = [int]($certificate.NotAfter - (Get-Date)).TotalDays
    Write-Host "certificate: CN=$($certificate.Subject) expires $($certificate.NotAfter.ToString('yyyy-MM-dd')) ($daysLeft days)"
    if ($daysLeft -lt 30) {
        Write-Warning 'Certificate expires soon. Re-run bootstrap.ps1 -RotateCertificates.'
    }
}
else {
    Write-Host 'certificate: MISSING (run bootstrap.ps1)'
}

# --- health ------------------------------------------------------------------
if ($configuredIp -and (Test-Path $python)) {
    # Probed through Python rather than Invoke-WebRequest: -SkipCertificateCheck
    # only exists on PowerShell 7+, and on the Windows PowerShell 5.1 that ships
    # with the OS this step would report every healthy gateway as unreachable.
    #
    # The gateway uses a private CA, so chain validation is skipped here; this
    # checks liveness, not trust. Client-side trust is verified by
    # verify-connection.sh. ProxyHandler({}) keeps a system HTTPS proxy from
    # intercepting the LAN address and stalling the handshake.
    $health = & $python -c @"
import ssl, urllib.request
context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
opener = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=context), urllib.request.ProxyHandler({})
)
try:
    with opener.open('https://${configuredIp}:${port}/healthz', timeout=5) as response:
        print(response.read(200).decode('utf-8', 'replace').strip())
except Exception as error:
    print(f'unreachable ({type(error).__name__}: {error})')
"@ 2>$null
    if ([string]::IsNullOrWhiteSpace($health)) { $health = 'unreachable (probe failed)' }
    Write-Host "health     : $health"
}
