"""SQLite persistence for artifacts and jobs.

Artifact records outlive their bytes: deletion is hard on disk and soft in the
database, so an audit trail survives cleanup. Job rows persist so a client can
still poll a job whose result arrived while it was disconnected.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from .models import (
    ACTIVE_JOB_STATUSES,
    ArtifactRecord,
    ArtifactState,
    JobRecord,
    JobStatus,
    utc_now,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    logical_name TEXT NOT NULL,
    state TEXT NOT NULL,
    expected_size INTEGER NOT NULL,
    expected_sha256 TEXT NOT NULL,
    committed_size INTEGER NOT NULL DEFAULT 0,
    actual_sha256 TEXT,
    media_type TEXT NOT NULL,
    storage_path TEXT,
    content_root TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    tool_id TEXT NOT NULL,
    status TEXT NOT NULL,
    request TEXT NOT NULL,
    result TEXT,
    error_code TEXT,
    error_message TEXT,
    events TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_inputs (
    job_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    PRIMARY KEY (job_id, artifact_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_artifacts_state ON artifacts(state);
"""


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        # WAL keeps the hourly cleanup sweep from blocking in-flight requests.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database is not connected")
        return self._conn

    # ---------------------------------------------------------- artifacts

    async def upsert_artifact(self, record: ArtifactRecord) -> None:
        await self.conn.execute(
            """
            INSERT INTO artifacts (id, logical_name, state, expected_size,
                expected_sha256, committed_size, actual_sha256, media_type,
                storage_path, content_root, created_at, completed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                state=excluded.state,
                committed_size=excluded.committed_size,
                actual_sha256=excluded.actual_sha256,
                storage_path=excluded.storage_path,
                content_root=excluded.content_root,
                completed_at=excluded.completed_at
            """,
            (
                record.id, record.logical_name, str(record.state),
                record.expected_size, record.expected_sha256,
                record.committed_size, record.actual_sha256, record.media_type,
                record.storage_path, record.content_root,
                record.created_at.isoformat(),
                record.completed_at.isoformat() if record.completed_at else None,
            ),
        )
        await self.conn.commit()

    async def get_artifact(self, artifact_id: str) -> ArtifactRecord | None:
        cursor = await self.conn.execute(
            "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
        )
        row = await cursor.fetchone()
        return self._artifact_from_row(row) if row else None

    async def list_artifacts(
        self, include_deleted: bool = False
    ) -> list[ArtifactRecord]:
        sql = "SELECT * FROM artifacts"
        params: tuple[Any, ...] = ()
        if not include_deleted:
            sql += " WHERE state != ?"
            params = (str(ArtifactState.DELETED),)
        sql += " ORDER BY created_at DESC"
        cursor = await self.conn.execute(sql, params)
        return [self._artifact_from_row(r) for r in await cursor.fetchall()]

    async def artifact_has_active_job(self, artifact_id: str) -> bool:
        """True when a live job still references this artifact.

        Cleanup and manual deletion both consult this so an input cannot be
        pulled out from under a running publish.
        """
        placeholders = ",".join("?" * len(ACTIVE_JOB_STATUSES))
        cursor = await self.conn.execute(
            f"""
            SELECT 1 FROM job_inputs
            JOIN jobs ON jobs.id = job_inputs.job_id
            WHERE job_inputs.artifact_id = ?
              AND jobs.status IN ({placeholders})
            LIMIT 1
            """,
            (artifact_id, *(str(s) for s in ACTIVE_JOB_STATUSES)),
        )
        return await cursor.fetchone() is not None

    @staticmethod
    def _artifact_from_row(row: aiosqlite.Row) -> ArtifactRecord:
        return ArtifactRecord(
            id=row["id"],
            logical_name=row["logical_name"],
            state=ArtifactState(row["state"]),
            expected_size=row["expected_size"],
            expected_sha256=row["expected_sha256"],
            committed_size=row["committed_size"],
            actual_sha256=row["actual_sha256"],
            media_type=row["media_type"],
            storage_path=row["storage_path"],
            content_root=row["content_root"],
            created_at=_dt(row["created_at"]),
            completed_at=_dt(row["completed_at"]),
        )

    # --------------------------------------------------------------- jobs

    async def upsert_job(
        self, record: JobRecord, input_artifacts: list[str] | None = None
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO jobs (id, tool_id, status, request, result, error_code,
                error_message, events, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status,
                result=excluded.result,
                error_code=excluded.error_code,
                error_message=excluded.error_message,
                events=excluded.events,
                updated_at=excluded.updated_at
            """,
            (
                record.id, record.tool_id, str(record.status),
                json.dumps(record.request, default=str),
                json.dumps(record.result, default=str) if record.result else None,
                record.error_code, record.error_message,
                json.dumps(record.events, default=str),
                record.created_at.isoformat(), record.updated_at.isoformat(),
            ),
        )
        for artifact_id in input_artifacts or []:
            await self.conn.execute(
                "INSERT OR IGNORE INTO job_inputs (job_id, artifact_id) "
                "VALUES (?,?)",
                (record.id, artifact_id),
            )
        await self.conn.commit()

    async def get_job(self, job_id: str) -> JobRecord | None:
        cursor = await self.conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        return self._job_from_row(row) if row else None

    async def list_jobs(self, limit: int = 50) -> list[JobRecord]:
        cursor = await self.conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [self._job_from_row(r) for r in await cursor.fetchall()]

    async def reconcile_after_restart(self) -> list[str]:
        """Mark jobs that were in flight when the gateway died.

        The queue is in memory, so these cannot resume. Reporting INTERRUPTED is
        honest: the gateway does not know whether Engine finished or not.
        """
        placeholders = ",".join("?" * len(ACTIVE_JOB_STATUSES))
        cursor = await self.conn.execute(
            f"SELECT id FROM jobs WHERE status IN ({placeholders})",
            tuple(str(s) for s in ACTIVE_JOB_STATUSES),
        )
        ids = [row["id"] for row in await cursor.fetchall()]
        if ids:
            await self.conn.execute(
                f"UPDATE jobs SET status = ?, error_code = ?, "
                f"error_message = ?, updated_at = ? "
                f"WHERE status IN ({placeholders})",
                (
                    str(JobStatus.INTERRUPTED),
                    "gateway_restarted",
                    "The gateway restarted while this job was in flight; its "
                    "outcome is unknown. Re-submit if the output is missing.",
                    utc_now().isoformat(),
                    *(str(s) for s in ACTIVE_JOB_STATUSES),
                ),
            )
            await self.conn.commit()
        return ids

    @staticmethod
    def _job_from_row(row: aiosqlite.Row) -> JobRecord:
        return JobRecord(
            id=row["id"],
            tool_id=row["tool_id"],
            status=JobStatus(row["status"]),
            request=json.loads(row["request"]),
            result=json.loads(row["result"]) if row["result"] else None,
            error_code=row["error_code"],
            error_message=row["error_message"],
            events=json.loads(row["events"] or "[]"),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )
