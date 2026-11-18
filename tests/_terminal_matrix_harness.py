from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console

from alysis_code.cli_impl.commands import welcome as welcome_mod
from alysis_code.surface import theme as theme_mod

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "terminal_matrix"
MATRIX_ROOT = Path("/tmp/alysis-matrix")
WORKSPACE = Path.home() / "Desktop" / "alysis"
MODEL = "test-model"
VERSION = "0.1.4"

CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)")

ENV_KEYS = {
    "CI",
    "COLORTERM",
    "COLORFGBG",
    "NO_COLOR",
    "OWL_FALLBACK_THEME",
    "OWL_THEME",
    "PYTEST_CURRENT_TEST",
    "ALYSIS_FALLBACK_THEME",
    "ALYSIS_ENABLE_OSC11",
    "ALYSIS_THEME",
    "ALYSIS_THEME_DEBUG",
    "ALYSIS_WINDOWS_TERMINAL_SETTINGS",
    "TERM",
    "TERM_PROGRAM",
    "WT_PROFILE_ID",
    "WT_SESSION",
    "WSL_DISTRO_NAME",
    "WSL_INTEROP",
}


class MatrixStream(io.StringIO):
    def __init__(self, *, isatty: bool) -> None:
        super().__init__()
        self._isatty = isatty

    def isatty(self) -> bool:
        return self._isatty


@dataclass
class Simulation:
    scenario: dict[str, Any]
    stream: MatrixStream
    stderr: io.StringIO
    osc11_call_count: int = 0

    def debug_text(self) -> str:
        return self.stderr.getvalue()


_CURRENT_SIMULATION: Simulation | None = None


def scenario_ids() -> list[int]:
    return list(range(1, 13))


def load_scenario(scenario_id: int) -> dict[str, Any]:
    path = FIXTURE_DIR / f"scenario_{scenario_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _write_windows_terminal_settings(scenario: dict[str, Any]) -> Path | None:
    settings = scenario.get("windows_terminal_settings")
    if not isinstance(settings, dict):
        return None

    scheme_name = str(settings.get("scheme_name") or "Alysis Code Matrix")
    background = str(settings.get("background") or "#0c0c0c")
    profile_guid = "{11111111-1111-1111-1111-111111111111}"
    payload = {
        "defaultProfile": profile_guid,
        "profiles": {
            "defaults": {"colorScheme": scheme_name},
            "list": [
                {
                    "guid": profile_guid,
                    "name": "Alysis Code Matrix",
                    "colorScheme": scheme_name,
                }
            ],
        },
        "schemes": [{"name": scheme_name, "background": background}],
    }
    path = Path(f"/tmp/alysis-matrix-settings-{scenario['id']}") / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@contextlib.contextmanager
def simulate(scenario: dict[str, Any]) -> Iterator[Simulation]:
    global _CURRENT_SIMULATION

    previous_env = {key: os.environ.get(key) for key in ENV_KEYS}
    for key in ENV_KEYS:
        os.environ.pop(key, None)
    os.environ.update({str(key): str(value) for key, value in scenario.get("env", {}).items()})
    os.environ["ALYSIS_THEME_DEBUG"] = "1"
    os.environ["PYTEST_CURRENT_TEST"] = "terminal-matrix"

    settings_path = _write_windows_terminal_settings(scenario)
    if settings_path is not None:
        os.environ["ALYSIS_WINDOWS_TERMINAL_SETTINGS"] = os.fspath(settings_path)

    stream = MatrixStream(isatty=bool(scenario.get("isatty", True)))
    stderr = io.StringIO()
    simulation = Simulation(scenario=scenario, stream=stream, stderr=stderr)

    old_stdout = sys.stdout
    old_stderr = sys.stderr
    old_is_wsl = theme_mod._is_wsl
    old_osc11 = theme_mod._theme_from_osc11
    old_terminal_size = welcome_mod.shutil.get_terminal_size

    def fake_is_wsl() -> bool:
        return bool(scenario.get("is_wsl", False))

    def fake_osc11(_stream: Any | None) -> str | None:
        simulation.osc11_call_count += 1
        if fake_is_wsl():
            theme_mod._theme_debug("OSC11=skipped (WSL)")
            return None
        result = scenario.get("osc11_result")
        theme_mod._theme_debug(f"OSC11=simulated -> {result or 'none'}")
        return result if result in {"light", "dark"} else None

    def fake_terminal_size(fallback: tuple[int, int] = (80, 20)) -> os.terminal_size:
        return os.terminal_size((120, 30))

    try:
        sys.stdout = stream
        sys.stderr = stderr
        theme_mod._is_wsl = fake_is_wsl
        theme_mod._theme_from_osc11 = fake_osc11
        welcome_mod.shutil.get_terminal_size = fake_terminal_size
        _CURRENT_SIMULATION = simulation
        yield simulation
    finally:
        _CURRENT_SIMULATION = None
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        theme_mod._is_wsl = old_is_wsl
        theme_mod._theme_from_osc11 = old_osc11
        welcome_mod.shutil.get_terminal_size = old_terminal_size
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def render(scenario: dict[str, Any]) -> str:
    simulation = _CURRENT_SIMULATION
    if simulation is None:
        raise RuntimeError("render() must be called inside simulate(scenario)")
    console = Console(
        file=simulation.stream,
        force_terminal=bool(scenario.get("isatty", True)),
        width=120,
        color_system="truecolor" if scenario.get("color_capability") == "truecolor" else None,
    )
    return welcome_mod.printWelcome(
        console=console,
        workspace=WORKSPACE,
        model=MODEL,
        version=VERSION,
    )


def to_visible(ansi: str) -> str:
    return CSI_RE.sub("", OSC_RE.sub("", ansi))


def detect_theme_for_scenario(scenario: dict[str, Any]) -> str:
    stream = MatrixStream(isatty=bool(scenario.get("isatty", True)))
    return theme_mod.detect_terminal_theme(stream)


def panel_present(ansi: str) -> bool:
    return "\x1b[48;2;255;255;255m" in ansi or "\x1b[48;5;231m" in ansi


def assert_consistency(scenario: dict[str, Any], ansi: str) -> str:
    actual_theme = detect_theme_for_scenario(scenario)
    assert actual_theme == scenario["expected_theme"]

    assert panel_present(ansi) is bool(scenario["expects_white_panel"])
    for forbidden in scenario.get("forbidden_ansi", []):
        assert forbidden not in ansi
    assert "\x1b]11;" not in ansi
    assert "0c/0c0c" not in ansi

    if scenario.get("expects_neutral_palette"):
        assert "\x1b[97mThe autonomous coding agent" not in ansi
        assert "\x1b[30mThe autonomous coding agent" not in ansi
        assert not panel_present(ansi)

    visible = to_visible(ansi)
    for anchor in ("Alysis Code", "workspace", "model", "version", "/resume", "/status", "/help"):
        assert anchor in visible
    return actual_theme


def matrix_output_dir(scenario_id: int) -> Path:
    return Path(f"/tmp/alysis-matrix-{scenario_id}")


def write_outputs(
    scenario: dict[str, Any],
    *,
    ansi: str,
    visible: str,
    debug: str,
    actual_theme: str,
) -> None:
    output_dir = matrix_output_dir(int(scenario["id"]))
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "ansi.txt").write_text(ansi, encoding="utf-8")
    (output_dir / "visible.txt").write_text(visible, encoding="utf-8")
    (output_dir / "debug.txt").write_text(debug, encoding="utf-8")
    snippet = "\n".join(line for line in visible.splitlines() if line.strip())[:800]
    snippet_lines = "\n".join(snippet.splitlines()[:5])
    panel = "yes" if panel_present(ansi) else "no"
    (output_dir / "SUMMARY.md").write_text(
        "\n".join(
            [
                f"# Scenario {scenario['id']}: {scenario['name']}",
                "",
                f"- expected theme: {scenario['expected_theme']}",
                f"- actual theme: {actual_theme}",
                f"- white panel present: {panel}",
                "",
                "```text",
                snippet_lines,
                "```",
                "",
            ]
        ),
        encoding="utf-8",
    )


def render_scenario(scenario_id: int) -> str:
    scenario = load_scenario(scenario_id)
    with simulate(scenario) as simulation:
        ansi = render(scenario)
        actual_theme = assert_consistency(scenario, ansi)
        write_outputs(
            scenario,
            ansi=ansi,
            visible=to_visible(ansi),
            debug=simulation.debug_text(),
            actual_theme=actual_theme,
        )
        return ansi


def clean_matrix_outputs() -> None:
    MATRIX_ROOT.mkdir(parents=True, exist_ok=True)
    for scenario_id in scenario_ids():
        shutil.rmtree(matrix_output_dir(scenario_id), ignore_errors=True)
