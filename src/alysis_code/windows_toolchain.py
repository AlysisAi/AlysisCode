"""Read-only tool grants use verified installation paths, never PATH discovery.

The Windows known-folder API supplies the installation parent. Project files,
environment overrides and executable search in the working directory cannot
select the executable or enlarge this grant.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import MutableMapping
from pathlib import Path


def known_folder(csidl: int) -> Path | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.SHGetFolderPathW.argtypes = [
        wintypes.HWND,
        ctypes.c_int,
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
    ]
    shell.SHGetFolderPathW.restype = ctypes.c_long
    value = ctypes.create_unicode_buffer(32768)
    if shell.SHGetFolderPathW(None, csidl, None, 0, value) != 0:
        return None
    return Path(value.value)


def program_files() -> Path | None:
    return known_folder(0x26)


def _verify_bundled_git_files(root: Path, files: dict[str, str]) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from .windows_sandbox import _local_path

    def verify(relative: str) -> None:
        path = _local_path(root / relative, directory=False)
        with path.open("rb") as binary:
            actual = hashlib.file_digest(binary, "sha256").hexdigest()
        if actual != files[relative]:
            raise ValueError("Bundled Git file integrity check failed: " + relative)

    # The pinned manifest has independent files. Finish all checks before granting the tool.
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(verify, files))


def tool_installation_roots() -> dict[str, Path]:
    """Known installation slots, including absent ones. These paths alone grant no access."""
    parent = program_files()
    if parent is None:
        return {}
    assets = Path(__file__).parent / "assets"
    return {
        "node": parent / "nodejs",
        "git": assets / "windows-git",
        "git-sign": assets / "git-sign",
    }


def installed_tools(*, only: set[str] | None = None) -> dict[str, tuple[Path, Path]]:
    """Admit the selected installations with the same live checks as a shell request."""
    from .windows_sandbox import _local_path

    roots = tool_installation_roots()
    if only is not None:
        roots = {name: root for name, root in roots.items() if name in only}
    result = {}
    node = roots.get("node")
    if node is not None and (node / "node.exe").is_file():
        result["node"] = (_local_path(node / "node.exe", directory=False), _local_path(node))
    bundled = roots.get("git")
    if bundled is not None and bundled.exists():
        root = _local_path(bundled)
        raw = (root / "bundle-manifest.json").read_bytes()
        if (
            hashlib.sha256(raw).hexdigest()
            != "3cb8d24f84c6b2b705aa76457ab7ac931ef1ca3fd66492e1302a159c2f1d094c"
        ):
            raise ValueError("Bundled Git manifest integrity check failed")
        manifest = json.loads(raw)
        actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
        if actual != set(manifest["files"]) | {"bundle-manifest.json"}:
            raise ValueError("Bundled Git file inventory changed")
        _verify_bundled_git_files(root, manifest["files"])
        result["git"] = (root / "cmd/git.exe", root)
    signer_root = roots.get("git-sign")
    if signer_root is not None and signer_root.exists():
        signer = signer_root / "alysis-git-ssh-sign.exe"
        _local_path(signer_root)
        _local_path(signer, directory=False)
        if (
            hashlib.sha256(signer.read_bytes()).hexdigest()
            != "6f0397a21a0d3b31c6972b38c079738b1ba05ae472d19cc4345026ffb6973078"
        ):
            raise ValueError("Contained Git signer integrity check failed")
        result["git-sign"] = (signer, signer_root)
    return result


def search_directories(tools: dict[str, tuple[Path, Path]]) -> list[Path]:
    paths = [binary.parent for binary, _ in tools.values()]
    if "git" in tools:
        root = tools["git"][1]
        paths.extend(
            path
            for path in (root / "ucrt64/bin", root / "mingw64/bin", root / "usr/bin")
            if path.is_dir()
        )
    return list(dict.fromkeys(paths))


def add_bundled_git_to_path(
    environ: MutableMapping[str, str] | None = None, *, windows: bool = os.name == "nt"
) -> Path | None:
    """On Windows without Git, put the engine's own pinned Git last on PATH.

    Restore points (ide/change_ledger.py) and the Git tools run ``git`` from PATH, and
    many Windows computers have no Git: every change was then refused. A Git the person
    installed stays first on PATH, so it is still the one used. Returns the folder added.
    """
    if not windows:
        return None
    environ = os.environ if environ is None else environ
    path = environ.get("PATH", "")
    if shutil.which("git", path=path) is not None:
        return None
    try:
        git = installed_tools().get("git")
    except (OSError, ValueError):  # an altered bundle is never put on PATH
        return None
    if git is None:
        return None
    commands = git[0].parent
    environ["PATH"] = os.pathsep.join(part for part in (path, str(commands)) if part)
    return commands
