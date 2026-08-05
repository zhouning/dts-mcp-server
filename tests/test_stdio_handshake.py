"""Verify the server works as a real stdio subprocess.

Everything else tests the tool functions in-process. This test launches
``python -m dts_mcp_server`` the way an MCP client does and drives a genuine
JSON-RPC handshake over its stdin/stdout, so a packaging or entry-point
regression cannot pass unnoticed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

PROTOCOL_VERSION = "2025-06-18"
TIMEOUT = 60


def _frame(payload: dict) -> str:
    return json.dumps(payload) + "\n"


def _launch() -> subprocess.Popen:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "dts_mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
        bufsize=1,
    )


def _converse(requests: list[dict]) -> list[dict]:
    """Send requests over stdio one at a time, reading each response.

    Writing everything at once then closing stdin does not work: the server
    reaches EOF and shuts down while later requests are still queued. Each
    request that carries an ``id`` is written, then its reply is read before the
    next one goes out.
    """
    proc = _launch()
    messages: list[dict] = []
    try:
        for req in requests:
            proc.stdin.write(_frame(req))
            proc.stdin.flush()
            if "id" not in req:
                continue  # notification: no reply expected
            while True:
                line = proc.stdout.readline()
                if not line:
                    err = proc.stderr.read()
                    raise AssertionError(
                        f"server closed stdout awaiting id={req['id']}.\n"
                        f"stderr={err[-2000:]!r}"
                    )
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
                break
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
    assert messages, "no JSON-RPC responses"
    return messages


def test_initialize_handshake():
    """The server must complete initialize and report its name."""
    responses = _converse([
        {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
    ])
    init = next(m for m in responses if m.get("id") == 1)
    assert "error" not in init, init
    assert init["result"]["serverInfo"]["name"] == "dts-engine"


def test_tools_list_over_stdio():
    """tools/list must return the full tool set through a real transport."""
    responses = _converse([
        {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ])
    listing = next(m for m in responses if m.get("id") == 2)
    assert "error" not in listing, listing
    names = {t["name"] for t in listing["result"]["tools"]}
    assert "dts_ping" in names
    assert "dts_publish" in names
    assert len(names) == 8


def test_tool_call_over_stdio():
    """A read-only tool call must round-trip through the transport."""
    responses = _converse([
        {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "dts_explain_error", "arguments": {"code": 201}},
        },
    ])
    call = next(m for m in responses if m.get("id") == 2)
    assert "error" not in call, call
    payload = json.dumps(call["result"])
    assert "terrain" in payload
    assert "DOM path is empty" in payload
