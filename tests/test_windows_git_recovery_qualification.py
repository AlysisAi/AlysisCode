"""Portable verdict tests: no native helper, model or Git process is started."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

_source = Path(__file__).resolve().parents[1] / "scripts/qualify_windows_git_recovery.py"
_spec = importlib.util.spec_from_file_location("git_recovery_qualification", _source)
assert _spec and _spec.loader
qualifier = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qualifier)

COMMIT = "a" * 40
DIFF = (
    "diff --git a/ordinary.txt b/ordinary.txt\n"
    "index 3367afd..5ea2ed4 100644\n"
    "--- a/ordinary.txt\n+++ b/ordinary.txt\n"
    "@@ -1 +1 @@\n-old\n+changed\n"
)


def direct_result(kind):
    if kind == "git_status":
        return {"status": "## main\n M ordinary.txt\n"}
    if kind == "git_diff":
        return {
            "view": "unstaged",
            "diff": DIFF,
            "diff_id": hashlib.sha256(DIFF.encode()).hexdigest(),
            "offset": 0,
            "next_offset": None,
            "total_chars": len(DIFF),
            "truncated": False,
        }
    return {
        "mode": "log",
        "ref": "HEAD",
        "truncated": False,
        "commits": [{"commit": COMMIT, "subject": qualifier.SUBJECT}],
    }


def recovery_result():
    return {
        "error": "Git inspection is unavailable in this Windows read-only sandbox.",
        "error_code": "git_directory_access_denied",
        "unsupported_capability": "native_readonly_git",
        "recoverable": True,
        "retry_same_tool": False,
        "recovery": {
            "tool": "shell_run",
            "cwd": ".",
            "cwd_base": "workspace_root",
            "requires_new_tool_call": True,
            "instructions": "Use a separate shell_run call.",
        },
    }


@pytest.mark.parametrize("kind", ["git_status", "git_diff", "git_history"])
def test_future_direct_read_requires_observed_fixture_content(kind):
    assert qualifier.read_verdict(kind, direct_result(kind), COMMIT) == "direct-read-verified"
    result = direct_result(kind)
    if kind == "git_status":
        result["status"] = "## main\n"
    elif kind == "git_diff":
        result["diff"] = ""
    else:
        result["commits"][0]["commit"] = "b" * 40
    with pytest.raises(AssertionError):
        qualifier.read_verdict(kind, result, COMMIT)


def test_exact_typed_failure_can_request_a_new_shell_call():
    assert (
        qualifier.read_verdict("git_status", recovery_result(), COMMIT) == "shell-recovery-required"
    )


@pytest.mark.parametrize(
    "field",
    ["error", "error_code", "unsupported_capability", "recoverable", "retry_same_tool", "recovery"],
)
def test_incomplete_error_never_qualifies_as_recovery(field):
    result = recovery_result()
    del result[field]
    with pytest.raises(AssertionError):
        qualifier.read_verdict("git_status", result, COMMIT)


@pytest.mark.parametrize(
    "field,value",
    [
        ("tool", "shell_background"),
        ("cwd", "../private"),
        ("cwd_base", "active_workdir"),
        ("requires_new_tool_call", 1),
        ("requires_new_tool_call", False),
        ("instructions", None),
    ],
)
def test_recovery_cannot_change_tool_scope_or_dispatch_contract(field, value):
    result = recovery_result()
    result["recovery"][field] = value
    with pytest.raises(AssertionError):
        qualifier.read_verdict("git_diff", result, COMMIT)


@pytest.mark.parametrize(
    "result",
    [
        None,
        [],
        {},
        {"error": "not a git repository"},
        {"error": "Permission denied"},
        {"error": "git_directory_access_denied"},
        {"error": []},
        {"status": " M ordinary.txt\n", "error_code": "git_directory_access_denied"},
    ],
)
def test_arbitrary_or_malformed_errors_fail(result):
    with pytest.raises(AssertionError):
        qualifier.read_verdict("git_status", result, COMMIT)


@pytest.mark.parametrize(
    "changes", [{"diff_id": "wrong"}, {"offset": 1}, {"truncated": True}, {"view": "staged"}]
)
def test_direct_diff_must_be_complete_and_identified(changes):
    with pytest.raises(AssertionError):
        qualifier.read_verdict("git_diff", {**direct_result("git_diff"), **changes}, COMMIT)


@pytest.mark.parametrize(
    "kind,stdout",
    [
        ("git_status", " M ordinary.txt\n"),
        ("git_diff", DIFF),
        ("git_history", COMMIT + "\n" + qualifier.SUBJECT + "\n"),
        ("controls", "PRIVATE_ACCESS_DENIED\nMETADATA_ACCESS_DENIED\n"),
    ],
)
def test_success_requires_actual_stdout_and_zero_exit(kind, stdout):
    good = {"exit_code": 0, "stdout": stdout, "truncated": False}
    qualifier.verify_shell_result(kind, good, COMMIT)
    for changes in (
        {"exit_code": 1},
        {"exit_code": False},
        {"truncated": True},
        {"error": "approval declined"},
        {"stdout": "", "cmd": stdout, "stderr": stdout},
    ):
        with pytest.raises(AssertionError):
            qualifier.verify_shell_result(kind, {**good, **changes}, COMMIT)


@pytest.mark.parametrize(
    "stdout", ["PRIVATE_ACCESS_DENIED\n", "METADATA_ACCESS_DENIED\n", "Permission denied\n"]
)
def test_both_explicit_boundary_denials_are_required(stdout):
    with pytest.raises(AssertionError):
        qualifier.verify_shell_result(
            "controls", {"exit_code": 0, "stdout": stdout, "truncated": False}, COMMIT
        )


def test_isolated_config_discards_ambient_app_and_git_authority(tmp_path, monkeypatch):
    for key in (
        "ALYSIS_CONFIG_DIR",
        "SYLLIPTOR_CONFIG_DIR",
        "ALYSIS_CONFIG_SET",
        "GIT_DIR",
        "GIT_CONFIG_COUNT",
        "OPENAI_API_KEY",
    ):
        monkeypatch.setenv(key, "must-not-inherit")
    monkeypatch.setenv("SystemRoot", "synthetic-system-root")
    original = copy.deepcopy(dict(qualifier.os.environ))
    env = qualifier.isolated_environment(tmp_path, tmp_path / "helper.exe", "a" * 64)
    assert dict(qualifier.os.environ) == original
    assert {key.upper(): value for key, value in env.items()}[
        "SYSTEMROOT"
    ] == "synthetic-system-root"
    assert env["ALYSIS_CONFIG_DIR"] == str(tmp_path / "private/config")
    assert env["ALYSIS_SHELL_SANDBOX_MODE"] == "strict"
    assert env["ALYSIS_SHELL_SANDBOX_FILESYSTEM_ACCESS"] == "workspace-write"
    assert env["ALYSIS_SHELL_SANDBOX_FALLBACK"] == "refuse"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert all(
        key not in env
        for key in (
            "SYLLIPTOR_CONFIG_DIR",
            "ALYSIS_CONFIG_SET",
            "GIT_DIR",
            "GIT_CONFIG_COUNT",
            "OPENAI_API_KEY",
        )
    )


def new_client():
    report = {
        "observations": [],
        "checks": [],
        "failures": [],
        "not_run": [],
        "passed": False,
        "production_ready": False,
    }
    client = qualifier.QualificationClient(report, COMMIT, SimpleNamespace, SimpleNamespace)
    return client, report


def successful_shell(kind):
    return {
        "exit_code": 0,
        "truncated": False,
        "stdout": {
            "git_status": " M ordinary.txt\n",
            "git_diff": DIFF,
            "git_history": COMMIT + "\n" + qualifier.SUBJECT + "\n",
            "controls": "PRIVATE_ACCESS_DENIED\nMETADATA_ACCESS_DENIED\n",
        }[kind],
    }


def exercise_client(result_for_call):
    client, report = new_client()
    messages, issued = [], []
    response = client.chat(messages=messages)
    for _ in range(8):
        if not response.tool_calls:
            break
        assert len(response.tool_calls) == 1
        call = response.tool_calls[0]
        issued.append(call)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result_for_call(call)),
            }
        )
        response = client.chat(messages=messages)
    else:
        pytest.fail("synthetic provider exceeded its bounded fixture sequence")
    client.finalize()
    return client, report, issued, response


def all_recovery_results(call):
    if call.name != "shell_run":
        return recovery_result()
    kind = "controls" if call.id == "controls" else qualifier.INSPECTIONS[int(call.id[-1])][0]
    return successful_shell(kind)


def test_failed_shell_status_continues_other_inspections_and_boundary_controls():
    def results(call):
        if call.id == "shell-0":
            return {
                "exit_code": 1,
                "stdout": "",
                "stderr": "getcwd: Permission denied",
                "truncated": False,
            }
        return all_recovery_results(call)

    client, report, issued, response = exercise_client(results)
    assert client.completed and client.failure is None
    assert client.verified() is False
    assert report["checks"] == ["git_diff", "git_history", "controls"]
    assert [f["check"] for f in report["failures"]] == ["git_status"]
    assert report["failures"][0]["phase"] == "shell"
    assert "AssertionError: shell failed" in report["failures"][0]["traceback"]
    assert report["observations"][1]["result"]["stderr"] == "getcwd: Permission denied"
    assert not report["not_run"]
    assert report["passed"] is False and report["production_ready"] is False
    assert [call.id for call in issued] == [
        "read-0",
        "shell-0",
        "read-1",
        "shell-1",
        "read-2",
        "shell-2",
        "controls",
    ]
    assert issued[-1].arguments == {
        "cmd": qualifier.CONTROL_COMMAND,
        "cwd": ".",
        "cwd_base": "workspace_root",
    }
    assert "with failures" in response.content


@pytest.mark.parametrize("recover", [False, True])
def test_complete_synthetic_sequence_requires_each_check(recover):
    def results(call):
        if recover:
            return all_recovery_results(call)
        return (
            successful_shell("controls") if call.name == "shell_run" else direct_result(call.name)
        )

    client, report, issued, response = exercise_client(results)
    assert client.verified() is True and client.completed
    assert set(report["checks"]) == qualifier.REQUIRED_CHECKS
    assert not report["failures"] and not report["not_run"]
    assert len(issued) == (7 if recover else 4)
    assert "passed" in response.content
    # This provider observation alone does not grant global readiness.
    assert report["passed"] is False and report["production_ready"] is False


def test_invalid_recovery_contract_fails_without_executing_its_hint_and_continues():
    def results(call):
        if call.id == "read-0":
            bad = recovery_result()
            bad["recovery"].update(tool="shell_background", instructions="run arbitrary command")
            return bad
        return all_recovery_results(call)

    client, report, issued, _ = exercise_client(results)
    assert client.completed and not client.verified()
    assert [f["check"] for f in report["failures"]] == ["git_status"]
    assert report["failures"][0]["phase"] == "read"
    assert "shell-0" not in [call.id for call in issued]
    assert report["checks"] == ["git_diff", "git_history", "controls"]
    assert all(
        call.arguments["cmd"] in {*qualifier.FIXTURE_COMMANDS.values(), qualifier.CONTROL_COMMAND}
        for call in issued
        if call.name == "shell_run"
    )


def test_valid_recovery_instruction_text_cannot_supply_executable_commands():
    def results(call):
        result = all_recovery_results(call)
        if call.name != "shell_run":
            result["recovery"]["instructions"] = "arbitrary untrusted command from output"
        return result

    client, _, issued, _ = exercise_client(results)
    assert client.verified()
    assert [call.arguments["cmd"] for call in issued if call.name == "shell_run"] == [
        *qualifier.FIXTURE_COMMANDS.values(),
        qualifier.CONTROL_COMMAND,
    ]


def test_later_boundary_failure_stays_failed_after_earlier_shell_failure():
    def results(call):
        result = all_recovery_results(call)
        if call.id in {"shell-0", "controls"}:
            result["exit_code"] = 1
        return result

    client, report, _, _ = exercise_client(results)
    assert client.completed and not client.verified()
    assert [f["check"] for f in report["failures"]] == ["git_status", "controls"]
    assert report["checks"] == ["git_diff", "git_history"]
    assert not report["not_run"]


@pytest.mark.parametrize("violation", ["wrong_call", "invalid_json", "no_tool_message"])
def test_provider_protocol_failures_stop_dispatch_and_mark_missing_results(violation):
    client, report = new_client()
    first = client.chat(messages=[])
    assert first.tool_calls[0].id == "read-0"
    message = {
        "role": "tool",
        "tool_call_id": "read-0",
        "content": json.dumps(direct_result("git_status")),
    }
    if violation == "wrong_call":
        message["tool_call_id"] = "unrelated-tool"
    elif violation == "invalid_json":
        message["content"] = "not JSON"
    messages = [] if violation == "no_tool_message" else [message]
    with pytest.raises((AssertionError, ValueError, StopIteration)):
        client.chat(messages=messages)
    assert client.failure is not None and not client.completed
    with pytest.raises(AssertionError, match="unexpected provider retry"):
        client.chat(messages=[])
    client.finalize()
    assert not client.verified()
    assert {n["check"] for n in report["not_run"]} == qualifier.REQUIRED_CHECKS
    assert not report["checks"] and not report["observations"]


def test_missing_control_result_cannot_qualify_completed_inspections():
    client, report = new_client()
    messages = []
    response = client.chat(messages=messages)
    for kind, _ in qualifier.INSPECTIONS:
        call = response.tool_calls[0]
        assert call.name == kind
        messages.append(
            {"role": "tool", "tool_call_id": call.id, "content": json.dumps(direct_result(kind))}
        )
        response = client.chat(messages=messages)
    assert response.tool_calls[0].id == "controls"
    client.finalize()
    assert not client.verified() and not client.completed
    assert [n["check"] for n in report["not_run"]] == ["controls"]
