"""High-level publish operations.

Wraps the backend with pipeline validation and models the multi-process shape of
OSGB publishing, which the raw CLI does not express.
"""
from __future__ import annotations

from pathlib import Path

from ..utils.engine_backend import collect_outputs, run_worker
from . import pipelines


def publish(
    pipeline: str,
    flags: dict[str, object],
    engine_dir: str | Path | None = None,
    timeout: int = 3600,
    strict: bool = True,
    on_line=None,
) -> dict:
    """Run one pipeline and report result plus produced artifacts.

    ``strict`` rejects unknown/missing flags before launching; set False to pass
    experimental flags straight through.
    """
    problems = pipelines.validate(pipeline, flags)
    if problems and strict:
        return {
            "ok": False,
            "pipeline": pipeline,
            "validation_errors": problems,
            "hint": "Pass strict=False (CLI: --no-strict) to run anyway.",
        }

    merged = pipelines.to_flags(pipeline, flags)
    result = run_worker(merged, engine_dir=engine_dir, timeout=timeout, on_line=on_line)
    result["pipeline"] = pipeline
    if problems:
        result["validation_warnings"] = problems
    outpath = flags.get("outpath")
    if outpath:
        result["outputs"] = collect_outputs(str(outpath))
        result["output_count"] = len(result["outputs"])
        result["output_bytes"] = sum(o["size"] for o in result["outputs"])
    return result


def find_osgb_tiles(inpath: str | Path) -> list[Path]:
    """Root .osgb of each tile: ``<inpath>/Data/<Tile>/<Tile>.osgb``."""
    data = Path(inpath) / "Data"
    if not data.is_dir():
        return []
    return [t / f"{t.name}.osgb" for t in sorted(data.iterdir())
            if t.is_dir() and (t / f"{t.name}.osgb").is_file()]


def publish_osgb(
    inpath: str | Path,
    outpath: str | Path,
    engine_dir: str | Path | None = None,
    async_pyramid: bool = False,
    timeout: int = 3600,
    on_line=None,
    **extra,
) -> dict:
    """Publish OSGB as the GUI does: per-tile conversion, then the LOD pyramid.

    Captured live from Engine 7.0.0814 (2026-11-14). Stage 1 is EngineMaster,
    which spawns one EngineWorker per tile with
    ``-handleType="" -MasterCreate=true -inpath=<Data/T/T.osgb>
    -outpath=<out/Data/T> -filter=+osgb`` (forward-slash paths). Stage 2 is
    EngineWorker ``-handleType=osgbLod -inpath=<root> -outpath=<out>``, which
    writes only the ~0.5MB root pyramid ``<out>/<name>.3dt``.

    We replay stage 1 per tile ourselves instead of via EngineMaster so each
    tile gets its own exit code. Calling ``osgbLod`` with ``-filter=+osgb`` on
    the root (the old stage 1) yields only the root pyramid and NO tile 3DTs.
    ``ok`` requires every tile and the pyramid to succeed.
    """
    base: dict[str, object] = {
        "inpath": str(inpath),
        "outpath": str(outpath),
        **extra,
    }

    tiles = find_osgb_tiles(inpath)
    tile_results = []
    for root in tiles:
        flags = {
            **extra,
            "handleType": "",
            "MasterCreate": "true",
            "inpath": root.as_posix(),
            "outpath": (Path(outpath) / "Data" / root.parent.name).as_posix(),
            "filter": "+osgb",
        }
        r = run_worker(flags, engine_dir=engine_dir, timeout=timeout, on_line=on_line)
        tile_results.append({"tile": root.parent.name, **r})
    stage1 = {
        "ok": bool(tiles) and all(t.get("ok") for t in tile_results),
        "tile_count": len(tiles),
        "failed_tiles": [t["tile"] for t in tile_results if not t.get("ok")],
        "tiles": tile_results,
    }
    if not tiles:
        stage1["error"] = {"code": 104, "band": "lod",
                           "message": "no <inpath>/Data/<Tile>/<Tile>.osgb found"}
    stages = [{"stage": "tile", **stage1}]

    # The shell skips the pyramid when stage one failed and it is not running
    # the async variant.
    if stage1.get("ok") or async_pyramid:
        stage2 = publish(
            "osgbLod", base,
            engine_dir=engine_dir, timeout=timeout, strict=False, on_line=on_line,
        )
        stages.append({"stage": "pyramid", **stage2})

    return {
        "pipeline": "osgbLod",
        "mode": "async_pyramid" if async_pyramid else "sequential",
        "ok": all(s.get("ok") for s in stages) and len(stages) == 2,
        "stages": stages,
        "outputs": collect_outputs(str(outpath)),
    }
