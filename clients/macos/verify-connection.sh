#!/usr/bin/env bash
# Diagnose a macOS client's connection to the DTS MCP gateway.
#
# Checks seven things in order, so the first failure tells you which layer is
# broken. Never prints the token and never dumps the environment.
#
#   ./verify-connection.sh --host 192.168.50.170
set -euo pipefail

KEYCHAIN_SERVICE="dts-mcp"
SUPPORT_DIR="$HOME/Library/Application Support/dts-mcp"
CA_CERT="$SUPPORT_DIR/ca.crt"
LAUNCH_LABEL="com.dts.mcp.token"

HOST=""
PORT="8770"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) HOST="${2:-}"; shift 2 ;;
        --port) PORT="${2:-}"; shift 2 ;;
        -h|--help)
            echo "Usage: verify-connection.sh --host <ip> [--port 8770]"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 1 ;;
    esac
done

[[ -n "$HOST" ]] || { echo "--host is required." >&2; exit 1; }

failures=0
step() { printf '%-46s' "$1"; }
ok()   { echo "OK${1:+ — $1}"; }
bad()  { echo "FAIL${1:+ — $1}"; failures=$((failures + 1)); }

echo "Verifying DTS MCP gateway at https://${HOST}:${PORT}"
echo

# 1 -------------------------------------------------------------------------
step "1. token present in Keychain"
account="$(id -un)"
if token="$(security find-generic-password -a "$account" -s "$KEYCHAIN_SERVICE" -w 2>/dev/null)"; then
    if [[ ${#token} -ge 32 ]]; then ok "${#token} chars"; else bad "only ${#token} chars"; fi
else
    bad "not found; run configure-macos.sh"
    token=""
fi

# 2 -------------------------------------------------------------------------
step "2. token published to GUI apps"
if launchctl getenv DTS_MCP_TOKEN >/dev/null 2>&1 \
   && [[ -n "$(launchctl getenv DTS_MCP_TOKEN)" ]]; then
    ok
else
    bad "run: launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/${LAUNCH_LABEL}.plist"
fi

# 3 -------------------------------------------------------------------------
step "3. CA present and trusted"
if [[ -f "$CA_CERT" ]]; then
    if security verify-cert -c "$CA_CERT" -p ssl >/dev/null 2>&1; then
        ok
    else
        bad "present but not trusted; re-run with --refresh-ca"
    fi
else
    bad "missing $CA_CERT"
fi

# 4 -------------------------------------------------------------------------
step "4. TCP reachable"
if python3 - "$HOST" "$PORT" <<'PY' 2>/dev/null
import socket, sys
socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5).close()
PY
then ok; else bad "no route or firewall blocks this client"; fi

# 5 -------------------------------------------------------------------------
step "5. healthz over verified TLS"
if health="$(curl --fail --silent --cacert "$CA_CERT" \
        "https://${HOST}:${PORT}/healthz" 2>/dev/null)"; then
    ok "$health"
else
    bad "TLS verification or liveness failed"
fi

# 6 -------------------------------------------------------------------------
step "6. authenticated MCP initialize"
if [[ -n "$token" ]]; then
    # The token goes through a curl config file on a process-substitution FD, so
    # it never appears in the process argument list.
    body='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"verify","version":"0"}}}'
    if response="$(curl --fail --silent --cacert "$CA_CERT" \
            --config <(printf 'header = "Authorization: Bearer %s"\n' "$token") \
            -H 'Content-Type: application/json' \
            -H 'Accept: application/json, text/event-stream' \
            -X POST --data "$body" \
            "https://${HOST}:${PORT}/mcp" 2>/dev/null)"; then
        if [[ "$response" == *'"serverInfo"'* ]]; then ok "handshake accepted"
        else bad "unexpected response"; fi
    else
        bad "rejected; token may be stale — re-run with --rotate-token"
    fi
else
    bad "skipped, no token"
fi

# 7 -------------------------------------------------------------------------
step "7. tools advertised"
if [[ -n "$token" ]]; then
    body='{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
    if tools="$(curl --fail --silent --cacert "$CA_CERT" \
            --config <(printf 'header = "Authorization: Bearer %s"\n' "$token") \
            -H 'Content-Type: application/json' \
            -H 'Accept: application/json, text/event-stream' \
            -X POST --data "$body" \
            "https://${HOST}:${PORT}/mcp" 2>/dev/null)"; then
        count="$(printf '%s' "$tools" | grep -o '"name"' | wc -l | tr -d ' ')"
        if [[ "$count" -gt 0 ]]; then ok "$count tools"; else bad "none listed"; fi
    else
        bad "tools/list failed"
    fi
else
    bad "skipped, no token"
fi

unset token

echo
if [[ $failures -eq 0 ]]; then
    echo "All checks passed."
else
    echo "$failures check(s) failed." >&2
    exit 1
fi
