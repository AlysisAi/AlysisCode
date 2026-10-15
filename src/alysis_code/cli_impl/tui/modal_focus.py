"""Modal focus traversal independent of the previous renderer frame."""

from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.containers import Container
from prompt_toolkit.layout.layout import walk


def focus_modal(layout: Layout, container: Container, direction: int = 1) -> None:
    """Cycle current focusable controls, excluding hidden conditional branches.

    Call on the UI thread. Unlike Layout.focus_next(), this also works immediately
    after mounting a modal or revealing confirmation controls, before a repaint.
    The caller supplies its modal root so focus cannot escape into the composer.
    """
    windows = [
        child
        for child in walk(container, skip_hidden=True)
        if isinstance(child, Window) and child.content.is_focusable()
    ]
    if not windows:
        return
    try:
        index = windows.index(layout.current_window)
    except ValueError:
        index = -1 if direction > 0 else 0
    layout.focus(windows[(index + direction) % len(windows)])
