# dts-mcp-server

MCP server and CLI for **DTS Engine 6.1** (飞渡科技 / Freedo), a Windows GUI tool
that produces 3D digital-twin tiles from shapefiles, oblique photogrammetry,
DEM/DOM rasters, BIM, and point clouds.

Engine ships no SDK and no documented CLI. This project drives the 93-flag
command line that Engine's own GUI shell uses internally — recovered by
decompiling that shell, which is a managed .NET assembly. The full contract is in
[`docs/ENGINE_PROTOCOL.md`](docs/ENGINE_PROTOCOL.md).

Two interfaces over one core:

- **MCP server** (`dts-mcp-server`) — 8 tools over stdio, for agents.
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

## MCP usage

Register as a stdio server. For the GIS Data Agent (`data_agent/mcp_servers.yaml`):

```yaml
dts:
  transport: stdio
  command: D:/adk/standalone/dts-mcp-server/.venv/Scripts/python.exe
  args: ["-m", "dts_mcp_server"]
  env:
    DTS_ENGINE_DIR: "D:/Users/zn198/AppData/Roaming/DTS Engine/6.1"
```

For Claude Code (`.mcp.json`):

```json
{
  "mcpServers": {
    "dts": {
      "command": "D:/adk/standalone/dts-mcp-server/.venv/Scripts/python.exe",
      "args": ["-m", "dts_mcp_server"]
    }
  }
}
```

### Tools

| Tool | Purpose |
|---|---|
| `dts_ping` | Verify the install. Call this first. |
| `dts_list_pipelines` | The 9 pipelines, their flags, and verified status. |
| `dts_explain_error` | Resolve an exit code through Engine's shipped table. |
| `dts_validate` | Pre-flight a flag set without running Engine. |
| `dts_publish` | Run a publish pipeline. |
| `dts_publish_osgb` | Publish OSGB, running both required stages. |
| `dts_job_new` | Write an `.aconf` job file. |
| `dts_job_types` | Job data types and their transcribed schemas. |

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
src/dts_mcp_server/        MCP layer (stdio)
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

Unit tests are pure and run anywhere. The E2E tier invokes the real Engine and
**fails rather than skips** when it is absent — a harness that cannot drive the
software is not working. See [`docs/TEST.md`](docs/TEST.md) for results and an
honest list of gaps.

## Status

`road` is verified end-to-end and publishes a real `.3dt` tile. The other eight
pipelines have flag names transcribed from the decompiled shell but unconfirmed
input requirements; they report `verified: false`, and `dts_list_pipelines` says
so. Verifying them needs input data (OSGB datasets, `.max` scenes, `.vtpk`
packages, DTM job descriptions) not available on the development machine.

## License

MIT.
