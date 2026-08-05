"""Bridge between the job queue and the DTS Engine harness.

Translates a remote client's artifact-relative request into concrete host paths
inside the job's sandbox, runs the harness, then hands outputs back for
registration as downloadable artifacts.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from cli_anything.dts_engine.core import pipelines as pipemod
from cli_anything.dts_engine.core.publish import publish, publish_osgb

#: Flags whose value is a path the client supplies. Each must be resolved from an
#: artifact rather than taken literally, or a client could name any host file.
PATH_FLAGS: frozenset[str] = frozenset({
    "roadShp", "inpath", "demPath", "domPath", "seaPath", "seaDomPath",
    "coastDemPath", "shpOuterPath", "shpInnerPath", "jsonPath",
    "styleJsonPath", "dtmjsonpath",
})


class InputResolutionError(ValueError):
    """A path flag could not be resolved to an artifact-relative file."""


async def resolve_path_flags(
    flags: dict[str, Any],
    artifacts,
    job_input_dir: Path,
) -> tuple[dict[str, Any], list[str]]:
    """Replace ``{"artifact": id, "path": rel}`` values with host paths.

    Returns the rewritten flags and the artifact IDs referenced, so the caller
    can record them and keep cleanup from deleting a live input.

    A bare string in a path flag is rejected outright: allowing it would let a
    remote client name ``C:\\Windows\\...`` and have Engine read it.
    """
    resolved: dict[str, Any] = {}
    referenced: list[str] = []

    for key, value in flags.items():
        if key not in PATH_FLAGS:
            resolved[key] = value
            continue
        if not isinstance(value, dict) or "artifact" not in value:
            raise InputResolutionError(
                f"-{key} must be given as "
                f'{{"artifact": "<id>", "path": "<relative path>"}}; '
                "host paths are not accepted from remote clients."
            )
        artifact_id = str(value["artifact"])
        relative = str(value.get("path") or "")
        if not relative:
            record = await artifacts.get_artifact(artifact_id)
            relative = record.logical_name
        path = await artifacts.resolve_ready_path(artifact_id, relative)
        resolved[key] = str(path)
        referenced.append(artifact_id)

    return resolved, referenced


def build_runner(engine_dir: Path | None) -> Callable[..., dict]:
    """Return a callable the job queue can run in a worker thread."""

    def run(request: dict[str, Any], on_line) -> dict:
        kind = request.get("kind", "publish")
        flags = dict(request.get("flags") or {})
        outpath = request["outpath"]

        if kind == "publish_osgb":
            inpath = flags.pop("inpath", None)
            if not inpath:
                return {
                    "ok": False,
                    "error": {"code": 1, "band": "io",
                              "message": "inpath is required for OSGB"},
                }
            return publish_osgb(
                inpath, outpath,
                engine_dir=engine_dir,
                async_pyramid=bool(request.get("async_pyramid")),
                timeout=int(request.get("timeout", 3600)),
                on_line=on_line,
                **flags,
            )

        flags["outpath"] = outpath
        return publish(
            request["pipeline"], flags,
            engine_dir=engine_dir,
            timeout=int(request.get("timeout", 3600)),
            strict=bool(request.get("strict", True)),
            on_line=on_line,
        )

    return run


def describe_pipelines() -> list[dict[str, Any]]:
    """Pipeline metadata, annotated with which flags take artifact references."""
    return [
        {
            "name": p.name,
            "handleType": p.handle_type,
            "required": list(p.required),
            "optional": list(p.optional),
            "path_flags": sorted(
                f for f in (*p.required, *p.optional) if f in PATH_FLAGS
            ),
            "verified": p.verified,
            "summary": p.summary,
            "notes": p.notes,
            "source": p.source,
        }
        for p in pipemod.PIPELINES.values()
    ]
