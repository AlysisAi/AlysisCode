from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event, Lock
from time import perf_counter
from typing import Any

import pytest

from alysis_code import agent_loop
from alysis_code.agent.errors import AgentRuntimeError
from alysis_code.agent.read_ledger import SessionReadLedger
from alysis_code.agent_loop import ToolDef, build_tools, create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.session_store import SessionStore
from alysis_code.subagents import SubagentDefinition, built_in_subagents
from alysis_code.surface.noop_surface import NoopSurface
from alysis_code.surface.types import (
    ApprovalDecision,
    ApprovalRequest,
    SubagentEndEvent,
)
from alysis_code.usage_tracker import UsageSummary


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", os.fspath(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "app.txt").write_text("base\n", encoding="utf-8")
    (root / "dirty.txt").write_text("clean\n", encoding="utf-8")
    (root / "protected").mkdir()
    (root / "protected" / "keep.txt").write_text("keep\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=Alysis Code Tests",
        "-c",
        "user.email=tests@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--no-gpg-sign",
        "--no-verify",
        "-qm",
        "initial",
    )
    return root


class _ChildStore:
    def __init__(
        self, session_id: str, final_content: str = "Implemented the requested edit."
    ) -> None:
        self.session_id = session_id
        self.final_content = final_content

    def events_snapshot(self) -> list[dict[str, Any]]:
        return [{"type": "final", "payload": {"content": self.final_content}}]


class _ScriptedClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.calls = 0
        self.call_messages: list[list[dict[str, Any]]] = []

    def chat(self, *, messages: list[dict[str, Any]], **_kwargs: Any) -> LLMResponse:
        index = self.calls
        self.calls += 1
        self.call_messages.append(list(messages))
        return self.responses[min(index, len(self.responses) - 1)]


class _EditingChildSession:
    def __init__(
        self,
        *,
        root: Path,
        edit_path: str | None = "app.txt",
        content: str = "child\n",
        tools: dict[str, ToolDef] | None = None,
    ) -> None:
        self.root = root
        self.edit_path = edit_path
        self.content = content
        self.tools = tools or {
            "fs_read": ToolDef(
                name="fs_read",
                description="read",
                parameters={"type": "object", "properties": {}},
                run=lambda _args: {"ok": True},
            ),
            "fs_write": ToolDef(
                name="fs_write",
                description="write",
                parameters={"type": "object", "properties": {}},
                run=lambda _args: {"ok": True},
            ),
            "shell_run": ToolDef(
                name="shell_run",
                description="diagnose",
                parameters={"type": "object", "properties": {}},
                run=lambda _args: {"ok": True},
            ),
            "verify_run": ToolDef(
                name="verify_run",
                description="verify",
                parameters={"type": "object", "properties": {}},
                run=lambda _args: {"ok": True},
            ),
        }
        self.tool_list = [tool.as_openai_tool() for tool in self.tools.values()]
        self.messages = [{"role": "assistant", "content": "Implemented the requested edit."}]
        self.store = _ChildStore(f"child-{id(self)}")
        self.usage_summary = UsageSummary()
        self.workspace_touched_paths: set[str] = set()
        self.closed = False

    def run_turn(self, _task: str, *, cancellation_token: Any | None = None) -> int:
        _ = cancellation_token
        if self.edit_path is not None:
            target = self.root / self.edit_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.content, encoding="utf-8")
            self.workspace_touched_paths.add(self.edit_path)
        return 0

    def close(self) -> None:
        self.closed = True


class _ApprovalChildSession(_EditingChildSession):
    def __init__(self, *, root: Path, surface: Any) -> None:
        super().__init__(root=root, edit_path=None)
        self.surface = surface

    def run_turn(self, task: str, *, cancellation_token: Any | None = None) -> int:
        _ = cancellation_token
        decision = self.surface.request_approval(
            ApprovalRequest(
                kind="fs_write",
                reason=task,
                preview=task,
                files=["approved.txt"],
            )
        )
        outcome = "allow" if decision.allow else "deny"
        final = f"decision:{task}:{outcome}"
        self.messages = [{"role": "assistant", "content": final}]
        self.store.final_content = final
        return 0


class _SerializedApprovalSurface(NoopSurface):
    def __init__(self) -> None:
        self._state_lock = threading.Lock()
        self.active_requests = 0
        self.max_active_requests = 0
        self.decisions: dict[str, bool] = {}

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        with self._state_lock:
            self.active_requests += 1
            self.max_active_requests = max(
                self.max_active_requests,
                self.active_requests,
            )
        try:
            time.sleep(0.05)
            allow = request.reason == "alpha"
            with self._state_lock:
                self.decisions[request.reason] = allow
            return ApprovalDecision(allow=allow)
        finally:
            with self._state_lock:
                self.active_requests -= 1


class _RecordingSubagentSurface(NoopSurface):
    def __init__(self) -> None:
        self.subagent_ends: list[SubagentEndEvent] = []

    def on_subagent_end(self, event: SubagentEndEvent) -> None:
        self.subagent_ends.append(event)


class _Harness:
    def __init__(
        self,
        *,
        tmp_path: Path,
        root: Path,
        create_child: Callable[..., _EditingChildSession],
        monkeypatch: pytest.MonkeyPatch,
        deny_write_prefixes: list[str] | None = None,
        allow_workspace_writes: bool = True,
        subagent_registry: dict[str, SubagentDefinition] | None = None,
        surface: Any | None = None,
        non_interactive: bool = True,
    ) -> None:
        monkeypatch.setattr(agent_loop, "create_session", create_child)
        self.store = SessionStore(
            enabled=True,
            sessions_dir=tmp_path / "sessions",
            session_id="parent",
            cwd=os.fspath(root),
            repo_root=os.fspath(root),
        )
        self.tools = build_tools(
            root=root,
            console=None,
            store=self.store,
            mode="auto",
            yes=True,
            cfg=AppConfig(model="test-model", web_search_mode="off"),
            api_key="test-key",
            non_interactive=non_interactive,
            surface=surface,
            deny_write_prefixes=deny_write_prefixes,
            subagents_enabled=True,
            subagent_registry=subagent_registry
            or {
                "general": SubagentDefinition(
                    name="general",
                    description="implementation child",
                    system_prompt="Implement the requested change.",
                    mode="auto",
                    allow_tools=("fs_read", "fs_write"),
                    allow_workspace_writes=allow_workspace_writes,
                )
            },
            create_session_factory=create_child,
            runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        )
        self.launcher = self.tools["subagent_run"].run.__self__

    def run(self) -> dict[str, Any]:
        return self.tools["subagent_run"].run(
            {
                "name": "general",
                "task": "Change app.txt from base to child.",
                "workspace_view": "isolated",
            }
        )

    def close(self) -> None:
        scheduler = self.launcher.child_scheduler
        if scheduler is not None:
            scheduler.shutdown(cancel_pending=True)
        self.store.close()


def test_parallel_children_serialize_parent_approval_prompts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    parent_surface = _SerializedApprovalSurface()

    def create_child(**kwargs: Any) -> _ApprovalChildSession:
        return _ApprovalChildSession(
            root=Path(kwargs["root"]),
            surface=kwargs["surface"],
        )

    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=create_child,
        monkeypatch=monkeypatch,
        surface=parent_surface,
        non_interactive=False,
    )
    try:
        scheduler = harness.launcher.child_scheduler
        assert scheduler is not None
        results = scheduler.run_readonly_batch(
            [
                {
                    "name": "general",
                    "task": "alpha",
                    "workspace_view": "isolated",
                },
                {
                    "name": "general",
                    "task": "beta",
                    "workspace_view": "isolated",
                },
            ]
        )

        assert parent_surface.max_active_requests == 1
        assert parent_surface.decisions == {"alpha": True, "beta": False}
        assert [result["result"] for result in results] == [
            "decision:alpha:allow",
            "decision:beta:deny",
        ]
    finally:
        harness.close()


def test_isolated_general_edits_worktree_not_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    child_roots: list[Path] = []

    def create_child(**kwargs: Any) -> _EditingChildSession:
        child_root = Path(kwargs["root"])
        child_roots.append(child_root)
        return _EditingChildSession(root=child_root)

    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=create_child,
        monkeypatch=monkeypatch,
    )
    try:
        result = harness.run()

        assert result["workspace"]["view"] == "isolated"
        assert result["patch_summary"]["files"] == ["app.txt"]
        assert "patch" not in result
        assert (root / "app.txt").read_text(encoding="utf-8") == "base\n"
        assert child_roots[0] != root
        assert (child_roots[0] / "app.txt").read_text(encoding="utf-8") == "child\n"
    finally:
        harness.close()


def test_identical_isolated_results_share_one_material_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=lambda **kwargs: _EditingChildSession(root=Path(kwargs["root"])),
        monkeypatch=monkeypatch,
    )
    try:
        first = harness.run()
        duplicate = harness.run()
        provider = harness.launcher.workspace_provider
        assert provider is not None
        first_record = provider.get(first["run_id"])
        duplicate_record = provider.get(duplicate["run_id"])

        assert first_record is not None and first_record.state == "captured"
        assert first_record.worktree_path.exists() is True
        assert duplicate_record is not None and duplicate_record.state == "duplicate"
        assert duplicate_record.worktree_path.exists() is False
        assert duplicate["semantic_no_progress"] is True
        assert duplicate["duplicate_of"] == first["run_id"]
        assert duplicate["canonical_run_id"] == first["run_id"]
        assert duplicate["already_integrated"] is False
        assert duplicate["candidate_worktree_retained"] is False
        assert duplicate["physical_worktree_removed"] is True
        assert duplicate["material_identity"] == first["material_identity"]
        assert duplicate["material_identity_sha256"] == first["material_identity_sha256"]
        assert (
            duplicate["patch_summary"]["patch_artifact"] != first["patch_summary"]["patch_artifact"]
        )
        assert harness.launcher.child_scheduler.unapplied_isolated_results() == [
            {
                "run_id": first["run_id"],
                "files": ["app.txt"],
                "insertions": 1,
                "deletions": 1,
            }
        ]
        worktrees = _git(root, "worktree", "list", "--porcelain")
        assert sum(line.startswith("worktree ") for line in worktrees.splitlines()) == 2

        pinned = provider.acquire_pin(duplicate["run_id"], consumer_run_id="reader")
        assert pinned["ok"] is True
        assert pinned["run_id"] == first["run_id"]
        assert pinned["requested_run_id"] == duplicate["run_id"]
        provider.release_pin(duplicate["run_id"], consumer_run_id="reader")
        assert provider.get(first["run_id"]).pinned_by_run_ids == ()  # type: ignore[union-attr]

        discarded = harness.tools["subagent_discard"].run({"run_id": duplicate["run_id"]})
        assert discarded["ok"] is True
        assert discarded["semantic_no_progress"] is True
        assert discarded["canonical_state"] == "captured"
        assert provider.get(first["run_id"]).state == "captured"  # type: ignore[union-attr]
        assert first_record.worktree_path.exists() is True

        duplicate_events = [
            event["payload"]
            for event in harness.store.events_snapshot()
            if event.get("type") == "subagent_workspace"
            and event.get("payload", {}).get("action") == "duplicate"
        ]
        assert len(duplicate_events) == 1
        assert duplicate_events[0]["duplicate_of"] == first["run_id"]
        assert duplicate_events[0]["semantic_no_progress"] is True
    finally:
        harness.close()


def test_discarded_duplicate_alias_is_terminal_but_canonical_remains_applicable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=lambda **kwargs: _EditingChildSession(root=Path(kwargs["root"])),
        monkeypatch=monkeypatch,
    )
    try:
        canonical = harness.run()
        duplicate = harness.run()
        provider = harness.launcher.workspace_provider
        assert provider is not None

        discarded = harness.tools["subagent_discard"].run({"run_id": duplicate["run_id"]})

        assert discarded["ok"] is True
        assert discarded["duplicate_of"] == canonical["run_id"]
        assert discarded["canonical_state"] == "captured"
        discarded_record = provider.get(duplicate["run_id"])
        assert discarded_record is not None
        assert discarded_record.state == "discarded"
        assert discarded_record.duplicate_of == canonical["run_id"]

        repeated_discard = harness.tools["subagent_discard"].run({"run_id": duplicate["run_id"]})
        stale_apply = harness.tools["subagent_apply"].run({"run_id": duplicate["run_id"]})

        assert repeated_discard["ok"] is False
        assert repeated_discard["error_code"] == "workspace_already_discarded"
        assert stale_apply["ok"] is False
        assert stale_apply["error_code"] == "workspace_already_discarded"
        canonical_record = provider.get(canonical["run_id"])
        assert canonical_record is not None
        assert canonical_record.state == "captured"
        assert canonical_record.worktree_path.exists() is True

        applied = harness.tools["subagent_apply"].run({"run_id": canonical["run_id"]})
        assert applied["ok"] is True
        assert applied["applied_paths"] == ["app.txt"]
        assert (root / "app.txt").read_text(encoding="utf-8") == "child\n"
    finally:
        harness.close()


def test_duplicate_cleanup_failure_is_retried_at_provider_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=lambda **kwargs: _EditingChildSession(root=Path(kwargs["root"])),
        monkeypatch=monkeypatch,
    )
    try:
        canonical = harness.run()
        provider = harness.launcher.workspace_provider
        assert provider is not None
        original_remove = provider._remove_worktree
        attempts = 0

        def fail_once(worktree_path: Path) -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return "simulated duplicate cleanup failure"
            return original_remove(worktree_path)

        monkeypatch.setattr(provider, "_remove_worktree", fail_once)
        duplicate = harness.run()
        duplicate_record = provider.get(duplicate["run_id"])

        assert duplicate["duplicate_of"] == canonical["run_id"]
        assert duplicate["cleanup_pending"] is True
        assert duplicate["cleanup_warning"] == "simulated duplicate cleanup failure"
        assert duplicate["physical_worktree_removed"] is False
        assert duplicate["candidate_worktree_retained"] is True
        assert duplicate_record is not None
        assert duplicate_record.state == "duplicate"
        assert duplicate_record.cleanup_pending is True
        assert duplicate_record.worktree_path.exists() is True
        assert [
            result["run_id"]
            for result in harness.launcher.child_scheduler.unapplied_isolated_results()
        ] == [canonical["run_id"]]

        provider.close()

        cleaned = provider.get(duplicate["run_id"])
        assert cleaned is not None
        assert cleaned.cleanup_pending is False
        assert cleaned.worktree_path.exists() is False
        assert (
            sum(
                line.startswith("worktree ")
                for line in _git(root, "worktree", "list", "--porcelain").splitlines()
            )
            == 1
        )
        actions = [
            event["payload"]["action"]
            for event in harness.store.events_snapshot()
            if event.get("type") == "subagent_workspace"
        ]
        assert actions[-3:] == ["cleanup_failed", "discarded", "cleanup_completed"]
    finally:
        harness.close()


def test_duplicate_alias_actions_report_and_retry_pending_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=lambda **kwargs: _EditingChildSession(root=Path(kwargs["root"])),
        monkeypatch=monkeypatch,
    )
    try:
        canonical = harness.run()
        provider = harness.launcher.workspace_provider
        assert provider is not None
        original_remove = provider._remove_worktree
        attempts = 0

        def fail_first_cleanup(worktree_path: Path) -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return "simulated duplicate cleanup failure"
            return original_remove(worktree_path)

        monkeypatch.setattr(provider, "_remove_worktree", fail_first_cleanup)
        duplicate = harness.run()

        assert duplicate["cleanup_pending"] is True
        inspected = provider.inspect_pin_source(duplicate["run_id"])
        assert inspected["ok"] is True
        assert inspected["cleanup_pending"] is True
        assert inspected["cleanup_warning"] == "simulated duplicate cleanup failure"
        assert inspected["physical_worktree_removed"] is False

        applied = harness.tools["subagent_apply"].run({"run_id": duplicate["run_id"]})

        assert applied["ok"] is True
        assert applied["canonical_run_id"] == canonical["run_id"]
        assert applied["cleanup_pending"] is True
        assert applied["cleanup_warning"] == "simulated duplicate cleanup failure"
        assert applied["physical_worktree_removed"] is False
        assert (root / "app.txt").read_text(encoding="utf-8") == "child\n"

        discarded = harness.tools["subagent_discard"].run({"run_id": duplicate["run_id"]})

        assert discarded["ok"] is True
        assert discarded["cleanup_pending"] is False
        assert discarded["physical_worktree_removed"] is True
        assert "cleanup_warning" not in discarded
        duplicate_record = provider.get(duplicate["run_id"])
        assert duplicate_record is not None
        assert duplicate_record.cleanup_pending is False
        assert duplicate_record.worktree_path.exists() is False
        cleanup_events = [
            event["payload"]
            for event in harness.store.events_snapshot()
            if event.get("type") == "subagent_workspace"
            and event.get("payload", {}).get("action") in {"cleanup_failed", "cleanup_completed"}
            and event.get("payload", {}).get("run_id") == duplicate["run_id"]
        ]
        assert [event["action"] for event in cleanup_events] == [
            "cleanup_failed",
            "cleanup_completed",
        ]
        assert cleanup_events[-1]["retry"] == "explicit_discard"
    finally:
        harness.close()


def test_duplicate_apply_reports_failed_canonical_worktree_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=lambda **kwargs: _EditingChildSession(root=Path(kwargs["root"])),
        monkeypatch=monkeypatch,
    )
    try:
        canonical = harness.run()
        duplicate = harness.run()
        provider = harness.launcher.workspace_provider
        assert provider is not None
        canonical_record = provider.get(canonical["run_id"])
        assert canonical_record is not None
        original_remove = provider._remove_worktree

        def fail_canonical_cleanup(worktree_path: Path) -> str:
            if worktree_path == canonical_record.worktree_path:
                return "simulated canonical cleanup failure"
            return original_remove(worktree_path)

        monkeypatch.setattr(provider, "_remove_worktree", fail_canonical_cleanup)

        applied = harness.tools["subagent_apply"].run({"run_id": duplicate["run_id"]})

        assert applied["ok"] is True
        assert applied["applied_via_canonical"] is True
        assert applied["cleanup_pending"] is True
        assert applied["cleanup_pending_run_ids"] == [canonical["run_id"]]
        assert applied["cleanup_warning"] == "simulated canonical cleanup failure"
        assert applied["physical_worktree_removed"] is False
        discarded_alias = harness.tools["subagent_discard"].run({"run_id": duplicate["run_id"]})
        assert discarded_alias["cleanup_pending"] is True
        assert discarded_alias["cleanup_pending_run_ids"] == [canonical["run_id"]]
        assert discarded_alias["physical_worktree_removed"] is False

        monkeypatch.setattr(provider, "_remove_worktree", original_remove)
        recovered = provider.close()
        assert recovered["ok"] is True
        recovered_canonical = provider.get(canonical["run_id"])
        assert recovered_canonical is not None
        assert recovered_canonical.cleanup_pending is False
        assert recovered_canonical.worktree_path.exists() is False
    finally:
        harness.close()


def test_applying_duplicate_uses_canonical_candidate_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=lambda **kwargs: _EditingChildSession(root=Path(kwargs["root"])),
        monkeypatch=monkeypatch,
    )
    try:
        canonical = harness.run()
        duplicate = harness.run()

        applied = harness.tools["subagent_apply"].run({"run_id": duplicate["run_id"]})

        assert applied["ok"] is True
        assert applied["run_id"] == duplicate["run_id"]
        assert applied["canonical_run_id"] == canonical["run_id"]
        assert applied["duplicate_of"] == canonical["run_id"]
        assert applied["applied_via_canonical"] is True
        assert applied["semantic_no_progress"] is False
        assert applied["applied_paths"] == ["app.txt"]
        assert (root / "app.txt").read_text(encoding="utf-8") == "child\n"
        assert harness.launcher.child_scheduler.unapplied_isolated_results() == []
        assert (
            sum(
                line.startswith("worktree ")
                for line in _git(root, "worktree", "list", "--porcelain").splitlines()
            )
            == 1
        )
    finally:
        harness.close()


def test_rerun_starts_from_applied_parent_state_and_reports_no_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=lambda **kwargs: _EditingChildSession(root=Path(kwargs["root"])),
        monkeypatch=monkeypatch,
    )
    try:
        canonical = harness.run()
        assert harness.tools["subagent_apply"].run({"run_id": canonical["run_id"]})["ok"] is True

        unchanged = harness.run()

        assert unchanged["semantic_no_progress"] is True
        assert unchanged["status"] == "no_changes"
        assert unchanged["workspace"]["no_changes"] is True
        assert unchanged["patch_summary"]["files"] == []
        assert (root / "app.txt").read_text(encoding="utf-8") == "child\n"
        assert harness.launcher.child_scheduler.unapplied_isolated_results() == []
        assert (
            sum(
                line.startswith("worktree ")
                for line in _git(root, "worktree", "list", "--porcelain").splitlines()
            )
            == 1
        )
    finally:
        harness.close()


def test_reverted_applied_result_becomes_a_fresh_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=lambda **kwargs: _EditingChildSession(root=Path(kwargs["root"])),
        monkeypatch=monkeypatch,
    )
    try:
        applied_result = harness.run()
        assert (
            harness.tools["subagent_apply"].run({"run_id": applied_result["run_id"]})["ok"] is True
        )
        (root / "app.txt").write_text("base\n", encoding="utf-8")

        fresh = harness.run()

        assert "duplicate_of" not in fresh
        assert fresh["material_identity_sha256"] == applied_result["material_identity_sha256"]
        assert harness.launcher.child_scheduler.unapplied_isolated_results() == [
            {
                "run_id": fresh["run_id"],
                "files": ["app.txt"],
                "insertions": 1,
                "deletions": 1,
            }
        ]
        assert (
            sum(
                line.startswith("worktree ")
                for line in _git(root, "worktree", "list", "--porcelain").splitlines()
            )
            == 2
        )
        invalidated = [
            event["payload"]
            for event in harness.store.events_snapshot()
            if event.get("type") == "subagent_workspace"
            and event.get("payload", {}).get("action") == "integration_invalidated"
        ]
        assert [event["run_id"] for event in invalidated] == [applied_result["run_id"]]
    finally:
        harness.close()


def test_different_isolated_patches_remain_distinct_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    contents = iter(("first child\n", "second child\n"))
    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=lambda **kwargs: _EditingChildSession(
            root=Path(kwargs["root"]),
            content=next(contents),
        ),
        monkeypatch=monkeypatch,
    )
    try:
        first = harness.run()
        second = harness.run()

        assert "duplicate_of" not in first
        assert "duplicate_of" not in second
        assert first["material_identity_sha256"] != second["material_identity_sha256"]
        assert len(harness.launcher.child_scheduler.unapplied_isolated_results()) == 2
        assert (
            sum(
                line.startswith("worktree ")
                for line in _git(root, "worktree", "list", "--porcelain").splitlines()
            )
            == 3
        )
    finally:
        harness.close()


def test_same_patch_against_changed_base_is_a_distinct_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=lambda **kwargs: _EditingChildSession(root=Path(kwargs["root"])),
        monkeypatch=monkeypatch,
    )
    try:
        first = harness.run()
        (root / "dirty.txt").write_text("new base\n", encoding="utf-8")
        _git(root, "add", "dirty.txt")
        _git(
            root,
            "-c",
            "user.name=Alysis Code Tests",
            "-c",
            "user.email=tests@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--no-gpg-sign",
            "--no-verify",
            "-qm",
            "advance base",
        )
        second = harness.run()

        assert first["patch_summary"]["sha256"] == second["patch_summary"]["sha256"]
        assert (
            first["material_identity"]["base_commit"] != second["material_identity"]["base_commit"]
        )
        assert first["material_identity_sha256"] != second["material_identity_sha256"]
        assert "duplicate_of" not in second
        assert len(harness.launcher.child_scheduler.unapplied_isolated_results()) == 2
        assert (
            sum(
                line.startswith("worktree ")
                for line in _git(root, "worktree", "list", "--porcelain").splitlines()
            )
            == 3
        )
    finally:
        harness.close()


def test_capture_apply_roundtrip_is_uncommitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=lambda **kwargs: _EditingChildSession(root=Path(kwargs["root"])),
        monkeypatch=monkeypatch,
    )
    try:
        head_before = _git(root, "rev-parse", "HEAD")
        run_result = harness.run()
        record = harness.launcher.workspace_provider.get(run_result["run_id"])
        assert record is not None and record.worktree_path.exists()

        applied = harness.tools["subagent_apply"].run({"run_id": run_result["run_id"]})

        assert applied["ok"] is True
        assert applied["applied_paths"] == ["app.txt"]
        assert (root / "app.txt").read_text(encoding="utf-8") == "child\n"
        assert _git(root, "rev-parse", "HEAD") == head_before
        assert _git(root, "status", "--short", "--", "app.txt") == "M app.txt"
        assert record.worktree_path.exists() is False
        lifecycle = []
        for event in harness.store.events_snapshot():
            event_type = str(event.get("type") or "")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if event_type == "subagent_workspace":
                lifecycle.append(f"{event_type}:{payload.get('action')}")
            elif event_type == "subagent_state":
                lifecycle.append(f"{event_type}:{payload.get('state')}")
            elif event_type in {"subagent_start", "subagent_tool_catalog", "subagent_end"}:
                lifecycle.append(event_type)
        assert lifecycle == [
            "subagent_state:spawned",
            "subagent_workspace:prepared",
            "subagent_state:running",
            "subagent_start",
            "subagent_tool_catalog",
            "subagent_workspace:captured",
            "subagent_end",
            "subagent_state:joined",
            "subagent_workspace:applied",
        ]
        assert (
            harness.tools["subagent_apply"].run({"run_id": run_result["run_id"]})["error_code"]
            == "workspace_already_applied"
        )
    finally:
        harness.close()


def test_apply_remains_successful_when_worktree_cleanup_is_deferred(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=lambda **kwargs: _EditingChildSession(root=Path(kwargs["root"])),
        monkeypatch=monkeypatch,
    )
    try:
        run_result = harness.run()
        provider = harness.launcher.workspace_provider
        record = provider.get(run_result["run_id"])
        assert record is not None
        original_remove = provider._remove_worktree
        attempts = 0

        def fail_once(worktree_path: Path) -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return "simulated worktree cleanup failure"
            return original_remove(worktree_path)

        monkeypatch.setattr(provider, "_remove_worktree", fail_once)

        applied = harness.tools["subagent_apply"].run({"run_id": run_result["run_id"]})

        assert applied["ok"] is True
        assert applied["cleanup_pending"] is True
        assert applied["cleanup_warning"] == "simulated worktree cleanup failure"
        assert (root / "app.txt").read_text(encoding="utf-8") == "child\n"
        assert record.worktree_path.exists() is True
        updated = provider.get(run_result["run_id"])
        assert updated is not None
        assert updated.state == "applied"
        assert updated.cleanup_pending is True
        assert provider.unapplied_summaries() == []
        assert (
            harness.tools["subagent_apply"].run({"run_id": run_result["run_id"]})["error_code"]
            == "workspace_already_applied"
        )

        provider.close()

        cleaned = provider.get(run_result["run_id"])
        assert cleaned is not None
        assert cleaned.cleanup_pending is False
        assert record.worktree_path.exists() is False
        actions = [
            event["payload"]["action"]
            for event in harness.store.events_snapshot()
            if event.get("type") == "subagent_workspace"
        ]
        assert actions[-3:] == ["applied", "cleanup_failed", "cleanup_completed"]
    finally:
        harness.close()


def test_apply_conflict_changes_nothing_and_retains_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=lambda **kwargs: _EditingChildSession(root=Path(kwargs["root"])),
        monkeypatch=monkeypatch,
    )
    try:
        run_result = harness.run()
        record = harness.launcher.workspace_provider.get(run_result["run_id"])
        assert record is not None
        (root / "app.txt").write_text("parent\n", encoding="utf-8")

        conflict = harness.tools["subagent_apply"].run({"run_id": run_result["run_id"]})

        assert conflict["error_code"] == "merge_conflict"
        assert conflict["conflicting_paths"] == ["app.txt"]
        assert conflict["patch_artifact"] == run_result["patch_summary"]["patch_artifact"]
        assert (root / "app.txt").read_text(encoding="utf-8") == "parent\n"
        assert record.worktree_path.exists() is True
        events = harness.store.events_snapshot()
        actions = [
            event["payload"]["action"]
            for event in events
            if event.get("type") == "subagent_workspace"
        ]
        assert actions[-1] == "conflict"
        harness.tools["subagent_discard"].run({"run_id": run_result["run_id"]})
    finally:
        harness.close()


def test_discard_and_apply_errors_are_structured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    edit_path: str | None = "app.txt"

    def create_child(**kwargs: Any) -> _EditingChildSession:
        return _EditingChildSession(root=Path(kwargs["root"]), edit_path=edit_path)

    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=create_child,
        monkeypatch=monkeypatch,
    )
    try:
        run_result = harness.run()
        record = harness.launcher.workspace_provider.get(run_result["run_id"])
        assert record is not None
        discarded = harness.tools["subagent_discard"].run({"run_id": run_result["run_id"]})
        assert discarded["ok"] is True
        assert record.worktree_path.exists() is False
        assert (root / "app.txt").read_text(encoding="utf-8") == "base\n"
        assert (
            harness.tools["subagent_apply"].run({"run_id": run_result["run_id"]})["error_code"]
            == "workspace_already_discarded"
        )
        assert (
            harness.tools["subagent_apply"].run({"run_id": "unknown"})["error_code"]
            == "unknown_workspace_run"
        )

        edit_path = None
        empty_result = harness.run()
        assert empty_result["patch_summary"]["files"] == []
        assert empty_result["workspace"]["no_changes"] is True
        assert empty_result["status"] == "no_changes"
        assert empty_result["semantic_no_progress"] is True
        assert "error" not in empty_result
        assert "failure_category" not in empty_result
        assert "final_report_problem" not in empty_result
        assert harness.launcher.child_scheduler.unapplied_isolated_results() == []
        assert (
            harness.tools["subagent_apply"].run({"run_id": empty_result["run_id"]})["error_code"]
            == "no_changes"
        )
    finally:
        harness.close()


def test_no_change_is_host_derived_and_auto_released(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)

    def create_child(**kwargs: Any) -> _EditingChildSession:
        child = _EditingChildSession(root=Path(kwargs["root"]), edit_path=None)
        final = "The requested behavior already exists."
        child.messages = [{"role": "assistant", "content": final}]
        child.store.final_content = final
        return child

    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=create_child,
        monkeypatch=monkeypatch,
    )
    try:
        result = harness.run()
        record = harness.launcher.workspace_provider.get(result["run_id"])

        assert "error" not in result
        assert result["status"] == "no_changes"
        assert result["workspace"]["no_changes"] is True
        assert result["semantic_no_progress"] is True
        assert result["candidate_worktree_retained"] is False
        assert result["physical_worktree_removed"] is True
        assert record is not None and record.state == "discarded"
        assert record.worktree_path.exists() is False
        assert harness.launcher.child_scheduler.unapplied_isolated_results() == []
        assert (
            harness.tools["subagent_apply"].run({"run_id": result["run_id"]})["error_code"]
            == "no_changes"
        )
        workspace_events = [
            event["payload"]
            for event in harness.store.events_snapshot()
            if event.get("type") == "subagent_workspace"
        ]
        assert workspace_events[-1]["action"] == "discarded"
        assert workspace_events[-1]["reason"] == "no_changes"
    finally:
        harness.close()


def test_session_close_discards_unapplied_worktree(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    session = create_session(
        cfg=AppConfig(model="test-model", web_search_mode="off"),
        root=root,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="test-key",
        session_log_dir_override=tmp_path / "session-close",
        subagents_enabled=True,
    )
    launcher = session.tools["subagent_run"].run.__self__
    provider = launcher.workspace_provider
    assert provider is not None
    prepared = provider.prepare("unapplied")
    worktree_path = Path(prepared["worktree_path"])
    assert worktree_path.exists()

    session.close()

    assert worktree_path.exists() is False


def test_provider_close_records_failed_release_and_recovers_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=lambda **kwargs: _EditingChildSession(root=Path(kwargs["root"])),
        monkeypatch=monkeypatch,
    )
    try:
        result = harness.run()
        provider = harness.launcher.workspace_provider
        assert provider is not None
        original_remove = provider._remove_worktree
        monkeypatch.setattr(
            provider,
            "_remove_worktree",
            lambda _path: "simulated close release failure",
        )

        failed = provider.close()

        record = provider.get(result["run_id"])
        assert failed == {
            "ok": False,
            "released_run_ids": [],
            "cleanup_completed_run_ids": [],
            "cleanup_pending_run_ids": [result["run_id"]],
            "failures": [
                {
                    "run_id": result["run_id"],
                    "error": "simulated close release failure",
                    "error_code": "workspace_release_failed",
                }
            ],
        }
        assert record is not None
        assert record.state == "captured"
        assert record.cleanup_pending is True
        assert record.cleanup_error == "simulated close release failure"
        assert record.worktree_path.exists() is True
        failures = [
            event["payload"]
            for event in harness.store.events_snapshot()
            if event.get("type") == "subagent_workspace"
            and event.get("payload", {}).get("action") == "cleanup_failed"
        ]
        assert failures[-1] == {
            "run_id": result["run_id"],
            "action": "cleanup_failed",
            "release_action": "discarded",
            "error": "simulated close release failure",
            "error_code": "workspace_release_failed",
            "retry": "session_close",
        }
        cleanup_summaries = [
            event["payload"]
            for event in harness.store.events_snapshot()
            if event.get("type") == "subagent_workspace_cleanup_summary"
        ]
        assert cleanup_summaries[-1] == failed

        monkeypatch.setattr(provider, "_remove_worktree", original_remove)
        recovered = provider.close()

        assert recovered == {
            "ok": True,
            "released_run_ids": [result["run_id"]],
            "cleanup_completed_run_ids": [],
            "cleanup_pending_run_ids": [],
            "failures": [],
        }
        recovered_record = provider.get(result["run_id"])
        assert recovered_record is not None
        assert recovered_record.state == "discarded"
        assert recovered_record.cleanup_pending is False
        assert recovered_record.cleanup_error == ""
        assert recovered_record.worktree_path.exists() is False
        cleanup_summaries = [
            event["payload"]
            for event in harness.store.events_snapshot()
            if event.get("type") == "subagent_workspace_cleanup_summary"
        ]
        assert cleanup_summaries[-1] == recovered
    finally:
        harness.close()


def test_coordinator_cleanup_retry_works_after_store_callback_closes_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=lambda **kwargs: _EditingChildSession(root=Path(kwargs["root"])),
        monkeypatch=monkeypatch,
    )
    try:
        result = harness.run()
        coordinator = harness.launcher.child_scheduler
        provider = harness.launcher.workspace_provider
        assert coordinator is not None
        assert provider is not None
        original_remove = provider._remove_worktree
        monkeypatch.setattr(
            provider,
            "_remove_worktree",
            lambda _path: "simulated first cleanup failure",
        )
        coordinator.run_after_shutdown_cleanup(harness.store.close)

        coordinator.shutdown(cancel_pending=True, wait_for_running=True)

        assert harness.store._fh is None  # noqa: SLF001
        failed_status = coordinator.workspace_cleanup_status()
        assert failed_status["attempted"] is True
        assert failed_status["succeeded"] is False
        assert failed_status["summary"]["cleanup_pending_run_ids"] == [result["run_id"]]
        retained = provider.get(result["run_id"])
        assert retained is not None
        assert retained.cleanup_pending is True
        assert retained.worktree_path.exists() is True

        monkeypatch.setattr(provider, "_remove_worktree", original_remove)
        retried = coordinator.retry_workspace_cleanup()

        assert retried["ok"] is True
        assert retried["cleanup_pending_run_ids"] == []
        recovered_status = coordinator.workspace_cleanup_status()
        assert recovered_status["succeeded"] is True
        assert recovered_status["attempt_count"] == 2
        recovered = provider.get(result["run_id"])
        assert recovered is not None
        assert recovered.state == "discarded"
        assert recovered.cleanup_pending is False
        assert recovered.worktree_path.exists() is False
    finally:
        harness.close()


def test_non_git_isolated_view_returns_structured_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "plain"
    root.mkdir()

    def unexpected_child(**_kwargs: Any) -> _EditingChildSession:
        raise AssertionError("non-git isolated work must not launch a child session")

    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=unexpected_child,
        monkeypatch=monkeypatch,
    )
    try:
        result = harness.run()

        assert result["error_code"] == "isolated_workspace_requires_git"
        assert result["error"] == "workspace_view=isolated requires a git repository"
        assert result["workspace"] == {"view": "isolated"}
    finally:
        harness.close()


def test_deny_prefix_is_enforced_inside_isolated_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    observed: dict[str, Any] = {}

    def create_child(**kwargs: Any) -> _EditingChildSession:
        child_root = Path(kwargs["root"])
        child_store = SessionStore(
            enabled=False,
            sessions_dir=tmp_path / "child-sessions",
            session_id="deny-child",
            cwd=os.fspath(child_root),
            repo_root=os.fspath(child_root),
        )
        child_tools = build_tools(
            root=child_root,
            console=None,
            store=child_store,
            mode="auto",
            yes=True,
            cfg=kwargs["cfg"],
            api_key="test-key",
            non_interactive=True,
            deny_write_prefixes=kwargs["deny_write_prefixes"],
            subagents_enabled=False,
            runtime_kind=RuntimeKind.SUBAGENT,
        )
        with pytest.raises(AgentRuntimeError, match="Blocked write to protected path"):
            child_tools["fs_write"].run({"path": "protected/blocked.txt", "content": "blocked\n"})
        observed["root"] = child_root
        observed["deny_write_prefixes"] = kwargs["deny_write_prefixes"]
        child_store.close()
        return _EditingChildSession(root=child_root, edit_path=None, tools=child_tools)

    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=create_child,
        monkeypatch=monkeypatch,
        deny_write_prefixes=["protected"],
    )
    try:
        result = harness.run()

        assert observed["root"] != root
        assert "protected" in observed["deny_write_prefixes"]
        assert not (observed["root"] / "protected" / "blocked.txt").exists()
        assert not (root / "protected" / "blocked.txt").exists()
        harness.tools["subagent_discard"].run({"run_id": result["run_id"]})
    finally:
        harness.close()


def test_parent_dirty_paths_are_reported_in_result_and_prepare_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    (root / "dirty.txt").write_text("parent dirty\n", encoding="utf-8")
    (root / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=lambda **kwargs: _EditingChildSession(root=Path(kwargs["root"])),
        monkeypatch=monkeypatch,
    )
    try:
        result = harness.run()

        assert result["workspace"]["parent_dirty_paths"] == ["dirty.txt", "untracked.txt"]
        prepared = [
            event["payload"]
            for event in harness.store.events_snapshot()
            if event.get("type") == "subagent_workspace"
            and event.get("payload", {}).get("action") == "prepared"
        ]
        assert prepared[-1]["parent_dirty_paths"] == ["dirty.txt", "untracked.txt"]
        assert (
            harness.launcher.workspace_provider.get(result["run_id"]).worktree_path / "dirty.txt"
        ).read_text(encoding="utf-8") == "parent dirty\n"
        harness.tools["subagent_discard"].run({"run_id": result["run_id"]})
    finally:
        harness.close()


def test_non_editing_role_mutation_degrades_inside_isolated_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=lambda **kwargs: _EditingChildSession(root=Path(kwargs["root"])),
        monkeypatch=monkeypatch,
        allow_workspace_writes=False,
    )
    try:
        result = harness.run()

        assert result["status"] == "degraded"
        assert result["error_code"] == "unexpected_workspace_mutation"
        assert result["material_touched_repo_paths"] == ["app.txt"]
        assert result["patch_summary"]["files"] == ["app.txt"]
        assert (root / "app.txt").read_text(encoding="utf-8") == "base\n"
        harness.tools["subagent_discard"].run({"run_id": result["run_id"]})
    finally:
        harness.close()


def test_workspace_action_tools_respect_kill_switch(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    for runtime_kind, isolation_enabled in ((RuntimeKind.INTERACTIVE_CHAT, False),):
        store = SessionStore(
            enabled=False,
            sessions_dir=tmp_path / f"sessions-{runtime_kind.value}",
            session_id=f"parent-{runtime_kind.value}",
            cwd=os.fspath(root),
            repo_root=os.fspath(root),
        )
        cfg = AppConfig(model="test-model", web_search_mode="off")
        cfg.subagent_orchestration.workspace_isolation_enabled = isolation_enabled
        tools = build_tools(
            root=root,
            console=None,
            store=store,
            mode="auto",
            yes=True,
            cfg=cfg,
            api_key="test-key",
            subagents_enabled=True,
            subagent_registry={},
            runtime_kind=runtime_kind,
        )
        try:
            assert "subagent_apply" not in tools
            assert "subagent_discard" not in tools
        finally:
            launcher = tools["subagent_run"].run.__self__
            launcher.child_scheduler.shutdown(cancel_pending=True)
            store.close()


def test_background_isolated_spawn_respects_disabled_launcher_snapshot(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    store = SessionStore(
        enabled=False,
        sessions_dir=tmp_path / "sessions-isolation-disabled",
        session_id="parent-isolation-disabled",
        cwd=os.fspath(root),
        repo_root=os.fspath(root),
    )
    cfg = AppConfig(model="test-model", routing_mode="code_only", web_search_mode="off")
    cfg.subagent_orchestration.workspace_isolation_enabled = False
    tools = build_tools(
        root=root,
        console=None,
        store=store,
        mode="auto",
        yes=True,
        cfg=cfg,
        api_key="test-key",
        subagents_enabled=True,
        subagent_registry=built_in_subagents(),
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )
    launcher = tools["subagent_run"].run.__self__
    try:
        # The provider remains passive and reusable if a later launcher snapshot
        # enables isolation, but this snapshot must reject admission immediately.
        assert launcher.workspace_provider is not None

        result = tools["subagent_spawn"].run(
            {
                "name": "general",
                "task": "Change app.txt in an isolated candidate.",
                "workspace_view": "isolated",
            }
        )

        assert result == {
            "error": "Isolated subagent workspaces are disabled for this session.",
            "error_code": "workspace_isolation_disabled",
            "subagent": "general",
        }
        assert launcher.child_scheduler.status()["children"] == []
    finally:
        launcher.child_scheduler.shutdown(cancel_pending=True)
        store.close()


def _run_isolated_writer_batch(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    edits: dict[str, tuple[str, str]],
) -> tuple[Any, Path, list[dict[str, Any]], dict[str, float], dict[str, float]]:
    root = _repo(tmp_path)
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", web_search_mode="off"),
        root=root,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=False,
        api_key_override="test-key",
        session_log_dir_override=tmp_path / "batch-sessions",
        subagents_enabled=True,
    )
    lock = Lock()
    both_started = Event()
    start_times: dict[str, float] = {}
    end_times: dict[str, float] = {}
    parent_before = {
        path: ((root / path).read_text(encoding="utf-8") if (root / path).exists() else None)
        for path, _content in edits.values()
    }

    class _BatchChild(_EditingChildSession):
        def run_turn(self, task: str, *, cancellation_token: Any | None = None) -> int:
            _ = cancellation_token
            with lock:
                start_times[task] = perf_counter()
                if len(start_times) == len(edits):
                    both_started.set()
            assert both_started.wait(timeout=30.0), "isolated writers were serialized"
            assert all(
                ((root / path).read_text(encoding="utf-8") if (root / path).exists() else None)
                == parent_before[path]
                for path, _content in edits.values()
            )
            edit_path, content = edits[task]
            (self.root / edit_path).write_text(content, encoding="utf-8")
            self.workspace_touched_paths.add(edit_path)
            with lock:
                end_times[task] = perf_counter()
            return 0

    monkeypatch.setattr(
        agent_loop,
        "create_session",
        lambda **kwargs: _BatchChild(root=Path(kwargs["root"]), edit_path=None),
    )
    tasks = list(edits)
    client = _ScriptedClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id=f"call-{index}",
                        name="subagent_run",
                        arguments={
                            "name": "general",
                            "task": task,
                            "workspace_view": "isolated",
                        },
                    )
                    for index, task in enumerate(tasks)
                ],
                raw={},
            ),
            LLMResponse(content="Batch complete.", tool_calls=[], raw={}),
        ]
    )
    session.client = client
    assert session.run_turn("Implement both isolated edits in one batch.") == 0
    tool_messages = [
        message for message in client.call_messages[1] if message.get("role") == "tool"
    ]
    results = [json.loads(str(message["content"])) for message in tool_messages]
    return session, root, results, start_times, end_times


def test_parallel_isolated_generals_apply_independent_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    edits = {
        "alpha": ("alpha.txt", "alpha child\n"),
        "beta": ("beta.txt", "beta child\n"),
    }
    session, root, results, start_times, end_times = _run_isolated_writer_batch(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        edits=edits,
    )
    try:
        assert max(start_times.values()) <= min(end_times.values())
        assert [result["patch_summary"]["files"] for result in results] == [
            ["alpha.txt"],
            ["beta.txt"],
        ]
        assert not (root / "alpha.txt").exists()
        assert not (root / "beta.txt").exists()

        applied = [
            session.tools["subagent_apply"].run({"run_id": result["run_id"]}) for result in results
        ]

        assert [result["ok"] for result in applied] == [True, True]
        assert (root / "alpha.txt").read_text(encoding="utf-8") == "alpha child\n"
        assert (root / "beta.txt").read_text(encoding="utf-8") == "beta child\n"
    finally:
        session.close()


def test_parallel_isolated_generals_conflict_without_partial_second_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    edits = {
        "alpha": ("app.txt", "alpha child\n"),
        "beta": ("app.txt", "beta child\n"),
    }
    session, root, results, start_times, end_times = _run_isolated_writer_batch(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        edits=edits,
    )
    try:
        assert max(start_times.values()) <= min(end_times.values())
        assert (root / "app.txt").read_text(encoding="utf-8") == "base\n"
        first = session.tools["subagent_apply"].run({"run_id": results[0]["run_id"]})
        second = session.tools["subagent_apply"].run({"run_id": results[1]["run_id"]})

        assert first["ok"] is True
        assert second["error_code"] == "merge_conflict"
        assert second["conflicting_paths"] == ["app.txt"]
        assert (root / "app.txt").read_text(encoding="utf-8") == "alpha child\n"
    finally:
        session.close()


def test_background_isolated_writer_is_retained_at_turn_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", web_search_mode="off"),
        root=root,
        mode="auto",
        yes=True,
        max_steps=6,
        no_log=False,
        api_key_override="test-key",
        session_log_dir_override=tmp_path / "background-sessions",
        subagents_enabled=True,
    )
    monkeypatch.setattr(
        agent_loop,
        "create_session",
        lambda **kwargs: _EditingChildSession(root=Path(kwargs["root"])),
    )
    spawn = session.tools["subagent_spawn"].run(
        {
            "name": "general",
            "task": "Change app.txt in the isolated candidate.",
            "workspace_view": "isolated",
        }
    )
    client = _ScriptedClient(
        [
            LLMResponse(content=f"Final attempt {index}.", tool_calls=[], raw={})
            for index in range(4)
        ]
    )
    session.client = client
    try:
        assert session.run_turn("Finish after collecting the background result.") == 0
        provider = session.tools["subagent_run"].run.__self__.workspace_provider
        record = provider.get(spawn["run_id"])
        assert record is not None and record.state == "captured"
        assert record.worktree_path.exists()
        assert (root / "app.txt").read_text(encoding="utf-8") == "base\n"
        final_events = [
            event["payload"]
            for event in session.store.events_snapshot()
            if event.get("type") == "final"
        ]
        assert spawn["run_id"] in str(final_events[-1]["content"])
        # The child may finish before the host needs to wait. Retention and
        # explicit disposal below must hold in either scheduling order.
        assert (
            session.tools["subagent_status"].run({})["unapplied_isolated_results"][0]["run_id"]
            == spawn["run_id"]
        )
        assert session.tools["subagent_discard"].run({"run_id": spawn["run_id"]})["ok"] is True
    finally:
        session.close()


def test_verifier_reads_pinned_candidate_before_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    observed: list[str] = []

    class _VerifierSession(_EditingChildSession):
        def run_turn(self, _task: str, *, cancellation_token: Any | None = None) -> int:
            _ = cancellation_token
            content = (self.root / "app.txt").read_text(encoding="utf-8")
            observed.append(content)
            self.store.final_content = f"verified: {content.strip()}"
            self.messages = [{"role": "assistant", "content": self.store.final_content}]
            return 0

    def create_child(**kwargs: Any) -> _EditingChildSession:
        if str(kwargs.get("usage_role") or "").endswith(":verifier"):
            return _VerifierSession(root=Path(kwargs["root"]), edit_path=None)
        return _EditingChildSession(root=Path(kwargs["root"]))

    registry = built_in_subagents()
    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=create_child,
        monkeypatch=monkeypatch,
        subagent_registry={name: registry[name] for name in ("general", "verifier")},
    )
    try:
        implementation = harness.run()
        verification = harness.tools["subagent_run"].run(
            {
                "name": "verifier",
                "task": "Verify app.txt contains the candidate edit.",
                "workspace_from_run": implementation["run_id"],
            }
        )
        assert observed == ["child\n"]
        assert verification["workspace"] == {
            "view": "pinned",
            "source_run_id": implementation["run_id"],
        }
        assert "verified: child" in verification["result"]
        assert (root / "app.txt").read_text(encoding="utf-8") == "base\n"

        applied = harness.tools["subagent_apply"].run({"run_id": implementation["run_id"]})
        assert applied["ok"] is True
        assert (root / "app.txt").read_text(encoding="utf-8") == "child\n"
        released = harness.tools["subagent_run"].run(
            {
                "name": "verifier",
                "task": "Try the released candidate.",
                "workspace_from_run": implementation["run_id"],
            }
        )
        assert released["error_code"] == "workspace_from_run_released"

        lifecycle: list[str] = []
        for event in harness.store.events_snapshot():
            event_type = str(event.get("type") or "")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if event_type == "subagent_workspace":
                lifecycle.append(f"workspace:{payload.get('action')}")
            elif event_type == "subagent_state":
                lifecycle.append(f"state:{payload.get('state')}")
            elif event_type in {"subagent_start", "subagent_tool_catalog", "subagent_end"}:
                lifecycle.append(event_type)
        assert lifecycle[:17] == [
            "state:spawned",
            "workspace:prepared",
            "state:running",
            "subagent_start",
            "subagent_tool_catalog",
            "workspace:captured",
            "subagent_end",
            "state:joined",
            "state:spawned",
            "workspace:pinned",
            "state:running",
            "subagent_start",
            "subagent_tool_catalog",
            "subagent_end",
            "workspace:unpinned",
            "state:joined",
            "workspace:applied",
        ]
    finally:
        harness.close()


def test_discard_is_release_locked_while_background_verifier_reads_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    verifier_started = Event()
    release_verifier = Event()

    class _BlockingVerifierSession(_EditingChildSession):
        def run_turn(self, _task: str, *, cancellation_token: Any | None = None) -> int:
            _ = cancellation_token
            assert (self.root / "app.txt").read_text(encoding="utf-8") == "child\n"
            verifier_started.set()
            assert release_verifier.wait(timeout=30.0)
            self.store.final_content = "Pinned candidate verified."
            return 0

    def create_child(**kwargs: Any) -> _EditingChildSession:
        if str(kwargs.get("usage_role") or "").endswith(":verifier"):
            return _BlockingVerifierSession(root=Path(kwargs["root"]), edit_path=None)
        return _EditingChildSession(root=Path(kwargs["root"]))

    registry = built_in_subagents()
    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=create_child,
        monkeypatch=monkeypatch,
        subagent_registry={name: registry[name] for name in ("general", "verifier")},
    )
    try:
        implementation = harness.run()
        spawned = harness.tools["subagent_spawn"].run(
            {
                "name": "verifier",
                "task": "Verify the pinned candidate.",
                "workspace_from_run": implementation["run_id"],
            }
        )
        assert verifier_started.wait(timeout=5.0)

        locked = harness.tools["subagent_discard"].run({"run_id": implementation["run_id"]})
        assert locked["error_code"] == "workspace_release_locked"
        assert locked["pinned_by_run_ids"] == [spawned["run_id"]]

        release_verifier.set()
        waited = harness.tools["subagent_wait"].run({"run_id": spawned["run_id"]})
        assert waited["pending_run_ids"] == []
        assert (
            harness.tools["subagent_discard"].run({"run_id": implementation["run_id"]})["ok"]
            is True
        )
    finally:
        release_verifier.set()
        harness.close()


def test_dependency_chain_implements_verifies_pinned_worktree_and_applies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    implementation_started = Event()
    release_implementation = Event()
    verifier_observed: list[str] = []

    class _BlockingImplementerSession(_EditingChildSession):
        def run_turn(self, task: str, *, cancellation_token: Any | None = None) -> int:
            implementation_started.set()
            assert release_implementation.wait(timeout=5.0)
            return super().run_turn(task, cancellation_token=cancellation_token)

    class _PinnedVerifierSession(_EditingChildSession):
        def run_turn(self, _task: str, *, cancellation_token: Any | None = None) -> int:
            _ = cancellation_token
            content = (self.root / "app.txt").read_text(encoding="utf-8")
            verifier_observed.append(content)
            self.store.final_content = f"verified: {content.strip()}"
            self.messages = [{"role": "assistant", "content": self.store.final_content}]
            return 0

    def create_child(**kwargs: Any) -> _EditingChildSession:
        if str(kwargs.get("usage_role") or "").endswith(":verifier"):
            return _PinnedVerifierSession(root=Path(kwargs["root"]), edit_path=None)
        return _BlockingImplementerSession(root=Path(kwargs["root"]), content="child\n")

    registry = built_in_subagents()
    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=create_child,
        monkeypatch=monkeypatch,
        subagent_registry={name: registry[name] for name in ("general", "verifier")},
    )
    try:
        implementation = harness.tools["subagent_spawn"].run(
            {
                "name": "general",
                "task": "Edit app.txt in isolation.",
                "run_id": "impl",
                "workspace_view": "isolated",
            }
        )
        assert implementation["run_id"] == "impl"
        assert implementation_started.wait(timeout=5.0)
        verification = harness.tools["subagent_spawn"].run(
            {
                "name": "verifier",
                "task": "Verify the general's app.txt.",
                "depends_on": [implementation["run_id"]],
                "workspace_from_run": implementation["run_id"],
            }
        )
        assert verification["state"] == "waiting"
        assert (root / "app.txt").read_text(encoding="utf-8") == "base\n"

        release_implementation.set()
        waited = harness.tools["subagent_wait"].run({"run_id": "all", "timeout_s": 5.0})
        assert waited["pending_run_ids"] == []
        assert verifier_observed == ["child\n"]
        assert "verified: child" in waited["results"][verification["run_id"]]["result"]
        assert (root / "app.txt").read_text(encoding="utf-8") == "base\n"

        applied = harness.tools["subagent_apply"].run({"run_id": implementation["run_id"]})
        assert applied["ok"] is True
        assert (root / "app.txt").read_text(encoding="utf-8") == "child\n"

        sequence: list[str] = []
        for event in harness.store.events_snapshot():
            event_type = str(event.get("type") or "")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            run_id = str(payload.get("run_id") or "")
            label = (
                "impl"
                if run_id == implementation["run_id"]
                else "verify"
                if run_id == verification["run_id"]
                else ""
            )
            if event_type == "subagent_state":
                sequence.append(f"{label}:state:{payload.get('state')}")
            elif event_type == "subagent_workspace":
                sequence.append(f"{label}:workspace:{payload.get('action')}")
            elif event_type in {"subagent_start", "subagent_tool_catalog", "subagent_end"}:
                name = str(payload.get("name") or "")
                sequence.append(f"{name}:{event_type}")
            elif event_type == "subagent_batch_summary":
                sequence.append("chain:subagent_batch_summary")
        assert sequence == [
            "impl:state:spawned",
            "impl:workspace:prepared",
            "impl:state:running",
            "general:subagent_start",
            "general:subagent_tool_catalog",
            "verify:state:spawned",
            "verify:state:waiting",
            "impl:workspace:captured",
            "general:subagent_end",
            "impl:state:joined",
            "verify:state:queued",
            "impl:workspace:pinned",
            "verify:state:running",
            "verifier:subagent_start",
            "verifier:subagent_tool_catalog",
            "verifier:subagent_end",
            "impl:workspace:unpinned",
            "verify:state:joined",
            "chain:subagent_batch_summary",
            "impl:workspace:applied",
        ]
        summaries = [
            event["payload"]
            for event in harness.store.events_snapshot()
            if event.get("type") == "subagent_batch_summary"
        ]
        assert summaries == [
            {
                "run_ids": [implementation["run_id"], verification["run_id"]],
                "statuses": ["success", "success"],
                "wall_ms": summaries[0]["wall_ms"],
                "usage_totals": summaries[0]["usage_totals"],
                "workspace_views": ["isolated", "pinned"],
            }
        ]
    finally:
        release_implementation.set()
        harness.close()


def test_one_response_spawns_implement_verify_chain_then_applies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    sessions_dir = tmp_path / "chain-turn-sessions"
    implementation_started = Event()
    release_implementation = Event()
    verifier_observed: list[str] = []
    parent = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", web_search_mode="off"),
        root=root,
        mode="auto",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="test-key",
        session_log_dir_override=sessions_dir,
        subagents_enabled=True,
        verification_enabled=False,
    )

    class _TurnImplementerSession(_EditingChildSession):
        def run_turn(self, task: str, *, cancellation_token: Any | None = None) -> int:
            implementation_started.set()
            assert release_implementation.wait(timeout=5.0)
            return super().run_turn(task, cancellation_token=cancellation_token)

    class _TurnVerifierSession(_EditingChildSession):
        def run_turn(self, _task: str, *, cancellation_token: Any | None = None) -> int:
            _ = cancellation_token
            content = (self.root / "app.txt").read_text(encoding="utf-8")
            verifier_observed.append(content)
            self.store.final_content = f"verified: {content.strip()}"
            self.messages = [{"role": "assistant", "content": self.store.final_content}]
            return 0

    def create_child(**kwargs: Any) -> _EditingChildSession:
        if str(kwargs.get("usage_role") or "").endswith(":verifier"):
            return _TurnVerifierSession(root=Path(kwargs["root"]), edit_path=None)
        return _TurnImplementerSession(root=Path(kwargs["root"]), content="child\n")

    monkeypatch.setattr(agent_loop, "create_session", create_child)

    class _ChainClient(_ScriptedClient):
        def chat(self, *, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
            if self.calls == 1:
                release_implementation.set()
            return super().chat(messages=messages, **kwargs)

    client = _ChainClient(
        [
            LLMResponse(
                content="Start implementation and verification.",
                tool_calls=[
                    ToolCall(
                        id="spawn-impl",
                        name="subagent_spawn",
                        arguments={
                            "name": "general",
                            "task": "Edit app.txt in isolation.",
                            "run_id": "impl",
                            "workspace_view": "isolated",
                        },
                    ),
                    ToolCall(
                        id="spawn-verify",
                        name="subagent_spawn",
                        arguments={
                            "name": "verifier",
                            "task": "Verify impl's app.txt.",
                            "run_id": "verify",
                            "depends_on": ["impl"],
                            "workspace_from_run": "impl",
                        },
                    ),
                ],
                raw={},
            ),
            LLMResponse(
                content="Collect the chain.",
                tool_calls=[
                    ToolCall(
                        id="wait-chain",
                        name="subagent_wait",
                        arguments={"run_id": "all"},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="Apply the verified candidate.",
                tool_calls=[
                    ToolCall(
                        id="apply-impl",
                        name="subagent_apply",
                        arguments={"run_id": "impl"},
                    )
                ],
                raw={},
            ),
            LLMResponse(content="Implemented, verified, and applied.", tool_calls=[], raw={}),
        ]
    )
    parent.client = client
    try:
        assert parent.run_turn("Implement and verify the candidate in one chain.") == 0
        assert implementation_started.is_set()
        assert verifier_observed == ["child\n"]
        assert (root / "app.txt").read_text(encoding="utf-8") == "child\n"
        first_step_spawns = [
            event["payload"]
            for event in parent.store.events_snapshot()
            if event.get("type") == "tool_call"
            and event.get("payload", {}).get("step") == 1
            and event.get("payload", {}).get("name") == "subagent_spawn"
        ]
        assert [event["tool_call_id"] for event in first_step_spawns] == [
            "spawn-impl",
            "spawn-verify",
        ]
        assert any(
            event.get("type") == "subagent_batch_summary"
            and event.get("payload", {}).get("run_ids") == ["impl", "verify"]
            for event in parent.store.events_snapshot()
        )
    finally:
        release_implementation.set()
        parent.close()


@pytest.mark.parametrize("launch_kind", ["background", "sync"])
def test_resume_incomplete_child_reuses_history_and_retained_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    launch_kind: str,
) -> None:
    root = _repo(tmp_path)
    child_roots: list[Path] = []
    resumed_messages: list[list[dict[str, Any]]] = []
    resumed_read_results: list[dict[str, Any]] = []

    class _EventStore:
        def __init__(self, session_id: str) -> None:
            self.session_id = session_id
            self.events: list[dict[str, Any]] = []

        def events_snapshot(self) -> list[dict[str, Any]]:
            return list(self.events)

        def events_since(self, cursor: int) -> tuple[list[dict[str, Any]], int]:
            start = max(0, int(cursor))
            return list(self.events[start:]), len(self.events)

    class _IncompleteChild(_EditingChildSession):
        def __init__(self, *, child_root: Path) -> None:
            super().__init__(root=child_root, content="partial\n")
            self.messages = []
            self.store = _EventStore("incomplete-child")
            self.read_ledger = SessionReadLedger(root=child_root)
            self.read_events: list[dict[str, Any]] = []

        def _read(self, path: str) -> dict[str, Any]:
            content_hash = self.read_ledger.content_hash(path)
            result = {"path": path, "content": (self.root / path).read_text()}
            result = self.read_ledger.filter_result(
                path=path,
                result=result,
                content_hash_before=content_hash,
                force=False,
            )
            content = json.dumps(result)
            self.read_ledger.record_delivery(result=result, content_for_message=content)
            self.read_events.extend(
                [
                    {
                        "type": "assistant_message",
                        "payload": {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": path,
                                        "type": "function",
                                        "function": {
                                            "name": "fs_read",
                                            "arguments": json.dumps({"path": path}),
                                        },
                                    }
                                ],
                            }
                        },
                    },
                    {"type": "tool_result", "payload": {"tool_call_id": path, "content": content}},
                ]
            )
            return result

        def run_turn(self, task: str, *, cancellation_token: Any | None = None) -> int:
            _ = cancellation_token
            (self.root / "app.txt").write_text("partial\n", encoding="utf-8")
            self.workspace_touched_paths.add("app.txt")
            self._read("dirty.txt")
            self._read("app.txt")
            partial = "Partial analysis: the edit still needs verification."
            self.messages.append({"role": "assistant", "content": partial})
            self.store.events = [
                {"type": "user_message", "payload": {"content": task}},
                *self.read_events,
                {
                    "type": "assistant_message",
                    "payload": {
                        "content": partial,
                        "message": {"role": "assistant", "content": partial},
                    },
                },
                {
                    "type": "forced_final_summary_fallback",
                    "payload": {"termination_kind": "max_steps"},
                },
                {
                    "type": "final",
                    "payload": {"content": "Internal stop report.", "internal_fallback": True},
                },
            ]
            return 0

    class _ResumedChild(_EditingChildSession):
        def __init__(self, *, child_root: Path) -> None:
            super().__init__(root=child_root, content="resumed\n")
            self.messages = []
            self.store = _EventStore("resumed-child")
            self.read_ledger = SessionReadLedger(root=child_root)
            (self.root / "app.txt").write_text(
                "changed-before-resume\n",
                encoding="utf-8",
            )

        def _read(self, path: str) -> dict[str, Any]:
            content_hash = self.read_ledger.content_hash(path)
            result = {"path": path, "content": (self.root / path).read_text()}
            return self.read_ledger.filter_result(
                path=path,
                result=result,
                content_hash_before=content_hash,
                force=False,
            )

        def run_turn(self, task: str, *, cancellation_token: Any | None = None) -> int:
            _ = cancellation_token
            resumed_messages.append(list(self.messages))
            assert task == "Finish and verify the retained edit."
            assert any(
                "Partial analysis" in str(message.get("content") or "") for message in self.messages
            )
            assert any(
                "resumes background run" in str(message.get("content") or "")
                for message in self.messages
            )
            resumed_read_results.extend([self._read("dirty.txt"), self._read("app.txt")])
            (self.root / "app.txt").write_text("resumed\n", encoding="utf-8")
            self.workspace_touched_paths.add("app.txt")
            final = "Finished and verified the retained edit."
            self.messages.append({"role": "assistant", "content": final})
            self.store.events = [{"type": "final", "payload": {"content": final}}]
            return 0

    created = 0
    parent_surface = _RecordingSubagentSurface()

    def create_child(**kwargs: Any) -> _EditingChildSession:
        nonlocal created
        child_root = Path(kwargs["root"])
        child_roots.append(child_root)
        created += 1
        if created == 1:
            return _IncompleteChild(child_root=child_root)
        return _ResumedChild(child_root=child_root)

    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=create_child,
        monkeypatch=monkeypatch,
        surface=parent_surface,
    )
    try:
        launch_args = {
            "name": "general",
            "task": "Start the isolated edit.",
            "workspace_view": "isolated",
            "max_steps": 20,
        }
        if launch_kind == "background":
            spawned = harness.tools["subagent_spawn"].run(launch_args)
            first_wait = harness.tools["subagent_wait"].run({"run_id": spawned["run_id"]})
            first_result = first_wait["results"][spawned["run_id"]]
        else:
            first_result = harness.tools["subagent_run"].run(launch_args)
            spawned = {"run_id": first_result["run_id"]}
        assert first_result["status"] == "incomplete"
        assert first_result["stop_reason"] == "max_steps"
        assert first_result["steps_used"] == 0
        assert first_result["resolved_step_ceiling"] > first_result["steps_used"]
        assert first_result["deadline_remaining_s"] is None
        assert first_result["run_id"] == spawned["run_id"]
        assert first_result["retained_worktree_run_id"] == spawned["run_id"]
        assert first_result["resume_affordance"] == (
            "This run can be continued with "
            f"subagent_resume(run_id={spawned['run_id']}) preserving its transcript "
            "and worktree."
        )
        assert parent_surface.subagent_ends[-1].status == "incomplete"
        assert parent_surface.subagent_ends[-1].subagent_run_id == spawned["run_id"]
        assert parent_surface.subagent_ends[-1].workspace_view == "isolated"
        incomplete_end = next(
            event["payload"]
            for event in harness.store.events_snapshot()
            if event.get("type") == "subagent_end"
            and event.get("payload", {}).get("status") == "incomplete"
        )
        for key in (
            "stop_reason",
            "steps_used",
            "resolved_step_ceiling",
            "deadline_remaining_s",
            "run_id",
            "retained_worktree_run_id",
            "resume_affordance",
        ):
            assert incomplete_end[key] == first_result[key]
        assert (root / "app.txt").read_text(encoding="utf-8") == "base\n"
        sync_status = harness.tools["subagent_status"].run({"run_id": spawned["run_id"]})
        assert sync_status["children"][0]["state"] == "joined"
        sync_view = harness.launcher.child_scheduler.view_since(
            run_id=spawned["run_id"],
            cursor=0,
        )
        assert sync_view["run_id"] == spawned["run_id"]
        assert sync_view["transcript_tail"]

        resumed = harness.tools["subagent_resume"].run(
            {
                "run_id": spawned["run_id"],
                "task": "Finish and verify the retained edit.",
            }
        )
        assert resumed["run_id"] != spawned["run_id"]
        assert resumed["resumed_from"] == spawned["run_id"]
        assert "assigned work with this child" in resumed["orchestration_note"]
        if launch_kind == "background":
            assert resumed["orchestration_note"] == spawned["orchestration_note"]
        else:
            assert "orchestration_note" not in spawned
        second_wait = harness.tools["subagent_wait"].run({"run_id": resumed["run_id"]})
        second_result = second_wait["results"][resumed["run_id"]]
        assert second_result["resumed_from"] == spawned["run_id"]
        assert second_result["result"] == "Finished and verified the retained edit."
        assert child_roots[0] == child_roots[1]
        assert resumed_messages
        assert resumed_read_results[0]["read_ledger_skipped"] is True
        assert "already returned in this session" in resumed_read_results[0]["content"]
        assert resumed_read_results[1]["content"] == "changed-before-resume\n"
        assert "read_ledger_skipped" not in resumed_read_results[1]

        provider = harness.launcher.workspace_provider
        assert provider is not None
        assert provider.get(spawned["run_id"]) is None
        resumed_workspace = provider.get(resumed["run_id"])
        assert resumed_workspace is not None
        assert resumed_workspace.worktree_path == child_roots[1]
        assert (root / "app.txt").read_text(encoding="utf-8") == "base\n"
        applied = harness.tools["subagent_apply"].run({"run_id": resumed["run_id"]})
        assert applied["ok"] is True
        assert (root / "app.txt").read_text(encoding="utf-8") == "resumed\n"

        resumed_state_events = [
            event["payload"]
            for event in harness.store.events_snapshot()
            if event.get("type") == "subagent_state"
            and event.get("payload", {}).get("run_id") == resumed["run_id"]
        ]
        assert [event["state"] for event in resumed_state_events] == [
            "spawned",
            "running",
            "joined",
        ]
        assert all(event["resumed_from"] == spawned["run_id"] for event in resumed_state_events)
        resume_lifecycle: list[str] = []
        recording_resume = False
        for event in harness.store.events_snapshot():
            event_type = str(event.get("type") or "")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if (
                event_type == "subagent_workspace"
                and payload.get("run_id") == resumed["run_id"]
                and payload.get("action") == "reattached"
            ):
                recording_resume = True
            if not recording_resume:
                continue
            if event_type == "subagent_workspace":
                resume_lifecycle.append(f"workspace:{payload.get('action')}")
            elif event_type == "subagent_state":
                resume_lifecycle.append(f"state:{payload.get('state')}")
            elif event_type in {"subagent_start", "subagent_tool_catalog", "subagent_end"}:
                resume_lifecycle.append(event_type)
        assert resume_lifecycle == [
            "workspace:reattached",
            "state:spawned",
            "state:running",
            "subagent_start",
            "subagent_tool_catalog",
            "workspace:captured",
            "subagent_end",
            "state:joined",
            "workspace:applied",
        ]
    finally:
        harness.close()


def test_shared_review_incomplete_advertises_launchable_isolated_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    child_roots: list[Path] = []

    class _EventStore:
        def __init__(self, session_id: str) -> None:
            self.session_id = session_id
            self.events: list[dict[str, Any]] = []

        def events_snapshot(self) -> list[dict[str, Any]]:
            return list(self.events)

    class _IncompleteDiagnostic(_EditingChildSession):
        def __init__(self, *, child_root: Path) -> None:
            super().__init__(root=child_root, edit_path=None)
            self.messages = []
            self.store = _EventStore("incomplete-diagnostic")

        def run_turn(self, task: str, *, cancellation_token: Any | None = None) -> int:
            _ = cancellation_token
            partial = "Partial diagnosis: inspect the scheduler ownership next."
            self.messages.append({"role": "assistant", "content": partial})
            self.store.events = [
                {"type": "user_message", "payload": {"content": task}},
                {
                    "type": "assistant_message",
                    "payload": {
                        "content": partial,
                        "message": {"role": "assistant", "content": partial},
                    },
                },
                {
                    "type": "forced_final_summary_fallback",
                    "payload": {"termination_kind": "max_steps"},
                },
                {
                    "type": "final",
                    "payload": {"content": partial, "internal_fallback": True},
                },
            ]
            return 0

    class _ResumedDiagnostic(_EditingChildSession):
        def __init__(self, *, child_root: Path) -> None:
            super().__init__(root=child_root, edit_path=None)
            self.messages = []
            self.store = _EventStore("resumed-diagnostic")

        def run_turn(self, task: str, *, cancellation_token: Any | None = None) -> int:
            _ = task, cancellation_token
            assert any(
                "Partial diagnosis" in str(message.get("content") or "")
                for message in self.messages
            )
            final = "Diagnosis complete: scheduler ownership is stale."
            self.messages.append({"role": "assistant", "content": final})
            self.store.events = [{"type": "final", "payload": {"content": final}}]
            return 0

    created = 0

    def create_child(**kwargs: Any) -> _EditingChildSession:
        nonlocal created
        child_root = Path(kwargs["root"])
        child_roots.append(child_root)
        created += 1
        if created == 1:
            return _IncompleteDiagnostic(child_root=child_root)
        return _ResumedDiagnostic(child_root=child_root)

    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=create_child,
        monkeypatch=monkeypatch,
        subagent_registry={
            "debugger": SubagentDefinition(
                name="debugger",
                description="diagnostic child",
                system_prompt="Diagnose the failure.",
                mode="review",
                allow_tools=("fs_read", "shell_run"),
                required_tools=("shell_run",),
                allow_workspace_writes=False,
            )
        },
    )
    try:
        first = harness.tools["subagent_run"].run(
            {"name": "debugger", "task": "Diagnose the scheduler.", "max_steps": 20}
        )
        assert first["status"] == "incomplete"
        refused_shared = harness.tools["subagent_resume"].run(
            {"run_id": first["run_id"], "workspace_view": "shared"}
        )
        assert refused_shared["error_code"] == "background_subagent_requires_readonly"
        assert refused_shared["resume_alternative"] == {
            "tool": "subagent_resume",
            "run_id": first["run_id"],
            "workspace_view": "isolated",
        }
        assert 'workspace_view="isolated"' in refused_shared["error"]
        assert "fresh synchronous subagent_run" in refused_shared["error"]
        resume_args: dict[str, Any] = {"run_id": first["run_id"]}
        if 'workspace_view="isolated"' in first["resume_affordance"]:
            resume_args["workspace_view"] = "isolated"

        resumed = harness.tools["subagent_resume"].run(resume_args)

        assert "error" not in resumed, resumed
        assert first["resume_affordance"] == (
            "This run can be continued with "
            f'subagent_resume(run_id={first["run_id"]}, workspace_view="isolated") '
            "preserving its transcript in a fresh isolated workspace."
        )
        waited = harness.tools["subagent_wait"].run({"run_id": resumed["run_id"]})
        assert waited["results"][resumed["run_id"]]["result"] == (
            "Diagnosis complete: scheduler ownership is stale."
        )
        assert child_roots[0] == root
        assert child_roots[1] != root
    finally:
        harness.close()


def test_applying_incomplete_candidate_requires_explicit_acknowledgement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)

    class _IncompleteCandidate(_EditingChildSession):
        def __init__(self, *, child_root: Path) -> None:
            super().__init__(root=child_root, content="partial\n")
            self.messages = []
            self.store = type(
                "_Store",
                (),
                {
                    "session_id": "incomplete-apply-child",
                    "events_snapshot": lambda self: [
                        {
                            "type": "forced_final_summary_fallback",
                            "payload": {"termination_kind": "step_budget_exhausted"},
                        },
                        {
                            "type": "final",
                            "payload": {
                                "content": "Remaining work: verify the partial edit.",
                                "internal_fallback": True,
                            },
                        },
                    ],
                },
            )()

        def run_turn(self, task: str, *, cancellation_token: Any | None = None) -> int:
            _ = (task, cancellation_token)
            (self.root / "app.txt").write_text("partial\n", encoding="utf-8")
            self.workspace_touched_paths.add("app.txt")
            return 0

    def create_child(**kwargs: Any) -> _IncompleteCandidate:
        return _IncompleteCandidate(child_root=Path(kwargs["root"]))

    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=create_child,
        monkeypatch=monkeypatch,
    )
    try:
        result = harness.tools["subagent_run"].run(
            {
                "name": "general",
                "task": "Start the isolated edit.",
                "workspace_view": "isolated",
            }
        )
        assert result["status"] == "incomplete"

        blocked = harness.tools["subagent_apply"].run({"run_id": result["run_id"]})
        assert blocked["error_code"] == "incomplete_candidate_requires_acknowledgement"
        assert blocked["run_status"] == "incomplete"
        assert blocked["stop_reason"] == "step_budget_exhausted"
        assert blocked["unfinished_work"]["summary"]
        assert (root / "app.txt").read_text(encoding="utf-8") == "base\n"

        applied = harness.tools["subagent_apply"].run(
            {"run_id": result["run_id"], "acknowledge_incomplete": True}
        )
        assert applied["ok"] is True
        assert applied["incomplete_acknowledged"] is True
        assert applied["run_status"] == "incomplete"
        assert (root / "app.txt").read_text(encoding="utf-8") == "partial\n"
    finally:
        harness.close()


def test_resume_rejects_running_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    started = Event()
    release = Event()

    class _BlockingChild(_EditingChildSession):
        def run_turn(self, task: str, *, cancellation_token: Any | None = None) -> int:
            started.set()
            assert release.wait(timeout=5.0)
            return super().run_turn(task, cancellation_token=cancellation_token)

    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=lambda **kwargs: _BlockingChild(root=Path(kwargs["root"])),
        monkeypatch=monkeypatch,
    )
    try:
        spawned = harness.tools["subagent_spawn"].run(
            {
                "name": "general",
                "task": "Block briefly.",
                "workspace_view": "isolated",
            }
        )
        assert started.wait(timeout=5.0)
        rejected = harness.tools["subagent_resume"].run({"run_id": spawned["run_id"]})
        assert rejected["error_code"] == "subagent_resume_requires_terminal"
        assert rejected["state"] == "running"
    finally:
        release.set()
        harness.close()


def test_resume_rejects_released_isolated_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)

    class _FailedChild(_EditingChildSession):
        def run_turn(self, task: str, *, cancellation_token: Any | None = None) -> int:
            super().run_turn(task, cancellation_token=cancellation_token)
            return 1

    harness = _Harness(
        tmp_path=tmp_path,
        root=root,
        create_child=lambda **kwargs: _FailedChild(root=Path(kwargs["root"])),
        monkeypatch=monkeypatch,
    )
    try:
        spawned = harness.tools["subagent_spawn"].run(
            {
                "name": "general",
                "task": "Fail after editing.",
                "workspace_view": "isolated",
            }
        )
        waited = harness.tools["subagent_wait"].run({"run_id": spawned["run_id"]})
        assert waited["results"][spawned["run_id"]]["error"]
        assert harness.tools["subagent_discard"].run({"run_id": spawned["run_id"]})["ok"] is True
        rejected = harness.tools["subagent_resume"].run({"run_id": spawned["run_id"]})
        assert rejected["error_code"] == "subagent_resume_worktree_released"
        assert rejected["workspace_state"] == "discarded"
    finally:
        harness.close()
