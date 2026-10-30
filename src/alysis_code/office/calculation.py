"""Recalculate a private XLSX copy and copy only its result caches back.

The converter never saves over the original. Original formulas, styles and other
package parts survive; conversion is used solely as a calculation engine.
"""

from __future__ import annotations

import copy
import io
import posixpath
import tempfile
import zipfile
from pathlib import Path

from lxml import etree as ET

from alysis_code.office import renderer_path, sheets
from alysis_code.office.rendering import _check_renderable, _convert

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _xml(data: bytes):
    return ET.fromstring(data, parser=ET.XMLParser(resolve_entities=False, no_network=True))


def _worksheets(archive: zipfile.ZipFile) -> dict[str, tuple[str, object]]:
    relationships = {
        item.get("Id"): item.get("Target")
        for item in _xml(archive.read("xl/_rels/workbook.xml.rels"))
    }
    result = {}
    for sheet in _xml(archive.read("xl/workbook.xml")).find(f"{{{NS}}}sheets"):
        target = relationships[sheet.get(f"{{{REL}}}id")]
        part = posixpath.normpath(posixpath.join("xl", target)).lstrip("/")
        result[sheet.get("name")] = (part, _xml(archive.read(part)))
    return result


def _formulas(tree) -> dict[str, object]:
    result = {}
    for cell in tree.iter(f"{{{NS}}}c"):
        formula = cell.find(f"{{{NS}}}f")
        if formula is not None:
            if (
                formula.get("t", "normal") != "normal"
                or set(formula.attrib) - {"t", "ca", "aca"}
                or any(
                    formula.get(flag, "false") not in {"0", "1", "false", "true"}
                    for flag in ("ca", "aca")
                )
                or not formula.text
            ):
                raise ValueError(
                    "Recalculation currently requires ordinary cell formulas; shared/array formulas are unsupported."
                )
            sheets._formula("=" + formula.text)
            result[cell.get("r")] = cell
    return result


def _write_parts(original: bytes, changed: dict[str, bytes], output: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(output, "w") as target:
        target.comment = source.comment
        for entry in source.infolist():
            target.writestr(entry, changed.get(entry.filename, source.read(entry.filename)))


def recalculate(source: Path, output: Path, root: Path, **conversion_options) -> dict:
    executable = renderer_path()
    if executable is None:
        raise ValueError(
            "Spreadsheet recalculation is unavailable: install LibreOffice or configure ALYSIS_OFFICE_SOFFICE."
        )
    original = sheets._snapshot(source)
    # Editing rewrites a workbook through openpyxl, so its fidelity restrictions
    # are stricter than calculation. Here every original package part survives;
    # only ordinary cell-result caches are replaced. Print titles, custom
    # properties and formatting must not prevent a fresh calculation.
    sheets._package(original)
    _check_renderable(source)
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        originals = _worksheets(archive)
    expected = {name: _formulas(tree) for name, (_, tree) in originals.items()}
    cleared = {}
    for part, tree in originals.values():
        clone = copy.deepcopy(tree)
        for cell in _formulas(clone).values():
            for value in cell.findall(f"{{{NS}}}v"):
                cell.remove(value)
        cleared[part] = ET.tostring(clone, xml_declaration=True, encoding="UTF-8")
    with tempfile.TemporaryDirectory(prefix=".alysis-office-calc-", dir=root) as temporary:
        stage = Path(temporary)
        calculation_input = stage / "input.xlsx"
        _write_parts(original, cleared, calculation_input)
        converted = stage / "converted"
        converted.mkdir()
        _convert(
            executable,
            calculation_input,
            converted,
            stage / "profile",
            target_format="xlsx:Calc MS Excel 2007 XML",
            **conversion_options,
        )
        calculated = converted / "input.xlsx"
        if not calculated.is_file() or calculated.is_symlink():
            raise ValueError("Spreadsheet calculator did not produce its expected workbook.")
        # Bound and validate the converter output before reading any XML caches.
        sheets.inspect(calculated)
        with zipfile.ZipFile(calculated) as archive:
            results = _worksheets(archive)
        if set(results) != set(originals):
            raise ValueError("Calculation changed the worksheet set; output was not published.")
        changed, failures, values = {}, [], []
        formula_count = 0
        for name, (part, tree) in originals.items():
            result_tree = results[name][1]
            result_cells = {c.get("r"): c for c in result_tree.iter(f"{{{NS}}}c")}
            for reference, cell in expected[name].items():
                result = result_cells.get(reference)
                value = None if result is None else result.find(f"{{{NS}}}v")
                if result is None or result.find(f"{{{NS}}}f") is None or value is None:
                    raise ValueError(
                        f"Calculation did not return a formula result for {name}!{reference}; output was not published."
                    )
                # A shared-string index belongs to the converter's string table,
                # which is deliberately not copied over the original package.
                if result.get("t", "n") not in {"n", "b", "e", "str"}:
                    raise ValueError(
                        f"Calculation returned an unsupported cache type for {name}!{reference}; output was not published."
                    )
                for old_value in cell.findall(f"{{{NS}}}v"):
                    cell.remove(old_value)
                cell.append(copy.deepcopy(value))
                cell.attrib.pop("t", None)
                if result.get("t") is not None:
                    cell.set("t", result.get("t"))
                record = {
                    "sheet": name,
                    "cell": reference,
                    "type": result.get("t", "n"),
                    "value": value.text or "",
                }
                formula_count += 1
                if record["type"] == "e":
                    failures.append(record)
                if len(values) < sheets.MAX_PREVIEW_CELLS:
                    values.append(record)
            if expected[name]:
                changed[part] = ET.tostring(tree, xml_declaration=True, encoding="UTF-8")
        _write_parts(original, changed, output)
    return {
        "state": "failed" if failures else "passed",
        "engine": "LibreOffice",
        "basis": "Fresh profile, forced recalculation, input caches removed; only computed caches copied into the original package.",
        "formula_count": formula_count,
        "error_count": len(failures),
        "errors": failures[: sheets.MAX_PREVIEW_CELLS],
        "results": values,
        "results_truncated": formula_count > len(values),
        "limitations": "Checks ordinary cell formula errors and cache completeness, not business requirements. Chart caches and other embedded content are preserved, not refreshed. External content is disabled; unsupported functions may produce errors.",
    }
