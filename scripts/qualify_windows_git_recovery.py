"""Qualify installed Windows MXC Git inspection for the sandboxed coding scope.

Run with the installed candidate's Python using -I, without a source
PYTHONPATH. Requires pinned ALYSIS_WINDOWS_MXC_HELPER and ALYSIS_WINDOWS_MXC_SHA256.
Creates a new absolute output directory and retains result.json and session logs.
No live model is called. A pass permits typed recovery through a fresh shell_run;
it does not claim strict read-only Git support or general production readiness.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import re
import subprocess
import sys
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any

SUBJECT = "Synthetic Git recovery qualification"
PRIVATE_CANARY = "SYNTHETIC_PRIVATE_GIT_RECOVERY_CANARY"
INSPECTIONS = (
    ("git_status", {}),
    ("git_diff", {}),
    ("git_history", {"mode": "log", "limit": 1}),
)
CONTROL_COMMAND = (
    "$ErrorActionPreference='Stop'; "
    "try { [IO.File]::ReadAllText('../private/canary') | Out-Null; exit 9 } "
    "catch { if ($_.Exception.GetBaseException().HResult -ne -2147024891) { throw }; "
    "Write-Output PRIVATE_ACCESS_DENIED }; "
    "try { [IO.File]::WriteAllText('.git/synthetic-canary', 'MUST DENY'); exit 10 } "
    "catch { if ($_.Exception.GetBaseException().HResult -ne -2147024891) { throw }; "
    "Write-Output METADATA_ACCESS_DENIED }; exit 0"
)
SHELL_COMMANDS = {
    "git_status": "git --no-optional-locks -c core.fsmonitor=false status --porcelain=v1",
    "git_diff": (
        "git --no-optional-locks -c core.fsmonitor=false diff --no-ext-diff --no-textconv --no-color"
    ),
    "git_history": "git --no-optional-locks --no-pager log -1 --format='%H%n%s'",
}
FIXTURE_COMMANDS = {
    name: "$env:GIT_CONFIG_NOSYSTEM='1'; $env:GIT_CONFIG_GLOBAL='NUL'; " + command
    for name, command in SHELL_COMMANDS.items()
}
REQUIRED_CHECKS = frozenset({name for name, _ in INSPECTIONS} | {"controls"})


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _verify_git_text(kind: str, output: Any, commit: str) -> None:
    require(isinstance(output, str), f"{kind}: missing textual output")
    lines = output.splitlines()
    if kind == "git_status":
        require(" M ordinary.txt" in lines, "status did not observe the modified fixture")
    elif kind == "git_diff":
        for line in (
            "diff --git a/ordinary.txt b/ordinary.txt",
            "--- a/ordinary.txt",
            "+++ b/ordinary.txt",
            "@@ -1 +1 @@",
            "-old",
            "+changed",
        ):
            require(line in lines, f"diff did not observe the fixture: {line}")
    elif kind == "git_history":
        require(lines == [commit, SUBJECT], "history did not observe the fixture commit")
    else:
        raise AssertionError(f"Unknown inspection: {kind}")


def read_verdict(kind: str, result: Any, commit: str) -> str:
    """Only verified fixture data or a complete typed recovery contract is accepted."""
    require(isinstance(result, dict), "read result is not an object")
    if "error" in result:
        require(isinstance(result["error"], str) and bool(result["error"]), "invalid error")
        require(result.get("error_code") == "git_directory_access_denied", "unclassified failure")
        require(result.get("unsupported_capability") == "native_readonly_git", "wrong capability")
        require(result.get("recoverable") is True, "Git failure is not recoverable")
        require(result.get("retry_same_tool") is False, "read-tool retry was not prohibited")
        hint = result.get("recovery")
        require(isinstance(hint, dict), "missing recovery hint")
        require(hint.get("tool") == "shell_run", "recovery must use shell_run")
        require(hint.get("cwd") == ".", "recovery changed the working directory")
        require(hint.get("cwd_base") == "workspace_root", "recovery changed the path base")
        require(hint.get("requires_new_tool_call") is True, "recovery must be a new tool call")
        require(
            isinstance(hint.get("instructions"), str) and bool(hint["instructions"]), "no guidance"
        )
        return "shell-recovery-required"
    require("error_code" not in result and "recovery" not in result, "ambiguous read result")
    if kind == "git_status":
        _verify_git_text(kind, result.get("status"), commit)
    elif kind == "git_diff":
        diff = result.get("diff")
        _verify_git_text(kind, diff, commit)
        require(result.get("view") == "unstaged", "wrong diff view")
        require(type(result.get("offset")) is int and result["offset"] == 0, "wrong diff page")
        require(
            result.get("truncated") is False and result.get("next_offset") is None, "partial diff"
        )
        require(result.get("total_chars") == len(diff), "incorrect diff length")
        require(
            result.get("diff_id") == hashlib.sha256(diff.encode()).hexdigest(), "invalid diff id"
        )
    elif kind == "git_history":
        require(result.get("mode") == "log" and result.get("ref") == "HEAD", "wrong history view")
        require(result.get("truncated") is False, "partial history")
        commits = result.get("commits")
        require(isinstance(commits, list) and len(commits) == 1, "missing fixture history")
        require(isinstance(commits[0], dict), "malformed commit")
        require(commits[0].get("commit") == commit, "wrong history commit")
        require(commits[0].get("subject") == SUBJECT, "wrong history subject")
    else:
        raise AssertionError(f"Unknown inspection: {kind}")
    return "direct-read-verified"


def verify_shell_result(kind: str, result: Any, commit: str) -> None:
    require(isinstance(result, dict) and "error" not in result, "shell returned an error")
    require(type(result.get("exit_code")) is int and result["exit_code"] == 0, "shell failed")
    require(result.get("truncated") is False, "shell output was truncated")
    output = result.get("stdout")
    if kind == "controls":
        require(isinstance(output, str), "missing control stdout")
        require(
            output.splitlines() == ["PRIVATE_ACCESS_DENIED", "METADATA_ACCESS_DENIED"],
            "boundary controls did not return both explicit access denials",
        )
    else:
        _verify_git_text(kind, output, commit)


class QualificationClient:
    """Fixed synthetic provider; observation failures do not end later probes.

    A bad provider sequence, tool-call identity, or JSON envelope is still fatal.
    Only assertions about a correctly associated result are collected as failed
    checks. Recovery commands come exclusively from our fixed fixture allowlist.
    """

    model = "test-model"
    temperature = 0.2

    def __init__(self, report, commit, response_type, tool_call_type):
        self.report = report
        self.commit = commit
        self.response_type = response_type
        self.tool_call_type = tool_call_type
        self.calls = 0
        self.index = 0
        self.pending: tuple[str, str, str] | None = None
        self.completed = False
        self.failure: str | None = None
        self.routes: dict[str, str] = {}

    def verified(self) -> bool:
        checks = self.report["checks"]
        return (
            self.completed
            and self.failure is None
            and len(checks) == len(set(checks))
            and set(checks) == REQUIRED_CHECKS
            and not self.report["failures"]
            and not self.report["not_run"]
        )

    def finalize(self) -> None:
        seen = set(self.report["checks"]) | {f["check"] for f in self.report["failures"]}
        self.report["not_run"] = [
            {"check": name, "reason": "provider sequence ended before verification"}
            for name in sorted(REQUIRED_CHECKS - seen)
        ]

    def response(self, name: str, arguments: dict, call_id: str) -> Any:
        return self.response_type(
            content="",
            tool_calls=[self.tool_call_type(id=call_id, name=name, arguments=arguments)],
            raw={},
        )

    def shell(self, command: str, call_id: str) -> Any:
        require(command in {*FIXTURE_COMMANDS.values(), CONTROL_COMMAND}, "unknown fixture command")
        return self.response(
            "shell_run", {"cmd": command, "cwd": ".", "cwd_base": "workspace_root"}, call_id
        )

    def chat(self, *, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.calls += 1
        try:
            require(self.failure is None and not self.completed, "unexpected provider retry")
            if self.pending is not None:
                phase, kind, call_id = self.pending
                message = next(m for m in reversed(messages) if m.get("role") == "tool")
                require(message.get("tool_call_id") == call_id, "wrong tool observation")
                result = json.loads(message["content"])
                self.pending = None
                self.report["observations"].append({"phase": phase, "tool": kind, "result": result})
                try:
                    if phase == "read":
                        route = read_verdict(kind, result, self.commit)
                        self.routes[kind] = route
                        if route == "shell-recovery-required":
                            self.pending = ("shell", kind, f"shell-{self.index}")
                            return self.shell(FIXTURE_COMMANDS[kind], self.pending[2])
                    else:
                        verify_shell_result(kind, result, self.commit)
                except AssertionError:
                    self.report["failures"].append(
                        {"check": kind, "phase": phase, "traceback": traceback.format_exc()}
                    )
                else:
                    self.report["checks"].append(kind)
                if phase == "controls":
                    self.completed = True
                    return self.response_type(
                        content=(
                            "Synthetic Git qualification passed."
                            if self.verified()
                            else "Synthetic Git qualification completed with failures."
                        ),
                        tool_calls=[],
                        raw={},
                    )
                self.index += 1
            if self.index < len(INSPECTIONS):
                name, arguments = INSPECTIONS[self.index]
                call_id = f"read-{self.index}"
                self.pending = ("read", name, call_id)
                return self.response(name, arguments, call_id)
            self.pending = ("controls", "controls", "controls")
            return self.shell(CONTROL_COMMAND, "controls")
        except Exception as exc:
            self.failure = str(exc)
            raise


def isolated_environment(output: Path, helper: Path, digest: str) -> dict[str, str]:
    # Both branding prefixes and provider/Git overrides are untrusted test inputs.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("ALYSIS_", "SYLLIPTOR_", "OPENAI_", "ANTHROPIC_", "GIT_"))
    }
    private = output / "private"
    env.update(
        ALYSIS_CONFIG_DIR=str(private / "config"),
        ALYSIS_DATA_DIR=str(private / "data"),
        ALYSIS_TERMINAL_OWNERSHIP_DIR=str(private / "terminal-ownership"),
        ALYSIS_DESKTOP_CONTROL_PATHS=json.dumps([str(private)]),
        ALYSIS_DESKTOP_RUNTIME_ROOTS="[]",
        ALYSIS_SHELL_SANDBOX_ADDITIONAL_WORKSPACE_ROOTS="[]",
        ALYSIS_SHELL_SANDBOX_MODE="strict",
        ALYSIS_SHELL_SANDBOX_BACKEND="windows-mxc",
        ALYSIS_SHELL_SANDBOX_NETWORK="off",
        ALYSIS_SHELL_SANDBOX_FILESYSTEM_ACCESS="workspace-write",
        ALYSIS_SHELL_SANDBOX_FALLBACK="refuse",
        ALYSIS_SHELL_SANDBOX_CLEAR_ENV="1",
        ALYSIS_SHELL_SANDBOX_PROTECT_REPO_META="1",
        ALYSIS_SHELL_SANDBOX_SAFE_GIT_WRITES="0",
        ALYSIS_WINDOWS_MXC_HELPER=str(helper),
        ALYSIS_WINDOWS_MXC_SHA256=digest,
        ALYSIS_CONTEXT_WINDOW="128000",
        ALYSIS_MAX_OUTPUT_TOKENS="4096",
        ALYSIS_UPDATE_CHECK_ENABLED="0",
        ALYSIS_SKILLS_ENABLED="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
    )
    return env


def git_metadata(work: Path) -> dict[str, str]:
    return {
        path.relative_to(work).as_posix(): sha256(path) if path.is_file() else "directory"
        for path in sorted((work / ".git").rglob("*"))
    }


def qualify(output: Path, helper: Path, expected_digest: str) -> dict[str, Any]:
    report: dict[str, Any] = {
        "scope": "installed Windows MXC sandboxed coding Git inspection; typed shell recovery allowed",
        "production_ready": False,
        "passed": False,
        "python": sys.version,
        "executable": sys.executable,
        "windows": platform.platform(),
        "script_sha256": sha256(Path(__file__)),
        "helper": str(helper),
        "helper_expected_sha256": expected_digest,
        "observations": [],
        "checks": [],
        "failures": [],
        "not_run": [],
        "live_model_calls": 0,
    }
    session = None
    client = None
    surface = None
    before = None
    original_environment = dict(os.environ)
    work, private = output / "workspace", output / "private"
    try:
        report["helper_sha256"] = sha256(helper)
        require(report["helper_sha256"] == expected_digest, "helper digest mismatch; not launched")
        work.mkdir()
        private.mkdir()
        (private / "canary").write_text(PRIVATE_CANARY, encoding="utf-8")
        environment = isolated_environment(output, helper, expected_digest)
        os.environ.clear()
        os.environ.update(environment)

        # Imports deliberately occur only after isolating app state. No src path is added.
        modules = [
            "alysis_code.agent.tools_assembly",
            "alysis_code.contained_read_tools",
            "alysis_code.host_read_worker",
            "alysis_code.tools.git",
            "alysis_code.sandbox_runner",
            "alysis_code.windows_mxc",
            "alysis_code.windows_sandbox",
        ]
        report["installed_modules"] = {}
        for name in modules:
            module_path = Path(importlib.import_module(name).__file__).resolve()
            report["installed_modules"][name] = {
                "path": str(module_path),
                "sha256": sha256(module_path),
            }
        from alysis_code.agent_loop import create_session
        from alysis_code.config import AppConfig
        from alysis_code.llm.openai_compat import LLMResponse, ToolCall
        from alysis_code.sandbox_settings import resolve_shell_sandbox_settings
        from alysis_code.surface import ApprovalDecision, NoopSurface
        from alysis_code.windows_mxc import WindowsMxcRunner, resolve_helper

        require(resolve_helper() == helper, "verified helper changed")
        report["helper_verified"] = True

        def git(*arguments: str) -> str:
            completed = subprocess.run(
                ["git", "-C", str(work), *arguments],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15,
            )
            return completed.stdout

        report["git_version"] = git("--version").strip()
        git("-c", "init.templateDir=", "init", "--quiet")
        # Empty private hooks and explicit local settings also protect native Git,
        # whose sanitized environment does not inherit the host's Git overrides.
        hooks = private / "empty-hooks"
        hooks.mkdir()
        for key, value in {
            "core.hooksPath": str(hooks),
            "core.autocrlf": "false",
            "core.fsmonitor": "false",
            "commit.gpgsign": "false",
            "log.showSignature": "false",
            "user.name": "Synthetic Qualification",
            "user.email": "qualification@example.invalid",
        }.items():
            git("config", "--local", key, value)
        (work / "ordinary.txt").write_bytes(b"old\n")
        git("add", "ordinary.txt")
        git("commit", "--quiet", "-m", SUBJECT)
        commit = git("rev-parse", "HEAD").strip()
        report["fixture_commit"] = commit
        (work / "ordinary.txt").write_bytes(b"changed\n")
        before = git_metadata(work)
        report["git_metadata_before"] = before

        class Surface(NoopSurface):
            host_managed_approvals = True

            def __init__(self) -> None:
                self.requests: list[dict[str, Any]] = []

            def request_approval(self, request: Any) -> Any:
                self.requests.append({"kind": request.kind, "command": request.command})
                require(request.kind == "shell_run", "unexpected approval kind")
                require(request.command in allowed_commands, "unexpected approved command")
                return ApprovalDecision(allow=True)

        # Known fixture commands only. Never execute command text from a tool result.
        allowed_commands = {*FIXTURE_COMMANDS.values(), CONTROL_COMMAND}

        cfg = AppConfig(
            model="test-model",
            routing_mode="code_only",
            stream=False,
            skills_enabled=False,
            bundled_skills_enabled=False,
            custom_tools_enabled=False,
            subagents_enabled=False,
            update_check_enabled=False,
        )
        settings = resolve_shell_sandbox_settings(cfg)
        for key, expected in {
            "mode": "strict",
            "backend": "windows-mxc",
            "filesystem_access": "workspace-write",
            "network": "off",
            "unavailable_fallback": "refuse",
            "clear_env": True,
            "protect_repo_meta": True,
            "safe_git_writes": False,
            "additional_workspace_roots": (),
            "windows_read_roots": (),
        }.items():
            require(getattr(settings, key) == expected, f"unexpected sandbox policy: {key}")
        report["sandbox_settings"] = asdict(settings)
        surface = Surface()
        client = QualificationClient(report, commit, LLMResponse, ToolCall)
        session = create_session(
            cfg=cfg,
            root=work,
            mode="review",
            yes=False,
            non_interactive=True,
            max_steps=12,
            no_log=False,
            api_key_override="synthetic-unused-key",
            session_log_dir_override=private / "sessions",
            session_id_override="native-git-recovery",
            surface=surface,
            verification_enabled=False,
            enable_compaction=False,
            enable_conversation_summarization=False,
            enable_tool_output_offload=False,
        )
        session.client = client
        report["exit_code"] = session.run_turn(
            "Inspect this synthetic repository's status, diff and history, then run the synthetic boundary controls."
        )
        require(
            report["exit_code"] == 0 and client.completed and client.failure is None,
            "agent qualification incomplete",
        )
        loaded_runner = session.shell_runner.resolved_runner
        require(
            isinstance(loaded_runner, WindowsMxcRunner),
            "shell did not retain the native MXC runner",
        )
        require(loaded_runner.settings == settings, "shell policy changed")
        require(git_metadata(work) == before, "Git metadata changed")
        require(
            (private / "canary").read_text(encoding="utf-8") == PRIVATE_CANARY,
            "private file changed",
        )
        require(not (work / ".git/synthetic-canary").exists(), "Git metadata write succeeded")
        require(sha256(helper) == expected_digest, "helper changed during qualification")
        report["metadata_unchanged"] = True
        report["private_unchanged"] = True
        require(client.verified(), "one or more required Git or boundary checks failed")
        report["passed"] = True
    except Exception:
        report["failure"] = traceback.format_exc()
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                report["passed"] = False
                report["close_failure"] = traceback.format_exc()
        if client is not None:
            client.finalize()
            if not client.verified():
                report["passed"] = False
            report["synthetic_provider_calls"] = client.calls
            report["provider_failure"] = client.failure
            report["routes"] = client.routes
        else:
            report["not_run"] = [
                {"check": name, "reason": "qualification setup failed"}
                for name in sorted(REQUIRED_CHECKS)
            ]
        if surface is not None:
            report["approvals"] = surface.requests
        if before is not None:
            try:
                report["git_metadata_after"] = git_metadata(work)
                report["metadata_unchanged"] = report["git_metadata_after"] == before
                report["private_unchanged"] = (private / "canary").read_text(
                    encoding="utf-8"
                ) == PRIVATE_CANARY
                if not report["metadata_unchanged"] or not report["private_unchanged"]:
                    report["passed"] = False
            except Exception:
                report["passed"] = False
                report["final_boundary_failure"] = traceback.format_exc()
        report["native_readonly_git_supported"] = bool(
            report["passed"]
            and client is not None
            and len(client.routes) == 3
            and all(route == "direct-read-verified" for route in client.routes.values())
        )
        os.environ.clear()
        os.environ.update(original_environment)
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New absolute output directory")
    args = parser.parse_args()
    if os.name != "nt" or platform.system() != "Windows":
        parser.error("Native Windows is required; no helper was launched")
    if not sys.flags.isolated:
        parser.error(
            "Run the installed candidate's Python with -I; source PYTHONPATH is not supported"
        )
    if not args.output.is_absolute():
        parser.error("--output must be absolute")
    helper = Path(os.environ.get("ALYSIS_WINDOWS_MXC_HELPER", ""))
    digest = os.environ.get("ALYSIS_WINDOWS_MXC_SHA256", "").lower()
    if not helper.is_absolute() or not re.fullmatch(r"[0-9a-f]{64}", digest):
        parser.error(
            "Set an absolute ALYSIS_WINDOWS_MXC_HELPER and its verified ALYSIS_WINDOWS_MXC_SHA256"
        )
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = qualify(output, helper.resolve(strict=True), digest)
    print(json.dumps({"passed": report["passed"], "result": str(output / "result.json")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
