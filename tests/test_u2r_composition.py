"""Cross-side U2R seams: real queue/core and closed metadata boundaries."""

from pathlib import Path

import click
import pytest
from typer.main import get_group

from tests.test_ide_host_actions import _request, _respond, _response_for
from tests.test_plugin_skill_admission import create, finish, root, setup, wait_for  # noqa: F401


def test_plugin_admission_preserves_new_task_folders_and_memory(setup, tmp_path):  # noqa: F811 -- imported pytest fixture
    bridge, _, _, selected, calls = setup
    sid = create(setup)
    extra = tmp_path / "attached"
    extra.mkdir()
    scope = {"project_id": None, "enabled": False}
    bridge.process_line(
        _request(
            "chat.send",
            dict(
                session_id=sid,
                message="Summarize synthetic notes.",
                explicit_plugin_skill=selected,
                task_relation="new_task",
                project_folders=[str(extra.resolve())],
                memory_scope=scope,
            ),
            "joined",
        )
    )
    ack = _response_for(bridge.stdout, "joined")
    assert ack["ok"], ack
    event = wait_for(bridge, "host_action_requested")
    assert not calls
    row = bridge._prompt_queue.list(session_id=sid)[0]
    assert row.payload["task_relation"] == "new_task"
    assert row.payload["explicit_plugin_skill"] == selected
    assert row.payload["project_folders"] == [str(extra.resolve())]
    assert row.payload["memory_scope"] == scope
    _respond(bridge, event, request_id="admit", result={"admit": True})
    assert finish(bridge, ack).status == "completed"
    assert calls
    agent = bridge._sessions[sid].agent_session
    assert agent.project_folders == [str(extra.resolve())]
    assert agent.memory_scope == scope
    assert not bridge._p2_readiness


def test_memory_command_uses_original_lazy_cli_routing():
    from alysis_code.cli_impl.commands import root as cli
    from alysis_code.cli_impl.commands.memory import memory_app

    group = get_group(cli.app)
    with click.Context(group) as ctx:
        assert "memory" in group.list_commands(ctx)
        assert group.get_command(ctx, "memory") is not None
    assert cli.memory_app is memory_app


def test_main_tool_consumers_share_u1r_error_identity():
    from alysis_code.agent import tools_assembly
    from alysis_code.tools import errors, git, search, shell, symbols

    assert tools_assembly.ShellError is shell.ShellError is errors.ShellError
    assert search.SearchError is errors.SearchError
    assert symbols.SymbolSearchError is errors.SymbolSearchError
    assert git.GitDirectoryAccessError is errors.GitDirectoryAccessError


def test_expired_external_observation_never_falls_back_to_attached_metadata(tmp_path, monkeypatch):
    from alysis_code.agent.acceptance_contract import (
        AcceptanceCriterionKind,
        _missing_required_output_paths,
        build_acceptance_contract,
    )
    from alysis_code.agent.artifact_output_observation import ArtifactOutputObserver

    root = tmp_path / "root"  # noqa: F811 -- separate from imported pytest fixture
    root.mkdir()
    output = tmp_path / "external.bmp"
    contract = build_acceptance_contract(root=root, instruction=f'Create "{output}".')
    observer = ArtifactOutputObserver(contract, lambda *a: True, remaining_seconds=lambda: 0)
    observer.refresh(contract)
    criterion = next(
        c for c in contract.criteria if c.kind == AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH
    )
    original_resolve = Path.resolve

    def resolve(path, *a, **kw):
        if path == output:
            pytest.fail("expired observation caused new external path IO")
        return original_resolve(path, *a, **kw)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert _missing_required_output_paths(
        criterion=criterion,
        root=root,
        external_path_observations={str(output): str(output)},
        observer=observer,
    )


@pytest.mark.parametrize("head", [None, "", True, 1, "a" * 39, "a" * 41, "A" * 40, "a" * 40 + "\n"])
def test_push_present_invalid_lease_and_pull_extra_refuse(head):
    from alysis_code.host_actions import HostActionError
    from alysis_code.mcp import github_repository_policy as policy
    from tests.test_github_repository_policy import payload

    bound = policy.RepositoryPolicy.parse(payload())
    for action in ("github.git.push", "github.git.pull"):
        with pytest.raises(HostActionError):
            policy.guard_arguments(
                bound, action, {"branch": "main", "expected_remote_head": head}, None
            )


@pytest.mark.parametrize("head", [None, "a" * 40, "b" * 64])
def test_real_push_registry_guard_and_host_normalizer_preserve_lease(head):
    from alysis_code.host_actions import HostActionError, normalize_host_action_arguments
    from alysis_code.mcp import github_repository_policy as policy
    from tests.test_github_repository_policy import payload

    bound = policy.RepositoryPolicy.parse(payload())
    args = {"branch": "main"}
    if head is not None:
        args["expected_remote_head"] = head
    guarded = policy.guard_arguments(bound, "github.git.push", args, None)
    assert guarded == args
    assert normalize_host_action_arguments("github.git.push", guarded) == args
    if head is not None:
        with pytest.raises(HostActionError):
            policy.guard_arguments(bound, "github.git.pull", args, None)


def test_push_corpus_current_labels_and_explicit_source_mutation_remain_distinct():
    from alysis_code.mcp import github_repository_policy as policy
    from tests.test_github_repository_policy import CORPUS

    pin = policy.NATIVE["github.git.push"][1]
    for case in CORPUS["cases"]:
        if case.get("action") != "github.git.push":
            continue
        assert case["effective_schema_sha256"] == pin
        if case["id"] == "G-git-push-changed-schema":
            assert case["source_schema_sha256"] == "f" * 64
            assert case["expected"]["decision"] != "allow"
        else:
            assert case["source_schema_sha256"] == pin


def test_external_observed_directory_does_not_become_file_output(tmp_path):
    from alysis_code.agent.acceptance_contract import (
        AcceptanceCriterionKind,
        _missing_required_output_paths,
        build_acceptance_contract,
    )
    from alysis_code.agent.artifact_output_observation import ArtifactOutputObserver

    root = tmp_path / "root"  # noqa: F811 -- separate from imported pytest fixture
    root.mkdir()
    output = tmp_path / "external.bmp"
    output.mkdir()
    contract = build_acceptance_contract(root=root, instruction=f'Create "{output}".')
    observer = ArtifactOutputObserver(contract, lambda *a: True)
    observer.refresh(contract)
    criterion = next(
        c for c in contract.criteria if c.kind == AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH
    )
    assert _missing_required_output_paths(
        criterion=criterion,
        root=root,
        external_path_observations={str(output): str(output)},
        observer=observer,
    )
