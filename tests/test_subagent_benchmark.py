"""Deterministic, credential-free executable map for subagent audit M01-M10.

The manifest keeps every audit case attached to a real pytest node.  M03 and the
parallel readonly decomposition case execute here through the repository's real CLI,
OpenAI-compatible client, nested ``AgentSession`` lifecycle, and ``MockLLMServer``.
The remaining cases reuse narrower named regression nodes and are checked for stale
references by ``test_subagent_benchmark_manifest_nodes_exist``.

Run independently with::

    python -m pytest -q tests/test_subagent_benchmark.py
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.qa.mock_llm import MockLLMServer

QA_MODEL = "qa-mock-model"
CLI = (sys.executable, "-m", "alysis_code.cli")
REPO_ROOT = Path(__file__).resolve().parents[1]
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Subagent Benchmark",
    "GIT_AUTHOR_EMAIL": "subagent-benchmark@example.invalid",
    "GIT_COMMITTER_NAME": "Subagent Benchmark",
    "GIT_COMMITTER_EMAIL": "subagent-benchmark@example.invalid",
}


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    prompt: str
    node_ids: tuple[str, ...]
    provider_request_count: str
    child_roles: tuple[str, ...]
    tool_names: tuple[str, ...]
    terminal_status: str
    changed_paths: tuple[str, ...]


BENCHMARK_CASES = (
    BenchmarkCase(
        case_id="M01",
        prompt="Use explorer to read existing README.md and report its contents.",
        node_ids=(
            "tests/test_mock_provider_smoke.py::test_smoke_chat_delegates_readme_review_end_to_end",
        ),
        provider_request_count="2 child requests",
        child_roles=("explorer",),
        tool_names=("fs_read",),
        terminal_status="success",
        changed_paths=(),
    ),
    BenchmarkCase(
        case_id="M02",
        prompt=(
            "Use the explorer subagent to inspect the repository and summarize it; do not edit."
        ),
        node_ids=(
            "tests/test_agent_loop_one_shot_follow_through.py::"
            "test_one_shot_readonly_explorer_subagent_synthesis_completes_via_advisory_completion",
        ),
        provider_request_count="2 scripted parent requests",
        child_roles=("explorer",),
        tool_names=("subagent_run",),
        terminal_status="success",
        changed_paths=(),
    ),
    BenchmarkCase(
        case_id="M03",
        prompt=(
            "Use general to update src/a.py and tests/test_a.py, run focused tests, "
            "and report changed files."
        ),
        node_ids=(
            "tests/test_subagent_benchmark.py::"
            "test_subagent_benchmark_m03_multi_file_implementation",
        ),
        provider_request_count="7 parent/child requests",
        child_roles=("general",),
        tool_names=("subagent_run", "fs_write", "verify_run"),
        terminal_status="success",
        changed_paths=("src/a.py", "tests/test_a.py"),
    ),
    BenchmarkCase(
        case_id="M04",
        prompt=(
            "Use explorer for spawning/context and code-reviewer for failures; run them "
            "in parallel."
        ),
        node_ids=(
            "tests/test_subagent_benchmark.py::"
            "test_subagent_benchmark_m04_parallel_readonly_decomposition",
            "tests/test_agent_loop_event_emission.py::"
            "test_run_turn_dispatches_same_batch_subagent_runs_in_parallel",
        ),
        provider_request_count="6 parent/child requests",
        child_roles=("explorer", "code-reviewer"),
        tool_names=("subagent_run", "fs_read"),
        terminal_status="success,success",
        changed_paths=(),
    ),
    BenchmarkCase(
        case_id="M05",
        prompt=(
            "Use debugger to reproduce the failing command and identify the earliest "
            "broken invariant; do not edit."
        ),
        node_ids=("tests/test_subagents.py::test_built_in_subagents_allow_navigation_tools",),
        provider_request_count="0 (static specialist contract)",
        child_roles=("debugger",),
        tool_names=("shell_run", "verify_run"),
        terminal_status="contract validated",
        changed_paths=(),
    ),
    BenchmarkCase(
        case_id="M06",
        prompt=(
            "Use code-reviewer to review this diff with verdict, blocking issues, and test impact."
        ),
        node_ids=(
            "tests/test_subagents.py::"
            "test_code_reviewer_model_role_uses_review_model_client_and_temperature",
            "tests/test_subagents.py::test_built_in_subagents_allow_navigation_tools",
        ),
        provider_request_count="0 (static specialist contract)",
        child_roles=("code-reviewer",),
        tool_names=("git_diff", "fs_read"),
        terminal_status="contract validated",
        changed_paths=(),
    ),
    BenchmarkCase(
        case_id="M07",
        prompt="Use explorer to inspect slowly; cancel now.",
        node_ids=(
            "tests/test_subagent_cancellation.py::"
            "test_serial_subagent_receives_parent_cancellation_and_cleans_up_once",
            "tests/test_subagent_cancellation.py::"
            "test_parallel_subagents_receive_parent_cancellation_and_clean_up_once",
        ),
        provider_request_count="1 scripted parent request",
        child_roles=("explorer",),
        tool_names=("subagent_run",),
        terminal_status="cancelled",
        changed_paths=(),
    ),
    BenchmarkCase(
        case_id="M08",
        prompt="Run visual-designer while image generation is disabled.",
        node_ids=(
            "tests/test_subagents.py::"
            "test_disabled_visual_designer_returns_actionable_capability_error",
        ),
        provider_request_count="0 (launch refused)",
        child_roles=("visual-designer",),
        tool_names=("subagent_run",),
        terminal_status="capability_unavailable",
        changed_paths=(),
    ),
    BenchmarkCase(
        case_id="M09",
        prompt="Run a child that returns no authoritative final report.",
        node_ids=(
            "tests/test_subagents.py::test_subagent_without_final_report_signal_is_degraded",
        ),
        provider_request_count="1 scripted child request",
        child_roles=("explorer",),
        tool_names=("subagent_run",),
        terminal_status="degraded",
        changed_paths=(),
    ),
    BenchmarkCase(
        case_id="M10",
        prompt="Run explorer near an exhausted deadline, then show the TUI state.",
        node_ids=(
            "tests/test_subagents.py::"
            "test_subagent_refuses_launch_when_deadline_has_too_little_remaining_time",
            "tests/test_tui_subagent.py::test_subagent_end_clears_badge_and_appends_finish_line",
        ),
        provider_request_count="0 (deadline launch refused)",
        child_roles=("explorer",),
        tool_names=("subagent_run",),
        terminal_status="deadline_exhausted; TUI cleared",
        changed_paths=(),
    ),
)


def _git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", os.fspath(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_IDENTITY},
    )


def _make_repo(repo: Path) -> None:
    (repo / "src").mkdir(parents=True)
    (repo / "README.md").write_text("# Subagent benchmark\n", encoding="utf-8")
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "app.py").write_text("def main() -> str:\n    return 'ok'\n", encoding="utf-8")
    _git(repo, ["init", "-q"])
    _git(repo, ["config", "user.name", "Subagent Benchmark"])
    _git(repo, ["config", "user.email", "subagent-benchmark@example.invalid"])
    _git(repo, ["checkout", "-B", "main"])
    _git(repo, ["add", "-A"])
    _git(repo, ["commit", "-q", "-m", "seed"])


def _write_config(config_dir: Path, *, base_url: str, repo: Path) -> None:
    config_dir.mkdir(parents=True)
    config = {
        "base_url": base_url,
        "model": QA_MODEL,
        "default_mode": "fullaccess",
        "stream": False,
        "routing_mode": "code_only",
        "subagents_enabled": True,
        "skills_enabled": False,
        "update_check_enabled": False,
        "max_steps": 8,
        "subagent_max_steps": 6,
        "verify_commands": ["python -m pytest -q tests/test_a.py"],
        "profiles": {
            "mock": {
                "name": "mock",
                "protocol": "openai_compat",
                "base_url": base_url,
                "api_key_env": "ALYSIS_API_KEY",
                "default_model": QA_MODEL,
                "extra_headers": {},
                "notes": "subagent benchmark mock",
            }
        },
        "active_profile": "mock",
        "default_workspace_path": os.fspath(repo.resolve()),
    }
    (config_dir / "config.json").write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _env(work_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    src = os.fspath(REPO_ROOT / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = src if not existing else f"{src}{os.pathsep}{existing}"
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    env.update(
        {
            "ALYSIS_CONFIG_DIR": os.fspath((work_dir / "config").resolve()),
            "ALYSIS_DATA_DIR": os.fspath((work_dir / "data").resolve()),
            "ALYSIS_API_KEY": "mock-key",
            "OPENAI_API_KEY": "mock-key",
            "NO_COLOR": "1",
            "TERM": "xterm-256color",
            "ALYSIS_ROUTING_MODE": "code_only",
            "ALYSIS_TUI": "0",
            "ALYSIS_UPDATE_CHECK_ENABLED": "0",
            "ALYSIS_SHELL_SANDBOX_MODE": "off",
            "ALYSIS_VERIFY_SANDBOX_MODE": "off",
            "ALYSIS_SKILLS_ENABLED": "0",
            "ALYSIS_CONTEXT_WINDOW": "200000",
            "ALYSIS_MAX_OUTPUT_TOKENS": "4096",
        }
    )
    return env


def _run_chat(
    *,
    repo: Path,
    work_dir: Path,
    base_url: str,
    mode: str,
    input_text: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            *CLI,
            "chat",
            "--path",
            os.fspath(repo.resolve()),
            "--allow-broad-workspace",
            "--mode",
            mode,
            "--model",
            QA_MODEL,
            "--base-url",
            base_url,
            "--api-key",
            "mock-key",
            "--no-stream",
            "--subagents",
            "--max-steps",
            "8",
            "--yes",
        ],
        cwd=os.fspath(repo),
        env=_env(work_dir),
        input=input_text,
        check=False,
        capture_output=True,
        text=True,
        timeout=60.0,
    )


def _session_events(work_dir: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for path in (work_dir / "data" / "sessions").glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _payloads(events: list[dict[str, object]], event_type: str) -> list[dict[str, object]]:
    return [
        payload
        for event in events
        if event.get("type") == event_type
        for payload in [event.get("payload")]
        if isinstance(payload, dict)
    ]


def _called_tool_names(requests: list[dict[str, object]]) -> set[str]:
    return {
        str(function.get("name") or "")
        for request in requests
        for message in request.get("messages") or ()
        if isinstance(message, dict)
        for call in message.get("tool_calls") or ()
        if isinstance(call, dict)
        for function in [call.get("function")]
        if isinstance(function, dict)
    }


def _changed_paths(repo: Path) -> tuple[str, ...]:
    lines = _git(repo, ["status", "--porcelain", "--untracked-files=all"]).stdout.splitlines()
    paths = [
        path
        for line in lines
        if line
        for path in [line[3:].split(" -> ")[-1].replace("\\", "/")]
        if "__pycache__" not in path and not path.endswith((".pyc", ".pyo"))
    ]
    return tuple(sorted(paths))


def _request_summary(requests: list[dict[str, object]]) -> list[dict[str, object]]:
    summary: list[dict[str, object]] = []
    for request in requests:
        messages = request.get("messages") or ()
        last_user = next(
            (
                str(message.get("content") or "")
                for message in reversed(messages)
                if isinstance(message, dict) and message.get("role") == "user"
            ),
            "",
        )
        summary.append(
            {
                "last_user": last_user[:100],
                "called_tools": sorted(_called_tool_names([request])),
            }
        )
    return summary


def _assert_clean_cli_result(result: subprocess.CompletedProcess[str]) -> None:
    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, combined
    assert "Traceback (most recent call last)" not in result.stderr
    assert "LLMError" not in combined


def test_subagent_benchmark_manifest_nodes_exist() -> None:
    assert [case.case_id for case in BENCHMARK_CASES] == [f"M{index:02d}" for index in range(1, 11)]
    for case in BENCHMARK_CASES:
        assert case.prompt
        assert case.provider_request_count
        assert case.child_roles
        assert case.tool_names
        assert case.terminal_status
        for node_id in case.node_ids:
            path_text, separator, test_name = node_id.partition("::")
            assert separator and test_name.startswith("test_"), node_id
            path = REPO_ROOT / path_text
            assert path.is_file(), node_id
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=os.fspath(path))
            test_functions = {
                node.name
                for node in tree.body
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            }
            assert test_name in test_functions, node_id


@pytest.mark.smoke
def test_subagent_benchmark_m03_multi_file_implementation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _make_repo(repo)
    task = (
        "SUBAGENT_BENCH_M03 update src/a.py and tests/test_a.py, run focused tests, "
        "and report changed files."
    )

    with MockLLMServer() as server:
        _write_config(tmp_path / "config", base_url=server.base_url, repo=repo)
        result = _run_chat(
            repo=repo,
            work_dir=tmp_path,
            base_url=server.base_url,
            mode="fullaccess",
            input_text=f"Use the general subagent to complete this task: {task}\n/exit\n",
        )
        requests = list(server.requests)

    _assert_clean_cli_result(result)
    # The parent delegates, then the child writes, verifies, checks its diff,
    # and reports before the parent synthesizes the result.
    assert len(requests) == 7
    assert _called_tool_names(requests) == {"subagent_run", "fs_write", "verify_run"}
    assert _changed_paths(repo) == ("src/a.py", "tests/test_a.py")
    events = _session_events(tmp_path)
    catalogs = _payloads(events, "subagent_tool_catalog")
    ends = _payloads(events, "subagent_end")
    general_catalog = next(item for item in catalogs if item.get("name") == "general")
    assert {"fs_write", "verify_run"}.issubset(general_catalog["tool_names"])
    assert any(item.get("name") == "general" and item.get("status") == "success" for item in ends)
    assert "src/a.py and tests/test_a.py" in result.stdout


@pytest.mark.smoke
def test_subagent_benchmark_m04_parallel_readonly_decomposition(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _make_repo(repo)
    task = (
        "SUBAGENT_BENCH_M04 use explorer and code-reviewer to inspect two areas in "
        "parallel; synthesize their evidence and do not edit."
    )

    with MockLLMServer() as server:
        _write_config(tmp_path / "config", base_url=server.base_url, repo=repo)
        result = _run_chat(
            repo=repo,
            work_dir=tmp_path,
            base_url=server.base_url,
            mode="review",
            input_text=f"{task}\n/exit\n",
        )
        requests = list(server.requests)
        rendezvous = server.benchmark_parallel_rendezvous

    _assert_clean_cli_result(result)
    # One parent planning request launches both children. Each child then makes
    # one planning request and one fs_read request, followed by one parent
    # synthesis request. Keep this phase count aligned with the router-free
    # runtime; a read-only delegation adds no mutation-gate retry.
    assert len(requests) == 6, json.dumps(_request_summary(requests), indent=2)
    assert rendezvous == {"alpha": True, "beta": True}
    assert _called_tool_names(requests) == {"subagent_run", "fs_read"}
    assert _changed_paths(repo) == ()
    events = _session_events(tmp_path)
    catalogs = _payloads(events, "subagent_tool_catalog")
    ends = _payloads(events, "subagent_end")
    observed_roles = {str(item.get("name") or "") for item in catalogs}
    assert observed_roles == {"explorer", "code-reviewer"}
    for catalog in catalogs:
        assert "fs_read" in catalog["tool_names"]
        assert set(catalog["tool_names"]).isdisjoint({"fs_write", "fs_edit", "shell_run"})
    assert sorted(str(item.get("name")) for item in ends if item.get("status") == "success") == [
        "code-reviewer",
        "explorer",
    ]
    assert "src/app.py defines main()" in result.stdout
