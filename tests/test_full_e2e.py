"""E2E tests — these invoke the real EngineWorker.exe.

Per the cli-anything SOP there is no graceful degradation: if DTS Engine is not
installed these tests FAIL rather than skip, because a harness that cannot drive
the software is not working.

Set DTS_ENGINE_DIR if the install is not in the standard location.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile

import pytest

from cli_anything.dts_engine.core import errors as errmod
from cli_anything.dts_engine.core.install import (
    KNOWN_MAJORS,
    describe_install,
    find_engine_dir,
)
from cli_anything.dts_engine.core.publish import publish

MIN_TILE_BYTES = 1_000_000


def _resolve_cli(name):
    """Resolve installed CLI command; falls back to python -m for dev."""
    force = os.environ.get("CLI_ANYTHING_FORCE_INSTALLED", "").strip() == "1"
    path = shutil.which(name)
    if path:
        print(f"[_resolve_cli] Using installed command: {path}")
        return [path]
    if force:
        raise RuntimeError(f"{name} not found in PATH. Install with: pip install -e .")
    module = "cli_anything.dts_engine.dts_engine_cli"
    print(f"[_resolve_cli] Falling back to: {sys.executable} -m {module}")
    return [sys.executable, "-m", module]


@pytest.fixture(scope="session")
def engine_dir():
    return find_engine_dir()


@pytest.fixture(scope="session")
def fixtures(tmp_path_factory):
    """A projected shapefile plus DOM/DEM rasters covering its extent.

    Engine rejects geographic coordinates (codes 203/209/215), so the source
    must be projected. Rasters are generated to match the shapefile's own bounds.

    Fixtures are generated, not copied from an installed dataset, so a fresh
    clone of this repo can run the E2E tier on any machine with Engine present.
    """
    geopandas = pytest.importorskip("geopandas")
    rasterio = pytest.importorskip("rasterio")
    numpy = pytest.importorskip("numpy")
    from rasterio.transform import from_bounds
    from shapely.geometry import Polygon

    d = tmp_path_factory.mktemp("dts_fixtures")

    # A small grid of square parcels in UTM 17N (projected — Engine rejects
    # geographic coordinates with 203/209/215). Origin is a plausible
    # easting/northing so the extent is realistic rather than near (0,0).
    x0, y0, size = 300_000.0, 4_100_000.0, 200.0
    polys, names = [], []
    for row in range(6):
        for col in range(6):
            ox, oy = x0 + col * size * 1.5, y0 + row * size * 1.5
            polys.append(Polygon([
                (ox, oy), (ox + size, oy),
                (ox + size, oy + size), (ox, oy + size),
            ]))
            names.append(f"parcel_{row}_{col}")
    g = geopandas.GeoDataFrame(
        {"name": names, "height": [10.0 + i for i in range(len(polys))]},
        geometry=polys,
        crs="EPSG:32617",
    )
    g.to_file(d / "vautm17n.shp")

    assert g.crs is not None and g.crs.is_projected, "fixture must be projected"
    minx, miny, maxx, maxy = g.total_bounds
    tr = from_bounds(minx, miny, maxx, maxy, 512, 512)

    dem = (numpy.random.RandomState(0).rand(512, 512).astype("float32") * 50 + 100)
    with rasterio.open(d / "dem.tif", "w", driver="GTiff", height=512, width=512,
                       count=1, dtype="float32", crs=g.crs, transform=tr) as f:
        f.write(dem, 1)
    dom = numpy.random.RandomState(1).randint(0, 255, (3, 512, 512)).astype("uint8")
    with rasterio.open(d / "dom.tif", "w", driver="GTiff", height=512, width=512,
                       count=3, dtype="uint8", crs=g.crs, transform=tr) as f:
        f.write(dom)

    return {
        "shp": str(d / "vautm17n.shp"),
        "dom": str(d / "dom.tif"),
        "dem": str(d / "dem.tif"),
    }


@pytest.fixture(scope="session")
def published(fixtures, tmp_path_factory):
    """Run the road pipeline once; several tests assert on the same artifacts."""
    out = tmp_path_factory.mktemp("dts_out")
    result = publish("road", {
        "roadShp": fixtures["shp"],
        "domPath": fixtures["dom"],
        "demPath": fixtures["dem"],
        "outpath": str(out),
        "roadRGB": "255,0,0",
        "roadSize": 5,
    }, timeout=900)
    return result, out


# ------------------------------------------------------------- environment

def test_install_discovered(engine_dir):
    d = describe_install(engine_dir)
    assert d["version"].split(".")[0] in KNOWN_MAJORS, d["version"]
    assert d["executables"]["EngineWorker.exe"] is True
    assert any("gdal" in g for g in d["gdal"]), "GDAL should be bundled"


def test_shipped_error_table_is_used(engine_dir):
    """The install's own table, not the 9-entry enum fallback."""
    table = errmod.load_table(engine_dir)
    assert len(table) > 50, "expected the shipped ~90-code table"
    assert table[201] == "The DOM path is empty"
    assert "tree" in table[302].lower()
    assert "GDAL" in table[6]


# ------------------------------------------------------------------ publish

def test_road_publish_succeeds(published):
    result, out = published
    assert result["exit_code"] == 0, (
        f"publish failed: {result.get('error')} log={result.get('log')}"
    )
    assert result["ok"] is True
    print(f"\n  elapsed: {result['elapsed_seconds']}s")
    for o in result["outputs"]:
        print(f"  artifact: {o['path']} ({o['size']:,} bytes)")


def test_3dt_artifact_verified(published):
    """Verify the output, do not trust the exit code alone."""
    result, out = published
    tiles = [o for o in result["outputs"] if o["suffix"] == ".3dt"]
    assert tiles, f"no .3dt produced; got {[o['name'] for o in result['outputs']]}"
    tile = tiles[0]
    assert tile["size"] > MIN_TILE_BYTES, f"tile suspiciously small: {tile['size']}"
    # Engine's own binary container: not a ZIP (3D Tiles/glTF-style) archive.
    assert not zipfile.is_zipfile(tile["path"])
    with open(tile["path"], "rb") as f:
        assert f.read(4) != b"glTF"


def test_datainfor_is_encryption_key(published):
    """DataInfor.txt holds the 32-hex key tied to -useGeneralEncryptKey."""
    result, out = published
    info = [o for o in result["outputs"] if o["name"] == "DataInfor.txt"]
    assert info, "DataInfor.txt not produced"
    text = open(info[0]["path"], encoding="utf-8").read().strip()
    assert re.fullmatch(r"[0-9A-Fa-f]{32}", text), f"unexpected content: {text!r}"


def test_output_totals_reported(published):
    result, _ = published
    assert result["output_count"] >= 2
    assert result["output_bytes"] > MIN_TILE_BYTES


# ------------------------------------------------------------------ failure

def test_missing_dom_fails_with_201(fixtures, tmp_path):
    """The live-observed failure mode; --no-strict lets it reach the Engine."""
    result = publish("road", {
        "roadShp": fixtures["shp"],
        "outpath": str(tmp_path / "o"),
    }, strict=False, timeout=300)
    assert result["exit_code"] == 201
    assert result["error"]["band"] == "terrain"
    assert result["error"]["message"] == "The DOM path is empty"


def test_udp_log_captured_on_failure(fixtures, tmp_path):
    result = publish("road", {
        "roadShp": fixtures["shp"],
        "outpath": str(tmp_path / "o2"),
    }, strict=False, timeout=300)
    errors = [r for r in result["log"] if r["level"] == "ERROR"]
    assert errors, f"no ERROR lines captured; log={result['log']}"
    assert errors[0]["body"]
    assert result["log_retcode"] == 201


def test_shp_pipeline_rejects_polygons(fixtures, tmp_path):
    """`shp` builds vegetation resources; it is not a general vector converter."""
    result = publish("shp", {
        "inpath": fixtures["shp"],
        "outpath": str(tmp_path / "o3"),
    }, timeout=300)
    assert result["exit_code"] == 302
    assert result["error"]["band"] == "resource"


def test_strict_validation_does_not_launch(fixtures, tmp_path):
    result = publish("road", {
        "roadShp": fixtures["shp"], "outpath": str(tmp_path / "o4"),
    }, strict=True)
    assert result["ok"] is False
    assert "missing required flag: -domPath" in result["validation_errors"]
    assert "exit_code" not in result, "strict mode must not start a process"


# -------------------------------------------------------- CLI subprocess

class TestCLISubprocess:
    CLI_BASE = _resolve_cli("cli-anything-dts-engine")

    def _run(self, args, check=True):
        return subprocess.run(
            self.CLI_BASE + args, capture_output=True, text=True, check=check
        )

    def test_help(self):
        assert self._run(["--help"]).returncode == 0

    def test_version(self):
        assert "1.0.0" in self._run(["--version"]).stdout

    def test_info_json(self):
        data = json.loads(self._run(["--json", "info"]).stdout)
        assert data["ok"] is True
        assert data["engine_dir"]

    def test_pipelines_json(self):
        data = json.loads(self._run(["--json", "pipelines"]).stdout)
        assert len(data) == 9
        assert next(p for p in data if p["name"] == "road")["verified"] is True

    def test_errors_json(self):
        data = json.loads(self._run(["--json", "errors", "--code", "201"]).stdout)
        assert data["band"] == "terrain"

    def test_job_new(self, tmp_path):
        out = tmp_path / "v.aconf"
        data = json.loads(
            self._run(["--json", "job", "new", "Vector", "-o", str(out)]).stdout
        )
        assert data["ok"] is True
        assert json.loads(out.read_text(encoding="utf-8"))["Type"] == 6

    def test_full_publish_through_installed_cli(self, fixtures, tmp_path):
        out = tmp_path / "cli_out"
        res = self._run([
            "--json", "run", "road", "--outpath", str(out),
            "--set", f"roadShp={fixtures['shp']}",
            "--set", f"domPath={fixtures['dom']}",
            "--set", f"demPath={fixtures['dem']}",
        ])
        data = json.loads(res.stdout)
        assert data["ok"] is True, data
        tiles = [o for o in data["outputs"] if o["suffix"] == ".3dt"]
        assert tiles and os.path.getsize(tiles[0]["path"]) > MIN_TILE_BYTES
        print(f"\n  CLI artifact: {tiles[0]['path']} ({tiles[0]['size']:,} bytes)")
