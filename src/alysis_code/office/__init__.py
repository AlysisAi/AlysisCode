"""Local editable Office artifacts; heavy libraries load only for an operation."""

from __future__ import annotations

import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

FORMATS = {
    "docx": ("word", ("docx", "lxml")),
    "xlsx": ("sheets", ("openpyxl", "xlsxwriter")),
    "pptx": ("slides", ("pptx", "PIL", "lxml")),
}


def renderer_path() -> Path | None:
    """Find an installed converter without launching or downloading anything."""
    configured = os.environ.get("ALYSIS_OFFICE_SOFFICE", "").strip()
    if configured:
        path = Path(configured)
        return path if path.is_absolute() and path.is_file() and os.access(path, os.X_OK) else None
    candidates = [
        Path(found) for name in ("soffice", "libreoffice") if (found := shutil.which(name))
    ]
    if sys.platform == "darwin":
        candidates.extend(
            base / "LibreOffice.app/Contents/MacOS/soffice"
            for base in (Path("/Applications"), Path.home() / "Applications")
        )
    return next(
        (path.resolve() for path in candidates if path.is_file() and os.access(path, os.X_OK)), None
    )


def office_capability_status() -> dict:
    argv = [sys.executable, "-m", "alysis_code.office"]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    formats = {}
    for name, (_, modules) in FORMATS.items():
        missing = [module for module in modules if importlib.util.find_spec(module) is None]
        formats[name] = {"state": "unavailable" if missing else "ready", "missing_modules": missing}
    preview_ready = sys.platform in {"darwin", "linux"} and (
        renderer_path() is not None and importlib.util.find_spec("pypdfium2") is not None
    )
    calculation_ready = (
        sys.platform in {"darwin", "linux"}
        and renderer_path() is not None
        and all(importlib.util.find_spec(module) is not None for module in ("openpyxl", "lxml"))
    )
    return {
        "state": "ready"
        if all(item["state"] == "ready" for item in formats.values())
        else "partial",
        "basis": "Dependency presence only; every output is reopened and checked per operation.",
        "command": command,
        "formats": formats,
        "describe_example": command + " describe --format docx",
        "create_example": command
        + ' create --format docx --spec "request.json" --output "report.docx"',
        "inspect_example": command + ' inspect --source "report.docx"',
        "edit_example": command
        + ' edit --source "report.docx" --spec "edits.json" --output "revision.docx"',
        "preview": {
            "state": "configured" if preview_ready else "unavailable",
            "basis": "Installed LibreOffice plus PDFium; successful conversion still requires page review.",
            "configuration": "Uses LibreOffice on PATH or in standard macOS Applications folders. ALYSIS_OFFICE_SOFFICE overrides discovery with an absolute executable path.",
            "render_example": command
            + ' render --source "report.docx" --preview-dir "scratch/office-review"',
        },
        "calculation": {
            "state": "configured" if calculation_ready else "unavailable",
            "basis": "LibreOffice recalculates a private copy with active/external content disabled; only formula result caches are copied into a new workbook.",
            "recalculate_example": command
            + ' recalculate --source "model.xlsx" --output "model-calculated.xlsx"',
            "guidance": "Check calculation.state, errors and expected business results. A successful inspection or an old cached value is not proof of calculation. Recalculate again after changing inputs or formulas.",
        },
        "guidance": "Use describe for the selected format's exact JSON schema and example, then use this exact interpreter through shell_run. Save editable files in the requested output folder. Inspect before editing and use source/selection hashes. Originals are preserved; choose a new output filename for a revision. When previews are configured, render then inspect every returned page image with asset_view. No automatic installs or converter launches during capability discovery. Existing shell and filesystem permissions still apply.",
        "limits": "Bounded DOCX/XLSX/PPTX operations, not arbitrary Office fidelity. Unsupported edits fail explicitly. Excel formulas are not calculated by these Python libraries; use the separate LibreOffice recalculation command. Inspection alone never verifies existing caches. Rendering and calculation require installed LibreOffice. No macros.",
    }
