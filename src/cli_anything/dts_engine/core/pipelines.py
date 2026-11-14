"""DTS Engine pipeline definitions.

Every entry is transcribed from an argument-composition site in the decompiled
managed shell (AirCityEngine/FormMain.cs). ``source`` records that line so the
spec can be re-verified against a future Engine version.

Verified live on Engine 6.1.0525.497: ``road`` with DOM+DEM exits 0 and emits a
.3dt tile. Pipelines marked ``verified=False`` have correct flag names but their
required-input combinations have not been confirmed by a successful run.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Pipeline:
    name: str
    handle_type: str | None
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    source: str = ""
    summary: str = ""
    verified: bool = False
    notes: str = ""


#: Flags every pipeline accepts. ``udp`` is injected by the backend.
COMMON_OPTIONAL: tuple[str, ...] = (
    "inWkt", "outWkt", "projid", "srsOriginX", "srsOriginY", "srsOriginZ",
    "unit", "license_server", "license_port", "useGeneralEncryptKey",
    "overridekeyinfo", "packAllFile", "stream",
)

PIPELINES: dict[str, Pipeline] = {
    "road": Pipeline(
        name="road",
        handle_type="road",
        required=("roadShp", "outpath", "domPath"),
        optional=(
            "demPath", "xshpoffset", "yshpoffset", "roadRGB", "roadSize",
            "getShpColorField", "shpOuterPath", "shpInnerPath", "jsonPath",
            "fillvalue", "elevationField",
        ),
        source="FormMain.cs:7480",
        summary="Drape vector features onto terrain and publish a 3DT tile.",
        verified=True,
        notes=(
            "domPath is mandatory despite not appearing in the shell's base "
            "template: omitting it fails with code 201 (DOM path empty). "
            "Confirmed by live probe."
        ),
    ),
    "osgbLod": Pipeline(
        name="osgbLod",
        handle_type="osgbLod",
        required=("inpath", "outpath"),
        optional=(
            "xshpoffset", "yshpoffset", "collision", "forceCullFace",
            "reductionTileNode", "filter", "hlod", "hlodOctree",
            "hlodMergeLeaf", "hlodMergeDistance", "hlodMaxDrawDistance",
            "hlodDistanceScale", "reductionHLOD", "mergeTexture",
            "minMipmapSize", "recalculateNormal", "viewDetailLevel",
        ),
        source="FormMain.cs:3290",
        summary="Tile oblique-photogrammetry OSGB and build the LOD pyramid.",
        notes=(
            "TWO stages, captured from the 7.0 GUI 2026-11-14. Stage 1 = "
            "EngineMaster (exit 0) spawning one Worker per tile: -handleType=\"\" "
            "-MasterCreate=true -inpath=<Data/T/T.osgb> -outpath=<out/Data/T> "
            "-filter=+osgb -> <out>/Data/T/T.3dt (37MB for the 31MB sample). "
            "Stage 2 = Worker -handleType=osgbLod -inpath=<root> -outpath=<out> "
            "-> root pyramid <out>/<name>.3dt (~520KB). osgbLod alone never makes "
            "tile 3DTs. Use publish.publish_osgb(). Empty inpath -> 104."
        ),
    ),
    "vtpk": Pipeline(
        name="vtpk",
        handle_type="vtpk",
        required=("roadShp", "jsonPath", "styleJsonPath", "outpath"),
        optional=("domPath", "demPath", "xshpoffset", "yshpoffset"),
        source="FormMain.cs:7462",
        summary="Publish an Esri vector tile package.",
        notes="The shell decompresses the .vtpk first and passes the extracted JSON.",
    ),
    "waterTerrain": Pipeline(
        name="waterTerrain",
        handle_type="waterTerrain",
        required=("seaPath", "outpath"),
        optional=(
            "xshpoffset", "yshpoffset", "seaDomPath", "coastDemPath",
            "coastLength", "coastMeterWaterHeight", "water",
        ),
        source="FormMain.cs:7874",
        summary="Generate sea/water surface geometry against coastal terrain.",
    ),
    "shp": Pipeline(
        name="shp",
        handle_type="shp",
        required=("inpath", "outpath"),
        optional=("xshpoffset", "yshpoffset", "tree", "shpOidFieldName", "useCustomOID"),
        source="FormMain.cs:7138",
        summary="Build vegetation/material resources from a point shapefile.",
        notes=(
            "NOT a general vector converter. Driven by the Material tab and "
            "expects tree-resource attributes; a plain polygon shapefile fails "
            "with code 302. Species names come from resource/custom_tree.txt."
        ),
    ),
    "multosgb": Pipeline(
        name="multosgb",
        handle_type="multosgb",
        required=("inpath", "outpath"),
        optional=("allowScanDir", "filter", "xshpoffset", "yshpoffset"),
        source="FormMain.cs:3948",
        summary="Batch-process multiple OSGB datasets.",
        verified=True,
        notes=(
            "Live 2026-11-14 on Engine 7.0: -inpath=<root with metadata.xml + "
            "Data/Tile_*> exits 0 after ~310s on a 31MB/158-tile sample and "
            "writes one <dir>_multosgb.3dt of ~139MB -- processes every LOD level."
        ),
    ),
    "max": Pipeline(
        name="max",
        handle_type="max",
        required=("inpath", "outpath"),
        optional=("instance", "forceInstanceCheck", "mergeTexture", "specialMaterial"),
        source="FormMain.cs:3944",
        summary="Publish 3ds Max scene data.",
    ),
    "Library": Pipeline(
        name="Library",
        handle_type="Library",
        required=("inpath", "outpath"),
        optional=("replaceWithCustomAssets", "instance"),
        source="FormMain.cs:8220",
        summary="Publish a model library.",
    ),
    "terrain": Pipeline(
        name="terrain",
        handle_type="gdal",
        required=("domPath", "demPath", "outpath"),
        optional=(
            "xshpoffset", "yshpoffset", "domEnableNoValue", "demEnableNoValue",
            "collision", "fillvalue", "emergenceDem", "emegencelength",
            "maxMipmapCount", "shpOuterPath", "shpInnerPath", "dtmjsonpath",
            "dtmOverlapMeter", "dtmInnerDownMeter", "dtmInnerEmgenceMeter",
            "elevationField",
        ),
        source="captured live from Engine 7.0.0814.5654 GUI (EngineMaster cmdline)",
        summary="Publish terrain (DEM + DOM) to a 3DT tile set.",
        verified=True,
        notes=(
            "Terrain is -handleType=gdal (NOT 'terrain', which returns code 2). "
            "Captured 2026-11-14 from the GUI: -domPath -demPath -outpath "
            "-xshpoffset=0 -yshpoffset=0 -domEnableNoValue=true "
            "-demEnableNoValue=true -collision=true -fillvalue=0 -emergenceDem "
            "-emegencelength=50 -useGeneralEncryptKey=True -maxMipmapCount=1. "
            "Replayed on EngineWorker directly: full set and the minimal "
            "domPath+demPath+outpath both exit 0 in ~12s, ~320KB .3dt + "
            "DataInfor.txt, same size as the GUI run. -dtmjsonpath is optional "
            "terrain fusion (DTM merge), not the entry point. The .aconf "
            "-jsonPath mode returns code 1; the GUI does not use it for terrain."
        ),
    ),
}


def get(name: str) -> Pipeline:
    try:
        return PIPELINES[name]
    except KeyError:
        raise KeyError(
            f"Unknown pipeline {name!r}. Known: {', '.join(sorted(PIPELINES))}"
        ) from None


def validate(name: str, flags: dict[str, object]) -> list[str]:
    """Return a list of problems; empty means the flag set looks runnable."""
    spec = get(name)
    problems = [f"missing required flag: -{r}" for r in spec.required
                if not flags.get(r)]
    allowed = set(spec.required) | set(spec.optional) | set(COMMON_OPTIONAL) | {
        "handleType", "udp", "window", "title",
    }
    problems += [f"unknown flag for pipeline {name!r}: -{k}"
                 for k in flags if k not in allowed]
    return problems


def to_flags(name: str, flags: dict[str, object]) -> dict[str, object]:
    """Merge the pipeline's handleType into a user flag mapping."""
    spec = get(name)
    out = dict(flags)
    if spec.handle_type:
        out["handleType"] = spec.handle_type
    return out
