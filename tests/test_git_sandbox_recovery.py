"""Recovery is advice for a fresh dispatch, never a permission-changing retry."""

from __future__ import annotations

import base64
import io
import json
import subprocess
from dataclasses import replace

import pytest
from rich.console import Console

import alysis_code.agent_loop as loop
import alysis_code.contained_read_tools as contained
import alysis_code.host_read_worker as worker
from alysis_code.agent.errors import AgentRuntimeError
from alysis_code.config import AppConfig, ConfigError
from alysis_code.hooks import HookDispatcher, HookDispatchResult, load_resolved_hooks_config
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.sandbox_runner import HostShellRunner, LazyShellRunner, SandboxBackendChoice
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.session_store import SessionStore, read_session_events
from alysis_code.surface import ApprovalDecision, NoopSurface
from alysis_code.tools.git import GitDirectoryAccessError, GitError
from alysis_code.windows_mxc import WindowsMxcRunner

SETTINGS = ShellSandboxSettings(backend="windows-mxc", safe_git_writes=False)


@pytest.fixture
def native_scope(monkeypatch):
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "strict")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_BACKEND", "windows-mxc")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_FILESYSTEM_ACCESS", "workspace-write")
    reads = []

    def denied(self, name, args, **kwargs):
        reads.append((name, args))
        raise contained.ContainedGitDirectoryError("synthetic directory denial")

    monkeypatch.setattr(contained.ContainedReadTools, "run", denied)
    return reads


def tools(root, runner, **overrides):
    return loop.build_tools(
        root=root,
        console=Console(file=io.StringIO()),
        surface=NoopSurface(),
        store=SessionStore(
            enabled=False,
            sessions_dir=root / "sessions",
            session_id="recovery",
            cwd=str(root),
            repo_root=str(root),
        ),
        mode=overrides.pop("mode", "review"),
        yes=False,
        shell_runner=runner,
        **overrides,
    )


@pytest.mark.parametrize(
    "name,args",
    [
        ("git_status", {}),
        ("git_diff", {"max_chars": 500}),
        ("git_history", {"mode": "log", "limit": 2}),
    ],
)
def test_advice_is_rooted_and_does_not_initialize_or_run_shell(tmp_path, native_scope, name, args):
    if name == "git_history":
        subprocess.run(
            ["git", "-c", "init.templateDir=", "init", str(tmp_path)],
            check=True,
            capture_output=True,
            timeout=10,
        )

    def must_not_load():
        pytest.fail("Advice must not initialize or execute the shell runner")

    runner = LazyShellRunner(must_not_load, settings=SETTINGS)
    with pytest.raises(AgentRuntimeError) as caught:
        tools(tmp_path, runner)[name].run(args)
    payload = caught.value.result_payload
    assert payload["recoverable"] is True and payload["retry_same_tool"] is False
    recovery = payload["recovery"]
    assert recovery["tool"] == "shell_run" and recovery["requires_new_tool_call"] is True
    assert (recovery["cwd"], recovery["cwd_base"]) == (".", "workspace_root")
    assert "cmd" not in recovery  # No interpolation of untrusted arguments into shell source.
    assert "separate sandboxed shell call is available" in payload["error"]
    assert "Attempt the separate dispatch once" in recovery["instructions"]
    assert "without escalating" in recovery["instructions"]
    assert native_scope == [(name, args)] and runner.resolved_runner is None


@pytest.mark.parametrize(
    "kind",
    [
        "readonly",
        "persona",
        "host",
        "loaded-host",
        "readonly-runner",
        "unknown",
        "off",
        "auto",
        "child",
    ],
)
def test_incompatible_session_never_suggests_broader_execution(tmp_path, native_scope, kind):
    runner = WindowsMxcRunner(SETTINGS)
    options = {}
    if kind == "readonly":
        options["mode"] = "readonly"
    elif kind == "child":
        options["subagent_depth"] = 1
    elif kind == "persona":
        options["persona_allow_write_globs"] = ["docs/**"]
    elif kind == "host":
        runner = HostShellRunner(requires_command_approval=True)
    elif kind == "loaded-host":
        runner = LazyShellRunner(lambda: HostShellRunner(), settings=SETTINGS)
        runner._resolve_runner()
    elif kind == "readonly-runner":
        runner = WindowsMxcRunner(replace(SETTINGS, filesystem_access="read-only"))
    elif kind == "unknown":
        runner = object()
    else:
        settings = (
            replace(SETTINGS, mode="off") if kind == "off" else replace(SETTINGS, backend="auto")
        )
        runner = LazyShellRunner(lambda: pytest.fail("Must not load"), settings=settings)
    with pytest.raises(AgentRuntimeError) as caught:
        tools(tmp_path, runner, **options)["git_status"].run({})
    payload = caught.value.result_payload
    assert payload["recoverable"] is False and "recovery" not in payload
    assert "cannot offer a qualified writable-shell recovery" in payload["limitation"]


def test_later_config_changes_do_not_reinterpret_retained_runner(
    tmp_path, native_scope, monkeypatch
):
    registry = tools(tmp_path, WindowsMxcRunner(SETTINGS))
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
    with pytest.raises(AgentRuntimeError) as caught:
        registry["git_status"].run({})
    assert caught.value.result_payload["recoverable"] is True


def test_generic_filter_refusal_does_not_become_recovery(tmp_path, native_scope, monkeypatch):
    def refused(*_a, **_k):
        raise ConfigError("Repository configured a Git filter; inspection refused")

    monkeypatch.setattr(contained.ContainedReadTools, "run", refused)
    with pytest.raises(ConfigError, match="Git filter"):
        tools(tmp_path, WindowsMxcRunner(SETTINGS))["git_diff"].run({})


@pytest.mark.parametrize("typed", [True, False])
def test_worker_transports_only_the_typed_directory_code(tmp_path, monkeypatch, capsys, typed):
    def refused(*_a, **_k):
        exception = GitDirectoryAccessError if typed else GitError
        raise exception("git_directory_access_denied: synthetic text")

    monkeypatch.setattr(worker, "execute", refused)
    monkeypatch.chdir(tmp_path)
    payload = base64.b64encode(
        json.dumps({"tool": "git_status", "arguments": {}}).encode()
    ).decode()
    monkeypatch.setattr("sys.argv", ["worker", payload])
    assert worker.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["error_code"] == ("git_directory_access_denied" if typed else None)


@pytest.mark.parametrize(
    "backend,write,code,ok,typed",
    [
        ("windows-mxc", False, 1, False, True),
        ("windows-mxc", True, 1, False, False),
        ("bwrap", False, 1, False, False),
        ("windows-mxc", False, 0, False, False),
        ("windows-mxc", False, 1, True, False),
        ("windows-mxc", False, 125, False, False),
    ],
)
def test_transport_rejects_wrong_phase_backend_and_malformed_envelopes(
    tmp_path,
    monkeypatch,
    backend,
    write,
    code,
    ok,
    typed,
):
    monkeypatch.setattr(
        contained, "select_shell_sandbox_backend", lambda _: SandboxBackendChoice(backend)
    )

    class Runner:
        def run(self, **_kwargs):
            return subprocess.CompletedProcess(
                [],
                code,
                json.dumps(
                    {
                        "ok": ok,
                        "error": "denied",
                        "error_code": "git_directory_access_denied",
                    }
                ),
                "",
            )

    reader = contained.ContainedReadTools(SETTINGS, tmp_path)
    reader.runner = reader.write_runner = Runner()
    with pytest.raises(ConfigError) as caught:
        reader._invoke(["unused"], write=write, cancel_check=None, timeout_s=5)
    assert isinstance(caught.value, contained.ContainedGitDirectoryError) is typed


@pytest.mark.parametrize("decision", ["allow", "person-deny", "hook-deny"])
def test_model_uses_fresh_dispatch_with_hooks_and_approval(
    tmp_path, native_scope, monkeypatch, decision
):
    starts, hook_calls = [], []
    command = "git --no-optional-locks -c core.fsmonitor=false status --porcelain"

    class RecordingRunner(WindowsMxcRunner):
        def run(self, **kwargs):
            starts.append(kwargs)
            return subprocess.CompletedProcess(kwargs["cmd"], 0, " M ordinary.txt\n", "")

    class Surface(NoopSurface):
        host_managed_approvals = True

        def __init__(self):
            self.requests = []

        def request_approval(self, request):
            self.requests.append(request)
            return ApprovalDecision(allow=decision != "person-deny")

    class Client:
        model = "test-model"
        temperature = 0.2
        calls = 0

        def chat(self, *, messages, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                call = ToolCall(id="read", name="git_status", arguments={})
            elif self.calls == 2:
                result = json.loads(
                    next(m["content"] for m in reversed(messages) if m.get("role") == "tool")
                )
                assert result["error_code"] == "git_directory_access_denied"
                advice = result["recovery"]
                assert starts == []
                call = ToolCall(
                    id="shell",
                    name=advice["tool"],
                    arguments={
                        "cmd": command,
                        "cwd": advice["cwd"],
                        "cwd_base": advice["cwd_base"],
                    },
                )
            else:
                return LLMResponse(
                    content="Inspection complete or blocked as reported.", tool_calls=[], raw={}
                )
            return LLMResponse(content="", tool_calls=[call], raw={})

    def pre_tool(self, *, tool_name, **kwargs):
        hook_calls.append(tool_name)
        return HookDispatchResult(
            blocked=decision == "hook-deny" and tool_name == "shell_run", reason="test hook"
        )

    monkeypatch.setattr(HookDispatcher, "fire_pre_tool_use", pre_tool)
    runner = RecordingRunner(SETTINGS)
    monkeypatch.setattr(loop, "build_shell_runner", lambda **_: runner)
    surface, client = Surface(), Client()
    session = loop.create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=5,
        no_log=False,
        api_key_override="synthetic",
        session_log_dir_override=tmp_path / "sessions",
        session_id_override="git-recovery",
        surface=surface,
    )
    session.client = client
    session.hook_dispatcher = HookDispatcher(
        config=load_resolved_hooks_config(tmp_path),
        workspace_root=tmp_path,
        repo_root=tmp_path,
        session_id="git-recovery",
        mode="review",
        runtime_kind="interactive_chat",
    )
    try:
        code = session.run_turn("Inspect the repository status.")
    finally:
        session.close()
    assert code == (1 if decision == "person-deny" else 0)
    assert client.calls == (2 if decision == "person-deny" else 3)
    assert hook_calls == ["git_status", "shell_run"]
    assert len(starts) == int(decision == "allow")
    if starts:
        assert starts[0]["cwd"] == tmp_path and starts[0]["root"] == tmp_path
    assert [r.command for r in surface.requests] == ([] if decision == "hook-deny" else [command])
    events = list(read_session_events(tmp_path / "sessions/git-recovery.jsonl"))
    calls = [e["payload"]["name"] for e in events if e.get("type") == "tool_call"]
    assert calls == ["git_status", "shell_run"]
