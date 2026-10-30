"""Bounded document operations in the engine runtime, independent of shell containers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from . import FORMATS


def native_capability_status(format_name: str) -> dict:
    """Expose executable agent routes, without host-only shell examples."""
    from alysis_code.documents import pdf_capability_status
    from alysis_code.office import office_capability_status

    capability = pdf_capability_status() if format_name == "pdf" else office_capability_status()
    for key in (
        "command",
        "describe_example",
        "create_example",
        "inspect_example",
        "edit_example",
        "render_example",
        "extract_example",
    ):
        capability.pop(key, None)
    if "preview" in capability:
        capability["preview"].pop("render_example", None)
        capability["preview"]["tool_example"] = {"tool": "artifact_check", "path": "report.docx"}
    if "calculation" in capability:
        capability["calculation"].pop("recalculate_example", None)
        capability["calculation"]["tool_example"] = {
            "tool": "document_operation",
            "action": "recalculate",
            "source": "scratch/model-draft.xlsx",
            "output": "model.xlsx",
        }
    capability["tool"] = "document_operation"
    capability["tool_example"] = {"action": "describe", "format": format_name}
    capability["guidance"] = (
        "Check the reported dependency state. Use document_operation for supported creation, inspection, "
        "editing and recalculation in the installed engine environment. To render and check the output, "
        "call artifact_check on its path, then inspect every returned page with asset_view. "
        "Do not invoke the engine's host interpreter through shell_run: its container can have a different "
        "filesystem and libraries. Unsupported features require an explicitly available library or connector. "
        "Workspace and file permissions remain enforced."
    )
    return capability


def run_operation(
    root: Path,
    arguments: dict,
    *,
    read_guard,
    write_guard,
    execution_deadline=None,
    cancel_check=None,
) -> dict:
    action = arguments.get("action")
    if action not in {"describe", "create", "inspect", "edit", "recalculate"}:
        raise ValueError("Use describe, create, inspect, edit or recalculate.")
    allowed = {"action", "format", "source", "spec", "output", "title"}
    if set(arguments) - allowed:
        raise ValueError("Unknown document operation fields.")
    for key, value in arguments.items():
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string.")
    format_name = arguments.get("format")
    if action not in {"describe", "create"}:
        source = arguments.get("source")
        if not source:
            raise ValueError("source is required for this operation.")
        format_name = Path(source).suffix.lower().removeprefix(".")
    if format_name not in {*FORMATS, "pdf"}:
        raise ValueError("Supported formats are docx, xlsx, pptx and pdf.")
    if format_name == "pdf" and action not in {"describe", "create"}:
        raise ValueError(
            "For PDF review use artifact_check and asset_view; retain Markdown for revisions."
        )
    required = {"output"} if action in {"create", "edit", "recalculate"} else set()
    if action in {"create", "edit"}:
        required.add("source" if format_name == "pdf" else "spec")
    if any(not arguments.get(key) for key in required):
        raise ValueError("Required fields: " + ", ".join(sorted(required)))

    from alysis_code.documents.__main__ import _path

    for key in ("source", "spec", "output"):
        if value := arguments.get(key):
            _path(root, value)  # Reject traversal and symlinks before any read or approval.
    for key in ("source", "spec"):
        if value := arguments.get(key):
            read_guard(value)
    if output := arguments.get("output"):
        if _path(root, output).exists():
            raise ValueError("Choose a new output path; existing files are never overwritten.")
        write_guard(output)

    if format_name == "pdf":
        if action == "describe":
            return {
                "format": "pdf",
                "guide": native_capability_status("pdf"),
                "example": {
                    "action": "create",
                    "format": "pdf",
                    "source": "draft.md",
                    "output": "report.pdf",
                },
            }
        from alysis_code.documents.__main__ import run

        preview_dir = f"Task reviews/{uuid4().hex}"
        _path(root, preview_dir)
        write_guard(preview_dir)
        return run(
            SimpleNamespace(
                workspace=str(root),
                command="create",
                source=arguments["source"],
                output=arguments["output"],
                title=arguments.get("title", ""),
                overwrite=False,
                preview_dir=preview_dir,
                execution_deadline=execution_deadline,
                cancel_check=cancel_check,
            )
        )

    from .__main__ import run

    result = run(
        SimpleNamespace(
            workspace=str(root),
            command=action,
            format=format_name,
            source=arguments.get("source"),
            spec=arguments.get("spec"),
            output=arguments.get("output"),
            read_guard=read_guard,
            execution_deadline=execution_deadline,
            cancel_check=cancel_check,
        )
    )
    if action in {"create", "edit", "recalculate"}:
        result["next"] = (
            "Call artifact_check on this output path to check and render the exact file. "
            "Inspect every returned page image with asset_view; do not run a separate shell renderer."
        )
    return result
