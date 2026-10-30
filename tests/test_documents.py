from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
from contextlib import closing
from types import SimpleNamespace

import pytest

from alysis_code.documents import pdf_capability_status
from alysis_code.documents.__main__ import run


def create(root, text, **kwargs):
    source = root / "draft.md"
    source.write_text(text, encoding="utf-8")
    return run(
        SimpleNamespace(
            workspace=str(root),
            command="create",
            source="draft.md",
            output="outputs/report.pdf",
            preview_dir="scratch/review",
            title="Sample",
            overwrite=False,
            **kwargs,
        )
    )


def extracted(pdf_path):
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(pdf_path) as pdf:
        texts = []
        for index in range(len(pdf)):
            with closing(pdf[index]) as page, closing(page.get_textpage()) as textpage:
                texts.append(textpage.get_text_range())
        return "\n".join(texts)


def test_create_renders_all_pages_and_preserves_multilingual_content(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Document helper must not connect to the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    paragraphs = "This paragraph uses exact font metrics and wraps safely. " * 30
    rows = "\n".join(f"| Channel {i} | {paragraphs[:200]} |" for i in range(55))
    text = (
        f"# Startup promotion\n\nΕλληνική προώθηση. Русский текст.\n\n{paragraphs}\n\n"
        "## Campaign\n\n3. First action\n   - Nested action\n4. Second action\n\n"
        "[Source](https://example.com) **bold** *italic* `code`\n\n"
        "```text\n" + "longcode" * 150 + "\n```\n\n"
        "| Channel | Action |\n| --- | --- |\n" + rows
    )
    result = create(tmp_path, text)
    assert result["status"] == "created"
    assert result["page_count"] > 3
    assert result["visual_review"] == "not_performed"
    assert len(result["pages"]) == len(list((tmp_path / "scratch/review").glob("*.png")))
    assert all(p["content_characters"] > 0 for p in result["pages"])
    assert all(
        p["characters_outside_page"] == p["characters_outside_content_frame"] == 0
        for p in result["pages"]
    )
    content = extracted(tmp_path / "outputs/report.pdf")
    for phrase in [
        "Ελληνική προώθηση",
        "Русский текст",
        "First action",
        "Nested action",
        "Channel 54",
    ]:
        assert phrase in content
    assert (tmp_path / "draft.md").read_text() == text
    assert not list(tmp_path.glob(".alysis-pdf-*"))


@pytest.mark.parametrize(
    "text,reason",
    [
        ("", "no content"),
        ("---", "empty or clipped"),
        ("# 漢字", "Unsupported font glyphs"),
        ("![image](https://example.com/image.png)", "Unsupported Markdown"),
        ("[unsafe](relative-file)", "links must use"),
        ("العربية", "complex-script shaping"),
    ],
)
def test_invalid_sources_publish_no_pdf_or_previews(tmp_path, text, reason):
    with pytest.raises(ValueError, match=reason):
        create(tmp_path, text)
    assert not (tmp_path / "outputs/report.pdf").exists()
    assert not (tmp_path / "scratch/review").exists()
    assert not list(tmp_path.glob(".alysis-pdf-*"))


def test_raw_html_is_literal_and_never_fetches_external_assets(tmp_path):
    create(tmp_path, '<img src="https://example.com/private.png">\n\nLiteral <b>text</b>.')
    assert '<img src="https://example.com/private.png">' in extracted(
        tmp_path / "outputs/report.pdf"
    )


def test_wide_tables_fail_with_actionable_error(tmp_path):
    row = "| " + " | ".join(["column"] * 9) + " |\n"
    separator = "| " + " | ".join(["---"] * 9) + " |\n"
    with pytest.raises(ValueError, match="at most eight"):
        create(tmp_path, row + separator + row)


def test_existing_pdf_and_previews_are_preserved_until_authorized_revision(tmp_path):
    create(tmp_path, "# Original\n\nContent")
    original = (tmp_path / "outputs/report.pdf").read_bytes()
    args = SimpleNamespace(
        workspace=str(tmp_path),
        command="create",
        source="draft.md",
        output="outputs/report.pdf",
        preview_dir="scratch/new-review",
        title="Revision",
        overwrite=False,
    )
    (tmp_path / "draft.md").write_text("# Revised\n\nNew content")
    with pytest.raises(ValueError, match="already exists"):
        run(args)
    assert (tmp_path / "outputs/report.pdf").read_bytes() == original
    args.overwrite = True
    run(args)
    assert "Revised" in extracted(tmp_path / "outputs/report.pdf")
    with pytest.raises(ValueError, match="new preview directory"):
        run(args)
    assert len(list((tmp_path / "scratch/review").glob("*.png"))) == 1


@pytest.mark.parametrize("field", ["source", "output", "preview_dir"])
def test_workspace_escape_is_rejected(tmp_path, field):
    (tmp_path / "draft.md").write_text("Content")
    args = SimpleNamespace(
        workspace=str(tmp_path),
        command="create",
        source="draft.md",
        output="outputs/report.pdf",
        preview_dir="scratch/review",
        title="",
        overwrite=False,
    )
    setattr(args, field, "../outside")
    with pytest.raises(ValueError, match="inside the workspace"):
        run(args)


@pytest.mark.parametrize("field", ["source", "output", "preview_dir"])
def test_symlink_parents_are_rejected(tmp_path, field):
    (tmp_path / "draft.md").write_text("Content")
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    args = SimpleNamespace(
        workspace=str(tmp_path),
        command="create",
        source="draft.md",
        output="outputs/report.pdf",
        preview_dir="scratch/review",
        title="",
        overwrite=False,
    )
    setattr(args, field, "link/file.pdf")
    with pytest.raises(ValueError, match="symbolic links"):
        run(args)


def test_reading_hardlinked_source_preserves_both_names(tmp_path):
    (tmp_path / "original.md").write_text("Checkpointed source")
    os.link(tmp_path / "original.md", tmp_path / "draft.md")
    result = create(tmp_path, "Checkpointed source")
    assert result["status"] == "created"
    assert (tmp_path / "original.md").read_text() == "Checkpointed source"


def test_page_and_source_limits_publish_nothing(tmp_path, monkeypatch):
    from alysis_code.documents import __main__ as documents

    monkeypatch.setattr(documents, "MAX_SOURCE_BYTES", 10)
    with pytest.raises(ValueError, match="MiB limit"):
        create(tmp_path, "content" * 20)
    monkeypatch.setattr(documents, "MAX_SOURCE_BYTES", 1024 * 1024)
    monkeypatch.setattr(documents, "MAX_PAGES", 1)
    with pytest.raises(ValueError, match="exceeds 1 pages"):
        create(tmp_path, "Long paragraph. " * 1000)
    assert not (tmp_path / "outputs/report.pdf").exists()


def test_render_existing_pdf_and_cli_errors(tmp_path):
    create(tmp_path, "# Sample\n\nReview existing PDF")
    result = run(
        SimpleNamespace(
            workspace=str(tmp_path),
            command="render",
            source="outputs/report.pdf",
            preview_dir="scratch/second-review",
        )
    )
    assert result["status"] == "rendered"
    assert result["visual_review"] == "not_performed"
    (tmp_path / "bad.pdf").write_bytes(b"not a PDF")
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "alysis_code.documents",
            "--workspace",
            str(tmp_path),
            "render",
            "--source",
            "bad.pdf",
            "--preview-dir",
            "scratch/bad-review",
        ],
        capture_output=True,
        text=True,
    )
    assert process.returncode == 1
    assert json.loads(process.stdout)["status"] == "error"
    assert "Traceback" not in process.stderr


def test_advertised_command_creates_actual_pdf_in_workspace_with_spaces(tmp_path):
    root = tmp_path / "task with spaces"
    root.mkdir()
    (root / "draft.md").write_text(
        "# Command test\n\n```python\ndef sample():\n    return 42\n```\n"
    )
    capability = pdf_capability_status()
    prefix = capability["command"]
    expected = [sys.executable, "-m", "alysis_code.documents"]
    assert prefix == (
        subprocess.list2cmdline(expected) if os.name == "nt" else shlex.join(expected)
    )
    assert capability["create_example"].startswith(prefix + " ")
    argv = expected + shlex.split(capability["create_example"][len(prefix) :])
    process = subprocess.run(argv, cwd=root, capture_output=True, text=True)
    assert process.returncode == 0, process.stdout + process.stderr
    result = json.loads(process.stdout)
    assert result["status"] == "created"
    assert (root / result["pages"][0]["image"]).is_file()
    import pypdfium2 as pdfium

    with (
        pdfium.PdfDocument(root / "result.pdf") as pdf,
        closing(pdf[0]) as page,
        closing(page.get_textpage()) as textpage,
    ):
        text = textpage.get_text_range()
        assert "return 42" in text
        # PDF text extraction discards leading spaces; verify their rendered position.
        assert (
            textpage.get_charbox(text.index("return"))[0]
            > textpage.get_charbox(text.index("def"))[0] + 8
        )


def test_capability_uses_current_interpreter_and_missing_dependencies_fail_closed(monkeypatch):
    status = pdf_capability_status()
    assert status["state"] == "ready"
    assert sys.executable in status["command"]
    assert "asset_view" in status["guidance"]
    monkeypatch.setattr("alysis_code.documents.importlib.util.find_spec", lambda name: None)
    unavailable = pdf_capability_status()
    assert unavailable["state"] == "unavailable"
    assert "command" not in unavailable
    assert "reportlab" in unavailable["missing_modules"]
