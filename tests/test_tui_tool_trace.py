from __future__ import annotations

import io
import json
import subprocess
from dataclasses import replace

import httpx
import pytest
from prompt_toolkit.utils import get_cwidth
from rich.console import Console

from alysis_code.agent.tools_assembly import _unified_diff, build_tools
from alysis_code.cli_impl.tui.surface import TuiSurface, set_active_cancellation
from alysis_code.cli_impl.tui.tool_trace import (
    MAX_CHARS,
    bounded_text,
    output_rows,
    patch_entry,
    patch_rows,
    preformatted_rows,
    tool_output,
)
from alysis_code.cli_impl.tui.transcript import TuiTranscript
from alysis_code.config import AppConfig
from alysis_code.profile_presets import PROFILE_PRESETS, make_profile_from_preset
from alysis_code.session_store import SessionStore
from alysis_code.surface.hidden_surface import NestedSubagentSurface
from alysis_code.surface.types import ApprovalDecision, PatchEvent, ToolOutputEvent, ToolStartEvent


def _surface(level="full"):
    transcript = TuiTranscript()
    surface = TuiSurface(transcript)
    surface.set_trace_level(level)
    return surface, transcript


def _tools(root, surface, *, mode="auto"):
    return build_tools(
        root=root,
        console=Console(file=io.StringIO(), force_terminal=False),
        surface=surface,
        store=SessionStore(
            enabled=False,
            sessions_dir=root / "sessions",
            session_id="trace",
            cwd=str(root),
            repo_root=str(root),
        ),
        cfg=AppConfig(model="test-model"),
        mode=mode,
        yes=mode == "auto",
        non_interactive=False,
    )


def _text(rows):
    return "\n".join("".join(text for _, text in row) for row in rows)


@pytest.mark.parametrize("level", ["off", "compact", "full"])
def test_trace_levels_show_actual_diffs_and_tool_output(level):
    surface, transcript = _surface(level)
    surface.on_tool_start(ToolStartEvent("call", "shell", {"cmd": "python demo.py"}, 1))
    surface.on_tool_output(ToolOutputEvent("call", "shell", '{"stdout":"    hello\\n"}'))
    surface.on_patch_generated(
        PatchEvent(["demo.py"], _unified_diff("old\n", "new\n", "demo.py"), applied=True)
    )
    details = [text for role, text in transcript.entries if role == "tool_output"]
    patches = [text for role, text in transcript.entries if role == "patch"]
    assert bool(details) is (level == "full")
    assert bool(patches) is (level != "off")
    if details:
        assert "$ python demo.py" in details[0]
        assert "    hello\n" in details[1]
    if patches:
        rendered = _text(patch_rows(patches[0], 100, expanded=level == "full"))
        assert "CHANGES SAVED · demo.py · 1 line added · 1 line removed" in rendered
        assert ("1 -old" in rendered) is (level == "full")
        assert ("1 +new" in rendered) is (level == "full")


def test_diff_counts_headers_multifile_no_newline_and_noop():
    diff = _unified_diff("--old", "++new", "a.py") + _unified_diff("", "more\n", "b.py")
    entry = patch_entry(PatchEvent(["a.py", "b.py"], diff, applied=True))
    rendered = _text(patch_rows(entry, 120, expanded=True))
    assert "2 lines added · 1 line removed" in rendered
    assert "1 ---old" in rendered and "1 +++new" in rendered
    assert "File: b.py" in rendered
    assert rendered.count("No newline at end of file") == 2
    noop = _text(patch_rows(patch_entry(PatchEvent(["x"], "", applied=True)), 80, expanded=True))
    assert "no text changes" in noop
    assert "Preview" in _text(patch_rows(patch_entry(PatchEvent(["x"], diff)), 100, expanded=False))


def test_viewed_source_is_explicitly_read_only_even_with_plus_minus_content():
    surface, transcript = _surface()
    surface.on_tool_output(
        ToolOutputEvent(
            "read", "fs_read", json.dumps({"path": "x.py", "content": "+existing\n-existing\n"})
        )
    )
    entry = next(text for role, text in transcript.entries if role == "tool_output")
    rendered = _text(output_rows(entry, 100))
    assert "VIEWING ONLY" in rendered and "no file changes" in rendered
    assert "Viewed content — unchanged:" in rendered
    assert "+existing" in rendered and "-existing" in rendered
    assert "ADDED" not in rendered and "REMOVED" not in rendered


def test_diff_labels_repeat_on_wrapped_code_and_distinguish_unsaved_preview():
    diff = _unified_diff("same\nold " + "x" * 80, "same\nnew " + "y" * 80, "x.py")
    rendered = _text(
        patch_rows(patch_entry(PatchEvent(["x.py"], diff, applied=True)), 40, expanded=True)
    )
    assert "UNCHANGED 1  same" in rendered
    assert rendered.count("REMOVED") > 1 and rendered.count("ADDED") > 1
    assert "REMOVED   2 -old" in rendered and "ADDED     2 +new" in rendered
    proposed = _text(patch_rows(patch_entry(PatchEvent(["x.py"], diff)), 100, expanded=True))
    assert "PROPOSED CHANGES" in proposed and "have not been saved" in proposed
    assert "1 line to add" in proposed and "1 line to remove" in proposed
    assert "REMOVED" not in proposed and "ADDED" not in proposed


def test_full_trace_redacts_bounds_and_preserves_source_whitespace(monkeypatch):
    raw = "\x1b[31mOPENAI_API_KEY=secret-value\x1b[0m\n\tprint('界')\n\n"
    rendered = tool_output(json.dumps({"content": raw, "ok": True}))
    assert "secret-value" not in rendered and "redacted" in rendered.lower()
    assert "\x1b" not in rendered
    assert "    print('界')\n\n" in _text(preformatted_rows(rendered, 80))
    for row in preformatted_rows("    界界界abcdef", 7):
        assert get_cwidth(_text([row])) <= 7
    assert "truncated" in bounded_text("x" * (MAX_CHARS + 1))
    assert "truncated" in bounded_text("line\n" * 200)
    diff = _unified_diff("", "api_key=secret-value\n" * 200, "x")
    entry = patch_entry(PatchEvent(["x"], diff, applied=True))
    assert "secret-value" not in entry
    assert "+200 -0" in entry and "truncated" in entry
    monkeypatch.setenv("TEST_TRACE_SECRET", "environment-secret-value")
    output = tool_output(json.dumps({"stdout": "environment-secret-value", "password": "abc123"}))
    assert "environment-secret-value" not in output and "abc123" not in output


def test_nested_patch_attribution_and_cancellation():
    surface, transcript = _surface()
    child = NestedSubagentSurface(
        surface, subagent_name="coder", subagent_mode="code", workspace_view="isolated"
    )
    child.on_patch_generated(PatchEvent(["x"], _unified_diff("", "yes\n", "x"), applied=True))
    assert "coder [isolated]" in transcript.entries[-1][1]
    before = list(transcript.entries)
    token = type("Token", (), {"is_cancelled": True})()
    set_active_cancellation(token)
    try:
        child.on_patch_generated(PatchEvent(["x"], "anything"))
        surface.on_tool_output(ToolOutputEvent("id", "shell", "late output"))
    finally:
        set_active_cancellation(None)
    assert transcript.entries == before


def test_truncated_original_is_not_presented_as_exact_change_counts(tmp_path):
    (tmp_path / "large.txt").write_text("a" * 2_000_001, encoding="utf-8")
    surface, transcript = _surface()
    _tools(tmp_path, surface)["fs_write"].run({"path": "large.txt", "content": "replacement\n"})
    patch = next(text for role, text in transcript.entries if role == "patch")
    assert "partial diff · counts unavailable" in patch
    assert (tmp_path / "large.txt").read_text(encoding="utf-8") == "replacement\n"


@pytest.mark.parametrize("name", ["fs_write", "fs_edit", "git_apply_patch"])
def test_real_mutations_emit_applied_diff_only_after_disk_write(tmp_path, name):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    path = tmp_path / "demo.py"
    path.write_text("before\n", encoding="utf-8")
    surface, transcript = _surface()
    observed = []
    original = surface.on_patch_generated

    def record(event):
        observed.append((event.applied, path.read_text(encoding="utf-8")))
        original(event)

    surface.on_patch_generated = record
    args = {
        "fs_write": {"path": "demo.py", "content": "after\n"},
        "fs_edit": {
            "path": "demo.py",
            "edits": [{"op": "replace_exact", "target": "before", "replacement": "after"}],
        },
        "git_apply_patch": {"patch": _unified_diff("before\n", "after\n", "demo.py")},
    }[name]
    _tools(tmp_path, surface)[name].run(args)
    assert observed == [(True, "after\n")]
    patch = next(text for role, text in transcript.entries if role == "patch")
    assert "+1 -1" in patch and "-before" in patch and "+after" in patch


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("name", ["fs_write", "fs_edit", "git_apply_patch"])
def test_denied_or_stale_write_never_claims_applied(tmp_path, stale, name):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    path = tmp_path / "demo.py"
    path.write_text("before\n", encoding="utf-8")
    surface, transcript = _surface()

    def approve(request):
        if stale:
            path.write_text("external edit\n", encoding="utf-8")
        return ApprovalDecision(allow=stale)

    surface._approval_ui = approve
    args = {
        "fs_write": {"path": "demo.py", "content": "after\n"},
        "fs_edit": {
            "path": "demo.py",
            "edits": [{"op": "replace_exact", "target": "before", "replacement": "after"}],
        },
        "git_apply_patch": {"patch": _unified_diff("before\n", "after\n", "demo.py")},
    }[name]
    tool = _tools(tmp_path, surface, mode="review")[name]
    if stale:
        result = tool.run(args)
        assert stale and "error" in result
    else:
        from alysis_code.agent.errors import ApprovalDeclinedError

        with pytest.raises(ApprovalDeclinedError):
            tool.run(args)
    assert not any(role == "patch" for role, _ in transcript.entries)
    assert path.read_text(encoding="utf-8") == ("external edit\n" if stale else "before\n")


@pytest.mark.parametrize("preset", PROFILE_PRESETS, ids=lambda preset: preset.key)
@pytest.mark.parametrize("stream", [False, True])
def test_every_provider_decodes_tools_into_real_edits_and_full_trace(tmp_path, preset, stream):
    from test_agent_loop_event_emission import _make_session
    from test_llm_factory_profiles import _cfg_with_profile
    from test_provider_trace_contract import _response

    from alysis_code.llm.factory import make_llm_client, resolve_model_protocol

    profile = make_profile_from_preset(preset)
    if not profile.base_url:
        profile = replace(profile, base_url="https://custom.example/v1")
    model = profile.default_model or "test-model"
    protocol = resolve_model_protocol(
        provider_key=preset.provider_key, protocol=profile.protocol, model=model
    )
    args = {"path": "new.py", "content": "def hello():\n    return 42\n"}
    function = {"name": "fs_write", "arguments": json.dumps(args)}
    bodies = {
        "openai_compat": {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "call_write", "type": "function", "function": function}
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        "openai_responses": {
            "id": "resp_write",
            "output": [{"type": "function_call", "call_id": "call_write", **function}],
        },
        "anthropic_messages": {
            "id": "msg_write",
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_write", "name": "fs_write", "input": args}
            ],
            "stop_reason": "tool_use",
        },
        "gemini_generate_content": {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {"name": "fs_write", "args": args},
                                "thoughtSignature": "opaque",
                            }
                        ],
                    },
                    "finishReason": "STOP",
                }
            ]
        },
        "gemini_interactions": {
            "id": "interaction_write",
            "steps": [
                {
                    "type": "model_output",
                    "content": [
                        {
                            "type": "function_call",
                            "id": "call_write",
                            "name": "fs_write",
                            "arguments": args,
                        }
                    ],
                }
            ],
        },
    }
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) > 1:
            return _response(protocol, stream)
        body = bodies[protocol]
        if not stream:
            return httpx.Response(200, json=body)
        if protocol == "openai_compat":
            call = {**body["choices"][0]["message"]["tool_calls"][0], "index": 0}
            events = [
                {"choices": [{"delta": {"tool_calls": [call]}, "finish_reason": "tool_calls"}]}
            ]
        elif protocol == "openai_responses":
            events = [{"type": "response.completed", "response": body}]
        elif protocol == "anthropic_messages":
            events = [
                {"type": "message_start", "message": {"id": "msg_write", "content": []}},
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "call_write",
                        "name": "fs_write",
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": json.dumps(args)},
                },
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
                {"type": "message_stop"},
            ]
        else:
            events = [body]
        sse = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        if protocol == "openai_compat":
            sse += "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)

    client = make_llm_client(
        cfg=_cfg_with_profile(profile),
        api_key="test-key",
        model=model,
        transport=httpx.MockTransport(handler),
    )
    surface, transcript = _surface()
    session = _make_session(
        root=tmp_path,
        client=client,
        surface=surface,
        stream=stream,
        tool=_tools(tmp_path, surface)["fs_write"],
    )
    try:
        session.run_turn(
            "Create new.py with a hello function returning 42. Do not run verification."
        )
    finally:
        session.close()
    assert (tmp_path / "new.py").read_text(encoding="utf-8") == args["content"]
    patches = [text for role, text in transcript.entries if role == "patch"]
    assert len(patches) == 1
    rendered = _text(patch_rows(patches[0], 100, expanded=True))
    assert "CHANGES SAVED · new.py · 2 lines added · 0 lines removed" in rendered
    assert "2 +    return 42" in rendered
    assert any(role == "tool_output" and "new.py" in text for role, text in transcript.entries)


@pytest.mark.parametrize("theme", ["dark", "light", "neutral"])
def test_real_tui_renders_and_collapses_full_trace(monkeypatch, tmp_path, theme):
    import asyncio
    import importlib

    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui.state import TuiState

    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    module = importlib.import_module("alysis_code.cli_impl.tui.app")
    original = module.Application
    surfaces, errors = [], []

    class Output(DummyOutput):
        def get_size(self):
            return Size(rows=70, columns=110)

    class Session:
        def __init__(self, surface):
            self.surface = surface
            surfaces.append(surface)

        def run_turn(self, text, **kwargs):
            self.surface.on_user_message(text)
            self.surface.set_trace_level("full")
            self.surface.on_tool_start(ToolStartEvent("id", "shell", {"cmd": "python demo.py"}, 1))
            self.surface.on_tool_output(
                ToolOutputEvent("id", "shell", '{"stdout":"    TRACE_OUTPUT\\n"}')
            )
            self.surface.on_patch_generated(
                PatchEvent(
                    ["demo.py"],
                    _unified_diff("old_source\n", "    new_source\n", "demo.py"),
                    applied=True,
                )
            )
            self.surface.on_assistant_message_done("Finished.")
            return 0

    def screen(app):
        app._redraw()
        return "\n".join(
            "".join(cell.char for _, cell in sorted(row.items()))
            for _, row in sorted(app.renderer._last_screen.data_buffer.items())
        )

    def color_at(app, text):
        for row in app.renderer._last_screen.data_buffer.values():
            cells = [cell for _, cell in sorted(row.items())]
            line = "".join(cell.char for cell in cells)
            if text in line:
                return app.style.get_attrs_for_style_str(cells[line.index(text)].style).color
        raise AssertionError(f"Missing rendered text: {text}")

    with create_pipe_input() as pipe:

        class Application(original):
            def run(self, *, pre_run):
                async def exercise():
                    try:
                        pipe.send_text("show trace\r")
                        for _ in range(300):
                            if surfaces and any(
                                role == "patch" for role, _ in surfaces[0]._t.entries
                            ):
                                break
                            await asyncio.sleep(0.01)
                        full = screen(self)
                        assert "CHANGES SAVED · demo.py · 1 line added · 1 line removed" in full
                        assert "1 +    new_source" in full
                        assert "1 -old_source" in full
                        assert "    TRACE_OUTPUT" in full
                        assert "$ python demo.py" in full
                        # Check actual screen colors, including the separate count
                        # and code styles, rather than just renderer class names.
                        added = color_at(self, "+    new_source")
                        removed = color_at(self, "-old_source")
                        applied = color_at(self, "CHANGES SAVED · demo.py")
                        command = color_at(self, "$ python")
                        active = color_at(self, "▸ ")
                        assert added == applied == color_at(self, "1 line added")
                        assert removed == color_at(self, "1 line removed")
                        assert len({added, removed, command, active}) == 4
                        assert "│ " in full
                        surfaces[0].set_trace_level("compact")
                        compact = screen(self)
                        assert "CHANGES SAVED · demo.py · 1 line added · 1 line removed" in compact
                        assert "new_source" not in compact and "TRACE_OUTPUT" not in compact
                        surfaces[0].set_trace_level("off")
                        assert "CHANGES SAVED · demo.py" not in screen(self)
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        self.exit()

                def begin():
                    pre_run()
                    self.create_background_task(exercise())

                return super().run(pre_run=begin, set_exception_handler=False)

        monkeypatch.setattr(module, "Application", Application)
        module.run_tui(
            TuiState(model_name="test"),
            input=pipe,
            output=Output(),
            owl_color=False,
            background_turns=False,
            session_builder=Session,
            workspace_root=tmp_path,
            theme=theme,
        )
    if errors:
        raise errors[0]
