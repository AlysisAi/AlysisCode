"""Observed checks bound to exact local deliverable bytes, independent of prose."""

from __future__ import annotations

import copy
import hashlib
import tempfile
import threading
import uuid
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .documents.__main__ import _check_execution, _path, _publish_preview, _render
from .execution_deadline import DeadlineExhausted, ExecutionDeadline, deadline_timeout_or_raise
from .office import office_capability_status
from .office.__main__ import MAX_FILE, _adapter, _package, _read


def _cached_formula_values(path: Path) -> dict[tuple[str, str], tuple[str, object]]:
    from .office.calculation import NS, _worksheets

    result = {}
    with zipfile.ZipFile(path) as archive:
        for name, (_, tree) in _worksheets(archive).items():
            for cell in tree.iter(f"{{{NS}}}c"):
                if cell.find(f"{{{NS}}}f") is None:
                    continue
                value = cell.find(f"{{{NS}}}v")
                kind, text = cell.get("t", "n"), None if value is None else value.text or ""
                if kind == "n" and text is not None:
                    try:
                        text = Decimal(text)
                    except InvalidOperation:
                        pass
                result[name, cell.get("r")] = (kind, text)
    return result


class ArtifactChecks:
    def __init__(
        self,
        root: Path,
        *,
        execution_deadline: ExecutionDeadline | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ):
        self.root = root.resolve()
        self.execution_deadline = execution_deadline
        self.cancel_check = cancel_check
        self._reports: dict[str, dict] = {}
        self._lock = threading.Lock()

    def reports(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(list(self._reports.values()))

    def _conversion_options(self) -> dict:
        return {
            "timeout_seconds": deadline_timeout_or_raise(
                self.execution_deadline, 90.0, reserve_seconds=1.0, operation="artifact_check"
            ),
            "cancel_check": self.cancel_check,
        }

    def check(
        self,
        path: str,
        *,
        expected_sha256: str | None = None,
        write_guard: Callable[[str], None] | None = None,
        retain_report: Callable[[], bool] | None = None,
    ) -> dict:
        _check_execution(self.execution_deadline, self.cancel_check)
        source = _path(self.root, path)
        data = _read(source, MAX_FILE)
        sha = hashlib.sha256(data).hexdigest()
        if expected_sha256 is not None and expected_sha256 != sha:
            raise ValueError(
                "The artifact changed; inspect the current version before checking it."
            )
        format_name = source.suffix.lower().removeprefix(".")
        if format_name not in {"docx", "xlsx", "pptx", "pdf"}:
            raise ValueError("Artifact checks support DOCX, XLSX, PPTX and PDF files.")
        report = {
            "path": str(source.relative_to(self.root)),
            "documentSha256": sha,
            "format": format_name,
            "checkedAt": datetime.now(timezone.utc).isoformat(),
            "items": [],
            "preview": None,
            "scope": "Structure, calculation and rendered page checks only; verify source accuracy and task requirements separately.",
        }

        def record(identifier, label, state, detail=None):
            report["items"].append(
                {
                    "id": identifier,
                    "label": label,
                    "status": state,
                    "basis": "observed",
                    **({"detail": detail} if detail else {}),
                }
            )

        with tempfile.TemporaryDirectory(
            prefix=".alysis-artifact-check-", dir=self.root
        ) as temporary:
            stage = Path(temporary)
            snapshot = stage / ("source." + format_name)
            snapshot.write_bytes(data)
            try:
                if format_name == "pdf":
                    import pypdfium2

                    from .documents import PDFIUM_LOCK

                    with PDFIUM_LOCK, pypdfium2.PdfDocument(snapshot) as pdf:
                        if not 1 <= len(pdf) <= 50:
                            raise ValueError("PDF must contain between 1 and 50 pages.")
                    inspected = {}
                else:
                    _package(data)
                    inspected = _adapter(format_name).inspect(snapshot)
                record("structure", "File structure", "passed")
            except (ValueError, OSError, ImportError, RuntimeError, zipfile.BadZipFile) as exc:
                record("structure", "File structure", "failed", str(exc))
                return self._publish(source, sha, report, retain_report=retain_report)

            if format_name == "pptx" and inspected["text_box_overlaps"]["count"]:
                overlaps = inspected["text_box_overlaps"]
                sample = overlaps["examples"][0]
                report["textBoxOverlaps"] = overlaps
                record(
                    "text_box_layout",
                    "Text box layout",
                    "not_checked",
                    f"{overlaps['count']} pair(s) of text boxes share space. "
                    f"Inspect slide {sample['slide']}, shapes {sample['shape_ids'][0]} and "
                    f"{sample['shape_ids'][1]}. Box geometry alone cannot establish whether "
                    "the text collides. Review the slide and adjust bounds if needed.",
                )

            if format_name == "xlsx" and inspected.get("heading_fit", {}).get("count"):
                fit = inspected["heading_fit"]
                sample = fit["examples"][0]
                report["headingFit"] = fit
                record(
                    "heading_fit",
                    "Workbook headings",
                    "not_checked",
                    f"{fit['count']} unwrapped heading(s) may exceed their available column width. "
                    f"Review {sample['sheet']}!{sample['cell']} in the rendered sheet. "
                    "Widen the heading area or wrap its text if it is clipped.",
                )

            if format_name == "xlsx" and inspected.get("header_contrast", {}).get("count"):
                contrast = inspected["header_contrast"]
                sample = contrast["examples"][0]
                report["headerContrast"] = contrast
                detail = (
                    f"Stored text {sample['font']} on {sample['fill']} has a contrast ratio of "
                    f"{sample['ratio']}:1, below {sample['minimum']}:1."
                    if sample["reason"] == "low_contrast"
                    else f"Excel can supply the header text color over the explicit {sample['fill']} fill."
                )
                record(
                    "header_contrast",
                    "Workbook header contrast",
                    "not_checked",
                    f"Review {sample['sheet']}!{sample['cell']}. {detail} Inspect the rendered "
                    "header. If difficult to read, use a formatting-only font_color edit and recheck.",
                )

            if format_name == "xlsx" and inspected.get("print_coverage", {}).get("count"):
                coverage = inspected["print_coverage"]
                report["printCoverage"] = coverage
                first = coverage["examples"][0]
                record(
                    "print_coverage",
                    "Printed table coverage",
                    "not_checked",
                    f"On {first['sheet']}, rows {first['first_outside_row']} through "
                    f"{first['through_row']} continue below both table {first['table']} and "
                    "the print area. They will be absent in a print or PDF. Check those records "
                    "against the request. If they belong, use extend_printed_table and recheck.",
                )

            capabilities = office_capability_status()
            if format_name == "xlsx" and inspected.get("formula_count"):
                if capabilities["calculation"]["state"] != "configured":
                    record(
                        "calculation",
                        "Formula results",
                        "not_checked",
                        "No compatible calculation route is available for this workbook.",
                    )
                else:
                    from .office.calculation import recalculate

                    try:
                        calculated = stage / "calculated.xlsx"
                        result = recalculate(
                            snapshot, calculated, self.root, **self._conversion_options()
                        )
                        matching = _cached_formula_values(snapshot) == _cached_formula_values(
                            calculated
                        )
                        passed = result["state"] == "passed" and matching
                        record(
                            "calculation",
                            "Formula results",
                            "passed" if passed else "failed",
                            "Recalculated results match this file."
                            if passed
                            else "Formula errors or stale/missing cached results; recalculate into a new workbook.",
                        )
                        report["calculation"] = {
                            "formulaCount": result["formula_count"],
                            "errorCount": result["error_count"],
                            "cachedResultsMatch": matching,
                        }
                    except DeadlineExhausted:
                        raise
                    except (ValueError, OSError, ImportError, RuntimeError) as exc:
                        record("calculation", "Formula results", "failed", str(exc))

            if format_name != "pdf" and capabilities["preview"]["state"] != "configured":
                record(
                    "render", "Rendered pages", "not_checked", "An Office renderer is unavailable."
                )
            else:
                preview = _path(self.root, f"Task reviews/{uuid.uuid4().hex}")
                if write_guard is not None:
                    write_guard(str(preview.relative_to(self.root)))
                try:
                    if format_name == "pdf":
                        pages = stage / "preview"
                        pages.mkdir()
                        execution_options = {
                            "execution_deadline": self.execution_deadline,
                            "cancel_check": self.cancel_check,
                        }
                        result = _render(snapshot, pages, **execution_options)
                        _publish_preview(pages, preview, **execution_options)
                        for page in result["pages"]:
                            page["image"] = str((preview / page["image"]).relative_to(self.root))
                    else:
                        from .office.rendering import render

                        result = render(
                            snapshot,
                            preview,
                            self.root,
                            sha,
                            execution_deadline=self.execution_deadline,
                            **self._conversion_options(),
                        )
                    outside = sum(page["characters_outside_page"] for page in result["pages"])
                    record(
                        "render",
                        "Rendered pages",
                        "failed" if outside else "passed",
                        "Text extends outside page bounds."
                        if outside
                        else "Every page rendered; inspect the images for layout quality.",
                    )
                    report["preview"] = {
                        "documentSha256": sha,
                        "pageCount": result["page_count"],
                        "pages": result["pages"],
                        "visualReview": "not_performed",
                    }
                    if (
                        report.get("textBoxOverlaps")
                        and result["page_count"] == inspected["slide_count"]
                    ):
                        report["preview"]["suggestedPage"] = report["textBoxOverlaps"]["examples"][
                            0
                        ]["slide"]
                    pdf_path = (
                        str(source.relative_to(self.root))
                        if format_name == "pdf"
                        else result["pdf"]
                    )
                    report["preview"].update(
                        pdf=pdf_path,
                        pdfSha256=hashlib.sha256(
                            _read(_path(self.root, pdf_path), MAX_FILE)
                        ).hexdigest(),
                    )
                except DeadlineExhausted:
                    raise
                except (ValueError, OSError, ImportError, RuntimeError) as exc:
                    record("render", "Rendered pages", "failed", str(exc))
        return self._publish(source, sha, report, retain_report=retain_report)

    def _publish(
        self, source: Path, sha: str, report: dict, *, retain_report: Callable[[], bool] | None
    ) -> dict:
        _check_execution(self.execution_deadline, self.cancel_check)
        if hashlib.sha256(_read(_path(self.root, str(source)), MAX_FILE)).hexdigest() != sha:
            raise ValueError("The artifact changed while checking; no evidence was recorded.")
        report["next"] = (
            "Inspect every returned page image with asset_view before claiming layout review. Fix failed checks and check the new file again."
        )
        with self._lock:
            # Evaluate after the read and preview guards: either can classify the
            # result as sensitive. Never retain it for a later public bridge read.
            self._reports.pop(report["path"], None)
            if retain_report is None or retain_report():
                self._reports[report["path"]] = copy.deepcopy(report)
            while len(self._reports) > 50:
                self._reports.pop(next(iter(self._reports)))
        return report
