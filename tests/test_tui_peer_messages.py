from __future__ import annotations

import importlib
import json

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.utils import get_cwidth

from alysis_code.cli_impl.tui.app import _has_conversation, _strip_transcript_copy_chrome
from alysis_code.cli_impl.tui.peer_messages import peer_message_rows
from alysis_code.cli_impl.tui.state import TuiState
from alysis_code.cli_impl.tui.surface import TuiSurface
from alysis_code.cli_impl.tui.transcript import TuiTranscript
from alysis_code.surface.types import ToolEndEvent


def message(**values):
    return {
        "message_id": "message-1",
        "sender": "alpha",
        "recipient": "beta",
        "content": "Καλημέρα 雲\n  <literal text> **not markdown**",
        "state": "queued",
        "updated": 1,
        **values,
    }


def test_receipts_update_one_block_without_splitting_live_text_or_reasoning():
    transcript = TuiTranscript()
    surface = TuiSurface(transcript)
    original = message()
    surface.on_chat_message("chat_message_sent", original)
    surface.on_assistant_token("Still ")
    surface.on_chat_message("chat_message_state", message(state="processing", updated=2))
    surface.on_assistant_token("working")
    assert len(transcript.entries) == 2
    assert transcript.entries[-1] == ("assistant", "Still working")
    surface.on_reasoning_start("reasoning-1")
    surface.on_reasoning_token("Reasoning continues")
    surface.on_chat_message("chat_message_state", message(state="delivered", updated=3))
    assert transcript.reasoning_snapshot()[0] is not None
    surface.on_chat_message("chat_message_sent", original)  # Exact send retry.
    assert len([entry for role, entry in transcript.entries if role == "peer_message"]) == 1
    card = json.loads(transcript.entries[0][1])
    assert card["content"] == original["content"]
    assert card["state"] == "delivered"
    assert card["direction"] == "sent" and card["peer_id"] == "beta"


def test_early_and_out_of_order_receipts_and_session_reset():
    transcript = TuiTranscript()
    transcript.on_chat_message("chat_message_state", message(state="delivered", updated=3))
    assert transcript.entries == []  # Receipts alone never invent a sent/received message.
    transcript.on_chat_message("chat_message_received", message(state="processing", updated=2))
    transcript.on_chat_message("chat_message_state", message(state="queued", updated=1))
    card = json.loads(transcript.entries[0][1])
    assert (card["direction"], card["peer_id"], card["state"]) == ("received", "alpha", "delivered")
    assert _has_conversation(transcript.entries)
    for reset in (lambda: transcript.clear(), lambda: transcript.load_history([])):
        reset()
        transcript.on_chat_message("chat_message_received", message(state="processing", updated=4))
        assert len(transcript.entries) == 1
        assert json.loads(transcript.entries[0][1])["state"] == "processing"


def test_compact_send_has_one_message_block_and_failed_sends_remain_visible():
    transcript = TuiTranscript()
    surface = TuiSurface(transcript)
    surface.on_chat_message("chat_message_sent", message())
    surface.on_tool_end(ToolEndEvent("send", "send_chat_message", "done", 1))
    assert len(transcript.entries) == 1
    surface.on_tool_end(
        ToolEndEvent(
            "refused", "send_chat_message", "failed", 1, meta={"error": "Chats are not linked"}
        )
    )
    assert transcript.entries[-1][0] == "error"
    assert "Chats are not linked" in transcript.entries[-1][1]


def test_receipt_keeps_its_message_after_split_assistant_blocks_are_reconciled():
    transcript = TuiTranscript()
    transcript.stream_assistant("First ")
    transcript.append("trace", "tool boundary")
    transcript.stream_assistant("second ")
    transcript.on_chat_message("chat_message_sent", message())
    transcript.stream_assistant("third")
    transcript.finish_assistant("First second third")
    transcript.on_chat_message("chat_message_state", message(state="delivered", updated=2))
    cards = [json.loads(text) for role, text in transcript.entries if role == "peer_message"]
    assert len(cards) == 1 and cards[0]["state"] == "delivered"
    assert transcript.entries[0] == ("assistant", "First second third")


def test_peer_prose_wraps_between_words_and_keeps_long_tokens_visible():
    text = "Please reply with your marker and number.\n" + "界" * 50
    rows = peer_message_rows(
        json.dumps(message(direction="sent", peer_id="beta", content=text)), 27
    )
    lines = ["".join(text for _, text in row).removeprefix("│ ") for row in rows[1:]]
    assert lines[0].rstrip() == "Please reply with your"
    assert lines[1] == "marker and number."
    assert "".join(lines[2:]) == "界" * 50


@pytest.mark.parametrize("width", [8, 32, 80, 120])
def test_literal_multilingual_messages_wrap_by_cells_and_sanitize_terminal_codes(width):
    data = message(
        direction="sent",
        peer_id="beta",
        content="界界界\n  Καλημέρα [bold] **hi**\n\x1b[31munsafe\x1b[0m\x07",
    )
    rows = peer_message_rows(json.dumps(data), width)
    lines = ["".join(text for _, text in row) for row in rows]
    assert all(get_cwidth(line) <= width for line in lines)
    assert "\x1b" not in "\n".join(lines) and "\x07" not in "\n".join(lines)
    body = "".join(line.removeprefix("│ ") for line in lines[1:])
    if width >= 32:
        assert "[bold] **hi**" in body and "  Καλημέρα" in body
    assert _strip_transcript_copy_chrome("│ literal text", role="peer_message") == "literal text"


@pytest.mark.parametrize("columns", [40, 80, 120])
def test_actual_tui_renders_message_and_receipt_without_user_turn(monkeypatch, tmp_path, columns):
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    module = importlib.import_module("alysis_code.cli_impl.tui.app")
    original = module.Application
    surfaces, errors = [], []

    class Output(DummyOutput):
        def get_size(self):
            return Size(rows=40, columns=columns)

    class Session:
        def __init__(self, surface):
            surfaces.append(surface)

    def screen(app):
        app._redraw()
        return "\n".join(
            "".join(cell.char for _, cell in sorted(row.items()))
            for _, row in sorted(app.renderer._last_screen.data_buffer.items())
        )

    with create_pipe_input() as pipe:

        class Application(original):
            def run(self, *, pre_run):
                async def exercise():
                    try:
                        surface = surfaces[0]
                        surface.on_chat_message(
                            "chat_message_received", message(content="Original peer text")
                        )
                        surface.set_trace_level("off")
                        before = screen(self)
                        assert "Received from alpha" in before
                        assert "│ Original peer text" in before
                        assert "Queued" in before
                        surface.on_chat_message(
                            "chat_message_state", message(state="delivered", updated=2)
                        )
                        after = screen(self)
                        assert "Delivered" in after and "Queued" not in after
                        assert after.count("Original peer text") == 1
                        assert not any(role == "user" for role, _ in surface._t.entries)
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
        )
    if errors:
        raise errors[0]
