"""MCP server for DTS Engine — 3D digital-twin tile publishing.

DTS Engine (飞渡科技 / Freedo) is a Windows GUI product with no SDK and no
documented CLI. This server drives the 93-flag command line that Engine's own
GUI shell uses internally; see docs/ENGINE_PROTOCOL.md for how that contract was
recovered.

Transport is stdio, matching the sibling arcgis-mcp and qgis-mcp servers, so the
Engine install must be reachable from this process (same machine).
"""
from __future__ import annotations

import os
from typing import Any

from mcp.server import MCPServer

from cli_anything.dts_engine.core import errors as errmod
from cli_anything.dts_engine.core import job as jobmod
from cli_anything.dts_engine.core import pipelines as pipemod
from cli_anything.dts_engine.core.install import EngineNotFound, describe_install, find_engine_dir
from cli_anything.dts_engine.core.publish import publish, publish_osgb

from . import __version__

mcp = MCPServer(
    "dts-engine",
    description="Publish 3D digital-twin tiles with DTS Engine (飞渡科技 / Freedo).",
    instructions=(
        "Call dts_ping first to confirm DTS Engine is installed. Then "
        "dts_list_pipelines to see what can be published and which pipelines are "
        "verified. Only `road` is verified end-to-end; treat others as "
        "best-effort and read dts_explain_error on any non-zero exit code. "
        "Input shapefiles must use a projected CRS, not lat/lon."
    ),
    version=__version__,
)

#: Default cap for a publish job. Real jobs can run for many minutes; Engine
#: gives no progress total, so a hang is indistinguishable from slow work.
DEFAULT_TIMEOUT = int(os.environ.get("DTS_JOB_TIMEOUT", "3600"))


def _engine_dir() -> str | None:
    """Install dir override, if configured. None lets discovery run."""
    return os.environ.get("DTS_ENGINE_DIR") or None


def _fail(message: str, **extra: Any) -> dict:
    return {"ok": False, "error": message, **extra}


@mcp.tool()
def dts_ping() -> dict:
    """Check that DTS Engine is installed and reachable.

    Call this first. Reports the Engine version, which executables are present,
    and the bundled GDAL. Every other tool fails if this one does.
    """
    try:
        info = describe_install(_engine_dir())
    except EngineNotFound as e:
        return _fail(
            str(e),
            hint="Install DTS Engine, or set DTS_ENGINE_DIR to the directory "
                 "containing EngineWorker.exe.",
        )
    return {"ok": True, **info}


@mcp.tool()
def dts_list_pipelines() -> dict:
    """List the publish pipelines, their flags, and whether each is verified.

    `verified: true` means the pipeline has been confirmed by a successful real
    run. Unverified pipelines have correct flag names transcribed from the
    decompiled GUI shell, but their required input combinations are unconfirmed —
    expect to iterate on error codes.
    """
    return {
        "ok": True,
        "pipelines": [
            {
                "name": p.name,
                "handleType": p.handle_type,
                "required": list(p.required),
                "optional": list(p.optional),
                "verified": p.verified,
                "summary": p.summary,
                "notes": p.notes,
                "source": p.source,
            }
            for p in pipemod.PIPELINES.values()
        ],
        "common_optional_flags": list(pipemod.COMMON_OPTIONAL),
    }


@mcp.tool()
def dts_explain_error(code: int) -> dict:
    """Explain a DTS Engine exit code using the table Engine ships.

    Bands: io (0-19), lod (100-119), terrain (200-249), resource (300-309),
    crs (400-409), vtpk (500-509), dem_opt (600+).

    `ok` reports whether the lookup succeeded. Whether the *code* means success
    is `indicates_success` — do not conflate the two.
    """
    try:
        table = errmod.load_table(find_engine_dir(_engine_dir()))
    except EngineNotFound:
        table = dict(errmod.FALLBACK)
    described = errmod.describe(code, table)
    return {
        "ok": True,
        "code": described["code"],
        "indicates_success": described["ok"],
        "band": described["band"],
        "message": described["message"],
    }


@mcp.tool()
def dts_validate(pipeline: str, flags: dict[str, Any]) -> dict:
    """Check a flag set against a pipeline spec without running anything.

    Cheap pre-flight: catches missing required flags and typos locally, so a
    malformed publish costs no Engine time.
    """
    try:
        problems = pipemod.validate(pipeline, flags)
    except KeyError as e:
        return _fail(str(e), known=sorted(pipemod.PIPELINES))
    return {"ok": not problems, "pipeline": pipeline, "problems": problems}


@mcp.tool()
def dts_publish(
    pipeline: str,
    outpath: str,
    flags: dict[str, Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    strict: bool = True,
) -> dict:
    """Run a DTS Engine publish pipeline and report artifacts.

    `flags` takes any of Engine's 93 flags without the leading dash, e.g.
    {"roadShp": "roads.shp", "domPath": "dom.tif", "demPath": "dem.tif"}.

    The verified workflow is `pipeline="road"`, which drapes vector features onto
    terrain. It needs a PROJECTED shapefile (geographic coordinates fail with
    203/209/215) plus a DOM raster; domPath is mandatory despite looking
    optional.

    Success yields a .3dt tile plus DataInfor.txt. On failure, read `error.band`
    and `log` — the UDP log usually carries a more specific message than the
    exit code. Use dts_explain_error for any code.

    Set strict=False to pass experimental flags the spec does not list.

    For OSGB use dts_publish_osgb instead: that format needs two sequential
    processes and this tool would only run the first.
    """
    merged = dict(flags or {})
    merged["outpath"] = outpath
    try:
        result = publish(
            pipeline, merged,
            engine_dir=_engine_dir(), timeout=timeout, strict=strict,
        )
    except EngineNotFound as e:
        return _fail(str(e))
    except KeyError as e:
        return _fail(str(e), known=sorted(pipemod.PIPELINES))
    return result


@mcp.tool()
def dts_publish_osgb(
    inpath: str,
    outpath: str,
    flags: dict[str, Any] | None = None,
    async_pyramid: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Publish oblique-photogrammetry OSGB data, running both required stages.

    OSGB publishing is two sequential Engine processes: tiling, then the LOD
    pyramid. Stopping after the first leaves an incomplete dataset, so both are
    run and reported here. `ok` is true only if both stages succeed.
    """
    try:
        return publish_osgb(
            inpath, outpath,
            engine_dir=_engine_dir(), async_pyramid=async_pyramid,
            timeout=timeout, **(flags or {}),
        )
    except EngineNotFound as e:
        return _fail(str(e))


@mcp.tool()
def dts_create_job(data_type: str, out: str, fields: dict[str, Any] | None = None) -> dict:
    """Write an .aconf job file, the input for Engine's -jsonPath mode.

    `data_type` is one of Terrain, Osgb, MAX, BIM, LAS, Material, Vector, Sea,
    Skeletal, DTOModeData. Field names are the C# property names Engine expects
    (e.g. RoadShpPath, OutputPath) — call dts_job_types to list the known ones.

    Note: job-file mode is transcribed from the decompiled shell but has not been
    verified end-to-end. Prefer dts_publish for production work.
    """
    try:
        job = jobmod.new_job(data_type, **(fields or {}))
    except KeyError as e:
        return _fail(str(e), known=sorted(jobmod.DATA_TYPES))
    path = jobmod.write_job(job, out)
    return {
        "ok": True,
        "path": str(path),
        "data_type": data_type,
        "job": job,
        "warnings": jobmod.validate_job(data_type, job),
    }


@mcp.tool()
def dts_job_types() -> dict:
    """List .aconf job data types and the fields transcribed for each.

    A null `fields` means that type's schema has not been transcribed from the
    decompiled shell, so its fields are not validated rather than guessed at.
    """
    return {
        "ok": True,
        "types": [
            {
                "data_type": name,
                "ordinal": ordinal,
                "fields": list(jobmod.JOB_FIELDS.get(name, ())) or None,
            }
            for name, ordinal in jobmod.DATA_TYPES.items()
        ],
    }
