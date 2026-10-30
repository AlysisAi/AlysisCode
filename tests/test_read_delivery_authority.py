from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from alysis_code.agent.read_ledger import SessionReadLedger
from alysis_code.agent_loop import create_session
from alysis_code.compaction.tool_output_offload import ToolOutputOffloader
from alysis_code.config import AppConfig
from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.session_artifacts import SessionArtifactLayout
from alysis_code.tools.artifacts import SessionArtifactReadError, session_artifact_read
from alysis_code.tools.fs import fs_read


def _prepare(
    ledger: SessionReadLedger, path: str = "source.txt", *, force=False, include_line_numbers=False
):
    before = ledger.content_hash(path)
    return ledger.filter_result(
        path=path,
        result=fs_read(
            root=ledger.root,
            path=path,
            max_bytes=100_000,
            **({"include_line_numbers": True} if include_line_numbers else {}),
        ),
        content_hash_before=before,
        force=force,
        include_line_numbers=include_line_numbers,
    )


def _offloader(tmp_path: Path, *, preview=100):
    return ToolOutputOffloader(
        artifact_layout=SessionArtifactLayout(tmp_path / "session"),
        workspace_root=tmp_path,
        threshold_chars=200,
        preview_chars=preview,
    )


def test_host_fetch_without_message_delivery_never_suppresses_a_read(tmp_path: Path):
    (tmp_path / "source.txt").write_text("alpha\nbeta\n", encoding="utf-8", newline="")
    ledger = SessionReadLedger(root=tmp_path)
    first = _prepare(ledger)
    assert ledger.snapshot() == {}
    second = _prepare(ledger)
    assert second["content"] == first["content"]
    receipt = ledger.record_delivery(result=second, content_for_message=json.dumps(second))
    assert receipt["delivered_ranges"] == [{"start_line": 1, "end_line": 2}]
    assert _prepare(ledger)["read_ledger_skipped"] is True


def test_unicode_preview_only_credits_complete_visible_lines_and_resets_on_compaction(
    tmp_path: Path,
):
    text = "α🙂\nβ🙂\n" + "hidden detail\n" * 80
    (tmp_path / "source.txt").write_text(text, encoding="utf-8", newline="")
    ledger = SessionReadLedger(root=tmp_path)
    result = _prepare(ledger)
    shaped = _offloader(tmp_path, preview=5).maybe_offload(
        tool_name="fs_read",
        tool_call_id="unicode",
        step=1,
        result=result,
        content_json=json.dumps(result, ensure_ascii=False),
    )
    receipt = ledger.record_delivery(result=result, content_for_message=shaped.content_for_message)
    assert receipt["delivered_ranges"] == [{"start_line": 1, "end_line": 1}]
    assert receipt["visible_chars"] == 5
    assert receipt["visible_utf8_bytes"] == len("α🙂\nβ🙂".encode())
    assert receipt["retrievable_handle"].startswith("artifact:")
    assert ledger.snapshot()["source.txt"]["ranges"] == [(1, 1)]
    resumed = SessionReadLedger(root=tmp_path)
    assert resumed.seed_from_snapshot(ledger.snapshot()) == 1
    assert _prepare(resumed)["content"].startswith("β🙂\n")
    assert resumed.reset() == 1
    assert _prepare(resumed)["content"] == text


def test_redaction_and_changed_file_cannot_credit_hidden_source(tmp_path: Path):
    (tmp_path / "source.txt").write_text("private\nvisible\n", encoding="utf-8", newline="")
    ledger = SessionReadLedger(root=tmp_path)
    result = _prepare(ledger)
    receipt = ledger.record_delivery(
        result=result,
        content_for_message=json.dumps({**result, "content": "[redacted]\nvisible\n"}),
    )
    assert receipt["delivered_ranges"] == [{"start_line": 2, "end_line": 2}]
    staged = _prepare(ledger, force=True)
    (tmp_path / "source.txt").write_text("changed", encoding="utf-8")
    assert ledger.record_delivery(result=staged, content_for_message=json.dumps(staged)) is None
    assert ledger.snapshot() == {}


@pytest.mark.parametrize("include_line_numbers", [False, True])
def test_resume_seeds_only_receipts_whose_exact_tool_messages_are_restored(
    tmp_path: Path, include_line_numbers: bool
):
    (tmp_path / "source.txt").write_text("retained evidence\n", encoding="utf-8", newline="")
    original = SessionReadLedger(root=tmp_path)
    result = _prepare(original, include_line_numbers=include_line_numbers)
    content = json.dumps(result)
    original.record_delivery(result=result, content_for_message=content)
    snapshot = original.snapshot()
    for retained in ([], [{"role": "tool", "content": "[summarized]"}]):
        resumed = SessionReadLedger(root=tmp_path)
        assert resumed.seed_from_snapshot(snapshot, retained_messages=retained) == 0
        assert "read_ledger_skipped" not in _prepare(
            resumed, include_line_numbers=include_line_numbers
        )
    resumed = SessionReadLedger(root=tmp_path)
    assert (
        resumed.seed_from_snapshot(
            snapshot, retained_messages=[{"role": "tool", "content": content}]
        )
        == 1
    )
    assert (
        _prepare(resumed, include_line_numbers=include_line_numbers)["read_ledger_skipped"] is True
    )
    # Retaining one presentation cannot claim delivery of another.
    assert "read_ledger_skipped" not in _prepare(
        resumed, include_line_numbers=not include_line_numbers
    )


def test_failed_offload_still_credits_only_visible_preview(tmp_path: Path):
    (tmp_path / "source.txt").write_text("first\n" + "hidden\n" * 100, encoding="utf-8", newline="")
    (tmp_path / "session").write_text("blocks the store", encoding="utf-8")
    ledger = SessionReadLedger(root=tmp_path)
    result = _prepare(ledger)
    shaped = _offloader(tmp_path, preview=8).maybe_offload(
        tool_name="fs_read",
        tool_call_id="failed-store",
        step=1,
        result=result,
        content_json=json.dumps(result),
    )
    assert shaped.error
    receipt = ledger.record_delivery(result=result, content_for_message=shaped.content_for_message)
    assert receipt["delivered_ranges"] == [{"start_line": 1, "end_line": 1}]
    assert receipt["retrievable_handle"] is None
    assert _prepare(ledger)["content"].startswith("hidden\n")


def test_exact_handles_survive_resume_and_typos_expose_only_current_session(tmp_path: Path):
    parent = SessionArtifactLayout(tmp_path / "parent")
    child = SessionArtifactLayout(tmp_path / "child")
    handles = []
    for layout, contents in ((parent, "parent private"), (child, "child private")):
        artifact = layout.artifact_fs_path("tool_outputs", "same-name.txt")
        artifact.parent.mkdir(parents=True)
        artifact.write_text(contents, encoding="utf-8")
        handles.append(layout.register_artifact(artifact))
    assert handles[0] != handles[1]
    resumed = SessionArtifactLayout(parent.filesystem_root)
    assert (
        session_artifact_read(artifact_layout=resumed, handle=handles[0])["content"]
        == "parent private"
    )
    for unknown in (handles[0], handles[1][:-1], "artifact:../../parent"):
        with pytest.raises(SessionArtifactReadError) as exc:
            session_artifact_read(artifact_layout=child, handle=unknown)
        result = exc.value.result_payload
        assert result["error_code"] == "session_artifact_handle_not_found"
        assert [item["handle"] for item in result["available_handles"]] == [handles[1]]
        assert "parent private" not in str(result)
    assert (
        session_artifact_read(artifact_layout=child, list_handles=True)["available_handles"]
        == child.available_handles()
    )


def test_matching_handle_and_locator_read_only_the_verified_current_artifact(tmp_path: Path):
    layout = SessionArtifactLayout(tmp_path / "session")
    artifact = layout.artifact_fs_path("tool_outputs", "result.txt")
    artifact.parent.mkdir(parents=True)
    artifact.write_text("observed result", encoding="utf-8")
    handle = layout.register_artifact(artifact)
    locator = layout.locator_for_path(artifact)
    assert (
        session_artifact_read(artifact_layout=layout, handle=handle, locator=locator)["content"]
        == "observed result"
    )
    for mismatched in (
        "session_artifacts/tool_outputs/other.txt",
        "session_artifacts/../result.txt",
    ):
        with pytest.raises(SessionArtifactReadError) as exc:
            session_artifact_read(artifact_layout=layout, handle=handle, locator=mismatched)
        assert exc.value.result_payload["error_code"] == "session_artifact_reference_mismatch"
    for invalid_handle in ("artifact:unknown", handle):
        if invalid_handle == handle:
            artifact.write_text("changed result", encoding="utf-8")
        with pytest.raises(SessionArtifactReadError) as exc:
            session_artifact_read(artifact_layout=layout, handle=invalid_handle, locator=locator)
        assert exc.value.result_payload["error_code"] == "session_artifact_handle_not_found"


def test_handle_cannot_silently_resolve_changed_artifact_content(tmp_path: Path):
    layout = SessionArtifactLayout(tmp_path / "session")
    artifact = layout.artifact_fs_path("tool_outputs", "mutable.txt")
    artifact.parent.mkdir(parents=True)
    artifact.write_text("first generation", encoding="utf-8")
    old_handle = layout.register_artifact(artifact)
    artifact.write_text("second generation", encoding="utf-8")
    with pytest.raises(SessionArtifactReadError):
        session_artifact_read(artifact_layout=layout, handle=old_handle)
    new_handle = layout.register_artifact(artifact)
    assert new_handle != old_handle
    assert layout.available_handles() == [
        {"handle": new_handle, "locator": layout.locator_for_path(artifact)}
    ]


def test_copying_foreign_handle_index_does_not_authorize_parent_content(tmp_path: Path):
    parent = SessionArtifactLayout(tmp_path / "parent")
    child = SessionArtifactLayout(tmp_path / "child")
    artifact = parent.artifact_fs_path("private.txt")
    artifact.parent.mkdir(parents=True)
    artifact.write_text("parent evidence", encoding="utf-8")
    handle = parent.register_artifact(artifact)
    from alysis_code.session_artifacts import ARTIFACT_HANDLE_INDEX_DIR

    index_name = handle.removeprefix("artifact:") + ".json"
    copied_index = child.artifact_fs_path(ARTIFACT_HANDLE_INDEX_DIR, index_name)
    copied_index.parent.mkdir(parents=True)
    copied_index.write_bytes(
        parent.artifact_fs_path(ARTIFACT_HANDLE_INDEX_DIR, index_name).read_bytes()
    )
    child.artifact_fs_path("private.txt").write_text("child evidence", encoding="utf-8")
    with pytest.raises(SessionArtifactReadError):
        session_artifact_read(artifact_layout=child, handle=handle)
    assert child.available_handles() == []


def test_handle_discovery_is_bounded_and_can_continue(tmp_path: Path):
    layout = SessionArtifactLayout(tmp_path / "session")
    produced = set()
    for number in range(102):
        artifact = layout.artifact_fs_path("tool_outputs", f"output-{number}.txt")
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(f"output {number}", encoding="utf-8")
        produced.add(layout.register_artifact(artifact))
    first = session_artifact_read(artifact_layout=layout, list_handles=True)
    second = session_artifact_read(
        artifact_layout=layout, list_handles=True, after_handle=first["next_handle"]
    )
    assert len(first["available_handles"]) == 100
    assert len(second["available_handles"]) == 2
    assert second["next_handle"] is None
    assert {
        item["handle"] for page in (first, second) for item in page["available_handles"]
    } == produced


def test_utf8_pages_round_trip_without_replacement_or_split_characters(tmp_path: Path):
    layout = SessionArtifactLayout(tmp_path / "session")
    artifact = layout.artifact_fs_path("unicode.txt")
    artifact.parent.mkdir(parents=True)
    original = "🙂α界\n" * 10
    artifact.write_text(original, encoding="utf-8", newline="")
    handle = layout.register_artifact(artifact)
    offset, chunks = 0, []
    while True:
        page = session_artifact_read(
            artifact_layout=layout, handle=handle, offset=offset, max_bytes=5
        )
        assert page["bytes_returned"] <= 5
        chunks.append(page["content"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert "".join(chunks) == original
    with pytest.raises(SessionArtifactReadError, match="splits a UTF-8"):
        session_artifact_read(artifact_layout=layout, handle=handle, offset=1)
    small = session_artifact_read(artifact_layout=layout, handle=handle, max_bytes=1)
    assert small["bytes_returned"] == 0
    assert small["minimum_bytes_to_progress"] == 4


def test_real_compiler_error_in_offloaded_tail_is_recoverable_by_handle(tmp_path: Path):
    source = tmp_path / "broken_module.py"
    source.write_text("def broken(:\n", encoding="utf-8")
    compiled = subprocess.run(
        [sys.executable, "-m", "py_compile", str(source)],
        capture_output=True,
        text=True,
    )
    assert compiled.returncode != 0
    output = {
        "stdout": "build progress\n" * 200,
        "stderr": compiled.stderr,
        "exit_code": compiled.returncode,
    }
    shaped = _offloader(tmp_path).maybe_offload(
        tool_name="shell_run",
        tool_call_id="real-compiler",
        step=1,
        result=output,
        content_json=json.dumps(output),
    )
    message = json.loads(shaped.content_for_message)
    assert "SyntaxError" not in message["preview"]
    page = session_artifact_read(
        artifact_layout=SessionArtifactLayout(tmp_path / "session"),
        handle=message["artifact_handle"],
    )
    assert "SyntaxError" in json.loads(json.loads(page["content"])["content_json"])["stderr"]


class _ReadClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self):
        self.calls = []

    def chat(self, *, messages, **kwargs):
        self.calls.append(list(messages))
        actions = [
            ("fs_read", {"path": "source.txt", "max_bytes": 100_000}),
            ("fs_read", {"path": "source.txt", "start_line": 299, "end_line": 300}),
            ("fs_read", {"path": "source.txt", "start_line": 299, "end_line": 300}),
            (
                "fs_read",
                {"path": "source.txt", "start_line": 299, "end_line": 300, "force": True},
            ),
        ]
        index = len(self.calls) - 1
        if index < len(actions):
            name, args = actions[index]
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(id=f"read-{index}", name=name, arguments=args)],
                raw={},
            )
        return LLMResponse(
            content="Inspected the source and the diagnostic in its tail.", tool_calls=[], raw={}
        )


def test_real_turn_records_delivery_after_offload_and_allows_tail_then_force(tmp_path: Path):
    content = "source statement with details\n" * 299 + "SyntaxError: unexpected token in tail\n"
    (tmp_path / "source.txt").write_text(content, encoding="utf-8", newline="")
    cfg = AppConfig(model="test-model", routing_mode="code_only", stream=False, max_steps=6)
    cfg.extra_fields = {
        "compaction": {
            "enabled": True,
            "offload_tool_outputs": True,
            "tool_output_offload_threshold_chars": 6000,
            "tool_output_preview_chars": 2000,
            "summarize_conversation": False,
        }
    }
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="readonly",
        yes=True,
        max_steps=6,
        no_log=False,
        api_key_override="test-key",
        session_log_dir_override=tmp_path / "sessions",
        session_id_override="delivery-runtime",
        enable_compaction=False,
        enable_tool_output_offload=True,
        enable_conversation_summarization=False,
    )
    client = _ReadClient()
    session.client = client
    try:
        assert session.run_turn("Inspect the source and explain its final diagnostic.") == 0
        messages = [
            json.loads(message["content"])
            for message in session.messages
            if message.get("role") == "tool"
        ]
        assert messages[0]["offloaded"] is True
        assert "SyntaxError" not in messages[0]["preview"]
        assert "SyntaxError" in messages[1]["content"]
        assert "read_ledger_skipped" not in messages[1]
        assert messages[2]["read_ledger_skipped"] is True
        assert messages[3]["read_ledger_forced"] is True
        assert "SyntaxError" in messages[3]["content"]
        snapshot = session.read_ledger.snapshot()["source.txt"]
        assert snapshot["numbered_ranges"] == [(299, 300)]
        assert snapshot["ranges"][0][0] == 1
        assert all(end < 299 for _, end in snapshot["ranges"])
    finally:
        session.close()
