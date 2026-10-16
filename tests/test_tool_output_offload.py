from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

import alysis_code.compaction.tool_output_offload as offload_module
from alysis_code import private_artifact_io
from alysis_code.agent_loop import create_session
from alysis_code.compaction.settings import resolve_compaction_settings
from alysis_code.compaction.tool_output_offload import ToolOutputOffloader
from alysis_code.config import AppConfig
from alysis_code.ide.artifacts import ArtifactRoot, ArtifactStore
from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.session_artifacts import SessionArtifactLayout
from alysis_code.session_store import read_session_events
from alysis_code.tools.artifacts import (
    SessionArtifactReadError,
    session_artifact_read,
)


class _ArtifactReaderClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, *, locator: str) -> None:
        self._locator = locator
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = stream, on_text_delta, temperature
        self.calls.append({"messages": list(messages), "tools": tools})
        if len(self.calls) == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="read-artifact",
                        name="session_artifact_read",
                        arguments={"locator": self._locator, "max_bytes": 10_000},
                    )
                ],
                raw={},
            )
        return LLMResponse(content="Artifact inspected.", tool_calls=[], raw={})


class _TerminalArtifactReaderClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, *, locator: str) -> None:
        self._locator = locator
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        temperature: float | None = None,
    ) -> LLMResponse:
        _ = stream, on_text_delta, temperature
        self.calls.append({"messages": list(messages), "tools": tools})
        if len(self.calls) == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="read-parent-artifact",
                        name="session_artifact_read",
                        arguments={"locator": self._locator},
                    )
                ],
                raw={},
            )
        return LLMResponse(
            content="The artifact belongs to another session.",
            tool_calls=[],
            raw={},
        )


def test_compaction_settings_default_to_lighter_tool_output_retention() -> None:
    settings = resolve_compaction_settings(AppConfig(model="gpt-5-nano"))

    assert settings.tool_output_offload_threshold_chars == 6000
    assert settings.tool_output_preview_chars == 2000


def test_offloader_does_not_offload_small_output(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    session_artifact_root = tmp_path / "session-store" / "session-1"
    workspace_root.mkdir()
    offloader = ToolOutputOffloader(
        artifact_layout=SessionArtifactLayout(filesystem_root=session_artifact_root),
        workspace_root=workspace_root,
        threshold_chars=200,
        preview_chars=50,
    )
    content_json = json.dumps({"ok": "small"}, ensure_ascii=True)
    result = offloader.maybe_offload(
        tool_name="shell_run",
        tool_call_id="call1",
        step=1,
        result={"ok": "small"},
        content_json=content_json,
    )

    assert result.offloaded is False
    assert result.transcript_shaped is False
    assert result.artifact_locator is None
    assert result.artifact_fs_path is None
    assert result.artifact_readable_via_fs is False
    assert result.message_chars == len(content_json)
    assert result.content_for_message == content_json
    assert not session_artifact_root.exists()


def test_offloader_shows_old_dead_band_output_in_full(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    session_artifact_root = tmp_path / "session-store" / "session-shaped"
    workspace_root.mkdir()
    offloader = ToolOutputOffloader(
        artifact_layout=SessionArtifactLayout(filesystem_root=session_artifact_root),
        workspace_root=workspace_root,
        threshold_chars=6000,
        preview_chars=2000,
    )
    payload = {"path": "README.md", "content": "A" * 1800, "truncated": False}
    content_json = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))

    result = offloader.maybe_offload(
        tool_name="fs_read",
        tool_call_id="call-shaped",
        step=2,
        result=payload,
        content_json=content_json,
    )

    assert result.offloaded is False
    assert result.transcript_shaped is False
    assert result.artifact_locator is None
    assert result.artifact_fs_path is None
    assert result.artifact_readable_via_fs is False
    assert result.message_chars == result.original_chars
    assert result.content_for_message == content_json
    assert not session_artifact_root.exists()


def test_offloader_offloads_large_output_and_writes_artifact_in_session_root(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    session_artifact_root = tmp_path / "session-store" / "session_one"
    workspace_root.mkdir()
    offloader = ToolOutputOffloader(
        artifact_layout=SessionArtifactLayout(filesystem_root=session_artifact_root),
        workspace_root=workspace_root,
        threshold_chars=50,
        preview_chars=40,
    )
    large_text = "x" * 500
    payload = {"stdout": large_text}
    content_json = json.dumps(payload, ensure_ascii=True)

    result = offloader.maybe_offload(
        tool_name="shell/run",
        tool_call_id="call:abc",
        step=3,
        result=payload,
        content_json=content_json,
    )

    assert result.offloaded is True
    assert result.transcript_shaped is True
    assert result.artifact_locator == (
        "session_artifacts/tool_outputs/step3_shell_run_call_abc.json"
    )
    assert result.artifact_fs_path is not None
    assert not str(result.artifact_locator).startswith("/")
    assert result.artifact_readable_via_fs is False
    assert result.artifact_location == "external_session_store"
    artifact_path = Path(result.artifact_fs_path)
    assert artifact_path.exists()
    assert artifact_path.is_absolute()
    assert artifact_path.is_relative_to(session_artifact_root.resolve())
    assert not (workspace_root / ".alysis").exists()

    saved = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert saved["tool_name"] == "shell/run"
    assert saved["tool_call_id"] == "call:abc"
    assert saved["step"] == 3
    assert "result" not in saved
    assert json.loads(saved["content_json"])["stdout"] == large_text
    assert artifact_path.read_text(encoding="utf-8").count(large_text) == 1

    stub = json.loads(result.content_for_message)
    assert stub["offloaded"] is True
    assert stub["artifact_locator"] == result.artifact_locator
    assert "artifact_path" not in stub
    assert stub["artifact_saved"] is True
    assert stub["artifact_readable_via_fs"] is False
    assert stub["artifact_location"] == "external_session_store"
    assert "fs_read_path" not in stub
    assert stub["full_output"] == (
        f"Truncated to 40 of {result.original_chars} chars. Full output saved as "
        f"{result.artifact_locator}; use session_artifact_read with that locator."
    )
    assert stub["raw_saved_in_session_log"] is False
    assert "summary" in stub
    assert "preview" in stub
    assert len(stub["preview"]) <= 40 + len("...(truncated)")
    assert result.error is None

    read = session_artifact_read(
        artifact_layout=SessionArtifactLayout(filesystem_root=session_artifact_root),
        locator=str(result.artifact_locator),
        max_bytes=20_000,
    )
    assert read["locator"] == result.artifact_locator
    assert read["truncated"] is False
    assert json.loads(read["content"])["content_json"] == content_json


@pytest.mark.parametrize(
    "locator",
    (
        "",
        "/session_artifacts/tool_outputs/output.json",
        "tool_outputs/output.json",
        "session_artifacts/../outside.json",
        "session_artifacts\\tool_outputs\\output.json",
    ),
)
def test_session_artifact_reader_rejects_malformed_or_escaping_locator(
    tmp_path: Path,
    locator: str,
) -> None:
    layout = SessionArtifactLayout(filesystem_root=tmp_path / "session")

    with pytest.raises(SessionArtifactReadError, match="locator") as exc_info:
        session_artifact_read(artifact_layout=layout, locator=locator)

    payload = exc_info.value.result_payload
    assert payload["error_code"] == "invalid_session_artifact_locator"
    assert payload["terminal"] is True
    assert payload["retryable"] is False
    assert "starts with session_artifacts/" in payload["error"]
    assert "Guessing a locator will not work" in payload["error"]


def test_session_artifact_reader_returns_clean_error_for_unknown_locator(
    tmp_path: Path,
) -> None:
    layout = SessionArtifactLayout(filesystem_root=tmp_path / "session")

    with pytest.raises(SessionArtifactReadError, match="not found in this session") as exc_info:
        session_artifact_read(
            artifact_layout=layout,
            locator="session_artifacts/tool_outputs/missing.json",
        )

    payload = exc_info.value.result_payload
    assert payload["error_code"] == "session_artifact_not_found"
    assert payload["terminal"] is True
    assert payload["retryable"] is False
    assert "other sessions are not authorized" in payload["error"]
    assert payload["available_handles"] == []


def test_child_runtime_rejects_parent_session_artifact_locator_terminally(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    sessions_dir = tmp_path / "sessions"
    cfg = AppConfig(model="test-model", routing_mode="code_only", stream=False, max_steps=3)
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "test-model": {"context_window_tokens": 4096, "max_output_tokens": 512},
            },
            "default": {"context_window_tokens": 4096, "max_output_tokens": 512},
        }
    }
    parent = create_session(
        cfg=cfg,
        root=workspace_root,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        session_id_override="artifact-parent-runtime",
        enable_compaction=False,
    )
    parent_artifact = parent.store.session_artifact_layout.artifact_fs_path(
        "tool_outputs",
        "parent-only.txt",
    )
    parent_artifact.parent.mkdir(parents=True, exist_ok=True)
    parent_artifact.write_text("parent-only", encoding="utf-8")
    parent_locator = parent.store.session_artifact_layout.locator_for_path(parent_artifact)

    child = create_session(
        cfg=cfg,
        root=workspace_root,
        mode="readonly",
        yes=True,
        max_steps=3,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=sessions_dir,
        session_id_override="artifact-child-runtime",
        enable_compaction=False,
        subagent_depth=1,
        runtime_kind="subagent",
    )
    client = _TerminalArtifactReaderClient(locator=parent_locator)
    child.client = client  # type: ignore[assignment]

    try:
        assert child.run_turn("Read the parent artifact.") == 0
        child_log_path = child.store.path
    finally:
        child.close()
        parent.close()

    assert len(client.calls) == 2
    tool_message = next(
        message for message in client.calls[1]["messages"] if message.get("role") == "tool"
    )
    result = json.loads(str(tool_message["content"]))
    assert result["error_code"] == "session_artifact_not_found"
    assert result["terminal"] is True
    assert result["retryable"] is False
    assert "other sessions are not authorized" in result["error"]
    assert result["available_handles"] == []
    assert "parent-only" not in str(result)

    mismatch_events = [
        event
        for event in read_session_events(child_log_path)
        if event.get("type") == "session_artifact_read_not_found"
    ]
    assert len(mismatch_events) == 1
    assert mismatch_events[0]["payload"] == {
        "locator": parent_locator,
        "runtime_kind": "subagent",
        "terminal": True,
    }


def test_session_artifact_reader_rejects_symlink_escape(tmp_path: Path) -> None:
    artifact_root = tmp_path / "session"
    outside = tmp_path / "outside.txt"
    artifact_root.mkdir()
    outside.write_text("outside", encoding="utf-8")
    try:
        (artifact_root / "escape.txt").symlink_to(outside)
    except OSError:
        pytest.skip("File symlinks are unavailable on this host")

    with pytest.raises(SessionArtifactReadError, match="outside"):
        session_artifact_read(
            artifact_layout=SessionArtifactLayout(filesystem_root=artifact_root),
            locator="session_artifacts/escape.txt",
        )


def test_session_artifact_reader_is_bounded_and_redacts_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "session-artifact-secret-canary"
    monkeypatch.setenv("ALYSIS_API_KEY", secret)
    layout = SessionArtifactLayout(filesystem_root=tmp_path / "session")
    artifact_path = layout.artifact_fs_path("tool_outputs", "large.txt")
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(f"{secret}:" + "x" * 500, encoding="utf-8")

    result = session_artifact_read(
        artifact_layout=layout,
        locator=layout.locator_for_path(artifact_path),
        max_bytes=128,
    )

    assert result["truncated"] is True
    assert result["max_bytes"] == 128
    assert secret not in result["content"]
    assert "<redacted>" in result["content"]


def test_session_artifact_reader_pages_without_gaps_or_overlap(tmp_path: Path) -> None:
    layout = SessionArtifactLayout(filesystem_root=tmp_path / "session")
    artifact_path = layout.artifact_fs_path("tool_outputs", "paged.txt")
    artifact_path.parent.mkdir(parents=True)
    original = "".join(f"record-{index:04d}\n" for index in range(100))
    artifact_path.write_bytes(original.encode("utf-8"))
    locator = layout.locator_for_path(artifact_path)

    offset = 0
    pages: list[str] = []
    while True:
        page = session_artifact_read(
            artifact_layout=layout,
            locator=locator,
            offset=offset,
            max_bytes=37,
        )
        assert page["offset"] == offset
        assert page["bytes_returned"] == len(page["content"].encode("utf-8"))
        assert page["size"] == len(original.encode("utf-8"))
        pages.append(str(page["content"]))
        if not page["has_more"]:
            assert page["next_offset"] is None
            break
        assert page["next_offset"] == offset + page["bytes_returned"]
        offset = int(page["next_offset"])

    assert "".join(pages) == original


@pytest.mark.parametrize("offset", (-1, "not-an-integer"))
def test_session_artifact_reader_rejects_invalid_offset(
    tmp_path: Path,
    offset: object,
) -> None:
    layout = SessionArtifactLayout(filesystem_root=tmp_path / "session")
    artifact_path = layout.artifact_fs_path("tool_outputs", "offset.txt")
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("payload", encoding="utf-8")

    with pytest.raises(SessionArtifactReadError, match="offset"):
        session_artifact_read(
            artifact_layout=layout,
            locator=layout.locator_for_path(artifact_path),
            offset=offset,
        )


def test_session_artifact_reader_past_end_returns_empty_page(tmp_path: Path) -> None:
    layout = SessionArtifactLayout(filesystem_root=tmp_path / "session")
    artifact_path = layout.artifact_fs_path("tool_outputs", "short.txt")
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("short", encoding="utf-8")

    result = session_artifact_read(
        artifact_layout=layout,
        locator=layout.locator_for_path(artifact_path),
        offset=100,
        max_bytes=10,
    )

    assert result["offset"] == 100
    assert result["bytes_returned"] == 0
    assert result["size"] == 5
    assert result["content"] == ""
    assert result["has_more"] is False
    assert result["next_offset"] is None
    assert result["truncated"] is False


def test_run_turn_does_not_reoffload_session_artifact_reader_output(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    cfg = AppConfig(model="test-model", routing_mode="code_only", stream=False, max_steps=4)
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "test-model": {"context_window_tokens": 4096, "max_output_tokens": 512},
            },
            "default": {"context_window_tokens": 4096, "max_output_tokens": 512},
        },
        "compaction": {
            "enabled": True,
            "offload_tool_outputs": True,
            "tool_output_offload_threshold_chars": 6000,
            "tool_output_preview_chars": 2000,
            "summarize_conversation": False,
        },
    }
    session = create_session(
        cfg=cfg,
        root=workspace_root,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        session_id_override="artifact-reader-runtime",
        enable_compaction=False,
        enable_tool_output_offload=True,
        enable_conversation_summarization=False,
    )
    artifact_path = session.store.session_artifact_layout.artifact_fs_path(
        "tool_outputs",
        "large-source.txt",
    )
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    original = "artifact-page:" + "x" * 8_000
    artifact_path.write_text(original, encoding="utf-8")
    client = _ArtifactReaderClient(
        locator=session.store.session_artifact_layout.locator_for_path(artifact_path)
    )
    session.client = client  # type: ignore[assignment]

    try:
        assert session.run_turn("Inspect the saved artifact.") == 0
        log_path = session.store.path
    finally:
        session.close()

    assert len(client.calls) == 2
    followup_messages = client.calls[1]["messages"]
    tool_message = next(
        message for message in reversed(followup_messages) if message.get("role") == "tool"
    )
    read_result = json.loads(str(tool_message["content"]))
    assert read_result["content"] == original
    assert read_result["bytes_returned"] == len(original.encode("utf-8"))
    assert read_result["has_more"] is False
    assert "offloaded" not in read_result

    reader_offloads = [
        event
        for event in read_session_events(log_path)
        if event.get("type") == "tool_output_offloaded"
        and (event.get("payload") or {}).get("tool") == "session_artifact_read"
    ]
    assert reader_offloads == []


def test_offloaded_truncated_read_keeps_continuation_and_artifact_reference(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    offloader = ToolOutputOffloader(
        artifact_layout=SessionArtifactLayout(filesystem_root=tmp_path / "session-store"),
        workspace_root=workspace_root,
        threshold_chars=50,
        preview_chars=20,
    )
    payload = {
        "path": "large.txt",
        "content": "x" * 500,
        "truncated": True,
        "total_lines": 20,
        "returned_range": {"start_line": 1, "end_line": 8},
        "next_range": {"start_line": 9, "end_line": 20},
    }

    result = offloader.maybe_offload(
        tool_name="fs_read",
        tool_call_id="continued-read",
        step=1,
        result=payload,
        content_json=json.dumps(payload, separators=(",", ":")),
    )

    assert result.offloaded is True
    stub = json.loads(result.content_for_message)
    assert stub["artifact_locator"] == result.artifact_locator
    assert stub["total_lines"] == 20
    assert stub["returned_range"] == {"start_line": 1, "end_line": 8}
    assert stub["next_range"] == {"start_line": 9, "end_line": 20}


@pytest.mark.parametrize("next_offset", [20000, None])
def test_offloaded_diff_preserves_pagination_and_full_patch(
    tmp_path: Path, next_offset: int | None
) -> None:
    layout = SessionArtifactLayout(filesystem_root=tmp_path / "session-store")
    offloader = ToolOutputOffloader(
        artifact_layout=layout, workspace_root=tmp_path, threshold_chars=50, preview_chars=20
    )
    payload = {
        "diff_id": "a" * 64,
        "offset": 0,
        "next_offset": next_offset,
        "total_chars": 40000,
        "truncated": next_offset is not None,
        "diff": "patch" * 4000,
    }
    result = offloader.maybe_offload(
        tool_name="git_diff",
        tool_call_id="diff-page",
        step=1,
        result=payload,
        content_json=json.dumps(payload),
    )
    stub = json.loads(result.content_for_message)
    assert stub["diff_id"] == payload["diff_id"]
    assert stub["next_offset"] == next_offset
    assert stub["total_chars"] == 40000
    assert stub["artifact_locator"]
    saved = json.loads(Path(result.artifact_fs_path).read_text(encoding="utf-8"))
    assert json.loads(saved["content_json"])["diff"] == payload["diff"]


@pytest.mark.parametrize(
    "tool_name",
    [
        "embedded_browser_open",
        "embedded_browser_read",
        "embedded_browser_navigate",
        "embedded_browser_back",
        "embedded_browser_forward",
        "embedded_browser_reload",
    ],
)
@pytest.mark.parametrize("next_offset", [6000, None])
def test_offloaded_browser_page_preserves_tab_and_continuation(
    tmp_path: Path,
    tool_name: str,
    next_offset: int | None,
) -> None:
    offloader = ToolOutputOffloader(
        artifact_layout=SessionArtifactLayout(filesystem_root=tmp_path / "session-store"),
        workspace_root=tmp_path,
        threshold_chars=6000,
        preview_chars=200,
    )
    payload = {
        "tab_id": "tab-1",
        "title": "Example page",
        "url": "https://example.com/",
        "text": "x" * 6000,
        "links": [],
        "next_offset": next_offset,
    }
    result = offloader.maybe_offload(
        tool_name=tool_name,
        tool_call_id="browser-page",
        step=1,
        result=payload,
        content_json=json.dumps(payload),
    )
    assert result.offloaded is True
    stub = json.loads(result.content_for_message)
    for key in ("tab_id", "title", "url", "next_offset"):
        assert stub[key] == payload[key]
    saved = json.loads(Path(result.artifact_fs_path).read_text(encoding="utf-8"))
    assert json.loads(saved["content_json"]) == payload


def test_session_data_dir_override_keeps_offloads_outside_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    external_data_dir = tmp_path / "external-data"
    workspace_root.mkdir()
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(external_data_dir))
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=workspace_root,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="test-key",
        session_id_override="external-offload",
        enable_compaction=False,
        enable_tool_output_offload=True,
        enable_conversation_summarization=False,
    )
    try:
        offloader = session.tool_output_offloader
        assert offloader is not None
        content_json = json.dumps({"stdout": "x" * 8_000})
        result = offloader.maybe_offload(
            tool_name="shell_run",
            tool_call_id="external-call",
            step=1,
            result={},
            content_json=content_json,
        )

        assert result.offloaded is True
        assert Path(str(result.artifact_fs_path)).is_relative_to(
            session.store.session_artifact_root.resolve()
        )
        assert session.store.session_artifact_root.is_relative_to(external_data_dir.resolve())
        assert not (workspace_root / ".alysis").exists()
    finally:
        session.close()


def test_offloader_write_failure_returns_json_stub(tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / "workspace"
    session_artifact_root = tmp_path / "session-store" / "session-fail"
    workspace_root.mkdir()
    offloader = ToolOutputOffloader(
        artifact_layout=SessionArtifactLayout(filesystem_root=session_artifact_root),
        workspace_root=workspace_root,
        threshold_chars=50,
        preview_chars=40,
    )

    def _raise_write_error(*_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        raise OSError("disk full")

    monkeypatch.setattr(offload_module, "_atomic_private_write_text", _raise_write_error)
    payload = {"stdout": "x" * 500}
    content_json = json.dumps(payload, ensure_ascii=True)

    result = offloader.maybe_offload(
        tool_name="shell_run",
        tool_call_id="call-fail",
        step=2,
        result=payload,
        content_json=content_json,
    )

    assert result.offloaded is False
    assert result.transcript_shaped is True
    assert result.artifact_locator is None
    assert result.artifact_fs_path is None
    assert result.artifact_readable_via_fs is False
    assert result.error is not None
    stub = json.loads(result.content_for_message)
    assert stub["offloaded"] is False
    assert stub["transcript_shaped"] is True
    assert stub["tool"] == "shell_run"
    assert stub["tool_call_id"] == "call-fail"
    assert stub["step"] == 2
    assert "preview" in stub
    assert "summary" in stub
    assert "error" in stub
    assert "the rest is not readable via fs" in stub["full_output"]
    assert "Re-run the command narrowed" in stub["full_output"]


def test_offloader_atomic_publish_preserves_old_artifact_after_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    session_artifact_root = tmp_path / "session-store" / "atomic-session"
    workspace_root.mkdir()
    offloader = ToolOutputOffloader(
        artifact_layout=SessionArtifactLayout(filesystem_root=session_artifact_root),
        workspace_root=workspace_root,
        threshold_chars=50,
        preview_chars=40,
    )
    first_json = json.dumps({"stdout": "first-committed-" + "a" * 500})
    first = offloader.maybe_offload(
        tool_name="shell_run",
        tool_call_id="same-call",
        step=7,
        result={},
        content_json=first_json,
    )
    assert first.offloaded is True
    artifact_path = Path(str(first.artifact_fs_path))
    committed = artifact_path.read_bytes()

    def _crash_before_replace(_source: Path, _target: Path) -> None:
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(private_artifact_io.os, "replace", _crash_before_replace)
    second = offloader.maybe_offload(
        tool_name="shell_run",
        tool_call_id="same-call",
        step=7,
        result={},
        content_json=json.dumps({"stdout": "second-uncommitted-" + "b" * 500}),
    )

    assert second.offloaded is False
    assert artifact_path.read_bytes() == committed
    assert not list(artifact_path.parent.glob(f".{artifact_path.name}.*.tmp"))


@pytest.mark.skipif(os.name == "nt", reason="Windows ACLs are not POSIX mode bits")
def test_offloader_artifact_is_private_on_posix(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    offloader = ToolOutputOffloader(
        artifact_layout=SessionArtifactLayout(
            filesystem_root=tmp_path / "sessions" / "private-session"
        ),
        workspace_root=workspace_root,
        threshold_chars=10,
        preview_chars=5,
    )

    result = offloader.maybe_offload(
        tool_name="shell_run",
        tool_call_id="private",
        step=1,
        result={},
        content_json=json.dumps({"stdout": "private" * 100}),
    )

    artifact_path = Path(str(result.artifact_fs_path))
    assert artifact_path.stat().st_mode & 0o777 == 0o600
    assert artifact_path.parent.stat().st_mode & 0o777 == 0o700


def test_offloader_is_session_scoped_and_artifact_read_is_bounded_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    secret = "secret-offload-read-canary"
    monkeypatch.setenv("ALYSIS_API_KEY", secret)
    outputs: list[tuple[ToolOutputOffloader, str]] = []
    for session_id, marker in (("session-a", "alpha"), ("session-b", "bravo")):
        offloader = ToolOutputOffloader(
            artifact_layout=SessionArtifactLayout(
                filesystem_root=tmp_path / "sessions" / session_id
            ),
            workspace_root=workspace_root,
            threshold_chars=20,
            preview_chars=10,
        )
        result = offloader.maybe_offload(
            tool_name="shell_run",
            tool_call_id="same-call",
            step=1,
            result={},
            content_json=json.dumps({"stdout": f"{marker}:{secret}:" + "x" * 500}),
        )
        assert result.offloaded is True
        assert secret not in result.content_for_message
        outputs.append((offloader, marker))

    assert outputs[0][0].artifact_root != outputs[1][0].artifact_root
    first_store = ArtifactStore([ArtifactRoot("session", outputs[0][0].artifact_root)])
    listed = first_store.list()
    tool_outputs = [
        artifact
        for artifact in listed["artifacts"]
        if str(artifact["path"]).startswith("tool_outputs/")
    ]
    assert len(tool_outputs) == 1
    artifact_id = str(tool_outputs[0]["artifact_id"])
    read = first_store.read(artifact_id, max_bytes=256)
    serialized = json.dumps(read)
    assert "alpha" in serialized
    assert "bravo" not in serialized
    assert secret not in serialized
    assert "<redacted>" in serialized
    assert read["truncated"] is True


def test_offloader_rejects_workspace_symlink_escape_and_falls_back_to_session_store(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspace"
    outside = tmp_path / "workspace-controlled-target"
    session_artifact_root = tmp_path / "trusted-session-store" / "escape-session"
    workspace_root.mkdir()
    outside.mkdir()
    try:
        (workspace_root / ".alysis").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are unavailable on this host")
    offloader = ToolOutputOffloader(
        artifact_layout=SessionArtifactLayout(filesystem_root=session_artifact_root),
        workspace_root=workspace_root,
        threshold_chars=20,
        preview_chars=10,
    )

    result = offloader.maybe_offload(
        tool_name="shell_run",
        tool_call_id="escape-call",
        step=1,
        result={},
        content_json=json.dumps({"stdout": "contained" + "x" * 500}),
    )

    assert result.offloaded is True
    assert offloader.artifact_root == session_artifact_root.resolve()
    assert Path(str(result.artifact_fs_path)).is_relative_to(session_artifact_root.resolve())
    assert result.artifact_readable_via_fs is False
    assert list(outside.rglob("*")) == []


def test_offloader_non_fs_readable_stub_guides_to_session_artifact_reader(
    tmp_path: Path,
) -> None:
    session_artifact_root = tmp_path / "session-store" / "session-external"
    offloader = ToolOutputOffloader(
        artifact_layout=SessionArtifactLayout(filesystem_root=session_artifact_root),
        workspace_root=None,
        threshold_chars=50,
        preview_chars=40,
    )
    payload = {"stdout": "x" * 500}
    content_json = json.dumps(payload, ensure_ascii=True)

    result = offloader.maybe_offload(
        tool_name="shell_run",
        tool_call_id="call-external",
        step=4,
        result=payload,
        content_json=content_json,
    )

    assert result.offloaded is True
    assert result.artifact_readable_via_fs is False
    assert result.artifact_location == "external_session_store"
    stub = json.loads(result.content_for_message)
    assert stub["artifact_readable_via_fs"] is False
    assert stub["artifact_location"] == "external_session_store"
    assert str(result.artifact_locator) in stub["full_output"]
    assert "use session_artifact_read with that locator" in stub["full_output"]


def test_create_session_disable_compaction_does_not_create_offloader(tmp_path: Path) -> None:
    cfg = AppConfig(model="gpt-5-nano")
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="readonly",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="test-key",
        enable_compaction=False,
    )
    try:
        assert session.tool_output_offloader is None
        assert session.conversation_compactor is None
    finally:
        session.close()


def test_create_session_can_enable_offload_without_conversation_summarization(
    tmp_path: Path,
) -> None:
    sessions_dir = tmp_path / "logs"
    cfg = AppConfig(model="gpt-5-nano")
    cfg.extra_fields = {
        "compaction": {
            "enabled": True,
            "offload_tool_outputs": True,
            "summarize_conversation": True,
        }
    }
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="readonly",
        yes=False,
        max_steps=1,
        no_log=False,
        api_key_override="test-key",
        session_log_dir_override=sessions_dir,
        session_id_override="split-offload",
        enable_compaction=False,
        enable_tool_output_offload=True,
        enable_conversation_summarization=False,
    )
    try:
        assert session.tool_output_offloader is not None
        assert session.conversation_compactor is None
        assert session.tool_output_offload_enabled is True
        assert session.conversation_summarization_enabled is False
    finally:
        session.close()

    events = list(read_session_events(sessions_dir / "split-offload.jsonl"))
    session_start = next(event for event in events if event.get("type") == "session_start")
    payload = dict(session_start.get("payload") or {})
    assert payload["requested_enable_compaction"] is False
    assert payload["requested_tool_output_offload"] is True
    assert payload["requested_conversation_summarization"] is False
    assert payload["logging_enabled"] is True
    assert payload["explicit_session_artifact_root"] is True
    assert payload["tool_output_offload_artifact_persistence_available"] is True
    assert payload["tool_output_offload_enabled"] is True
    assert payload["conversation_summarization_enabled"] is False


def test_create_session_no_log_without_explicit_artifact_root_disables_offload(
    tmp_path: Path,
) -> None:
    cfg = AppConfig(model="gpt-5-nano")
    cfg.extra_fields = {
        "compaction": {
            "enabled": True,
            "offload_tool_outputs": True,
            "summarize_conversation": False,
        }
    }
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="readonly",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="test-key",
        enable_compaction=False,
        enable_tool_output_offload=True,
        enable_conversation_summarization=False,
    )
    try:
        assert session.tool_output_offloader is None
        assert session.tool_output_offload_enabled is False
    finally:
        session.close()


def test_create_session_no_log_with_explicit_artifact_root_keeps_offload_enabled(
    tmp_path: Path,
) -> None:
    sessions_dir = tmp_path / "runtime" / "sessions"
    cfg = AppConfig(model="gpt-5-nano")
    cfg.extra_fields = {
        "compaction": {
            "enabled": True,
            "offload_tool_outputs": True,
            "summarize_conversation": False,
        }
    }
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="readonly",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="test-key",
        session_log_dir_override=sessions_dir,
        session_id_override="offload-runtime",
        enable_compaction=False,
        enable_tool_output_offload=True,
        enable_conversation_summarization=False,
    )
    try:
        assert session.tool_output_offloader is not None
        assert session.tool_output_offload_enabled is True
    finally:
        session.close()


@pytest.mark.parametrize(
    "tool_name",
    [
        "embedded_browser_snapshot",
        "embedded_browser_click",
        "embedded_browser_type",
        "embedded_browser_select",
        "embedded_browser_scroll",
        "embedded_browser_key",
    ],
)
def test_offloaded_browser_snapshot_keeps_observation_identity(tmp_path, tool_name):
    offloader = ToolOutputOffloader(
        artifact_layout=SessionArtifactLayout(filesystem_root=tmp_path / "session-store"),
        workspace_root=tmp_path,
        threshold_chars=6000,
        preview_chars=200,
    )
    payload = {
        "tab_id": "tab",
        "snapshot_id": "observed",
        "url": "https://example.com/",
        "text": "x" * 6000,
        "elements": [{"ref": "e1", "role": "textbox", "name": "Date", "input_type": "date"}],
        "focused_ref": "e1",
        "viewport": {"width": 800, "height": 600},
    }
    result = offloader.maybe_offload(
        tool_name=tool_name,
        tool_call_id="snapshot",
        step=1,
        result=payload,
        content_json=json.dumps(payload),
    )
    stub = json.loads(result.content_for_message)
    assert stub["snapshot_id"] == "observed"
    assert stub["elements"] == payload["elements"]
    assert stub["elements_truncated"] is False
    assert stub["viewport"] == payload["viewport"]
    assert stub["focused_ref"] == "e1"
    saved = json.loads(Path(result.artifact_fs_path).read_text())
    assert json.loads(saved["content_json"])["elements"] == payload["elements"]


def test_browser_offloaded_controls_are_bounded_and_preserve_real_refs(tmp_path):
    offloader = ToolOutputOffloader(
        artifact_layout=SessionArtifactLayout(filesystem_root=tmp_path / "session-store"),
        workspace_root=tmp_path,
        threshold_chars=6000,
        preview_chars=200,
    )
    elements = [{"ref": f"control-{i}", "role": "button", "name": "x" * 500} for i in range(80)]
    payload = {"tab_id": "tab", "snapshot_id": "observed", "text": "x" * 6000, "elements": elements}
    result = offloader.maybe_offload(
        tool_name="embedded_browser_snapshot",
        tool_call_id="bounded",
        step=1,
        result=payload,
        content_json=json.dumps(payload),
    )
    stub = json.loads(result.content_for_message)
    assert stub["elements_truncated"] is True
    assert 0 < len(stub["elements"]) < 20
    assert len(json.dumps(stub["elements"], ensure_ascii=False)) <= 3500
    assert all(element in elements for element in stub["elements"])
    assert "never guess" in stub["element_details"]
