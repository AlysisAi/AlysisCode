from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from alysis_code.approval_scope import (
    approval_session_scope_for_request,
    exact_command_scope,
    exact_file_set_scope,
)
from alysis_code.permission_policy import (
    InMemorySessionGrantAdapter,
    PermissionPolicy,
    PermissionPolicyCorruptError,
    PermissionPolicyStore,
    PermissionPolicyValidationError,
    PermissionRequest,
    PolicyEffect,
    normalize_command_pattern,
    normalize_path_pattern,
    normalize_workspace_path,
)


def _request(
    tool: str = "fs_read",
    *,
    path: str | None = None,
    paths: tuple[str, ...] = (),
    command: str | None = None,
    platform: str = "posix",
    root: str | None = None,
    sensitive: bool = False,
    external: bool = False,
) -> PermissionRequest:
    return PermissionRequest.create(
        tool,
        path=path,
        paths=paths,
        command=command,
        platform=platform,  # type: ignore[arg-type]
        workspace_root=root,
        sensitive=sensitive,
        external_directory=external,
    )


def test_policy_defaults_to_ask_with_stable_explanation() -> None:
    result = PermissionPolicy().evaluate(_request())

    assert result.decision is PolicyEffect.ASK
    assert result.reason == "no_matching_rule"
    assert result.matched_rule_id is None
    assert result.specificity == 0


def test_rule_requires_selector_and_uses_opaque_id() -> None:
    policy = PermissionPolicy()

    with pytest.raises(PermissionPolicyValidationError, match="requires a selector"):
        policy.grant("allow")
    rule = policy.grant("allow", tool_pattern=" FS_* ", source="workspace")

    assert rule.id.startswith("pr_")
    assert len(rule.id) == 35
    assert rule.tool_pattern == "fs_*"
    assert policy.list_rules() == (rule,)


def test_specific_rule_beats_less_specific_rule() -> None:
    policy = PermissionPolicy()
    broad_deny = policy.grant("deny", tool_pattern="fs_*", path_pattern="src/**")
    narrow_allow = policy.grant("allow", tool_pattern="fs_read", path_pattern="src/public/*.py")

    result = policy.evaluate(_request(path="src/public/model.py"))

    assert result.decision is PolicyEffect.ALLOW
    assert result.matched_rule_id == narrow_allow.id
    assert result.specificity > policy.evaluate(_request(path="src/private/key.py")).specificity
    assert policy.evaluate(_request(path="src/private/key.py")).matched_rule_id == broad_deny.id


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("allow", "deny", "deny"),
        ("deny", "allow", "deny"),
        ("allow", "ask", "ask"),
        ("ask", "allow", "ask"),
    ],
)
def test_fail_closed_effect_wins_at_equal_specificity(
    first: str, second: str, expected: str
) -> None:
    policy = PermissionPolicy()
    policy.grant(first, tool_pattern="shell_run", command_pattern="git *")
    winner = policy.grant(second, tool_pattern="shell_run", command_pattern="git *")

    result = policy.evaluate(_request("shell_run", command=" git   status "))

    assert result.decision.value == expected
    if second == expected:
        assert result.matched_rule_id == winner.id


def test_earlier_rule_wins_when_effect_and_specificity_tie() -> None:
    policy = PermissionPolicy()
    first = policy.grant("allow", tool_pattern="fs_read", source="first")
    policy.grant("allow", tool_pattern="fs_read", source="second")

    result = policy.evaluate(_request())

    assert result.matched_rule_id == first.id
    assert result.matched_rule_source == "first"


def test_deny_or_ask_path_rule_guards_any_target_but_allow_must_cover_all() -> None:
    allow = PermissionPolicy()
    allow.grant("allow", tool_pattern="fs_copy", path_pattern="src/**")
    deny = PermissionPolicy()
    deny.grant("deny", tool_pattern="fs_copy", path_pattern="secrets/**")

    mixed = _request("fs_copy", paths=("src/a.py", "outside/b.py"))
    dangerous = _request("fs_copy", paths=("src/a.py", "secrets/key.pem"))

    assert allow.evaluate(mixed).decision is PolicyEffect.ASK
    assert deny.evaluate(dangerous).decision is PolicyEffect.DENY


def test_path_globs_are_segment_aware_and_double_star_is_recursive() -> None:
    policy = PermissionPolicy()
    policy.grant("allow", path_pattern="src/*.py")
    policy.grant("deny", path_pattern="src/private/**")

    assert policy.evaluate(_request(path="src/main.py")).decision is PolicyEffect.ALLOW
    assert policy.evaluate(_request(path="src/nested/main.py")).decision is PolicyEffect.ASK
    assert policy.evaluate(_request(path="src/private/key.txt")).decision is PolicyEffect.DENY
    assert policy.evaluate(_request(path="src/private/deep/key.txt")).decision is PolicyEffect.DENY


def test_windows_paths_are_slash_normalized_and_case_insensitive() -> None:
    policy = PermissionPolicy()
    policy.grant("allow", tool_pattern="FS_READ", path_pattern=r"SRC\**\*.PY")

    result = policy.evaluate(
        _request(
            path=r"c:\work\repo\src\Deep\Model.py",
            platform="windows",
            root=r"C:\Work\Repo",
        )
    )

    assert result.decision is PolicyEffect.ALLOW
    assert normalize_workspace_path(
        r"C:\WORK\Repo\Src\Model.PY",
        workspace_root=r"c:\work\repo",
        platform="windows",
    ) == ("src/model.py", False)


def test_posix_paths_remain_case_sensitive() -> None:
    policy = PermissionPolicy()
    policy.grant("allow", path_pattern="src/*.py")

    assert policy.evaluate(_request(path="src/model.py")).decision is PolicyEffect.ALLOW
    assert policy.evaluate(_request(path="SRC/model.py")).decision is PolicyEffect.ASK


@pytest.mark.parametrize(
    "pattern",
    ["/etc/**", r"C:\Users\**", "../outside/**", "src/../../outside"],
)
def test_persisted_path_patterns_must_be_workspace_relative(pattern: str) -> None:
    with pytest.raises(PermissionPolicyValidationError, match="workspace-relative"):
        normalize_path_pattern(pattern)


@pytest.mark.parametrize(
    ("path", "root", "platform"),
    [
        ("/other/key", "/repo", "posix"),
        ("../key", "/repo", "posix"),
        (r"D:\key", r"C:\repo", "windows"),
        (r"..\key", r"C:\repo", "windows"),
    ],
)
def test_external_paths_are_detected_and_cannot_match_allow(
    path: str, root: str, platform: str
) -> None:
    policy = PermissionPolicy()
    policy.grant("allow", tool_pattern="fs_read", path_pattern="**")

    result = policy.evaluate(_request(path=path, root=root, platform=platform))

    assert result.decision is PolicyEffect.ASK
    assert result.reason == "external_directory_requires_approval"
    assert result.matched_rule_id == "override:external_directory"


def test_sensitive_and_external_safety_overrides_cannot_be_allowed() -> None:
    policy = PermissionPolicy()
    policy.grant("allow", tool_pattern="*", path_pattern="**", command_pattern="*")

    sensitive = policy.evaluate(
        _request("shell_run", path=".env", command="printenv", sensitive=True)
    )
    external = policy.evaluate(_request(path="src/a.py", external=True))

    assert sensitive.decision is PolicyEffect.ASK
    assert sensitive.reason == "sensitive_resource_requires_approval"
    assert external.decision is PolicyEffect.ASK
    with pytest.raises(PermissionPolicyValidationError, match="cannot allow"):
        policy.set_safety_overrides(sensitive="allow")


def test_deny_safety_override_is_stronger_than_ask() -> None:
    policy = PermissionPolicy(sensitive_override="deny", external_directory_override="ask")

    result = policy.evaluate(_request(sensitive=True, external=True))

    assert result.decision is PolicyEffect.DENY
    assert result.reason == "sensitive_resource_requires_approval"


@pytest.mark.parametrize("sensitive,external", [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize("platform", ["posix", "windows"])
def test_safety_question_cannot_turn_a_winning_deny_rule_into_an_approval(
    sensitive, external, platform
):
    policy = PermissionPolicy()
    rule = policy.grant("deny", tool_pattern="fs_read")
    result = policy.evaluate(_request(sensitive=sensitive, external=external, platform=platform))
    assert result.decision is PolicyEffect.DENY
    assert result.matched_rule_id == rule.id
    assert result.reason == "matched_rule"


def test_command_normalization_is_whitespace_stable_and_windows_casefolded() -> None:
    policy = PermissionPolicy()
    policy.grant("allow", tool_pattern="shell_run", command_pattern="GIT   STATUS *")

    windows = policy.evaluate(
        _request("SHELL_RUN", command=" git\r\nstatus   --short ", platform="windows")
    )
    posix = policy.evaluate(_request("shell_run", command="git\nstatus --short", platform="posix"))

    assert windows.decision is PolicyEffect.ALLOW
    assert posix.decision is PolicyEffect.ASK
    assert normalize_command_pattern("  Echo   hello ", platform="windows") == "echo hello"


def test_command_normalization_preserves_whitespace_inside_quotes() -> None:
    policy = PermissionPolicy()
    policy.grant("allow", command_pattern="printf 'a  b'")

    exact = policy.evaluate(_request("shell_run", command="  printf   'a  b'  "))
    changed_argument = policy.evaluate(_request("shell_run", command="printf 'a b'"))

    assert exact.decision is PolicyEffect.ALLOW
    assert changed_argument.decision is PolicyEffect.ASK


def test_rule_and_request_repr_do_not_contain_command_content() -> None:
    secret = "deploy --token super-secret-value"
    policy = PermissionPolicy()
    rule = policy.grant("allow", command_pattern=secret)
    request = _request("shell_run", command=secret)

    assert secret not in repr(rule)
    assert secret not in repr(request)
    assert rule.to_public_dict()["has_command_pattern"] is True
    assert "command_pattern" not in rule.to_public_dict()
    assert rule.to_public_dict(reveal_command_pattern=True)["command_pattern"] == secret


def test_store_round_trip_grant_list_revoke_and_preserves_order(tmp_path: Path) -> None:
    path = tmp_path / "permissions.json"
    store = PermissionPolicyStore(path)

    first = store.grant("ask", tool_pattern="shell_*", source="organization")
    second = store.grant("allow", path_pattern=r"src\**", source="workspace")

    assert [rule.id for rule in store.list_rules()] == [first.id, second.id]
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert raw["rules"][1]["path_pattern"] == "src/**"
    assert store.revoke(first.id) is True
    assert store.revoke(first.id) is False
    assert store.list_rules() == (second,)
    assert not list(tmp_path.glob("*.tmp"))


def test_store_serializes_concurrent_grants_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "permissions.json"

    def grant(index: int) -> None:
        PermissionPolicyStore(path).grant("allow", tool_pattern=f"tool_{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(grant, range(40)))

    rules = PermissionPolicyStore(path).list_rules()
    assert len(rules) == 40
    assert {rule.tool_pattern for rule in rules} == {f"tool_{index}" for index in range(40)}
    assert [rule.order for rule in rules] == list(range(40))


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        json.dumps(
            {
                "schema_version": True,
                "safety_overrides": {"sensitive": "ask", "external_directory": "ask"},
                "rules": [],
            }
        ),
        json.dumps({"schema_version": 99, "safety_overrides": {}, "rules": []}),
        json.dumps({"schema_version": 1, "safety_overrides": {}, "rules": []}),
        json.dumps(
            {
                "schema_version": 1,
                "safety_overrides": {"sensitive": "ask", "external_directory": "ask"},
                "rules": [{"effect": "allow"}],
            }
        ),
    ],
)
def test_corrupt_or_unknown_policy_fails_closed(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "permissions.json"
    path.write_text(payload, encoding="utf-8")
    store = PermissionPolicyStore(path)

    result = store.evaluate(_request(path="src/a.py"))

    assert result.decision is PolicyEffect.DENY
    assert result.reason == "permission_policy_corrupt"
    with pytest.raises(PermissionPolicyCorruptError, match="access is denied"):
        store.list_rules()


def test_corrupt_policy_error_does_not_echo_secret_command(tmp_path: Path) -> None:
    secret = "token-that-must-never-be-logged"
    path = tmp_path / "permissions.json"
    path.write_text(f'{{"command_pattern":"{secret}"', encoding="utf-8")

    with pytest.raises(PermissionPolicyCorruptError) as captured:
        PermissionPolicyStore(path).load()

    assert secret not in str(captured.value)
    assert secret not in repr(captured.value)
    assert captured.value.__cause__ is None


def test_store_does_not_overwrite_corrupt_policy_on_grant_or_revoke(tmp_path: Path) -> None:
    path = tmp_path / "permissions.json"
    original = b"{corrupt"
    path.write_bytes(original)
    store = PermissionPolicyStore(path)

    with pytest.raises(PermissionPolicyCorruptError):
        store.grant("allow", tool_pattern="fs_read")
    with pytest.raises(PermissionPolicyCorruptError):
        store.revoke("pr_" + "a" * 32)

    assert path.read_bytes() == original


def test_session_grant_adapter_lists_revokes_and_matches_exact_scope() -> None:
    adapter = InMemorySessionGrantAdapter()
    scope = exact_command_scope("git clean -fd", kind="shell_run")

    grant = adapter.grant(kind="shell_run", scope=scope, source="approval")
    duplicate = adapter.grant(kind="shell_run", scope=scope, source="approval")

    assert grant.id.startswith("sg_")
    assert duplicate.id == grant.id
    assert adapter.list_grants() == (grant,)
    assert grant.to_public_dict() == {
        "id": grant.id,
        "kind": "shell_run",
        "scope_type": "exact_command_hash",
        "source": "approval",
    }
    result = adapter.evaluate(kind="shell_run", scope=scope)
    assert result.decision is PolicyEffect.ALLOW
    assert result.reason == "session_exact_grant"
    assert result.matched_rule_id == grant.id
    assert adapter.revoke(grant.id) is True
    assert adapter.evaluate(kind="shell_run", scope=scope).decision is PolicyEffect.ASK


def test_session_grant_adapter_accepts_validated_approval_scope() -> None:
    class Request:
        kind = "fs_edit"
        files = ["src/a.py"]
        allow_for_session_scope = exact_file_set_scope(files, operation="fs_edit")
        metadata = None

    session_scope = approval_session_scope_for_request(Request())
    adapter = InMemorySessionGrantAdapter()

    grant = adapter.grant_approval_scope(session_scope)

    assert grant.kind == "fs_edit"
    assert adapter.evaluate(kind="fs_edit", scope=session_scope.scope or {}).allowed is True


def test_session_grant_rejects_mismatched_declared_kind() -> None:
    adapter = InMemorySessionGrantAdapter()
    scope = exact_command_scope("git status", kind="shell_run")

    with pytest.raises(PermissionPolicyValidationError, match="kind does not match"):
        adapter.grant(kind="fs_read", scope=scope)


def test_session_grant_never_bypasses_sensitive_or_external_safety() -> None:
    adapter = InMemorySessionGrantAdapter()
    scope = exact_command_scope("printenv", kind="shell_run")
    adapter.grant(kind="shell_run", scope=scope)

    sensitive = adapter.evaluate(kind="shell_run", scope=scope, sensitive=True)
    external = adapter.evaluate(kind="shell_run", scope=scope, external_directory=True)

    assert sensitive.decision is PolicyEffect.ASK
    assert sensitive.reason == "sensitive_resource_requires_approval"
    assert external.decision is PolicyEffect.ASK
    assert external.reason == "external_directory_requires_approval"


def test_session_grants_expose_no_command_content_in_repr_or_public_shape() -> None:
    command = "curl -H Authorization:secret https://example.invalid"
    scope = exact_command_scope(command, kind="shell_run")
    grant = InMemorySessionGrantAdapter().grant(kind="shell_run", scope=scope)

    assert command not in repr(grant)
    assert command not in json.dumps(grant.to_public_dict())


def test_policy_file_permissions_follow_atomic_writer_defaults(tmp_path: Path) -> None:
    # This is primarily a regression guard that persistence goes through a real file replacement.
    path = tmp_path / "permissions.json"
    PermissionPolicyStore(path).grant("allow", tool_pattern="fs_read")

    assert path.is_file()
    if os.name != "nt":
        assert path.stat().st_mode & 0o600
