# dts-mcp-server

MCP server and CLI for **DTS Engine 6.1 and 7.0** (飞渡科技 / Freedo), a Windows
GUI tool that produces 3D digital-twin tiles from shapefiles, oblique
photogrammetry, DEM/DOM rasters, BIM, and point clouds.

Engine ships no SDK and no documented CLI. This project drives the 93-flag
command line that Engine's own GUI shell uses internally — recovered by
decompiling that shell, which is a managed .NET assembly. The full contract is in
[`docs/ENGINE_PROTOCOL.md`](docs/ENGINE_PROTOCOL.md).

Three interfaces over one core:

- **Remote MCP gateway** (`dts-mcp-server`) — 14 tools over HTTPS with bearer
  auth, plus a signed data channel for uploading inputs and collecting results.
  This is how a macOS or Linux client drives a Windows-only product. See
  [`docs/REMOTE.md`](docs/REMOTE.md).
- **Local stdio server** (`dts-mcp-server --stdio`) — 8 tools, no TLS and no
  data plane, for a client on the same machine that can pass host paths.
- **CLI** (`cli-anything-dts-engine`) — subcommands plus a REPL, for humans and
  shell scripts.

## Requirements

- **Windows.** Engine is Windows-only.
- **DTS Engine installed and licensed.** A required dependency, not optional:
  this project drives the real `EngineWorker.exe`.
- Python 3.10+.

The flag contract was recovered from **6.1**; **7.0** drives the same command line
and publishes a valid `.3dt`, but its exit codes are less specific, so
`dts_explain_error` resolves fewer of them to a distinct cause. Discovery accepts
any tree holding `EngineWorker.exe`; `core.install.KNOWN_MAJORS` records only the
majors actually exercised, and the tests assert against it rather than a literal.

## Install

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

Engine is discovered automatically at `%APPDATA%\DTS Engine\<version>`. Override
with `DTS_ENGINE_DIR` when it lives elsewhere:

```bash
export DTS_ENGINE_DIR="D:/Users/you/AppData/Roaming/DTS Engine/7.0"
```

## Remote access (macOS → Windows)

On the **Windows host**:

```powershell
Set-ExecutionPolicy Bypass -Scope Process   # client Windows defaults to Restricted
.\scripts\bootstrap.ps1                 # secrets, address, certificates
.\scripts\install_scheduled_task.ps1    # autostart at logon + firewall
.\scripts\start.ps1
.\scripts\status.ps1                    # verify
```

Run these as the **normal user, not elevated** — only the firewall step needs
administrator, and it self-elevates. An elevated shell would leave the gateway
process running with rights it does not need.

Copy `%LOCALAPPDATA%\DtsMCP\certs\ca.crt` to the Mac, and read the bearer token
from `%LOCALAPPDATA%\DtsMCP\config\server.env`. The gateway sends only its leaf
certificate, so a client cannot pull the CA off the wire — it has to arrive out of
band. Verify it did by comparing digests on both ends:

```powershell
# Windows. Hashes the DER encoding, not the file, so a CRLF/LF rewrite in transit
# does not change the answer.
[Security.Cryptography.X509Certificates.X509Certificate2]::new(
    "$env:LOCALAPPDATA\DtsMCP\certs\ca.crt").GetCertHashString('SHA256')
```

```bash
# Client. Same value, lower case.
openssl x509 -in ca.crt -outform der | shasum -a 256
```

On the **Mac**:

```bash
./clients/macos/configure-macos.sh install --host <windows-ip> --ca ./ca.crt
./clients/macos/verify-connection.sh --host <windows-ip>
```

That stores the token in the Keychain, publishes it to GUI apps via a
LaunchAgent, trusts the CA for SSL in the login keychain, and writes an
`mcp.json` naming the env var rather than embedding the secret:

```json
{
  "mcpServers": {
    "dts": {
      "type": "http",
      "url": "https://192.168.50.170:8770/mcp",
      "bearer_token_env_var": "DTS_MCP_TOKEN"
    }
  }
}
```

**That `mcp.json` is a reference shape, not a universal one.** `bearer_token_env_var`
is not a field every client understands, so check what yours expects. Claude Code,
for one, wants the header spelled out, and will not find the CA in the login
keychain because Node keeps its own trust store:

```bash
claude mcp add --transport http dts https://<windows-ip>:8770/mcp \
  --header "Authorization: Bearer $(security find-generic-password -a "$(id -un)" -s dts-mcp -w)"
export NODE_EXTRA_CA_CERTS="$HOME/Library/Application Support/dts-mcp/ca.crt"
```

Without `NODE_EXTRA_CA_CERTS` the handshake fails with an error that names neither
the certificate nor the CA. The same applies on Linux, where
`configure-macos.sh` does not run at all: install the CA wherever the client's TLS
stack looks, and pass the token however that client accepts it.

**The Windows host must stay logged in.** Autostart is a logon scheduled task,
not a Windows service, because Engine's licence check and GUI subsystem need an
interactive session — session 0 will not do.

Read [`docs/REMOTE.md`](docs/REMOTE.md) before using the data plane; it covers
uploads, artifact references, retention, and the security boundary.

### Deployment traps

Distinct from the Engine traps below: these cost a debugging session, not a failed
publish.

- **The firewall rule is Private-profile only.** If Windows has classified the
  network as Public — the default for many Wi-Fi connections — the rule installs,
  looks correct in `Get-NetFirewallRule`, and silently drops every client. Check
  with `Get-NetConnectionProfile` and reclassify:
  `Set-NetConnectionProfile -InterfaceAlias <name> -NetworkCategory Private`.
- **Scripts will not run under the default execution policy.** Client editions of
  Windows ship `Restricted`, which refuses every `.ps1` with a `SecurityError`
  that says nothing about policy scope. `Set-ExecutionPolicy Bypass -Scope Process`
  fixes it for one shell without changing machine state.
- **Do not run `bootstrap.ps1` from inside a sandboxed or packaged shell.** Writes
  to `%LOCALAPPDATA%` get redirected into that application's private container, so
  a later `start.ps1` from an ordinary shell reports the configuration missing
  while the certificates sit somewhere else entirely.

### Tools

The two servers expose **overlapping but different** sets — stdio is not a subset
of remote. Five tools are common. The artifact and job families are remote-only,
because only the gateway has a data plane and a queue; three are stdio-only,
because that mode is free to accept and return host paths.

| | Remote (14) | stdio (8) |
|---|---|---|
| `dts_ping`, `dts_list_pipelines`, `dts_explain_error`, `dts_publish`, `dts_publish_osgb` | ✓ | ✓ |
| `dts_create_upload`, `dts_complete_upload`, `dts_artifact_status`, `dts_list_artifacts`, `dts_delete_artifact`, `dts_download_output` | ✓ | — |
| `dts_get_job`, `dts_list_jobs`, `dts_cancel_job` | ✓ | — |
| `dts_validate`, `dts_create_job`, `dts_job_types` | — | ✓ |

Remote:

| Tool | Purpose |
|---|---|
| `dts_ping` | Verify the install. Call this first. |
| `dts_list_pipelines` | The 9 pipelines, their flags, `path_flags`, verified status. |
| `dts_explain_error` | Resolve an exit code through Engine's shipped table. |
| `dts_create_upload` | Begin an upload; returns a signed PUT URL. |
| `dts_complete_upload` | Verify the digest and extract archives. |
| `dts_artifact_status` | State and `committed_size`, for resuming. |
| `dts_list_artifacts` / `dts_delete_artifact` | Manage stored inputs. |
| `dts_publish` | Queue a publish job. |
| `dts_publish_osgb` | Queue OSGB, running both required stages. |
| `dts_get_job` / `dts_list_jobs` / `dts_cancel_job` | Track work. |
| `dts_download_output` | Signed GET URL for the result archive. |

### Local stdio mode

For a client on the Windows host itself, skip TLS and the data plane entirely:

```yaml
# data_agent/mcp_servers.yaml
dts:
  transport: stdio
  command: D:/adk/standalone/dts-mcp-server/.venv/Scripts/python.exe
  args: ["-m", "dts_mcp_server", "--stdio"]
```

Eight tools: `dts_ping`, `dts_list_pipelines`, `dts_explain_error`, `dts_publish`,
`dts_publish_osgb`, plus three with no remote equivalent — `dts_validate`
(pre-flight a flag set without running Engine), `dts_create_job` (write an `.aconf`
job file, the input for Engine's `-jsonPath` mode), and `dts_job_types` (the
`.aconf` data types, with the fields transcribed for each; a null `fields` means
that schema was never transcribed, so it is not validated rather than guessed at).
Path flags take plain host paths here, not artifact references.

## CLI usage

```bash
cli-anything-dts-engine info
cli-anything-dts-engine --json pipelines
cli-anything-dts-engine --json errors --code 201

cli-anything-dts-engine run road --outpath ./out \
  --set roadShp=roads.shp --set domPath=dom.tif --set demPath=dem.tif
```

Run with no subcommand for a REPL. Agent-facing docs:
[`src/cli_anything/dts_engine/skills/SKILL.md`](src/cli_anything/dts_engine/skills/SKILL.md).

## Traps

Learned by probing a real install; each cost a failed run to discover.

- **`road` requires `domPath`** even though the shell's base template omits it.
  Without it: exit 201.
- **Input CRS must be projected.** Geographic coordinates fail with 203/209/215.
- **`shp` is not a general vector converter.** It builds vegetation/material
  resources and wants tree attributes; a polygon shapefile fails with 302. Use
  `road` or `vtpk` for general vector work.
- **OSGB needs two processes.** Tiling then LOD pyramid. Use `dts_publish_osgb`
  / `publish-osgb`, not a bare `osgbLod` run.
- **Never drive `EngineMaster.exe`.** It returns -1 (0xFFFFFFFF) instead of the
  child's error code. This project always uses `EngineWorker.exe`.
- **Engine is not a GDAL wrapper.** It bundles GDAL 3.0.5 for format I/O and
  reprojection only; LOD generation, mesh simplification, texture atlasing, and
  3DT tiling have no GDAL equivalent.

## Layout

```
src/dts_mcp_server/
  app.py                   MCP at /mcp + artifact routes on one port
  auth.py                  bearer verifier, artifact URL signer
  config.py                settings; secrets carry a minimum length
  tls_bootstrap.py         local CA + 825-day IP-SAN leaf
  host_address.py          one address for SAN, base URL and firewall scope
  artifacts.py             resumable upload, signed transfer, retention
  workspace.py             per-job sandbox, safe archive extraction
  jobs.py                  queue, cancellation, host-path scrubbing
  engine.py                artifact references -> Engine flags
  mcp_tools.py             the 14 remote tools
  stdio_server.py          local-only mode (no TLS, no data plane)
scripts/                   Windows deployment (bootstrap, start, firewall)
clients/macos/             configure-macos.sh, verify-connection.sh
src/cli_anything/dts_engine/
  core/pipelines.py        9 pipelines, each citing its source line in the shell
  core/errors.py           parses Engine's shipped ~90-code error table
  core/job.py              .aconf job files, schema from Config*.cs
  core/publish.py          execution, incl. OSGB's two-stage sequence
  utils/engine_backend.py  invokes the real EngineWorker.exe
  utils/udp_log.py         Engine's UDP progress channel
docs/ENGINE_PROTOCOL.md    the reverse-engineered contract
docs/TEST.md               test plan, results, and coverage gaps
```

## Tests

```bash
export DTS_ENGINE_DIR="…/DTS Engine/7.0"
.venv/Scripts/python.exe -m pip install -e ".[dev,test-fixtures]"
.venv/Scripts/python.exe -m pytest tests/ -v
```

153 tests. Unit and gateway tests are pure and run anywhere; the E2E tier invokes
the real Engine and **fails rather than skips** when it is absent — a harness that
cannot drive the software is not working. Security tests encode attacker intent
(path traversal, zip bombs, signed-URL replay, host-path leakage) rather than
happy paths. One symlink test skips without the privilege to create one.

`tests/remote_smoke.py` is a separate end-to-end check against a running
listener, since TLS is terminated by uvicorn rather than by the app:

```bash
python tests/remote_smoke.py --base https://<host>:8770 --ca <ca.crt>
```

See [`docs/TEST.md`](docs/TEST.md) for the plan, results, and an honest list of
coverage gaps.

## Status

`road` is verified end-to-end and publishes a real `.3dt` tile. The other eight
pipelines have flag names transcribed from the decompiled shell but unconfirmed
input requirements; they report `verified: false`, and `dts_list_pipelines` says
so. Verifying them needs input data (OSGB datasets, `.max` scenes, `.vtpk`
packages, DTM job descriptions) not available on the development machine.

## License

MIT.
