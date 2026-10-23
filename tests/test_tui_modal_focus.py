"""Fast modal keyboard navigation before the first paint, with real VT decoding."""

import asyncio

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Size
from prompt_toolkit.filters import Condition
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.layout import walk
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.widgets import Button
from test_tui_integration_modal import make_modal

from alysis_code.cli_impl.tui.modal_focus import focus_modal


class Output(DummyOutput):
    def __init__(self, columns):
        self.columns = columns

    def get_size(self):
        return Size(rows=24, columns=self.columns)


def make(kind, tmp_path):
    return make_modal(tmp_path)[0]


@pytest.mark.parametrize("kind", ["integration"])
@pytest.mark.parametrize("columns", [40, 80])
def test_fast_tab_before_first_paint_respects_modal_controls(kind, columns, tmp_path):
    async def exercise():
        modal = make(kind, tmp_path)
        with create_pipe_input() as pipe:
            app = Application(
                layout=Layout(modal.container, focused_element=modal.focus_target),
                input=pipe,
                output=Output(columns),
                full_screen=True,
            )
            app.ttimeoutlen = 0.01
            app.timeoutlen = 0.01
            visited = []

            def before_paint():
                app.layout.update_parents_relations()
                assert app.renderer.last_rendered_screen is None
                visible = [
                    c
                    for c in walk(modal.container, skip_hidden=True)
                    if isinstance(c, Window) and c.content.is_focusable()
                ]
                start = visible.index(app.layout.current_window)
                parser = Vt100Parser(app.key_processor.feed)
                for offset in range(1, len(visible) + 1):
                    parser.feed("\t")
                    app.key_processor.process_keys()
                    assert app.layout.current_window is visible[(start + offset) % len(visible)]
                    visited.append(app.layout.current_window)
                parser.feed("\x1b[Z")
                app.key_processor.process_keys()
                assert app.layout.current_window is visible[(start - 1) % len(visible)]

            task = asyncio.create_task(app.run_async(pre_run=before_paint))
            try:
                await asyncio.sleep(0.1)
                if task.done():
                    await task
                assert len(set(visited)) > 1
                assert app.renderer.last_rendered_screen is not None
                pipe.send_text("\x1b")
                await asyncio.sleep(0.1)
                assert not modal.is_open
            finally:
                if not task.done():
                    app.exit()
                await task
                modal.close()

    asyncio.run(exercise())


def test_focus_helper_rechecks_hidden_confirmation_without_render():
    first, confirm, last = Button("First"), Button("Confirm"), Button("Last")
    shown = False
    root = HSplit([first, ConditionalContainer(confirm, Condition(lambda: shown)), last])
    layout = Layout(root, focused_element=first)
    focus_modal(layout, root)
    assert layout.has_focus(last)
    shown = True
    focus_modal(layout, root, -1)
    assert layout.has_focus(confirm)
    shown = False
    focus_modal(layout, root)
    assert layout.has_focus(first)
