"""Real local executions keep blast-radius scope honest after temporary cleanup."""

from __future__ import annotations

import copy
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_batching_guidance_delivery import (
    _isolated_offline_environment as _isolated_offline_environment,
)
from test_inferred_verification_authority import _ScriptedClient

import alysis_code.agent.turn.core as turn_core
from alysis_code.agent import session as session_mod
from alysis_code.agent.blast_radius import RepoTestIndex, absent_agent_created_tests
from alysis_code.config import AppConfig
from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events
from alysis_code.surface.noop_surface import NoopSurface

_TEMPORARY = "tests/test_repro.py"
_REPRO = (
    "import unittest\nfrom app import total\n"
    "class Reproduction(unittest.TestCase):\n"
    "    def test_nonempty(self):\n"
    "        self.assertEqual(total([1, 2]), 3)\n"
)
_ORIGINAL_TEST = (
    "import unittest\nfrom app import total\n"
    "class Existing(unittest.TestCase):\n"
    "    def test_empty(self):\n"
    "        self.assertEqual(total([]), 0)\n"
)
_EXPANDED_TEST = _ORIGINAL_TEST + (
    "    def test_nonempty(self):\n        self.assertEqual(total([1, 2]), 3)\n"
)


def _call(name: str, call_id: str, **arguments: Any) -> LLMResponse:
    return LLMResponse(
        content="", tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)], raw={}
    )


def _exercise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    one_shot: bool,
    preexisting_deleted: bool = False,
    leave_real_failure: bool = False,
    check_cleanup_boundary: bool = False,
    recreate_before_final_cleanup: bool = False,
) -> dict[str, Any]:
    for prefix in ("ALYSIS", "ALYSIS"):
        for suffix in ("VERIFY_SANDBOX_MODE", "SHELL_SANDBOX_MODE"):
            monkeypatch.delenv(f"{prefix}_{suffix}", raising=False)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests/__init__.py").write_text("")
    (root / "app.py").write_text("def total(values):\n    return 0\n")
    (root / "tests/test_app.py").write_text(_ORIGINAL_TEST)
    if preexisting_deleted:
        (root / _TEMPORARY).write_text(_REPRO)

    executable = (
        subprocess.list2cmdline([sys.executable])
        if os.name == "nt"
        else shlex.quote(sys.executable)
    )
    focused = [
        f"{executable} -m unittest tests.test_repro.Reproduction.test_nonempty -v",
        f"{executable} -m unittest tests.test_app -v",
    ]
    full = f"{executable} -m unittest discover -s tests -v"
    responses = []
    if not preexisting_deleted:
        responses.append(_call("fs_write", "create-repro", path=_TEMPORARY, content=_REPRO))
    responses.extend(
        [
            _call("verify_run", "baseline", commands=focused),
            _call(
                "fs_write",
                "implementation",
                path="app.py",
                content=(
                    "def total(values):\n    return len(values)\n"
                    if leave_real_failure
                    else "def total(values):\n    return sum(values)\n"
                ),
            ),
            _call("fs_write", "retained-tests", path="tests/test_app.py", content=_EXPANDED_TEST),
            _call("verify_run", "targeted-after-edit", commands=focused),
        ]
    )
    final = LLMResponse(
        content="Updated app.py, retained the regression in tests/test_app.py, and ran the checks.",
        tool_calls=[],
        raw={},
    )
    if check_cleanup_boundary:
        responses.append(_call("verify_run", "full-before-cleanup", commands=[full]))
    responses.append(_call("fs_delete", "cleanup-repro", path=_TEMPORARY))
    if recreate_before_final_cleanup:
        responses.extend(
            [
                _call("fs_write", "recreate-repro", path=_TEMPORARY, content=_REPRO),
                _call("fs_delete", "cleanup-recreated-repro", path=_TEMPORARY),
            ]
        )
    if check_cleanup_boundary:
        responses.append(final)
    responses.append(_call("verify_run", "full-after-cleanup", commands=[full]))
    # A bounded response supply lets an erroneous finalization nudge be observed;
    # the script never obeys it by recreating a file or inventing another run.
    responses.extend([final] * 5)
    clients = []
    observed: list[dict[str, Any]] = []
    state_refs = []
    record = turn_core._record_tool_effect

    def observe(**kwargs: Any) -> None:
        record(**kwargs)
        state_refs.append(kwargs["state"])
        if kwargs["tool_name"] == "verify_run":
            observed.append(copy.deepcopy(kwargs["result"]))

    def create_client(**kwargs: Any) -> _ScriptedClient:
        client = _ScriptedClient(responses, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(turn_core, "_record_tool_effect", observe)
    monkeypatch.setattr(session_mod, "_make_session_llm_client", create_client)
    session = session_mod.create_session(
        cfg=AppConfig(
            model="offline-scope-lifecycle-model",
            routing_mode="code_only",
            skills_enabled=False,
            extra_fields={"verify_sandbox": {"mode": "off"}},
        ),
        root=root,
        mode="auto",
        yes=True,
        max_steps=14,
        no_log=False,
        api_key_override="unused-offline-key",
        verify_cmd=[full],
        one_shot_execution=one_shot,
        enable_chat_turn_step_budget=True,
        session_log_dir_override=tmp_path / "sessions",
        surface=NoopSurface(),
        enable_compaction=False,
        subagents_enabled=False,
    )
    try:
        session.run_turn(
            "Fix app.total to sum the supplied values. Reproduce the problem, retain a "
            "meaningful regression in tests/test_app.py, remove the redundant reproduction, "
            "and run the full configured discovery command after cleanup. "
            "Do not claim a failing check passed."
        )
        events = list(read_session_events(session.store.path))
    finally:
        session.close()
    assert len(clients) == 1
    assert len(observed) == (4 if check_cleanup_boundary else 3)
    assert not (root / _TEMPORARY).exists()
    assert (root / "tests/test_app.py").read_text() == _EXPANDED_TEST
    return {"state": state_refs[-1], "results": observed, "events": events, "full": full}


@pytest.mark.parametrize("one_shot", [False, True], ids=["chat", "one-shot"])
def test_cleaned_temporary_repro_is_not_a_remaining_test_obligation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, one_shot: bool
) -> None:
    run = _exercise(tmp_path, monkeypatch, one_shot=one_shot)
    rows = [r["command_results"] for r in run["results"]]
    assert [[row["exit_code"] for row in batch] for batch in rows] == [[1, 0], [0, 0], [0]]
    state = run["state"]
    assert _TEMPORARY in state.agent_created_paths
    assert _TEMPORARY not in state.blast_radius_scope.paths
    assert "tests/test_app.py" in state.blast_radius_scope.paths
    assert state.last_verification_passed is True
    assert not state.missing_verification_commands()
    assert not any(
        "blast_radius_unverified" in event["payload"].get("problems", [])
        for event in run["events"]
        if event["type"] == "completion_gate_nudge"
    )


@pytest.mark.parametrize("one_shot", [False, True], ids=["chat", "one-shot"])
def test_deleted_preexisting_test_is_not_silently_retired_as_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, one_shot: bool
) -> None:
    run = _exercise(tmp_path, monkeypatch, one_shot=one_shot, preexisting_deleted=True)
    state = run["state"]
    assert run["results"][-1]["command_results"][0]["exit_code"] == 0
    assert _TEMPORARY not in state.agent_created_paths
    assert _TEMPORARY in state.blast_radius_scope.paths
    assert (
        state.compute_blast_radius_assessment(enabled=True, turn_intent="execute").status != "clean"
    )


@pytest.mark.parametrize("one_shot", [False, True], ids=["chat", "one-shot"])
def test_temporary_cleanup_does_not_hide_failure_in_retained_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, one_shot: bool
) -> None:
    run = _exercise(tmp_path, monkeypatch, one_shot=one_shot, leave_real_failure=True)
    state = run["state"]
    assert run["results"][-1]["command_results"][0]["exit_code"] == 1
    assert state.last_verification_passed is False
    assert run["full"] in state.failed_verification_commands()
    assert run["full"] in state.missing_verification_commands()
    assert not state.accepted_verification_evidence
    assert (
        state.compute_blast_radius_assessment(enabled=True, turn_intent="execute").status != "clean"
    )


@pytest.mark.parametrize("one_shot", [False, True], ids=["chat", "one-shot"])
def test_cleanup_requires_a_new_scope_run_instead_of_reusing_the_previous_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, one_shot: bool
) -> None:
    run = _exercise(tmp_path, monkeypatch, one_shot=one_shot, check_cleanup_boundary=True)
    results = run["results"]
    assert results[-2]["command_results"][0]["exit_code"] == 0
    assert results[-1]["command_results"][0]["exit_code"] == 0
    assessments = [
        event["payload"] for event in run["events"] if event["type"] == "blast_radius_assessment"
    ]
    assert assessments[0]["status"] == "gate_missing", (
        "a pre-cleanup pass must not certify the changed workspace even when its selected "
        "files contain every surviving scope path"
    )
    assert assessments[-1]["status"] != "gate_missing"
    assert _TEMPORARY not in run["state"].blast_radius_scope.paths


@pytest.mark.parametrize("one_shot", [False, True], ids=["chat", "one-shot"])
@pytest.mark.parametrize("preexisting", [False, True], ids=["temporary", "preexisting"])
def test_delete_recreate_delete_preserves_original_path_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, one_shot: bool, preexisting: bool
) -> None:
    run = _exercise(
        tmp_path,
        monkeypatch,
        one_shot=one_shot,
        preexisting_deleted=preexisting,
        recreate_before_final_cleanup=True,
    )
    state = run["state"]
    assert (_TEMPORARY in state.agent_created_paths) is not preexisting
    assert (_TEMPORARY in state.blast_radius_scope.paths) is preexisting
    scope_presence = []
    for event in run["events"]:
        if event["type"] != "blast_radius_scope_selected":
            continue
        present = _TEMPORARY in event["payload"]["paths"]
        if not scope_presence or present != scope_presence[-1]:
            scope_presence.append(present)
    assert scope_presence == ([True] if preexisting else [True, False, True, False])


def test_scope_retirement_requires_confirmed_absence_and_known_created_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = "tests/test_missing.py"
    denied = "tests/test_denied.py"
    preexisting = "tests/test_preexisting.py"
    original_lstat = Path.lstat

    def observe_lstat(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == tmp_path / denied:
            raise PermissionError("read access unavailable")
        if path == tmp_path / preexisting:
            raise AssertionError("preexisting files must not be candidates for retirement")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", observe_lstat)
    assert absent_agent_created_tests(
        root=tmp_path,
        index=RepoTestIndex(test_files=(missing, denied, preexisting)),
        agent_created_paths={missing, denied},
    ) == (missing,)


def test_broken_leaf_symlink_and_existing_test_are_not_retired(
    tmp_path: Path, require_symlinks: None
) -> None:
    (tmp_path / "tests").mkdir()
    broken = "tests/test_broken.py"
    existing = "tests/test_existing.py"
    (tmp_path / broken).symlink_to(tmp_path / "missing-target.py")
    (tmp_path / existing).write_text("import unittest\n")
    assert (
        absent_agent_created_tests(
            root=tmp_path,
            index=RepoTestIndex(test_files=(broken, existing)),
            agent_created_paths={broken, existing},
        )
        == ()
    )
