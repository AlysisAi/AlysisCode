from __future__ import annotations

import hashlib
import json
import socket
import zipfile
from contextlib import closing
from copy import copy
from datetime import datetime
from itertools import pairwise
from pathlib import Path

import openpyxl
import pytest
import xlsxwriter

from alysis_code.artifact_checks import ArtifactChecks
from alysis_code.office import sheets


def create(root: Path, spec: dict | None = None) -> tuple[Path, dict]:
    source = root / "source.xlsx"
    result = sheets.create(spec or sheets.example_spec(), source, root)
    return source, result


def edit(root: Path, source: Path, digest: str, changes: list) -> tuple[Path, dict]:
    output = root / "revised.xlsx"
    return output, sheets.edit(
        source, {"expected_source_sha256": digest, "changes": changes}, output, root
    )


def replace_part(path: Path, name: str, transform) -> None:
    with zipfile.ZipFile(path) as archive:
        parts = {item.filename: archive.read(item) for item in archive.infolist()}
    parts[name] = transform(parts.get(name, b""))
    with zipfile.ZipFile(path, "w") as archive:
        for item, data in parts.items():
            archive.writestr(item, data)


@pytest.mark.parametrize(
    "extra",
    [
        {"merge_ranges": ["A1:A2"]},
        {"merge_ranges": ["A1:C1", "B1:C1"]},
        {"merge_ranges": ["A1:C1"], "cells": {"B1": "0002"}},
        {"merge_ranges": ["A1:C1"], "cells": {"C1": {"formula": "=1+2"}}},
        {"merge_ranges": ["A1:C1"], "cells": {"B1": {"value": None, "format": {"bold": True}}}},
        {"merge_ranges": ["A1:C1"], "tables": [{"name": "Cases", "range": "A1:A2"}]},
        {"row_heights": {"1": False}},
        {"row_heights": {"9999": 16}},
        {"cells": {"C1": {"value": "x", "format": {"font_size": 1000}}}},
    ],
)
def test_unsafe_or_destructive_heading_geometry_fails_before_publication(tmp_path, extra):
    with pytest.raises(ValueError):
        create(tmp_path, {"sheets": [{"name": "Å", "rows": [["Étapes"], [2]], **extra}]})
    assert not (tmp_path / "source.xlsx").exists()


def test_merged_multiline_heading_gets_native_height_and_leaves_header_cells_sortable(tmp_path):
    spec = {
        "sheets": [
            {
                "name": "Étapes",
                "rows": [
                    [
                        {
                            "value": "Trois contrôles\nrestent à planifier",
                            "format": {"font_size": 18, "wrap": True},
                        }
                    ],
                    ["Lot", "Étape"],
                    ["0002", "ouvert"],
                ],
                "column_widths": [13, 18],
                "merge_ranges": ["A1:B1"],
                "tables": [{"name": "Lots", "range": "A2:B3"}],
            }
        ]
    }
    path, _ = create(tmp_path, spec)
    with closing(openpyxl.load_workbook(path)) as book:
        assert book.active.row_dimensions[1].height >= 48
        assert book.active["A1"].font.sz == 18
        assert str(book.active.merged_cells) == "A1:B1"
        assert book.active.tables["Lots"].autoFilter.ref == "A2:B3"
        assert book.active["A3"].value == "0002"


def test_custom_heading_height_preserves_separation_between_native_charts(tmp_path):
    chart = {
        "chart": "column",
        "categories": "A2:A3",
        "series": [{"name": "Heures", "values": "B2:B3"}],
    }
    path, _ = create(
        tmp_path,
        {
            "sheets": [
                {
                    "name": "Évolution",
                    "rows": [["Secteurs", "Heures"], ["Q1", 3], ["Q2", 7]],
                    "row_heights": {"1": 72},
                    "charts": [chart] * 5,
                }
            ]
        },
    )
    with closing(openpyxl.load_workbook(path)) as book:
        ws = book.active
        anchors = [ch.anchor for ch in ws._charts]
        breaks = [br.id for br in ws.row_breaks.brk]
        assert breaks
        for earlier, later in pairwise(anchors):
            assert earlier.to.row < later._from.row
        for anchor in anchors:
            assert all(not anchor._from.row < row <= anchor.to.row for row in breaks)


@pytest.mark.parametrize(
    "layout",
    [
        {"merge_ranges": ["A2:B2"]},
        {"merge_ranges": ["B1:C1"]},
        {"merge_ranges": ["A3:B3"]},
        {"extend_print_area": "A1:B2"},
    ],
)
def test_edit_rejects_overlapping_layout_and_shrinking_print_area(tmp_path, layout):
    path, saved = create(
        tmp_path,
        {
            "sheets": [
                {
                    "name": "Données",
                    "rows": [["Examen"], ["Équipe", "Temps"], ["Est", 14]],
                    "merge_ranges": ["A1:B1"],
                    "page_setup": {"print_area": "A1:B3"},
                    "tables": [{"name": "Durees", "range": "A2:B3"}],
                }
            ]
        },
    )
    with pytest.raises(ValueError):
        edit(tmp_path, path, saved["source_sha256"], [{"sheet": "Données", **layout}])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == saved["source_sha256"]
    assert not (tmp_path / "revised.xlsx").exists()


def test_revision_examples_are_bounded_and_missing_previous_formula_display_is_unknown(tmp_path):
    for name, offset in [("source.xlsx", 0), ("final.xlsx", 1)]:
        with xlsxwriter.Workbook(tmp_path / name) as book:
            ws = book.add_worksheet("Mesure")
            unit = book.add_format({"num_format": '0.00 "h"'})
            for row in range(17):
                ws.write_formula(
                    row, 0, "=1+1", unit, "" if row == 0 and not offset else row + offset
                )
    result = sheets.formula_changes(
        tmp_path / "source.xlsx",
        tmp_path / "final.xlsx",
        {
            "formula_count": 17,
            "results": [{"sheet": "Mesure", "cell": f"A{row}"} for row in range(1, 18)],
        },
    )
    assert result["source_value_unavailable"] == 1 and result["changed"] == result["compared"] == 16
    assert result["unexamined"] == 0 and result["examples_truncated"]
    assert len(result["examples"]) == sheets.MAX_CHANGE_EXAMPLES
    assert result["examples"][0] == {
        "sheet": "Mesure",
        "cell": "A2",
        "numeric_difference": "1",
        "previous": {"type": "n", "value": 1, "number_format": '0.00 "h"'},
        "current": {"type": "n", "value": 2, "number_format": '0.00 "h"'},
    }


def test_overflowing_filled_heading_can_be_wrapped_without_replacing_cell_contents(tmp_path):
    title = "Disponibilité des bénévoles pour les permanences du samedi"
    source = tmp_path / "source.xlsx"
    with xlsxwriter.Workbook(source) as book:
        sheet = book.add_worksheet("Équipe")
        sheet.set_column("A:B", 14)
        header = book.add_format({"bold": True, "bg_color": "#183153", "font_color": "#FFFFFF"})
        sheet.write("A1", title, header)
        sheet.merge_range("A2:B2", "Ordre du jour", header)
        sheet.write_formula("A3", "=SUM(2,3)")
        sheet.set_row(3, None, None, {"hidden": True})
        sheet.write("A4", title, header)
    result = sheets.inspect(source)
    assert result["heading_fit"] == {"count": 1, "examples": [{"sheet": "Équipe", "cell": "A1"}]}
    revised, inspection = edit(
        tmp_path,
        source,
        result["source_sha256"],
        [
            {"sheet": "Équipe", "cell": "A1", "format": {"wrap": True}},
            {"sheet": "Équipe", "cell": "A3", "format": {"bold": True}},
        ],
    )
    assert inspection["heading_fit"]["count"] == 0
    with closing(openpyxl.load_workbook(revised)) as book:
        assert book.active["A1"].value == title
        assert book.active["A3"].data_type == "f" and book.active["A3"].value == "=SUM(2,3)"
    with pytest.raises(ValueError, match="nonempty format"):
        edit(tmp_path, source, result["source_sha256"], [{"sheet": "Équipe", "cell": "A1"}])


def test_table_header_wrap_survives_table_creation_and_inspection_locates_unwrapped_header(
    tmp_path,
):
    long_header = "Différence (compté − théorique)"
    source, result = create(
        tmp_path,
        {
            "sheets": [
                {
                    "name": "Écarts",
                    "rows": [["Inventaire"], ["Référence", long_header], ["0007", -3]],
                    "column_widths": [16, 24],
                    "tables": [{"name": "Inventaire", "range": "A2:B3"}],
                }
            ]
        },
    )
    assert result["heading_fit"]["count"] == 0
    with closing(openpyxl.load_workbook(source)) as book:
        sheet = book.active
        assert sheet["B2"].alignment.wrap_text and not sheet["A2"].alignment.wrap_text
        assert sheet["B2"].value == long_header and sheet.tables["Inventaire"].ref == "A2:B3"
        assert sheet["B2"].fill.patternType is None  # The table's theme still supplies the fill.
    _, inspection = edit(
        tmp_path,
        source,
        result["source_sha256"],
        [
            {"sheet": "Écarts", "cell": "B2", "format": {"wrap": False}},
        ],
    )
    assert inspection["heading_fit"] == {
        "count": 1,
        "examples": [{"sheet": "Écarts", "cell": "B2"}],
    }


def test_header_contrast_respects_direct_colors_and_native_table_theme(tmp_path, monkeypatch):
    def header(label, **style):
        return {"value": label, "format": style}

    source, result = create(
        tmp_path,
        {
            "sheets": [
                {
                    "name": "Stock",
                    "header_row": True,
                    "rows": [
                        [header("Stock report", fill="#FFF1BC")],
                        [
                            header("Pale", fill="#D9EAF7"),
                            header("Dark", fill="#17365D"),
                            header("Explicit", fill="#D9EAF7", font_color="#FFFFFF"),
                            "Themed",
                        ],
                        [1, 2, 3, 4],
                    ],
                    "tables": [{"name": "Inventory", "range": "A2:D3"}],
                }
            ]
        },
    )
    with closing(openpyxl.load_workbook(source)) as book:
        sheet = book.active
        assert [sheet[c].font.color.rgb[-6:] for c in ("A1", "A2", "B2", "C2")] == [
            "000000",
            "000000",
            "FFFFFF",
            "FFFFFF",
        ]
        assert sheet["D2"].fill.patternType is None
        assert sheet["D2"].font.color.type == "theme"
        # A workbook authored before the correction can have a solid custom fill
        # while its native table supplies the header font.
        sheet["A2"].font = copy(sheet["D2"].font)
        legacy = tmp_path / "legacy.xlsx"
        book.save(legacy)
    assert result["header_contrast"]["count"] == 1
    issue = result["header_contrast"]["examples"][0]
    assert issue["cell"] == "C2" and issue["reason"] == "low_contrast"
    assert 1.20 < issue["ratio"] < 1.30
    monkeypatch.setattr("alysis_code.office.renderer_path", lambda: None)
    report = ArtifactChecks(tmp_path).check(source.name)
    assert report["headerContrast"] == result["header_contrast"]
    check = next(entry for entry in report["items"] if entry["id"] == "header_contrast")
    assert check["status"] == "not_checked" and "Stock!C2" in check["detail"]
    assert "formatting-only font_color" in check["detail"]
    inspected = sheets.inspect(legacy)
    assert inspected["header_contrast"]["examples"][0] == {
        "sheet": "Stock",
        "cell": "A2",
        "reason": "theme_dependent",
        "fill": "#D9EAF7",
    }
    _, repaired = edit(
        tmp_path,
        legacy,
        inspected["source_sha256"],
        [
            {"sheet": "Stock", "cell": cell, "format": {"font_color": "#17365D"}}
            for cell in ("A2", "C2")
        ],
    )
    assert repaired["header_contrast"]["count"] == 0


def test_print_coverage_can_extend_a_table_filter_and_print_area_without_changing_cells(tmp_path):
    spec = {
        "sheets": [
            {
                "name": "Écarts",
                "rows": [
                    ["Ref", "Attendu", "Compté"],
                    ["0007", 18, 15],
                    ["0042", 6, 8],
                    ["Q-021", 14, 10],
                    [],
                    ["Note", "Texte"],
                ],
                "tables": [{"name": "Inventaire", "range": "A1:C3"}],
                "page_setup": {"print_area": "A1:C3", "orientation": "landscape"},
            }
        ]
    }
    source, result = create(tmp_path, spec)
    finding = result["print_coverage"]["examples"][0]
    assert finding["first_outside_row"] == finding["through_row"] == 4
    assert finding["extended_table"] == finding["extended_print_area"] == "A1:C4"
    output, repaired = edit(
        tmp_path,
        source,
        result["source_sha256"],
        [{"sheet": "Écarts", "extend_printed_table": "Inventaire"}],
    )
    assert repaired["print_coverage"]["count"] == 0
    with closing(openpyxl.load_workbook(output)) as book:
        ws = book.active
        assert ws.tables["Inventaire"].ref == ws.tables["Inventaire"].autoFilter.ref == "A1:C4"
        assert str(ws.print_area).endswith("$A$1:$C$4")
        assert ws["A2"].value == "0007" and ws["A4"].value == "Q-021"
        assert ws["B6"].value == "Texte"
    assert sheets.inspect(source)["print_coverage"]["count"] == 1


def test_greek_create_inspect_edit_preserves_original_styles_tables_and_formulas(
    tmp_path, monkeypatch
):
    def no_network(*args, **kwargs):
        raise AssertionError("Spreadsheet adapter must remain local")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    source, result = create(tmp_path)
    original = source.read_bytes()
    assert result["formula_count"] == 1
    assert result["needs_recalculation"] is True
    assert result["formula_results"] == "not_calculated"
    assert result["cached_values"] == "not_read_or_trusted"
    assert result["visual_review"] == "not_performed"
    assert result["editable"] is True
    output, changed = edit(
        tmp_path,
        source,
        result["source_sha256"],
        [
            {"sheet": "Έσοδα", "cell": "B2", "value": 195.5},
            {"sheet": "Έσοδα", "cell": "B4", "formula": "=SUM(B2:B3)", "format": {"italic": True}},
        ],
    )
    assert source.read_bytes() == original
    assert changed["changes_applied"] == 2
    assert changed["original_sha256"] == hashlib.sha256(original).hexdigest()
    with closing(openpyxl.load_workbook(output)) as book:
        ws = book["Έσοδα"]
        assert ws["A3"].value == "Θεσσαλονίκη"
        assert ws["B2"].value == 195.5
        assert ws["B4"].value == "=SUM(B2:B3)"
        assert ws["B4"].number_format == '#,##0.00 "€"'
        assert ws["B4"].font.bold and ws["B4"].font.italic
        assert ws.freeze_panes == "A2"
        assert ws.tables["Revenue"].ref == "A1:B3"
        assert book.calculation.fullCalcOnLoad and book.calculation.forceFullCalc
    json.dumps(changed, ensure_ascii=False, allow_nan=False)


def test_only_explicit_formula_objects_make_formulas_or_links(tmp_path):
    literals = [
        "=1+1",
        "+1+1",
        "-1+1",
        "@SUM(A1:A2)",
        "https://example.com",
        "mailto:a@example.com",
        "#N/A",
    ]
    source, result = create(
        tmp_path, {"sheets": [{"name": "Data", "rows": [literals + [{"formula": "=1+1"}]]}]}
    )
    with closing(openpyxl.load_workbook(source)) as book:
        for column, literal in enumerate(literals, 1):
            cell = book.active.cell(1, column)
            assert cell.value == literal
            assert cell.data_type == "s"
            assert cell.hyperlink is None
        assert book.active["H1"].data_type == "f"
    output, edited = edit(
        tmp_path,
        source,
        result["source_sha256"],
        [
            {"sheet": "Data", "cell": "H1", "value": '=HYPERLINK("https://example.com")'},
            {"sheet": "Data", "cell": "A2", "value": "https://example.com"},
        ],
    )
    with closing(openpyxl.load_workbook(output)) as book:
        assert book.active["H1"].data_type == "s"
        assert book.active["A2"].hyperlink is None
    assert edited["formula_count"] == 0 and not edited["needs_recalculation"]


def test_formulas_never_return_cached_results_as_calculated(tmp_path):
    source = tmp_path / "source.xlsx"
    with xlsxwriter.Workbook(source) as book:
        book.add_worksheet("Data").write_formula("A1", "=2+2", None, 999)
    result = sheets.inspect(source)
    assert result["sheets"][0]["cells"][0]["formula"] == "=2+2"
    # The cached result is reported nowhere. The source hash is left out: it is random hex
    # that can contain "999" by chance.
    reported = {key: value for key, value in result.items() if key != "source_sha256"}
    assert "999" not in json.dumps(reported)
    assert result["needs_recalculation"]
    output, _ = edit(
        tmp_path, source, result["source_sha256"], [{"sheet": "Data", "cell": "A2", "value": 3}]
    )
    with closing(openpyxl.load_workbook(output, data_only=True)) as book:
        assert book.active["A1"].value is None


@pytest.mark.parametrize(
    "formula",
    [
        "=cmd|' /C calc'!A1",
        "='[other.xlsx]Sheet1'!A1",
        "=Revenue[Amount]",
        "=NamedRange",
        "=SUM(A1:A10001)",
        "={1,2}",
        "=#REF!",
        "=",
        "SUM(A1:A2)",
        "='Missing'!A1",
    ],
)
def test_unsafe_or_unsupported_explicit_formulas_fail_before_write(tmp_path, formula):
    with pytest.raises(ValueError):
        create(tmp_path, {"sheets": [{"name": "Data", "rows": [[{"formula": formula}]]}]})
    assert not (tmp_path / "source.xlsx").exists()


def test_local_cross_sheet_formula_works_with_greek_and_quoted_apostrophe(tmp_path):
    source, result = create(
        tmp_path,
        {
            "sheets": [
                {"name": "Πωλήσεις", "rows": [[4], [6]]},
                {"name": "O'Brien", "rows": [[9]]},
                {"name": "Σύνοψη", "rows": [[{"formula": "=SUM('Πωλήσεις'!A1:A2)+'O''Brien'!A1"}]]},
            ]
        },
    )
    assert result["formula_count"] == 1 and result["editable"]


def test_stale_hash_and_original_alias_are_rejected(tmp_path):
    source, result = create(tmp_path)
    original = source.read_bytes()
    change = [{"sheet": "Έσοδα", "cell": "B2", "value": 10}]
    with pytest.raises(ValueError, match="stale"):
        edit(tmp_path, source, "0" * 64, change)
    with pytest.raises(ValueError, match="expected_source_sha256"):
        sheets.edit(source, {"changes": change}, tmp_path / "other.xlsx", tmp_path)
    with pytest.raises(ValueError, match="separate output"):
        sheets.edit(
            source,
            {"expected_source_sha256": result["source_sha256"], "changes": change},
            source,
            tmp_path,
        )
    alias = tmp_path / "alias.xlsx"
    alias.hardlink_to(source)
    with pytest.raises(ValueError, match="separate output"):
        sheets.edit(
            source,
            {"expected_source_sha256": result["source_sha256"], "changes": change},
            alias,
            tmp_path,
        )
    assert original == source.read_bytes()
    assert not (tmp_path / "revised.xlsx").exists()


def test_source_changed_during_validation_fails_without_output(tmp_path, monkeypatch):
    source, result = create(tmp_path)
    load = sheets._load

    def change_after_read(data):
        source.write_bytes(source.read_bytes() + b"changed")
        return load(data)

    monkeypatch.setattr(sheets, "_load", change_after_read)
    with pytest.raises(ValueError, match="changed during edit"):
        edit(
            tmp_path,
            source,
            result["source_sha256"],
            [{"sheet": "Έσοδα", "cell": "B2", "value": 10}],
        )
    assert not (tmp_path / "revised.xlsx").exists()


@pytest.mark.parametrize(
    "part",
    [
        "xl/vbaProject.bin",
        "xl/drawings/drawing1.xml",
        "xl/pivotTables/pivotTable1.xml",
        "customXml/item1.xml",
    ],
)
def test_unsupported_parts_reject_edit_without_losing_source(tmp_path, part):
    source, _ = create(tmp_path)
    replace_part(source, part, lambda _: b"<unsupported />" if part.endswith(".xml") else b"macro")
    original = source.read_bytes()
    with pytest.raises(ValueError, match="Unsupported package part"):
        edit(
            tmp_path,
            source,
            hashlib.sha256(original).hexdigest(),
            [{"sheet": "Έσοδα", "cell": "B2", "value": 10}],
        )
    assert original == source.read_bytes()
    assert not (tmp_path / "revised.xlsx").exists()


@pytest.mark.parametrize(
    "feature",
    [
        "<extLst />",
        "<conditionalFormatting />",
        "<dataValidations />",
        "<sheetProtection />",
        "<ignoredErrors />",
        "<unknownNewFeature />",
    ],
)
def test_unsupported_sheet_features_reject_edit(tmp_path, feature):
    source, _ = create(tmp_path)
    replace_part(
        source,
        "xl/worksheets/sheet1.xml",
        lambda raw: raw.replace(b"</worksheet>", feature.encode() + b"</worksheet>"),
    )
    with pytest.raises(ValueError, match="safely edited"):
        edit(
            tmp_path,
            source,
            hashlib.sha256(source.read_bytes()).hexdigest(),
            [{"sheet": "Έσοδα", "cell": "B2", "value": 10}],
        )


def test_external_relationship_is_reported_and_rejected(tmp_path):
    source = tmp_path / "source.xlsx"
    with xlsxwriter.Workbook(source) as book:
        book.add_worksheet("Data").write_url("A1", "https://example.com")
    result = sheets.inspect(source)
    assert not result["editable"]
    assert "External relationships are unsupported" in result["edit_blockers"]
    with pytest.raises(ValueError, match="External relationships"):
        edit(
            tmp_path,
            source,
            result["source_sha256"],
            [{"sheet": "Data", "cell": "B2", "value": 10}],
        )


def test_macro_extension_rejected(tmp_path):
    source, _ = create(tmp_path)
    macro = source.with_suffix(".xlsm")
    source.rename(macro)
    with pytest.raises(ValueError, match="macro-enabled"):
        sheets.inspect(macro)


@pytest.mark.parametrize(
    "spec",
    [
        {"sheets": []},
        {"sheets": [{"name": "Data", "rows": [[1]]}] * 17},
        {"sheets": [{"name": "Data"}, {"name": "data"}]},
        {"sheets": [{"name": "bad/name"}]},
        {"sheets": [{"name": "Data", "cells": {"IW1": 1}}]},
        {"sheets": [{"name": "Data", "cells": {"A10001": 1}}]},
        {"sheets": [{"name": "Data", "cells": {"IV10000": 1}}]},
        {"sheets": [{"name": "Data", "rows": [[float("nan")]]}]},
        {"sheets": [{"name": "Data", "rows": [[10**16]]}]},
        {"sheets": [{"name": "Data", "rows": [["a" * 32768]]}]},
        {"sheets": [{"name": "Data", "rows": [["a\x00b"]]}]},
        {"sheets": [{"name": "Data", "rows": [[{"value": 1, "formula": "=1"}]]}]},
        {"sheets": [{"name": "Data", "rows": [[{"value": 1, "format": {"align": []}}]]}]},
        {
            "sheets": [
                {
                    "name": "Data",
                    "rows": [[{"value": 1, "format": {"hyperlink": "https://example.com"}}]],
                }
            ]
        },
        {"sheets": [{"name": "Data", "rows": [[1]], "cells": {"A1": 2}}]},
        {"sheets": [{"name": "Data", "rows": [[1]], "column_widths": [float("nan")]}]},
        {"sheets": [{"name": "Data", "rows": [[1]], "unsupported": True}]},
    ],
)
def test_invalid_create_specs_fail_explicitly_without_output(tmp_path, spec):
    with pytest.raises(ValueError):
        create(tmp_path, spec)
    assert not (tmp_path / "source.xlsx").exists()


def test_package_dimension_and_merge_bombs_rejected_before_loader(tmp_path, monkeypatch):
    source, _ = create(tmp_path)
    replace_part(
        source,
        "xl/worksheets/sheet1.xml",
        lambda raw: raw.replace(b'ref="A1:B4"', b'ref="A1:IV10000"'),
    )
    monkeypatch.setattr(sheets, "_load", lambda *_: pytest.fail("must not reach loader"))
    with pytest.raises(ValueError, match="limits"):
        sheets.inspect(source)


def test_package_entity_and_duplicate_parts_rejected(tmp_path):
    source, _ = create(tmp_path)
    replace_part(
        source,
        "xl/worksheets/sheet1.xml",
        lambda raw: b'<!DOCTYPE worksheet [<!ENTITY bomb "unsafe">]>' + raw,
    )
    with pytest.raises(ValueError, match="DTDs"):
        sheets.inspect(source)


def test_duplicate_zip_parts_rejected(tmp_path):
    source, _ = create(tmp_path)
    with zipfile.ZipFile(source, "a") as archive, pytest.warns(UserWarning):
        archive.writestr("xl/workbook.xml", b"<workbook />")
    with pytest.raises(ValueError, match="duplicate package"):
        sheets.inspect(source)


def test_repeated_merge_expansion_rejected_before_loader(tmp_path, monkeypatch):
    source, _ = create(tmp_path)
    merges = b"<mergeCells>" + b'<mergeCell ref="A1:E10000"/>' * 2 + b"</mergeCells>"
    replace_part(
        source,
        "xl/worksheets/sheet1.xml",
        lambda raw: raw.replace(b"</worksheet>", merges + b"</worksheet>"),
    )
    monkeypatch.setattr(sheets, "_load", lambda *_: pytest.fail("must not reach loader"))
    with pytest.raises(ValueError, match="merged-cell area"):
        sheets.inspect(source)


def test_empty_create_and_excel_future_function_roundtrip(tmp_path):
    source, result = create(
        tmp_path,
        {
            "sheets": [
                {"name": "Empty"},
                {"name": "Data", "rows": [[{"formula": '=CONCAT("a","b")'}]]},
            ]
        },
    )
    assert result["editable"] and result["sheet_count"] == 2
    output, result = edit(
        tmp_path,
        source,
        result["source_sha256"],
        [{"sheet": "Empty", "cell": "A1", "value": "filled"}],
    )
    assert result["editable"] and result["formula_count"] == 1


def test_inspect_non_finite_values_stays_json_safe_and_edit_rejects(tmp_path):
    source, _ = create(tmp_path, {"sheets": [{"name": "Data", "rows": [[1]]}]})
    replace_part(
        source, "xl/worksheets/sheet1.xml", lambda raw: raw.replace(b"<v>1</v>", b"<v>1e999</v>")
    )
    result = sheets.inspect(source)
    json.dumps(result, allow_nan=False)
    assert not result["editable"]
    with pytest.raises(ValueError, match="Non-finite"):
        edit(
            tmp_path, source, result["source_sha256"], [{"sheet": "Data", "cell": "A1", "value": 1}]
        )


def test_edit_table_header_and_non_anchor_merge_rejected(tmp_path):
    source, result = create(tmp_path)
    with pytest.raises(ValueError, match="table header"):
        edit(
            tmp_path,
            source,
            result["source_sha256"],
            [{"sheet": "Έσοδα", "cell": "A1", "value": "changed"}],
        )
    with closing(openpyxl.load_workbook(source)) as book:
        book.active.merge_cells("A6:B6")
        book.save(source)
    digest = sheets.inspect(source)["source_sha256"]
    with pytest.raises(ValueError, match="merged cell"):
        edit(tmp_path, source, digest, [{"sheet": "Έσοδα", "cell": "B6", "value": "changed"}])


def test_inspection_is_bounded_and_json_serializable_for_dates(tmp_path):
    source = tmp_path / "source.xlsx"
    book = openpyxl.Workbook()
    book.active["A1"] = datetime(2026, 10, 6, 14, 0)
    for row in range(2, 1200):
        book.active.cell(row, 1, "Στοιχεία")
    book.save(source)
    result = sheets.inspect(source)
    assert result["cell_count"] == 1199
    assert len(result["sheets"][0]["cells"]) == sheets.MAX_PREVIEW_CELLS
    assert result["sheets"][0]["cells_truncated"]
    assert result["sheets"][0]["cells"][0]["value"] == "2026-10-06T14:00:00"
    json.dumps(result, allow_nan=False)


def test_formatting_and_literal_table_headers(tmp_path):
    source, result = create(
        tmp_path,
        {
            "sheets": [
                {
                    "name": "Data",
                    "rows": [
                        ["=Literal", "https://example.com"],
                        [
                            {
                                "value": "00123",
                                "format": {
                                    "fill": "#ffeeaa",
                                    "font_color": "#123456",
                                    "align": "center",
                                    "wrap": True,
                                },
                            },
                            True,
                        ],
                    ],
                    "tables": [{"range": "A1:B2", "name": "SafeTable"}],
                }
            ]
        },
    )
    with closing(openpyxl.load_workbook(source)) as book:
        assert book.active["A1"].data_type == "s"
        assert book.active["B1"].hyperlink is None
        cell = book.active["A2"]
        assert cell.value == "00123"
        assert cell.fill.fgColor.rgb == "FFFFEEAA"
        assert cell.font.color.rgb == "FF123456"
        assert cell.alignment.horizontal == "center" and cell.alignment.wrap_text
        assert book.active["B2"].value is True
    assert result["editable"] and not result["needs_recalculation"]


def test_print_layout_survives_creation_and_cell_revision(tmp_path):
    spec = sheets.example_spec()
    spec["sheets"][0]["page_setup"] = {
        "orientation": "landscape",
        "fit_to_pages": [1, 0],
        "print_area": "A1:B4",
    }
    source, result = create(tmp_path, spec)
    output, _ = edit(
        tmp_path, source, result["source_sha256"], [{"sheet": "Έσοδα", "cell": "B2", "value": 130}]
    )
    for path in (source, output):
        with closing(openpyxl.load_workbook(path)) as book:
            ws = book.active
            assert ws.page_setup.orientation == "landscape"
            # OOXML's default width is one page; XlsxWriter omits that attribute.
            assert ws.page_setup.fitToWidth in {None, 1}
            assert ws.page_setup.fitToHeight == 0
            assert ws.sheet_properties.pageSetUpPr.fitToPage
            assert "$A$1:$B$4" in str(ws.print_area)


@pytest.mark.parametrize(
    "setup",
    [
        {"fit_to_pages": [True, 0]},
        {"fit_to_pages": [-1, 0]},
        {"fit_to_pages": [1]},
        {"fit_to_pages": None},
        {"orientation": "diagonal"},
        {"orientation": []},
        {"print_area": "A1:C10"},
        {"extra": True},
    ],
)
def test_invalid_print_layout_does_not_create_output(tmp_path, setup):
    spec = sheets.example_spec()
    spec["sheets"][0]["page_setup"] = setup
    with pytest.raises(ValueError):
        create(tmp_path, spec)
    assert not (tmp_path / "source.xlsx").exists()


@pytest.mark.parametrize(
    "reference,attributes",
    [
        ("'[external.xlsx]Έσοδα'!$A$1:$B$4", {}),
        ("'Other sheet'!$A$1:$B$4", {}),
        ("'Έσοδα'!$A$1:$B$4", {"localSheetId": "1"}),
        ("'Έσοδα'!$A$1:$B$4", {"name": "BusinessFormula"}),
        ("OFFSET('Έσοδα'!$A$1,0,0,4,2)", {}),
        ("'Έσοδα'!$A$1:$B$4,'Έσοδα'!$D$1:$E$4", {}),
    ],
)
def test_print_area_exception_does_not_admit_other_defined_names(tmp_path, reference, attributes):
    from xml.etree import ElementTree as ET

    spec = sheets.example_spec()
    spec["sheets"][0]["page_setup"] = {"print_area": "A1:B4"}
    source, _ = create(tmp_path, spec)

    def alter(data):
        root = ET.fromstring(data)
        area = root.find("{*}definedNames/{*}definedName")
        area.text = reference
        area.attrib.update(attributes)
        return ET.tostring(root)

    replace_part(source, "xl/workbook.xml", alter)
    original = source.read_bytes()
    with pytest.raises(ValueError, match="unsupported|Unsupported"):
        edit(
            tmp_path,
            source,
            hashlib.sha256(original).hexdigest(),
            [{"sheet": "Έσοδα", "cell": "B2", "value": 130}],
        )
    assert source.read_bytes() == original
    assert not (tmp_path / "revised.xlsx").exists()
