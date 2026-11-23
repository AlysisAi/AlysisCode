"""The free owl scenario tier: every scenario in scripts/qa/owls_scenarios/scenarios replays its
turns through the real owls runtime with a scripted model and must pass its checks.

No model is called and nothing may reach the network: every run here happens with sockets
refused, so a scenario that would reach outside fails instead of passing by luck.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import os
import socket

import pytest

from scripts.qa.owls_scenarios import scenario as scenarios
from scripts.qa.owls_scenarios.run import report, run_scenario

PATHS = scenarios.all_paths()


def _local(address) -> bool:
    """A Unix socket or the loopback interface (a socket pair on Windows connects there)."""
    if not isinstance(address, tuple):
        return True
    host = address[0]
    if host in (None, "localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture
def offline(monkeypatch):
    """Any attempt to reach another computer fails the test."""
    attempts = []
    connect, create, resolve = socket.socket.connect, socket.create_connection, socket.getaddrinfo

    def guarded(original, address_of):
        def call(*args, **kwargs):
            address = address_of(args)
            if _local(address):
                return original(*args, **kwargs)
            attempts.append(address)
            raise OSError("Scenario runs never reach the network.")

        return call

    monkeypatch.setattr(socket.socket, "connect", guarded(connect, lambda a: a[1]))
    monkeypatch.setattr(socket, "create_connection", guarded(create, lambda a: a[0]))
    monkeypatch.setattr(socket, "getaddrinfo", guarded(resolve, lambda a: (a[0], None)))
    yield attempts
    assert attempts == [], "A scenario tried to reach the network."


@pytest.mark.parametrize("path", PATHS, ids=[path.stem for path in PATHS])
def test_every_scenario_passes(path, offline):
    scenario = scenarios.load(path)
    assert scenarios.problems(scenario) == []
    result = run_scenario(scenario)
    assert result.passed, report([result])


def test_the_set_covers_languages_capabilities_and_each_pinned_failure():
    loaded = [scenarios.load(path) for path in PATHS]
    ids = {s["id"] for s in loaded}
    assert len(ids) == len(loaded) >= 8
    assert all(path.stem == s["id"] for path, s in zip(PATHS, loaded, strict=True))
    assert sum(1 for s in loaded if s.get("language") != "en") >= 2
    # At least one scenario's model declares a capability unlike the default model's.
    assert any("image_input" in (s.get("model") or {}) for s in loaded)
    assert all(s.get("pins") and s.get("checks") for s in loaded)


def test_a_script_that_does_not_fit_the_run_is_reported_not_passed(offline):
    scenario = scenarios.load(scenarios.SCENARIOS / "cut-off-call-runs-nothing-el.json")
    short = copy.deepcopy(scenario)
    short["script"]["owl"] = short["script"]["owl"][:1]  # the turn needs a second reply
    result = run_scenario(short)
    assert not result.passed
    assert any("owl call 2 has no scripted reply" in p for p in result.problems)
    assert "FAIL" in report([result])


def test_a_script_longer_than_the_run_is_reported_too(offline):
    scenario = scenarios.load(scenarios.SCENARIOS / "quiet-routine-run-zh.json")
    longer = copy.deepcopy(scenario)
    longer["script"]["owl"].append({"say": "Αυτό δεν το ζητά κανείς."})
    result = run_scenario(longer)
    assert not result.passed
    assert result.problems == ["Script: 1 scripted owl reply was never used."]


def test_a_check_that_does_not_hold_fails_the_scenario(offline):
    scenario = scenarios.load(scenarios.SCENARIOS / "steer-during-tool-batch-el.json")
    wrong = copy.deepcopy(scenario)
    wrong["checks"].append(
        {"kind": "tool_not_run", "name": "save_file", "why": "deliberately wrong"}
    )
    result = run_scenario(wrong)
    assert not result.passed and result.problems == []
    [failed] = [v for v in result.verdicts if not v["passed"]]
    assert "save_file ran 2 time(s)" in failed["detail"]
    assert "deliberately wrong" in report([result])


def test_an_invalid_scenario_is_refused_before_it_runs():
    result = run_scenario({"id": "broken", "steps": [{"do": "fly"}], "checks": [{"kind": "x"}]})
    assert not result.passed and result.evidence == {}
    assert any("unknown step 'fly'" in p for p in result.problems)
    assert any("Unknown check kind 'x'" in p for p in result.problems)
    assert any("'pins'" in p for p in result.problems)


def test_the_command_line_prints_a_report_and_writes_evidence(tmp_path, capsys, offline):
    from scripts.qa.owls_scenarios.run import main

    code = main(["--scenario", "quiet-routine-run-zh", "--evidence", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("# Owl scenarios") and "| `quiet-routine-run-zh` | pass |" in out
    saved = json.loads((tmp_path / "quiet-routine-run-zh.json").read_text(encoding="utf-8"))
    assert saved["id"] == "quiet-routine-run-zh" and saved["turns"]
    with pytest.raises(SystemExit, match="Unknown scenario"):
        main(["--scenario", "no-such-scenario"])


def test_a_declared_context_window_reaches_the_runtime_and_the_shell_is_left_alone(
    monkeypatch, offline
):
    monkeypatch.setenv("ALYSIS_CONTEXT_WINDOW", "999999")
    scenario = scenarios.load(scenarios.SCENARIOS / "quiet-routine-run-zh.json")
    small = copy.deepcopy(scenario)
    small["model"] = {"id": "scripted-small-window", "context_window": 64_000}
    result = run_scenario(small)
    offered = [
        e["data"]
        for turn in result.evidence["turns"]
        for e in turn["events"]
        if e["kind"] == "tools_offered"
    ]
    # Every turn read the declared window, whatever the shell had set, and a small window
    # puts most tools behind look-up.
    assert offered and all(o["context_window"] == 64_000 for o in offered)
    assert all(o["mode"] != "full" for o in offered)
    assert os.environ["ALYSIS_CONTEXT_WINDOW"] == "999999"
