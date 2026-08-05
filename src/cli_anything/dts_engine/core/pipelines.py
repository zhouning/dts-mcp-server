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
            "TWO sequential processes: tiling, then pyramid/LOD. The first "
            "exiting does not mean the job is done. See run_osgb_two_stage()."
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
        handle_type=None,
        required=("dtmjsonpath", "outpath"),
        optional=(
            "demPath", "domPath", "dtmOverlapMeter", "dtmInnerDownMeter",
            "dtmInnerEmgenceMeter", "fillvalue", "elevationField",
        ),
        source="FormMain.cs (terrain tab, -dtmjsonpath template)",
        summary="Publish terrain from a DTM job description.",
        notes=(
            "Driven by a JSON job file, not a handleType. Supplying only "
            "-domPath/-demPath with no job file fails with code 1."
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
