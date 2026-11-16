from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.console import Group
from rich.table import Table
from rich.text import Text

from alysis_code.cli_impl.commands import chat_terminal as chat_terminal_mod
from alysis_code.surface import theme as theme_mod
from alysis_code.surface.console import make_console
from alysis_code.surface.theme import (
    detect_terminal_theme,
    detect_terminal_theme_if_available,
)


class _Pipe:
    def isatty(self) -> bool:
        return False


def _style_strings(renderable: Any) -> list[str]:
    styles: list[str] = []
    seen: set[int] = set()

    def visit(value: Any) -> None:
        if value is None:
            return
        obj_id = id(value)
        if obj_id in seen:
            return
        seen.add(obj_id)

        if isinstance(value, Text):
            if value.style:
                styles.append(str(value.style))
            for span in value.spans:
                if span.style:
                    styles.append(str(span.style))
            return

        if isinstance(value, Table):
            for column in value.columns:
                if column.style:
                    styles.append(str(column.style))
                if column.header_style:
                    styles.append(str(column.header_style))
                for cell in column._cells:
                    visit(cell)
            return

        if isinstance(value, Group):
            for child in value.renderables:
                visit(child)
            return

        children = getattr(value, "renderables", None)
        if children is not None:
            for child in children:
                visit(child)

    visit(renderable)
    return styles


def test_no_color_env_disables_color(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")

    console = make_console()

    assert console.no_color is True


def test_wt_session_enables_truecolor(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("WT_SESSION", "test-session")

    console = make_console()

    assert console.color_system == "truecolor"


def test_detect_terminal_theme_respects_alysis_theme_override(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_THEME", "light")
    monkeypatch.setenv("OWL_THEME", "dark")

    assert detect_terminal_theme(stream=_Pipe()) == "light"


def test_unknown_terminal_theme_uses_neutral_fallback_without_guessing(monkeypatch) -> None:
    for env_name in (
        "ALYSIS_THEME",
        "OWL_THEME",
        "COLORFGBG",
        "WT_SESSION",
        "TERM_PROGRAM",
        "ALYSIS_WINDOWS_TERMINAL_SETTINGS",
        "ALYSIS_FALLBACK_THEME",
        "OWL_FALLBACK_THEME",
    ):
        monkeypatch.delenv(env_name, raising=False)

    monkeypatch.setattr(theme_mod, "_theme_from_osc11", lambda _stream: None)
    monkeypatch.setattr(theme_mod, "_windows_terminal_settings_paths", lambda: [])

    assert detect_terminal_theme(stream=_Pipe()) == "neutral"
    assert detect_terminal_theme_if_available(stream=_Pipe()) is None


def test_osc11_skipped_on_wsl_to_prevent_stdin_leak(monkeypatch) -> None:
    monkeypatch.setattr(theme_mod, "_is_wsl", lambda: True)

    opens: list[str] = []
    real_open = theme_mod.os.open

    def spy_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
        if path == "/dev/tty":
            opens.append(path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(theme_mod.os, "open", spy_open)

    class _TTY:
        def isatty(self) -> bool:
            return True

    assert theme_mod._theme_from_osc11(_TTY()) is None
    assert opens == []


def test_osc11_disabled_by_default(monkeypatch) -> None:
    for env_name in (
        "ALYSIS_THEME",
        "OWL_THEME",
        "COLORFGBG",
        "WT_SESSION",
        "TERM_PROGRAM",
        "ALYSIS_WINDOWS_TERMINAL_SETTINGS",
        "ALYSIS_FALLBACK_THEME",
        "OWL_FALLBACK_THEME",
        "ALYSIS_ENABLE_OSC11",
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setattr(
        theme_mod,
        "_theme_from_osc11",
        lambda _stream: (_ for _ in ()).throw(AssertionError("unexpected OSC 11 query")),
    )
    monkeypatch.setattr(theme_mod, "_windows_terminal_settings_paths", lambda: [])

    assert detect_terminal_theme(stream=_Pipe()) == "neutral"


def test_detect_terminal_theme_uses_wsl_settings_without_wt_session(tmp_path, monkeypatch) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(
        """
        {
          "profiles": {
            "defaults": {
              "colorScheme": "Alysis Code Light"
            },
            "list": []
          },
          "schemes": [
            {
              "name": "Alysis Code Light",
              "background": "#ffffff"
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    for env_name in (
        "ALYSIS_THEME",
        "OWL_THEME",
        "COLORFGBG",
        "WT_SESSION",
        "TERM_PROGRAM",
        "ALYSIS_WINDOWS_TERMINAL_SETTINGS",
        "ALYSIS_FALLBACK_THEME",
        "OWL_FALLBACK_THEME",
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setattr(theme_mod, "_is_wsl", lambda: True)
    monkeypatch.setattr(theme_mod, "_windows_terminal_settings_paths", lambda: [settings])

    assert detect_terminal_theme(stream=_Pipe()) == "light"


def test_windows_terminal_settings_paths_include_wsl_host_mount(monkeypatch) -> None:
    c_settings = Path(
        "/mnt/c/Users/Alice/AppData/Local/Packages/"
        "Microsoft.WindowsTerminal_8wekyb3d8bbwe/LocalState/settings.json"
    )
    host_settings = Path(
        "/mnt/host/c/Users/Alice/AppData/Local/Packages/"
        "Microsoft.WindowsTerminal_8wekyb3d8bbwe/LocalState/settings.json"
    )

    def fake_glob(path: Path, pattern: str) -> list[Path]:
        if path == Path("/mnt") and pattern == "[a-z]":
            return [Path("/mnt/c")]
        if path == Path("/mnt/host") and pattern == "[a-z]":
            return [Path("/mnt/host/c")]
        if pattern == "Microsoft.WindowsTerminal*_8wekyb3d8bbwe/LocalState/settings.json":
            if path == Path("/mnt/c/Users/Alice/AppData/Local/Packages"):
                return [c_settings]
            if path == Path("/mnt/host/c/Users/Alice/AppData/Local/Packages"):
                return [host_settings]
        return []

    def fake_is_dir(path: Path) -> bool:
        return path in {Path("/mnt/c/Users"), Path("/mnt/host/c/Users")}

    def fake_iterdir(path: Path) -> list[Path]:
        if path == Path("/mnt/c/Users"):
            return [Path("/mnt/c/Users/Alice")]
        if path == Path("/mnt/host/c/Users"):
            return [Path("/mnt/host/c/Users/Alice")]
        return []

    monkeypatch.setattr(theme_mod, "_safe_sorted_glob", fake_glob)
    monkeypatch.setattr(theme_mod, "_safe_is_dir", fake_is_dir)
    monkeypatch.setattr(theme_mod, "_safe_sorted_iterdir", fake_iterdir)

    paths = theme_mod._windows_terminal_settings_paths()

    assert c_settings in paths
    assert host_settings in paths


def test_detect_terminal_theme_does_not_lock_dark_under_wsl_only(monkeypatch) -> None:
    for env_name in (
        "ALYSIS_THEME",
        "OWL_THEME",
        "COLORFGBG",
        "WT_SESSION",
        "TERM_PROGRAM",
        "ALYSIS_WINDOWS_TERMINAL_SETTINGS",
        "ALYSIS_FALLBACK_THEME",
        "OWL_FALLBACK_THEME",
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setattr(theme_mod, "_is_wsl", lambda: True)
    monkeypatch.setattr(theme_mod, "_windows_terminal_settings_paths", lambda: [])

    assert detect_terminal_theme(stream=_Pipe()) == "neutral"


def test_detect_terminal_theme_ignores_wsl_settings_scan_errors(monkeypatch) -> None:
    for env_name in (
        "ALYSIS_THEME",
        "OWL_THEME",
        "COLORFGBG",
        "WT_SESSION",
        "TERM_PROGRAM",
        "ALYSIS_WINDOWS_TERMINAL_SETTINGS",
        "ALYSIS_FALLBACK_THEME",
        "OWL_FALLBACK_THEME",
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setattr(theme_mod, "_is_wsl", lambda: True)
    monkeypatch.setattr(
        theme_mod,
        "_windows_terminal_settings_paths",
        lambda: (_ for _ in ()).throw(OSError("inaccessible /mnt/c users tree")),
    )

    assert detect_terminal_theme(stream=_Pipe()) == "neutral"


def test_apple_terminal_does_not_infer_profile_from_system_appearance(monkeypatch) -> None:
    for env_name in (
        "ALYSIS_THEME",
        "OWL_THEME",
        "COLORFGBG",
        "WT_SESSION",
        "ALYSIS_WINDOWS_TERMINAL_SETTINGS",
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    monkeypatch.setattr(theme_mod, "_theme_from_osc11", lambda _stream: None)
    monkeypatch.setattr(theme_mod, "_windows_terminal_settings_paths", lambda: [])

    assert detect_terminal_theme_if_available(stream=_Pipe()) is None
    assert detect_terminal_theme(stream=_Pipe()) == "neutral"


def test_theme_debug_emits_detection_steps(monkeypatch, capsys) -> None:
    for env_name in (
        "ALYSIS_THEME",
        "OWL_THEME",
        "WT_SESSION",
        "TERM_PROGRAM",
        "ALYSIS_WINDOWS_TERMINAL_SETTINGS",
        "ALYSIS_FALLBACK_THEME",
        "OWL_FALLBACK_THEME",
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("COLORFGBG", "0;15")
    monkeypatch.setenv("ALYSIS_THEME_DEBUG", "1")

    assert detect_terminal_theme(stream=_Pipe()) == "light"

    err = capsys.readouterr().err
    assert "[alysis-theme] env=ALYSIS_THEME -> none" in err
    assert "[alysis-theme] COLORFGBG=0;15 -> light" in err
    assert "[alysis-theme] result=light" in err


def test_no_color_theme_detection_skips_terminal_query(monkeypatch) -> None:
    for env_name in (
        "ALYSIS_THEME",
        "OWL_THEME",
        "COLORFGBG",
        "TERM_PROGRAM",
        "OWL_FALLBACK_THEME",
    ):
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("ALYSIS_FALLBACK_THEME", "light")
    monkeypatch.setattr(
        theme_mod,
        "_theme_from_osc11",
        lambda _stream: (_ for _ in ()).throw(AssertionError("unexpected OSC 11 query")),
    )
    monkeypatch.setattr(theme_mod, "_windows_terminal_settings_paths", lambda: [])

    assert detect_terminal_theme(stream=_Pipe()) == "light"


def test_no_hardcoded_white_in_picker(monkeypatch) -> None:
    monkeypatch.setattr(chat_terminal_mod, "_is_narrow_terminal", lambda: False, raising=False)
    panel = chat_terminal_mod._selectable_options_panel(
        title="Guarded Workspace",
        rows=[
            ("current", "1) Current workspace", "Use the current repository."),
            ("narrow", "2) Narrower workspace", "Pick a safer child folder."),
        ],
        selected_value="current",
        interactive=True,
    )

    styles = _style_strings(panel)

    assert "white" not in styles
    assert "bold white" not in styles
