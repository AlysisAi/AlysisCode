"""Optional host-configured Office rendering. No downloads or user-profile reuse."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from alysis_code.documents.__main__ import _check_execution, _path, _render
from alysis_code.office import renderer_path


def _check_renderable(source: Path) -> None:
    with zipfile.ZipFile(source) as archive:
        for name in archive.namelist():
            if any(part in name.lower() for part in ("/embeddings/", "vbaproject", "/activex/")):
                raise ValueError("Embedded programs and macros cannot be rendered by this helper.")
            if name.endswith(".rels"):
                for relation in ET.fromstring(archive.read(name)):
                    if relation.attrib.get("TargetMode") == "External" and not relation.attrib.get(
                        "Type", ""
                    ).endswith("/hyperlink"):
                        raise ValueError(
                            "Linked external document content must be removed before rendering."
                        )


@contextmanager
def _cancellation():
    def cancelled(signum, frame):
        raise KeyboardInterrupt

    old = None
    if threading.current_thread() is threading.main_thread():
        old = signal.signal(signal.SIGTERM, cancelled)
    try:
        yield
    finally:
        if old is not None:
            signal.signal(signal.SIGTERM, old)


def _publish_directory(source: Path, destination: Path) -> None:
    """Publish a complete directory atomically without replacing even an empty one."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = rename(os.fsencode(source), os.fsencode(destination), 4)  # RENAME_EXCL
    elif sys.platform == "linux" and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        result = rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    else:
        raise ValueError("Atomic Office preview publication is not supported on this platform.")
    if result:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))


def _convert(
    executable: Path,
    source: Path,
    destination: Path,
    profile: Path,
    *,
    target_format: str = "pdf",
    timeout_seconds: float = 90,
    cancel_check: Callable[[], bool] | None = None,
) -> None:
    if os.name != "posix":
        raise ValueError("Office renderer process containment currently supports macOS and Linux.")

    profile.mkdir()
    (profile / "user").mkdir()
    (profile / "user/registrymodifications.xcu").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
        '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
        '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
        '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>'
        '<prop oor:name="DisableActiveContent" oor:op="fuse"><value>true</value></prop>'
        '<prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse"><value>true</value></prop>'
        '</item><item oor:path="/org.openoffice.Office.Calc/Formula/Load">'
        '<prop oor:name="OOXMLRecalcMode" oor:op="fuse"><value>0</value></prop>'
        "</item></oor:items>",
        encoding="utf-8",
    )
    command = [
        str(executable),
        "-env:UserInstallation=" + profile.as_uri(),
        "--headless",
        "--nologo",
        "--nodefault",
        "--norestore",
        "--convert-to",
        target_format,
        "--outdir",
        str(destination),
        str(source),
    ]
    # Log to a file instead of an unbounded PIPE. The entire temporary tree is removed on exit.
    with _cancellation(), (destination / "converter.log").open("wb") as log:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True
        )
        try:
            deadline = time.monotonic() + timeout_seconds
            while process.poll() is None:
                if cancel_check and cancel_check():
                    raise KeyboardInterrupt
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout_seconds)
                try:
                    process.wait(timeout=min(0.2, remaining))
                except subprocess.TimeoutExpired:
                    continue
            status = process.returncode
            if status:
                raise ValueError(
                    f"Office renderer failed (exit {status}); no preview was published."
                )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Office rendering timed out; no preview was published.") from exc
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process.poll() is None:
                process.kill()
            process.wait()


def render(source: Path, preview: Path, root: Path, source_hash: str, **conversion_options) -> dict:
    with _cancellation():
        return _render_office(source, preview, root, source_hash, **conversion_options)


def _render_office(
    source: Path,
    preview: Path,
    root: Path,
    source_hash: str,
    *,
    execution_deadline=None,
    **conversion_options,
) -> dict:
    execution_options = {
        "execution_deadline": execution_deadline,
        "cancel_check": conversion_options.get("cancel_check"),
    }
    _check_execution(**execution_options)
    executable = renderer_path()
    if executable is None:
        raise ValueError(
            "Office preview is unavailable: the host must configure ALYSIS_OFFICE_SOFFICE. The editable file remains usable."
        )
    if preview.exists():
        raise ValueError("Use a new preview directory for every revision.")
    _check_renderable(source)
    with tempfile.TemporaryDirectory(prefix=".alysis-office-render-", dir=root) as temp:
        stage = Path(temp)
        converted = stage / "converted"
        converted.mkdir()
        _convert(executable, source, converted, stage / "profile", **conversion_options)
        pdf = converted / (source.stem + ".pdf")
        if not pdf.is_file() or pdf.is_symlink():
            raise ValueError("Office renderer did not create its expected PDF.")
        pages = stage / "preview"
        pages.mkdir()
        shutil.copyfile(pdf, pages / "preview.pdf")
        result = _render(pages / "preview.pdf", pages, **execution_options)
        # A rendering defect is precisely when the caller needs these images.
        # Preserve the observations and publish the preview; artifact_check
        # records the failed bounds check separately from conversion success.
        result.update(
            {
                "status": "rendered",
                "source_sha256": source_hash,
                "renderer": "LibreOffice",
                "formula_calculation": "not_verified",
                "pdf": str((preview / "preview.pdf").relative_to(root)),
            }
        )
        for page in result["pages"]:
            page["image"] = str((preview / page["image"]).relative_to(root))
        (pages / "preview.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _path(root, str(preview))
        _check_execution(**execution_options)
        preview.parent.mkdir(parents=True, exist_ok=True)
        _publish_directory(pages, preview)
        return result
