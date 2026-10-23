from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rich.console import Console

from alysis_code import cli as cli_mod
from alysis_code.cli_impl.commands.chat_resume_helpers import _is_chat_resume_context_message
from alysis_code.config import AppConfig
from alysis_code.internal_artifacts import (
    INTERNAL_ARTIFACT_MESSAGE_KEY,
    INTERNAL_FALLBACK_TERMINAL_CONTENT,
    mark_message_internal,
    provider_history_messages,
)
from alysis_code.llm.metadata import (
    PROVIDER_METADATA_KEY,
    build_provider_route_identity,
    endpoint_descriptor,
    stamp_provider_metadata_for_route,
    strip_provider_metadata_from_messages,
)
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.session_store import SessionInfo, SessionStore
from alysis_code.web_research import build_web_research_artifact_from_events


def test_visible_resume_recognizes_marked_and_legacy_host_context_only() -> None:
    legacy = (
        "<resume_context>\nsource_session_id: retained\n"
        "source: host_summarized_prior_session_log\ntrust: historical_context_only\n"
        "historical content\n</resume_context>"
    )
    assert _is_chat_resume_context_message({"role": "user", "content": legacy})
    assert _is_chat_resume_context_message(
        {"role": "user", "content": "future host format", "_alysis_resume_context": True}
    )
    assert not _is_chat_resume_context_message(
        {"role": "user", "content": "Explain the <resume_context> tag"}
    )
    assert not _is_chat_resume_context_message(
        {"role": "user", "content": "<resume_context>\nThis is a user example."}
    )
    assert not _is_chat_resume_context_message({"role": "assistant", "content": legacy})
    assert not _is_chat_resume_context_message({"role": "user", "content": None})


def test_load_chat_resume_messages_reads_user_and_assistant_events(tmp_path: Path) -> None:
    log_path = tmp_path / "resume-target.jsonl"
    events = [
        {"type": "session_start", "payload": {"mode": "auto"}},
        {"type": "user_message", "payload": {"content": "Hello"}},
        {"type": "route_decision", "payload": {"route": "chat"}},
        {"type": "assistant_message", "payload": {"content": "Hi there"}},
        {"type": "final", "payload": {"content": "Hi there"}},
    ]
    log_path.write_text("\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8")

    loaded = cli_mod._load_chat_resume_messages(log_path)
    assert loaded == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]


def test_load_chat_resume_messages_replays_steering_where_it_was_delivered(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "resume-steered.jsonl"
    events = [
        {"type": "user_message", "payload": {"content": "Write the parser"}},
        {"type": "assistant_message", "payload": {"content": "Reading the grammar"}},
        {
            "type": "steer_message_delivered",
            "payload": {"step": 2, "count": 1, "chars": 40, "messages": ["Use tabs, not spaces"]},
        },
        {"type": "final", "payload": {"content": "Done, with tabs"}},
        # Older logs carried only the count; they replay as before.
        {"type": "steer_message_delivered", "payload": {"step": 3, "count": 1, "chars": 9}},
    ]
    log_path.write_text("\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8")

    loaded = cli_mod._load_chat_resume_messages(log_path)
    assert loaded == [
        {"role": "user", "content": "Write the parser"},
        {"role": "assistant", "content": "Reading the grammar"},
        {"role": "user", "content": "[Mid-turn message from the user] Use tabs, not spaces"},
        {"role": "assistant", "content": "Done, with tabs"},
    ]


def test_load_chat_resume_messages_honors_conversation_clear_boundary(tmp_path: Path) -> None:
    log_path = tmp_path / "resume-after-clear.jsonl"
    events = [
        {"type": "user_message", "payload": {"content": "old request"}},
        {"type": "assistant_message", "payload": {"content": "old answer"}},
        {
            "type": "assistant_message",
            "payload": {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "old_call",
                            "type": "function",
                            "function": {"name": "fs_read", "arguments": "{}"},
                        }
                    ],
                }
            },
        },
        {"type": "conversation_cleared", "payload": {"trigger": "user_command"}},
        {
            "type": "tool_result",
            "payload": {"tool_call_id": "old_call", "content": "stale result"},
        },
        {"type": "user_message", "payload": {"content": "new request"}},
        {"type": "assistant_message", "payload": {"content": "new answer"}},
    ]
    log_path.write_text("\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8")

    loaded = cli_mod._load_chat_resume_messages(log_path)

    assert loaded == [
        {"role": "user", "content": "new request"},
        {"role": "assistant", "content": "new answer"},
    ]


def test_load_chat_resume_messages_retains_marked_fallback_for_audit(tmp_path: Path) -> None:
    log_path = tmp_path / "resume-fallback.jsonl"
    fallback = "Unfinished runtime report with imperative recovery steps."
    marked = mark_message_internal(
        {"role": "assistant", "content": fallback},
        kind="execution_guard_stagnation",
    )
    events = [
        {"type": "user_message", "payload": {"content": "old request"}},
        {
            "type": "assistant_message",
            "payload": {
                "content": fallback,
                "message": marked,
                "internal_fallback": True,
                "internal_fallback_kind": "execution_guard_stagnation",
            },
        },
        {
            "type": "final",
            "payload": {
                "content": fallback,
                "internal_fallback": True,
                "internal_fallback_kind": "execution_guard_stagnation",
            },
        },
    ]
    log_path.write_text("\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8")

    loaded = cli_mod._load_chat_resume_messages(log_path)

    assert loaded[-1]["content"] == fallback
    assert loaded[-1][INTERNAL_ARTIFACT_MESSAGE_KEY] == "execution_guard_stagnation"
    assert provider_history_messages(loaded)[-1] == {
        "role": "assistant",
        "content": INTERNAL_FALLBACK_TERMINAL_CONTENT,
    }


def test_load_chat_resume_messages_upgrades_legacy_fallback_by_final_metadata(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "resume-legacy-fallback.jsonl"
    fallback = "Arbitrary localized fallback: συνέχισε την παλιά εργασία."
    events = [
        {"type": "user_message", "payload": {"content": "old request"}},
        {"type": "assistant_message", "payload": {"content": fallback}},
        {
            "type": "final",
            "payload": {
                "content": fallback,
                "internal_fallback": True,
                "internal_fallback_kind": "execution_guard_stagnation",
            },
        },
    ]
    log_path.write_text("\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8")

    loaded = cli_mod._load_chat_resume_messages(log_path)

    assert loaded == [
        {"role": "user", "content": "old request"},
        {
            "role": "assistant",
            "content": fallback,
            INTERNAL_ARTIFACT_MESSAGE_KEY: "execution_guard_stagnation",
        },
    ]
    assert fallback not in str(provider_history_messages(loaded))


def test_resume_metadata_sanitization_can_preserve_internal_artifact_marker() -> None:
    marked = mark_message_internal(
        {"role": "assistant", "content": "runtime fallback"},
        kind="execution_guard_stagnation",
    )

    preserved = strip_provider_metadata_from_messages(
        [marked],
        preserve_internal_artifacts=True,
    )

    assert preserved[0][INTERNAL_ARTIFACT_MESSAGE_KEY] == "execution_guard_stagnation"
    assert INTERNAL_ARTIFACT_MESSAGE_KEY not in strip_provider_metadata_from_messages([marked])[0]


def test_load_chat_resume_runtime_settings_uses_latest_valid_event(tmp_path: Path) -> None:
    log_path = tmp_path / "resume-settings.jsonl"
    events = [
        {"type": "session_start", "payload": {"stream": True}},
        {
            "type": "session_setting_changed",
            "payload": {"setting": "trace_level", "value": "full"},
        },
        {
            "type": "session_setting_changed",
            "payload": {"setting": "stream", "value": False},
        },
        {"type": "session_start", "payload": {"mode": "review"}},
        {
            "type": "session_setting_changed",
            "payload": {"setting": "trace_level", "value": "unsafe_raw"},
        },
    ]
    log_path.write_text("\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8")

    settings = cli_mod._load_chat_resume_runtime_settings(log_path)

    assert settings == {"stream": False, "trace_level": "full"}


def test_load_chat_resume_runtime_settings_defaults_legacy_trace_to_compact(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "resume-legacy-settings.jsonl"
    log_path.write_text(
        json.dumps({"type": "session_start", "payload": {"mode": "review"}}) + "\n",
        encoding="utf-8",
    )

    assert cli_mod._load_chat_resume_runtime_settings(log_path) == {
        "stream": None,
        "trace_level": "compact",
    }


def test_load_chat_resume_messages_uses_latest_compacted_snapshot(tmp_path: Path) -> None:
    log_path = tmp_path / "resume-compacted.jsonl"
    memory_message = {
        "role": "user",
        "content": '<<<ALYSIS_CONVERSATION_MEMORY_JSON>>>\n{"goal":"ship"}',
    }
    events = [
        {"type": "user_message", "payload": {"content": "old raw turn"}},
        {"type": "assistant_message", "payload": {"content": "old raw reply"}},
        {
            "type": "conversation_summary_updated",
            "payload": {
                "active_conversation_messages": [
                    memory_message,
                    {"role": "user", "content": "recent retained turn"},
                ]
            },
        },
        {"type": "assistant_message", "payload": {"content": "reply after compaction"}},
    ]
    log_path.write_text("\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8")

    loaded = cli_mod._load_chat_resume_messages(log_path)

    assert loaded == [
        memory_message,
        {"role": "user", "content": "recent retained turn"},
        {"role": "assistant", "content": "reply after compaction"},
    ]
    assert all("old raw" not in str(message.get("content") or "") for message in loaded)


_SYNTHETIC_TOOL_RESULT_CONTENT = "[tool result unavailable after session resume]"


def _assert_resume_tool_pairing(messages: list[dict[str, Any]]) -> None:
    call_ids = {
        str(call.get("id") or "").strip()
        for message in messages
        if str(message.get("role") or "") == "assistant"
        for call in (message.get("tool_calls") or [])
        if isinstance(call, dict)
    }
    result_ids = {
        str(message.get("tool_call_id") or "").strip()
        for message in messages
        if str(message.get("role") or "") == "tool"
    }
    assert call_ids == result_ids


def test_load_chat_resume_messages_keeps_tool_pairing_across_compacted_snapshot(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "resume-snapshot-open-tool-turn.jsonl"
    assistant_with_open_call = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "fs_read", "arguments": "{}"},
            }
        ],
    }
    events = [
        {
            "type": "conversation_summary_updated",
            "payload": {
                "active_conversation_messages": [
                    {"role": "user", "content": "Read the file"},
                    assistant_with_open_call,
                ]
            },
        },
        {
            "type": "tool_result",
            "payload": {
                "name": "fs_read",
                "tool_call_id": "call_1",
                "content": "real replayed result",
                "step": 1,
            },
        },
        {"type": "user_message", "payload": {"content": "Thanks"}},
    ]
    log_path.write_text("\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8")

    synthesized: list[str] = []
    loaded = cli_mod._load_chat_resume_messages(log_path, synthesized_tool_results=synthesized)

    assert loaded == [
        {"role": "user", "content": "Read the file"},
        assistant_with_open_call,
        {"role": "tool", "tool_call_id": "call_1", "content": "real replayed result"},
        {"role": "user", "content": "Thanks"},
    ]
    assert synthesized == []
    _assert_resume_tool_pairing(loaded)


def test_load_chat_resume_messages_synthesizes_result_for_snapshot_orphan(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "resume-snapshot-orphan.jsonl"
    assistant_with_open_call = {
        "role": "assistant",
        "content": "Let me check.",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "fs_read", "arguments": "{}"},
            }
        ],
    }
    events = [
        {
            "type": "conversation_summary_updated",
            "payload": {
                "active_conversation_messages": [
                    {"role": "user", "content": "Read the file"},
                    assistant_with_open_call,
                ]
            },
        },
    ]
    log_path.write_text("\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8")

    synthesized: list[str] = []
    loaded = cli_mod._load_chat_resume_messages(log_path, synthesized_tool_results=synthesized)

    assert loaded == [
        {"role": "user", "content": "Read the file"},
        assistant_with_open_call,
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": _SYNTHETIC_TOOL_RESULT_CONTENT,
        },
    ]
    assert synthesized == ["call_1"]
    _assert_resume_tool_pairing(loaded)


def test_load_chat_resume_messages_repairs_midstream_dropped_tool_result(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "resume-midstream-orphan.jsonl"
    events = [
        {"type": "user_message", "payload": {"content": "Run both tools"}},
        {
            "type": "assistant_message",
            "payload": {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "tc-a",
                            "type": "function",
                            "function": {"name": "fs_read", "arguments": "{}"},
                        },
                        {
                            "id": "tc-b",
                            "type": "function",
                            "function": {"name": "fs_list", "arguments": "{}"},
                        },
                    ],
                }
            },
        },
        {
            "type": "tool_result",
            "payload": {"name": "fs_read", "tool_call_id": "tc-a", "content": "alpha", "step": 1},
        },
        # Rendered content is empty, so replay drops this result.
        {
            "type": "tool_result",
            "payload": {"name": "fs_list", "tool_call_id": "tc-b", "content": "", "step": 2},
        },
        {"type": "final", "payload": {"content": "All done"}},
    ]
    log_path.write_text("\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8")

    synthesized: list[str] = []
    loaded = cli_mod._load_chat_resume_messages(log_path, synthesized_tool_results=synthesized)

    assert synthesized == ["tc-b"]
    assert loaded[2] == {"role": "tool", "tool_call_id": "tc-a", "content": "alpha"}
    assert loaded[3] == {
        "role": "tool",
        "tool_call_id": "tc-b",
        "content": _SYNTHETIC_TOOL_RESULT_CONTENT,
    }
    assert loaded[4] == {"role": "assistant", "content": "All done"}
    _assert_resume_tool_pairing(loaded)


def test_load_chat_resume_messages_leaves_fully_paired_transcript_unchanged(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "resume-fully-paired.jsonl"
    assistant_message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "tc-read",
                "type": "function",
                "function": {"name": "fs_read", "arguments": "{}"},
            }
        ],
    }
    events = [
        {"type": "session_start", "payload": {"mode": "auto"}},
        {"type": "user_message", "payload": {"content": "Read the file."}},
        {"type": "assistant_message", "payload": {"message": assistant_message}},
        {
            "type": "tool_result",
            "payload": {
                "name": "fs_read",
                "tool_call_id": "tc-read",
                "content": "file body",
                "step": 1,
            },
        },
        {"type": "final", "payload": {"content": "Read it."}},
    ]
    log_path.write_text("\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8")

    synthesized: list[str] = []
    loaded = cli_mod._load_chat_resume_messages(log_path, synthesized_tool_results=synthesized)

    assert loaded == [
        {"role": "user", "content": "Read the file."},
        assistant_message,
        {"role": "tool", "tool_call_id": "tc-read", "content": "file body"},
        {"role": "assistant", "content": "Read it."},
    ]
    assert synthesized == []
    assert all(
        _SYNTHETIC_TOOL_RESULT_CONTENT not in str(message.get("content") or "")
        for message in loaded
    )
    _assert_resume_tool_pairing(loaded)


def test_load_chat_resume_messages_uses_shaped_tool_result_content(tmp_path: Path) -> None:
    log_path = tmp_path / "resume-shaped-tool-result.jsonl"
    shaped_content = json.dumps(
        {
            "transcript_shaped": True,
            "tool": "fs_read",
            "tool_call_id": "tc-read",
            "summary": 'Loaded "large.txt" (5000 chars).',
            "preview": "A" * 80,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    events = [
        {"type": "session_start", "payload": {"mode": "auto"}},
        {"type": "user_message", "payload": {"content": "Read the large file."}},
        {
            "type": "assistant_message",
            "payload": {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "tc-read",
                            "type": "function",
                            "function": {
                                "name": "fs_read",
                                "arguments": json.dumps({"path": "large.txt"}),
                            },
                        }
                    ],
                }
            },
        },
        {
            "type": "tool_result",
            "payload": {
                "name": "fs_read",
                "tool_call_id": "tc-read",
                "result": {"path": "large.txt", "content": "A" * 5000},
                "content": shaped_content,
                "step": 1,
            },
        },
    ]
    log_path.write_text("\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8")

    loaded = cli_mod._load_chat_resume_messages(log_path)

    assert loaded[-1] == {
        "role": "tool",
        "tool_call_id": "tc-read",
        "content": shaped_content,
    }
    assert "A" * 1000 not in loaded[-1]["content"]


def test_load_chat_resume_messages_prefers_user_display_content(tmp_path: Path) -> None:
    log_path = tmp_path / "resume-display.jsonl"
    events = [
        {
            "type": "user_message",
            "payload": {
                # Older logs may carry a host-expanded instruction alongside the
                # text the user actually typed; the resume view prefers the latter.
                "content": "Fix the bug.\n\n<host_context>\nfocus: src/app.py\n</host_context>",
                "display_content": "Fix the bug.",
            },
        },
        {"type": "assistant_message", "payload": {"content": "Done."}},
    ]
    log_path.write_text("\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8")

    loaded = cli_mod._load_chat_resume_messages(log_path)
    assert loaded == [
        {"role": "user", "content": "Fix the bug."},
        {"role": "assistant", "content": "Done."},
    ]


def test_load_chat_resume_messages_accepts_final_or_route_reply_fallback(tmp_path: Path) -> None:
    log_path = tmp_path / "resume-fallback.jsonl"
    events = [
        {"type": "session_start", "payload": {"mode": "auto"}},
        {"type": "user_message", "payload": {"content": "Ping"}},
        {"type": "final", "payload": {"content": "Pong from final"}},
        {"type": "route_decision", "payload": {"route": "chat", "reply": "Natural reply"}},
    ]
    log_path.write_text("\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8")

    loaded = cli_mod._load_chat_resume_messages(log_path)
    assert loaded == [
        {"role": "user", "content": "Ping"},
        {"role": "assistant", "content": "Pong from final"},
        {"role": "assistant", "content": "Natural reply"},
    ]


def test_load_chat_resume_active_workdir_relpath_prefers_latest_change(tmp_path: Path) -> None:
    log_path = tmp_path / "resume-workdir.jsonl"
    events = [
        {
            "type": "session_start",
            "payload": {
                "mode": "auto",
                "active_workdir_relpath": ".",
            },
        },
        {
            "type": "session_workdir_changed",
            "payload": {
                "active_workdir_relpath": "packages/app",
            },
        },
        {
            "type": "session_workdir_changed",
            "payload": {
                "active_workdir_relpath": "packages/lib",
            },
        },
    ]
    log_path.write_text("\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8")

    assert cli_mod._load_chat_resume_active_workdir_relpath(log_path) == "packages/lib"


def test_first_user_message_preview_prefers_user_display_content(tmp_path: Path) -> None:
    log_path = tmp_path / "resume-preview.jsonl"
    events = [
        {
            "type": "user_message",
            "payload": {
                "content": (
                    "Implement the feature.\n\n<host_context>\nfocus: src/app.py\n</host_context>"
                ),
                "display_content": "Implement the feature.",
            },
        }
    ]
    log_path.write_text("\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8")

    assert cli_mod._first_user_message_preview(log_path) == "Implement the feature."


def test_resolve_chat_resume_target_supports_index_exact_and_unique_prefix(tmp_path: Path) -> None:
    sessions = [
        SessionInfo(session_id="20260101T100000Z_abcd1111", path=tmp_path / "a.jsonl", mtime=3.0),
        SessionInfo(session_id="20260102T100000Z_beef2222", path=tmp_path / "b.jsonl", mtime=2.0),
        SessionInfo(session_id="20260103T100000Z_cafe3333", path=tmp_path / "c.jsonl", mtime=1.0),
    ]

    assert (
        cli_mod._resolve_chat_resume_target(raw_value="2", sessions=sessions)
        == sessions[1].session_id
    )
    assert (
        cli_mod._resolve_chat_resume_target(
            raw_value="20260103T100000Z_cafe3333",
            sessions=sessions,
        )
        == sessions[2].session_id
    )
    assert (
        cli_mod._resolve_chat_resume_target(
            raw_value="20260102T100000Z_beef",
            sessions=sessions,
        )
        == sessions[1].session_id
    )
    assert cli_mod._resolve_chat_resume_target(raw_value="2026", sessions=sessions) is None


def test_collect_chat_resume_candidates_keeps_latest_50(tmp_path: Path, monkeypatch) -> None:
    sessions = [
        SessionInfo(session_id=f"s{i:03d}", path=tmp_path / f"s{i:03d}.jsonl", mtime=1000 - i)
        for i in range(70)
    ]

    monkeypatch.setattr(cli_mod, "list_sessions", lambda _sessions_dir: sessions)
    candidates = cli_mod._collect_chat_resume_candidates(
        sessions_dir=tmp_path,
        current_session_id="s000",
    )

    assert len(candidates) == 50
    assert candidates[0].session_id == "s001"
    assert candidates[-1].session_id == "s050"


def test_collect_chat_resume_candidates_scopes_to_current_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    ws_a = tmp_path / "workspace_a"
    ws_b = tmp_path / "workspace_b"
    ws_a.mkdir()
    ws_b.mkdir()
    sessions = [
        SessionInfo(
            session_id="a1", path=tmp_path / "a1.jsonl", mtime=3.0, workspace_root=str(ws_a)
        ),
        SessionInfo(
            session_id="b1", path=tmp_path / "b1.jsonl", mtime=2.0, workspace_root=str(ws_b)
        ),
        SessionInfo(
            session_id="a2", path=tmp_path / "a2.jsonl", mtime=1.0, workspace_root=str(ws_a)
        ),
    ]
    monkeypatch.setattr(cli_mod, "list_sessions", lambda _sessions_dir: sessions)

    # Scoped to workspace A: only A's sessions surface (B's are hidden).
    scoped = cli_mod._collect_chat_resume_candidates(
        sessions_dir=tmp_path,
        current_session_id="current",
        workspace_root=str(ws_a),
    )
    assert [s.session_id for s in scoped] == ["a1", "a2"]

    # No workspace passed => unscoped global listing (unchanged legacy behavior).
    unscoped = cli_mod._collect_chat_resume_candidates(
        sessions_dir=tmp_path,
        current_session_id="current",
    )
    assert [s.session_id for s in unscoped] == ["a1", "b1", "a2"]


def test_collect_chat_resume_candidates_legacy_gitroot_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    repo.mkdir()
    other.mkdir()
    sessions = [
        # Legacy logs predate workspace_root recording: only git_root is present.
        SessionInfo(session_id="match", path=tmp_path / "m.jsonl", mtime=3.0, git_root=str(repo)),
        SessionInfo(session_id="other", path=tmp_path / "o.jsonl", mtime=2.0, git_root=str(other)),
        # No deterministic identity at all -> hidden from the scoped picker.
        SessionInfo(session_id="stray", path=tmp_path / "s.jsonl", mtime=1.0),
    ]
    monkeypatch.setattr(cli_mod, "list_sessions", lambda _sessions_dir: sessions)

    scoped = cli_mod._collect_chat_resume_candidates(
        sessions_dir=tmp_path,
        current_session_id="current",
        workspace_root=str(repo),
        git_root=str(repo),
    )
    assert [s.session_id for s in scoped] == ["match"]


def test_collect_chat_resume_candidates_hides_foreign_owner_sessions(
    tmp_path: Path, monkeypatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    sessions = [
        SessionInfo(
            session_id="mine",
            path=tmp_path / "mine.jsonl",
            mtime=3.0,
            workspace_root=str(ws),
            owner="alice@laptop",
        ),
        # Same workspace, different account: another user's conversation must
        # never surface in this user's /resume, however the file arrived.
        SessionInfo(
            session_id="foreign",
            path=tmp_path / "foreign.jsonl",
            mtime=2.0,
            workspace_root=str(ws),
            owner="mallory@other-pc",
        ),
        # Legacy log predating owner stamping stays visible.
        SessionInfo(
            session_id="legacy",
            path=tmp_path / "legacy.jsonl",
            mtime=1.0,
            workspace_root=str(ws),
        ),
    ]
    monkeypatch.setattr(cli_mod, "list_sessions", lambda _sessions_dir: sessions)
    # Owner comparison is case-insensitive (Windows usernames / DNS hostnames).
    monkeypatch.setattr(cli_mod, "local_session_owner", lambda: "Alice@LAPTOP", raising=False)

    scoped = cli_mod._collect_chat_resume_candidates(
        sessions_dir=tmp_path,
        current_session_id="current",
        workspace_root=str(ws),
    )
    assert [s.session_id for s in scoped] == ["mine", "legacy"]

    # Owner scoping applies even without workspace scoping (no workspace_root).
    unscoped = cli_mod._collect_chat_resume_candidates(
        sessions_dir=tmp_path,
        current_session_id="current",
    )
    assert [s.session_id for s in unscoped] == ["mine", "legacy"]


def test_collect_chat_resume_candidates_hides_owned_sessions_without_local_identity(
    tmp_path: Path, monkeypatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    sessions = [
        SessionInfo(
            session_id="stamped",
            path=tmp_path / "stamped.jsonl",
            mtime=2.0,
            workspace_root=str(ws),
            owner="alice@laptop",
        ),
        SessionInfo(
            session_id="legacy",
            path=tmp_path / "legacy.jsonl",
            mtime=1.0,
            workspace_root=str(ws),
        ),
    ]
    monkeypatch.setattr(cli_mod, "list_sessions", lambda _sessions_dir: sessions)
    monkeypatch.setattr(cli_mod, "local_session_owner", lambda: None, raising=False)

    scoped = cli_mod._collect_chat_resume_candidates(
        sessions_dir=tmp_path,
        current_session_id="current",
        workspace_root=str(ws),
    )
    # No local identity established: never show an owner-stamped conversation.
    assert [s.session_id for s in scoped] == ["legacy"]


def test_resume_dt_prefers_log_ts_over_mtime(tmp_path: Path) -> None:
    now_local = datetime.now()
    # File mtime says "now" (e.g. after a zip-extract reset it), but the log's
    # own last-event ts says the user actually messaged ~2 hours ago.
    log_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    info = SessionInfo(
        session_id="s1",
        path=tmp_path / "s1.jsonl",
        mtime=now_local.timestamp(),
        last_event_ts=log_ts,
    )
    dt = cli_mod._resume_dt_from_info(info)
    assert dt is not None
    delta = now_local - dt
    assert timedelta(minutes=110) < delta < timedelta(minutes=130)


def test_resume_dt_falls_back_to_mtime_without_log_ts(tmp_path: Path) -> None:
    mtime = (datetime.now() - timedelta(days=3)).timestamp()
    info = SessionInfo(session_id="s1", path=tmp_path / "s1.jsonl", mtime=mtime)
    dt = cli_mod._resume_dt_from_info(info)
    assert dt is not None
    assert abs(dt.timestamp() - mtime) < 1.0


def test_resume_when_label_reflects_log_ts_not_mtime(tmp_path: Path) -> None:
    # mtime "now" would render "just now"; the log ts (~5h ago) must win.
    log = tmp_path / "s1.jsonl"
    log.write_text(
        json.dumps({"type": "user_message", "payload": {"content": "hello there"}}) + "\n",
        encoding="utf-8",
    )
    info = SessionInfo(
        session_id="s1",
        path=log,
        mtime=datetime.now().timestamp(),
        last_event_ts=(datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(),
    )
    [row] = cli_mod._chat_resume_display_rows(sessions=[info])
    assert row.when_label != "just now"
    assert "min ago" not in row.when_label


def test_chat_resume_panel_highlights_selected_session_case_insensitively(tmp_path: Path) -> None:
    first = tmp_path / "a.jsonl"
    second = tmp_path / "b.jsonl"
    first.write_text(
        json.dumps({"type": "user_message", "payload": {"content": "Fix startup latency imports"}})
        + "\n",
        encoding="utf-8",
    )
    second.write_text(
        json.dumps({"type": "user_message", "payload": {"content": "Improve resume picker UI"}})
        + "\n",
        encoding="utf-8",
    )
    sessions = [
        SessionInfo(session_id="20260101T100000Z_ABCD1111", path=first, mtime=2.0),
        SessionInfo(session_id="20260102T100000Z_BEEF2222", path=second, mtime=1.0),
    ]
    panel = cli_mod._chat_resume_panel(
        current_session_id="current",
        sessions=sessions,
        selected_session_id=sessions[0].session_id.lower(),
        interactive=True,
    )
    console = Console(width=140, record=True, color_system=None, force_terminal=False)
    console.print(panel)
    rendered = console.export_text()

    assert "Resume Session" in rendered
    assert "Current Session: * current" in rendered
    assert "> Fix startup latency imports" in rendered
    assert "Improve resume picker UI" in rendered


def test_chat_resume_panel_groups_by_date_and_shows_preview_and_footer(tmp_path: Path) -> None:
    now = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)

    today_path = tmp_path / "today.jsonl"
    yesterday_path = tmp_path / "yesterday.jsonl"
    older_path = tmp_path / "older.jsonl"

    today_path.write_text(
        json.dumps(
            {"type": "user_message", "payload": {"content": "Fix duplicate message bug in chat"}}
        )
        + "\n",
        encoding="utf-8",
    )
    yesterday_path.write_text(
        json.dumps({"type": "user_message", "payload": {"content": "Refactor resume panel layout"}})
        + "\n",
        encoding="utf-8",
    )
    older_path.write_text(
        json.dumps(
            {
                "type": "user_message",
                "payload": {
                    "content": "Investigate startup latency and import graph behavior for alysis",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    today_ts = now.timestamp()
    yesterday_ts = (now - timedelta(days=1)).timestamp()
    older_ts = (now - timedelta(days=3, hours=1)).timestamp()
    os.utime(today_path, (today_ts, today_ts))
    os.utime(yesterday_path, (yesterday_ts, yesterday_ts))
    os.utime(older_path, (older_ts, older_ts))

    sessions = [
        SessionInfo(session_id="today", path=today_path, mtime=today_ts),
        SessionInfo(session_id="yesterday", path=yesterday_path, mtime=yesterday_ts),
        SessionInfo(session_id="older", path=older_path, mtime=older_ts),
    ]

    panel = cli_mod._chat_resume_panel(
        current_session_id="current-session",
        sessions=sessions,
        selected_session_id="today",
        interactive=True,
    )

    console = Console(width=150, record=True, color_system=None, force_terminal=False)
    console.print(panel)
    rendered = console.export_text()

    assert "Today -" in rendered
    assert "Yesterday -" in rendered
    assert "Fix duplicate message bug in chat" in rendered
    assert "Refactor resume panel layout" in rendered
    assert "PgUp/PgDn Page" in rendered
    assert "Home/End Jump" in rendered


def test_clamp_resume_scroll_offset_keeps_selection_visible() -> None:
    assert (
        cli_mod._clamp_resume_scroll_offset(
            total_rows=20,
            selected_index=0,
            scroll_offset=7,
            visible_session_rows=5,
        )
        == 0
    )
    assert (
        cli_mod._clamp_resume_scroll_offset(
            total_rows=20,
            selected_index=9,
            scroll_offset=0,
            visible_session_rows=5,
        )
        == 5
    )
    assert (
        cli_mod._clamp_resume_scroll_offset(
            total_rows=20,
            selected_index=18,
            scroll_offset=0,
            visible_session_rows=5,
        )
        == 14
    )


def test_chat_resume_panel_scroll_indicators_and_sticky_group(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = datetime.now()
    sessions: list[SessionInfo] = []
    for idx in range(8):
        path = tmp_path / f"today-{idx}.jsonl"
        path.write_text(
            json.dumps({"type": "user_message", "payload": {"content": f"Today task {idx}"}})
            + "\n",
            encoding="utf-8",
        )
        ts = (now - timedelta(minutes=idx)).timestamp()
        sessions.append(SessionInfo(session_id=f"today-{idx}", path=path, mtime=ts))
    for idx in range(4):
        path = tmp_path / f"yesterday-{idx}.jsonl"
        path.write_text(
            json.dumps({"type": "user_message", "payload": {"content": f"Yesterday task {idx}"}})
            + "\n",
            encoding="utf-8",
        )
        ts = (now - timedelta(days=1, minutes=idx)).timestamp()
        sessions.append(SessionInfo(session_id=f"yesterday-{idx}", path=path, mtime=ts))

    monkeypatch.setattr(cli_mod, "_terminal_dimensions", lambda: (100, 20))

    panel = cli_mod._chat_resume_panel(
        current_session_id="current",
        sessions=sessions,
        selected_session_id="today-4",
        interactive=True,
        scroll_offset=2,
        visible_session_rows=4,
    )
    console = Console(width=120, record=True, color_system=None, force_terminal=False)
    console.print(panel)
    rendered = console.export_text()

    assert "... 2 more sessions above" in rendered
    assert "more sessions below" in rendered
    assert "Today -" in rendered
    assert "Today task 4" in rendered


def test_chat_resume_panel_shows_resize_warning_when_terminal_too_small(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "small.jsonl"
    path.write_text(
        json.dumps({"type": "user_message", "payload": {"content": "hello world"}}) + "\n",
        encoding="utf-8",
    )
    sessions = [SessionInfo(session_id="small", path=path, mtime=datetime.now().timestamp())]

    monkeypatch.setattr(cli_mod, "_terminal_dimensions", lambda: (30, 8))

    panel = cli_mod._chat_resume_panel(
        current_session_id="current",
        sessions=sessions,
        selected_session_id="small",
        interactive=True,
    )
    console = Console(width=60, record=True, color_system=None, force_terminal=False)
    console.print(panel)
    rendered = console.export_text()

    assert "Terminal too small - please resize" in rendered
    assert "Minimum: 40x10" in rendered


def test_resume_preview_prefers_custom_name_then_summary_then_first_message(tmp_path: Path) -> None:
    session_path = tmp_path / "session.jsonl"
    session_path.write_text(
        json.dumps(
            {"type": "user_message", "payload": {"content": "Fix duplicate chat render bug"}}
        )
        + "\n",
        encoding="utf-8",
    )
    info = SessionInfo(session_id="session", path=session_path, mtime=datetime.now().timestamp())

    metadata_path = cli_mod._session_metadata_path(session_path)
    metadata_path.write_text(
        json.dumps({"summary": "Auto generated summary", "custom_name": "My Custom Session"})
        + "\n",
        encoding="utf-8",
    )
    assert cli_mod._load_resume_preview_text(info) == "My Custom Session"

    metadata_path.write_text(
        json.dumps({"summary": "Auto generated summary"}) + "\n",
        encoding="utf-8",
    )
    assert cli_mod._load_resume_preview_text(info) == "Auto generated summary"

    metadata_path.unlink()
    assert cli_mod._load_resume_preview_text(info) == "Fix duplicate chat render bug"


def test_rename_resume_session_custom_title_persists_metadata(tmp_path: Path) -> None:
    session_path = tmp_path / "rename-target.jsonl"
    session_path.write_text("", encoding="utf-8")
    info = SessionInfo(
        session_id="rename-target", path=session_path, mtime=datetime.now().timestamp()
    )

    renamed, message = cli_mod._rename_resume_session_custom_title(
        info=info,
        new_title="Refactor Resume Session Screen",
    )

    assert renamed is True
    assert "renamed" in message.lower()
    metadata = json.loads(cli_mod._session_metadata_path(session_path).read_text(encoding="utf-8"))
    assert metadata["custom_name"] == "Refactor Resume Session Screen"


def test_ensure_session_summary_metadata_generates_claude_title_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_path = tmp_path / "summary-target.jsonl"
    events = [
        {"type": "session_start", "payload": {"mode": "review"}},
        {"type": "user_message", "payload": {"content": "Fix duplicate message rendering"}},
        {"type": "assistant_message", "payload": {"content": "Investigating prompt echo path"}},
        {"type": "user_message", "payload": {"content": "Add inline resume rename flow"}},
        {"type": "assistant_message", "payload": {"content": "Implemented picker key handling"}},
    ]
    session_path.write_text("\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8")

    generated: dict[str, int] = {"calls": 0}

    def _fake_summary(*, session: Any, transcript_messages: list[dict[str, str]]) -> str | None:
        _ = session
        assert len(transcript_messages) >= 4
        generated["calls"] += 1
        return "Fix resume message rendering"

    monkeypatch.setattr(cli_mod, "_generate_session_summary_with_model", _fake_summary)

    fake_session = SimpleNamespace(
        cfg=AppConfig(model="test-model", base_url="https://example.com/v1"),
        client=SimpleNamespace(api_key="k", model="test-model"),
        store=SimpleNamespace(
            enabled=True,
            path=session_path,
            sessions_dir=tmp_path,
            session_id="summary-target",
        ),
    )

    cli_mod._ensure_session_summary_metadata(session=fake_session)
    cli_mod._ensure_session_summary_metadata(session=fake_session)

    metadata = json.loads(cli_mod._session_metadata_path(session_path).read_text(encoding="utf-8"))
    assert metadata["summary"] == "Fix resume message rendering"
    assert metadata["summary_source"] == "generated_model"
    assert generated["calls"] == 1


def test_ensure_session_summary_metadata_can_skip_model_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_path = tmp_path / "summary-skip-model.jsonl"
    events = [
        {"type": "session_start", "payload": {"mode": "review"}},
        {"type": "user_message", "payload": {"content": "Fix duplicate message rendering"}},
        {"type": "assistant_message", "payload": {"content": "Investigating prompt echo path"}},
        {"type": "user_message", "payload": {"content": "Add inline resume rename flow"}},
        {"type": "assistant_message", "payload": {"content": "Implemented picker key handling"}},
    ]
    session_path.write_text("\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8")

    generated: dict[str, int] = {"calls": 0}

    def _fake_summary(*, session: Any, transcript_messages: list[dict[str, str]]) -> str | None:
        _ = session, transcript_messages
        generated["calls"] += 1
        return "Should not be used"

    monkeypatch.setattr(cli_mod, "_generate_session_summary_with_model", _fake_summary)

    fake_session = SimpleNamespace(
        cfg=AppConfig(model="test-model", base_url="https://example.com/v1"),
        client=SimpleNamespace(api_key="k", model="test-model"),
        store=SimpleNamespace(
            enabled=True,
            path=session_path,
            sessions_dir=tmp_path,
            session_id="summary-skip-model",
        ),
    )

    cli_mod._ensure_session_summary_metadata(session=fake_session, allow_model_summary=False)

    metadata = json.loads(cli_mod._session_metadata_path(session_path).read_text(encoding="utf-8"))
    assert metadata["summary"] == "Fix duplicate message rendering"
    assert metadata["summary_source"] == "first_user_message"
    assert metadata.get("summary_generated_at") in (None, "")
    assert generated["calls"] == 0


def test_render_chat_resume_history_uses_styled_panels(tmp_path: Path) -> None:
    _ = tmp_path
    console = Console(width=120, record=True, color_system=None, force_terminal=False)
    fake_session = SimpleNamespace(console=console)

    cli_mod._render_chat_resume_history(
        session=fake_session,
        messages=[
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ],
    )

    rendered = console.export_text()
    assert "Loaded 2 historical messages." in rendered
    assert "You" in rendered
    assert "Agent" in rendered
    assert "Hello" in rendered
    assert "Hi there" in rendered


def test_build_chat_resume_context_message_summarizes_tools_and_redacts_secrets(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "resume-context.jsonl"
    events = [
        {
            "type": "session_start",
            "payload": {
                "mode": "auto",
                "model": "test-model",
                "workspace_root": "/workspace/project",
                "focus_relpath": ".",
                "active_workdir_relpath": ".",
            },
        },
        {"type": "user_message", "payload": {"content": "Fix src/app.py"}},
        {
            "type": "tool_call",
            "payload": {"name": "fs_read", "arguments": {"path": "src/app.py"}, "step": 1},
        },
        {
            "type": "tool_result",
            "payload": {
                "name": "fs_read",
                "result": {"path": "src/app.py", "content": "print('hello')"},
                "step": 1,
            },
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "shell_run",
                "arguments": {"cmd": "OPENAI_API_KEY=sk-testsecret123456789 pytest -q"},
                "step": 2,
            },
        },
        {
            "type": "tool_result",
            "payload": {
                "name": "shell_run",
                "result": {
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "Authorization: Bearer abcdefghijk12345",
                },
                "step": 2,
            },
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "custom_tool",
                "arguments": {
                    "password": "plain-json-secret",
                    "nested": {"openai_api_key": "sk-jsonsecret1234567890"},
                },
                "step": 3,
            },
        },
        {
            "type": "tool_result",
            "payload": {
                "name": "fake_optional_tool",
                "result": {
                    "status": "tool_unavailable",
                    "tool": "fake_optional_tool",
                    "reason": "module not importable: fake_optional_dependency",
                },
                "step": 4,
            },
        },
        {
            "type": "verify_run",
            "payload": {
                "commands": ["pytest -q"],
                "all_passed": False,
                "summary": "1 failed, 3 passed",
            },
        },
        {"type": "assistant_message", "payload": {"content": "I found one failing test."}},
    ]
    log_path.write_text("\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8")

    context = cli_mod._build_chat_resume_context_message(log_path)

    assert context is not None
    assert context.startswith("<resume_context>")
    assert "source_session_id: resume-context" in context
    assert "Fix src/app.py" in context
    assert "tool_call fs_read" in context
    assert "tool_result shell_run status=failed" in context
    assert "tool_result fake_optional_tool status=tool_unavailable" in context
    assert "src/app.py" in context
    assert "pytest -q" in context
    assert "sk-testsecret" not in context
    assert "plain-json-secret" not in context
    assert "sk-jsonsecret" not in context
    assert "abcdefghijk12345" not in context
    assert "[REDACTED]" in context


def test_build_chat_resume_context_message_honors_conversation_clear_boundary(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "resume-context-after-clear.jsonl"
    events = [
        {
            "type": "session_start",
            "payload": {
                "mode": "auto",
                "model": "test-model",
                "workspace_root": "/workspace/project",
            },
        },
        {"type": "user_message", "payload": {"content": "OLD_REQUEST_SENTINEL"}},
        {
            "type": "tool_call",
            "payload": {
                "name": "shell_run",
                "arguments": {
                    "cmd": "old-command-sentinel",
                    "path": "old/path/sentinel.py",
                },
                "step": 1,
            },
        },
        {
            "type": "tool_result",
            "payload": {
                "name": "shell_run",
                "result": {"exit_code": 1, "stderr": "OLD_FAILURE_SENTINEL"},
                "step": 1,
            },
        },
        {
            "type": "verify_run",
            "payload": {"commands": ["old-verify-sentinel"], "all_passed": False},
        },
        {"type": "warning", "payload": {"warning": "OLD_WARNING_SENTINEL"}},
        {"type": "malformed_before_clear", "payload": "not-an-object"},
        {"type": "conversation_cleared", "payload": {"trigger": "user_command"}},
        {"type": "user_message", "payload": {"content": "NEW_REQUEST_SENTINEL"}},
        {
            "type": "tool_call",
            "payload": {
                "name": "fs_read",
                "arguments": {"path": "new/path/sentinel.py"},
                "step": 1,
            },
        },
        {"type": "assistant_message", "payload": {"content": "NEW_ANSWER_SENTINEL"}},
    ]
    log_path.write_text("\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8")

    context = cli_mod._build_chat_resume_context_message(log_path)

    assert context is not None
    assert "NEW_REQUEST_SENTINEL" in context
    assert "NEW_ANSWER_SENTINEL" in context
    assert "new/path/sentinel.py" in context
    assert "- conversation_cleared: 1" in context
    assert "- user_message: 1" in context
    assert "- tool_call: 1" in context
    assert "OLD_REQUEST_SENTINEL" not in context
    assert "old-command-sentinel" not in context
    assert "old/path/sentinel.py" not in context
    assert "OLD_FAILURE_SENTINEL" not in context
    assert "old-verify-sentinel" not in context
    assert "OLD_WARNING_SENTINEL" not in context
    assert "skipped_malformed_events" not in context


def test_build_chat_resume_context_message_keeps_recent_tool_activity_bounded(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "resume-large.jsonl"
    events = [
        {
            "type": "session_start",
            "payload": {
                "mode": "auto",
                "model": "test-model",
                "workspace_root": "/workspace/project",
            },
        }
    ]
    for step in range(cli_mod._CHAT_RESUME_CONTEXT_MAX_TOOL_EVENTS + 5):
        events.append(
            {
                "type": "tool_call",
                "payload": {
                    "name": "fs_read",
                    "arguments": {"path": f"src/file_{step}.py"},
                    "step": step,
                },
            }
        )
    log_path.write_text("\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8")

    context = cli_mod._build_chat_resume_context_message(log_path)

    assert context is not None
    assert len(context) <= cli_mod._CHAT_RESUME_CONTEXT_MAX_CHARS
    assert "- ... 5 older tool event(s) omitted" in context
    assert "step=0 tool_call fs_read" not in context
    latest_step = cli_mod._CHAT_RESUME_CONTEXT_MAX_TOOL_EVENTS + 4
    assert f"step={latest_step} tool_call fs_read" in context


def test_build_chat_resume_context_message_bounds_path_candidate_scanning(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "resume-huge-paths.jsonl"
    huge_paths = [
        f"generated/path_{idx}.py"
        for idx in range(cli_mod._CHAT_RESUME_CONTEXT_MAX_PATH_CANDIDATE_SCAN + 100)
    ]
    events = [
        {
            "type": "session_start",
            "payload": {"mode": "auto", "model": "test-model", "workspace_root": "/workspace"},
        },
        {
            "type": "tool_call",
            "payload": {
                "name": "custom_tool",
                "arguments": {"paths": huge_paths},
                "step": 1,
            },
        },
    ]
    log_path.write_text("\n".join(json.dumps(ev) for ev in events) + "\n", encoding="utf-8")

    context = cli_mod._build_chat_resume_context_message(log_path)

    assert context is not None
    assert len(context) <= cli_mod._CHAT_RESUME_CONTEXT_MAX_CHARS
    assert "generated/path_0.py" in context
    assert (
        f"generated/path_{cli_mod._CHAT_RESUME_CONTEXT_MAX_PATH_CANDIDATE_SCAN + 99}.py"
        not in context
    )


def test_resolve_chat_resume_direct_session_id_rejects_path_traversal(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (tmp_path / "outside.jsonl").write_text(
        json.dumps({"type": "user_message", "payload": {"content": "outside"}}) + "\n",
        encoding="utf-8",
    )

    assert (
        cli_mod._resolve_chat_resume_direct_session_id(
            raw_value="../outside",
            sessions_dir=sessions_dir,
        )
        is None
    )
    assert (
        cli_mod._resolve_chat_resume_direct_session_id(
            raw_value="..\\outside",
            sessions_dir=sessions_dir,
        )
        is None
    )


class _FakeStore:
    def __init__(self, *, sessions_dir: Path, session_id: str, enabled: bool = True) -> None:
        self.sessions_dir = sessions_dir
        self.session_id = session_id
        self.enabled = enabled
        self.notes: list[tuple[str, dict[str, object]]] = []

    def append(self, event_type: str, payload: dict[str, object]) -> None:
        self.notes.append((event_type, payload))


class _FakeSession:
    pass


def test_resume_chat_session_inserts_pinned_resume_context_before_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    target_id = "resume-with-context"
    target_path = sessions_dir / f"{target_id}.jsonl"
    target_events = [
        {
            "type": "session_start",
            "payload": {
                "mode": "review",
                "model": "resumed-model",
                "active_workdir_relpath": ".",
            },
        },
        {"type": "user_message", "payload": {"content": "Fix src/parser.py"}},
        {
            "type": "tool_call",
            "payload": {"name": "fs_read", "arguments": {"path": "src/parser.py"}, "step": 1},
        },
        {
            "type": "tool_result",
            "payload": {
                "name": "fs_read",
                "result": {"path": "src/parser.py", "content": "def parse(): pass"},
                "step": 1,
            },
        },
        {"type": "assistant_message", "payload": {"content": "I inspected the parser."}},
    ]
    target_path.write_text(
        "\n".join(json.dumps(ev) for ev in target_events) + "\n",
        encoding="utf-8",
    )

    current = _FakeSession()
    current.cfg = AppConfig(model="test-model", max_steps=10)
    current.root = tmp_path
    current.mode = "review"
    current.yes = True
    current.max_steps = 5
    current.console = None
    current.surface = object()
    current.store = _FakeStore(sessions_dir=sessions_dir, session_id="current-session")
    current.client = SimpleNamespace(api_key="override-key")
    current.usage_role = "main"
    current.tool_output_offloader = None
    current.conversation_compactor = None
    current.messages = []
    current.close = lambda: None

    def fake_create_session(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["active_workdir_relpath_override"] == "."
        new_session = _FakeSession()
        new_session.cfg = kwargs["cfg"]
        new_session.root = kwargs["root"]
        new_session.mode = kwargs["mode"]
        new_session.yes = kwargs["yes"]
        new_session.max_steps = kwargs["max_steps"]
        new_session.console = kwargs["console"]
        new_session.surface = kwargs["surface"]
        new_session.store = _FakeStore(
            sessions_dir=sessions_dir,
            session_id=kwargs["session_id_override"],
            enabled=not kwargs["no_log"],
        )
        new_session.client = SimpleNamespace(api_key="override-key")
        new_session.usage_role = kwargs["usage_role"]
        new_session.tool_output_offloader = None
        new_session.conversation_compactor = None
        new_session.messages = [{"role": "system", "content": "startup"}]
        new_session.pinned_prefix_len = 1
        return new_session

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)

    ok, message, loaded_history = cli_mod._resume_chat_session(
        session=current,
        target_session_id=target_id,
    )

    assert ok is True
    assert "Resumed session:" in message
    assert loaded_history == [
        {"role": "user", "content": "Fix src/parser.py"},
        {"role": "assistant", "content": "I inspected the parser."},
    ]
    assert current.pinned_prefix_len == 2
    assert current.messages[0] == {"role": "system", "content": "startup"}
    assert str(current.messages[1]["content"]).startswith("<resume_context>")
    assert "tool_result fs_read" in str(current.messages[1]["content"])
    assert current.messages[2:] == loaded_history
    system_note_payloads = [
        payload
        for event_type, payload in current.store.notes
        if event_type == "system_note" and payload.get("message") == "chat_resume"
    ]
    assert system_note_payloads
    assert system_note_payloads[-1]["resume_context_loaded"] is True
    assert system_note_payloads[-1]["resume_context_chars"] > 0


def _make_resume_current_session(*, sessions_dir: Path, root: Path) -> _FakeSession:
    current = _FakeSession()
    current.cfg = AppConfig(model="test-model", max_steps=10)
    current.root = root
    current.mode = "review"
    current.yes = True
    current.max_steps = 5
    current.console = None
    current.surface = object()
    current.store = _FakeStore(sessions_dir=sessions_dir, session_id="current-session")
    current.client = SimpleNamespace(api_key="override-key")
    current.usage_role = "main"
    current.tool_output_offloader = None
    current.conversation_compactor = None
    current.messages = []
    current.close = lambda: None
    return current


def _make_resume_create_session_factory(
    *,
    sessions_dir: Path,
    conversation_compactor: Any = None,
):
    def fake_create_session(**kwargs):  # type: ignore[no-untyped-def]
        new_session = _FakeSession()
        new_session.cfg = kwargs["cfg"]
        new_session.root = kwargs["root"]
        new_session.mode = kwargs["mode"]
        new_session.yes = kwargs["yes"]
        new_session.max_steps = kwargs["max_steps"]
        new_session.console = kwargs["console"]
        new_session.surface = kwargs["surface"]
        new_session.store = _FakeStore(
            sessions_dir=sessions_dir,
            session_id=kwargs["session_id_override"],
            enabled=not kwargs["no_log"],
        )
        new_session.client = SimpleNamespace(api_key="override-key")
        new_session.usage_role = kwargs["usage_role"]
        new_session.tool_output_offloader = None
        new_session.conversation_compactor = conversation_compactor
        new_session.messages = [{"role": "system", "content": "startup"}]
        new_session.pinned_prefix_len = 1
        return new_session

    return fake_create_session


def _chat_resume_note_payloads(store: _FakeStore) -> list[dict[str, object]]:
    return [
        payload
        for event_type, payload in store.notes
        if event_type == "system_note" and payload.get("message") == "chat_resume"
    ]


def test_resume_chat_session_reports_synthesized_tool_results(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    target_id = "resume-orphaned-tool-call"
    target_events = [
        {
            "type": "session_start",
            "payload": {"mode": "review", "active_workdir_relpath": "."},
        },
        {"type": "user_message", "payload": {"content": "Run the tool"}},
        {
            "type": "assistant_message",
            "payload": {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-orphan",
                            "type": "function",
                            "function": {"name": "fs_read", "arguments": "{}"},
                        }
                    ],
                }
            },
        },
    ]
    (sessions_dir / f"{target_id}.jsonl").write_text(
        "\n".join(json.dumps(ev) for ev in target_events) + "\n",
        encoding="utf-8",
    )

    current = _make_resume_current_session(sessions_dir=sessions_dir, root=tmp_path)
    monkeypatch.setattr(
        cli_mod,
        "create_session",
        _make_resume_create_session_factory(sessions_dir=sessions_dir),
    )

    ok, message, loaded_history = cli_mod._resume_chat_session(
        session=current,
        target_session_id=target_id,
    )

    assert ok is True
    assert "Resumed session:" in message
    assert loaded_history[-1] == {
        "role": "tool",
        "tool_call_id": "call-orphan",
        "content": _SYNTHETIC_TOOL_RESULT_CONTENT,
    }
    _assert_resume_tool_pairing(loaded_history)
    note_payloads = _chat_resume_note_payloads(current.store)
    assert note_payloads
    assert note_payloads[-1]["synthesized_tool_results"] == 1
    assert note_payloads[-1]["synthesized_tool_result_ids"] == ["call-orphan"]
    assert note_payloads[-1]["compaction_memory_reinjected"] is False


def test_resume_chat_session_reinjects_restored_compaction_memory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    target_id = "resume-with-compaction-memory"
    target_events = [
        {
            "type": "session_start",
            "payload": {"mode": "review", "active_workdir_relpath": "."},
        },
        {"type": "user_message", "payload": {"content": "Continue the task"}},
        {"type": "assistant_message", "payload": {"content": "Continuing."}},
    ]
    (sessions_dir / f"{target_id}.jsonl").write_text(
        "\n".join(json.dumps(ev) for ev in target_events) + "\n",
        encoding="utf-8",
    )

    class _FakeResumeCompactor:
        def __init__(self) -> None:
            self.state = SimpleNamespace(
                memory_message_index=None,
                pins_message_index=None,
                pinned_prefix_len=0,
            )
            self.reinject_inputs: list[list[dict[str, Any]]] = []

        def reinject_context_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
            self.reinject_inputs.append(list(messages))
            self.state.memory_message_index = len(messages)
            return [*messages, {"role": "user", "content": "<<<MEMORY-REINJECTED>>>"}]

    compactor = _FakeResumeCompactor()
    current = _make_resume_current_session(sessions_dir=sessions_dir, root=tmp_path)
    monkeypatch.setattr(
        cli_mod,
        "create_session",
        _make_resume_create_session_factory(
            sessions_dir=sessions_dir,
            conversation_compactor=compactor,
        ),
    )

    ok, message, loaded_history = cli_mod._resume_chat_session(
        session=current,
        target_session_id=target_id,
    )

    assert ok is True
    assert loaded_history
    # Reinjection ran after the replayed history was already extended in.
    assert compactor.reinject_inputs
    assert any(
        str(msg.get("content") or "") == "Continue the task" for msg in compactor.reinject_inputs[0]
    )
    assert current.messages[-1] == {"role": "user", "content": "<<<MEMORY-REINJECTED>>>"}
    note_payloads = _chat_resume_note_payloads(current.store)
    assert note_payloads
    assert note_payloads[-1]["compaction_memory_reinjected"] is True


def test_resume_chat_session_reports_active_workdir_restore_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    target_id = "resume-bad-workdir"
    target_path = sessions_dir / f"{target_id}.jsonl"
    target_events = [
        {
            "type": "session_start",
            "payload": {
                "mode": "review",
                "model": "resumed-model",
                "active_workdir_relpath": "missing-package",
            },
        },
        {"type": "user_message", "payload": {"content": "Continue the task"}},
    ]
    target_path.write_text(
        "\n".join(json.dumps(ev) for ev in target_events) + "\n",
        encoding="utf-8",
    )

    current = _FakeSession()
    current.cfg = AppConfig(model="test-model", max_steps=10)
    current.root = tmp_path
    current.mode = "review"
    current.yes = True
    current.max_steps = 5
    current.console = None
    current.surface = object()
    current.store = _FakeStore(sessions_dir=sessions_dir, session_id="current-session")
    current.client = SimpleNamespace(api_key="override-key")
    current.usage_role = "main"
    current.tool_output_offloader = None
    current.conversation_compactor = None
    current.messages = []
    current.close = lambda: None
    create_called = {"value": False}

    def fake_create_session(**kwargs):  # type: ignore[no-untyped-def]
        create_called["value"] = True
        raise AssertionError(
            "create_session should not run after active workdir prevalidation fails"
        )

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)

    ok, message, loaded_history = cli_mod._resume_chat_session(
        session=current,
        target_session_id=target_id,
    )

    assert ok is False
    assert (
        "could not restore active workdir 'missing-package': Directory does not exist:" in message
    )
    assert loaded_history == []
    assert create_called["value"] is False
    assert current.store.session_id == "current-session"
    assert current.messages == []


def test_resume_chat_session_rejects_unsafe_session_id_before_reading(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    outside_path = tmp_path / "outside.jsonl"
    outside_path.write_text(
        json.dumps({"type": "user_message", "payload": {"content": "outside secret"}}) + "\n",
        encoding="utf-8",
    )
    current = _FakeSession()
    current.cfg = AppConfig(model="test-model", max_steps=10)
    current.root = tmp_path
    current.store = _FakeStore(sessions_dir=sessions_dir, session_id="current-session")
    current.messages = []
    create_called = {"value": False}

    def fake_create_session(**kwargs):  # type: ignore[no-untyped-def]
        create_called["value"] = True
        raise AssertionError("unsafe resume target should not create a session")

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)

    ok, message, loaded_history = cli_mod._resume_chat_session(
        session=current,
        target_session_id="../outside",
    )

    assert ok is False
    assert "Invalid session id" in message
    assert loaded_history == []
    assert create_called["value"] is False
    assert current.messages == []


def test_resume_chat_session_replaces_current_session_state(tmp_path: Path, monkeypatch) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    target_id = "resume-target"
    target_path = sessions_dir / f"{target_id}.jsonl"
    resumed_route_identity = build_provider_route_identity(
        protocol="openai_compat",
        base_url="https://api.openai.com/v1",
        provider_key="openai",
        model="resumed-model",
        credential_scope="credential-scope:profile-default",
        routing_headers={
            "OpenAI-Organization": "org-resume",
            "OpenAI-Project": "project-resume",
        },
        protocol_revision="2026-07-12",
        session_scope="subscription-account:resume",
    )
    resumed_provider_metadata = stamp_provider_metadata_for_route(
        {"provider_state": {"opaque": "same-route-state"}},
        resumed_route_identity,
    )
    target_events = [
        {
            "type": "session_start",
            "payload": {
                "mode": "readonly",
                "model": "resumed-model",
                "profile_name": "default",
                "protocol": "openai_compat",
                "provider_base_url_descriptor": endpoint_descriptor("https://api.openai.com/v1"),
                "auth_provider": None,
                "reasoning_trace_adapter": "auto",
                "temperature": 0.2,
                "stream": True,
                "routing_mode": "code_only",
                "yes": False,
                "max_steps": 11,
                "step_budget_policy": "fixed",
                "task_max_steps": 41,
                "subagent_max_steps": 9,
                "enable_chat_turn_step_budget": False,
                "chat_turn_fixed_override": 7,
                "compaction_enabled": True,
                "tool_output_offload_enabled": True,
                "conversation_summarization_enabled": False,
            },
        },
        {"type": "user_message", "payload": {"content": "previous question"}},
        {
            "type": "assistant_message",
            "payload": {
                "message": {
                    "role": "assistant",
                    "content": "previous answer",
                    PROVIDER_METADATA_KEY: resumed_provider_metadata,
                }
            },
        },
    ]
    target_path.write_text(
        "\n".join(json.dumps(ev) for ev in target_events) + "\n",
        encoding="utf-8",
    )

    old_closed = {"value": False}
    applied_operations: list[object] = []

    def apply_staged_operation(operation: object) -> None:
        applied_operations.append(operation)

    def pending_operation_labels() -> list[str]:
        return ["permissions: fast"]

    scheduler_replacements: list[str] = []

    def on_child_scheduler_replaced(reason: str) -> None:
        scheduler_replacements.append(reason)

    lifecycle_events: list[dict[str, Any]] = []

    def lifecycle_listener(payload: dict[str, Any]) -> None:
        lifecycle_events.append(payload)

    class _Scheduler:
        def __init__(self, listener=None) -> None:  # type: ignore[no-untyped-def]
            self._listener = listener

        @property
        def lifecycle_listener(self):  # type: ignore[no-untyped-def]
            return self._listener

        def set_lifecycle_listener(self, listener) -> None:  # type: ignore[no-untyped-def]
            self._listener = listener

    current = _FakeSession()
    current.cfg = AppConfig(
        model="test-model",
        max_steps=37,
        step_budget_policy="adaptive",
        task_max_steps=100,
        subagent_max_steps=16,
    )
    current.root = tmp_path
    current.mode = "auto"
    current.yes = True
    current.max_steps = 5
    current.console = None
    current.surface = object()
    current.store = _FakeStore(sessions_dir=sessions_dir, session_id="current-session")
    current.client = SimpleNamespace(api_key="override-key")
    current.usage_role = "main"
    current.tool_output_offloader = None
    current.conversation_compactor = None
    current.messages = [{"role": "system", "content": "seed-old"}]
    current._alysis_tui_interactive = True
    current.apply_staged_operation = apply_staged_operation
    current.pending_operation_labels = pending_operation_labels
    current.on_child_scheduler_replaced = on_child_scheduler_replaced
    current.child_scheduler = _Scheduler(lifecycle_listener)

    def _close_old() -> None:
        old_closed["value"] = True

    current.close = _close_old

    def fake_create_session(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["session_id_override"] == target_id
        assert kwargs["mode"] == "readonly"
        assert kwargs["runtime_kind"] == RuntimeKind.INTERACTIVE_CHAT
        assert kwargs["session_source"] == "resume"
        assert kwargs["session_source_metadata"] == {
            "from_session_id": "current-session",
            "resumed_session_id": target_id,
            "loaded_message_count": 2,
            "active_workdir_relpath": None,
        }
        assert kwargs["yes"] is False
        assert kwargs["max_steps"] == 11
        assert kwargs["enable_chat_turn_step_budget"] is False
        assert kwargs["chat_turn_fixed_override"] == 7
        cfg = kwargs["cfg"]
        assert cfg.model == "resumed-model"
        assert cfg.max_steps == 11
        assert cfg.step_budget_policy == "limited"
        assert cfg.task_max_steps == 41
        assert cfg.subagent_max_steps == 9
        assert cfg.temperature == 0.2
        assert cfg.stream is True
        assert cfg.routing_mode == "code_only"
        assert kwargs["enable_compaction"] is True
        assert kwargs["enable_tool_output_offload"] is True
        assert kwargs["enable_conversation_summarization"] is False
        new_session = _FakeSession()
        new_session.cfg = kwargs["cfg"]
        new_session.root = kwargs["root"]
        new_session.mode = kwargs["mode"]
        new_session.yes = kwargs["yes"]
        new_session.max_steps = kwargs["max_steps"]
        new_session.console = kwargs["console"]
        new_session.surface = kwargs["surface"]
        new_session.store = _FakeStore(
            sessions_dir=sessions_dir,
            session_id=kwargs["session_id_override"],
            enabled=not kwargs["no_log"],
        )
        new_session.client = SimpleNamespace(
            api_key="override-key",
            route_identity=resumed_route_identity,
        )
        new_session.usage_role = kwargs["usage_role"]
        new_session.tool_output_offloader = object()
        new_session.conversation_compactor = None
        new_session.messages = [{"role": "system", "content": "seed-new"}]
        new_session.child_scheduler = _Scheduler()
        return new_session

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)

    ok, message, loaded_history = cli_mod._resume_chat_session(
        session=current,
        target_session_id=target_id,
    )

    assert ok is True
    assert "Resumed session:" in message
    assert old_closed["value"] is True
    assert current.store.session_id == target_id
    assert current._alysis_tui_interactive is True
    assert current.apply_staged_operation is apply_staged_operation
    assert current.pending_operation_labels is pending_operation_labels
    assert current.on_child_scheduler_replaced is on_child_scheduler_replaced
    assert current.child_scheduler.lifecycle_listener is lifecycle_listener
    staged = object()
    current.apply_staged_operation(staged)
    assert applied_operations == [staged]
    assert current.pending_operation_labels() == ["permissions: fast"]
    assert scheduler_replacements == []
    assert lifecycle_events == []
    assert loaded_history == [
        {"role": "user", "content": "previous question"},
        {
            "role": "assistant",
            "content": "previous answer",
            PROVIDER_METADATA_KEY: resumed_provider_metadata,
        },
    ]
    assert current.messages[-2:] == loaded_history
    assert any(
        event_type == "system_note" and payload.get("message") == "chat_resume"
        for event_type, payload in current.store.notes
    )


def test_resume_chat_session_strips_provider_metadata_when_connection_differs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    target_id = "resume-other-provider-route"
    target_path = sessions_dir / f"{target_id}.jsonl"
    active_route_identity = build_provider_route_identity(
        protocol="openai_compat",
        base_url="https://api.openai.com/v1",
        provider_key="openai",
        model="current-model",
    )
    matching_message_metadata = stamp_provider_metadata_for_route(
        {"provider_state": {"opaque": "must-strip-on-session-route-mismatch"}},
        active_route_identity,
    )
    target_events = [
        {
            "type": "session_start",
            "payload": {
                "mode": "review",
                "model": "historical-model",
                "profile_name": "default",
                "protocol": "openai_compat",
                "provider_base_url_descriptor": endpoint_descriptor(
                    "https://other-provider.example/v1"
                ),
                "auth_provider": None,
                "reasoning_trace_adapter": "auto",
            },
        },
        {"type": "user_message", "payload": {"content": "continue safely"}},
        {
            "type": "assistant_message",
            "payload": {
                "message": {
                    "role": "assistant",
                    "content": "visible historical answer",
                    PROVIDER_METADATA_KEY: matching_message_metadata,
                }
            },
        },
    ]
    target_path.write_text(
        "\n".join(json.dumps(event) for event in target_events) + "\n",
        encoding="utf-8",
    )

    current = _FakeSession()
    current.cfg = AppConfig(model="current-model", max_steps=10)
    current.root = tmp_path
    current.mode = "review"
    current.yes = False
    current.max_steps = 10
    current.console = None
    current.surface = object()
    current.store = _FakeStore(sessions_dir=sessions_dir, session_id="current-session")
    current.client = SimpleNamespace(api_key="override-key")
    current.usage_role = "main"
    current.tool_output_offloader = None
    current.conversation_compactor = None
    current.messages = []
    current.close = lambda: None

    def fake_create_session(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["cfg"].model == "current-model"
        assert kwargs["session_source_metadata"]["loaded_message_count"] == 2
        new_session = _FakeSession()
        new_session.cfg = kwargs["cfg"]
        new_session.root = kwargs["root"]
        new_session.mode = kwargs["mode"]
        new_session.yes = kwargs["yes"]
        new_session.max_steps = kwargs["max_steps"]
        new_session.console = kwargs["console"]
        new_session.surface = kwargs["surface"]
        new_session.store = _FakeStore(
            sessions_dir=sessions_dir,
            session_id=kwargs["session_id_override"],
            enabled=not kwargs["no_log"],
        )
        new_session.client = SimpleNamespace(
            api_key="override-key",
            route_identity=active_route_identity,
        )
        new_session.usage_role = kwargs["usage_role"]
        new_session.tool_output_offloader = None
        new_session.conversation_compactor = None
        new_session.messages = []
        return new_session

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)

    ok, message, loaded_history = cli_mod._resume_chat_session(
        session=current,
        target_session_id=target_id,
    )

    assert ok is True
    assert "current provider and model were kept for safety" in message
    assert loaded_history == [
        {"role": "user", "content": "continue safely"},
        {"role": "assistant", "content": "visible historical answer"},
    ]
    assert current.messages[-2:] == loaded_history


def test_resume_chat_session_uses_current_config_defaults_for_older_step_payloads(
    tmp_path: Path, monkeypatch
) -> None:
    class _TraceSurface:
        trace_level = "full"

        def set_trace_level(self, level: str) -> str:
            self.trace_level = level
            return level

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    target_id = "resume-legacy"
    target_path = sessions_dir / f"{target_id}.jsonl"
    active_route_identity = build_provider_route_identity(
        protocol="openai_compat",
        base_url="https://api.openai.com/v1",
        provider_key="openai",
        model="test-model",
    )
    matching_message_metadata = stamp_provider_metadata_for_route(
        {"provider_state": {"opaque": "must-strip-without-session-identity"}},
        active_route_identity,
    )
    target_events = [
        {
            "type": "session_start",
            "payload": {
                "mode": "readonly",
                "model": "resumed-model",
                "yes": False,
            },
        },
        {"type": "user_message", "payload": {"content": "legacy question"}},
        {
            "type": "assistant_message",
            "payload": {
                "message": {
                    "role": "assistant",
                    "content": "legacy answer",
                    PROVIDER_METADATA_KEY: matching_message_metadata,
                    "reasoning_content": "legacy raw reasoning",
                    "reasoning": "legacy raw provider state",
                    "reasoning_details": [{"opaque": "legacy detail"}],
                }
            },
        },
    ]
    target_path.write_text(
        "\n".join(json.dumps(ev) for ev in target_events) + "\n",
        encoding="utf-8",
    )

    current = _FakeSession()
    current.cfg = AppConfig(
        model="test-model",
        max_steps=37,
        step_budget_policy="fixed",
        task_max_steps=91,
        subagent_max_steps=13,
    )
    current.root = tmp_path
    current.mode = "auto"
    current.yes = True
    current.max_steps = 5
    current.console = None
    current.surface = _TraceSurface()
    current.store = _FakeStore(sessions_dir=sessions_dir, session_id="current-session")
    current.client = SimpleNamespace(api_key="override-key")
    current.usage_role = "main"
    current.tool_output_offloader = None
    current.conversation_compactor = None
    current.messages = []
    current.close = lambda: None

    def fake_create_session(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["session_source"] == "resume"
        assert kwargs["session_source_metadata"] == {
            "from_session_id": "current-session",
            "resumed_session_id": target_id,
            "loaded_message_count": 2,
            "active_workdir_relpath": None,
        }
        assert kwargs["max_steps"] == 37
        assert kwargs["enable_chat_turn_step_budget"] is True
        assert kwargs["chat_turn_fixed_override"] is None
        cfg = kwargs["cfg"]
        assert cfg.model == "test-model"
        assert cfg.max_steps == 37
        assert cfg.step_budget_policy == "limited"
        assert cfg.task_max_steps == 91
        assert cfg.subagent_max_steps == 13
        new_session = _FakeSession()
        new_session.cfg = kwargs["cfg"]
        new_session.root = kwargs["root"]
        new_session.mode = kwargs["mode"]
        new_session.yes = kwargs["yes"]
        new_session.max_steps = kwargs["max_steps"]
        new_session.console = kwargs["console"]
        new_session.surface = kwargs["surface"]
        new_session.store = _FakeStore(
            sessions_dir=sessions_dir,
            session_id=kwargs["session_id_override"],
            enabled=not kwargs["no_log"],
        )
        new_session.client = SimpleNamespace(
            api_key="override-key",
            route_identity=active_route_identity,
        )
        new_session.usage_role = kwargs["usage_role"]
        new_session.tool_output_offloader = None
        new_session.conversation_compactor = None
        new_session.messages = []
        return new_session

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)

    ok, message, loaded_history = cli_mod._resume_chat_session(
        session=current,
        target_session_id=target_id,
    )

    assert ok is True
    assert "Resumed session:" in message
    assert loaded_history == [
        {"role": "user", "content": "legacy question"},
        {"role": "assistant", "content": "legacy answer"},
    ]
    assert current.messages[-2:] == loaded_history
    assert current.surface.trace_level == "compact"


def test_resume_chat_session_preserves_web_provenance_in_reopened_store(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    target_id = "resume-web-provenance"
    target_path = sessions_dir / f"{target_id}.jsonl"
    target_events = [
        {"type": "session_start", "payload": {"mode": "review", "yes": False}},
        {
            "type": "user_message",
            "payload": {"content": "Please inspect https://docs.example.com/spec"},
        },
        {
            "type": "tool_result",
            "payload": {
                "name": "web_search",
                "step": 1,
                "result": {
                    "query": "docs example",
                    "backend": "openai_responses",
                    "sources": [
                        {
                            "title": "Guide",
                            "url": "https://docs.example.com/guide",
                            "snippet": "Official guide",
                        }
                    ],
                },
            },
        },
        {"type": "assistant_message", "payload": {"content": "Loaded docs."}},
    ]
    target_path.write_text(
        "\n".join(json.dumps(ev) for ev in target_events) + "\n",
        encoding="utf-8",
    )

    current = _FakeSession()
    current.cfg = AppConfig(model="test-model", max_steps=10)
    current.root = tmp_path
    current.mode = "review"
    current.yes = True
    current.max_steps = 5
    current.console = None
    current.surface = object()
    current.store = _FakeStore(sessions_dir=sessions_dir, session_id="current-session")
    current.client = SimpleNamespace(api_key="override-key")
    current.usage_role = "main"
    current.tool_output_offloader = None
    current.conversation_compactor = None
    current.messages = []
    current.close = lambda: None

    def fake_create_session(**kwargs):  # type: ignore[no-untyped-def]
        new_session = _FakeSession()
        new_session.cfg = kwargs["cfg"]
        new_session.root = kwargs["root"]
        new_session.mode = kwargs["mode"]
        new_session.yes = kwargs["yes"]
        new_session.max_steps = kwargs["max_steps"]
        new_session.console = kwargs["console"]
        new_session.surface = kwargs["surface"]
        new_session.store = SessionStore(
            enabled=not kwargs["no_log"],
            artifact_persistence_enabled=(not kwargs["no_log"])
            or kwargs["session_log_dir_override"] is not None,
            sessions_dir=kwargs["session_log_dir_override"],
            session_id=kwargs["session_id_override"],
            cwd=str(kwargs["root"]),
            repo_root=str(kwargs["root"]),
        )
        new_session.client = SimpleNamespace(api_key="override-key")
        new_session.usage_role = kwargs["usage_role"]
        new_session.tool_output_offloader = None
        new_session.conversation_compactor = None
        new_session.messages = []
        return new_session

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)

    ok, message, loaded_history = cli_mod._resume_chat_session(
        session=current,
        target_session_id=target_id,
    )

    assert ok is True
    assert "Resumed session:" in message
    assert loaded_history == [
        {"role": "user", "content": "Please inspect https://docs.example.com/spec"},
        {"role": "assistant", "content": "Loaded docs."},
    ]
    assert current.store.classify_web_fetch_url("https://docs.example.com/spec") == "user_provided"
    assert (
        current.store.classify_web_fetch_url("https://docs.example.com/guide")
        == "returned_by_web_search"
    )


def test_resume_chat_session_merges_newer_web_artifact_ahead_of_log(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    target_id = "resume-web-artifact-ahead"
    target_path = sessions_dir / f"{target_id}.jsonl"
    target_events = [
        {"type": "session_start", "payload": {"mode": "review", "yes": False}},
        {
            "type": "user_message",
            "payload": {"content": "Please inspect https://docs.example.com/spec"},
        },
        {"type": "assistant_message", "payload": {"content": "Loaded docs."}},
    ]
    target_path.write_text(
        "\n".join(json.dumps(ev) for ev in target_events) + "\n",
        encoding="utf-8",
    )
    artifact_root = sessions_dir / target_id
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_payload = build_web_research_artifact_from_events(
        target_events
        + [
            {
                "type": "tool_result",
                "payload": {
                    "name": "web_search",
                    "step": 1,
                    "result": {
                        "query": "docs example",
                        "backend": "openai_responses",
                        "sources": [
                            {
                                "title": "Guide",
                                "url": "https://docs.example.com/guide",
                                "snippet": "Artifact-only guide",
                            }
                        ],
                    },
                },
            }
        ]
    )
    (artifact_root / "web_research_sources.json").write_text(
        json.dumps(artifact_payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )

    current = _FakeSession()
    current.cfg = AppConfig(model="test-model", max_steps=10)
    current.root = tmp_path
    current.mode = "review"
    current.yes = True
    current.max_steps = 5
    current.console = None
    current.surface = object()
    current.store = _FakeStore(sessions_dir=sessions_dir, session_id="current-session")
    current.client = SimpleNamespace(api_key="override-key")
    current.usage_role = "main"
    current.tool_output_offloader = None
    current.conversation_compactor = None
    current.messages = []
    current.close = lambda: None

    def fake_create_session(**kwargs):  # type: ignore[no-untyped-def]
        new_session = _FakeSession()
        new_session.cfg = kwargs["cfg"]
        new_session.root = kwargs["root"]
        new_session.mode = kwargs["mode"]
        new_session.yes = kwargs["yes"]
        new_session.max_steps = kwargs["max_steps"]
        new_session.console = kwargs["console"]
        new_session.surface = kwargs["surface"]
        new_session.store = SessionStore(
            enabled=not kwargs["no_log"],
            artifact_persistence_enabled=(not kwargs["no_log"])
            or kwargs["session_log_dir_override"] is not None,
            sessions_dir=kwargs["session_log_dir_override"],
            session_id=kwargs["session_id_override"],
            cwd=str(kwargs["root"]),
            repo_root=str(kwargs["root"]),
        )
        new_session.client = SimpleNamespace(api_key="override-key")
        new_session.usage_role = kwargs["usage_role"]
        new_session.tool_output_offloader = None
        new_session.conversation_compactor = None
        new_session.messages = []
        return new_session

    monkeypatch.setattr(cli_mod, "create_session", fake_create_session)

    ok, message, loaded_history = cli_mod._resume_chat_session(
        session=current,
        target_session_id=target_id,
    )

    assert ok is True
    assert "Resumed session:" in message
    assert loaded_history == [
        {"role": "user", "content": "Please inspect https://docs.example.com/spec"},
        {"role": "assistant", "content": "Loaded docs."},
    ]
    assert current.store.classify_web_fetch_url("https://docs.example.com/spec") == "user_provided"
    assert (
        current.store.classify_web_fetch_url("https://docs.example.com/guide")
        == "returned_by_web_search"
    )


def _write_stamped_resume_log(
    path: Path,
    *,
    session_id: str,
    workspace_root: str,
    owner: str | None,
    ts: str,
    content: str,
) -> None:
    stamp = {"owner": owner} if owner else {}
    events = [
        {
            "type": "session_start",
            "ts": ts,
            "session_id": session_id,
            "workspace_root": workspace_root,
            **stamp,
            "payload": {"mode": "auto", "workspace_root": workspace_root},
        },
        {
            "type": "user_message",
            "ts": ts,
            "session_id": session_id,
            "workspace_root": workspace_root,
            **stamp,
            "payload": {"content": content},
        },
    ]
    path.write_text(
        "".join(json.dumps(event, ensure_ascii=True) + "\n" for event in events),
        encoding="utf-8",
    )


def test_collect_chat_resume_candidates_end_to_end_hides_foreign_log_on_disk(
    tmp_path: Path,
) -> None:
    """Full composition, no stubs: disk -> list_sessions -> owner filter.

    A foreign-stamped log file sitting in the real sessions dir (same
    workspace) must never surface, while the local account's own store-written
    session and a legacy unstamped log stay visible.
    """
    import uuid

    from alysis_code.session_store import SessionStore

    ws = tmp_path / "project"
    ws.mkdir()

    own_id = "20260711T120000Z_aaaaaaaa"
    own_store = SessionStore(
        enabled=True,
        sessions_dir=tmp_path,
        session_id=own_id,
        cwd=str(ws),
        repo_root=str(ws),
        workspace_root=str(ws),
    )
    own_store.append("session_start", {"mode": "auto"})
    own_store.append("user_message", {"content": "my own conversation"})
    own_store.close()

    foreign_id = "20260711T100000Z_bbbbbbbb"
    _write_stamped_resume_log(
        tmp_path / f"{foreign_id}.jsonl",
        session_id=foreign_id,
        workspace_root=str(ws),
        owner=f"foreign-user@foreign-host-{uuid.uuid4().hex}",
        ts="2026-07-11T10:00:00+00:00",
        content="ANOTHER USER'S PRIVATE CHAT",
    )

    legacy_id = "20260710T090000Z_cccccccc"
    _write_stamped_resume_log(
        tmp_path / f"{legacy_id}.jsonl",
        session_id=legacy_id,
        workspace_root=str(ws),
        owner=None,
        ts="2026-07-10T09:00:00+00:00",
        content="my old pre-upgrade chat",
    )

    candidates = cli_mod._collect_chat_resume_candidates(
        sessions_dir=tmp_path,
        current_session_id="current",
        workspace_root=str(ws),
    )
    ids = [info.session_id for info in candidates]
    assert own_id in ids
    assert legacy_id in ids
    assert foreign_id not in ids


def test_collect_chat_resume_candidates_filters_before_max_candidates_slice(
    tmp_path: Path, monkeypatch
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    sessions = [
        SessionInfo(
            session_id=f"foreign{i}",
            path=tmp_path / f"foreign{i}.jsonl",
            mtime=100.0 - i,
            workspace_root=str(ws),
            owner="mallory@other-pc",
        )
        for i in range(2)
    ] + [
        SessionInfo(
            session_id="own",
            path=tmp_path / "own.jsonl",
            mtime=1.0,
            workspace_root=str(ws),
            owner="alice@laptop",
        )
    ]
    monkeypatch.setattr(cli_mod, "list_sessions", lambda _sessions_dir: sessions)
    monkeypatch.setattr(cli_mod, "local_session_owner", lambda: "alice@laptop", raising=False)

    # Two newer foreign sessions must not consume the slice: filtering happens
    # BEFORE max_candidates, so the older own session survives.
    scoped = cli_mod._collect_chat_resume_candidates(
        sessions_dir=tmp_path,
        current_session_id="current",
        workspace_root=str(ws),
        max_candidates=1,
    )
    assert [s.session_id for s in scoped] == ["own"]


def test_classic_resume_explicit_id_survives_fully_filtered_candidates(
    tmp_path: Path, monkeypatch
) -> None:
    """The documented escape hatch: '/resume <id>' must reach the direct-id
    path even when owner scoping filters the candidate list down to nothing
    (classic/non-TUI handler)."""
    import io
    import uuid

    from alysis_code.cli_impl import chat as chat_impl_mod
    from alysis_code.cli_impl.chat import commands as chat_commands_mod

    ws = tmp_path / "ws"
    ws.mkdir()
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    foreign_id = "20260101T000000Z_feedbeef"
    _write_stamped_resume_log(
        sessions_dir / f"{foreign_id}.jsonl",
        session_id=foreign_id,
        workspace_root=str(ws),
        owner=f"mallory@other-pc-{uuid.uuid4().hex}",
        ts="2026-01-01T00:00:00+00:00",
        content="foreign conversation",
    )

    captured: dict[str, Any] = {}

    def fake_resume(*, session: Any, target_session_id: str) -> tuple[bool, str, list]:
        captured["target"] = target_session_id
        # Resume first restores the historical base grant. The persona overlay
        # is reapplied separately below.
        session.mode = "review"
        session.pending_permissions_mode = None
        return True, "Resumed.", []

    def fake_reapply_persona(*, session: Any, console: Any) -> None:
        del console
        captured["persona_reapplied"] = True
        session.mode = "readonly"
        session.persona_restore_mode = "review"

    chat_impl_mod._sync_cli_globals(cli_mod)
    # Prime the facade-to-command dependency sync exactly as an earlier classic
    # command does in the full suite.  The direct resume override must survive a
    # later dispatch regardless of whether this test runs first or after chat.
    chat_commands_mod._sync_command_globals(chat_impl_mod.__dict__)
    monkeypatch.setattr(cli_mod, "_resume_chat_session", fake_resume, raising=False)
    monkeypatch.setattr(chat_commands_mod, "_resume_chat_session", fake_resume, raising=False)
    monkeypatch.setattr(
        chat_commands_mod,
        "_reapply_resumed_persona",
        fake_reapply_persona,
        raising=False,
    )

    session = SimpleNamespace(
        cfg=AppConfig(model="test-model"),
        mode="auto",
        pending_permissions_mode="review",
        store=SimpleNamespace(
            sessions_dir=sessions_dir,
            session_id="current",
            workspace_root=str(ws),
            git_root=None,
        ),
    )
    out = io.StringIO()
    result = chat_impl_mod._handle_chat_command(
        input_text=f"/resume {foreign_id}",
        root=tmp_path,
        session=session,
        pending_images=[],
        console=Console(file=out, force_terminal=False),
        forge_state=cli_mod._ForgeChatState(),
    )

    assert result == "handled"
    assert captured.get("target") == foreign_id
    assert captured.get("persona_reapplied") is True
    assert session.mode == "readonly"
    assert session.persona_restore_mode == "review"
    assert session.pending_permissions_mode == "review"
    assert "No previous sessions" not in out.getvalue()
