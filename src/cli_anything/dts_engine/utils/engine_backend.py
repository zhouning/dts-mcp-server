"""Invoke the real DTS Engine worker.

Follows the invocation contract recovered from the managed shell (Engine.exe,
decompiled): compose flags, bind a UDP log port, launch with cwd set to the
install dir, then map the exit code through the shipped error table.

Two rules learned from live probing:

1. Always drive ``EngineWorker.exe``. ``EngineMaster.exe`` is a scheduler that
   returns -1 (0xFFFFFFFF) instead of propagating the child's error code.
2. cwd MUST be the install directory — the worker resolves gdal305.dll, data/,
   and resource/ relative to it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ..core import errors as errmod
from ..core.install import WORKER, find_engine_dir
from .udp_log import UdpLogListener

#: Engine hangs are indistinguishable from long jobs; cap them by default.
DEFAULT_TIMEOUT = 3600


def build_args(flags: dict[str, object]) -> list[str]:
    """Render a flag mapping into Engine's ``-key=value`` argv form.

    ``None`` values are dropped (matching the shell's NullValueHandling.Ignore).
    Booleans render lowercase, as the shell emits them.
    """
    out: list[str] = []
    for key, value in flags.items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        out.append(f"-{key}={value}")
    return out


def run_worker(
    flags: dict[str, object],
    engine_dir: str | Path | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    on_line=None,
    capture_udp: bool = True,
) -> dict:
    """Run EngineWorker.exe with ``flags`` and return a structured result.

    Returns a dict with ``exit_code``, ``ok``, ``error`` (band + message),
    ``log`` (parsed UDP records), ``command``, and ``elapsed_seconds``.
    """
    import time

    d = find_engine_dir(engine_dir)
    table = errmod.load_table(d)

    listener = UdpLogListener(on_line=on_line) if capture_udp else None
    if listener:
        listener.start()
        flags = {**flags, "udp": listener.port}

    argv = [str(d / WORKER)] + build_args(flags)
    start = time.time()
    timed_out = False
    try:
        proc = subprocess.run(
            argv, cwd=str(d), capture_output=True, timeout=timeout
        )
        code = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        code = None
    elapsed = time.time() - start

    if listener:
        listener.stop()

    result: dict = {
        "command": argv,
        "exit_code": code,
        "timed_out": timed_out,
        "elapsed_seconds": round(elapsed, 2),
        "ok": code == 0,
    }
    if code is not None:
        result["error"] = errmod.describe(code, table)
    if listener:
        result["log"] = listener.records
        result["log_retcode"] = listener.retcode
    return result


def collect_outputs(outpath: str | Path) -> list[dict]:
    """List produced artifacts. Engine writes ``.3dt`` tiles + DataInfor.txt."""
    p = Path(outpath)
    if not p.is_dir():
        return []
    return [
        {
            "path": str(f),
            "name": f.name,
            "size": f.stat().st_size,
            "suffix": f.suffix.lower(),
        }
        for f in sorted(p.rglob("*"))
        if f.is_file()
    ]
