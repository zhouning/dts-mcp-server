<#
.SYNOPSIS
  Autostart the gateway at logon and scope the inbound firewall rule.

.DESCRIPTION
  A logon scheduled task, not a Windows service: DTS Engine is a licensed
  desktop product whose licence check and GUI subsystem expect an interactive
  session. Session 0 does not provide one, so the host must be logged in.

  The firewall rule is scoped to the local /24 on the Private profile only.
  Re-running re-points an existing rule rather than leaving it: a rule pinned to
  a stale subnet still looks installed but silently drops every client on the
  current network.

  Only the firewall step needs elevation, and it self-elevates rather than
  requiring the whole script to run as administrator.
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'DtsMcpGateway',
    [switch]$SkipFirewall
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$startScript = Join-Path $PSScriptRoot 'start.ps1'
$stateDirectory = Join-Path $env:LOCALAPPDATA 'DtsMCP'
$environmentFile = Join-Path $stateDirectory 'config\server.env'
$python = Join-Path $repositoryRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $environmentFile)) {
    throw "Missing $environmentFile. Run .\scripts\bootstrap.ps1 first."
}

$port = '8770'
foreach ($line in Get-Content $environmentFile) {
    $parts = $line.Split('=', 2)
    if ($parts.Count -eq 2 -and $parts[0] -eq 'DTS_MCP_BIND_PORT') {
        $port = $parts[1].Trim('"')
    }
}

# --- scheduled task ----------------------------------------------------------
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startScript`"" `
    -WorkingDirectory $repositoryRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description 'DTS Engine MCP gateway' -Force | Out-Null
Write-Host "Registered scheduled task '$TaskName' (at logon, user $env:USERNAME)."

# --- firewall ----------------------------------------------------------------
if ($SkipFirewall) {
    Write-Host 'Skipping firewall configuration as requested.'
}
else {
    $serverIp = (& $python -c @"
import sys
sys.path.insert(0, r'$repositoryRoot\src')
from dts_mcp_server.host_address import resolve_server_ip
print(resolve_server_ip())
"@).Trim()
    $subnet = (& $python -c @"
import sys
sys.path.insert(0, r'$repositoryRoot\src')
from dts_mcp_server.host_address import resolve_ipv4_network
print(resolve_ipv4_network('$serverIp'))
"@).Trim()

    $ruleName = "DTS MCP Gateway ($port)"
    $firewallScript = @"
`$existing = Get-NetFirewallRule -DisplayName '$ruleName' -ErrorAction SilentlyContinue
if (`$existing) {
    `$currentScope = (`$existing | Get-NetFirewallAddressFilter).RemoteAddress
    if (`$currentScope -ne '$subnet') {
        Write-Host "Updating firewall scope from '`$currentScope' to '$subnet'."
        `$existing | Set-NetFirewallRule -RemoteAddress '$subnet'
    } else {
        Write-Host 'Firewall rule already scoped correctly.'
    }
} else {
    New-NetFirewallRule -DisplayName '$ruleName' -Direction Inbound -Action Allow ``
        -Protocol TCP -LocalPort $port -Profile Private -RemoteAddress '$subnet' | Out-Null
    Write-Host 'Created firewall rule.'
}
"@

    $identity = [Security.Principal.WindowsPrincipal]::new(
        [Security.Principal.WindowsIdentity]::GetCurrent()
    )
    $isAdmin = $identity.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )

    if ($isAdmin) {
        Invoke-Expression $firewallScript
    }
    else {
        Write-Host "Elevating to configure the firewall (TCP $port, scoped to $subnet)..."
        $encoded = [Convert]::ToBase64String(
            [Text.Encoding]::Unicode.GetBytes($firewallScript)
        )
        $elevated = Start-Process -FilePath 'powershell.exe' `
            -ArgumentList @('-NoProfile', '-EncodedCommand', $encoded) `
            -Verb RunAs -Wait -PassThru
        if ($elevated.ExitCode -ne 0) {
            Write-Warning "Firewall configuration failed (exit $($elevated.ExitCode)). Configure TCP $port inbound manually, scoped to $subnet."
        }
    }
}

Write-Host ''
Write-Host 'Install complete. Start now with: .\scripts\start.ps1'
Write-Host 'Do not expose this port to the public Internet; it assumes a trusted LAN or VPN.'
