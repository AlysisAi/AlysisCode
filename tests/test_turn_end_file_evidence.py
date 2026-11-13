"""What counts as a file change when a turn ends.

The end-of-turn check sends a turn back to work when it tried to change the
workspace's files and no change is there. Work the turn committed, files it
removed and a turn that changed no files are not that case, and in a chat the
model is shown the missing change once.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_agent_loop_one_shot_follow_through import _failed_edit_response, _ScriptedClient
from test_approval_review import _Surface

import alysis_code.agent_loop as agent_loop_mod
from alysis_code.agent.turn.core import file_change_evidence_is_owed
from alysis_code.agent.verification import TurnExecutionState, _record_tool_effect
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import read_session_events
from alysis_code.surface import StandingApproval

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="requires git")

_IDENTITY = {
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", os.fspath(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_IDENTITY},
    )


def _repository(root: Path) -> Path:
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    (root / ".gitignore").write_text("out/\n*.log\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    return root


def _call(name: str, **arguments: Any) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCall(id=f"call-{name}-{len(arguments)}", name=name, arguments=arguments)],
        raw={},
    )


def _say(text: str) -> LLMResponse:
    return LLMResponse(content=text, tool_calls=[], raw={})


@pytest.fixture
def real_shell(monkeypatch):
    """shell_run that runs the command here, reporting the paths the test names."""
    touched: dict[str, list[str]] = {}

    def run(*, root: Path, cmd: str, cwd: str | None = None, runner=None) -> dict[str, Any]:
        _ = cwd, runner
        done = subprocess.run(
            cmd,
            shell=True,
            cwd=root,
            capture_output=True,
            text=True,
            env={**os.environ, **_IDENTITY},
        )
        result = {
            "cmd": cmd,
            "effective_cmd": cmd,
            "exit_code": done.returncode,
            "stdout": done.stdout,
            "stderr": done.stderr,
        }
        if cmd in touched:
            result["touched_repo_paths"] = touched[cmd]
        return result

    monkeypatch.setattr(agent_loop_mod, "shell_run", run)
    return touched


def _turns(tmp_path: Path, root: Path, script: list[list[LLMResponse]], *, one_shot=False):
    """Run one turn per script entry; return the session's events."""
    sessions = tmp_path / "sessions"
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=root,
        mode="auto",
        yes=True,
        max_steps=14,
        no_log=False,
        api_key_override="override-key",
        one_shot_execution=one_shot,
        enable_chat_turn_step_budget=True,
        verification_enabled=False,
        session_log_dir_override=sessions,
        session_id_override="turn-end-evidence",
        # These fixed commands exercise file evidence in a disposable workspace.
        # Give them an explicit test approval rather than calling a live reviewer.
        surface=_Surface(standing=StandingApproval("allow", "test-owned workspace commands")),
    )
    try:
        for index, responses in enumerate(script):
            session.client = _ScriptedClient(responses)  # type: ignore[assignment]
            assert session.run_turn(f"Do part {index + 1} of the work.") == 0
            between = getattr(responses, "after", None)
            if between is not None:
                between()
    finally:
        session.close()
    return list(read_session_events(sessions / "turn-end-evidence.jsonl"))


def _blocked(events) -> list[dict[str, Any]]:
    return [e for e in events if e.get("type") == "empty_diff_finalization_blocked"]


@pytest.mark.parametrize("start", ["no repository", "repository without a commit"])
def test_work_committed_where_there_was_no_commit_ends_the_turn(tmp_path, real_shell, start):
    root = tmp_path / "work"
    root.mkdir()
    (root / "README.md").write_text("# timer\n", encoding="utf-8")
    if start == "repository without a commit":
        _git(root, "init", "-q", "-b", "main")
    script = [
        _call("fs_write", path="timer.py", content="def seconds():\n    return 60\n"),
        *([_call("shell_run", cmd="git init -q")] if start == "no repository" else []),
        _call("shell_run", cmd="git add -A"),
        _call("shell_run", cmd="git commit -q -m first"),
        _say("Created timer.py and committed everything."),
        _say("Created timer.py and committed everything."),
    ]
    events = _turns(tmp_path, root, [script])
    assert _git(root, "log", "--format=%s").stdout.split() == ["first"]
    assert _git(root, "status", "--porcelain").stdout == ""
    assert not _blocked(events)


def test_removing_files_git_does_not_track_ends_the_turn(tmp_path, real_shell):
    root = _repository(tmp_path / "work")
    (root / "out").mkdir()
    (root / "out" / "old.bin").write_bytes(b"x")
    (root / "run.log").write_text("log\n", encoding="utf-8")
    script = [
        _call("fs_delete", paths=["out/old.bin", "run.log"]),
        _say("Removed the build output and the log."),
        _say("Removed the build output and the log."),
    ]
    events = _turns(tmp_path, root, [script])
    assert not (root / "run.log").exists() and not (root / "out" / "old.bin").exists()
    assert _git(root, "status", "--porcelain", "--ignored").stdout == ""
    assert not _blocked(events)


@pytest.mark.parametrize(
    ("folder", "command"),
    [
        ("out", "rm -r out"),  # ignored by Git: no command snapshot ever shows it
        ("vendor/cache", "rm -r vendor"),
    ],
)
def test_removing_a_folder_by_command_after_the_file_tool_refused_it_ends_the_turn(
    tmp_path, real_shell, folder, command
):
    root = _repository(tmp_path / "work")
    (root / ".gitignore").write_text("out/\nvendor/\n*.log\n", encoding="utf-8")
    _git(root, "commit", "-q", "-am", "ignore vendor")
    (root / folder).mkdir(parents=True)
    (root / folder / "old.bin").write_bytes(b"x")
    top = folder.split("/")[0]
    script = [
        _call("fs_delete", path=top),  # a folder: the file tool says so
        _call("shell_run", cmd=command),
        _say(f"Removed the {top} folder."),
        _say(f"Removed the {top} folder."),
    ]
    events = _turns(tmp_path, root, [script])
    assert not (root / top).exists()
    refusal = [
        e["payload"]
        for e in events
        if e.get("type") == "tool_result" and (e.get("payload") or {}).get("name") == "fs_delete"
    ]
    assert f"{top} is a folder" in str(refusal)
    assert not _blocked(events)
    final = [e["payload"] for e in events if e.get("type") == "final"][-1]
    assert "No file" not in str(final.get("content"))


def test_one_missing_file_in_a_list_does_not_undo_the_others(tmp_path, real_shell):
    root = _repository(tmp_path / "work")
    (root / "out").mkdir()
    (root / "out" / "a.bin").write_bytes(b"x")
    (root / "run.log").write_text("log\n", encoding="utf-8")
    script = [
        _call("fs_delete", paths=["out/a.bin", "out/gone.bin", "run.log"]),
        _say("Removed the build output and the log."),
        _say("Removed the build output and the log."),
    ]
    events = _turns(tmp_path, root, [script])
    assert not (root / "out" / "a.bin").exists() and not (root / "run.log").exists()
    result = [
        e["payload"]["result"]
        for e in events
        if e.get("type") == "tool_result" and (e.get("payload") or {}).get("name") == "fs_delete"
    ][0]
    by_path = {entry["path"]: entry for entry in result["results"]}
    assert by_path["out/a.bin"]["ok"] and by_path["run.log"]["ok"]
    assert not by_path["out/gone.bin"]["ok"] and by_path["out/gone.bin"]["error"]
    assert not _blocked(events)


@pytest.mark.parametrize(
    "made_and_removed",
    [
        [
            ("fs_write", {"path": "scratch.py", "content": "print('x')\n"}),
            ("fs_delete", {"path": "scratch.py"}),
        ],
        [
            ("shell_run", {"cmd": "printf 'print(1)' > repro.py"}),
            ("shell_run", {"cmd": "python3 repro.py"}),
            ("shell_run", {"cmd": "rm repro.py"}),
        ],
        [
            ("fs_copy", {"source_path": "README.md", "destination_path": "README.bak"}),
            ("fs_delete", {"path": "README.bak"}),
        ],
        [
            ("fs_move", {"source_path": "README.md", "destination_path": "README.txt"}),
            ("fs_move", {"source_path": "README.txt", "destination_path": "README.md"}),
        ],
        [
            ("shell_run", {"cmd": "printf x > new.py"}),
            ("shell_run", {"cmd": "git clean -fdq"}),
        ],
        [
            # A folder that came with the file, and output Git ignores: neither
            # was there as the turn began, whatever removed them.
            ("fs_write", {"path": "tmp/work/scratch.py", "content": "print('x')\n"}),
            ("fs_delete", {"path": "tmp"}),
            ("shell_run", {"cmd": "rm -r tmp"}),
        ],
        [
            ("shell_run", {"cmd": "mkdir -p out && printf x > out/x.bin"}),
            ("fs_delete", {"path": "out/x.bin"}),
            ("shell_run", {"cmd": "rm -r out"}),
        ],
    ],
    ids=[
        "file tool",
        "command",
        "copy",
        "move away and back",
        "git clean",
        "new folder",
        "ignored output",
    ],
)
def test_a_file_made_and_removed_again_is_not_a_change(tmp_path, real_shell, made_and_removed):
    root = _repository(tmp_path / "work")
    claim = "Fixed the bug."
    script = [
        *[_call(name, **arguments) for name, arguments in made_and_removed],
        *[_say(claim)] * 4,
    ]
    events = _turns(tmp_path, root, [script])
    assert _git(root, "status", "--porcelain").stdout == ""
    # Nothing is left of it, so the claim is questioned, once in a chat.
    assert len(_blocked(events)) == 1
    final = [e["payload"] for e in events if e.get("type") == "final"][-1]
    assert str(final.get("content")).startswith(claim)


@pytest.mark.parametrize("one_shot", [False, True])
def test_a_chat_is_shown_the_missing_change_once(tmp_path, one_shot):
    root = _repository(tmp_path / "work")
    answer = "The fix would be to change README.md."
    script = [_failed_edit_response(), *[_say(answer)] * 6]
    events = _turns(tmp_path, root, [script], one_shot=one_shot)
    # This check sends a one-shot run back up to three times and a chat turn
    # once. (The separate finish check may still add its own single pass.)
    assert len(_blocked(events)) == (3 if one_shot else 1)
    forced = [e for e in events if e.get("type") == "empty_diff_forced"]
    assert len(forced) == 1
    assert (forced[0].get("payload") or {}).get("reason") == "corrective_cap_exhausted"


def test_file_changes_are_owed_only_by_a_turn_that_worked_on_files():
    # A started preview or service counts as a material action and names no
    # file, and neither does a command that changed only another folder.
    assert not file_change_evidence_is_owed(
        "execute", file_tool_called=False, turn_touched_paths=()
    )
    assert file_change_evidence_is_owed("execute", file_tool_called=True, turn_touched_paths=())
    assert file_change_evidence_is_owed(
        "execute", file_tool_called=False, turn_touched_paths={"app.py"}
    )
    for intent in ("read_only", "advisory", ""):
        assert not file_change_evidence_is_owed(
            intent, file_tool_called=True, turn_touched_paths={"app.py"}
        )


def _effect(state, root, tool, arguments, result, status="success"):
    _record_tool_effect(
        root=root,
        state=state,
        tool_name=tool,
        arguments=arguments,
        status=status,
        result=result,
        known_verification_commands=[],
    )


def test_a_list_of_deletes_touches_only_what_it_removed(tmp_path):
    state = TurnExecutionState(execution_requested=True)
    _effect(
        state,
        tmp_path,
        "fs_delete",
        {"path": "kept.txt", "paths": ["a.bin", "b.bin"]},
        {
            "ok": False,
            "deleted_count": 1,
            "results": [{"path": "a.bin", "ok": True}, {"path": "b.bin", "ok": False}],
        },
    )
    assert state.turn_touched_repo_paths == {"a.bin"}
    # Entries from another folder are relative to that folder, not to this project.
    _effect(
        state,
        tmp_path,
        "fs_delete",
        {"paths": ["x.log"]},
        {"scoped_workspace": "/elsewhere", "results": [{"path": "README.md", "ok": True}]},
    )
    assert state.turn_touched_repo_paths == {"a.bin"}
