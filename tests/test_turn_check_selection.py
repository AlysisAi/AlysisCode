"""A turn's check comes from the owner's configuration or from the project itself.

The request chooses nothing: not its words ("docs", "frontend", "python"), not
a test command it happens to quote, and not the paths it mentions. The same
request in any language gives the same check, and whether that check applies
to a turn is decided afterwards from the files the turn changed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_verify_tool import _create_interactive_session, _write_repo_files

import alysis_code.agent_loop as agent_loop_mod
from alysis_code.config import AppConfig

# The same kinds of request in English, Greek, Chinese and informal English.
# Each contains words or a command that used to steer the choice of check.
REQUESTS = [
    "Fix the bug.",
    "Update the docs and the README.",
    "Fix the frontend UI in the React component.",
    "Improve the python upload endpoint and its logging.",
    "Update the GitHub Actions workflow.",
    "Do not run `pytest -x tests/slow`. Fix the import error.",
    "Add doctests to the documentation.",
    "Διόρθωσε το σφάλμα και ενημέρωσε την τεκμηρίωση.",
    "Μην τρέξεις το `pytest -x tests/slow`. Διόρθωσε το σφάλμα εισαγωγής.",
    "修复前端界面的错误, 然后更新文档",
    "yo the python api endpoint is busted, sort it + docs pls",
]


def _selection(session, instruction: str) -> tuple[tuple[str, ...], str, str]:
    agent_loop_mod._refresh_interactive_turn_verification_selection(
        session, instruction=instruction, route_execution_posture="execute"
    )
    return (
        tuple(session.effective_verification_commands),
        session.verification_selection_source,
        session.verification_contract_type,
    )


def _session(tmp_path: Path, files: dict[str, str], cfg: AppConfig, name: str):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    _write_repo_files(repo, files)
    return _create_interactive_session(
        repo, sessions_dir=tmp_path / "sessions", session_id=name, cfg=cfg
    )


PYTHON_WITHOUT_TESTS = {
    "pyproject.toml": '[project]\nname = "demo"\nversion = "0.1.0"\n',
    "src/demo/app.py": "def handler() -> str:\n    return 'ok'\n",
    "README.md": "# demo\n",
}
PYTHON_WITH_TESTS = {
    **PYTHON_WITHOUT_TESTS,
    "tests/test_app.py": "def test_placeholder() -> None:\n    assert True\n",
}


@pytest.mark.parametrize("instruction", REQUESTS)
def test_no_default_check_is_assumed_whatever_the_request_says(tmp_path, instruction) -> None:
    """Nobody configured a check and the project has none: none is demanded."""
    session = _session(
        tmp_path, PYTHON_WITHOUT_TESTS, AppConfig(model="test-model"), "no-check-known"
    )
    try:
        commands, source, contract = _selection(session, instruction)
    finally:
        session.close()
    assert commands == ()
    assert contract == "unavailable"
    assert source == "repo_scan.no_authoritative_commands"


@pytest.mark.parametrize("instruction", REQUESTS)
def test_the_owners_check_stands_whatever_the_request_says(tmp_path, instruction) -> None:
    cfg = AppConfig(model="test-model", verify_commands=["make check"])
    session = _session(tmp_path, PYTHON_WITHOUT_TESTS, cfg, "owner-check")
    try:
        commands, source, _ = _selection(session, instruction)
    finally:
        session.close()
    assert commands == ("make check",)
    assert source == "config.verify_commands"


def test_the_projects_own_check_is_the_same_for_every_request(tmp_path) -> None:
    session = _session(tmp_path, PYTHON_WITH_TESTS, AppConfig(model="test-model"), "project-check")
    try:
        selections = {_selection(session, instruction) for instruction in REQUESTS}
        commands = list(session.effective_verification_commands)
    finally:
        session.close()
    assert len(selections) == 1, selections
    assert commands, "the project's tests were discovered"
    assert all("slow" not in command for command in commands)
    # Whether it applies is a fact about what the turn changed.
    for touched, expected in (
        ({"src/demo/app.py"}, True),
        ({"README.md"}, False),
        ({"docs/guide.md", "README.md"}, False),
    ):
        assert (
            agent_loop_mod._verification_expected_for_turn(
                turn_intent="execute",
                blocked=False,
                touched_repo_paths=touched,
                effective_verification_commands=commands,
            )
            is expected
        )


def test_a_test_command_the_request_quotes_does_not_become_the_check(tmp_path) -> None:
    session = _session(tmp_path, PYTHON_WITH_TESTS, AppConfig(model="test-model"), "quoted-command")
    try:
        before = _selection(session, "Fix the import error.")
        quoted = _selection(session, "Do not run `pytest -x tests/slow`. Fix the import error.")
        asked = _selection(session, "Run `pytest -x tests/slow` and fix the import error.")
    finally:
        session.close()
    # Whether the user wants that command run is the model's reading of the
    # request; it declares it if so. The host's selection does not move.
    assert before == quoted == asked


NODE_WITH_SCRIPT = {
    "package.json": '{"name":"demo-web","scripts":{"build":"vite build"}}\n',
    "src/index.ts": "export const value = 1;\n",
    "README.md": "# demo\n",
}


def test_a_node_projects_script_is_its_check_for_every_request(tmp_path) -> None:
    session = _session(tmp_path, NODE_WITH_SCRIPT, AppConfig(model="test-model"), "node-script")
    try:
        selections = {_selection(session, instruction) for instruction in REQUESTS}
        commands = list(session.effective_verification_commands)
    finally:
        session.close()
    assert selections == {(("npm run build",), "repo_scan.likely_test_commands", "selected")}
    # Asked of a turn that changed TypeScript, not of one that changed a document.
    for touched, expected in (({"src/index.ts"}, True), ({"README.md"}, False)):
        assert (
            agent_loop_mod._verification_expected_for_turn(
                turn_intent="execute",
                blocked=False,
                touched_repo_paths=touched,
                effective_verification_commands=commands,
            )
            is expected
        )


def test_a_selection_the_session_already_degraded_is_kept(tmp_path) -> None:
    """A runner the engine cannot use was set aside at session start; it stays set aside."""
    files = {
        "package.json": '{"name":"demo","scripts":{"test":"bun test"}}\n',
        "bun.lockb": "",
        "src/index.ts": "export const value = 1;\n",
    }
    session = _session(tmp_path, files, AppConfig(model="test-model"), "degraded-selection")
    try:
        at_start = (
            tuple(session.effective_verification_commands),
            session.verification_selection_source,
            session.verification_contract_type,
        )
        after = {_selection(session, instruction) for instruction in REQUESTS}
    finally:
        session.close()
    assert after == {at_start}


def test_a_later_file_does_not_make_itself_the_projects_check(tmp_path) -> None:
    session = _session(
        tmp_path, PYTHON_WITHOUT_TESTS, AppConfig(model="test-model"), "later-test-file"
    )
    try:
        first = _selection(session, "Fix the bug.")
        tests = Path(session.root) / "tests"
        tests.mkdir()
        (tests / "test_app.py").write_text("def test_ok() -> None:\n    assert True\n")
        second = _selection(session, "Fix the next bug.")
    finally:
        session.close()
    assert first[0] == () and second == first
