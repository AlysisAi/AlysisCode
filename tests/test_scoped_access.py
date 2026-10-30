"""Operation grants must cross the approval authority, never mutate session policy."""

import io
import json
import os
import socket
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console
from test_approval_review import _reviewer, _Surface

from alysis_code.agent.action_approval import action_needs_person, decide_action_approval
from alysis_code.agent.errors import ApprovalDeclinedError, ApprovalReviewDeniedError
from alysis_code.agent.scoped_access import SandboxAccessApproval, ScopedAccess, directory_identity
from alysis_code.agent.tools_assembly import build_tools
from alysis_code.config import ConfigError
from alysis_code.host_file_policy import HostFilePolicy, HostFilePolicyError
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.sandbox_runner import HostShellRunner
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.session_store import SessionStore
from alysis_code.surface import ApprovalDecision, ApprovalRequest


@pytest.fixture
def access(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "private-config"))
    root = tmp_path / "project"
    root.mkdir()
    extra = tmp_path / "assets"
    extra.mkdir()
    requests, events = [], []
    monkeypatch.setattr(
        "alysis_code.sandbox_runner.select_shell_sandbox_backend",
        lambda _: SimpleNamespace(backend="windows-mxc"),
    )
    manager = ScopedAccess(
        root=root,
        settings=ShellSandboxSettings(backend="windows-mxc"),
        enabled=True,
        policy=HostFilePolicy((), ()),
        approve=lambda req: requests.append(req) or ApprovalDecision(True),
        record=lambda *event: events.append(event),
    )
    return manager, extra, requests, events


@pytest.mark.parametrize("tool", ["fs_read", "fs_write", "fs_list"])
def test_selected_project_folders_are_local_until_detached(access, tool):
    from alysis_code.project_folders import folder_scope

    manager, extra, requests, _ = access
    args = {"root_path": str(extra)} if tool == "fs_list" else {"path": str(extra / "data.txt")}
    with folder_scope(manager.root, [str(extra)]):
        assert manager.external_file_root(tool, args) is None
        assert not requests
    # Detaching restores the ordinary external-access approval for this call.
    identity = manager.external_file_root(tool, args)
    assert identity is not None and identity.path == str(extra)
    assert len(requests) == 1


def test_copy_between_selected_project_folders_needs_no_external_grant(access):
    from alysis_code.project_folders import folder_scope

    manager, extra, requests, _ = access
    args = {
        "source_path": str(manager.root / "data.txt"),
        "destination_path": str(extra / "copy.txt"),
    }
    with folder_scope(manager.root, [str(extra)]):
        assert manager.external_file_root("fs_copy", args) is None
    assert not requests


def test_grant_is_exact_reviewed_temporary_and_strict(access):
    manager, extra, requests, events = access
    baseline = manager.settings
    grant = manager.prepare(
        {"network": True, "local_preview": True, "read_folders": [str(extra)]},
        command="node preview.js",
        tool="shell_run",
    )
    assert manager.settings is baseline
    assert baseline.network == "off" and baseline.preview_access == "auto"
    assert grant.settings.network == "on" and grant.settings.preview_access == "local"
    assert grant.settings.windows_read_roots == (str(extra),)
    assert not grant.settings.additional_workspace_roots
    assert grant.settings.mode == "strict" and grant.settings.unavailable_fallback == "refuse"
    assert isinstance(requests[0], SandboxAccessApproval)
    assert json.loads(requests[0].preview)["lan_ingress"] is False
    assert events[0][0] == "sandbox_access_granted"
    assert manager.prepare(None, command="next", tool="shell_run").settings is baseline


@pytest.mark.parametrize(
    "proposal", [{}, {"reason": "Inspect HEAD"}, {"scope": "operation", "reason": "Inspect HEAD"}]
)
def test_descriptive_permissions_do_not_create_an_approval(access, proposal):
    manager, _, requests, events = access
    grant = manager.prepare(proposal, command="git status", tool="shell_run")
    assert grant.settings is manager.settings
    assert not requests and not events


def test_descriptive_permissions_still_validate_retained_access(access):
    manager, extra, requests, _ = access
    manager.events_since = lambda _: ([], 0)
    manager.prepare(
        {"read_folders": [str(extra)], "scope": "turn"}, command="first", tool="shell_run"
    )
    reused = []
    manager.validate_reuse = lambda request: reused.append(request)
    manager.prepare({"reason": "Inspect HEAD"}, command="git status", tool="shell_run")
    assert len(requests) == 1 and len(reused) == 1


def test_github_auth_is_explicit_account_authority_and_expires(access):
    manager, _, requests, events = access
    grant = manager.prepare(
        {"network": True, "github_auth": True},
        command="git fetch origin",
        tool="shell_run",
    )
    assert grant.settings.github_auth
    assert requests[0].authentication
    assert requests[0].metadata["access"] == "control"
    assert requests[0].metadata["allow_for_session_disabled"]
    assert "full authority" in json.loads(requests[0].preview)["github_auth"]
    assert "HTTPS" in events[0][1]["github_auth"]
    assert not manager.settings.github_auth
    assert not manager.prepare(
        None, command="git fetch origin", tool="shell_run"
    ).settings.github_auth
    with pytest.raises(ConfigError, match="cannot be retained"):
        manager._payload(grant)


@pytest.mark.parametrize(
    "proposal,command,tool",
    [
        ({"github_auth": True}, "git fetch origin", "shell_run"),
        ({"network": True, "github_auth": True}, "git fetch origin; evil", "shell_run"),
        ({"network": True, "github_auth": True}, "git log", "shell_run"),
        ({"network": True, "github_auth": True}, "git fetch origin", "shell_start"),
        ({"network": True, "github_auth": "yes"}, "git fetch origin", "shell_run"),
    ],
)
def test_invalid_github_auth_never_reaches_approval(access, proposal, command, tool):
    manager, _, requests, events = access
    with pytest.raises(ConfigError):
        manager.prepare(proposal, command=command, tool=tool)
    assert not requests and not events


def test_github_auth_cannot_be_retained_or_saved(access):
    manager, _, requests, events = access
    manager.events_since = lambda _: ([], 0)
    manager.access_store = SimpleNamespace(profile_name=lambda name: name, load=lambda: None)
    for extra in ({"scope": "turn"}, {"scope": "session"}, {"save_as": "auth"}):
        with pytest.raises(ConfigError, match="one operation|cannot be saved"):
            manager.prepare(
                {"network": True, "github_auth": True, **extra},
                command="git push origin HEAD",
                tool="shell_run",
            )
    assert not requests and not events


def test_windows_exact_ssh_files_are_reviewed_expire_and_check_identity(access):
    manager, extra, requests, _ = access
    key = extra / "synthetic-key"
    key.write_text("synthetic credential")
    grant = manager.prepare(
        {"network": True, "read_files": [str(key)]}, command="ssh host", tool="shell_run"
    )
    grant.check()
    assert grant.settings.additional_read_files == (str(key),)
    assert not grant.settings.windows_read_roots
    assert requests[0].authentication
    assert requests[0].metadata["allow_for_session_disabled"]
    assert not manager.prepare(
        None, command="next", tool="shell_run"
    ).settings.additional_read_files
    manager.events_since = lambda _: ([], 0)
    manager.access_store = SimpleNamespace(profile_name=lambda name: name, load=lambda: None)
    for more in ({"scope": "turn"}, {"scope": "session"}, {"save_as": "keys"}):
        with pytest.raises(ConfigError, match="one operation|cannot be saved"):
            manager.prepare(
                {"read_files": [str(key)], **more}, command="ssh host", tool="shell_run"
            )
    key.write_text("replaced credential")
    with pytest.raises(ConfigError, match="approved file changed"):
        grant.check()


@pytest.mark.parametrize(
    "baseline,proposal,expected",
    [
        ({}, {"network": True, "local_preview": True}, True),
        ({"network": "on"}, {"local_preview": True}, True),
        ({"preview_access": "local"}, {"network": True}, True),
        ({}, {"network_domains": ["example.com"], "local_preview": True}, False),
        ({}, {"network": True}, False),
    ],
)
def test_mac_native_trust_authority_is_visible_in_effective_approval(
    access, monkeypatch, baseline, proposal, expected
):
    manager, _, requests, _ = access
    monkeypatch.setattr(
        "alysis_code.sandbox_runner.select_shell_sandbox_backend",
        lambda _: SimpleNamespace(backend="seatbelt"),
    )
    manager.settings = replace(manager.settings, backend="seatbelt", **baseline)
    manager.prepare(proposal, command="tool download", tool="shell_run")
    details = json.loads(requests[0].preview)
    assert bool(details["system_trust_authority"]) == expected
    assert ("daemon network fetches" in requests[0].reason) == expected


def test_turn_grants_reuse_only_access_and_expire_on_next_user_message(access):
    manager, extra, requests, _ = access
    store = SessionStore(
        enabled=False,
        sessions_dir=extra,
        session_id="turn",
        cwd=str(manager.root),
        repo_root=str(manager.root),
    )
    manager.events_since = store.events_since
    store.append("user_message", {"content": "Work with these assets"})
    grant = manager.prepare(
        {"scope": "turn", "network": True, "read_folders": [str(extra)]},
        command="first",
        tool="shell_run",
    )
    later = manager.prepare(None, command="second", tool="shell_run")
    assert later.settings == grant.settings and len(requests) == 1
    assert later.command is None and later.tool is None  # no action approval is cached
    store.append("user_message", {"content": "Now inspect only this project"})
    assert manager.prepare(None, command="third", tool="shell_run").settings is manager.settings


def test_turn_grant_rechecks_folder_identity(access):
    manager, extra, _, _ = access
    manager.events_since = lambda cursor: ([], cursor)
    manager.prepare(
        {"scope": "turn", "read_folders": [str(extra)]}, command="first", tool="shell_run"
    )
    extra.rename(extra.with_name("old-assets"))
    extra.mkdir()
    with pytest.raises(ConfigError, match="changed"):
        manager.prepare(None, command="second", tool="shell_run")
    assert manager.prepare(None, command="third", tool="shell_run").settings is manager.settings


def test_failed_grant_record_cannot_leave_turn_permissions(access):
    manager, extra, _, _ = access
    manager.events_since = lambda cursor: ([], cursor)

    def failed_record(*_):
        raise OSError("record unavailable")

    manager.record = failed_record
    with pytest.raises(OSError, match="record unavailable"):
        manager.prepare(
            {"scope": "turn", "read_folders": [str(extra)]}, command="first", tool="shell_run"
        )
    assert manager.prepare(None, command="next", tool="shell_run").settings is manager.settings


@pytest.mark.skipif(os.name == "nt", reason="Unix sockets")
@pytest.mark.parametrize("allow", [False, True])
def test_daemon_access_requires_person_and_expires_after_operation(access, monkeypatch, allow):
    manager, _, requests, events = access
    monkeypatch.setattr(
        "alysis_code.sandbox_runner.select_shell_sandbox_backend",
        lambda _: SimpleNamespace(backend="seatbelt"),
    )
    reviewer, client, _ = _reviewer(manager.root, [], user_message="Inspect the local daemon")
    surface = _Surface(allow=allow)

    def approve(request):
        requests.append(request)
        return decide_action_approval(
            request,
            mode="auto",
            root=manager.root,
            surface=surface,
            can_ask_person=True,
            reviewer=reviewer,
        )

    manager.approve = approve
    with (
        tempfile.TemporaryDirectory(prefix="alysis-daemon-", dir="/tmp") as folder,
        socket.socket(socket.AF_UNIX) as server,
    ):
        path = Path(folder).resolve() / "daemon.sock"
        server.bind(str(path))
        proposal = {"unix_sockets": [str(path)]}
        if allow:
            grant = manager.prepare(proposal, command="docker version", tool="shell_run")
            assert grant.settings.unix_sockets == (str(path),)
            assert (
                manager.prepare(None, command="next", tool="shell_run").settings is manager.settings
            )
        else:
            with pytest.raises(ApprovalDeclinedError):
                manager.prepare(proposal, command="docker version", tool="shell_run")
            assert not events
        assert len(surface.requests) == 1 and not client.calls
        assert action_needs_person(requests[0], manager.root)
        assert requests[0].metadata["allow_for_session_disabled"] is True
        assert "beyond this sandbox" in json.loads(requests[0].preview)["daemon_authority"]
        manager.events_since = lambda cursor: ([], cursor)
        with pytest.raises(ConfigError, match="one approved operation"):
            manager.prepare(
                {**proposal, "scope": "turn"}, command="docker version", tool="shell_run"
            )
        assert len(surface.requests) == 1


@pytest.mark.parametrize("standing", ["deny", "ask_person"])
def test_combined_operation_approval_never_overrides_standing_rules(tmp_path, standing):
    from alysis_code.surface import StandingApproval

    surface = _Surface(allow=False, standing=StandingApproval(standing, "rule"))
    decision = decide_action_approval(
        ApprovalRequest("shell_run", "install", "npm install", command="npm install"),
        mode="auto",
        root=tmp_path,
        surface=surface,
        can_ask_person=True,
        operation_approved=True,
    )
    assert not decision.allow
    assert len(surface.requests) == (1 if standing == "ask_person" else 0)


def test_combined_approval_cannot_waive_mandatory_person(tmp_path):
    surface = _Surface(allow=False)
    decision = decide_action_approval(
        ApprovalRequest(
            "shell_run",
            "sensitive",
            "command",
            command="command",
            metadata={"mandatory_explicit_approval": True},
        ),
        mode="auto",
        root=tmp_path,
        surface=surface,
        can_ask_person=True,
        operation_approved=True,
    )
    assert not decision.allow and len(surface.requests) == 1


@pytest.mark.parametrize(
    "proposal",
    [
        {"network": "true"},
        {"network": 1},
        {"local_preview": 1},
        {"write_folders": "C:/"},
        {"read_folders": [1]},
        {"host": True},
        {"reason": "x" * 1001},
        [],
    ],
)
def test_malformed_requests_never_reach_review(access, proposal):
    manager, _extra, requests, _events = access
    with pytest.raises(ConfigError):
        manager.prepare(proposal, command="echo test", tool="shell_run")
    assert requests == []


@pytest.mark.parametrize("setting", [{"mode": "off"}, {"filesystem_access": "read-only"}])
def test_scope_cannot_weaken_host_policy(access, setting):
    manager, _, requests, _ = access
    manager.settings = replace(manager.settings, **setting)
    with pytest.raises(ConfigError):
        manager.prepare({"network": True}, command="echo test", tool="shell_run")
    assert not requests


def test_disabled_context_and_other_backend_fail_closed(access, monkeypatch):
    manager, _, requests, _ = access
    manager.enabled = False
    with pytest.raises(ConfigError):
        manager.prepare({"network": True}, command="echo test", tool="shell_run")
    manager.enabled = True
    monkeypatch.setattr(
        "alysis_code.sandbox_runner.select_shell_sandbox_backend",
        lambda _: SimpleNamespace(backend="host"),
    )
    with pytest.raises(ConfigError, match="MXC"):
        manager.prepare({"network": True}, command="echo test", tool="shell_run")
    assert not requests


def test_deny_does_not_record_grant(access):
    manager, _, _, events = access
    manager.approve = lambda _: ApprovalDecision(False)
    with pytest.raises(ApprovalDeclinedError):
        manager.prepare({"network": True}, command="echo test", tool="shell_run")
    assert not events


def test_external_write_review_contains_exact_content_and_rejects_hidden_tail(access):
    manager, extra, requests, _ = access
    target = extra / "report.txt"
    from alysis_code.agent.tools_assembly import _SHELL_CANCELLATION_TOKEN_ARG

    # Interactive dispatch injects an object-keyed cancellation token. Review
    # only model-visible arguments, while leaving the token intact for dispatch.
    args = {
        "path": str(target),
        "content": "Reviewed content",
        _SHELL_CANCELLATION_TOKEN_ARG: object(),
        "_private_dispatch": object(),
    }
    manager.external_file_root("fs_write", args)
    assert _SHELL_CANCELLATION_TOKEN_ARG in args
    assert json.loads(requests[0].command.split(" ", 1)[1]) == {
        "path": str(target),
        "content": "Reviewed content",
    }
    requests.clear()
    with pytest.raises(ConfigError, match="reviewed in full"):
        manager.external_file_root("fs_write", {"path": str(target), "content": "x" * 4000})
    assert not requests and not target.exists()


def test_folder_replacement_during_review_is_rejected(access):
    manager, extra, _, events = access

    def replace_folder(_):
        extra.rename(extra.with_name("original"))
        extra.mkdir()
        return ApprovalDecision(True)

    manager.approve = replace_folder
    with pytest.raises(ConfigError, match="changed"):
        manager.prepare({"write_folders": [str(extra)]}, command="echo test", tool="shell_run")
    assert not events


@pytest.mark.parametrize(
    "value", [str(Path.home()), str(Path.home().anchor), "relative", "C:/folder/../other"]
)
def test_broad_or_ambiguous_roots_rejected(value):
    with pytest.raises((ConfigError, FileNotFoundError)):
        directory_identity(value)


@pytest.mark.parametrize("folder", [".git", ".codex", ".github", ".agents", ".alysis"])
def test_metadata_cannot_be_rebased_as_an_ordinary_workspace(tmp_path, folder):
    target = tmp_path / folder / "nested"
    target.mkdir(parents=True)
    with pytest.raises(ConfigError, match="metadata cannot become permission roots"):
        directory_identity(str(target))


def test_private_state_and_runtime_cannot_be_granted(access):
    manager, extra, requests, _ = access
    manager.policy = HostFilePolicy((extra,), ())
    with pytest.raises(HostFilePolicyError):
        manager.prepare({"read_folders": [str(extra)]}, command="echo test", tool="shell_run")
    manager.policy = HostFilePolicy((), (extra,))
    with pytest.raises(HostFilePolicyError):
        manager.prepare({"write_folders": [str(extra)]}, command="echo test", tool="shell_run")
    assert not requests


def test_cannot_hide_unreviewed_command_tail(access):
    manager, _, requests, _ = access
    with pytest.raises(ConfigError, match="reviewed in full"):
        manager.prepare(
            {"network": True}, command="echo harmless" + " " * 4000 + "evil", tool="shell_run"
        )
    assert not requests


def test_external_file_grant_is_narrow_and_never_expands_common_ancestor(access):
    manager, extra, requests, _ = access
    identity = manager.external_file_root("fs_write", {"path": str(extra / "report.txt")})
    assert identity.path == str(extra)
    assert json.loads(requests[0].preview)["write_folders"] == [str(extra)]
    with pytest.raises(ConfigError, match="Cross-folder"):
        manager.external_file_root(
            "fs_copy",
            {"source_path": "in-project.txt", "destination_path": str(extra / "report.txt")},
        )


def test_only_typed_validated_requests_can_auto_review_external_paths(access):
    manager, extra, _, _ = access
    kwargs = dict(
        kind="sandbox_access",
        reason="test",
        preview="test",
        files=[str(extra / "file.txt")],
        metadata={"scope": "operation", "sandbox_access": True},
    )
    assert action_needs_person(ApprovalRequest(**kwargs), manager.root)
    assert not action_needs_person(SandboxAccessApproval(**kwargs), manager.root)
    assert action_needs_person(
        SandboxAccessApproval(**{**kwargs, "files": [str(extra / ".env")]}), manager.root
    )


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_scope_uses_real_reviewer_without_human_prompt(access, decision):
    manager, extra, _, _ = access
    reviewer, client, _ = _reviewer(
        manager.root,
        [{"decision": decision, "reason": "test"}],
        user_message=f"Read the report in {extra}",
    )
    surface = _Surface(allow=False)
    manager.approve = lambda req: decide_action_approval(
        req, mode="auto", root=manager.root, surface=surface, can_ask_person=True, reviewer=reviewer
    )
    if decision == "allow":
        manager.prepare({"read_folders": [str(extra)]}, command="Read report.txt", tool="fs_read")
    else:
        with pytest.raises(ApprovalReviewDeniedError):
            manager.prepare(
                {"read_folders": [str(extra)]}, command="Read report.txt", tool="fs_read"
            )
    assert not surface.requests and len(client.calls) == 1


@pytest.mark.skipif(os.name != "nt", reason="Native Windows tool assembly")
def test_shell_scoped_runner_never_changes_following_command(access, monkeypatch):
    manager, extra, _, _ = access
    reviewer, _, store = _reviewer(manager.root, [])
    captured = []
    baseline_runner = HostShellRunner()
    scoped_runner = HostShellRunner()
    monkeypatch.setattr(
        "alysis_code.sandbox_runner.build_shell_runner_from_settings",
        lambda settings, *args, **kwargs: captured.append(settings) or scoped_runner,
    )
    monkeypatch.setattr("alysis_code.sandbox_runner.with_closed_stdin", lambda runner: runner)
    seen = []
    monkeypatch.setattr(
        "alysis_code.agent_loop.shell_run",
        lambda **kwargs: (
            seen.append(kwargs["runner"]) or {"exit_code": 0, "stdout": "ok", "stderr": ""}
        ),
    )
    tools = build_tools(
        root=manager.root,
        console=Console(file=io.StringIO()),
        surface=_Surface(),
        store=store,
        mode="auto",
        yes=False,
        shell_runner=baseline_runner,
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        approval_reviewer=reviewer,
        verification_enabled=False,
    )
    assert "permissions" in tools["shell_run"].parameters["properties"]
    permissions = tools["shell_run"].parameters["properties"]["permissions"]["properties"]
    assert permissions["scope"]["enum"] == ["operation", "turn", "session"]
    assert "profile" in permissions and "save_as" in permissions
    assert "network_domains" not in permissions and "unix_sockets" not in permissions
    assert "sandbox_permissions" in tools
    assert tools["sandbox_permissions"].run({"action": "list"}) == {"session": None, "profiles": {}}
    tools["shell_run"].run({"cmd": "echo scoped", "permissions": {"network": True}})
    tools["shell_run"].run({"cmd": "echo ordinary"})
    assert seen == [scoped_runner, baseline_runner]
    assert len(captured) == 1 and captured[0].network == "on"


def test_desktop_standing_rules_only_exempt_validated_folder_requests(tmp_path):
    from test_ide_computer_use_host import _bridge_session

    bridge, _out, session, _job = _bridge_session(tmp_path)
    try:
        args = dict(
            kind="sandbox_access",
            reason="read requested folder",
            preview="read exact file",
            files=[str(tmp_path.parent / "outside-fixture" / "file.txt")],
        )
        assert bridge._standing_approval(session, ApprovalRequest(**args)).outcome == "ask_person"
        assert bridge._standing_approval(session, SandboxAccessApproval(**args)) is None
    finally:
        bridge.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows assembly")
def test_external_read_and_edit_use_guarded_tools_and_keep_primary_root(access, monkeypatch):
    manager, extra, _, _ = access
    # Exercise host file admission, normal edit guards and path translation. Native
    # worker composition is tested separately against the actual helper.
    monkeypatch.setattr(
        "alysis_code.contained_read_tools.ContainedReadTools.for_settings", lambda *a: None
    )
    reviewer, client, store = _reviewer(manager.root, [])
    tools = build_tools(
        root=manager.root,
        console=Console(file=io.StringIO()),
        surface=_Surface(),
        store=store,
        mode="auto",
        yes=False,
        shell_runner=HostShellRunner(),
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        approval_reviewer=reviewer,
        verification_enabled=False,
    )
    target = extra / "report.txt"
    target.write_text("original report\n")
    result = tools["fs_read"].run({"path": str(target)})
    assert "original report" in str(result)
    assert result["path"] == str(target)
    result = tools["fs_edit"].run(
        {
            "path": str(target),
            "edits": [{"op": "replace_exact", "target": "original", "replacement": "updated"}],
        }
    )
    assert target.read_text() == "updated report\n"
    assert result["path"] == str(target)
    assert result["scoped_workspace"] == str(extra)
    assert not (manager.root / "report.txt").exists()
    assert len(client.calls) >= 2


@pytest.mark.parametrize("effect", ["deny", "ask"])
def test_scoped_external_review_preserves_explicit_permission_rules(tmp_path, effect):
    from alysis_code.permission_policy import PermissionPolicy, PermissionRequest

    policy = PermissionPolicy()
    rule = policy.grant(effect, tool_pattern="sandbox_access")
    request = PermissionRequest.create(
        "sandbox_access", paths=[str(tmp_path / "external")], workspace_root=tmp_path / "project"
    )
    result = policy.evaluate(request, review_external=True)
    assert result.decision.value == effect and result.matched_rule_id == rule.id


def test_scoped_review_preserves_global_external_deny_and_sensitive_override(tmp_path):
    from alysis_code.permission_policy import PermissionPolicy, PermissionRequest

    policy = PermissionPolicy(external_directory_override="deny")
    request = PermissionRequest.create(
        "sandbox_access", paths=[str(tmp_path / "external")], workspace_root=tmp_path / "project"
    )
    assert policy.evaluate(request, review_external=True).decision.value == "deny"
    sensitive = replace(request, sensitive=True)
    assert (
        PermissionPolicy().evaluate(sensitive, review_external=True).matched_rule_id
        == "override:sensitive"
    )


def test_scoped_background_keeps_owner_cleanup_and_does_not_modify_manager(tmp_path):
    from test_terminal_manager import DirectBlockingPopen, DirectModeRunner

    from alysis_code.terminal_manager import TerminalManager

    baseline = ShellSandboxSettings(background_kill_timeout_s=0.5)
    ordinary, scoped = (
        DirectModeRunner(DirectBlockingPopen()),
        DirectModeRunner(DirectBlockingPopen()),
    )
    manager = TerminalManager(runner=ordinary, settings=baseline)
    extra = tmp_path / "extra"
    extra.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    try:
        identifier = manager.start(
            cmd="preview fixture",
            root=project,
            cwd=extra,
            scoped_runner=scoped,
            scoped_settings=replace(baseline, additional_workspace_roots=(str(extra),)),
        )
        assert scoped.popen.wait_called.wait(1)
        manager.kill(identifier)
        assert scoped.cleanup_calls == 1 and ordinary.cleanup_calls == 0
        with pytest.raises(ValueError, match="cwd escapes"):
            manager.start(cmd="later fixture", root=project, cwd=extra)
    finally:
        manager.shutdown_all()


def test_missing_optional_read_folder_does_not_discard_valid_grant(access):
    manager, extra, requests, _ = access
    absent = extra.with_name("not-created")
    grant = manager.prepare(
        {"read_folders": [str(extra), str(absent)]}, command="inspect", tool="shell_run"
    )
    assert grant.settings.windows_read_roots == (str(extra),)
    details = json.loads(requests[0].preview)
    assert details["missing_read_folders_not_granted"] == [str(absent)]
    absent.mkdir()
    assert str(absent) not in grant.settings.windows_read_roots
    assert not manager.settings.windows_read_roots


def test_missing_write_folder_requires_existing_parent(access):
    manager, extra, requests, _ = access
    with pytest.raises(ConfigError, match="existing parent"):
        manager.prepare(
            {"write_folders": [str(extra / "not-created")]},
            command="create",
            tool="shell_run",
        )
    assert not requests
