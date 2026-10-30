from __future__ import annotations

import io
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from rich.console import Console

import alysis_code.agent_loop as agent_loop_mod
import alysis_code.tools.fs as fs_module
from alysis_code.agent.errors import AgentRuntimeError
from alysis_code.agent_loop import AgentSession, build_tools, create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse, ToolCall
from alysis_code.session_store import SessionStore
from alysis_code.surface import ApprovalDecision, ApprovalRequest, NoopSurface
from alysis_code.tools.fs import (
    FsError,
    StaleFileError,
    capture_file_precondition,
    classify_sensitive_path,
    fs_delete,
    fs_move,
    prepare_fs_edit,
    prepare_fs_write,
    write_prepared_fs_edit,
    write_prepared_fs_write,
)


def _store(root: Path, *, enabled: bool = False) -> SessionStore:
    return SessionStore(
        enabled=enabled,
        sessions_dir=root / "sessions",
        session_id="fs-safety-policy-test",
        cwd=str(root),
        repo_root=str(root),
    )


class _RecordingSurface(NoopSurface):
    host_managed_approvals = True

    def __init__(
        self,
        *,
        decision: ApprovalDecision = ApprovalDecision(allow=True),
        before_decision: object | None = None,
    ) -> None:
        self.decision = decision
        self.before_decision = before_decision
        self.requests: list[ApprovalRequest] = []
        self.patch_events: list[object] = []

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        if callable(self.before_decision):
            self.before_decision()
        return self.decision

    def on_patch_generated(self, event: object) -> None:
        self.patch_events.append(event)


class _RuntimeReadClient:
    model = "test-model"
    temperature = 0.2

    def __init__(self, *, tool_name: str, arguments: dict[str, object]) -> None:
        self.tool_name = tool_name
        self.arguments = arguments
        self.calls: list[list[dict[str, object]]] = []

    def chat(self, *, messages: list[dict[str, object]], **_kwargs: object) -> LLMResponse:
        self.calls.append(list(messages))
        if len(self.calls) == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="runtime-sensitive-read",
                        name=self.tool_name,
                        arguments=self.arguments,
                    )
                ],
                raw={},
            )
        return LLMResponse(content="Stopped after the terminal read result.", tool_calls=[], raw={})


def _runtime_read_session(
    tmp_path: Path,
    *,
    surface: NoopSurface,
    session_id: str,
) -> AgentSession:
    cfg = AppConfig(model="test-model", routing_mode="code_only", stream=False, max_steps=3)
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "test-model": {"context_window_tokens": 4096, "max_output_tokens": 512},
            },
            "default": {"context_window_tokens": 4096, "max_output_tokens": 512},
        }
    }
    return create_session(
        cfg=cfg,
        root=tmp_path,
        mode="fullaccess",
        yes=True,
        max_steps=3,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        session_id_override=session_id,
        surface=surface,
        enable_compaction=False,
    )


def _tools(
    root: Path,
    *,
    surface: NoopSurface,
    mode: str = "auto",
    yes: bool = True,
    store: SessionStore | None = None,
) -> dict[str, object]:
    return build_tools(
        root=root,
        console=Console(file=io.StringIO(), force_terminal=False),
        surface=surface,
        store=store or _store(root),
        mode=mode,
        yes=yes,
        cfg=AppConfig(model="test-model"),
        non_interactive=True,
    )


@pytest.mark.parametrize(
    ("path", "sensitive", "category"),
    [
        (".env", True, "environment_file"),
        ("config/.env.production", True, "environment_file"),
        (".env.example", False, None),
        ("config/.env.test.template", False, None),
        ("keys/id_ed25519", True, "private_key"),
        ("keys/id_ed25519.pub", False, None),
        ("tls/server.pem", True, "private_key"),
        ("keys/deploy.ppk", True, "private_key"),
        (".aws/credentials", True, "credential_directory"),
        (".docker/config.json", True, "credential_directory"),
        (".ssh/config", True, "credential_directory"),
        (".kube/config", True, "credential_directory"),
        (".gnupg/random.txt", True, "credential_directory"),
        ("infra/kubeconfig", True, "credential_file"),
        ("config/service-account-prod.json", True, "credential_file"),
        ("config/settings.json", False, None),
    ],
)
def test_sensitive_path_classification(path: str, sensitive: bool, category: str | None) -> None:
    result = classify_sensitive_path(path)
    assert result.sensitive is sensitive
    assert result.category == category


def test_prepared_edit_rejects_human_change_with_stable_code(tmp_path: Path) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("before\n", encoding="utf-8")
    prepared = prepare_fs_edit(
        root=tmp_path,
        path="demo.txt",
        edits=[{"op": "replace_exact", "target": "before", "replacement": "agent"}],
    )

    target.write_text("human\n", encoding="utf-8")

    with pytest.raises(StaleFileError) as raised:
        write_prepared_fs_edit(prepared, root=tmp_path)
    assert raised.value.code == "stale_file"
    assert target.read_text(encoding="utf-8") == "human\n"
    assert not list(tmp_path.glob(".demo.txt.*.tmp"))


def test_prepared_write_rejects_file_created_while_waiting(tmp_path: Path) -> None:
    prepared = prepare_fs_write(root=tmp_path, path="new.txt", content="agent\n")
    (tmp_path / "new.txt").write_text("human\n", encoding="utf-8")

    with pytest.raises(StaleFileError, match="stale_file"):
        write_prepared_fs_write(prepared, root=tmp_path)

    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "human\n"


def test_atomic_write_preserves_edit_injected_after_final_precondition_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("before\n", encoding="utf-8")
    prepared = prepare_fs_write(root=tmp_path, path="demo.txt", content="agent\n")
    original_assert = fs_module.assert_file_precondition
    injected = False

    def assert_then_inject(*, root: Path, precondition: object) -> None:
        nonlocal injected
        original_assert(root=root, precondition=precondition)  # type: ignore[arg-type]
        if not injected:
            injected = True
            target.write_text("human\n", encoding="utf-8")

    monkeypatch.setattr(fs_module, "assert_file_precondition", assert_then_inject)

    with pytest.raises(StaleFileError, match="stale_file"):
        write_prepared_fs_write(prepared, root=tmp_path)

    assert target.read_text(encoding="utf-8") == "human\n"
    assert not list(tmp_path.glob(".demo.txt.*.tmp"))
    assert not list(tmp_path.glob(".demo.txt.*.displaced"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not portable to Windows")
def test_atomic_edit_preserves_existing_permissions(tmp_path: Path) -> None:
    target = tmp_path / "script.sh"
    target.write_text("echo before\n", encoding="utf-8")
    target.chmod(0o751)
    prepared = prepare_fs_edit(
        root=tmp_path,
        path="script.sh",
        edits=[{"op": "replace_exact", "target": "before", "replacement": "after"}],
    )

    write_prepared_fs_edit(prepared, root=tmp_path)

    assert stat.S_IMODE(target.stat().st_mode) == 0o751


def test_publication_preserves_name_reclaimed_after_displacement(tmp_path, monkeypatch):
    target = tmp_path / "demo.txt"
    target.write_text("before\n", encoding="utf-8")
    prepared = prepare_fs_write(root=tmp_path, path="demo.txt", content="agent\n")
    original = fs_module._publish_regular_file_no_replace

    def concurrent_claim(source, destination):
        assert destination == target and not target.exists()
        target.write_text("human\n", encoding="utf-8")
        return original(source, destination)

    monkeypatch.setattr(fs_module, "_publish_regular_file_no_replace", concurrent_claim)
    with pytest.raises(StaleFileError):
        write_prepared_fs_write(prepared, root=tmp_path)
    assert target.read_text(encoding="utf-8") == "human\n"
    assert not list(tmp_path.glob(".demo.txt.*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows no-replace rename")
def test_native_publication_needs_no_hardlink_permission(tmp_path, monkeypatch):
    from alysis_code.tools.image_generation import _atomic_create_bytes

    def refused_link(*args, **kwargs):
        raise PermissionError("hard links unavailable in this sandbox")

    monkeypatch.setattr(os, "link", refused_link)
    for content in ("first", "second"):
        prepared = prepare_fs_write(root=tmp_path, path="demo.txt", content=content)
        write_prepared_fs_write(prepared, root=tmp_path)
        assert (tmp_path / "demo.txt").read_text() == content
    _atomic_create_bytes(tmp_path / "image.bin", b"synthetic image")
    assert (tmp_path / "image.bin").read_bytes() == b"synthetic image"
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="Windows handle publication")
def test_displacement_collision_preserves_other_writers_file(tmp_path, monkeypatch):
    target = tmp_path / "demo.txt"
    target.write_text("before")
    tokens = iter(["staging", "collision", "available"])
    monkeypatch.setattr(fs_module.secrets, "token_hex", lambda _n: next(tokens))
    collision = tmp_path / ".demo.txt.collision.displaced"
    collision.write_text("other writer")
    prepared = prepare_fs_write(root=tmp_path, path="demo.txt", content="after")
    write_prepared_fs_write(prepared, root=tmp_path)
    assert target.read_text() == "after"
    assert collision.read_text() == "other writer"
    assert list(tmp_path.glob(".demo.txt.*")) == [collision]


@pytest.mark.skipif(os.name != "nt", reason="Windows guarded image publication")
def test_image_does_not_bypass_denied_publication(tmp_path, monkeypatch):
    import alysis_code.tools.image_generation as images

    def refuse(*args, **kwargs):
        raise PermissionError("guarded rename refused")

    monkeypatch.setattr(images, "rename_host_file_no_replace", refuse)
    target = tmp_path / "image.bin"
    with pytest.raises(PermissionError, match="guarded rename refused"):
        images._atomic_create_bytes(target, b"synthetic image")
    assert not target.exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_tool_edit_returns_stale_file_when_approval_wait_races(tmp_path: Path) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("before\n", encoding="utf-8")
    surface = _RecordingSurface(
        before_decision=lambda: target.write_text("human\n", encoding="utf-8")
    )
    tools = _tools(tmp_path, surface=surface, mode="review", yes=False)

    result = tools["fs_edit"].run(  # type: ignore[attr-defined]
        {
            "path": "demo.txt",
            "edits": [{"op": "replace_exact", "target": "before", "replacement": "agent"}],
        }
    )

    assert result["error_code"] == "stale_file"
    assert result["recoverable"] is True
    assert target.read_text(encoding="utf-8") == "human\n"


def test_tool_write_returns_stale_file_when_approval_wait_races(tmp_path: Path) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("before\n", encoding="utf-8")
    surface = _RecordingSurface(
        before_decision=lambda: target.write_text("human\n", encoding="utf-8")
    )
    tools = _tools(tmp_path, surface=surface, mode="review", yes=False)

    result = tools["fs_write"].run(  # type: ignore[attr-defined]
        {"path": "demo.txt", "content": "agent\n"}
    )

    assert result["error_code"] == "stale_file"
    assert target.read_text(encoding="utf-8") == "human\n"


@pytest.mark.parametrize("tool_name", ["fs_delete", "fs_move"])
def test_destructive_tool_rejects_source_change_during_approval(
    tmp_path: Path, tool_name: str
) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("before\n", encoding="utf-8")
    surface = _RecordingSurface(
        before_decision=lambda: target.write_text("human\n", encoding="utf-8")
    )
    tools = _tools(tmp_path, surface=surface, mode="review", yes=False)
    args = (
        {"path": "demo.txt"}
        if tool_name == "fs_delete"
        else {"source_path": "demo.txt", "destination_path": "moved.txt"}
    )

    result = tools[tool_name].run(args)  # type: ignore[attr-defined]

    assert result["error_code"] == "stale_file"
    assert target.read_text(encoding="utf-8") == "human\n"
    assert not (tmp_path / "moved.txt").exists()


def test_delete_preserves_edit_injected_after_final_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("before\n", encoding="utf-8")
    precondition = capture_file_precondition(root=tmp_path, path="demo.txt")
    original_assert = fs_module.assert_file_precondition
    injected = False

    def assert_then_inject(*, root: Path, precondition: object) -> None:
        nonlocal injected
        original_assert(root=root, precondition=precondition)  # type: ignore[arg-type]
        if not injected:
            injected = True
            target.write_text("human\n", encoding="utf-8")

    monkeypatch.setattr(fs_module, "assert_file_precondition", assert_then_inject)

    with pytest.raises(StaleFileError, match="stale_file"):
        fs_delete(root=tmp_path, path="demo.txt", precondition=precondition)

    assert target.read_text(encoding="utf-8") == "human\n"


def test_delete_rejects_path_recreated_after_verified_displacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "demo.txt"
    target.write_text("before\n", encoding="utf-8")
    precondition = capture_file_precondition(root=tmp_path, path="demo.txt")
    original_match = fs_module._displaced_file_matches
    match_checks = 0

    def match_then_recreate(path: Path, expected: object) -> bool:
        nonlocal match_checks
        result = original_match(path, expected)  # type: ignore[arg-type]
        match_checks += 1
        if match_checks == 2:
            target.write_text("human\n", encoding="utf-8")
        return result

    monkeypatch.setattr(fs_module, "_displaced_file_matches", match_then_recreate)

    with pytest.raises(StaleFileError, match="stale_file"):
        fs_delete(root=tmp_path, path="demo.txt", precondition=precondition)

    assert target.read_text(encoding="utf-8") == "human\n"


@pytest.mark.parametrize("racing_endpoint", ["source", "destination"])
def test_move_rolls_back_other_endpoint_when_commit_boundary_races(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    racing_endpoint: str,
) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("source-before\n", encoding="utf-8")
    destination.write_text("destination-before\n", encoding="utf-8")
    source_precondition = capture_file_precondition(root=tmp_path, path="source.txt")
    destination_precondition = capture_file_precondition(root=tmp_path, path="destination.txt")
    original_displace = fs_module._displace_regular_file_if_matches
    injected = False

    def displace_after_human_edit(*, target_path: Path, precondition: object) -> Path | None:
        nonlocal injected
        if not injected and target_path == (source if racing_endpoint == "source" else destination):
            injected = True
            target_path.write_text(f"{racing_endpoint}-human\n", encoding="utf-8")
        return original_displace(
            target_path=target_path,
            precondition=precondition,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(fs_module, "_displace_regular_file_if_matches", displace_after_human_edit)

    with pytest.raises(StaleFileError, match="stale_file"):
        fs_move(
            root=tmp_path,
            source_path="source.txt",
            destination_path="destination.txt",
            overwrite=True,
            source_precondition=source_precondition,
            destination_precondition=destination_precondition,
        )

    expected_source = "source-human\n" if racing_endpoint == "source" else "source-before\n"
    expected_destination = (
        "destination-human\n" if racing_endpoint == "destination" else "destination-before\n"
    )
    assert source.read_text(encoding="utf-8") == expected_source
    assert destination.read_text(encoding="utf-8") == expected_destination


@pytest.mark.parametrize("racing_endpoint", ["source", "destination"])
def test_copy_rejects_endpoint_change_during_approval(tmp_path: Path, racing_endpoint: str) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("source-before\n", encoding="utf-8")
    destination.write_text("destination-before\n", encoding="utf-8")
    raced = source if racing_endpoint == "source" else destination
    surface = _RecordingSurface(
        before_decision=lambda: raced.write_text(f"{racing_endpoint}-human\n", encoding="utf-8")
    )
    tools = _tools(tmp_path, surface=surface, mode="review", yes=False)

    result = tools["fs_copy"].run(  # type: ignore[attr-defined]
        {
            "source_path": "source.txt",
            "destination_path": "destination.txt",
            "overwrite": True,
        }
    )

    assert result["error_code"] == "stale_file"
    assert raced.read_text(encoding="utf-8") == f"{racing_endpoint}-human\n"


def test_copy_preserves_destination_edit_injected_at_commit_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("source\n", encoding="utf-8")
    destination.write_text("destination-before\n", encoding="utf-8")
    source_precondition = capture_file_precondition(root=tmp_path, path="source.txt")
    destination_precondition = capture_file_precondition(root=tmp_path, path="destination.txt")
    original_displace = fs_module._displace_regular_file_if_matches

    def displace_after_human_edit(*, target_path: Path, precondition: object) -> Path | None:
        if target_path == destination:
            destination.write_text("destination-human\n", encoding="utf-8")
        return original_displace(
            target_path=target_path,
            precondition=precondition,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(fs_module, "_displace_regular_file_if_matches", displace_after_human_edit)

    with pytest.raises(StaleFileError, match="stale_file"):
        fs_module.fs_copy(
            root=tmp_path,
            source_path="source.txt",
            destination_path="destination.txt",
            overwrite=True,
            source_precondition=source_precondition,
            destination_precondition=destination_precondition,
        )

    assert source.read_text(encoding="utf-8") == "source\n"
    assert destination.read_text(encoding="utf-8") == "destination-human\n"


def test_git_apply_rejects_change_during_approval(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    target = tmp_path / "demo.txt"
    target.write_text("before\n", encoding="utf-8", newline="\n")
    surface = _RecordingSurface(
        before_decision=lambda: target.write_text("human\n", encoding="utf-8", newline="\n")
    )
    tools = _tools(tmp_path, surface=surface, mode="review", yes=False)
    patch = """diff --git a/demo.txt b/demo.txt
--- a/demo.txt
+++ b/demo.txt
@@ -1 +1 @@
-before
+agent
"""

    result = tools["git_apply_patch"].run({"patch": patch})  # type: ignore[attr-defined]

    assert result["error_code"] == "stale_file"
    assert target.read_text(encoding="utf-8") == "human\n"


@pytest.mark.parametrize("line_options", [{}, {"start_line": 1, "end_line": 1}])
def test_sensitive_read_requires_one_time_approval_in_auto(
    tmp_path: Path, line_options: dict[str, int]
) -> None:
    secret = "token=do-not-render"
    (tmp_path / ".env").write_text(secret, encoding="utf-8")
    surface = _RecordingSurface()
    tools = _tools(tmp_path, surface=surface, mode="auto", yes=True)

    result = tools["fs_read"].run({"path": ".env", **line_options})  # type: ignore[attr-defined]

    assert result["content"] == (f"1: {secret}" if line_options else secret)
    assert result["_alysis_output_policy"]["persist"] == "redact"
    [request] = surface.requests
    assert request.kind == "fs_read"
    assert request.allow_for_session_scope is None
    assert request.metadata["mandatory_explicit_approval"] is True
    assert secret not in request.preview


def test_runtime_sensitive_missing_path_reports_terminal_nonexistence(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    surface = _RecordingSurface()
    session = _runtime_read_session(
        tmp_path,
        surface=surface,
        session_id="sensitive-missing-runtime",
    )
    client = _RuntimeReadClient(
        tool_name="fs_read",
        arguments={"path": ".git/logs/HEAD", "start_line": 1, "end_line": 10},
    )
    session.client = client  # type: ignore[assignment]

    try:
        assert session.run_turn("Read the worktree reflog.") == 0
    finally:
        session.close()

    tool_message = next(message for message in client.calls[1] if message.get("role") == "tool")
    result = json.loads(str(tool_message["content"]))
    assert result["error"] == (
        "Path does not exist: .git/logs/HEAD. This result is terminal; do not retry this path."
    )
    assert result["error_code"] == "fs_path_not_found"
    assert result["terminal"] is True
    assert result["retryable"] is False
    assert surface.requests == []


def test_runtime_sensitive_existing_read_failure_is_terminal_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "sensitive-read-failure-canary"
    (tmp_path / ".env").write_text(secret, encoding="utf-8")

    def _failed_read(**_kwargs: object) -> dict[str, object]:
        raise FsError(f"simulated protected read failure: {secret}")

    monkeypatch.setattr(agent_loop_mod, "fs_read", _failed_read)
    surface = _RecordingSurface()
    session = _runtime_read_session(
        tmp_path,
        surface=surface,
        session_id="sensitive-existing-runtime",
    )
    client = _RuntimeReadClient(tool_name="fs_read", arguments={"path": ".env"})
    session.client = client  # type: ignore[assignment]

    try:
        assert session.run_turn("Read the protected file.") == 0
        serialized_messages = json.dumps(client.calls, ensure_ascii=True)
    finally:
        session.close()

    tool_message = next(message for message in client.calls[1] if message.get("role") == "tool")
    result = json.loads(str(tool_message["content"]))
    assert result["error"] == (
        "Sensitive path is protected and will not be readable after this failure. "
        "No content was returned. This failure is terminal; do not retry."
    )
    assert result["error_code"] == "sensitive_read_terminal"
    assert result["terminal"] is True
    assert result["retryable"] is False
    assert "content" not in result
    assert secret not in serialized_messages
    assert surface.requests == [], "fullaccess removes prompts but must retain redaction"


@pytest.mark.parametrize("path", ["keys/deploy.ppk", ".kube/config", ".ssh/custom-key"])
def test_credential_locations_require_explicit_read_approval(
    tmp_path: Path,
    path: str,
) -> None:
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("credential-canary", encoding="utf-8")
    surface = _RecordingSurface(decision=ApprovalDecision(allow=False))
    tools = _tools(tmp_path, surface=surface, mode="auto", yes=True)

    with pytest.raises(AgentRuntimeError):
        tools["fs_read"].run({"path": path})  # type: ignore[attr-defined]

    [request] = surface.requests
    assert request.metadata["mandatory_explicit_approval"] is True
    assert "credential-canary" not in request.preview


def test_sensitive_read_rejects_auto_or_session_decision_without_reading(tmp_path: Path) -> None:
    secret = "token=never-returned"
    (tmp_path / ".env.local").write_text(secret, encoding="utf-8")
    surface = _RecordingSurface(decision=ApprovalDecision(allow=True, allow_for_session=True))
    tools = _tools(tmp_path, surface=surface, mode="auto", yes=True)

    with pytest.raises(AgentRuntimeError) as raised:
        tools["fs_read"].run({"path": ".env.local"})  # type: ignore[attr-defined]

    assert "Automatic or session approval cannot authorize" in str(raised.value)
    assert secret not in str(raised.value)


def test_sensitive_write_never_persists_or_emits_content_preview(tmp_path: Path) -> None:
    secret = "API_TOKEN=super-secret-value"
    surface = _RecordingSurface()
    store = _store(tmp_path, enabled=True)
    tools = _tools(
        tmp_path,
        surface=surface,
        mode="fullaccess",
        yes=True,
        store=store,
    )

    result = tools["fs_write"].run(  # type: ignore[attr-defined]
        {"path": ".env.production", "content": secret}
    )

    assert (tmp_path / ".env.production").read_text(encoding="utf-8") == secret
    assert result["_alysis_output_policy"]["display"] == "redact"
    serialized_events = json.dumps(store.events_snapshot(), ensure_ascii=True)
    assert secret not in serialized_events
    assert "sensitive_change_preview" in serialized_events
    assert surface.patch_events == []


@pytest.mark.parametrize(
    "tool_name",
    ["fs_edit", "fs_write", "fs_move", "fs_copy", "fs_delete", "git_apply_patch"],
)
def test_every_sensitive_mutation_rejects_human_edit_during_explicit_approval(
    tmp_path: Path, tool_name: str
) -> None:
    target = tmp_path / ".env"
    target.write_text("before\n", encoding="utf-8", newline="\n")
    if tool_name == "git_apply_patch":
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    surface = _RecordingSurface(
        before_decision=lambda: target.write_text("human\n", encoding="utf-8", newline="\n")
    )
    tools = _tools(tmp_path, surface=surface, mode="auto", yes=True)
    args_by_tool: dict[str, dict[str, object]] = {
        "fs_edit": {
            "path": ".env",
            "edits": [{"op": "replace_exact", "target": "before", "replacement": "agent"}],
        },
        "fs_write": {"path": ".env", "content": "agent\n"},
        "fs_move": {"source_path": ".env", "destination_path": "moved.txt"},
        "fs_copy": {"source_path": ".env", "destination_path": "copied.txt"},
        "fs_delete": {"path": ".env"},
        "git_apply_patch": {
            "patch": """diff --git a/.env b/.env
--- a/.env
+++ b/.env
@@ -1 +1 @@
-before
+agent
"""
        },
    }

    result = tools[tool_name].run(args_by_tool[tool_name])  # type: ignore[attr-defined]

    assert result["error_code"] == "stale_file"
    assert result["recoverable"] is True
    assert target.read_text(encoding="utf-8") == "human\n"
    assert not (tmp_path / "moved.txt").exists()
    assert not (tmp_path / "copied.txt").exists()
    assert [request.kind for request in surface.requests] == [tool_name]


def test_sensitive_create_rejects_file_created_during_explicit_approval(tmp_path: Path) -> None:
    target = tmp_path / ".env.local"
    surface = _RecordingSurface(
        before_decision=lambda: target.write_text("human\n", encoding="utf-8")
    )
    tools = _tools(tmp_path, surface=surface, mode="auto", yes=True)

    result = tools["fs_write"].run(  # type: ignore[attr-defined]
        {"path": ".env.local", "content": "agent\n"}
    )

    assert result["error_code"] == "stale_file"
    assert target.read_text(encoding="utf-8") == "human\n"


@pytest.mark.parametrize("tool_name", ["fs_move", "fs_copy"])
def test_sensitive_destination_edit_during_approval_is_preserved(
    tmp_path: Path, tool_name: str
) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / ".env.production"
    source.write_text("source\n", encoding="utf-8")
    destination.write_text("before\n", encoding="utf-8")
    surface = _RecordingSurface(
        before_decision=lambda: destination.write_text("human\n", encoding="utf-8")
    )
    tools = _tools(tmp_path, surface=surface, mode="auto", yes=True)

    result = tools[tool_name].run(  # type: ignore[attr-defined]
        {
            "source_path": "source.txt",
            "destination_path": ".env.production",
            "overwrite": True,
        }
    )

    assert result["error_code"] == "stale_file"
    assert source.read_text(encoding="utf-8") == "source\n"
    assert destination.read_text(encoding="utf-8") == "human\n"


def test_sensitive_edit_validation_error_does_not_echo_file_content(tmp_path: Path) -> None:
    secret = "PRIVATE_VALUE_THAT_MUST_NOT_BE_IN_ERRORS"
    (tmp_path / ".env").write_text(secret, encoding="utf-8")
    surface = _RecordingSurface()
    tools = _tools(tmp_path, surface=surface, mode="fullaccess", yes=True)

    with pytest.raises(AgentRuntimeError) as raised:
        tools["fs_edit"].run(  # type: ignore[attr-defined]
            {
                "path": ".env",
                "edits": [
                    {
                        "op": "replace_lines",
                        "start_line": 1,
                        "end_line": 1,
                        "expected_old": "a different value",
                        "replacement": "replacement",
                    }
                ],
            }
        )

    assert secret not in str(raised.value)
    assert "content details were redacted" in str(raised.value)


def test_env_example_remains_readable_without_approval(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("TOKEN=placeholder\n", encoding="utf-8")
    surface = _RecordingSurface(decision=ApprovalDecision(allow=False))
    tools = _tools(tmp_path, surface=surface, mode="fullaccess", yes=True)

    result = tools["fs_read"].run({"path": ".env.example"})  # type: ignore[attr-defined]

    assert result["content"].splitlines() == ["TOKEN=placeholder"]
    assert "_alysis_output_policy" not in result
    assert surface.requests == []


@pytest.fixture(autouse=True)
def file_semantics_without_os_backend(monkeypatch):
    # These unit tests isolate approval, stale-file and ledger behavior.
    # Native/WSL containment is exercised by the installed-runtime qualifier.
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
