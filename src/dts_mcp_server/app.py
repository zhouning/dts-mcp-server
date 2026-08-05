"""Application assembly.

The whole two-plane trick is here: ``streamable_http_app()`` produces a Starlette
app with MCP mounted at ``/mcp``, then the artifact transfer routes are appended
to that same router. One port, one TLS listener, one firewall rule — two
protocols.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import __version__
from .artifacts import ArtifactService, artifact_routes
from .auth import ArtifactSigner, StaticBearerVerifier
from .config import Settings
from .db import Database
from .engine import build_runner
from .jobs import JobService
from .mcp_tools import register_tools
from .workspace import Workspace

#: How often expired artifacts are swept.
CLEANUP_INTERVAL_SECONDS = 3600


@dataclass
class Services:
    db: Database
    workspace: Workspace
    artifacts: ArtifactService
    jobs: JobService


def build_services(settings: Settings) -> Services:
    settings.ensure_directories()
    db = Database(settings.database_path)
    workspace = Workspace(
        root=settings.workspace_dir,
        max_expanded_bytes=settings.max_expanded_bytes,
        max_zip_entries=settings.max_zip_entries,
        max_zip_depth=settings.max_zip_depth,
    )
    artifacts = ArtifactService(
        root=settings.artifact_dir,
        db=db,
        workspace=workspace,
        signer=ArtifactSigner(
            settings.artifact_signing_key.get_secret_value().encode("utf-8")
        ),
        max_upload_bytes=settings.max_upload_bytes,
        retention_days=settings.retention_days,
        incomplete_upload_hours=settings.incomplete_upload_hours,
    )
    jobs = JobService(
        db=db,
        workspace=workspace,
        runner=build_runner(settings.engine_dir),
        timeout_seconds=settings.job_timeout_seconds,
    )
    return Services(db=db, workspace=workspace, artifacts=artifacts, jobs=jobs)


def create_mcp(settings: Settings, services: Services) -> MCPServer:
    mcp = MCPServer(
        "dts-engine",
        version=__version__,
        description=(
            "Publish 3D digital-twin tiles with DTS Engine (飞渡科技 / Freedo)."
        ),
        instructions=(
            "Remote gateway to a Windows DTS Engine install. Call dts_ping "
            "first, then dts_list_pipelines. File bytes never travel over MCP: "
            "upload inputs with dts_create_upload then a plain HTTPS PUT to the "
            "returned signed URL, and collect results with dts_download_output "
            "then a GET. Do not send the bearer token to signed URLs. Path "
            "flags take {\"artifact\": id, \"path\": relative} rather than a "
            "host path. Publishing is asynchronous — poll dts_get_job. Input "
            "shapefiles must use a projected CRS, not lat/lon. Only the `road` "
            "pipeline is verified end to end."
        ),
        token_verifier=StaticBearerVerifier(
            settings.bearer_token.get_secret_value()
        ),
        # MCP 2.0 requires auth settings alongside a token verifier. There is no
        # OAuth server here — the static bearer is the whole scheme — so issuer
        # and resource both point at this gateway.
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(settings.base_url),
            resource_server_url=AnyHttpUrl(settings.base_url),
        ),
    )
    register_tools(mcp, settings, services.artifacts, services.jobs)
    return mcp


async def _cleanup_loop(services: Services) -> None:
    while True:
        try:
            await services.artifacts.cleanup_expired()
        except Exception as error:  # a sweep failure must not kill the task
            print(f"artifact cleanup failed: {error}", flush=True)
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


async def healthz(request: Request) -> JSONResponse:
    """Unauthenticated liveness probe.

    Deliberately leaks nothing but status and version, so deployment scripts can
    verify TLS and reachability without holding the bearer token.
    """
    return JSONResponse({"status": "ok", "version": __version__})


def create_application(settings: Settings) -> Starlette:
    services = build_services(settings)
    mcp = create_mcp(settings, services)

    # DNS-rebinding protection is on by default and rejects any Host header not
    # listed here with 421. It must be configured rather than disabled: without
    # it, a malicious page could make a browser on the client's network POST to
    # this gateway. Both the bare host and host:port forms are accepted because
    # clients differ in whether they include the port.
    host = settings.server_ip or "127.0.0.1"
    transport_security = TransportSecuritySettings(
        allowed_hosts=[host, f"{host}:{settings.bind_port}"],
        allowed_origins=[
            settings.base_url,
            f"https://{host}",
            f"https://{host}:{settings.bind_port}",
        ],
    )

    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=transport_security,
        # A single JSON body rather than an SSE stream: simpler for clients and
        # for curl-based diagnostics.
        json_response=True,
        # No server-side session affinity, so a gateway restart does not
        # invalidate a client's connection. Job continuity rides on job IDs.
        stateless_http=True,
    )
    app.state.services = services
    app.state.artifact_service = services.artifacts

    app.routes.extend(
        [Route("/healthz", healthz, methods=["GET"])] + artifact_routes()
    )

    original_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(scope_app):
        await services.db.connect()
        await services.artifacts.cleanup_expired()
        await services.jobs.start()
        cleanup = asyncio.create_task(_cleanup_loop(services))
        try:
            async with original_lifespan(scope_app):
                yield
        finally:
            cleanup.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup
            await services.jobs.stop()
            await services.db.close()

    app.router.lifespan_context = lifespan
    return app
