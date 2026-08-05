#!/usr/bin/env bash
# Configure a macOS client to reach the DTS MCP gateway.
#
# Stores the bearer token in the login Keychain, arranges for GUI-launched apps
# to see it, and imports the gateway's CA so TLS validates without --insecure.
#
#   ./configure-macos.sh install --host 192.168.50.170 --ca ./ca.crt
#   ./configure-macos.sh --rotate-token
#   ./configure-macos.sh --refresh-ca --ca ./new-ca.crt
set -euo pipefail

KEYCHAIN_SERVICE="dts-mcp"
ENV_VAR="DTS_MCP_TOKEN"
LAUNCH_LABEL="com.dts.mcp.token"
SUPPORT_DIR="$HOME/Library/Application Support/dts-mcp"
CA_DEST="$SUPPORT_DIR/ca.crt"
LOADER="$SUPPORT_DIR/load-token.sh"
PLIST="$HOME/Library/LaunchAgents/${LAUNCH_LABEL}.plist"

MODE="install"
HOST=""
PORT="8770"
CA_SOURCE=""

usage() {
    cat <<'USAGE'
Usage:
  configure-macos.sh install --host <ip> --ca <ca.crt> [--port 8770]
  configure-macos.sh --rotate-token
  configure-macos.sh --refresh-ca --ca <ca.crt>
USAGE
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        install)         MODE="install"; shift ;;
        --rotate-token)  MODE="rotate-token"; shift ;;
        --refresh-ca)    MODE="refresh-ca"; shift ;;
        --host)          HOST="${2:-}"; shift 2 ;;
        --port)          PORT="${2:-}"; shift 2 ;;
        --ca)            CA_SOURCE="${2:-}"; shift 2 ;;
        -h|--help)       usage ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done

[[ "$(uname -s)" == "Darwin" ]] || { echo "This script is for macOS." >&2; exit 1; }
for command in security launchctl curl python3 install; do
    command -v "$command" >/dev/null || { echo "missing required command: $command" >&2; exit 1; }
done

mkdir -p "$SUPPORT_DIR" "$(dirname "$PLIST")"

store_token() {
    local account token
    account="$(id -un)"
    # Read without echo and without word-splitting or backslash mangling. The
    # token is never passed as a command argument, so it cannot appear in `ps`
    # output or shell history.
    printf 'Paste the bearer token (input hidden): '
    IFS= read -r -s token
    printf '\n'
    if [[ ${#token} -lt 32 ]]; then
        echo "Token looks too short (${#token} chars); expected 32+." >&2
        exit 1
    fi
    security add-generic-password -U \
        -a "$account" -s "$KEYCHAIN_SERVICE" -w "$token" \
        -T /usr/bin/security >/dev/null
    unset token
    echo "Token stored in the login Keychain (service: $KEYCHAIN_SERVICE)."
}

install_token_loader() {
    umask 077
    # GUI-launched applications do not inherit a shell environment, so a
    # LaunchAgent publishes the token with `launchctl setenv` at login. Without
    # this, a GUI MCP client sees no token even though the shell does.
    cat >"$LOADER" <<'SH'
#!/bin/bash
set -euo pipefail
account="$(id -un)"
token="$(security find-generic-password -a "$account" -s dts-mcp -w 2>/dev/null || true)"
if [[ -n "$token" ]]; then
    launchctl setenv DTS_MCP_TOKEN "$token"
fi
unset token
SH
    chmod 700 "$LOADER"

    cat >"$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${LAUNCH_LABEL}</string>
    <key>ProgramArguments</key>
    <array><string>${LOADER}</string></array>
    <key>RunAtLoad</key><true/>
</dict>
</plist>
PLIST_EOF
    chmod 600 "$PLIST"

    launchctl bootout "gui/$(id -u)/${LAUNCH_LABEL}" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    "$LOADER"
    echo "Token loader installed; ${ENV_VAR} is published at login."
}

import_ca() {
    [[ -n "$CA_SOURCE" ]] || { echo "--ca is required." >&2; exit 1; }
    [[ -f "$CA_SOURCE" ]] || { echo "CA file not found: $CA_SOURCE" >&2; exit 1; }
    install -m 644 "$CA_SOURCE" "$CA_DEST"
    # Trust as a root for SSL only, in the login keychain — not the System
    # keychain. This grants the gateway's private CA the narrowest useful trust
    # and needs no sudo.
    security add-trusted-cert -r trustRoot -p ssl \
        -k "$HOME/Library/Keychains/login.keychain-db" "$CA_DEST"
    echo "CA imported and trusted for SSL: $CA_DEST"
}

write_mcp_config() {
    [[ -n "$HOST" ]] || { echo "--host is required for install." >&2; exit 1; }
    local config="$SUPPORT_DIR/mcp.json"
    cat >"$config" <<JSON
{
  "mcpServers": {
    "dts": {
      "type": "http",
      "url": "https://${HOST}:${PORT}/mcp",
      "bearer_token_env_var": "${ENV_VAR}"
    }
  }
}
JSON
    echo "Wrote client config: $config"
    echo "  (names the env var rather than embedding the token, so it is safe to commit)"
}

case "$MODE" in
    install)
        store_token
        install_token_loader
        import_ca
        write_mcp_config
        ;;
    rotate-token)
        store_token
        install_token_loader
        ;;
    refresh-ca)
        import_ca
        ;;
esac

if [[ -n "$HOST" ]]; then
    echo ''
    echo 'Verifying TLS trust end to end...'
    if curl --fail --silent --cacert "$CA_DEST" \
            "https://${HOST}:${PORT}/healthz" >/dev/null; then
        echo "OK: https://${HOST}:${PORT}/healthz reachable and trusted."
    else
        echo "WARNING: could not reach the gateway. Check that it is running" >&2
        echo "         and that the firewall allows this client." >&2
    fi
fi

echo ''
echo 'Done. Restart your MCP client so it picks up the environment variable.'
echo "Run ./verify-connection.sh --host ${HOST:-<ip>} to diagnose."
