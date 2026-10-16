from __future__ import annotations

import threading
import time

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import FloatContainer, Layout
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.widgets import TextArea
from test_user_questions_integration import make_session

from alysis_code.cli_impl.tui import app as app_module
from alysis_code.cli_impl.tui import question_panel as panel_module
from alysis_code.cli_impl.tui.question_panel import QuestionPanel
from alysis_code.cli_impl.tui.state import TuiState
from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.user_questions import UserQuestionBroker


class SizedOutput(DummyOutput):
    def __init__(self, columns=80, rows=30):
        self.columns, self.rows = columns, rows

    def get_size(self):
        return Size(rows=self.rows, columns=self.columns)


def wait_until(predicate, timeout=8):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for TUI state")


@pytest.mark.parametrize("columns, rows", [(40, 14), (60, 20), (80, 30)])
def test_panel_paints_at_narrow_sizes_and_requires_explicit_selection(columns, rows):
    import asyncio

    broker = UserQuestionBroker()
    broker.request(
        {
            "questions": [
                {
                    "id": "layout",
                    "question": "Which layout do you prefer?",
                    "kind": "multiple",
                    "options": ["Simple", "Detailed"],
                }
            ]
        }
    )
    composer = TextArea(text="My draft stays here")
    panel = QuestionPanel(
        broker=broker, restore_focus=lambda: None, blocked=lambda: False, on_answer=lambda _: None
    )
    bindings = KeyBindings()
    panel.register(bindings)
    with create_pipe_input() as pipe:
        application = Application(
            layout=Layout(FloatContainer(composer, floats=[panel.float])),
            key_bindings=bindings,
            input=pipe,
            output=SizedOutput(columns, rows),
            full_screen=True,
        )

        async def render():
            with set_app(application):
                panel.sync()
                panel.advance()
                assert broker.active is not None
                assert panel.error
                assert panel.answers[0]["selected"] == []
                application.renderer.render(application, application.layout)
                screen = application.renderer.last_rendered_screen
                painted = [
                    "".join(screen.data_buffer[y][x].char for x in range(columns)).rstrip()
                    for y in range(rows)
                ]
                broker.reset()
                broker.request(
                    {
                        "questions": [
                            {
                                "id": "long",
                                "question": "Long context. " * 55 + "final-detail-99",
                                "kind": "text",
                                "options": [],
                            }
                        ]
                    }
                )
                panel.sync()
                panel.question_scroll = len(panel._heading().splitlines()) - 1
                application.render_counter += 1
                application.renderer.render(application, application.layout)
                screen = application.renderer.last_rendered_screen
                scrolled = "\n".join(
                    "".join(screen.data_buffer[y][x].char for x in range(columns))
                    for y in range(rows)
                )
                assert "final-detail-99" in scrolled
                return painted

        painted = asyncio.run(render())
    text = "\n".join(painted)
    assert "Which layout" in text
    assert "Simple" in text
    assert "too small" not in text.lower()
    assert composer.text == "My draft stays here"


@pytest.mark.parametrize(
    "action", ["answer", "custom", "timeout", "partial_timeout", "dismiss", "interrupt"]
)
def test_real_tui_keyboard_to_agent_roundtrip(tmp_path, monkeypatch, action):
    captured = {}
    done = threading.Event()
    failures = []
    messages_seen = []
    painted = []
    now = [0.0]
    real_panel = panel_module.QuestionPanel

    def capture_panel(**kwargs):
        panel = real_panel(**kwargs)
        captured["panel"] = panel
        return panel

    def capture_application(*args, **kwargs):
        application = Application(*args, **kwargs)
        captured["app"] = application

        def after_render(app):
            screen = app.renderer.last_rendered_screen
            if screen and captured.get("panel") and captured["panel"].visible:
                painted.append(
                    "\n".join(
                        "".join(screen.data_buffer[y][x].char for x in range(80)).rstrip()
                        for y in range(30)
                    )
                )

        application.after_render += after_render
        return application

    monkeypatch.setattr(panel_module, "QuestionPanel", capture_panel)
    monkeypatch.setattr(app_module, "Application", capture_application)

    class Client:
        model = "test-model"
        temperature = 0.0

        def chat(self, *, messages, **kwargs):
            messages_seen.append(list(messages))
            if len(messages_seen) == 1:
                questions = [
                    {
                        "id": "theme",
                        "question": "Which theme?",
                        "kind": "single",
                        "options": ["Light", "Dark"],
                    },
                    {
                        "id": "features",
                        "question": "Which features?",
                        "kind": "multiple",
                        "options": ["Search", "History"],
                    },
                    {"id": "notes", "question": "Any details?", "kind": "text", "options": []},
                ]
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCall("ask1", "request_user_input", {"questions": questions})],
                    raw={},
                )
            if len(messages_seen) == 2:
                identifier = captured["session"].surface.user_questions.active.id
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCall("wait1", "wait_user_input", {"request_id": identifier})],
                    raw={},
                )
            assert len(messages_seen) == 3
            return LLMResponse(content="Preferences received.", tool_calls=[], raw={})

    def build(surface):
        if action in ("timeout", "partial_timeout"):
            surface.user_questions = UserQuestionBroker(
                clock=lambda: now[0], on_timeout=surface._on_question_timeout
            )
        session = make_session(tmp_path, surface, Client())
        captured["session"] = session
        return session

    with create_pipe_input() as pipe:

        def drive():
            try:
                wait_until(lambda: captured.get("app") and captured["app"].is_running)
                pipe.send_text("Ask about my preferences.\r")
                wait_until(lambda: captured.get("panel") and captured["panel"].visible)
                wait_until(lambda: bool(painted))
                panel = captured["panel"]
                assert not panel.editing
                assert panel.answers[0]["selected"] == []
                if action == "interrupt":
                    pipe.send_text("\x03")
                elif action == "dismiss":
                    pipe.send_text("\x18")
                elif action in ("timeout", "partial_timeout"):
                    assert any("Continues in 60s" in screen for screen in painted)
                    now[0] = 59.0
                    pipe.send_text("\x1b[B")
                    wait_until(lambda: panel.cursor == 1 and panel.broker.remaining_seconds == 60)
                    if action == "partial_timeout":
                        pipe.send_text("\r")
                        wait_until(lambda: panel.index == 1)
                        pipe.send_text("\t")
                        wait_until(lambda: panel.editing)
                        now[0] = 118.0
                        pipe.send_text("Unconfirmed draft")
                        wait_until(lambda: panel.broker.remaining_seconds == 60)
                        now[0] = 179.0
                    else:
                        now[0] = 120.0
                    wait_until(lambda: not panel.visible)
                elif action == "custom":
                    # The custom row opens the editor. Returning to choices,
                    # opening help, and going back must preserve the draft.
                    pipe.send_text("\x1b[B\x1b[B\r")
                    wait_until(lambda: panel.editing)
                    pipe.send_text("Custom theme\x0awith contrast\x1b")
                    wait_until(lambda: not panel.editing)
                    pipe.send_text("\x1bOP")  # F1 opens the secondary controls
                    wait_until(lambda: panel.help_visible)
                    pipe.send_text("\x1b")
                    wait_until(lambda: not panel.help_visible)
                    assert panel.visible
                    pipe.send_text("\r")
                    wait_until(lambda: panel.editing)
                    assert panel.input.text == "Custom theme\nwith contrast"
                    pipe.send_text("\r")
                    wait_until(lambda: panel.index == 1)
                    pipe.send_text(" \tKeep exports optional\t")
                    wait_until(lambda: panel.answers[1]["text"] == "Keep exports optional")
                    pipe.send_text("\x1b[D")  # previous question from choices
                    wait_until(lambda: panel.index == 0)
                    assert panel.input.text == "Custom theme\nwith contrast"
                    pipe.send_text("\r")
                    wait_until(lambda: panel.index == 1)
                    assert panel.answers[1]["selected"] == ["Search"]
                    assert panel.input.text == "Keep exports optional"
                    pipe.send_text("\r")
                    wait_until(lambda: panel.index == 2)
                    pipe.send_text("Exact custom answer 73\r")
                else:
                    # Hiding preserves the ordinary composer and reopens without
                    # silently choosing the initially highlighted option.
                    pipe.send_text("\x1b")
                    wait_until(lambda: not panel.visible)
                    pipe.send_text("keep this draft")
                    pipe.send_text("\x07")
                    wait_until(lambda: panel.visible)
                    pipe.send_text("\x1b[B\r")  # Enter explicitly chooses Dark, then next
                    wait_until(lambda: panel.index == 1)
                    pipe.send_text(" \x1b[B \r")  # both features
                    wait_until(lambda: panel.index == 2)
                    pipe.send_text("Exact custom answer 73\r")
                assert done.wait(8)
            except BaseException as exc:
                failures.append(exc)
            finally:
                app = captured.get("app")
                if app and app.loop and app.is_running:
                    app.loop.call_soon_threadsafe(lambda: app.exit(result="test-finished"))

        driver = threading.Thread(target=drive, daemon=True)
        driver.start()
        try:
            _, transcript = app_module.run_tui(
                TuiState(model_name="test-model"),
                session_builder=build,
                on_turn_complete=done.set,
                owl_color=False,
                input=pipe,
                output=SizedOutput(),
                workspace_root=tmp_path,
            )
        finally:
            driver.join(timeout=2)
            if captured.get("session"):
                captured["session"].close()
    assert not failures, failures
    assert painted and any("Which theme?" in screen for screen in painted)
    assert captured["session"].surface.user_questions.active is None
    if action in ("answer", "custom"):
        import json

        answers = [
            json.loads(message["content"])
            for message in messages_seen[-1]
            if message.get("tool_call_id") == "wait1"
        ]
        assert len(answers) == 1
        assert answers[0]["status"] == "answered"
        expected = (
            [
                {"id": "theme", "selected": [], "text": "Custom theme\nwith contrast"},
                {"id": "features", "selected": ["Search"], "text": "Keep exports optional"},
            ]
            if action == "custom"
            else [
                {"id": "theme", "selected": ["Dark"], "text": ""},
                {"id": "features", "selected": ["Search", "History"], "text": ""},
            ]
        )
        assert answers[0]["answers"] == [
            *expected,
            {"id": "notes", "selected": [], "text": "Exact custom answer 73"},
        ]
        assert any("Exact custom answer 73" in text for role, text in transcript if role == "user")
        assert not any(text == "keep this draft" for role, text in transcript if role == "user")
    elif action in ("timeout", "partial_timeout"):
        import json

        results = [
            json.loads(message["content"])
            for message in messages_seen[-1]
            if message.get("tool_call_id") == "wait1"
        ]
        assert len(results) == 1
        assert results[0]["status"] == "timed_out"
        assert results[0]["answers"] == (
            [{"id": "theme", "selected": ["Dark"], "text": ""}]
            if action == "partial_timeout"
            else []
        )
        assert "Unconfirmed draft" not in str(results)
        assert any("Question timer expired" in text for role, text in transcript if role == "info")
    elif action == "dismiss":
        assert any('"dismissed"' in str(message.get("content")) for message in messages_seen[-1])
    else:
        assert len(messages_seen) == 2
