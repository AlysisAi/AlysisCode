from __future__ import annotations

import asyncio

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.selection import SelectionState

from alysis_code.cli_impl.tui.panel_text import PanelText


@pytest.mark.parametrize("columns,rows", [(50, 18), (80, 24)])
@pytest.mark.parametrize(
    "paragraph",
    [
        "Εξήγηση: οι αλλαγές παραμένουν μόνο για ανάγνωση. ",
        "日本語の説明と中文内容 with wide characters 界界. ",
        "Combining cafe\u0301 and re\u0301sume\u0301 with emoji \U0001f680. ",
    ],
)
def test_wrapped_unicode_keyboard_reaches_last_content(columns, rows, paragraph):
    class Output(DummyOutput):
        def get_size(self):
            return Size(rows=rows, columns=columns)

    async def run():
        original = "BEGIN " + paragraph * 90 + "\n\n" + paragraph * 40 + " FINAL-CONTENT"
        pane = PanelText(text=original)
        failures = []
        with create_pipe_input() as pipe:
            app = Application(
                layout=Layout(pane, pane), input=pipe, output=Output(), full_screen=True
            )

            def key(value):
                app.key_processor.feed(KeyPress(value))
                app.key_processor.process_keys()
                app._redraw()

            def visible():
                screen = app.renderer._last_screen
                return "\n".join(
                    "".join(screen.data_buffer[y][x].char for x in range(columns - 1))
                    for y in range(rows)
                )

            async def check():
                try:
                    app._redraw()
                    assert "BEGIN" in visible()
                    assert "FINAL-CONTENT" not in "".join(visible().split())
                    # /find uses a selected match; reading must not expand it.
                    for movement in (Keys.Down, Keys.Home, Keys.End, Keys.PageUp):
                        pane.buffer.cursor_position = 5
                        pane.buffer.selection_state = SelectionState(original_cursor_position=0)
                        key(movement)
                        assert pane.buffer.selection_state is None
                    key(Keys.Home)
                    # Each Down traverses visual rows even inside one logical line.
                    for _ in range(len(original)):
                        before = pane.buffer.cursor_position
                        if before == len(original):
                            break
                        key(Keys.Down)
                        assert pane.buffer.cursor_position > before
                    assert pane.buffer.cursor_position == len(original)
                    assert "FINAL-CONTENT" in "".join(visible().split())
                    key(Keys.Home)
                    assert pane.buffer.cursor_position == 0 and "BEGIN" in visible()
                    for _ in range(len(original)):
                        before = pane.buffer.cursor_position
                        if before == len(original):
                            break
                        key(Keys.PageDown)
                        assert pane.buffer.cursor_position > before
                    assert "FINAL-CONTENT" in "".join(visible().split())
                    key(Keys.PageUp)
                    assert pane.buffer.cursor_position < len(original)
                    key(Keys.Home)
                    key(Keys.End)
                    assert pane.buffer.cursor_position == len(original)
                    assert "FINAL-CONTENT" in "".join(visible().split())
                    key(Keys.Up)
                    assert pane.buffer.cursor_position < len(original)
                    app.key_processor.feed(KeyPress("x", "x"))
                    app.key_processor.process_keys()
                    assert pane.text == original
                except BaseException as exc:
                    failures.append(exc)
                finally:
                    app.exit()

            await app.run_async(pre_run=lambda: app.create_background_task(check()))
            if failures:
                raise failures[0]

    asyncio.run(run())
