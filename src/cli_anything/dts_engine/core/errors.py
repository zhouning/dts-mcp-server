"""DTS Engine error codes.

The install tree ships the authoritative table as ErrorCode_en.txt (English) and
ErrorCode.txt (Chinese), in the format ``<code>---<message>``. Parse the shipped
file rather than hardcoding, so the harness stays correct across Engine versions.

The managed shell's own ``ProcessErrorCode`` enum only covers codes 0-7 and 255;
the shipped file has ~90 codes across functional bands.
"""
from __future__ import annotations

from pathlib import Path

#: Functional bands, from the shipped table's numbering scheme.
BANDS: list[tuple[range, str]] = [
    (range(0, 20), "io"),
    (range(100, 120), "lod"),
    (range(200, 250), "terrain"),
    (range(300, 310), "resource"),
    (range(400, 410), "crs"),
    (range(500, 510), "vtpk"),
    (range(600, 610), "dem_opt"),
]

#: Subset the .NET shell exposes as an enum; used as fallback when the shipped
#: table is unavailable.
FALLBACK: dict[int, str] = {
    0: "OK",
    1: "InvalidParam",
    2: "TypeNotSupport",
    3: "CreatePak",
    4: "ReadFile",
    5: "FileContent",
    6: "InitGDALReader",
    7: "FileNotFound",
    255: "Undefined",
    65535: "Undefined error code",
}


def band_of(code: int) -> str:
    """Return the functional band name for an error code."""
    for rng, name in BANDS:
        if code in rng:
            return name
    return "unknown"


def parse_table(path: str | Path) -> dict[int, str]:
    """Parse a shipped ``ErrorCode*.txt`` into ``{code: message}``.

    Tolerates blank lines and the trailing entries that have no separator.
    """
    table: dict[int, str] = {}
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line or "---" not in line:
            continue
        head, _, msg = line.partition("---")
        try:
            table[int(head.strip())] = msg.strip()
        except ValueError:
            continue
    return table


def load_table(engine_dir: str | Path | None, language: str = "en") -> dict[int, str]:
    """Load the error table from an install dir, falling back to the enum subset."""
    if engine_dir:
        name = "ErrorCode_en.txt" if language == "en" else "ErrorCode.txt"
        candidate = Path(engine_dir) / name
        if candidate.is_file():
            table = parse_table(candidate)
            if table:
                return table
    return dict(FALLBACK)


def describe(code: int, table: dict[int, str] | None = None) -> dict:
    """Structured description of an exit code."""
    table = table or FALLBACK
    # EngineMaster.exe returns -1 as an unsigned 32-bit value rather than
    # propagating the child's code.
    normalized = -1 if code == 4294967295 else code
    return {
        "code": normalized,
        "ok": normalized == 0,
        "band": band_of(normalized),
        "message": table.get(normalized, f"Unmapped code {normalized}"),
    }
