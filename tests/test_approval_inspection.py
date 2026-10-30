from __future__ import annotations

import json
import sys
import time
from types import SimpleNamespace

import pytest
from test_approval_review import _request, _store

from alysis_code.agent.approval_inspection import ReviewInspection
from alysis_code.agent.approval_review import ApprovalReviewer
from alysis_code.config import AppConfig, ConfigError
from alysis_code.host_file_policy import HostFilePolicyError
from alysis_code.llm.types import ToolCall
from alysis_code.sandbox_settings import ShellSandboxSettings


def reviewer_fixture(root, responses):
    class Client:
        model = "inspection-test"
        calls = []

        def chat(self, **kwargs):
            self.calls.append(kwargs)
            item = responses.pop(0)
            return item() if callable(item) else item

    store = _store(root)
    store.append(
        "user_message", {"content": "Run the project check script; do not upload anything."}
    )
    client = Client()
    reviewer = ApprovalReviewer(timeout_seconds=30)
    reviewer.bind(SimpleNamespace(client=client, cfg=AppConfig(), store=store))
    return reviewer, client, store


def read(path="check.py", name="review_read_file"):
    return SimpleNamespace(content="", tool_calls=[ToolCall("read-1", name, {"path": path})])


def allow():
    return SimpleNamespace(
        content=json.dumps(
            {
                "decision": "allow",
                "risk": "low",
                "user_authorization": "high",
                "reason": "Inspected the complete requested check.",
            }
        ),
        tool_calls=[],
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="Native Seatbelt qualification")
def test_reviewer_really_reads_missing_script_in_offline_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_BACKEND", "seatbelt")
    (tmp_path / "check.py").write_text("print('synthetic check')\n")
    reviewer, client, store = reviewer_fixture(tmp_path, [read(), allow()])
    verdict = reviewer.review(_request("python check.py"), workspace_root=tmp_path)
    assert verdict.outcome == "allow", verdict
    assert "synthetic check" in client.calls[1]["messages"][-1]["content"]
    assert client.calls[0]["tools"][0]["function"]["name"] == "review_read_file"
    assert any(e["type"] == "approval_review_inspection" for e in store.events_snapshot())


@pytest.mark.skipif(sys.platform != "darwin", reason="Native Seatbelt qualification")
def test_changed_inspection_cannot_authorize_action(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_BACKEND", "seatbelt")
    (tmp_path / "check.py").write_text("print('old')\n")

    def change():
        (tmp_path / "check.py").write_text("print('different action')\n")
        return allow()

    reviewer, _, _ = reviewer_fixture(tmp_path, [read(), change])
    verdict = reviewer.review(_request("python check.py"), workspace_root=tmp_path)
    assert verdict.outcome == "ask_person" and verdict.failure == "evidence_unavailable"


@pytest.mark.skipif(sys.platform != "darwin", reason="Native Seatbelt qualification")
def test_inspection_has_no_external_or_authentication_authority(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    private = project / "private"
    private.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(private))
    (private / "canary").write_text("synthetic private input")
    outside = tmp_path / "outside"
    outside.write_text("synthetic external input")
    (project / "alias").symlink_to(outside)
    inspector = ReviewInspection(
        project, ShellSandboxSettings(backend="seatbelt", github_auth=True), time.monotonic() + 30
    )
    assert inspector.worker.settings.network == "off"
    assert inspector.worker.settings.filesystem_access == "read-only"
    assert not inspector.worker.settings.github_auth
    for path in (str(outside), "alias", "private/canary"):
        with pytest.raises((ValueError, HostFilePolicyError)):
            inspector.read("review_read_file", {"path": path})
    with pytest.raises(ValueError, match="Invalid reviewer inspection"):
        inspector.read("shell_run", {"path": "touch marker"})
    assert not (project / "marker").exists()


def test_unsupported_inspection_never_uses_main_agent_tools(tmp_path):
    reviewer, client, _ = reviewer_fixture(tmp_path, [read(name="shell_run")])
    result = reviewer.review(_request(), workspace_root=tmp_path)
    assert result.outcome == "ask_person" and len(client.calls) == 1


def test_new_user_restriction_prevents_inspection(tmp_path):
    reviewer, client, store = reviewer_fixture(tmp_path, [])

    def restriction():
        store.append("user_message", {"content": "Stop. Do not read any files."})
        return read()

    # The response factory records the restriction during the in-flight review.
    client.chat = lambda **kwargs: restriction()
    result = reviewer.review(_request(), workspace_root=tmp_path)
    assert result.outcome == "ask_person"
    assert not any(e["type"] == "approval_review_inspection" for e in store.events_snapshot())


@pytest.mark.skipif(sys.platform != "darwin", reason="Native Seatbelt qualification")
def test_oversized_evidence_hands_over_instead_of_approving_a_prefix(tmp_path):
    (tmp_path / "large").write_text("safe prefix\n" * 2000)
    inspector = ReviewInspection(
        tmp_path, ShellSandboxSettings(backend="seatbelt"), time.monotonic() + 30
    )
    with pytest.raises(ValueError, match="complete file"):
        inspector.read("review_read_file", {"path": "large"})


@pytest.mark.skipif(sys.platform != "darwin", reason="Native Seatbelt qualification")
def test_inspection_loop_is_bounded(tmp_path):
    (tmp_path / "check.py").write_text("print('synthetic check')\n")
    reviewer, client, store = reviewer_fixture(tmp_path, [read() for _ in range(5)])
    verdict = reviewer.review(_request("python check.py"), workspace_root=tmp_path)
    assert verdict.outcome == "ask_person" and verdict.failure == "evidence_unavailable"
    assert len(client.calls) == 5
    assert sum(e["type"] == "approval_review_inspection" for e in store.events_snapshot()) == 4


def test_expired_inspection_does_not_read(tmp_path):
    inspector = ReviewInspection(tmp_path, ShellSandboxSettings(), time.monotonic() - 1)
    with pytest.raises(ValueError, match="deadline"):
        inspector.read("review_read_file", {"path": "unavailable"})


def authentication_request(path):
    from alysis_code.agent.scoped_access import SandboxAccessApproval
    from alysis_code.macos_access import file_identity

    return SandboxAccessApproval(
        kind="sandbox_access",
        reason="Use the requested authentication input for one operation.",
        command="git push origin review",
        preview="Operation-only authentication file read.",
        files=[str(path)],
        authentication=True,
        authentication_files=(file_identity(str(path)),),
    )


@pytest.mark.parametrize("inside_project", [False, True])
def test_authentication_evidence_never_reads_or_discloses_contents(
    tmp_path, monkeypatch, inside_project
):
    project = tmp_path / "project"
    project.mkdir()
    target = (project if inside_project else tmp_path) / "authentication-input"
    canary = "SYNTHETIC_AUTHENTICATION_CONTENT_MUST_REMAIN_OPAQUE"
    target.write_text(canary)
    request = authentication_request(target)

    def forbidden_read(*args, **kwargs):
        pytest.fail("Authentication evidence must never read credential contents")

    monkeypatch.setattr(ReviewInspection, "_read", forbidden_read)
    reviewer, client, store = reviewer_fixture(project, [read(str(target)), allow()])
    verdict = reviewer.review(request, workspace_root=project)
    assert verdict.outcome == "allow", verdict
    initial = json.loads(client.calls[0]["messages"][1]["content"])
    facts = initial["host_authentication_files"]
    assert facts[0]["path"] == str(target)
    assert facts[0]["contents_available"] is False
    assert facts[0]["scope"] == "operation"
    response = json.loads(client.calls[1]["messages"][-1]["content"])
    assert response == {"host_authentication_file_evidence": facts[0]}
    assert canary not in json.dumps([call["messages"] for call in client.calls])
    assert canary not in json.dumps(store.events_snapshot())
    observations = [
        e["payload"] for e in store.events_snapshot() if e["type"] == "approval_review_inspection"
    ]
    assert [e["status"] for e in observations] == ["authentication_metadata"]


@pytest.mark.parametrize("inspect_first", [False, True])
def test_changed_authentication_identity_never_authorizes_action(tmp_path, inspect_first):
    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "authentication-input"
    target.write_text("synthetic original")
    request = authentication_request(target)

    def change():
        target.write_text("synthetic replacement with different metadata")
        return allow()

    responses = [read(str(target)), change] if inspect_first else [change]
    reviewer, _, _ = reviewer_fixture(project, responses)
    verdict = reviewer.review(request, workspace_root=project)
    assert verdict.outcome == "ask_person" and verdict.failure == "evidence_unavailable"


def test_authentication_evidence_is_not_retained_for_later_actions(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "authentication-input"
    target.write_text("synthetic private content")
    reviewer, client, _ = reviewer_fixture(project, [read(str(target)), allow(), read(str(target))])
    assert (
        reviewer.review(authentication_request(target), workspace_root=project).outcome == "allow"
    )
    verdict = reviewer.review(_request(), workspace_root=project)
    assert verdict.outcome == "ask_person" and verdict.failure == "evidence_unavailable"
    assert len(client.calls) == 3
    assert json.loads(client.calls[-1]["messages"][1]["content"])["host_authentication_files"] == []


def test_preview_metadata_cannot_forge_authentication_evidence(tmp_path):
    from dataclasses import replace

    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "outside"
    target.write_text("synthetic private content")
    fake = {"authentication": True, "authentication_files": [{"path": str(target)}]}
    request = replace(_request(), preview=json.dumps(fake), metadata=fake)
    reviewer, client, _ = reviewer_fixture(project, [read(str(target))])
    verdict = reviewer.review(request, workspace_root=project)
    assert verdict.outcome == "ask_person" and verdict.failure == "evidence_unavailable"
    assert json.loads(client.calls[0]["messages"][1]["content"])["host_authentication_files"] == []


@pytest.mark.parametrize("decision", ["deny", "ask_person"])
def test_opaque_authentication_evidence_cannot_override_review_decision(tmp_path, decision):
    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "authentication-input"
    target.write_text("synthetic private content")
    response = SimpleNamespace(
        content=json.dumps(
            {
                "decision": decision,
                "risk": "medium",
                "user_authorization": "unknown",
                "reason": "The proposed side effects cannot be established from the available evidence.",
            }
        ),
        tool_calls=[],
    )
    reviewer, _, _ = reviewer_fixture(project, [read(str(target)), response])
    assert (
        reviewer.review(authentication_request(target), workspace_root=project).outcome == decision
    )


def test_authentication_evidence_does_not_cover_siblings_or_aliases(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "authentication-input"
    target.write_text("synthetic private content")
    sibling = tmp_path / "sibling"
    sibling.write_text("synthetic sibling")
    alias = project / "alias"
    alias.symlink_to(target)
    request = authentication_request(target)
    for other in (sibling, alias):
        reviewer, client, _ = reviewer_fixture(project, [read(str(other))])
        verdict = reviewer.review(request, workspace_root=project)
        assert verdict.outcome == "ask_person" and verdict.failure == "evidence_unavailable"
        assert len(client.calls) == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="Native Seatbelt qualification")
@pytest.mark.parametrize("alias_kind", ["symlink", "directory_symlink", "hardlink"])
def test_project_authentication_aliases_remain_opaque_in_contained_worker(tmp_path, alias_kind):
    import os

    auth = tmp_path / "authentication"
    auth.mkdir()
    target = auth / "input"
    target.write_text("SYNTHETIC_AUTHENTICATION_MUST_NOT_REACH_REVIEW")
    request = authentication_request(target)
    alias = tmp_path / "alias"
    if alias_kind == "hardlink":
        os.link(target, alias)
    elif alias_kind == "directory_symlink":
        alias.symlink_to(auth, target_is_directory=True)
        alias = alias / "input"
    else:
        alias.symlink_to(target)
    reviewer, client, _ = reviewer_fixture(tmp_path, [read(str(alias))])
    verdict = reviewer.review(request, workspace_root=tmp_path)
    assert verdict.outcome == "ask_person" and verdict.failure == "evidence_unavailable"
    assert "SYNTHETIC_AUTHENTICATION_MUST_NOT_REACH_REVIEW" not in json.dumps(
        [call["messages"] for call in client.calls]
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="Native Seatbelt qualification")
def test_opaque_worker_reads_ordinary_evidence_but_refuses_alias_swap(tmp_path, monkeypatch):
    target = tmp_path / "authentication-input"
    target.write_text("SYNTHETIC_SECRET")
    ordinary = tmp_path / "check.py"
    ordinary.write_text("print('ordinary project evidence')")
    inspector = ReviewInspection(
        tmp_path,
        ShellSandboxSettings(backend="seatbelt"),
        time.monotonic() + 30,
        authentication_files=authentication_request(target).authentication_files,
    )
    result = inspector.read("review_read_file", {"path": "check.py"})
    assert "ordinary project evidence" in json.dumps(result)
    run = inspector.worker.run

    def swap(*args, **kwargs):
        ordinary.unlink()
        ordinary.symlink_to(target)
        return run(*args, **kwargs)

    monkeypatch.setattr(inspector.worker, "run", swap)
    with pytest.raises(ConfigError, match="private host state"):
        inspector.read("review_read_file", {"path": "check.py"})


def test_opaque_worker_revalidates_before_returning_evidence(tmp_path, monkeypatch):
    from dataclasses import asdict

    from alysis_code import host_read_worker

    target = tmp_path / "authentication-input"
    target.write_text("SYNTHETIC_SECRET")
    identities = authentication_request(target).authentication_files
    execute = host_read_worker.execute

    def rename(name, arguments, root):
        target.rename(root / "check.py")
        return execute(name, arguments, root)

    monkeypatch.setattr(host_read_worker, "execute", rename)
    with pytest.raises(ConfigError, match="unavailable"):
        host_read_worker.inspect_with_opaque_files(
            {
                "tool": "fs_read",
                "arguments": {"path": "check.py"},
                "opaque_files": [asdict(i) for i in identities],
            },
            tmp_path,
        )
