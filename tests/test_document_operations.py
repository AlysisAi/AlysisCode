from __future__ import annotations

import json
import sys

import pytest
from docx import Document
from PIL import Image

from alysis_code.agent.errors import AgentRuntimeError
from alysis_code.agent_loop import build_tools
from alysis_code.config import AppConfig
from alysis_code.office import FORMATS
from alysis_code.office.__main__ import _adapter
from alysis_code.session_store import SessionStore
from alysis_code.surface import ApprovalDecision, NoopSurface


class Surface(NoopSurface):
    host_managed_approvals = True

    def __init__(self, allow=True):
        self.requests = []
        self.allow = allow

    def request_approval(self, request):
        self.requests.append(request)
        return ApprovalDecision(allow=self.allow)


def tools_for(root, **options):
    store = SessionStore(
        enabled=False,
        sessions_dir=root / "sessions",
        session_id="documents",
        cwd=str(root),
        repo_root=str(root),
    )
    return build_tools(
        root=root,
        console=None,
        surface=options.pop("surface", NoopSurface()),
        store=store,
        mode=options.pop("mode", "auto"),
        yes=True,
        cfg=AppConfig(model="test-model"),
        non_interactive=True,
        **options,
    )


@pytest.mark.parametrize("format_name", list(FORMATS))
def test_native_operation_creates_and_inspects_without_a_shell(tmp_path, format_name):
    tools = tools_for(tmp_path)
    tool = tools["document_operation"]
    description = tool.run({"action": "describe", "format": format_name})
    spec = description["example"]
    (tmp_path / "request.json").write_text(json.dumps(spec))
    result = tool.run(
        {
            "action": "create",
            "format": format_name,
            "spec": "request.json",
            "output": f"result.{format_name}",
        }
    )
    assert result["status"] == "created"
    assert result["sha256"]
    inspected = tool.run({"action": "inspect", "source": f"result.{format_name}"})
    assert inspected["source_sha256"] == result["sha256"]
    with pytest.raises(ValueError, match="never overwritten"):
        tool.run(
            {
                "action": "create",
                "format": format_name,
                "spec": "request.json",
                "output": f"result.{format_name}",
            }
        )
    assert tools["capability_status"].run({})["office"]["tool"] == "document_operation"


def test_native_pdf_creation_uses_local_markdown_and_keeps_source(tmp_path):
    source = "# Έκθεση\n\nRevenue EUR 31,700. Contribution excludes overhead.\n"
    (tmp_path / "draft.md").write_text(source)
    tool = tools_for(tmp_path)["document_operation"]
    result = tool.run(
        {"action": "create", "format": "pdf", "source": "draft.md", "output": "report.pdf"}
    )
    assert result["status"] == "created"
    assert result["page_count"] == 1
    assert (tmp_path / result["pages"][0]["image"]).is_file()
    assert (tmp_path / "draft.md").read_text() == source


def test_native_document_capabilities_do_not_advertise_a_host_interpreter_to_container_tools(
    tmp_path, monkeypatch
):
    tools = tools_for(tmp_path)
    monkeypatch.setattr(sys, "executable", "/host-only/engine-python")
    capabilities = tools["capability_status"].run({})
    pdf_guide = tools["document_operation"].run({"action": "describe", "format": "pdf"})
    assert "/host-only/engine-python" not in json.dumps(
        [capabilities["pdf"], capabilities["office"], pdf_guide]
    )
    assert capabilities["office"]["preview"]["tool_example"]["tool"] == "artifact_check"
    assert capabilities["office"]["calculation"]["tool_example"]["tool"] == "document_operation"
    # The standalone CLI still describes its own interpreter correctly.
    from alysis_code.office import office_capability_status

    assert "/host-only/engine-python" in office_capability_status()["command"]


def test_native_mutation_is_hidden_in_readonly_and_respects_write_scope(tmp_path):
    assert "document_operation" not in tools_for(tmp_path, mode="readonly")
    assert "artifact_check" not in tools_for(tmp_path, mode="readonly")
    tool = tools_for(tmp_path, deny_write_prefixes=["protected/"])["document_operation"]
    (tmp_path / "request.json").write_text(json.dumps(_adapter("docx").GUIDE["create"]))
    with pytest.raises(AgentRuntimeError, match="protected path"):
        tool.run(
            {
                "action": "create",
                "format": "docx",
                "spec": "request.json",
                "output": "protected/report.docx",
            }
        )
    assert not (tmp_path / "protected").exists()


@pytest.mark.parametrize(
    "options",
    [
        {"deny_write_prefixes": ["Task reviews/"]},
        {"allow_write_globs": ["outputs/**"]},
    ],
)
def test_native_pdf_and_artifact_previews_respect_write_scope(tmp_path, options):
    from reportlab.pdfgen import canvas

    pdf = canvas.Canvas(str(tmp_path / "input.pdf"))
    pdf.drawString(72, 720, "Original source")
    pdf.save()
    original = (tmp_path / "input.pdf").read_bytes()
    (tmp_path / "draft.md").write_text("# New document\n\nReviewable content.\n")
    tools = tools_for(tmp_path, **options)
    with pytest.raises(AgentRuntimeError, match="Blocked write"):
        tools["artifact_check"].run({"path": "input.pdf"})
    with pytest.raises(AgentRuntimeError, match="Blocked write"):
        tools["document_operation"].run(
            {
                "action": "create",
                "format": "pdf",
                "source": "draft.md",
                "output": "outputs/report.pdf",
            }
        )
    assert (tmp_path / "input.pdf").read_bytes() == original
    assert not (tmp_path / "Task reviews").exists()
    assert not (tmp_path / "outputs/report.pdf").exists()


def test_native_operation_requires_review_approval_before_publishing(tmp_path):
    surface = Surface(allow=False)
    tool = tools_for(tmp_path, surface=surface, mode="review")["document_operation"]
    (tmp_path / "request.json").write_text(json.dumps(_adapter("docx").GUIDE["create"]))
    with pytest.raises(AgentRuntimeError):
        tool.run(
            {"action": "create", "format": "docx", "spec": "request.json", "output": "report.docx"}
        )
    assert any(r.files == ["report.docx"] for r in surface.requests)
    assert not (tmp_path / "report.docx").exists()


def test_artifact_preview_requires_review_approval_before_writing(tmp_path):
    from reportlab.pdfgen import canvas

    pdf = canvas.Canvas(str(tmp_path / "input.pdf"))
    pdf.drawString(72, 720, "Original source")
    pdf.save()
    original = (tmp_path / "input.pdf").read_bytes()
    surface = Surface(allow=False)
    tool = tools_for(tmp_path, surface=surface, mode="review")["artifact_check"]
    with pytest.raises(AgentRuntimeError):
        tool.run({"path": "input.pdf"})
    assert len(surface.requests) == 1
    assert surface.requests[0].files[0].replace("\\", "/").startswith("Task reviews/")
    # QA-009: the approval says which document the files check, not only the random folder.
    assert surface.requests[0].metadata == {
        "purpose": "verification_files",
        "source_artifact": "input.pdf",
    }
    assert surface.requests[0].preview.startswith("Create verification files for input.pdf: ")
    assert (tmp_path / "input.pdf").read_bytes() == original
    assert not (tmp_path / "Task reviews").exists()


def test_pdf_preview_approval_names_the_document_it_checks(tmp_path):
    # QA-009: the page previews go to a random Task reviews folder; the approval names the PDF.
    (tmp_path / "draft.md").write_text("# New document\n\nReviewable content.\n")

    class AllowsTheOutputOnly(Surface):
        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision(allow=len(self.requests) == 1)

    surface = AllowsTheOutputOnly()
    tool = tools_for(tmp_path, surface=surface, mode="review")["document_operation"]
    with pytest.raises(AgentRuntimeError):
        tool.run(
            {"action": "create", "format": "pdf", "source": "draft.md", "output": "report.pdf"}
        )
    assert surface.requests[0].files == ["report.pdf"]
    assert surface.requests[0].metadata == {}
    preview = surface.requests[1]
    assert preview.files[0].startswith("Task reviews/")
    assert preview.metadata == {"purpose": "verification_files", "source_artifact": "report.pdf"}
    assert not (tmp_path / "report.pdf").exists()


def test_native_operation_rejects_source_symlinks_and_outside_output(tmp_path):
    document = Document()
    document.add_paragraph("original")
    document.save(tmp_path / "source.docx")
    (tmp_path / "alias.docx").symlink_to(tmp_path / "source.docx")
    tool = tools_for(tmp_path)["document_operation"]
    with pytest.raises(ValueError, match="symbolic"):
        tool.run({"action": "inspect", "source": "alias.docx"})
    with pytest.raises((ValueError, AgentRuntimeError)):
        tool.run(
            {
                "action": "create",
                "format": "docx",
                "spec": "request.json",
                "output": "../outside.docx",
            }
        )


def test_native_slides_recheck_sensitive_embedded_image_access(tmp_path):
    (tmp_path / ".ssh").mkdir()
    Image.new("RGB", (10, 10), "red").save(tmp_path / ".ssh" / "picture.png")
    (tmp_path / "request.json").write_text(
        json.dumps(
            {
                "slides": [
                    {
                        "elements": [
                            {
                                "type": "image",
                                "path": ".ssh/picture.png",
                                "x": 1,
                                "y": 1,
                                "width": 2,
                                "height": 2,
                            }
                        ]
                    }
                ]
            }
        )
    )
    surface = Surface(allow=False)
    tool = tools_for(tmp_path, surface=surface)["document_operation"]
    with pytest.raises(AgentRuntimeError):
        tool.run(
            {"action": "create", "format": "pptx", "spec": "request.json", "output": "report.pptx"}
        )
    assert any(r.files == [".ssh/picture.png"] for r in surface.requests)
    assert not (tmp_path / "report.pptx").exists()


def test_native_document_mutations_are_tracked_but_inspection_is_not(tmp_path):
    from alysis_code.agent.verification import TurnExecutionState, _record_tool_effect

    state = TurnExecutionState(execution_requested=True)
    for result in [
        {"status": "inspected", "source": "original.docx"},
        {"format": "docx", "guide": {}},
    ]:
        _record_tool_effect(
            root=tmp_path,
            state=state,
            tool_name="document_operation",
            arguments={},
            status="success",
            result=result,
            known_verification_commands=[],
        )
    assert not state.touched_repo_paths
    result = {"status": "created", "output": "Work outputs/report.docx"}
    _record_tool_effect(
        root=tmp_path,
        state=state,
        tool_name="document_operation",
        arguments={},
        status="success",
        result=result,
        known_verification_commands=[],
    )
    assert state.touched_repo_paths == {"Work outputs/report.docx"}
    assert "document_operation" in state.material_edit_tools
    assert not state.accepted_verification_evidence
