"""Job queue.

Publishing takes minutes, so every publish returns a job record immediately and
the client polls. Execution is single-lane: Engine is a licensed desktop product
and concurrent publishes contend for the same licence and the same disk, so the
queue serialises them.

There is no separate worker process. Engine work is already an out-of-process
``EngineWorker.exe`` subprocess, so a supervisor layer would only add a second
hop; cancellation is a subprocess kill either way.
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .db import Database
from .models import JobRecord, JobStatus, utc_now
from .workspace import Workspace

#: Windows absolute paths embedded in free-text log lines. Engine's UDP log
#: writes prose ("发布失败，RetCode:201"), so host paths appear mid-sentence
#: rather than as discrete fields and have to be rewritten in place.
_WINDOWS_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|]*")
_UNC_PATH = re.compile(r"\\\\[^\s\"'<>|]+")


class JobService:
    def __init__(
        self,
        db: Database,
        workspace: Workspace,
        runner: Callable[..., Any],
        timeout_seconds: int,
    ) -> None:
        self.db = db
        self.workspace = workspace
        self.runner = runner
        self.timeout_seconds = timeout_seconds
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._current: str | None = None
        self._cancelled: set[str] = set()

    # ----------------------------------------------------------- lifecycle

    async def start(self) -> None:
        interrupted = await self.db.reconcile_after_restart()
        if interrupted:
            # Not an error: the gateway cannot know whether Engine finished.
            print(f"marked {len(interrupted)} interrupted job(s)", flush=True)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # -------------------------------------------------------------- submit

    async def submit(
        self,
        tool_id: str,
        request: dict[str, Any],
        input_artifacts: list[str] | None = None,
        job_id: str | None = None,
    ) -> JobRecord:
        """Enqueue a job.

        ``job_id`` lets a caller reserve the sandbox and resolve inputs before
        the job becomes visible to the worker, avoiding a race where the worker
        picks up a half-built request.
        """
        job = JobRecord(
            id=job_id or str(uuid.uuid4()),
            tool_id=tool_id,
            status=JobStatus.QUEUED,
            request=request,
        )
        await self.db.upsert_job(job, input_artifacts=input_artifacts)
        await self._queue.put(job.id)
        return job

    async def get(self, job_id: str) -> JobRecord:
        job = await self.db.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    async def list_jobs(self, limit: int = 50) -> list[JobRecord]:
        return await self.db.list_jobs(limit)

    async def cancel(self, job_id: str) -> JobRecord:
        job = await self.get(job_id)
        if job.status in {
            JobStatus.SUCCEEDED, JobStatus.FAILED,
            JobStatus.CANCELLED, JobStatus.TIMED_OUT,
        }:
            return job
        self._cancelled.add(job_id)
        if job.status is JobStatus.QUEUED:
            # Never started, so it can be finalised immediately.
            return await self._transition(job, JobStatus.CANCELLED)
        return await self._transition(job, JobStatus.CANCELLING)

    # --------------------------------------------------------- transitions

    async def _transition(
        self,
        job: JobRecord,
        status: JobStatus,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> JobRecord:
        job.status = status
        job.updated_at = utc_now()
        if result is not None:
            job.result = result
        if error_code is not None:
            job.error_code = error_code
        if error_message is not None:
            job.error_message = error_message
        await self.db.upsert_job(job)
        return job

    # -------------------------------------------------------- sanitisation

    def sanitize(self, job_id: str, value: Any) -> Any:
        """Strip host paths from anything bound for a client.

        Paths inside the job's own workspace are rewritten relative to it — the
        client owns those and needs to recognise them. Anything else on disk is
        redacted, since an Engine install path or another job's directory is not
        the client's business.
        """
        if isinstance(value, dict):
            return {k: self.sanitize(job_id, v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.sanitize(job_id, v) for v in value]
        if not isinstance(value, str):
            return value

        root = self.workspace.job_root(job_id).resolve()

        def replace(match: re.Match[str]) -> str:
            candidate = Path(match.group(0))
            try:
                resolved = candidate.resolve(strict=False)
                if os.path.commonpath([root, resolved]) == str(root):
                    return resolved.relative_to(root).as_posix()
            except (ValueError, OSError):
                pass
            return "[redacted-path]"

        return _UNC_PATH.sub(replace, _WINDOWS_PATH.sub(replace, value))

    async def append_event(self, job_id: str, event: dict[str, Any]) -> None:
        job = await self.db.get_job(job_id)
        if job is None:
            return
        job.events.append(self.sanitize(job_id, event))
        job.updated_at = utc_now()
        await self.db.upsert_job(job)

    # ------------------------------------------------------------ execution

    async def _loop(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._execute(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # a bad job must not kill the queue
                job = await self.db.get_job(job_id)
                if job is not None:
                    await self._transition(
                        job, JobStatus.FAILED,
                        error_code="gateway_error",
                        error_message=self.sanitize(job_id, str(error)),
                    )
            finally:
                self._queue.task_done()

    async def _execute(self, job_id: str) -> None:
        job = await self.db.get_job(job_id)
        if job is None:
            return
        if job_id in self._cancelled:
            self._cancelled.discard(job_id)
            await self._transition(job, JobStatus.CANCELLED)
            return

        self._current = job_id
        job = await self._transition(job, JobStatus.STARTING)
        events: list[dict[str, Any]] = []

        def on_line(record: dict[str, Any]) -> None:
            # Called from the harness's UDP listener thread; only append here and
            # persist once the job finishes, to avoid cross-thread DB access.
            events.append(self.sanitize(job_id, record))

        job = await self._transition(job, JobStatus.RUNNING)
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self.runner, job.request, on_line),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            job.events = events
            await self._transition(
                job, JobStatus.TIMED_OUT,
                error_code="timeout",
                error_message=(
                    f"Engine did not finish within {self.timeout_seconds}s. "
                    "The subprocess was abandoned; check the host."
                ),
            )
            return
        finally:
            self._current = None

        job.events = events
        if job_id in self._cancelled:
            self._cancelled.discard(job_id)
            await self._transition(job, JobStatus.CANCELLED, result=result)
            return

        sanitized = self.sanitize(job_id, result)
        if result.get("ok"):
            await self._transition(job, JobStatus.SUCCEEDED, result=sanitized)
        else:
            error = result.get("error") or {}
            await self._transition(
                job, JobStatus.FAILED, result=sanitized,
                error_code=str(error.get("code", "engine_error")),
                error_message=error.get("message") or "Engine reported a failure",
            )
