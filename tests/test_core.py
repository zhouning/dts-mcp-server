"""Unit tests — pure, no DTS Engine install required."""
from __future__ import annotations

import json
import socket

import pytest

from cli_anything.dts_engine.core import errors as errmod
from cli_anything.dts_engine.core import job as jobmod
from cli_anything.dts_engine.core import pipelines
from cli_anything.dts_engine.core.install import EngineNotFound, find_engine_dir
from cli_anything.dts_engine.utils import udp_log
from cli_anything.dts_engine.utils.engine_backend import build_args, collect_outputs

# --------------------------------------------------------------- errors

def test_parse_table_synthetic(tmp_path):
    f = tmp_path / "ErrorCode_en.txt"
    f.write_text("0---Correct\n\n1---Parameter Error\ngarbage line\n65535---Undefined\n")
    table = errmod.parse_table(f)
    assert table == {0: "Correct", 1: "Parameter Error", 65535: "Undefined"}


def test_parse_table_ignores_malformed(tmp_path):
    f = tmp_path / "e.txt"
    f.write_text("notanumber---msg\n5---ok\n---\n")
    assert errmod.parse_table(f) == {5: "ok"}


@pytest.mark.parametrize(
    "code,band",
    [
        (0, "io"), (19, "io"), (100, "lod"), (119, "lod"),
        (200, "terrain"), (249, "terrain"), (302, "resource"),
        (400, "crs"), (500, "vtpk"), (600, "dem_opt"),
        (65535, "unknown"), (99, "unknown"),
    ],
)
def test_band_of(code, band):
    assert errmod.band_of(code) == band


def test_describe_ok():
    d = errmod.describe(0, {0: "Correct"})
    assert d["ok"] is True and d["code"] == 0


def test_describe_maps_band_and_message():
    d = errmod.describe(201, {201: "The DOM path is empty"})
    assert d["band"] == "terrain"
    assert d["message"] == "The DOM path is empty"
    assert d["ok"] is False


def test_describe_normalizes_enginemaster_unsigned_minus_one():
    """EngineMaster.exe returns -1 as 0xFFFFFFFF instead of the child's code."""
    assert errmod.describe(4294967295)["code"] == -1


def test_describe_unmapped_code():
    assert "Unmapped" in errmod.describe(31337, {})["message"]


def test_load_table_falls_back_without_engine_dir():
    assert errmod.load_table(None) == errmod.FALLBACK


def test_load_table_falls_back_on_missing_file(tmp_path):
    assert errmod.load_table(tmp_path) == errmod.FALLBACK


# ------------------------------------------------------------ pipelines

def test_all_pipelines_retrievable():
    # All 9 pipelines carry a handleType; terrain is `gdal` (live-captured 7.0).
    assert len(pipelines.PIPELINES) == 9
    assert sum(1 for p in pipelines.PIPELINES.values() if p.handle_type) == 9
    for name in pipelines.PIPELINES:
        assert pipelines.get(name).name == name


def test_get_unknown_lists_known():
    with pytest.raises(KeyError, match="road"):
        pipelines.get("nope")


def test_validate_reports_missing_required():
    problems = pipelines.validate("road", {"roadShp": "a.shp", "outpath": "out"})
    assert problems == ["missing required flag: -domPath"]


def test_validate_reports_unknown_flag():
    problems = pipelines.validate(
        "road",
        {"roadShp": "a.shp", "outpath": "o", "domPath": "d.tif", "bogusFlag": 1},
    )
    assert any("bogusFlag" in p for p in problems)


def test_validate_clean_flagset():
    assert pipelines.validate(
        "road", {"roadShp": "a.shp", "outpath": "o", "domPath": "d.tif"}
    ) == []


def test_validate_accepts_common_flags():
    assert pipelines.validate("road", {
        "roadShp": "a.shp", "outpath": "o", "domPath": "d.tif",
        "inWkt": "EPSG:4326", "license_server": "host",
    }) == []


def test_validate_accepts_injected_flags():
    """handleType/udp/window/title are added by the harness, never user errors."""
    assert pipelines.validate("road", {
        "roadShp": "a.shp", "outpath": "o", "domPath": "d.tif",
        "handleType": "road", "udp": 9527, "window": "0,0,1,1", "title": "x",
    }) == []


def test_to_flags_injects_handle_type():
    assert pipelines.to_flags("road", {"outpath": "o"})["handleType"] == "road"


def test_to_flags_terrain_uses_gdal_handle_type():
    """terrain publishes via -handleType=gdal (captured from the 7.0 GUI)."""
    assert pipelines.to_flags("terrain", {"outpath": "o"})["handleType"] == "gdal"


def test_road_is_marked_verified():
    assert pipelines.get("road").verified is True


def test_every_pipeline_cites_its_source():
    for p in pipelines.PIPELINES.values():
        assert p.source, f"{p.name} has no source citation"


# ------------------------------------------------------------------ job

def test_new_job_applies_vector_defaults():
    j = jobmod.new_job("Vector")
    assert j["Type"] == 6
    assert j["ColorSource"] == 0
    assert j["RoadWidth"] == 0.0


def test_new_job_applies_osgb_defaults():
    j = jobmod.new_job("Osgb")
    assert j["Type"] == 1
    assert j["Collision"] is False
    assert j["ReductionTileNode"] is False


def test_new_job_unknown_type():
    with pytest.raises(KeyError):
        jobmod.new_job("Nonexistent")


def test_new_job_drops_none_fields():
    assert "FileName" not in jobmod.new_job("Vector", FileName=None)


def test_validate_job_flags_unknown_field():
    problems = jobmod.validate_job("Vector", {"Type": 6, "NotAField": 1})
    assert any("NotAField" in p for p in problems)


def test_validate_job_skips_untranscribed_schema():
    """Types whose C# schema was not transcribed must not be guessed at."""
    assert jobmod.validate_job("BIM", {"Anything": 1}) == []


def test_write_read_job_roundtrip(tmp_path):
    j = jobmod.new_job("Vector", RoadShpPath="D:/x/道路.shp", OutputPath="D:/out")
    p = jobmod.write_job(j, tmp_path / "sub" / "job.aconf")
    assert p.is_file()
    back = jobmod.read_job(p)
    assert back["RoadShpPath"] == "D:/x/道路.shp"
    assert back == j


def test_write_job_preserves_non_ascii(tmp_path):
    p = jobmod.write_job({"Name": "华能"}, tmp_path / "j.aconf")
    assert "华能" in p.read_text(encoding="utf-8")


def test_write_job_drops_none(tmp_path):
    p = jobmod.write_job({"A": 1, "B": None}, tmp_path / "j.aconf")
    assert json.loads(p.read_text(encoding="utf-8")) == {"A": 1}


def test_data_types_match_csharp_ordinals():
    assert jobmod.DATA_TYPES["Terrain"] == 0
    assert jobmod.DATA_TYPES["DTOModeData"] == 9


# -------------------------------------------------------------- udp_log

def test_decode_gbk():
    assert udp_log.decode("影像路径为空".encode("gbk")) == "影像路径为空"


def test_decode_never_raises():
    assert udp_log.decode(b"\xff\xfe\x00\x01") is not None


def test_parse_line_real_error_line():
    line = "[ERROR]2026-08-05 09:58:31 发布失败，RetCode:201,message:影像路径为空"
    rec = udp_log.parse_line(line)
    assert rec["level"] == "ERROR"
    assert rec["timestamp"] == "2026-08-05 09:58:31"
    assert rec["retcode"] == 201


def test_parse_line_strips_leading_control_byte():
    """Engine prefixes datagrams with 0x01; regression for a real captured frame."""
    rec = udp_log.parse_line("\x01[ERROR]2026-08-05 10:11:42 RetCode:201,message:x")
    assert rec["level"] == "ERROR"
    assert rec["retcode"] == 201
    assert not rec["body"].startswith("\x01")


def test_decode_strips_leading_control_byte():
    raw = b"\x01[ERROR]2026-08-05 10:11:42 " + "发布失败".encode("gbk")
    assert udp_log.decode(raw).startswith("[ERROR]")


def test_parse_line_plain():
    rec = udp_log.parse_line("just text")
    assert rec["level"] == "INFO" and rec["retcode"] is None


def test_listener_receives_datagram():
    with udp_log.UdpLogListener() as lg:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(
            "[ERROR]2026-08-05 09:58:31 失败，RetCode:302,message:x".encode("gbk"),
            ("127.0.0.1", lg.port),
        )
        s.close()
        import time
        time.sleep(1.0)
        assert lg.retcode == 302
        assert len(lg.errors()) == 1
    assert lg.port > 0


# ------------------------------------------------------------- backend

def test_build_args_renders_flags():
    assert build_args({"inpath": "a", "outpath": "b"}) == ["-inpath=a", "-outpath=b"]


def test_build_args_drops_none():
    assert build_args({"a": 1, "b": None}) == ["-a=1"]


def test_build_args_lowercases_booleans():
    assert build_args({"collision": True, "cull": False}) == [
        "-collision=true", "-cull=false"
    ]


def test_collect_outputs_missing_dir(tmp_path):
    assert collect_outputs(tmp_path / "nope") == []


def test_collect_outputs_reports_files(tmp_path):
    (tmp_path / "a.3dt").write_bytes(b"x" * 100)
    out = collect_outputs(tmp_path)
    assert out[0]["name"] == "a.3dt"
    assert out[0]["size"] == 100
    assert out[0]["suffix"] == ".3dt"


# ------------------------------------------------------------- install

def test_find_engine_dir_honours_explicit(tmp_path):
    (tmp_path / "EngineWorker.exe").write_bytes(b"stub")
    assert find_engine_dir(tmp_path) == tmp_path


def test_find_engine_dir_raises_with_tried_paths(tmp_path, monkeypatch):
    monkeypatch.delenv("DTS_ENGINE_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(EngineNotFound, match="DTS_ENGINE_DIR"):
        find_engine_dir(tmp_path / "empty")


def test_roaming_roots_covers_relocated_profile(monkeypatch):
    """A relocated user profile leaves %APPDATA% on C: while the real folder is
    on another drive; discovery must probe both."""
    from cli_anything.dts_engine.core.install import _roaming_roots

    monkeypatch.setenv("APPDATA", r"C:\Users\someone\AppData\Roaming")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\someone")
    roots = [str(p) for p in _roaming_roots()]
    assert r"C:\Users\someone\AppData\Roaming" in roots
    # The USERPROFILE-derived path is included even when it duplicates APPDATA,
    # because the two disagree on a redirected profile.
    assert any("AppData" in r for r in roots)


def test_find_engine_dir_discovers_without_env(monkeypatch):
    """Discovery must work with no DTS_ENGINE_DIR set (real install required)."""
    from cli_anything.dts_engine.core.install import find_engine_dir

    monkeypatch.delenv("DTS_ENGINE_DIR", raising=False)
    found = find_engine_dir()
    assert (found / "EngineWorker.exe").is_file()
