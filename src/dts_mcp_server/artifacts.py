"""Artifact store — the data plane.

MCP negotiates; bytes move over plain HTTPS on the same port. Every transfer URL
is HMAC-signed with the operation bound into the MAC, so an upload URL cannot be
replayed as a download and a leaked URL grants one artifact for one TTL window.

Uploads are three-phase (declare, append, complete) and resumable: size and
SHA-256 are committed before the first byte, bytes land in ``{id}.part`` so an
incomplete upload can never be mistaken for a ready artifact, and the digest is
verified before the file is promoted.
"""
from __future__ import annotations

import hashlib
import shutil
import tempfile
import time
import uuid
from datetime import timedelta
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response
from starlette.routing import Route

from .auth import ArtifactSigner
from .db import Database
from .models import ArtifactRecord, ArtifactState, utc_now
from .workspace import Workspace

#: Read/write chunk for hashing and streaming; bounded so a 20 GiB artifact
#: never lands in memory.
CHUNK = 1024 * 1024


class ArtifactService:
    def __init__(
        self,
        root: Path,
        db: Database,
        workspace: Workspace,
        signer: ArtifactSigner,
        max_upload_bytes: int,
        retention_days: int,
        incomplete_upload_hours: int,
    ) -> None:
        self.root = root.resolve()
        self.db = db
        self.workspace = workspace
        self.signer = signer
        self.max_upload_bytes = max_upload_bytes
        self.retention_days = retention_days
        self.incomplete_upload_hours = incomplete_upload_hours
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- upload

    async def create_upload(
        self,
        logical_name: str,
        expected_size: int,
        expected_sha256: str,
        media_type: str = "application/octet-stream",
    ) -> ArtifactRecord:
        if expected_size < 0 or expected_size > self.max_upload_bytes:
            raise ValueError("upload size exceeds configured limit")
        if len(expected_sha256) != 64:
            raise ValueError("expected_sha256 must be a SHA-256 hex digest")
        try:
            int(expected_sha256, 16)
        except ValueError as error:
            raise ValueError(
                "expected_sha256 must be a SHA-256 hex digest"
            ) from error
        # The client names the file; treat that name as hostile input.
        safe_name = Path(logical_name).name
        if not safe_name or safe_name in {".", ".."}:
            raise ValueError("logical_name is not a usable file name")

        artifact_id = str(uuid.uuid4())
        record = ArtifactRecord(
            id=artifact_id,
            logical_name=safe_name,
            state=ArtifactState.INCOMPLETE,
            expected_size=expected_size,
            expected_sha256=expected_sha256.lower(),
            media_type=media_type,
            storage_path=str(self.root / f"{artifact_id}.part"),
        )
        await self.db.upsert_artifact(record)
        return record

    async def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        record = await self.db.get_artifact(artifact_id)
        if record is None:
            raise KeyError(artifact_id)
        return record

    async def append_upload(
        self, artifact_id: str, offset: int, chunk: bytes
    ) -> ArtifactRecord:
        record = await self.get_artifact(artifact_id)
        if record.state is not ArtifactState.INCOMPLETE:
            raise ValueError("artifact is not uploadable")
        # Strict offset matching makes a resumed upload deterministic and stops
        # concurrent PUTs from interleaving into a corrupt file.
        if offset != record.committed_size:
            raise ValueError(
                f"offset mismatch; expected {record.committed_size}"
            )
        if record.committed_size + len(chunk) > record.expected_size:
            raise ValueError("chunk exceeds declared upload size")
        path = Path(record.storage_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as output:
            output.write(chunk)
        record.committed_size += len(chunk)
        await self.db.upsert_artifact(record)
        return record

    async def complete_upload(self, artifact_id: str) -> ArtifactRecord:
        record = await self.get_artifact(artifact_id)
        if record.committed_size != record.expected_size:
            raise ValueError("upload is incomplete")
        digest = hashlib.sha256()
        with Path(record.storage_path).open("rb") as stream:
            for block in iter(lambda: stream.read(CHUNK), b""):
                digest.update(block)
        actual = digest.hexdigest()
        if actual != record.expected_sha256:
            raise ValueError("artifact hash mismatch")

        final_path = self.root / artifact_id / record.logical_name
        final_path.parent.mkdir(parents=True, exist_ok=True)
        Path(record.storage_path).replace(final_path)
        record.storage_path = str(final_path)
        if final_path.suffix.casefold() == ".zip":
            # Engine inputs arrive as archives: a shapefile is 4+ sidecar files
            # and an OSGB dataset is a directory tree.
            content_root = self.root / artifact_id / "content"
            self.workspace.extract_zip(final_path, content_root)
            record.content_root = str(content_root)
        else:
            record.content_root = str(final_path.parent)
        record.actual_sha256 = actual
        record.state = ArtifactState.READY
        record.completed_at = utc_now()
        await self.db.upsert_artifact(record)
        return record

    async def resolve_ready_path(
        self, artifact_id: str, relative_path: str
    ) -> Path:
        """Map an artifact-relative path to a real one inside the sandbox."""
        record = await self.get_artifact(artifact_id)
        if record.state is not ArtifactState.READY or not record.content_root:
            raise ValueError("artifact is not ready")
        resolved = self.workspace.resolve_relative(
            Path(record.content_root), relative_path
        )
        if not resolved.exists():
            raise ValueError("artifact-relative path does not exist")
        return resolved

    # ------------------------------------------------------------ outputs

    async def register_output(
        self,
        output_path: Path,
        logical_name: str | None = None,
        media_type: str = "application/octet-stream",
    ) -> ArtifactRecord:
        """Publish an Engine output as a downloadable artifact.

        Engine writes a directory (a ``.3dt`` tile plus ``DataInfor.txt``, and
        for OSGB a whole pyramid), so a directory is collapsed into a single ZIP
        rather than exposing a listing endpoint.
        """
        output_path = output_path.resolve()
        if not output_path.exists():
            raise ValueError(f"output path does not exist: {output_path.name}")

        if output_path.is_dir():
            base = self.root / str(uuid.uuid4()) / output_path.name
            base.parent.mkdir(parents=True, exist_ok=True)
            archived = Path(shutil.make_archive(str(base), "zip", output_path))
            source, name = archived, logical_name or f"{output_path.name}.zip"
            media_type = "application/zip"
        elif output_path.suffix.casefold() == ".3dt":
            # A .3dt is meaningless without the DataInfor.txt key beside it, so
            # ship the sibling set together.
            with tempfile.TemporaryDirectory(dir=self.root) as staging_name:
                staging = Path(staging_name)
                shutil.copy2(output_path, staging / output_path.name)
                key_file = output_path.parent / "DataInfor.txt"
                if key_file.is_file():
                    shutil.copy2(key_file, staging / key_file.name)
                base = self.root / str(uuid.uuid4()) / output_path.stem
                base.parent.mkdir(parents=True, exist_ok=True)
                archived = Path(shutil.make_archive(str(base), "zip", staging))
            source = archived
            name = logical_name or f"{output_path.stem}.zip"
            media_type = "application/zip"
        else:
            source, name = output_path, logical_name or output_path.name

        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as stream:
            for block in iter(lambda: stream.read(CHUNK), b""):
                digest.update(block)
                size += len(block)

        artifact_id = str(uuid.uuid4())
        final_path = self.root / artifact_id / name
        final_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, final_path)

        record = ArtifactRecord(
            id=artifact_id,
            logical_name=name,
            state=ArtifactState.READY,
            expected_size=size,
            expected_sha256=digest.hexdigest(),
            committed_size=size,
            actual_sha256=digest.hexdigest(),
            media_type=media_type,
            storage_path=str(final_path),
            content_root=str(final_path.parent),
            completed_at=utc_now(),
        )
        await self.db.upsert_artifact(record)
        return record

    # ---------------------------------------------------------- lifecycle

    async def list_artifacts(self) -> list[ArtifactRecord]:
        return await self.db.list_artifacts()

    async def delete_artifact(self, artifact_id: str) -> ArtifactRecord:
        record = await self.get_artifact(artifact_id)
        artifact_root = self.root / artifact_id
        if artifact_root.exists():
            shutil.rmtree(artifact_root, ignore_errors=True)
        if record.storage_path:
            stored = Path(record.storage_path)
            if stored.is_file():
                stored.unlink(missing_ok=True)
        # Hard on disk, soft in the database: the row remains for audit.
        record.state = ArtifactState.DELETED
        await self.db.upsert_artifact(record)
        return record

    async def cleanup_expired(self) -> list[str]:
        now = utc_now()
        cutoff = now - timedelta(days=self.retention_days)
        incomplete_cutoff = now - timedelta(hours=self.incomplete_upload_hours)
        deleted: list[str] = []
        for record in await self.list_artifacts():
            if record.state is ArtifactState.INCOMPLETE:
                if record.created_at < incomplete_cutoff:
                    await self.delete_artifact(record.id)
                    deleted.append(record.id)
                continue
            reference = record.completed_at or record.created_at
            if reference >= cutoff:
                continue
            if await self.db.artifact_has_active_job(record.id):
                continue
            await self.delete_artifact(record.id)
            deleted.append(record.id)
        return deleted

    # -------------------------------------------------------- signed URLs

    def signed_url(
        self, base_url: str, artifact_id: str, operation: str, ttl: int
    ) -> str:
        expires = int(time.time()) + ttl
        signature = self.signer.sign(operation, artifact_id, expires)
        return (
            f"{base_url.rstrip('/')}/artifacts/{artifact_id}/content"
            f"?op={operation}&expires={expires}&signature={signature}"
        )


# ------------------------------------------------------------------ routes

def _verified(
    request: Request, expected_operation: str
) -> tuple[ArtifactService, str] | Response:
    """Validate a signed URL, or return the 403 to send back."""
    service: ArtifactService = request.app.state.artifact_service
    artifact_id = request.path_params["artifact_id"]
    operation = request.query_params.get("op", "")
    signature = request.query_params.get("signature", "")
    try:
        expires_at = int(request.query_params.get("expires", "0"))
    except ValueError:
        return PlainTextResponse("invalid expiry", status_code=403)
    # The operation must match the route as well as the MAC, so an upload URL
    # cannot be replayed against the download route.
    if operation != expected_operation or not service.signer.verify(
        operation, artifact_id, expires_at, signature
    ):
        return PlainTextResponse("invalid signature", status_code=403)
    return service, artifact_id


async def upload_content(request: Request) -> Response:
    verified = _verified(request, "upload")
    if isinstance(verified, Response):
        return verified
    service, artifact_id = verified
    # The header is the documented interface; the query parameter is accepted
    # too because it survives clients that strip unknown headers. Offset is NOT
    # part of the signed payload, so a resumed upload reuses the same URL.
    raw_offset = request.headers.get("x-upload-offset")
    if raw_offset is None:
        raw_offset = request.query_params.get("offset", "0")
    try:
        offset = int(raw_offset)
    except ValueError:
        return PlainTextResponse("invalid offset", status_code=400)
    if offset < 0:
        return PlainTextResponse("invalid offset", status_code=400)

    try:
        record = await service.get_artifact(artifact_id)
    except KeyError:
        return PlainTextResponse("unknown artifact", status_code=404)
    if record.state is not ArtifactState.INCOMPLETE:
        return PlainTextResponse("artifact is not uploadable", status_code=409)
    # Check the offset before reading the body. append_upload would catch a
    # mismatch too, but not for an empty PUT, which never enters the loop — and a
    # silently accepted wrong offset is how a resumed upload corrupts a file.
    if offset != record.committed_size:
        return PlainTextResponse(
            f"offset mismatch; expected {record.committed_size}",
            status_code=409,
        )

    # Stream the body so a large PUT is not buffered whole in memory.
    written = offset
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            record = await service.append_upload(artifact_id, written, chunk)
            written += len(chunk)
    except ValueError as error:
        return PlainTextResponse(str(error), status_code=400)

    return PlainTextResponse(
        f"{record.committed_size}", status_code=204
        if record.committed_size == record.expected_size else 202,
    )


async def download_content(request: Request) -> Response:
    verified = _verified(request, "download")
    if isinstance(verified, Response):
        return verified
    service, artifact_id = verified
    try:
        record = await service.get_artifact(artifact_id)
    except KeyError:
        return PlainTextResponse("unknown artifact", status_code=404)
    if record.state is not ArtifactState.READY or not record.storage_path:
        return PlainTextResponse("artifact is not ready", status_code=409)
    path = Path(record.storage_path)
    if not path.is_file():
        return PlainTextResponse("artifact content is missing", status_code=410)
    return FileResponse(
        path, media_type=record.media_type, filename=record.logical_name
    )


def artifact_routes() -> list[Route]:
    return [
        Route(
            "/artifacts/{artifact_id}/content",
            upload_content,
            methods=["PUT"],
        ),
        Route(
            "/artifacts/{artifact_id}/content",
            download_content,
            methods=["GET", "HEAD"],
        ),
    ]
