"""Security tests. These encode attacker intent, not happy paths.

Each case is a real bypass that the corresponding defence exists to stop, so a
failure here means a remote client can reach something it should not.
"""
from __future__ import annotations

import time
import zipfile

import pytest

from dts_mcp_server.auth import ArtifactSigner, StaticBearerVerifier
from dts_mcp_server.engine import InputResolutionError, resolve_path_flags
from dts_mcp_server.workspace import UnsafeArchiveError, Workspace


@pytest.fixture
def workspace(tmp_path):
    return Workspace(
        root=tmp_path / "ws",
        max_expanded_bytes=1024 * 1024,
        max_zip_entries=100,
        max_zip_depth=4,
    )


# ------------------------------------------------------------ path traversal

@pytest.mark.parametrize(
    "hostile",
    [
        "../escape.txt",
        "../../escape.txt",
        "sub/../../escape.txt",
        "C:/Windows/System32/config/SAM",
        "C:\\Windows\\win.ini",
        "/etc/passwd",
        "\\\\attacker-host\\share\\payload.dll",
        "file.txt:hidden",          # NTFS alternate data stream
        "stream.txt::$DATA",
        "%2e%2e/escape.txt",        # percent-encoded traversal
        "%2E%2E%5Cescape.txt",
        "",
    ],
)
def test_resolve_relative_rejects_escapes(workspace, tmp_path, hostile):
    base = tmp_path / "base"
    base.mkdir()
    with pytest.raises(ValueError):
        workspace.resolve_relative(base, hostile)


def test_resolve_relative_allows_ordinary_paths(workspace, tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    resolved = workspace.resolve_relative(base, "sub/dir/roads.shp")
    assert str(resolved).startswith(str(base.resolve()))


def test_resolve_relative_blocks_symlink_escape(workspace, tmp_path):
    """A symlink inside the sandbox must not become an exit from it."""
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    try:
        (base / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation requires privilege on this host")
    with pytest.raises(ValueError):
        workspace.resolve_relative(base, "link/secret.txt")


# --------------------------------------------------------------- zip attacks

def test_archive_traversal_rejected(workspace, tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escaped.txt", "payload")
    with pytest.raises(UnsafeArchiveError):
        workspace.extract_zip(archive, tmp_path / "out")


def test_archive_absolute_path_rejected(workspace, tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("/absolute.txt", "payload")
    with pytest.raises(UnsafeArchiveError):
        workspace.extract_zip(archive, tmp_path / "out")


def test_archive_entry_count_limit(workspace, tmp_path):
    archive = tmp_path / "many.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for index in range(150):  # limit is 100
            zf.writestr(f"f{index}.txt", "x")
    with pytest.raises(UnsafeArchiveError, match="too many entries"):
        workspace.extract_zip(archive, tmp_path / "out")


def test_archive_depth_limit(workspace, tmp_path):
    archive = tmp_path / "deep.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a/b/c/d/e/f/deep.txt", "x")  # limit is 4
    with pytest.raises(UnsafeArchiveError, match="nesting"):
        workspace.extract_zip(archive, tmp_path / "out")


def test_archive_expanded_size_limit(workspace, tmp_path):
    """A zip bomb is caught by declared uncompressed size, before extraction."""
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb.bin", b"\0" * (4 * 1024 * 1024))  # limit is 1 MiB
    with pytest.raises(UnsafeArchiveError, match="expanded"):
        workspace.extract_zip(archive, tmp_path / "out")


def test_bomb_leaves_nothing_extracted(workspace, tmp_path):
    """Validate-all-then-write: a rejected archive writes no files at all."""
    archive = tmp_path / "mixed.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("innocent.txt", "fine")
        zf.writestr("../escaped.txt", "payload")
    destination = tmp_path / "out"
    with pytest.raises(UnsafeArchiveError):
        workspace.extract_zip(archive, destination)
    assert not (destination / "innocent.txt").exists()


def test_archive_symlink_entry_rejected(workspace, tmp_path):
    """A symlink entry could redirect a later entry outside the sandbox."""
    import stat as stat_module

    archive = tmp_path / "link.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat_module.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(UnsafeArchiveError, match="symlink"):
        workspace.extract_zip(archive, tmp_path / "out")


# ----------------------------------------------------------- signed URLs

def test_signature_roundtrip():
    signer = ArtifactSigner(b"k" * 48)
    expires = int(time.time()) + 600
    signature = signer.sign("upload", "abc", expires)
    assert signer.verify("upload", "abc", expires, signature)


def test_signature_operation_is_bound():
    """An upload URL must not be replayable as a download URL."""
    signer = ArtifactSigner(b"k" * 48)
    expires = int(time.time()) + 600
    signature = signer.sign("upload", "abc", expires)
    assert not signer.verify("download", "abc", expires, signature)


def test_signature_artifact_is_bound():
    signer = ArtifactSigner(b"k" * 48)
    expires = int(time.time()) + 600
    signature = signer.sign("download", "abc", expires)
    assert not signer.verify("download", "other", expires, signature)


def test_signature_expiry_enforced():
    signer = ArtifactSigner(b"k" * 48)
    expired = int(time.time()) - 1
    assert not signer.verify(
        "download", "abc", expired, signer.sign("download", "abc", expired)
    )


def test_signature_rejects_forgery():
    signer = ArtifactSigner(b"k" * 48)
    expires = int(time.time()) + 600
    assert not signer.verify("download", "abc", expires, "0" * 64)


def test_different_keys_do_not_interoperate():
    """Rotating the signing key must invalidate previously issued URLs."""
    expires = int(time.time()) + 600
    old = ArtifactSigner(b"a" * 48).sign("download", "abc", expires)
    assert not ArtifactSigner(b"b" * 48).verify(
        "download", "abc", expires, old
    )


# ----------------------------------------------------------------- bearer

async def test_bearer_accepts_exact_token():
    verifier = StaticBearerVerifier("t" * 48)
    assert await verifier.verify_token("t" * 48) is not None


@pytest.mark.parametrize(
    "wrong",
    ["", "t" * 47, "t" * 49, "T" * 48, "x" * 48],
)
async def test_bearer_rejects_wrong_token(wrong):
    verifier = StaticBearerVerifier("t" * 48)
    assert await verifier.verify_token(wrong) is None


# ------------------------------------------------------- input resolution

async def test_path_flag_rejects_bare_host_path():
    """A remote client must not be able to name a host path directly."""
    with pytest.raises(InputResolutionError, match="host paths are not accepted"):
        await resolve_path_flags(
            {"roadShp": "C:/Windows/win.ini"}, None, None
        )


async def test_path_flag_rejects_missing_artifact_key():
    with pytest.raises(InputResolutionError):
        await resolve_path_flags({"domPath": {"path": "dom.tif"}}, None, None)


async def test_non_path_flags_pass_through():
    resolved, referenced = await resolve_path_flags(
        {"roadRGB": "255,0,0", "roadSize": 5}, None, None
    )
    assert resolved == {"roadRGB": "255,0,0", "roadSize": 5}
    assert referenced == []
