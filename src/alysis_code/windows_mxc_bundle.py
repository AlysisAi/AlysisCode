"""Immutable, offline Windows helper shipped in wheel, sdist and frozen CLI.

The pins are release inputs, not instructions from a workspace or a sidecar.
Updating a helper requires changing these reviewed pins and its bundled bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

HELPER_SHA256 = "b977585de545b26f58fb7dd40e2793e3283ba1e30f9708a1c024c87e6eb6dfb8"
HELPER_BYTES = 4470568
HELPER_SOURCE = "ae3e9a1e494506a99782cbda16080228e090a11f"
HELPER_SIGNER = "3B04D12A1BAEF7E93EFBF933EE2A3DE82D9D846E"
BUNDLE_ROOT = Path(__file__).parent / "assets" / "windows-mxc"
BUNDLE_FILES = {
    "alysis-sandbox.exe": HELPER_SHA256,
    "SOURCE.json": "edef0a786f566ec01bfff00150c1b7726bc87eab5435e4a20258b874b98e8efc",
    "manifest.json": "ebd3a3aa897f877a4359f63bc1070888fd264782a8f2b23b57c49e22ba0cea8c",
    "LICENSE.mxc": "d9a1b1e30d633d5732ea18e3cba9538d293ebc53e1a9e4e96ab739e0c5c4f1cb",
}
LOCAL_CANDIDATE = False
_LOCAL_MARKER = Path(__file__).with_name("_windows_mxc_local.json")
_LOCAL_MARKER_SHA256 = None
if _LOCAL_MARKER.exists():
    # Only the explicit, provenance-checked wheel build creates this file. It is
    # part of the private installed runtime, never read from a workspace or env.
    if _LOCAL_MARKER.stat().st_size > 16 * 1024:
        raise ValueError("Local sandbox candidate marker is oversized")
    _marker_bytes = _LOCAL_MARKER.read_bytes()
    _marker = json.loads(_marker_bytes)
    if (
        set(_marker)
        != {"schema_version", "qualification", "source_commit", "helper_bytes", "files"}
        or _marker["schema_version"] != 1
        or _marker["qualification"] != "prototype-unqualified"
        or not re.fullmatch(r"[a-f0-9]{40}", str(_marker["source_commit"]))
        or type(_marker["helper_bytes"]) is not int
        or _marker["helper_bytes"] < 128
        or not isinstance(_marker["files"], dict)
        or set(_marker["files"]) != set(BUNDLE_FILES)
        or any(not re.fullmatch(r"[a-f0-9]{64}", str(value)) for value in _marker["files"].values())
    ):
        raise ValueError("Invalid local sandbox candidate marker")
    LOCAL_CANDIDATE = True
    HELPER_SHA256 = _marker["files"]["alysis-sandbox.exe"]
    HELPER_BYTES = _marker["helper_bytes"]
    HELPER_SOURCE = _marker["source_commit"]
    HELPER_SIGNER = None  # A local build makes no signing or release claim.
    BUNDLE_FILES = _marker["files"]
    _LOCAL_MARKER_SHA256 = hashlib.sha256(_marker_bytes).hexdigest()


def verify_bundle(root: Path = BUNDLE_ROOT) -> Path:
    """Validate every release input without executing any bundled code."""
    inputs = {root / name: expected for name, expected in BUNDLE_FILES.items()}
    if _LOCAL_MARKER_SHA256:
        inputs[_LOCAL_MARKER] = _LOCAL_MARKER_SHA256
    for path, expected in inputs.items():
        # On Windows these paths are executed under strict local-path rules.
        # Elsewhere the helper is inert package data; normal system aliases
        # such as macOS /var must not prevent building the distribution.
        inspected = (path, *path.parents) if os.name == "nt" else (path,)
        for item in inspected:
            metadata = item.lstat()
            if stat.S_ISLNK(metadata.st_mode) or (
                getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise ValueError("Windows sandbox bundle cannot traverse links or reparse points")
        if not path.is_file():
            raise ValueError("Windows sandbox bundle must contain ordinary files")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise ValueError(f"Windows sandbox bundle integrity check failed: {path.name}")
    helper = root / "alysis-sandbox.exe"
    if helper.stat().st_size != HELPER_BYTES:
        raise ValueError("Windows sandbox helper size mismatch")
    return helper


def runtime_cache_root() -> Path:
    """Only used for Windows package managers that hardlink their cache files."""
    base = Path(os.environ.get("LOCALAPPDATA", ""))
    if not base.is_absolute():
        raise ValueError("Windows sandbox requires an absolute LOCALAPPDATA")
    return base / "alysis-code" / "sandbox-helpers"


def runtime_helper() -> Path:
    helper = verify_bundle()
    if helper.stat().st_nlink == 1:
        return helper
    # uv can hardlink package data to its global cache. Do not grant that shared
    # directory to an untrusted child. Copy only pinned helper bytes into a
    # digest-specific local directory, publishing atomically without overwrite.
    from .windows_sandbox import _local_path

    directory = runtime_cache_root() / HELPER_SHA256
    for parent in reversed((directory, *directory.parents)):
        if not parent.exists():
            parent.mkdir()
        _local_path(parent)
    target = directory / "alysis-sandbox.exe"
    if not target.exists():
        descriptor, temporary = tempfile.mkstemp(prefix=".staging-", dir=directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                payload = helper.read_bytes()
                if hashlib.sha256(payload).hexdigest() != HELPER_SHA256:
                    raise ValueError("Windows sandbox source changed while preparing its helper")
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.rename(temporary, target)  # Windows refuses an existing target.
            except FileExistsError:
                pass  # Another launcher won; verify its bytes below.
        finally:
            Path(temporary).unlink(missing_ok=True)
    _local_path(target, directory=False)
    if (
        target.stat().st_nlink != 1
        or hashlib.sha256(target.read_bytes()).hexdigest() != HELPER_SHA256
    ):
        raise ValueError("Windows sandbox cache is damaged; no command was launched")
    return target
