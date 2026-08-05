"""Locate a DTS Engine install tree and describe its capabilities."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

#: Env var callers can set to point at a non-default install.
ENV_VAR = "DTS_ENGINE_DIR"

#: Executables the harness drives. EngineWorker is the real workhorse;
#: EngineMaster is a scheduler that does NOT propagate child exit codes.
WORKER = "EngineWorker.exe"
MASTER = "EngineMaster.exe"
SHELL = "Engine.exe"


class EngineNotFound(RuntimeError):
    """Raised when no DTS Engine install can be located."""


def _roaming_roots() -> list[Path]:
    """Directories that may hold a per-user "DTS Engine" install.

    %APPDATA% alone is not enough. A relocated user profile leaves %APPDATA%
    pointing at C: while the actual folder lives on another drive, so the same
    relative path is probed under every fixed drive as well.
    """
    roots: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata))

    profile = os.environ.get("USERPROFILE")
    if profile:
        roots.append(Path(profile) / "AppData" / "Roaming")

    # Same user-relative path on other drives, for a redirected profile.
    if appdata:
        tail = Path(appdata)
        try:
            relative = tail.relative_to(tail.anchor)
        except ValueError:
            relative = None
        if relative is not None:
            for letter in "DEFGH":
                candidate = Path(f"{letter}:\\") / relative
                if candidate.is_dir():
                    roots.append(candidate)

    program_files = os.environ.get("ProgramFiles")
    if program_files:
        roots.append(Path(program_files))
    return roots


def _candidates() -> list[Path]:
    out: list[Path] = []
    env = os.environ.get(ENV_VAR)
    if env:
        out.append(Path(env))
    for base in _roaming_roots():
        root = base / "DTS Engine"
        if root.is_dir():
            # Prefer the highest version directory present.
            versions = sorted((d for d in root.iterdir() if d.is_dir()), reverse=True)
            out.extend(versions)
            out.append(root)
    found = shutil.which(WORKER)
    if found:
        out.append(Path(found).parent)
    return out


def find_engine_dir(explicit: str | Path | None = None) -> Path:
    """Return the install directory containing EngineWorker.exe.

    Search order: explicit argument, ``DTS_ENGINE_DIR``, standard install
    locations under %APPDATA%/%ProgramFiles%, then PATH.
    """
    probes = [Path(explicit)] if explicit else []
    probes += _candidates()
    for p in probes:
        if (p / WORKER).is_file():
            return p
    tried = "\n  ".join(str(p) for p in probes) or "(none)"
    raise EngineNotFound(
        f"Could not find {WORKER}.\nTried:\n  {tried}\n\n"
        f"Install DTS Engine (vendor: 飞渡科技 / freedoonline.com), or set "
        f"{ENV_VAR} to the directory containing {WORKER}."
    )


def worker_path(engine_dir: str | Path | None = None) -> Path:
    return find_engine_dir(engine_dir) / WORKER


def describe_install(engine_dir: str | Path | None = None) -> dict:
    """Report version, present executables, and the bundled GDAL."""
    d = find_engine_dir(engine_dir)
    version = None
    vfile = d / "Version.ini"
    if vfile.is_file():
        version = vfile.read_text(encoding="utf-8", errors="replace").strip()
    gdal = sorted(p.name for p in d.glob("gdal*.dll"))
    return {
        "engine_dir": str(d),
        "version": version,
        "executables": {
            name: (d / name).is_file() for name in (WORKER, MASTER, SHELL)
        },
        "gdal": gdal,
        "error_table": (d / "ErrorCode_en.txt").is_file(),
    }
