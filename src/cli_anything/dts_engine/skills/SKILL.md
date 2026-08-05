---
name: "cli-anything-dts-engine"
description: "Publish 3D digital-twin tiles headlessly with DTS Engine 6.1 (飞渡科技/Freedo). Use when asked to convert shapefiles, OSGB oblique photogrammetry, DEM/DOM rasters, BIM, or point clouds into .3dt tiles, or to drive DTS Engine without its GUI."
---

# cli-anything-dts-engine

Headless CLI for **DTS Engine 6.1**, a Windows GUI tool for producing 3D
digital-twin tiles. Engine ships no SDK and no documented CLI; this harness
targets the 93-flag command line that its own GUI shell uses internally, so an
agent can publish tiles with no display and no mouse.

## Prerequisites

- **DTS Engine must be installed and licensed.** It is a required dependency,
  not optional — the CLI drives the real `EngineWorker.exe`.
- Windows. Engine is Win-only.
- Set `DTS_ENGINE_DIR` if the install is not at `%APPDATA%\DTS Engine\6.1`.

```bash
pip install -e agent-harness            # from subsystems/tool-mcp-servers/dts-mcp
cli-anything-dts-engine info            # confirm discovery
```

## Start here

Always run `info` then `pipelines` before composing a publish. `pipelines`
reports each pipeline's required flags and whether it is **verified** against a
real run — treat unverified ones as best-effort.

```bash
cli-anything-dts-engine --json info
cli-anything-dts-engine --json pipelines
```

## Commands

| Command | Purpose |
|---|---|
| `info` | Locate the install; report version, executables, bundled GDAL. |
| `pipelines` | List pipelines, required/optional flags, verified status. |
| `errors [--code N]` | The shipped ~90-code error table, or explain one code. |
| `run PIPELINE --outpath DIR --set K=V …` | Run any pipeline. |
| `publish-osgb --inpath … --outpath …` | OSGB in both required stages. |
| `job new TYPE -o FILE --set Field=Value` | Create an `.aconf` job file. |
| `job show FILE` / `job types` | Inspect job files and their schemas. |
| `repl` | Interactive session (also the default with no subcommand). |

Add `--json` **before** the subcommand for machine-readable output:
`cli-anything-dts-engine --json run road …`

## Publishing (verified workflow)

`road` drapes vector features onto terrain and is the one pipeline confirmed
end-to-end. It needs a **projected** shapefile plus a DOM (orthophoto) raster;
a DEM is strongly recommended.

```bash
cli-anything-dts-engine --json run road \
  --outpath ./out \
  --set roadShp=roads.shp \
  --set domPath=dom.tif \
  --set demPath=dem.tif \
  --set roadRGB=255,0,0 \
  --set roadSize=5
```

Success produces `<name>.3dt` (the tile, typically megabytes) and
`DataInfor.txt` (a 32-hex encryption key). Check `ok`, then `outputs`.

### OSGB needs both stages

Publishing OSGB is **two sequential processes** — tiling, then the LOD pyramid.
Running the raw pipeline once leaves an incomplete dataset. Use the dedicated
command, which runs and reports both:

```bash
cli-anything-dts-engine --json publish-osgb --inpath ./osgb --outpath ./out
```

## Interpreting failures

Exit codes are meaningful and come from a table Engine ships. Resolve any code:

```bash
cli-anything-dts-engine --json errors --code 201
# {"code": 201, "ok": false, "band": "terrain", "message": "The DOM path is empty"}
```

Bands: `io` (0–19), `lod` (100–119), `terrain` (200–249), `resource` (300–309),
`crs` (400–409), `vtpk` (500–509), `dem_opt` (600+).

Codes worth knowing:

| Code | Meaning | Fix |
|---|---|---|
| 1 | Invalid parameter | Check flag spelling against `pipelines`. |
| 6 | GDAL init failed | Input format unreadable. |
| 201 | DOM path empty | Add `--set domPath=…`. |
| 203 / 209 / 215 | CRS problems | Input must be a **projected** CRS, not lat/lon. |
| 302 | Tree resource read failed | `shp` expects vegetation attributes (see below). |

Engine streams log lines over UDP; the CLI captures them into `log` in JSON
output and prints ERROR lines to stderr otherwise. On failure, read `log` — it
usually carries a `RetCode` and a message more specific than the exit code.

## Traps

- **`shp` is not a general vector converter.** It builds vegetation/material
  resources and expects tree attributes; a plain polygon shapefile fails with
  302. For general vector work use `road` (or `vtpk`).
- **Projected CRS is mandatory.** Geographic coordinates trigger 203/209/215.
- **`road` requires `domPath`** even though it looks optional.
- **Never drive `EngineMaster.exe`.** It returns -1 (0xFFFFFFFF) instead of the
  child's error code. The harness always uses `EngineWorker.exe`; the exit code
  is the diagnostic.
- **Only `road` is verified.** `osgbLod`, `multosgb`, `max`, `vtpk`,
  `waterTerrain`, `Library`, `terrain`, and `shp`'s success path have correct
  flag names but unconfirmed input requirements. Expect to iterate on error
  codes; `--no-strict` lets experimental flags through.
- **Engine is not a GDAL wrapper.** It bundles GDAL 3.0.5 for format I/O and
  reprojection only. LOD generation, mesh simplification, texture atlasing,
  and 3DT tiling have no GDAL equivalent — do not substitute `ogr2ogr` for a
  publish step.

## Agent usage notes

- Use `--json` for every programmatic call and branch on `ok`.
- Validation runs locally first: a missing required flag returns
  `validation_errors` with no process spawned, so a malformed call costs nothing.
- Long jobs: `--timeout` (seconds, default 3600). `timed_out: true` in the
  result means the cap was hit, not that the job failed.
- `--set` accepts **any** of the 93 flags, so uncovered capabilities remain
  reachable without changing this CLI.

## Protocol reference

The full reverse-engineered contract — all 93 flags, job schema, UDP framing,
and how it was recovered — is in `ENGINE_PROTOCOL.md` at the harness root.
