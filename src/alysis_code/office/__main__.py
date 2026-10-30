"""Bounded Office helpers exposed through the ordinary authorized shell tool."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from alysis_code.documents.__main__ import _path
from alysis_code.office import FORMATS

MAX_SPEC = 2 * 1024 * 1024
MAX_FILE = 50 * 1024 * 1024
MAX_EXPANDED = 100 * 1024 * 1024


def _read(path: Path, limit: int) -> bytes:
    # Inputs are read into private snapshots, never modified in place. Checkpoint
    # storage can hold another hard link to an otherwise ordinary workspace file.
    if not path.is_file():
        raise ValueError("Input must be an existing regular file.")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Input exceeds the supported size limit.")
    return data


def _spec(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Request JSON contains duplicate keys.")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("Request JSON must use finite numbers.")

    value = json.loads(
        _read(path, MAX_SPEC).decode("utf-8"),
        object_pairs_hook=unique,
        parse_constant=invalid_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("Request JSON must be an object.")
    return value


def _package(data: bytes) -> None:
    """Bound package expansion before passing a snapshot to format libraries."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 5000 or sum(entry.file_size for entry in entries) > MAX_EXPANDED:
            raise ValueError("Office package exceeds the supported expansion limit.")
        names = set()
        for entry in entries:
            name = entry.filename
            if (
                name in names
                or "\\" in name
                or name.startswith("/")
                or ".." in PurePosixPath(name).parts
            ):
                raise ValueError("Office package contains duplicate or invalid entries.")
            names.add(name)
            if entry.flag_bits & 1 or entry.file_size > 20 * 1024 * 1024:
                raise ValueError("Encrypted or oversized Office parts are unsupported.")
            if "vbaproject" in name.lower():
                raise ValueError("Macro-enabled Office files are unsupported.")
            if name.endswith((".xml", ".rels")):
                xml = archive.read(entry)
                if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                    raise ValueError("Office XML entities are unsupported.")
        if "[Content_Types].xml" not in names:
            raise ValueError("Input is not an Office Open XML package.")


def _adapter(format_name: str):
    return importlib.import_module("alysis_code.office." + FORMATS[format_name][0])


def _format(path: Path) -> str:
    name = path.suffix.lower().removeprefix(".")
    if name not in FORMATS:
        raise ValueError("Only .docx, .xlsx and .pptx files are supported.")
    return name


def run(args) -> dict:
    if args.command == "describe":
        adapter = _adapter(args.format)
        return {
            "format": args.format,
            "guide": adapter.GUIDE,
            "example": adapter.example_spec()
            if hasattr(adapter, "example_spec")
            else adapter.GUIDE["create"],
        }
    root = Path(args.workspace).resolve(strict=True)
    source = _path(root, args.source) if getattr(args, "source", None) else None
    spec_path = _path(root, args.spec) if getattr(args, "spec", None) else None
    spec = _spec(spec_path) if spec_path else None
    format_name = args.format if args.command == "create" else _format(source)
    if args.command == "recalculate" and format_name != "xlsx":
        raise ValueError("Recalculation requires an .xlsx workbook.")
    output = _path(root, args.output) if getattr(args, "output", None) else None
    if output is not None:
        if _format(output) != format_name:
            raise ValueError("Output extension must match the document format.")
        if output in (source, spec_path) or output.exists():
            raise ValueError(
                "Choose a new output path; existing files and originals are never overwritten."
            )
    data = _read(source, MAX_FILE) if source else None
    if data is not None:
        _package(data)
    source_hash = hashlib.sha256(data).hexdigest() if data is not None else None
    if args.command == "edit" and spec.get("expected_source_sha256") != source_hash:
        raise ValueError("Source changed: inspect it again and use its expected_source_sha256.")
    with tempfile.TemporaryDirectory(prefix=".alysis-office-", dir=root) as temp:
        stage = Path(temp)
        snapshot = stage / ("source." + format_name)
        if data is not None:
            snapshot.write_bytes(data)
        if args.command == "render":
            from alysis_code.office.rendering import render

            return render(snapshot, _path(root, args.preview_dir), root, source_hash)
        adapter = _adapter(format_name)
        if args.command == "inspect":
            return {
                "status": "inspected",
                "format": format_name,
                "source": str(source.relative_to(root)),
                "source_sha256": source_hash,
                "content": adapter.inspect(snapshot),
            }
        staged_output = stage / ("result." + format_name)
        if args.command == "create":
            if format_name == "pptx":
                details = adapter.create(
                    spec, staged_output, root, read_guard=getattr(args, "read_guard", None)
                )
            else:
                details = adapter.create(spec, staged_output, root)
        elif args.command == "recalculate":
            from alysis_code.execution_deadline import deadline_timeout_or_raise
            from alysis_code.office.calculation import recalculate

            details = recalculate(
                snapshot,
                staged_output,
                root,
                timeout_seconds=deadline_timeout_or_raise(
                    getattr(args, "execution_deadline", None),
                    90.0,
                    reserve_seconds=1.0,
                    operation="document_operation",
                ),
                cancel_check=getattr(args, "cancel_check", None),
            )
        else:
            details = adapter.edit(snapshot, spec, staged_output, root)
        produced = _read(staged_output, MAX_FILE)
        _package(produced)
        inspected = adapter.inspect(staged_output)
        if args.command == "recalculate":
            inspected.update(
                formula_results="recalculated",
                cached_values="recalculated_this_operation",
                needs_recalculation=details["state"] != "passed",
            )
        if (
            source is not None
            and hashlib.sha256(_read(source, MAX_FILE)).hexdigest() != source_hash
        ):
            raise ValueError("Source changed during the operation; output was not published.")
        _path(root, str(output))
        output.parent.mkdir(parents=True, exist_ok=True)
        os.link(staged_output, output)  # exclusive, atomic; no silent replacement
        return {
            "status": {"create": "created", "edit": "edited", "recalculate": "recalculated"}[
                args.command
            ],
            "format": format_name,
            "output": str(output.relative_to(root)),
            "sha256": hashlib.sha256(produced).hexdigest(),
            "source_sha256": source_hash,
            "details": {key: value for key, value in details.items() if key not in inspected},
            "content": inspected,
            **({"calculation": details} if args.command == "recalculate" else {}),
            "preview": {"state": "not_rendered", "visual_review": "not_performed"},
            "next": "Render and inspect all page previews when a renderer is configured.",
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create, inspect, edit and render local Office files."
    )
    parser.add_argument(
        "--workspace", default=os.getcwd(), help="Authorized workspace (default cwd)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    describe = sub.add_parser("describe", help="Exact request schema, example and limits.")
    describe.add_argument("--format", choices=FORMATS, required=True)
    create = sub.add_parser("create")
    create.add_argument("--format", choices=FORMATS, required=True)
    create.add_argument("--spec", required=True)
    create.add_argument("--output", required=True)
    for command in ("inspect", "edit", "render", "recalculate"):
        item = sub.add_parser(command)
        item.add_argument("--source", required=True)
        if command == "edit":
            item.add_argument("--spec", required=True)
        if command in {"edit", "recalculate"}:
            item.add_argument("--output", required=True)
        if command == "render":
            item.add_argument(
                "--preview-dir",
                required=True,
                help="New scratch directory for PDF and every page PNG.",
            )
    args = parser.parse_args()
    try:
        result = run(args)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return int(result.get("calculation", {}).get("state") == "failed")
    except (ValueError, OSError, ImportError, zipfile.BadZipFile, RecursionError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"status": "cancelled", "error": "Office operation cancelled."}))
        return 130


if __name__ == "__main__":
    sys.exit(main())
