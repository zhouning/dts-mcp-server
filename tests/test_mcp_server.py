"""Tests for the MCP layer.

These exercise the server's tool registry and the tool functions directly.
Tools that do not touch Engine are safe to call anywhere; the ones that publish
are covered by the E2E tier.
"""
from __future__ import annotations

from dts_mcp_server import __version__
from dts_mcp_server.server import (
    dts_create_job,
    dts_explain_error,
    dts_job_types,
    dts_list_pipelines,
    dts_ping,
    dts_validate,
    mcp,
)

#: Every tool the server is contracted to expose.
EXPECTED_TOOLS = {
    "dts_ping",
    "dts_list_pipelines",
    "dts_explain_error",
    "dts_validate",
    "dts_publish",
    "dts_publish_osgb",
    "dts_create_job",
    "dts_job_types",
}


def _call(tool):
    """Unwrap an MCPServer-decorated tool so it can be invoked directly."""
    return getattr(tool, "fn", tool)


def test_version_exported():
    assert __version__ == "0.1.0"


async def test_all_tools_registered():
    tools = await mcp.list_tools()
    assert {t.name for t in tools} == EXPECTED_TOOLS


async def test_every_tool_has_a_description():
    """Descriptions are the agent's only guidance; none may be blank."""
    for t in await mcp.list_tools():
        assert t.description and t.description.strip(), f"{t.name} has no description"


async def test_publish_tool_documents_the_dom_trap():
    """The undocumented domPath requirement must reach the agent."""
    tools = {t.name: t for t in await mcp.list_tools()}
    desc = tools["dts_publish"].description
    assert "domPath" in desc
    assert "PROJECTED" in desc or "projected" in desc


async def test_publish_osgb_tool_documents_two_stages():
    tools = {t.name: t for t in await mcp.list_tools()}
    assert "two" in tools["dts_publish_osgb"].description.lower()


async def test_publish_tool_schema_accepts_flag_mapping():
    tools = {t.name: t for t in await mcp.list_tools()}
    props = tools["dts_publish"].input_schema["properties"]
    assert "pipeline" in props and "outpath" in props and "flags" in props


# ------------------------------------------------- tools that need no Engine

def test_list_pipelines_reports_all_nine():
    out = _call(dts_list_pipelines)()
    assert out["ok"] is True
    assert len(out["pipelines"]) == 9
    verified = [p["name"] for p in out["pipelines"] if p["verified"]]
    assert verified == ["road"]


def test_list_pipelines_includes_source_citations():
    for p in _call(dts_list_pipelines)()["pipelines"]:
        assert p["source"], f"{p['name']} lacks a source citation"


def test_explain_error_known_code():
    out = _call(dts_explain_error)(201)
    assert out["ok"] is True
    assert out["band"] == "terrain"
    assert out["code"] == 201


def test_explain_error_separates_lookup_from_verdict():
    """`ok` is "lookup worked"; `indicates_success` is "the code means success".

    Conflating them made a successful lookup of a failure code look like a
    failed tool call.
    """
    failure = _call(dts_explain_error)(201)
    assert failure["ok"] is True
    assert failure["indicates_success"] is False

    success = _call(dts_explain_error)(0)
    assert success["ok"] is True
    assert success["indicates_success"] is True


def test_explain_error_normalizes_enginemaster_return():
    assert _call(dts_explain_error)(4294967295)["code"] == -1


def test_validate_catches_missing_required_flag():
    out = _call(dts_validate)("road", {"roadShp": "a.shp", "outpath": "o"})
    assert out["ok"] is False
    assert any("domPath" in p for p in out["problems"])


def test_validate_passes_complete_flag_set():
    out = _call(dts_validate)(
        "road", {"roadShp": "a.shp", "outpath": "o", "domPath": "d.tif"}
    )
    assert out["ok"] is True
    assert out["problems"] == []


def test_validate_unknown_pipeline_lists_known():
    out = _call(dts_validate)("nope", {})
    assert out["ok"] is False
    assert "road" in out["known"]


def test_job_types_lists_ten_types():
    out = _call(dts_job_types)()
    assert out["ok"] is True
    assert len(out["types"]) == 10
    by_name = {t["data_type"]: t for t in out["types"]}
    assert by_name["Vector"]["ordinal"] == 6
    # Untranscribed schemas report null rather than an empty guess.
    assert by_name["BIM"]["fields"] is None


def test_create_job_writes_file(tmp_path):
    out = _call(dts_create_job)(
        "Vector", str(tmp_path / "j.aconf"), {"RoadShpPath": "r.shp"}
    )
    assert out["ok"] is True
    assert out["job"]["Type"] == 6
    assert out["job"]["RoadShpPath"] == "r.shp"


def test_create_job_rejects_unknown_type(tmp_path):
    out = _call(dts_create_job)("Bogus", str(tmp_path / "j.aconf"))
    assert out["ok"] is False
    assert "Vector" in out["known"]


# ------------------------------------------------------------ needs Engine

def test_ping_finds_the_install():
    """Requires a real install, per the no-graceful-degradation rule."""
    out = _call(dts_ping)()
    assert out["ok"] is True, out.get("error")
    assert out["version"].startswith("6.")
    assert out["executables"]["EngineWorker.exe"] is True
