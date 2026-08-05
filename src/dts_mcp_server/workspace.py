r"""Per-job sandbox and safe archive extraction.

Remote clients name paths inside their own job directory; nothing they send may
escape it. ``resolve_relative`` rejects five Windows-specific escapes, each of
which is a real bypass rather than a theoretical one:

* absolute paths
* ``:`` — catches both drive prefixes (``C:``) and NTFS alternate data streams
  (``file.txt:hidden``)
* ``\`` UNC prefixes, which would reach another host entirely
* any percent-escape, so a decoding layer downstream cannot reintroduce ``..``
* and finally ``commonpath`` after ``resolve()``, which catches symlinks and
  anything the textual checks missed

``extract_zip`` validates every entry before writing any of them, so an archive
whose bomb is at entry 90,000 leaves nothing partially extracted. It uses
``copyfileobj`` with a bounded chunk rather than ``extractall``, which is the API
with the historical traversal CVEs.
"""
import os
import re
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath


class UnsafeArchiveError(ValueError):
    pass


class Workspace:
    def __init__(
        self,
        root: Path,
        max_expanded_bytes: int,
        max_zip_entries: int,
        max_zip_depth: int,
    ):
        self.root = root.resolve()
        self.max_expanded_bytes = max_expanded_bytes
        self.max_zip_entries = max_zip_entries
        self.max_zip_depth = max_zip_depth
        self.root.mkdir(parents=True, exist_ok=True)

    def job_root(self, job_id: str) -> Path:
        path = self.root / "jobs" / job_id
        for name in ("input", "work", "output", "logs"):
            (path / name).mkdir(parents=True, exist_ok=True)
        return path

    def resolve_relative(self, base: Path, relative: str) -> Path:
        if (
            not relative
            or os.path.isabs(relative)
            or ":" in relative
            or relative.startswith("\\\\")
            or re.search(r"%[0-9A-Fa-f]{2}", relative)
        ):
            raise ValueError(
                "absolute, encoded, drive-prefixed, and UNC paths are rejected"
            )
        root = base.resolve()
        candidate = (root / relative).resolve(strict=False)
        if os.path.commonpath([root, candidate]) != str(root):
            raise ValueError("path leaves the workspace")
        return candidate

    def extract_zip(self, archive: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as source:
            entries = source.infolist()
            self._validate_entries(entries, destination)
            for entry in entries:
                target = self.resolve_relative(
                    destination, PurePosixPath(entry.filename).as_posix()
                )
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with (
                    source.open(entry) as input_stream,
                    target.open("wb") as output_stream,
                ):
                    shutil.copyfileobj(
                        input_stream, output_stream, length=1024 * 1024
                    )

    def _validate_entries(
        self, entries: list[zipfile.ZipInfo], destination: Path
    ) -> None:
        if len(entries) > self.max_zip_entries:
            raise UnsafeArchiveError("archive contains too many entries")
        expanded = 0
        for entry in entries:
            normalized = PurePosixPath(entry.filename)
            if normalized.is_absolute() or ".." in normalized.parts:
                raise UnsafeArchiveError("archive path traversal detected")
            if len(normalized.parts) > self.max_zip_depth:
                raise UnsafeArchiveError("archive nesting limit exceeded")
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise UnsafeArchiveError("archive symlinks are rejected")
            expanded += entry.file_size
            if expanded > self.max_expanded_bytes:
                raise UnsafeArchiveError(
                    "expanded archive size limit exceeded"
                )
            try:
                self.resolve_relative(destination, normalized.as_posix())
            except ValueError as error:
                raise UnsafeArchiveError(str(error)) from error
