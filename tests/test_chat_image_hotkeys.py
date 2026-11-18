from __future__ import annotations

import errno
import io
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rich.console import Console

from alysis_code import cli as cli_mod


def _binding_key_values(binding: Any) -> tuple[str, ...]:
    return tuple(str(getattr(key, "value", key)) for key in getattr(binding, "keys", ()))


def test_chat_prompt_ctrl_alt_v_pastes_clipboard_image(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, Any] = {}
    pending_images: list[str] = []
    pasted = tmp_path / "clipboard.png"

    class _PromptSessionStub:
        def __init__(self, **kwargs: Any) -> None:
            captured["key_bindings"] = kwargs["key_bindings"]
            self.app = SimpleNamespace(
                output=SimpleNamespace(responds_to_cpr=lambda: True),
                ttimeoutlen=0.5,
            )

    def fake_run_in_terminal(callback: Any) -> None:
        callback()

    def fake_paste_clipboard_image(*, root: Path, output_path: str | None = None) -> Path:
        assert root == tmp_path
        assert output_path is None
        return pasted

    import prompt_toolkit  # type: ignore[import-not-found]
    import prompt_toolkit.application  # type: ignore[import-not-found]

    monkeypatch.setattr(prompt_toolkit, "PromptSession", _PromptSessionStub)
    monkeypatch.setattr(prompt_toolkit.application, "run_in_terminal", fake_run_in_terminal)
    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(cli_mod, "paste_clipboard_image", fake_paste_clipboard_image)
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(tmp_path / "data"))

    buffer = io.StringIO()
    session = cli_mod._maybe_make_chat_prompt_session(
        console=Console(file=buffer, force_terminal=False),
        root=tmp_path,
        pending_images=pending_images,
    )

    assert session is not None
    key_bindings = captured["key_bindings"]
    bindings_by_keys = {
        _binding_key_values(binding): binding for binding in getattr(key_bindings, "bindings", [])
    }
    assert ("escape", "c-v") in bindings_by_keys
    assert ("c-b",) in bindings_by_keys

    bindings_by_keys[("escape", "c-v")].handler(SimpleNamespace())

    assert pending_images == [os.fspath(pasted)]
    output = buffer.getvalue()
    assert "Pasted clipboard image:" in output
    # Rich may fold a long temporary path in the middle of its filename at the
    # console boundary. Verify the emitted path independent of visual wrapping.
    assert pasted.name in "".join(output.splitlines())


def test_chat_prompt_session_skips_history_when_data_dir_is_read_only(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, Any] = {}
    data_dir = tmp_path / "data"

    class _PromptSessionStub:
        def __init__(self, **kwargs: Any) -> None:
            captured["kwargs"] = kwargs
            self.app = SimpleNamespace(
                output=SimpleNamespace(responds_to_cpr=lambda: True),
                ttimeoutlen=0.5,
            )

    import prompt_toolkit  # type: ignore[import-not-found]

    real_mkdir = Path.mkdir

    def fail_data_dir_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == data_dir:
            raise OSError(errno.EROFS, "Read-only file system", os.fspath(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(prompt_toolkit, "PromptSession", _PromptSessionStub)
    monkeypatch.setattr(cli_mod, "_is_non_interactive_terminal", lambda: False)
    monkeypatch.setattr(Path, "mkdir", fail_data_dir_mkdir)
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(data_dir))

    session = cli_mod._maybe_make_chat_prompt_session(
        console=Console(file=io.StringIO(), force_terminal=False),
        root=tmp_path,
        pending_images=[],
    )

    assert session is not None
    assert "history" not in captured["kwargs"]
