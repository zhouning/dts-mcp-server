"""End-to-end remote smoke test against a running gateway.

Exercises the flow a macOS client follows: verify TLS against the CA only, prove
auth is enforced, upload an input over the signed data-plane URL, publish, poll,
then download the result. Uses Python's ssl (OpenSSL) rather than Windows curl,
whose schannel backend will not honour --cacert for a private root.

Usage:
    python tests/remote_smoke.py --base https://HOST:8770 --ca path/to/ca.crt
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

TOKEN = os.environ["DTS_MCP_BEARER_TOKEN"]

#: Kept as a literal rather than imported from cli_anything.dts_engine.core.install,
#: because this script is meant to run from a client machine that has only Python
#: and the CA certificate — not the server package. Keep in sync with that module.
KNOWN_MAJORS = ("6", "7")


def make_opener(ca: Path) -> urllib.request.OpenerDirector:
    context = ssl.create_default_context(cafile=str(ca))
    # ProxyHandler({}) disables proxy use entirely. A system HTTPS proxy will
    # otherwise intercept a LAN address and hang the TLS handshake, which looks
    # exactly like a broken certificate.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
    )


class Client:
    def __init__(self, base: str, ca: Path) -> None:
        self.base = base.rstrip("/")
        self.opener = make_opener(ca)
        self._id = 0

    def raw(self, method: str, url: str, data=None, headers=None):
        request = urllib.request.Request(
            url, data=data, method=method, headers=headers or {}
        )
        with self.opener.open(request, timeout=120) as response:
            return response.status, response.read()

    def call(self, tool: str, arguments: dict) -> dict:
        """Invoke an MCP tool and return the parsed structured payload."""
        self._id += 1
        body = json.dumps({
            "jsonrpc": "2.0", "id": self._id, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }).encode()
        status, raw = self.raw(
            "POST", f"{self.base}/mcp", body,
            {
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        message = json.loads(raw)
        if "error" in message:
            raise AssertionError(f"{tool} -> {message['error']}")
        content = message["result"]["content"]
        return json.loads(content[0]["text"])


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--ca", required=True, type=Path)
    parser.add_argument("--fixtures", type=Path, default=Path("_smoke"))
    args = parser.parse_args()

    client = Client(args.base, args.ca)

    print("\n1. TLS and liveness")
    status, raw = client.raw("GET", f"{args.base}/healthz")
    check("healthz over TLS verified against the CA only", status == 200,
          raw.decode())

    print("\n2. Auth enforcement")
    for label, header in (
        ("no token rejected", {}),
        ("wrong token rejected", {"Authorization": "Bearer " + "x" * 48}),
    ):
        try:
            client.raw(
                "POST", f"{args.base}/mcp",
                b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
                {**header, "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
            )
        except urllib.error.HTTPError as error:
            check(label, error.code in (401, 403), f"HTTP {error.code}")
        else:
            check(label, False, "request succeeded without a valid token")

    print("\n3. Engine reachable")
    ping = client.call("dts_ping", {})
    check("dts_ping", ping.get("ok") is True, str(ping)[:160])
    check("Engine version reported", ping["version"].split(".")[0] in KNOWN_MAJORS,
          ping["version"])

    print("\n4. Upload input over the data plane")
    zip_path = args.fixtures / "input.zip"
    payload = zip_path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    created = client.call("dts_create_upload", {
        "logical_name": "input.zip",
        "expected_size": len(payload),
        "expected_sha256": digest,
        "media_type": "application/zip",
    })
    check("create_upload returned a signed URL",
          created.get("ok") and "upload_url" in created)
    check("signed URL carries op and signature",
          "op=upload" in created["upload_url"]
          and "signature=" in created["upload_url"])

    # Note: no Authorization header — signed URLs must not need the bearer.
    status, _ = client.raw(
        "PUT", created["upload_url"], payload, {"X-Upload-Offset": "0"}
    )
    check("PUT accepted without the bearer token", status in (200, 204),
          f"HTTP {status}")

    artifact_id = created["artifact"]["id"]
    completed = client.call("dts_complete_upload", {"artifact_id": artifact_id})
    check("complete_upload verified the digest", completed.get("ok") is True,
          str(completed)[:200])
    check("no host path leaked in the artifact record",
          "storage_path" not in completed["artifact"]
          and "content_root" not in completed["artifact"])

    print("\n5. Replay protection")
    try:
        client.raw("GET", created["upload_url"].replace("op=upload", "op=download"))
    except urllib.error.HTTPError as error:
        check("upload URL cannot be replayed as download", error.code == 403,
              f"HTTP {error.code}")
    else:
        check("upload URL cannot be replayed as download", False)

    print("\n6. Publish")
    job = client.call("dts_publish", {
        "pipeline": "road",
        "flags": {
            "roadShp": {"artifact": artifact_id, "path": "roads.shp"},
            "domPath": {"artifact": artifact_id, "path": "dom.tif"},
            "demPath": {"artifact": artifact_id, "path": "dem.tif"},
            "roadRGB": "255,0,0",
            "roadSize": 5,
        },
    })
    check("publish accepted and queued", job.get("ok") is True, str(job)[:300])
    job_id = job["job"]["id"]

    print("\n7. Poll to completion")
    deadline = time.time() + 900
    state = None
    while time.time() < deadline:
        current = client.call("dts_get_job", {"job_id": job_id})
        state = current["job"]["status"]
        if state in {"succeeded", "failed", "timed_out", "cancelled",
                     "interrupted"}:
            break
        time.sleep(3)
    check(f"job reached a terminal state ({state})", state == "succeeded",
          json.dumps(current["job"].get("error_message") or "", ensure_ascii=False))

    events = current["job"].get("events") or []
    leaked = [e for e in events if "zn198" in json.dumps(e, ensure_ascii=False)]
    check("no host path leaked in job events", not leaked, str(leaked)[:200])

    print("\n8. Download the result")
    download = client.call("dts_download_output", {"job_id": job_id})
    check("download_output returned a signed URL",
          download.get("ok") and "download_url" in download)
    status, blob = client.raw("GET", download["download_url"])
    check("GET returned bytes without the bearer token",
          status == 200 and len(blob) > 0, f"{len(blob):,} bytes")

    out = args.fixtures / "result.zip"
    out.write_bytes(blob)
    with zipfile.ZipFile(out) as archive:
        names = archive.namelist()
    check("result archive contains a .3dt tile",
          any(n.lower().endswith(".3dt") for n in names), str(names))
    check("result archive contains DataInfor.txt",
          any(n == "DataInfor.txt" for n in names), str(names))

    print(f"\nAll checks passed. Result: {out} ({len(blob):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
