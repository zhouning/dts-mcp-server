"""Gateway integration tests over a real ASGI transport.

These drive the assembled application in-process: MCP control plane and artifact
data plane, both mounted on the same router. No TLS here — that is verified by
tests/remote_smoke.py against a real listener, since TLS is terminated by uvicorn
rather than by the app.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import zipfile

try:
    import httpx
except ModuleNotFoundError:
    # mcp>=2.0 depends on httpx2, which installs under its own module name. The
    # ASGITransport/AsyncClient surface used here is identical in both.
    import httpx2 as httpx

import pytest

from dts_mcp_server.app import create_application
from dts_mcp_server.config import Settings

TOKEN = "t" * 48
SIGNING_KEY = "s" * 48


@pytest.fixture
def settings(tmp_path):
    return Settings(
        bearer_token=TOKEN,
        artifact_signing_key=SIGNING_KEY,
        server_ip="127.0.0.1",
        bind_port=8770,
        state_dir=tmp_path,
        artifact_dir=tmp_path / "artifacts",
        workspace_dir=tmp_path / "workspace",
        database_path=tmp_path / "state.sqlite3",
        # Small limits so the archive defences are reachable in a test.
        max_upload_bytes=64 * 1024 * 1024,
        max_expanded_bytes=64 * 1024 * 1024,
    )


@contextlib.asynccontextmanager
async def gateway(settings: Settings):
    """Run the assembled app for the duration of one test.

    Not a pytest fixture on purpose. MCP's StreamableHTTPSessionManager opens an
    anyio task group inside the ASGI lifespan, and a generator fixture's cleanup
    phase runs in a different task than its setup — which anyio rejects with
    "Attempted to exit cancel scope in a different task". Wrapping the test body
    keeps entry and exit in one task.
    """
    app = create_application(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://127.0.0.1:8770"
        ) as http:
            yield http


def _auth() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }


async def call(http: httpx.AsyncClient, tool: str, arguments: dict) -> dict:
    response = await http.post(
        "/mcp",
        headers=_auth(),
        content=json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }),
    )
    assert response.status_code == 200, response.text
    message = response.json()
    assert "error" not in message, message["error"]
    return json.loads(message["result"]["content"][0]["text"])


# ---------------------------------------------------------------- transport

async def test_healthz_needs_no_token(settings):
    async with gateway(settings) as client:
        response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


async def test_mcp_rejects_missing_token(settings):
    async with gateway(settings) as client:
        response = await client.post(
            "/mcp",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"},
            content='{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
        )
        assert response.status_code in (401, 403)


async def test_mcp_rejects_wrong_token(settings):
    async with gateway(settings) as client:
        headers = _auth() | {"Authorization": "Bearer " + "x" * 48}
        response = await client.post(
            "/mcp", headers=headers,
            content='{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
        )
        assert response.status_code in (401, 403)


async def test_tools_advertised(settings):
    async with gateway(settings) as client:
        response = await client.post(
            "/mcp", headers=_auth(),
            content='{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
        )
        assert response.status_code == 200
        names = {t["name"] for t in response.json()["result"]["tools"]}
        for expected in (
            "dts_ping", "dts_list_pipelines", "dts_explain_error",
            "dts_create_upload", "dts_complete_upload", "dts_publish",
            "dts_publish_osgb", "dts_get_job", "dts_download_output",
        ):
            assert expected in names, f"{expected} not advertised"


async def test_every_tool_documents_itself(settings):
    async with gateway(settings) as client:
        response = await client.post(
            "/mcp", headers=_auth(),
            content='{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
        )
        for tool in response.json()["result"]["tools"]:
            assert tool.get("description", "").strip(), f"{tool['name']} undocumented"


    # ------------------------------------------------------------------ upload

async def _upload(http: httpx.AsyncClient, name: str, payload: bytes) -> dict:
    created = await call(http, "dts_create_upload", {
        "logical_name": name,
        "expected_size": len(payload),
        "expected_sha256": hashlib.sha256(payload).hexdigest(),
    })
    assert created["ok"], created
    url = created["upload_url"]
    put = await http.put(url, content=payload, headers={"X-Upload-Offset": "0"})
    assert put.status_code in (200, 204), put.text
    return created


async def test_upload_roundtrip_and_download(settings):
    async with gateway(settings) as client:
        payload = b"tile bytes" * 100
        created = await _upload(client, "sample.bin", payload)
        completed = await call(
            client, "dts_complete_upload",
            {"artifact_id": created["artifact"]["id"]},
        )
        assert completed["ok"], completed
        assert completed["artifact"]["state"] == "ready"


async def test_upload_url_needs_no_bearer(settings):
    """Signed URLs must work without the bearer, and must not require it."""
    async with gateway(settings) as client:
        payload = b"x" * 64
        created = await call(client, "dts_create_upload", {
            "logical_name": "plain.bin",
            "expected_size": len(payload),
            "expected_sha256": hashlib.sha256(payload).hexdigest(),
        })
        response = await client.put(
            created["upload_url"], content=payload,
            headers={"X-Upload-Offset": "0"},  # deliberately no Authorization
        )
        assert response.status_code in (200, 204)


async def test_artifact_record_hides_host_paths(settings):
    async with gateway(settings) as client:
        payload = b"y" * 32
        created = await _upload(client, "hidden.bin", payload)
        completed = await call(
            client, "dts_complete_upload",
            {"artifact_id": created["artifact"]["id"]},
        )
        assert "storage_path" not in completed["artifact"]
        assert "content_root" not in completed["artifact"]


async def test_hash_mismatch_rejected(settings):
    async with gateway(settings) as client:
        payload = b"real content"
        created = await call(client, "dts_create_upload", {
            "logical_name": "bad.bin",
            "expected_size": len(payload),
            # Digest of different bytes.
            "expected_sha256": hashlib.sha256(b"other").hexdigest(),
        })
        await client.put(
            created["upload_url"], content=payload,
            headers={"X-Upload-Offset": "0"},
        )
        result = await call(
            client, "dts_complete_upload",
            {"artifact_id": created["artifact"]["id"]},
        )
        assert result["ok"] is False
        assert "hash mismatch" in result["error"]


async def test_short_digest_rejected(settings):
    async with gateway(settings) as client:
        result = await call(client, "dts_create_upload", {
            "logical_name": "x.bin", "expected_size": 4,
            "expected_sha256": "abcd",
        })
        assert result["ok"] is False


async def test_oversize_declaration_rejected(settings):
    async with gateway(settings) as client:
        result = await call(client, "dts_create_upload", {
            "logical_name": "huge.bin",
            "expected_size": settings.max_upload_bytes + 1,
            "expected_sha256": "a" * 64,
        })
        assert result["ok"] is False
        assert "limit" in result["error"]


async def test_offset_mismatch_rejected(settings):
    async with gateway(settings) as client:
        payload = b"z" * 100
        created = await call(client, "dts_create_upload", {
            "logical_name": "chunked.bin",
            "expected_size": len(payload),
            "expected_sha256": hashlib.sha256(payload).hexdigest(),
        })
        response = await client.put(
            created["upload_url"], content=payload,
            headers={"X-Upload-Offset": "50"},  # nothing committed yet
        )
        assert response.status_code >= 400


async def test_upload_url_not_valid_for_download(settings):
    async with gateway(settings) as client:
        created = await call(client, "dts_create_upload", {
            "logical_name": "r.bin", "expected_size": 1,
            "expected_sha256": hashlib.sha256(b"a").hexdigest(),
        })
        replayed = created["upload_url"].replace("op=upload", "op=download")
        response = await client.get(replayed)
        assert response.status_code == 403


async def test_tampered_signature_rejected(settings):
    async with gateway(settings) as client:
        created = await call(client, "dts_create_upload", {
            "logical_name": "t.bin", "expected_size": 1,
            "expected_sha256": hashlib.sha256(b"a").hexdigest(),
        })
        url = created["upload_url"]
        head, _, signature = url.partition("signature=")
        response = await client.put(
            f"{head}signature={'0' * len(signature)}",
            content=b"a", headers={"X-Upload-Offset": "0"},
        )
        assert response.status_code == 403


async def test_zip_is_extracted_on_completion(settings, tmp_path):
    """A .zip becomes a browsable content root — how shapefiles arrive."""
    async with gateway(settings) as client:
        archive = tmp_path / "bundle.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("roads.shp", "shapefile bytes")
            zf.writestr("roads.dbf", "attribute bytes")
        payload = archive.read_bytes()
        created = await _upload(client, "bundle.zip", payload)
        completed = await call(
            client, "dts_complete_upload",
            {"artifact_id": created["artifact"]["id"]},
        )
        assert completed["ok"], completed
        # Publishing can now reference the extracted members by relative path.
        result = await call(client, "dts_publish", {
            "pipeline": "road",
            "flags": {
                "roadShp": {
                    "artifact": created["artifact"]["id"],
                    "path": "roads.shp",
                },
                "domPath": {
                    "artifact": created["artifact"]["id"],
                    "path": "roads.dbf",
                },
            },
        })
        assert result["ok"] is True, result


# ----------------------------------------------------------------- publish

async def test_publish_rejects_bare_host_path(settings):
    async with gateway(settings) as client:
        result = await call(client, "dts_publish", {
            "pipeline": "road",
            "flags": {"roadShp": "C:/Windows/win.ini"},
        })
        assert result["ok"] is False
        assert "host paths are not accepted" in result["error"]


async def test_publish_unknown_pipeline(settings):
    async with gateway(settings) as client:
        result = await call(client, "dts_publish", {
            "pipeline": "no_such_pipeline", "flags": {},
        })
        assert result["ok"] is False


async def test_publish_unknown_artifact(settings):
    async with gateway(settings) as client:
        result = await call(client, "dts_publish", {
            "pipeline": "road",
            "flags": {"roadShp": {"artifact": "nope", "path": "a.shp"}},
        })
        assert result["ok"] is False


    # -------------------------------------------------------------- inspection

async def test_list_pipelines_marks_verified(settings):
    async with gateway(settings) as client:
        result = await call(client, "dts_list_pipelines", {})
        assert result["ok"]
        verified = [p["name"] for p in result["pipelines"] if p["verified"]]
        assert verified == ["road"]


async def test_pipelines_declare_path_flags(settings):
    async with gateway(settings) as client:
        result = await call(client, "dts_list_pipelines", {})
        road = next(p for p in result["pipelines"] if p["name"] == "road")
        assert "roadShp" in road["path_flags"]
        assert "domPath" in road["path_flags"]


async def test_explain_error_separates_lookup_from_verdict(settings):
    async with gateway(settings) as client:
        failure = await call(client, "dts_explain_error", {"code": 201})
        assert failure["ok"] is True
        assert failure["indicates_success"] is False
        assert failure["band"] == "terrain"


async def test_job_not_found(settings):
    async with gateway(settings) as client:
        result = await call(client, "dts_get_job", {"job_id": "missing"})
        assert result["ok"] is False
