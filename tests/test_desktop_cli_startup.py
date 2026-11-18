from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from alysis_code.cli_impl.commands import _entry, ide_bridge
from alysis_code.cli_impl.commands.root import app as full_app
from alysis_code.cli_impl.desktop_entry import create_desktop_app


@pytest.mark.parametrize("args", [["--version"], ["capabilities"], ["ide-bridge", "health"]])
def test_desktop_probes_match_the_full_cli(args):
    runner = CliRunner()
    expected = runner.invoke(full_app, args)
    result = runner.invoke(create_desktop_app(), args)
    assert expected.exit_code == result.exit_code == 0
    assert result.output == expected.output


def test_stdio_uses_the_same_role_parser_and_build_check(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ide_bridge, "run_stdio_bridge", lambda *, role="runtime": calls.append(role) or 0
    )
    runner = CliRunner()
    app = create_desktop_app()
    for tail, role in [([], "runtime"), (["--role", "plugin-projection"], "plugin_projection")]:
        assert runner.invoke(app, ["ide-bridge", "--stdio", *tail]).exit_code == 0
        assert calls[-1] == role
    assert runner.invoke(app, ["ide-bridge", "--stdio", "--role", "unknown"]).exit_code == 2
    monkeypatch.setattr(
        _entry,
        "decide_clean_build",
        lambda **_: SimpleNamespace(allowed=False, message="fixture refusal"),
    )
    refused = runner.invoke(app, ["ide-bridge", "--stdio"])
    assert refused.exit_code == 2
    assert "fixture refusal" in refused.output
    assert calls == ["runtime", "plugin_projection"]


def test_installed_cli_route_and_catalog_metadata_skip_terminal_startup(tmp_path):
    script = """
import json, sys
sys.argv[1:] = ["capabilities"]
from alysis_code.cli import app
from alysis_code.cli_impl.chat_slash_catalog import get_chat_specs
try:
    app()
finally:
    print(json.dumps({"root": "alysis_code.cli_impl.commands.root" in sys.modules,
                      "completer": "prompt_toolkit" in sys.modules,
                      "command": get_chat_specs()[0].name}), file=sys.stderr)
"""
    src = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(src)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)
    assert json.loads(result.stderr.strip().splitlines()[-1]) == {
        "root": False,
        "completer": False,
        "command": "help",
    }


def test_capability_command_preview_matches_live_chat_catalog():
    from alysis_code.capabilities import build_capabilities_payload
    from alysis_code.ide.management_protocol import handle_management_method

    preview = build_capabilities_payload()["chat_commands"]
    live = handle_management_method("commands.list", {"mode": "chat"}, request_id="catalog")
    assert preview == live["commands"]
    assert any(row["name"] == "compact" for row in preview)
