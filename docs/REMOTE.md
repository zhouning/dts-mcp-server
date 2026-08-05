# Remote access from macOS

How a Mac drives a Windows-only DTS Engine install, and how data gets in and out.

## The shape of it

**MCP is the control plane only. Bulk data moves over a separate HMAC-signed
HTTPS channel on the same port.** File bytes never travel through JSON-RPC.

```
   macOS client                        Windows host (Engine installed)
   ────────────                        ──────────────────────────────
   MCP tools/call  ──── HTTPS ──────▶  /mcp        (bearer token)
   curl PUT        ──── HTTPS ──────▶  /artifacts/{id}/content?op=upload&…
   curl GET        ◀─── HTTPS ───────  /artifacts/{id}/content?op=download&…
```

One port (8770), one TLS listener, one firewall rule, two protocols. In
`app.py` this is just `mcp.streamable_http_app()` followed by
`app.routes.extend(artifact_routes())`.

Two independent secrets, and the separation matters:

| Secret | Authorises | Never used for |
|---|---|---|
| `DTS_MCP_BEARER_TOKEN` | MCP control calls | artifact URLs |
| `DTS_MCP_ARTIFACT_SIGNING_KEY` | one artifact, one operation, 10 minutes | MCP calls |

**Do not send the bearer token to a signed URL.** A leaked transfer URL should
grant one artifact for one TTL window, not the whole API. The operation is bound
into the MAC, so an upload URL cannot be replayed as a download.

## Data in

Engine inputs are rarely a single file: a shapefile is 4+ sidecars, an OSGB
dataset is a directory tree. So upload a `.zip` — it is extracted on completion
and its members become addressable by relative path.

```bash
ZIP=roads.zip
SIZE=$(stat -f%z "$ZIP")            # macOS stat
SHA=$(shasum -a 256 "$ZIP" | cut -d' ' -f1)
```

1. `dts_create_upload {logical_name, expected_size, expected_sha256}`
   → returns `upload_url`.
2. `curl --cacert ca.crt -X PUT --data-binary @roads.zip -H 'X-Upload-Offset: 0' "$UPLOAD_URL"`
3. `dts_complete_upload {artifact_id}` — verifies the digest, extracts the zip.

Size and digest are declared **before** the first byte, so a truncated transfer
fails loudly instead of producing a corrupt input. Bytes land in `{id}.part`, so
an incomplete upload can never be mistaken for a ready artifact.

**Resuming:** call `dts_artifact_status` for `committed_size` and PUT again with
that offset. The offset is not part of the signature, so the same URL is reused.
A wrong offset is rejected with 409 rather than silently corrupting the file.

## Publishing

Path-valued flags take an **artifact reference**, never a string:

```json
{
  "pipeline": "road",
  "flags": {
    "roadShp": {"artifact": "<id>", "path": "roads.shp"},
    "domPath": {"artifact": "<id>", "path": "dom.tif"},
    "demPath": {"artifact": "<id>", "path": "dem.tif"},
    "roadRGB": "255,0,0"
  }
}
```

A bare string in a path flag is refused. Accepting one would let a remote client
name `C:\Windows\...` and have Engine read it. `dts_list_pipelines` reports
`path_flags` per pipeline so a client knows which are which.

Publishing is asynchronous — `dts_publish` returns a job immediately. Poll
`dts_get_job` until `status` is terminal (`succeeded`, `failed`, `timed_out`,
`cancelled`, `interrupted`). Execution is single-lane: Engine is a licensed
desktop product and concurrent publishes contend for the same licence.

`interrupted` means the gateway restarted mid-job. The queue is in memory, so the
job cannot resume and the gateway genuinely does not know whether Engine
finished — re-submit if the output is missing.

## Data out

`dts_download_output {job_id}` returns a signed GET URL. The result is always a
zip, because Engine's output is a set rather than a file: a `.3dt` tile is
useless without the `DataInfor.txt` key beside it, and OSGB produces a whole
pyramid.

```bash
curl --cacert ca.crt -o result.zip "$DOWNLOAD_URL"
```

## What the client never sees

Every response is scrubbed of host paths:

- `storage_path` and `content_root` are stripped from artifact records.
- Job `request`, `result`, `error_message` and `events` are rewritten: paths
  inside that job's own sandbox become relative, anything else becomes
  `[redacted-path]`.

That last one matters for Engine specifically. Its UDP log emits prose
("发布失败，RetCode:201"), so host paths appear mid-sentence rather than as
discrete fields and have to be rewritten in place.

## Retention

Ready artifacts live 7 days; incomplete uploads 24 hours (abandoned `.part`
files are the disk-exhaustion vector). Cleanup runs at startup and hourly, and
skips anything a live job still references. Deletion is hard on disk and soft in
the database, so an audit trail survives.

## Security boundary

The gateway assumes a **trusted LAN or an authenticated VPN**. Concretely:

- The firewall rule is scoped to the local /24 on the Private profile only.
- There is **no rate limiting and no lockout**. The compensating control is a
  384-bit token plus that network scope.
- TLS is fail-closed: the gateway refuses to start without a certificate.
- DNS-rebinding protection is on, with `allowed_hosts` pinned to the configured
  address. Without it a malicious web page could make a browser on the client's
  network POST to the gateway.

**Do not expose port 8770 to the public Internet.** If you need to, add per-IP
throttling on `/mcp` and on signature failures first.

## Certificate and address coupling

The certificate SAN, the published base URL and the firewall scope all derive
from one value (`host_address.resolve_server_ip()`), because a certificate whose
SAN no longer matches the dialled address fails at connect time with an error
that looks nothing like its cause.

Two constants in `tls_bootstrap.py` are load-bearing:

- The leaf is valid **825 days** — the maximum Apple platforms accept. Longer is
  rejected outright by macOS.
- The SAN is an `IPAddress`, not a DNS name, because clients dial a bare IP.

**DHCP drift is a hard break.** If the host's address changes, re-run
`bootstrap.ps1` (it detects the change, rewrites only that line, and reissues
certificates) then `configure-macos.sh --refresh-ca` on each client.
`status.ps1` warns when the configured and detected addresses disagree.

## Why a logon task rather than a Windows service

DTS Engine is a licensed desktop product whose licence check and GUI subsystem
expect an interactive session. Session 0 does not provide one, so the gateway
runs as a **scheduled task at logon** and the host must stay logged in. If it
locks the screen that is fine; if it logs out, the gateway stops.

## Known client-side gotcha

Windows `curl` uses the schannel backend, which ignores `--cacert` for a private
root — it wants the certificate in the system trust store. This does **not**
affect macOS, where curl uses LibreSSL and `--cacert` works. When testing from
the Windows host itself, use `tests/remote_smoke.py` (Python/OpenSSL) rather
than curl.
