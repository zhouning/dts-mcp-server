"""MCP tool surface for remote clients.

Every tool that touches bulk data returns a signed URL rather than bytes: file
content never travels through JSON-RPC. The remote flow is

    create_upload -> curl PUT -> complete_upload
      -> publish (returns a job)
      -> get_job until terminal
      -> download_output (returns a signed GET URL) -> curl GET

Host absolute paths are stripped from every response.
"""
from __future__ import annotations

import uuid
from typing import Any

from cli_anything.dts_engine.core import pipelines as pipemod

from .artifacts import ArtifactService
from .config import Settings
from .engine import InputResolutionError, describe_pipelines, resolve_path_flags
from .jobs import JobService
from .models import ArtifactRecord, JobRecord

#: Fields holding Windows paths; never returned to a client.
_PRIVATE_ARTIFACT_FIELDS = ("storage_path", "content_root")


def _artifact(record: ArtifactRecord) -> dict[str, Any]:
    data = record.model_dump(mode="json")
    for field in _PRIVATE_ARTIFACT_FIELDS:
        data.pop(field, None)
    return data


def _job(record: JobRecord, jobs: JobService) -> dict[str, Any]:
    """Serialise a job with host paths stripped.

    ``request`` holds the resolved absolute paths the gateway passes to Engine,
    so it must be sanitised too — not just events and results.
    """
    data = record.model_dump(mode="json")
    data["request"] = jobs.sanitize(record.id, data.get("request"))
    data["result"] = jobs.sanitize(record.id, data.get("result"))
    data["error_message"] = jobs.sanitize(record.id, data.get("error_message"))
    return data


def _fail(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **extra}


def register_tools(
    mcp,
    settings: Settings,
    artifacts: ArtifactService,
    jobs: JobService,
) -> None:
    """Attach every DTS tool to the MCP server."""

    ttl = settings.signed_url_ttl_seconds
    base_url = settings.base_url

    # ------------------------------------------------------------ inspect

    @mcp.tool()
    async def dts_ping() -> dict:
        """Check that DTS Engine is installed and this gateway can drive it.

        Call this first. Reports the Engine version and the bundled GDAL.
        """
        from cli_anything.dts_engine.core.install import (
            EngineNotFound,
            describe_install,
        )
        try:
            info = describe_install(settings.engine_dir)
        except EngineNotFound as error:
            return _fail(
                str(error),
                hint="Install DTS Engine on the gateway host, or set "
                     "DTS_MCP_ENGINE_DIR.",
            )
        return {"ok": True, **info}

    @mcp.tool()
    async def dts_list_pipelines() -> dict:
        """List publish pipelines, their flags, and which are verified.

        `path_flags` names the flags that take an artifact reference rather than
        a string: pass those as {"artifact": "<id>", "path": "<relative>"}.

        `verified: false` means the flag names are transcribed from Engine's own
        GUI shell but the required input combination is unconfirmed — expect to
        iterate on error codes.
        """
        return {"ok": True, "pipelines": describe_pipelines()}

    @mcp.tool()
    async def dts_explain_error(code: int) -> dict:
        """Explain an Engine exit code using the table Engine ships.

        `ok` reports whether the lookup succeeded; whether the code itself means
        success is `indicates_success`.
        """
        from cli_anything.dts_engine.core import errors as errmod
        from cli_anything.dts_engine.core.install import (
            EngineNotFound,
            find_engine_dir,
        )
        try:
            table = errmod.load_table(find_engine_dir(settings.engine_dir))
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

    # ------------------------------------------------------------- upload

    @mcp.tool()
    async def dts_create_upload(
        logical_name: str,
        expected_size: int,
        expected_sha256: str,
        media_type: str = "application/octet-stream",
    ) -> dict:
        """Begin an upload and return a signed PUT URL.

        Declare the exact byte count and SHA-256 first; the gateway verifies both
        before the artifact becomes usable, so a truncated transfer fails loudly
        rather than producing a corrupt input.

        Upload with a plain HTTPS PUT — do NOT send the bearer token to the
        signed URL:

            curl --cacert ca.crt -X PUT --data-binary @roads.zip \\
              -H 'X-Upload-Offset: 0' "<upload_url>"

        Then call dts_complete_upload. Uploads are resumable: check
        dts_artifact_status for `committed_size` and resume from that offset.

        A `.zip` is extracted on completion, which is how to send a shapefile
        (4+ sidecar files) or an OSGB directory tree.
        """
        try:
            record = await artifacts.create_upload(
                logical_name, expected_size, expected_sha256, media_type
            )
        except ValueError as error:
            return _fail(str(error))
        return {
            "ok": True,
            "artifact": _artifact(record),
            "upload_url": artifacts.signed_url(
                base_url, record.id, "upload", ttl
            ),
            "expires_in_seconds": ttl,
        }

    @mcp.tool()
    async def dts_complete_upload(artifact_id: str) -> dict:
        """Finalise an upload: verify the digest and extract archives."""
        try:
            record = await artifacts.complete_upload(artifact_id)
        except KeyError:
            return _fail(f"unknown artifact {artifact_id}")
        except ValueError as error:
            return _fail(str(error))
        return {"ok": True, "artifact": _artifact(record)}

    @mcp.tool()
    async def dts_artifact_status(artifact_id: str) -> dict:
        """Report an artifact's state, including `committed_size` for resume."""
        try:
            record = await artifacts.get_artifact(artifact_id)
        except KeyError:
            return _fail(f"unknown artifact {artifact_id}")
        return {"ok": True, "artifact": _artifact(record)}

    @mcp.tool()
    async def dts_list_artifacts() -> dict:
        """List artifacts that have not been deleted."""
        records = await artifacts.list_artifacts()
        return {"ok": True, "artifacts": [_artifact(r) for r in records]}

    @mcp.tool()
    async def dts_delete_artifact(artifact_id: str) -> dict:
        """Delete an artifact's bytes. Refused while a live job references it."""
        try:
            if await artifacts.db.artifact_has_active_job(artifact_id):
                return _fail(
                    "artifact is referenced by an active job",
                    artifact_id=artifact_id,
                )
            record = await artifacts.delete_artifact(artifact_id)
        except KeyError:
            return _fail(f"unknown artifact {artifact_id}")
        return {"ok": True, "artifact": _artifact(record)}

    # ------------------------------------------------------------ publish

    @mcp.tool()
    async def dts_publish(
        pipeline: str,
        flags: dict[str, Any],
        timeout: int | None = None,
        strict: bool = True,
    ) -> dict:
        """Queue a publish job. Returns immediately; poll with dts_get_job.

        Path-valued flags take an artifact reference, not a string:

            flags = {
              "roadShp": {"artifact": "<id>", "path": "roads.shp"},
              "domPath": {"artifact": "<id>", "path": "dom.tif"},
              "demPath": {"artifact": "<id>", "path": "dem.tif"},
              "roadRGB": "255,0,0"
            }

        The verified pipeline is `road`, which drapes vector features onto
        terrain. It needs a PROJECTED shapefile — geographic coordinates fail
        with 203/209/215 — and `domPath` is mandatory despite looking optional
        (omitting it fails with 201).

        For OSGB use dts_publish_osgb: that format needs two sequential Engine
        processes and this tool would run only the first.
        """
        # Reject an unknown pipeline before doing any work. Left to the runner,
        # this would surface as a failed job minutes later instead of an
        # immediate, actionable error.
        try:
            pipemod.get(pipeline)
        except KeyError:
            return _fail(
                f"unknown pipeline {pipeline!r}",
                known=sorted(pipemod.PIPELINES),
            )

        # Resolve inputs and reserve the sandbox BEFORE enqueueing. Submitting a
        # placeholder request and patching it afterwards races the queue worker,
        # which can pick the job up with empty flags.
        job_id = str(uuid.uuid4())
        job_root = jobs.workspace.job_root(job_id)
        try:
            resolved, referenced = await resolve_path_flags(
                flags, artifacts, job_root / "input"
            )
        except InputResolutionError as error:
            return _fail(str(error))
        except KeyError as error:
            return _fail(f"unknown artifact {error}")
        except ValueError as error:
            return _fail(str(error))

        record = await jobs.submit(
            f"publish:{pipeline}",
            {
                "kind": "publish",
                "pipeline": pipeline,
                "flags": resolved,
                "outpath": str(job_root / "output"),
                "timeout": timeout or settings.job_timeout_seconds,
                "strict": strict,
            },
            input_artifacts=referenced,
            job_id=job_id,
        )
        return {"ok": True, "job": _job(record, jobs)}

    @mcp.tool()
    async def dts_publish_osgb(
        inpath: dict[str, Any],
        flags: dict[str, Any] | None = None,
        async_pyramid: bool = False,
        timeout: int | None = None,
    ) -> dict:
        """Queue an OSGB publish, running both stages Engine requires.

        `inpath` is an artifact reference: {"artifact": "<id>", "path": "<rel>"}.
        Upload the OSGB tree as a .zip; it is extracted on completion.

        Tiling and the LOD pyramid are separate Engine processes; the job is only
        `succeeded` when both are.
        """
        merged = dict(flags or {})
        merged["inpath"] = inpath
        job_id = str(uuid.uuid4())
        job_root = jobs.workspace.job_root(job_id)
        try:
            resolved, referenced = await resolve_path_flags(
                merged, artifacts, job_root / "input"
            )
        except InputResolutionError as error:
            return _fail(str(error))
        except (KeyError, ValueError) as error:
            return _fail(str(error))

        record = await jobs.submit(
            "publish_osgb",
            {
                "kind": "publish_osgb",
                "flags": resolved,
                "outpath": str(job_root / "output"),
                "async_pyramid": async_pyramid,
                "timeout": timeout or settings.job_timeout_seconds,
            },
            input_artifacts=referenced,
            job_id=job_id,
        )
        return {"ok": True, "job": _job(record, jobs)}

    # ---------------------------------------------------------------- jobs

    @mcp.tool()
    async def dts_get_job(job_id: str) -> dict:
        """Poll a job. Terminal states: succeeded, failed, timed_out,
        cancelled, interrupted.

        On failure read `error_code` (an Engine exit code — resolve it with
        dts_explain_error) and `events`, which carry Engine's own log lines with
        host paths already stripped.

        On success call dts_download_output for a signed GET URL.
        """
        try:
            record = await jobs.get(job_id)
        except KeyError:
            return _fail(f"unknown job {job_id}")
        return {"ok": True, "job": _job(record, jobs)}

    @mcp.tool()
    async def dts_list_jobs(limit: int = 20) -> dict:
        """List recent jobs, newest first."""
        records = await jobs.list_jobs(limit)
        return {"ok": True, "jobs": [_job(r, jobs) for r in records]}

    @mcp.tool()
    async def dts_cancel_job(job_id: str) -> dict:
        """Request cancellation. A queued job cancels at once; a running one is
        marked `cancelling` and settles when Engine's subprocess exits."""
        try:
            record = await jobs.cancel(job_id)
        except KeyError:
            return _fail(f"unknown job {job_id}")
        return {"ok": True, "job": _job(record, jobs)}

    # ------------------------------------------------------------ download

    @mcp.tool()
    async def dts_download_output(job_id: str) -> dict:
        """Package a finished job's output and return a signed GET URL.

        Engine writes a directory (a .3dt tile plus its DataInfor.txt key, or a
        whole OSGB pyramid), so the output is collapsed into one ZIP.

        Download with a plain HTTPS GET; do NOT send the bearer token:

            curl --cacert ca.crt -o out.zip "<download_url>"
        """
        try:
            record = await jobs.get(job_id)
        except KeyError:
            return _fail(f"unknown job {job_id}")
        if record.status != "succeeded":
            return _fail(
                f"job is {record.status}, not succeeded",
                job_status=str(record.status),
            )
        outpath = record.request.get("outpath")
        if not outpath:
            return _fail("job has no recorded output path")
        try:
            artifact = await artifacts.register_output(
                __import__("pathlib").Path(outpath),
                logical_name=f"{job_id}-output.zip",
            )
        except ValueError as error:
            return _fail(str(error))
        return {
            "ok": True,
            "artifact": _artifact(artifact),
            "download_url": artifacts.signed_url(
                base_url, artifact.id, "download", ttl
            ),
            "expires_in_seconds": ttl,
        }
