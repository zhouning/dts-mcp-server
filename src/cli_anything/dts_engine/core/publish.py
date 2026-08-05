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


def publish_osgb(
    inpath: str | Path,
    outpath: str | Path,
    engine_dir: str | Path | None = None,
    async_pyramid: bool = False,
    timeout: int = 3600,
    on_line=None,
    **extra,
) -> dict:
    """Publish OSGB as the shell does: tile, then build the LOD pyramid.

    The shell runs these as two separate processes (FormMain.cs:3369 and 3416).
    A caller that stops after stage one gets an incomplete dataset, so both
    stages are reported here and ``ok`` requires both to succeed.
    """
    base: dict[str, object] = {
        "inpath": str(inpath),
        "outpath": str(outpath),
        **extra,
    }

    stage1 = publish(
        "osgbLod", {**base, "filter": "+osgb"},
        engine_dir=engine_dir, timeout=timeout, strict=False, on_line=on_line,
    )
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
