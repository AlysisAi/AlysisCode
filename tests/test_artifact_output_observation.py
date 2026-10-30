from __future__ import annotations

from pathlib import Path

import pytest

from alysis_code.agent.acceptance_contract import (
    AcceptanceCriterionKind,
    AcceptanceCriterionStatus,
    build_acceptance_contract,
    finalize_acceptance_contract,
    invalidate_acceptance_evidence,
)
from alysis_code.agent.artifact_output_observation import ArtifactOutputObserver, observe_file
from alysis_code.agent.verification import TurnExecutionState


def fixture(tmp_path, access=lambda path, initial: True):
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    output = tmp_path / "saved output.bmp"
    contract = build_acceptance_contract(root=root, instruction=f'Save the result to "{output}".')
    contract.task_id = "fixture-task"
    contract.artifact_output_observer = ArtifactOutputObserver(contract, access)
    state = TurnExecutionState(execution_requested=True, acceptance_contract=contract)
    criterion = next(
        c for c in contract.criteria if c.kind == AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH
    )
    return root, output, contract, state, criterion


def finish(root, contract, state):
    finalize_acceptance_contract(contract=contract, root=root, touched_paths=set())
    return state.compute_expectation_assessment(enabled=True, turn_intent="execute")


def test_external_gui_save_confirms_only_the_requested_file(tmp_path):
    root, output, contract, state, criterion = fixture(tmp_path)
    # Simulate another process saving a document. No fs_write/shell tool receipt.
    output.write_bytes(b"synthetic bytes, deliberately not a valid bitmap")
    assessment = finish(root, contract, state)
    assert criterion.status == AcceptanceCriterionStatus.PASSED
    assert assessment.confirmed == ("exp001",)
    assert not state.touched_repo_paths
    assert state.latest_expectation_evidence[0]["source"] == "host_file_metadata"
    assert all(e.category == "artifact_file_metadata" for e in contract.evidence)
    assert "contents not verified" in contract.evidence[-1].summary
    # Runtime observers/permissions cannot be restored from model-visible JSON.
    assert "artifact_output_observer" not in contract.as_payload()


@pytest.mark.parametrize("write", [False, True])
def test_no_observer_or_model_claim_cannot_certify_external_output(tmp_path, write):
    root, output, contract, state, criterion = fixture(tmp_path)
    contract.artifact_output_observer = None
    if write:
        output.write_bytes(b"exists")
    state.note_post_edit_run_output(command="claimed save", output=f"Saved {output}", generation=0)
    assert finish(root, contract, state).unaddressed == ("exp001",)
    assert criterion.status == AcceptanceCriterionStatus.UNVERIFIED


def test_unchanged_existing_file_does_not_confirm_requested_change(tmp_path):
    (tmp_path / "saved output.bmp").write_bytes(b"old artifact")
    root, output, contract, state, criterion = fixture(tmp_path)
    assert finish(root, contract, state).unaddressed == ("exp001",)
    assert criterion.status == AcceptanceCriterionStatus.PASSED  # Existence only.
    output.write_bytes(b"new artifact of a different size")
    assert finish(root, contract, state).confirmed == ("exp001",)


def test_deleted_or_replaced_output_revokes_previous_evidence(tmp_path):
    root, output, contract, state, criterion = fixture(tmp_path)
    output.write_bytes(b"new")
    assert finish(root, contract, state).confirmed
    output.unlink()
    assert finish(root, contract, state).unaddressed
    assert criterion.status == AcceptanceCriterionStatus.UNVERIFIED
    output.mkdir()
    assert finish(root, contract, state).unaddressed
    assert criterion.status == AcceptanceCriterionStatus.UNVERIFIED


def test_generation_and_task_identity_do_not_reuse_file_evidence(tmp_path):
    root, output, contract, state, _ = fixture(tmp_path)
    output.write_bytes(b"new")
    assert finish(root, contract, state).confirmed
    invalidate_acceptance_evidence(contract, generation=1)
    assert not state.current_expectation_evidence(contract.expectations)
    assert finish(root, contract, state).confirmed
    contract.task_id = "different-task"
    assert not state.current_expectation_evidence(contract.expectations)
    assert finish(root, contract, state).unaddressed


def test_permission_denial_and_revocation_never_read_file(tmp_path, monkeypatch):
    import alysis_code.agent.artifact_output_observation as observation

    reads = []
    original = observation.subprocess.Popen
    monkeypatch.setattr(
        observation.subprocess,
        "Popen",
        lambda *args, **kwargs: reads.append(args) or original(*args, **kwargs),
    )
    allowed = False
    root, output, contract, state, criterion = fixture(tmp_path, lambda path, initial: allowed)
    output.write_bytes(b"new")
    assert finish(root, contract, state).unaddressed
    assert not reads
    allowed = True
    contract.artifact_output_observer = ArtifactOutputObserver(
        contract, lambda path, initial: allowed
    )
    assert reads  # Prove the spy covers the real observation boundary.
    reads.clear()
    allowed = False
    assert finish(root, contract, state).unaddressed
    assert criterion.status == AcceptanceCriterionStatus.UNVERIFIED
    assert not reads


@pytest.mark.parametrize("stopped_by", ["deadline", "cancel"])
def test_stopped_observer_never_launches_io_or_requests_access(tmp_path, monkeypatch, stopped_by):
    import alysis_code.agent.artifact_output_observation as observation

    requested = (tmp_path / "output.bmp").as_posix()
    contract = build_acceptance_contract(root=tmp_path, instruction=f'Create "{requested}".')
    assert [ref.absolute_path for ref in contract.path_refs] == [requested]
    monkeypatch.setattr(
        observation.subprocess, "Popen", lambda *a, **kw: pytest.fail("started stopped I/O")
    )
    observer = ArtifactOutputObserver(
        contract,
        lambda *a: pytest.fail("requested access after stop"),
        remaining_seconds=lambda: 0 if stopped_by == "deadline" else None,
        cancelled=lambda: stopped_by == "cancel",
    )
    assert observer.loci_by_path == {requested: {"output.bmp"}}
    assert not observer.baseline  # Stop precedes admission, but retains the requested locus.
    observer.refresh(contract)
    assert set(observer.current) == {requested}
    assert all(item.state == "unavailable" for item in observer.current.values())
    assert not observer.changed_paths


def test_workspace_directory_requirement_uses_bounded_metadata_without_claiming_contents(tmp_path):
    contract = build_acceptance_contract(
        root=tmp_path, instruction='Create directory "outputs/report".'
    )
    contract.artifact_output_observer = ArtifactOutputObserver(contract, lambda *a: True)
    (tmp_path / "outputs/report").mkdir(parents=True)
    finalize_acceptance_contract(contract=contract, root=tmp_path, touched_paths=set())
    criterion = next(
        c for c in contract.criteria if c.kind == AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH
    )
    assert criterion.status == AcceptanceCriterionStatus.PASSED
    assert {Path(path) for path in contract.artifact_output_observer.changed_paths} == {
        tmp_path / "outputs/report"
    }
    state = TurnExecutionState(execution_requested=True, acceptance_contract=contract)
    assert state.compute_expectation_assessment(enabled=True, turn_intent="execute").confirmed
    assert all("contents not verified" in evidence.summary for evidence in contract.evidence)
    (tmp_path / "outputs/report").rmdir()
    assert finish(tmp_path, contract, state).unaddressed


def test_existing_directory_metadata_does_not_prove_changes_to_its_contents(tmp_path):
    from alysis_code.agent.task_artifact_binding import bind_path_interpretation

    output = tmp_path / "outputs/report"
    output.mkdir(parents=True)
    instruction = 'Update "outputs/report".'
    refs = bind_path_interpretation(
        root=tmp_path,
        instruction=instruction,
        prior_refs=[],
        arguments={
            "paths": [{"name": "outputs/report", "directory": "", "role": "required_output"}]
        },
    )
    contract = build_acceptance_contract(
        root=tmp_path, instruction=instruction, interpreted_path_refs=refs
    )
    contract.artifact_output_observer = ArtifactOutputObserver(contract, lambda *a: True)
    assert {
        Path(path): value for path, value in contract.artifact_output_observer.baseline.items()
    }[output].state == "directory"
    (output / "child.txt").write_text("new child")
    finalize_acceptance_contract(contract=contract, root=tmp_path, touched_paths=set())
    assert not contract.artifact_output_observer.changed_paths
    assert all(not evidence.passed for evidence in contract.evidence)


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows path identity")
def test_expired_windows_path_alias_cannot_fall_back_to_synchronous_exists(tmp_path, monkeypatch):
    requested = (tmp_path / "output.bmp").as_posix().lower()
    contract = build_acceptance_contract(root=tmp_path, instruction=f'Create "{requested}".')
    contract.artifact_output_observer = ArtifactOutputObserver(
        contract,
        lambda *a: True,
        remaining_seconds=lambda: 0,
    )
    original_exists = Path.exists

    def guarded_exists(path):
        if path.as_posix().lower() == requested:
            pytest.fail("Expired metadata observation fell back to synchronous I/O")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", guarded_exists)
    finalize_acceptance_contract(contract=contract, root=tmp_path, touched_paths=set())
    criterion = next(
        c for c in contract.criteria if c.kind == AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH
    )
    assert criterion.status == AcceptanceCriterionStatus.UNVERIFIED


@pytest.mark.parametrize("stopped_by", ["deadline", "cancel", "batch_budget"])
def test_stalled_metadata_is_bounded_reaped_and_revokes_success(tmp_path, monkeypatch, stopped_by):
    import time

    import alysis_code.agent.artifact_output_observation as observation

    root, output, contract, state, criterion = fixture(tmp_path)
    output.write_bytes(b"new")
    assert finish(root, contract, state).confirmed
    observer = contract.artifact_output_observer
    real_popen = observation.subprocess.Popen
    children = []

    def stalled(command, **kwargs):
        # Preserve the real base-interpreter/isolated startup contract. Replacing
        # it with sys.executable would reintroduce a Windows launcher descendant.
        child = real_popen([*command[:3], "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(observation.subprocess, "Popen", stalled)
    start = time.monotonic()
    if stopped_by == "deadline":
        observer.remaining_seconds = lambda: 0.15 - (time.monotonic() - start)
    elif stopped_by == "cancel":
        observer.cancelled = lambda: time.monotonic() - start >= 0.15
    assert finish(root, contract, state).unaddressed
    assert time.monotonic() - start < (4.5 if stopped_by == "batch_budget" else 2)
    assert len(children) == 1 and children[0].poll() is not None
    assert not observer.changed_paths
    assert criterion.status == AcceptanceCriterionStatus.UNVERIFIED
    assert observer.current[str(output).replace("\\", "/")].state == "unavailable"


def test_input_and_preserved_paths_are_not_enrolled(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    input_file = tmp_path / "source.bmp"
    contract = build_acceptance_contract(
        root=root, instruction=f'Inspect "{input_file}". Do not modify "{input_file}".'
    )
    approvals = []
    observer = ArtifactOutputObserver(
        contract, lambda path, initial: approvals.append(path) or True
    )
    assert not approvals and not observer.baseline


@pytest.mark.parametrize(
    "path", ["relative.bmp", "//server/share/file.bmp", r"\\server\share\file.bmp"]
)
def test_nonlocal_paths_are_not_observed(path):
    assert observe_file(path).state == "unavailable"


def test_symlink_is_not_followed(tmp_path):
    target = tmp_path / "target.bmp"
    target.write_bytes(b"synthetic")
    link = tmp_path / "link.bmp"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Host does not permit creating symlinks")
    assert observe_file(str(link)).state == "unavailable"


def test_windows_reparse_point_is_not_followed(tmp_path, monkeypatch):
    from types import SimpleNamespace

    target = tmp_path / "output.bmp"
    target.write_bytes(b"synthetic")
    real_lstat = Path.lstat

    def lstat(path):
        info = real_lstat(path)
        if path == tmp_path:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(Path, "lstat", lstat)
    assert observe_file(str(target)).state == "unavailable"


def test_file_metadata_does_not_pass_content_format_or_expected_literal(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    output = tmp_path / "output.json"
    contract = build_acceptance_contract(
        root=root,
        instruction=f'Save valid JSON to "{output}" with expected output `synthetic verified result`.',
    )
    contract.artifact_output_observer = ArtifactOutputObserver(contract, lambda path, initial: True)
    output.write_text("invalid JSON")
    state = TurnExecutionState(execution_requested=True, acceptance_contract=contract)
    assessment = finish(root, contract, state)
    assert assessment.confirmed and assessment.unaddressed
    formats = [
        c for c in contract.criteria if c.kind == AcceptanceCriterionKind.CONTENT_FORMAT_SCHEMA
    ]
    assert formats and all(c.status != AcceptanceCriterionStatus.PASSED for c in formats)


@pytest.mark.parametrize(
    "mode,allowed,standing,asks",
    [
        ("fullaccess", True, None, 0),
        ("fullaccess", True, "deny", 0),
        ("auto", True, None, 1),
        ("auto", False, None, 1),
    ],
)
def test_metadata_checks_use_existing_permission_authority(tmp_path, mode, allowed, standing, asks):
    from test_approval_review import _store, _Surface

    from alysis_code.agent.tools_assembly import build_tools
    from alysis_code.surface import StandingApproval

    surface = _Surface(
        allow=allowed, standing=StandingApproval(standing, "fixture") if standing else None
    )
    captured = []
    # Use the same assembly path as production, without adding a model-visible tool.
    tools = build_tools(
        root=tmp_path,
        console=None,
        store=_store(tmp_path),
        mode=mode,
        yes=False,
        surface=surface,
        artifact_output_access_sink=captured.append,
    )
    assert "host_file_metadata" not in tools
    path = str(tmp_path.parent / "requested.bmp")
    access = captured[0]
    if not allowed and standing != "deny":
        from alysis_code.agent.errors import ApprovalDeclinedError

        with pytest.raises(ApprovalDeclinedError, match="file_metadata"):
            access(path, True)
    else:
        assert access(path, True) == (allowed and standing != "deny")
    assert len(surface.requests) == asks
    if allowed and standing != "deny":
        assert access(path, False)
        assert len(surface.requests) == asks
        surface.standing = StandingApproval("deny", "revoked")
        assert not access(path, False)


@pytest.mark.parametrize("mode", ["auto", "readonly"])
def test_workspace_metadata_uses_routine_read_admission_and_rechecks_denials(tmp_path, mode):
    from test_approval_review import _store, _Surface

    from alysis_code.agent.tools_assembly import build_tools
    from alysis_code.surface import StandingApproval

    surface = _Surface(allow=False)
    captured = []
    build_tools(
        root=tmp_path,
        console=None,
        store=_store(tmp_path),
        mode=mode,
        yes=False,
        surface=surface,
        artifact_output_access_sink=captured.append,
    )
    access = captured[0]
    path = str(tmp_path / "requested.bmp")
    assert access(path, True)
    assert not surface.requests
    surface.standing = StandingApproval("deny", "revoked")
    assert not access(path, False)
    assert not surface.requests


def test_uninterpreted_external_paths_do_not_request_preflight_access(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    key = tmp_path / "arbitrary-authentication-input"
    instruction = (
        f"In clone create branch qa/test, set local user.signingkey '{key}', commit.gpgsign true."
    )
    contract = build_acceptance_contract(root=root, instruction=instruction)
    assert any(ref.absolute_path and Path(ref.absolute_path) == key for ref in contract.path_refs)
    observed = []
    observer = ArtifactOutputObserver(
        contract,
        lambda path, initial: observed.append(path),
        enroll_external=False,
    )
    assert all(Path(path).is_relative_to(root) for path in observed)
    assert key not in {Path(path) for path in observer.loci_by_path}


def test_exact_metadata_approval_can_use_reviewer_but_cannot_grant_contents(tmp_path):
    from test_approval_review import _reviewer, _Surface

    from alysis_code.agent.action_approval import action_needs_person
    from alysis_code.agent.artifact_output_observation import FileMetadataApproval
    from alysis_code.agent.tools_assembly import build_tools
    from alysis_code.ide.approval_binding import normalize_approval_action
    from alysis_code.surface import ApprovalRequest

    root = tmp_path / "project"
    root.mkdir()
    output = str(tmp_path / "result.bmp")
    reviewer, client, store = _reviewer(
        root, [{"decision": "allow"}], user_message=f'Save "{output}".'
    )
    surface, captured = _Surface(allow=False), []
    build_tools(
        root=root,
        console=None,
        store=store,
        mode="auto",
        yes=False,
        surface=surface,
        approval_reviewer=reviewer,
        artifact_output_access_sink=captured.append,
    )
    assert captured[0](output, True)
    assert client.calls and not surface.requests
    request = FileMetadataApproval(
        kind="fs_read", reason="Metadata only", preview="lstat", files=[output]
    )
    action = normalize_approval_action(request, project_root=root)
    assert action["access"] == "read" and action["risk_level"] == "unknown"
    # Untrusted metadata cannot promote a normal external reader into this host-only capability.
    ordinary = ApprovalRequest(
        kind="fs_read", reason="", preview="", files=[output], metadata={"metadata_only": True}
    )
    assert action_needs_person(ordinary, root)


@pytest.mark.parametrize("save", [True, False])
@pytest.mark.parametrize("gate", [True, False])
@pytest.mark.parametrize("location", ["external", "workspace", "workspace_relative"])
def test_real_agent_loop_verifies_local_save_without_shell_evidence(
    tmp_path, save, gate, location, monkeypatch
):
    from test_agent_loop_one_shot_follow_through import _init_git_repo_with_commit

    from alysis_code.agent_loop import ToolDef, create_session
    from alysis_code.config import AppConfig
    from alysis_code.llm.openai_compat import LLMResponse, ToolCall
    from alysis_code.surface import NoopSurface

    monkeypatch.setenv("ALYSIS_TURN_CONTRACT_V2", "on")
    root = tmp_path / "workspace"
    # Establish a workspace boundary even when pytest's temp dir lives inside
    # another checkout. The output's location must be relative to this exact root.
    _init_git_repo_with_commit(root)
    output = tmp_path / "external save.bmp"
    if location.startswith("workspace"):
        # Git reports this whole directory as untracked before and after a GUI save.
        # Its unchanged coarse status cannot prove the exact new file was written.
        scratch = root / "scratch"
        scratch.mkdir()
        (scratch / "preexisting.txt").write_text("leave this file alone")
        output = scratch / "saved output.bmp"
    session = create_session(
        cfg=AppConfig(
            model="test-model", stream=False, routing_mode="code_only", web_search_mode="off"
        ),
        root=root,
        mode="fullaccess",
        yes=False,
        max_steps=5,
        no_log=False,
        api_key_override="fixture-key",
        surface=NoopSurface(),
        enable_compaction=False,
        verification_enabled=True,
        enable_chat_turn_step_budget=gate,
        session_log_dir_override=tmp_path / "sessions",
    )

    def action(_args):
        if save:
            output.write_bytes(b"synthetic saved document")
        return {"content": [{"type": "text", "text": "Saved successfully"}]}

    tool = ToolDef(
        name="mcp__fixture__desktop",
        description="Fixture desktop action",
        parameters={"type": "object", "properties": {}},
        run=action,
    )
    session.tools[tool.name] = tool
    session.tool_list = [item.as_openai_tool() for item in session.tools.values()]

    class Client:
        model = "test-model"
        temperature = 0.2
        count = 0

        def chat(self, **kwargs):
            self.count += 1
            if self.count == 1:
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCall(id="save", name=tool.name, arguments={})],
                    raw={},
                )
            return LLMResponse(content="Saved the requested document.", tool_calls=[], raw={})

    session.client = Client()
    try:
        assert output.is_relative_to(session.root) == location.startswith("workspace")
        requested = output.relative_to(root) if location == "workspace_relative" else output
        session.run_turn(f'Use the desktop application to save the result to "{requested}".')
        state = session._turn_execution_state
        criterion = next(
            c
            for c in state.acceptance_contract.criteria
            if c.kind == AcceptanceCriterionKind.REQUIRED_ARTIFACT_PATH
        )
        assert (criterion.status == AcceptanceCriterionStatus.PASSED) == save
        final = [
            e["payload"]["content"]
            for e in session.store.events_snapshot()
            if e["type"] == "assistant_message"
        ][-1]
        assessment = state.compute_expectation_assessment(enabled=True, turn_intent="execute")
        assert bool(assessment.confirmed) == save, (
            vars(state.acceptance_contract.artifact_output_observer),
            state.acceptance_contract.task_id,
            state.acceptance_contract.generation,
        )
        if save:
            assert "Not confirmed:" not in final
        assert ("Not verified:" in final) != save
        assert not state.touched_repo_paths
        if not save:
            assert session.last_turn_outcome["outcome"] == "completed_unverified"
    finally:
        session.close()


@pytest.mark.parametrize("quote", ['"', "'", "`"])
def test_quoted_windows_path_is_preserved_exactly(quote):
    path = "C:/Users/Example/My  files/α; and save β.bmp"
    contract = build_acceptance_contract(
        root=Path("C:/workspace"), instruction=f"Save to {quote}{path}{quote}."
    )
    assert len(contract.path_refs) == 1
    assert contract.path_refs[0].absolute_path == path


@pytest.mark.parametrize("identity", [None, "", "relative-python", "//server/python"])
def test_metadata_leaf_requires_native_absolute_runtime_interpreter(
    tmp_path, monkeypatch, identity
):
    import alysis_code.agent.artifact_output_observation as observation

    monkeypatch.setattr(observation.sys, "_base_executable", identity)
    monkeypatch.setattr(
        observation.subprocess, "Popen", lambda *a, **kw: pytest.fail("unidentified interpreter")
    )
    requested = (tmp_path / "output.bmp").as_posix()
    contract = build_acceptance_contract(root=tmp_path, instruction=f'Create "{requested}".')
    assert [ref.absolute_path for ref in contract.path_refs] == [requested]
    observer = ArtifactOutputObserver(contract, lambda *a: True)
    assert observer.loci_by_path == {requested: {"output.bmp"}}
    assert set(observer.baseline) == {requested}
    assert all(item.state == "unavailable" for item in observer.baseline.values())


def test_metadata_worker_is_direct_base_interpreter_without_site_initialization(
    tmp_path, monkeypatch
):
    import json
    import sys

    import alysis_code.agent.artifact_output_observation as observation

    base_executable = sys._base_executable
    real_popen = observation.subprocess.Popen
    children = []
    commands = []

    def report_identity(command, **kwargs):
        commands.append(command)
        assert command[:3] == [base_executable, "-I", "-S"]
        paths = json.loads(command[-1])
        source = (
            "import json, os, sys; "
            "assert sys.flags.no_site and 'site' not in sys.modules; "
            "print(json.dumps({path: {'state': 'file', 'stamp': [os.getpid(), 1, 2, 3, 4]} "
            "for path in json.loads(sys.argv[1])}))"
        )
        child = real_popen([*command[:3], "-c", source, json.dumps(paths)], **kwargs)
        children.append(child)
        return child

    # A workspace/PATH-like executable choice must never replace runtime identity.
    monkeypatch.setattr(observation.sys, "executable", str(tmp_path / "untrusted-launcher"))
    monkeypatch.setattr(observation.subprocess, "Popen", report_identity)
    requested = (tmp_path / "output.bmp").as_posix()
    contract = build_acceptance_contract(root=tmp_path, instruction=f'Create "{requested}".')
    assert [ref.absolute_path for ref in contract.path_refs] == [requested]
    observer = ArtifactOutputObserver(contract, lambda *a: True)
    assert len(commands) == len(children) == 1
    assert children[0].poll() == 0
    assert observer.loci_by_path == {requested: {"output.bmp"}}
    assert set(observer.baseline) == {requested}
    for item in observer.baseline.values():
        assert item.state == "file"
        assert item.stamp[0] == children[0].pid, "the script must run in the owned process itself"


@pytest.mark.parametrize("kill_denied", [False, True])
def test_metadata_leaf_cleanup_keeps_reserve_without_global_inventory(
    tmp_path, monkeypatch, kill_denied
):
    from types import SimpleNamespace

    import alysis_code.agent.artifact_output_observation as observation
    from alysis_code.agent import checkpoint_worker

    now = [0.0]
    inventories = []
    launched = []
    closures = []

    def slow_inventory(_pid):
        # Deterministically model an inventory that consumes the whole reserve.
        # No real sleep or host process enumeration is needed for this regression.
        inventories.append(_pid)
        now[0] += 1.0
        return SimpleNamespace(children=lambda recursive: [])

    class StalledLeaf:
        pid = 101
        returncode = None
        kill_calls = 0

        def poll(self):
            return self.returncode

        def communicate(self, *, timeout):
            now[0] += timeout
            raise observation.subprocess.TimeoutExpired("metadata", timeout)

        def kill(self):
            self.kill_calls += 1
            if kill_denied:
                raise PermissionError("injected metadata kill denial")

        def wait(self, *, timeout):
            if kill_denied or timeout < 0.005:
                now[0] += timeout
                raise observation.subprocess.TimeoutExpired("metadata", timeout)
            now[0] += 0.005
            self.returncode = -9
            return self.returncode

    process = StalledLeaf()
    monkeypatch.setattr(observation.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(checkpoint_worker.psutil, "Process", slow_inventory)
    monkeypatch.setattr(
        observation.subprocess, "Popen", lambda *a, **kw: launched.append(a) or process
    )
    monkeypatch.setattr(
        observation,
        "_close_worker_streams",
        lambda child, *, deferred: closures.append((child, deferred)),
    )
    requested = (tmp_path / "output.bmp").as_posix()
    contract = build_acceptance_contract(root=tmp_path, instruction=f'Create "{requested}".')
    assert [ref.absolute_path for ref in contract.path_refs] == [requested]
    observer = ArtifactOutputObserver(
        contract, lambda *a: True, remaining_seconds=lambda: 0.15 - now[0]
    )
    assert observer.loci_by_path == {requested: {"output.bmp"}}
    assert set(observer.baseline) == {requested}
    assert len(launched) == 1
    assert now[0] <= 0.15
    assert not inventories
    assert process.kill_calls == 1
    assert observer._io_cleanup_pending is kill_denied
    assert (process.poll() is None) is kill_denied
    assert all(item.state == "unavailable" for item in observer.baseline.values())
    assert closures == [(process, True)]
    if kill_denied:
        observer.remaining_seconds = lambda: None
        observer.refresh(contract)
        assert len(launched) == 1, "pending cleanup must block another child even with fresh time"
        assert not observer.changed_paths
        assert set(observer.current) == {requested}
        assert all(item.state == "unavailable" for item in observer.current.values())
