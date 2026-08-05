"""DTS Engine ``.aconf`` job files.

The managed shell serializes one config object per data type with
Newtonsoft.Json (``Formatting.Indented``, ``NullValueHandling.Ignore``) and hands
the path to the worker via ``-jsonPath=``. Field names below are transcribed
from AirCityEngine/Config*.cs, so PascalCase is intentional — the worker expects
the C# property names.
"""
from __future__ import annotations

import json
from pathlib import Path

#: AirCityEngine/DataType.cs — the enum is serialized by ordinal position.
DATA_TYPES: dict[str, int] = {
    "Terrain": 0,
    "Osgb": 1,
    "MAX": 2,
    "BIM": 3,
    "LAS": 4,
    "Material": 5,
    "Vector": 6,
    "Sea": 7,
    "Skeletal": 8,
    "DTOModeData": 9,
}

#: AirCityEngine/ColorSourceType.cs
COLOR_SOURCES: dict[str, int] = {"Custom": 0, "ColorFiled": 1}

#: AirCityEngine/VectorDataType.cs
VECTOR_TYPES: dict[str, int] = {"SHP": 0, "VTPK": 1}

#: Base fields from AirCityEngine/Config.cs, shared by every job type.
BASE_FIELDS: tuple[str, ...] = (
    "Type", "TimeStamp", "OverrideKeyInfo", "AdditionalParam",
    "IncrementImporterStatus",
)

#: Per-type fields, transcribed from the Config* classes.
JOB_FIELDS: dict[str, tuple[str, ...]] = {
    "Vector": (
        "RoadShpPath", "OuterShp", "InnerShp", "OffsetX", "OffsetY",
        "ShpFileRelateVTPKPath", "ColorSource", "RoadColor", "RoadWidth",
        "OutputPath", "FileName", "ShowWindow", "AdvanceParam",
    ),
    "Osgb": (
        "InPath", "OutPath", "OuterShp", "InnerShp", "FileName",
        "OffsetX", "OffsetY", "ShowWindow", "ProcessCount",
        "SRSOriginX", "SRSOriginY", "SRSOriginZ", "InWKT", "OutWKT",
        "AsyncPyramid", "Collision", "CullBack", "ReductionTileNode",
    ),
}

#: Defaults the C# constructors apply; replicated so generated jobs match the
#: shell's own output.
DEFAULTS: dict[str, dict] = {
    "Vector": {
        "ShpFileRelateVTPKPath": "",
        "ColorSource": COLOR_SOURCES["Custom"],
        "RoadWidth": 0.0,
    },
    "Osgb": {
        "Collision": False,
        "ReductionTileNode": False,
    },
}


def new_job(data_type: str, **fields) -> dict:
    """Build a job dict for ``data_type`` (a DATA_TYPES key)."""
    if data_type not in DATA_TYPES:
        raise KeyError(
            f"Unknown data type {data_type!r}. Known: {', '.join(DATA_TYPES)}"
        )
    job: dict = {"Type": DATA_TYPES[data_type]}
    job.update(DEFAULTS.get(data_type, {}))
    job.update({k: v for k, v in fields.items() if v is not None})
    return job


def validate_job(data_type: str, job: dict) -> list[str]:
    """Flag fields the shell would not emit for this job type."""
    known = set(BASE_FIELDS) | set(JOB_FIELDS.get(data_type, ()))
    if not JOB_FIELDS.get(data_type):
        return []  # schema for this type not yet transcribed; do not guess
    return [f"unknown field for {data_type}: {k}" for k in job if k not in known]


def write_job(job: dict, path: str | Path) -> Path:
    """Write a job file, matching the shell's serializer settings."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in job.items() if v is not None}
    p.write_text(
        json.dumps(clean, indent=4, ensure_ascii=False), encoding="utf-8"
    )
    return p


def read_job(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
