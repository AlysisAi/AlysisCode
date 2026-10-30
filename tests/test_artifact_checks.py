from __future__ import annotations

import hashlib
import os

import pytest
from docx import Document

from alysis_code.agent.verification import TurnExecutionState, _record_tool_effect
from alysis_code.artifact_checks import ArtifactChecks
from alysis_code.run_outcome import task_outcome_notice


def document(path):
    doc = Document()
    doc.add_paragraph("Έκθεση: unchanged original")
    doc.save(path)
    return path.read_bytes()


def test_structure_checks_bound_to_hash_and_unavailable_render_is_not_passed(tmp_path, monkeypatch):
    monkeypatch.setattr("alysis_code.office.renderer_path", lambda: None)
    original = document(tmp_path / "report.docx")
    service = ArtifactChecks(tmp_path)
    result = service.check("report.docx")
    assert result["documentSha256"] == hashlib.sha256(original).hexdigest()
    assert [(item["id"], item["status"]) for item in result["items"]] == [
        ("structure", "passed"),
        ("render", "not_checked"),
    ]
    assert (tmp_path / "report.docx").read_bytes() == original
    result["items"].clear()
    assert len(service.reports()[0]["items"]) == 2
    with pytest.raises(ValueError, match="changed"):
        service.check("report.docx", expected_sha256="0" * 64)


def test_invalid_structure_and_unsafe_paths(tmp_path):
    (tmp_path / "bad.docx").write_bytes(b"not a document")
    service = ArtifactChecks(tmp_path)
    result = service.check("bad.docx")
    assert result["items"][0]["status"] == "failed"
    assert result["preview"] is None
    (tmp_path / "link.docx").symlink_to(tmp_path / "bad.docx")
    for path in ("link.docx", "../outside.docx"):
        with pytest.raises(ValueError):
            service.check(path)


def test_sensitive_report_never_reappears_after_a_public_check(tmp_path, monkeypatch):
    from test_document_operations import Surface, tools_for

    monkeypatch.setattr("alysis_code.office.renderer_path", lambda: None)
    (tmp_path / ".ssh").mkdir()
    document(tmp_path / ".ssh/private.docx")
    document(tmp_path / "public.docx")
    tool = tools_for(tmp_path, surface=Surface())["artifact_check"]
    result = tool.run({"path": ".ssh/private.docx", "path_base": "workspace_root"})
    assert result["_alysis_output_policy"]["persist"] == "redact"
    assert tool.metadata["artifact_reports"] == []
    tool.run({"path": "public.docx", "path_base": "workspace_root"})
    assert [item["path"] for item in tool.metadata["artifact_reports"]] == ["public.docx"]


def test_retention_guard_removes_previous_report_after_preview_becomes_sensitive(tmp_path):
    from reportlab.pdfgen.canvas import Canvas

    pdf = Canvas(str(tmp_path / "report.pdf"))
    pdf.drawString(72, 720, "Synthetic review fixture")
    pdf.save()
    service = ArtifactChecks(tmp_path)
    service.check("report.pdf")
    assert len(service.reports()) == 1
    sensitive = []
    result = service.check(
        "report.pdf",
        write_guard=lambda path: sensitive.append(path),
        retain_report=lambda: not sensitive,
    )
    assert result["preview"]
    assert service.reports() == []


def test_source_change_during_checks_does_not_publish_evidence(tmp_path, monkeypatch):
    source = tmp_path / "report.docx"
    document(source)
    service = ArtifactChecks(tmp_path)

    def changed():
        source.write_bytes(b"changed concurrently")
        return {"preview": {"state": "unavailable"}}

    monkeypatch.setattr("alysis_code.artifact_checks.office_capability_status", changed)
    with pytest.raises(ValueError, match="changed while checking"):
        service.check(source.name)
    assert service.reports() == []


def test_artifact_evidence_does_not_override_required_checks(tmp_path):
    state = TurnExecutionState(execution_requested=True, expected_verification_commands={"pytest"})
    result = {
        "path": "report.docx",
        "documentSha256": "a" * 64,
        "items": [
            {"id": "structure", "label": "File structure", "status": "passed", "basis": "observed"}
        ],
    }
    _record_tool_effect(
        root=tmp_path,
        state=state,
        tool_name="artifact_check",
        arguments={"path": "report.docx"},
        status="success",
        result=result,
        known_verification_commands=["pytest"],
    )
    assert state.missing_verification_commands() == {"pytest"}
    assert not state.accepted_verification_evidence
    assert state.as_payload()["artifact_checks"] == [result]
    assert task_outcome_notice({"artifact_checks": [result]}) == ""
    assert "required or observed checks failed" in task_outcome_notice(
        {"artifact_checks": [result], "problems": ["verification_failed"]}
    )
    assert "failed" not in task_outcome_notice({"problems": ["acceptance_criteria_unverified"]})


def test_real_document_preview_belongs_to_exact_source(tmp_path, monkeypatch):
    executable = os.environ.get("ALYSIS_TEST_OFFICE_SOFFICE")
    if not executable:
        pytest.skip("Set ALYSIS_TEST_OFFICE_SOFFICE for real rendering")
    monkeypatch.setenv("ALYSIS_OFFICE_SOFFICE", executable)
    original = document(tmp_path / "report.docx")
    service = ArtifactChecks(tmp_path)
    result = service.check("report.docx")
    assert all(item["status"] == "passed" for item in result["items"])
    assert result["preview"]["documentSha256"] == hashlib.sha256(original).hexdigest()
    assert result["preview"]["pageCount"] == 1
    assert (tmp_path / result["preview"]["pages"][0]["image"]).is_file()
    assert (
        hashlib.sha256((tmp_path / result["preview"]["pdf"]).read_bytes()).hexdigest()
        == result["preview"]["pdfSha256"]
    )
    assert result["preview"]["visualReview"] == "not_performed"
    assert (tmp_path / "report.docx").read_bytes() == original


def test_native_tool_retains_only_observed_reports_for_bridge(tmp_path, monkeypatch):
    from test_read_ledger_delivery_boundary import _Client, _response, _session

    from alysis_code.llm.types import LLMResponse, ToolCall

    monkeypatch.setattr("alysis_code.office.renderer_path", lambda: None)
    client = _Client(
        [
            _response(
                ToolCall(
                    id="check",
                    name="artifact_check",
                    arguments={"path": "report.docx", "path_base": "workspace_root"},
                )
            ),
            LLMResponse(content="File checked.", tool_calls=[], raw={}),
        ]
    )
    session = _session(tmp_path, monkeypatch, client)
    try:
        document(session.root / "report.docx")
        assert session.run_turn("Check report.docx.") == 0
        reports = session.tools["artifact_check"].metadata["artifact_reports"]
        assert reports[0]["items"][0]["status"] == "passed"
        assert (
            reports[0]["documentSha256"]
            == hashlib.sha256((session.root / "report.docx").read_bytes()).hexdigest()
        )
        assert (
            session.last_turn_outcome["artifact_checks"][0]["documentSha256"]
            == reports[0]["documentSha256"]
        )
    finally:
        session.close()


def test_clipped_office_preview_is_available_to_repair_and_marked_failed(tmp_path, monkeypatch):
    from alysis_code.office import rendering

    source = tmp_path / "report.docx"
    original = document(source)
    monkeypatch.setattr("alysis_code.office.renderer_path", lambda: source)
    monkeypatch.setattr(rendering, "renderer_path", lambda: source)

    def convert(_executable, input_path, destination, _profile, **kwargs):
        (destination / (input_path.stem + ".pdf")).write_bytes(b"%PDF-1.7\nfixture")

    def rendered(_source, destination, **kwargs):
        (destination / "page-001.png").write_bytes(b"fixture")
        return {
            "page_count": 1,
            "pages": [{"page": 1, "image": "page-001.png", "characters_outside_page": 12}],
        }

    monkeypatch.setattr(rendering, "_convert", convert)
    monkeypatch.setattr(rendering, "_render", rendered)
    report = ArtifactChecks(tmp_path).check(source.name)
    assert report["items"][-1]["status"] == "failed"
    assert report["preview"]["pages"][0]["characters_outside_page"] == 12
    assert (tmp_path / report["preview"]["pages"][0]["image"]).is_file()
    assert (tmp_path / report["preview"]["pdf"]).is_file()
    assert source.read_bytes() == original
