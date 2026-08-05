# dts-mcp-server

MCP server and CLI for **DTS Engine 6.1** (飞渡科技 / Freedo), a Windows GUI tool
that produces 3D digital-twin tiles from shapefiles, oblique photogrammetry,
DEM/DOM rasters, BIM, and point clouds.

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

## Install

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

Engine is discovered automatically at `%APPDATA%\DTS Engine\<version>`. Override
with `DTS_ENGINE_DIR` when it lives elsewhere:

```bash
export DTS_ENGINE_DIR="D:/Users/you/AppData/Roaming/DTS Engine/6.1"
```

## Remote access (macOS → Windows)

On the **Windows host**:

```powershell
.\scripts\bootstrap.ps1                 # secrets, address, certificates
.\scripts\install_scheduled_task.ps1    # autostart at logon + firewall
.\scripts\start.ps1
.\scripts\status.ps1                    # verify
```

Copy `%LOCALAPPDATA%\DtsMCP\certs\ca.crt` to the Mac, and read the bearer token
from `%LOCALAPPDATA%\DtsMCP\config\server.env`.

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

**The Windows host must stay logged in.** Autostart is a logon scheduled task,
not a Windows service, because Engine's licence check and GUI subsystem need an
interactive session — session 0 will not do.

Read [`docs/REMOTE.md`](docs/REMOTE.md) before using the data plane; it covers
uploads, artifact references, retention, and the security boundary.

### Tools

| Tool | Purpose |
|---|---|
| `dts_ping` | Verify the install. Call this first. |
| `dts_list_pipelines` | The 9 pipelines, their flags, `path_flags`, verified status. |
| `dts_explain_error` | Resolve an exit code through Engine's shipped table. |
| `dts_validate` | Pre-flight a flag set without running Engine. |
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
export DTS_ENGINE_DIR="…/DTS Engine/6.1"
.venv/Scripts/python.exe -m pip install -e ".[dev,test-fixtures]"
.venv/Scripts/python.exe -m pytest tests/ -v
```

152 tests. Unit and gateway tests are pure and run anywhere; the E2E tier invokes
the real Engine and **fails rather than skips** when it is absent — a harness that
cannot drive the software is not working. Security tests encode attacker intent
(path traversal, zip bombs, signed-URL replay, host-path leakage) rather than
happy paths.

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
