"""Exercise an editable workbook's formulas with one hypothetical source change."""

from __future__ import annotations

import hashlib
import io
import math
import tempfile
import zipfile
from pathlib import Path

from lxml import etree as ET

from . import calculation, sheets
from .calculation import NS


def _address(record, fields):
    sheets._object(record, {"sheet", "cell", *fields}, "input check cell")
    if set(record) != {"sheet", "cell", *fields}:
        raise ValueError("Each input check cell requires sheet, cell and its test values")
    for key in fields:
        value = record[key]
        if isinstance(value, dict) or (isinstance(value, str) and len(value) > 160):
            raise ValueError("Input checks take literal values of at most 160 characters")
        sheets._cell(value)
    sheets._position(record["cell"])
    return sheets._sheet_name(record["sheet"]), record["cell"]


def _matches(left, right):
    if type(left) in (float, int) and type(right) in (float, int):
        return math.isclose(left, right, rel_tol=0, abs_tol=1e-9)
    return type(left) is type(right) and left == right


def _substitute(cell, value):
    for child in list(cell):
        if child.tag in {f"{{{NS}}}is", f"{{{NS}}}v"}:
            cell.remove(child)
    cell.attrib.pop("t", None)
    if isinstance(value, str):
        cell.set("t", "inlineStr")
        text = ET.SubElement(ET.SubElement(cell, f"{{{NS}}}is"), f"{{{NS}}}t")
        text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        text.text = value
    elif value is not None:
        if isinstance(value, bool):
            cell.set("t", "b")
            value = int(value)
        ET.SubElement(cell, f"{{{NS}}}v").text = str(value)


def verify(
    source: Path,
    spec,
    root: Path,
    *,
    fresh_baseline=False,
    execution_deadline=None,
    cancel_check=None,
):
    """Always discard test bytes. Accepted checks demonstrate only the declared case."""
    sheets._object(spec, {"input", "expect"}, "input_check")
    source_key = _address(spec.get("input"), {"value"})
    cases = spec.get("expect")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 8:
        raise ValueError("input_check.expect must contain 1..8 expected formula results")
    targets = {_address(case, {"before", "after"}): case for case in cases}
    if len(targets) != len(cases) or source_key in targets:
        raise ValueError("Input and expected formula addresses must be distinct")
    if all(_matches(case["before"], case["after"]) for case in cases):
        raise ValueError("Choose a test change expected to move at least one reported formula")
    original = sheets._snapshot(source)
    sheets._package(original)
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        worksheets = calculation._worksheets(archive)
    addresses = {}
    input_cell = None
    for key in [source_key, *targets]:
        name, ref = key
        if name not in worksheets:
            raise ValueError("Input check must name existing worksheets exactly")
        tree = worksheets[name][1]
        cell = tree.find(f".//{{{NS}}}sheetData/{{{NS}}}row/{{{NS}}}c[@r='{ref}']")
        if cell is None or (cell.find(f"{{{NS}}}f") is None) != (key == source_key):
            raise ValueError(
                f"Choose an existing literal input and formula result cells; check {name}!{ref}"
            )
        addresses.setdefault(name, set()).add(ref)
        if key == source_key:
            input_cell = cell
    source_hash = hashlib.sha256(original).hexdigest()
    if calculation.renderer_path() is None:
        return {
            "state": "unavailable",
            "source_sha256": source_hash,
            "detail": "The installed runtime could not calculate the input change.",
        }
    from alysis_code.execution_deadline import deadline_timeout_or_raise

    def calculate(source, output, directory):
        return calculation.recalculate(
            source,
            output,
            directory,
            timeout_seconds=deadline_timeout_or_raise(
                execution_deadline, 90.0, reserve_seconds=1.0, operation="document_operation"
            ),
            cancel_check=cancel_check,
        )

    with tempfile.TemporaryDirectory(prefix=".alysis-input-check-", dir=root) as temp:
        stage = Path(temp)
        baseline = source
        if not fresh_baseline:
            baseline = stage / "baseline.xlsx"
            result = calculate(source, baseline, stage)
            if result["state"] != "passed":
                return {
                    "state": "failed",
                    "source_sha256": source_hash,
                    "detail": "Baseline recalculation contained errors.",
                }
        before = sheets.saved_values(baseline, addresses)
        if source_key not in before or any(key not in before for key in targets):
            raise ValueError("The calculator could not read the selected baseline cells")
        new_value = spec["input"]["value"]
        old_value = before[source_key].value
        if _matches(old_value, new_value):
            raise ValueError("The hypothetical input must differ from the authoritative value")
        _substitute(input_cell, new_value)
        part, tree = worksheets[source_key[0]]
        changed, calculated = stage / "input.xlsx", stage / "output.xlsx"
        calculation._write_parts(
            original, {part: ET.tostring(tree, xml_declaration=True, encoding="UTF-8")}, changed
        )
        result = calculate(changed, calculated, stage)
        after = sheets.saved_values(calculated, addresses)
        if any(key not in after for key in targets):
            raise ValueError("The calculator could not read the selected test result cells")
        checks = []
        for key, expected in targets.items():
            previous, current = before[key], after[key]
            passed = (
                previous.data_type != "e"
                and current.data_type != "e"
                and _matches(sheets._json_value(previous.value), expected["before"])
                and _matches(sheets._json_value(current.value), expected["after"])
            )
            checks.append(
                {
                    "sheet": key[0],
                    "cell": key[1],
                    "previous": sheets._evidence_cell(previous),
                    "current": sheets._evidence_cell(current),
                    "expected_before": expected["before"],
                    "expected_after": expected["after"],
                    "passed": passed,
                }
            )
        passed = result["state"] == "passed" and all(record["passed"] for record in checks)
    return {
        "state": "passed" if passed else "failed",
        "source_sha256": source_hash,
        "input": {
            "sheet": source_key[0],
            "cell": source_key[1],
            "original": sheets._evidence_cell(before[source_key]),
            "test_value": new_value,
        },
        "outputs": checks,
        "detail": "Only the declared source input was changed in a disposable copy. The delivered workbook retains the actual data.",
    }
