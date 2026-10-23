from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from alysis_code.config import AppConfig, ConfigError
from alysis_code.forge import create_plan_run
from alysis_code.llm.openai_compat import LLMError
from alysis_code.merge_conflict_reviewer import (
    ConflictReviewOutcome,
    capture_merge_conflict_context,
    review_merge_conflict,
    try_abort_merge,
    write_conflict_artifacts,
)


def _cp(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["git"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _git_args(cmd: list[str]) -> list[str]:
    assert cmd[0] == "git"
    assert cmd[1] == "-C"
    args = cmd[3:]
    cleaned: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "-c":
            i += 2
            continue
        cleaned.append(args[i])
        i += 1
    return cleaned


def test_capture_merge_conflict_context_collects_stage_blobs(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "x.py").write_text(
        "<<<<<<< ours\nA\n=======\nB\n>>>>>>> theirs\n",
        encoding="utf-8",
    )

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        if args == ["status", "--porcelain=v1"]:
            return _cp(stdout="UU src/x.py\n")
        if args == ["diff", "--name-only", "--diff-filter=U"]:
            return _cp(stdout="src/x.py\n")
        if args == ["show", ":1:src/x.py"]:
            return _cp(stdout="base-version\n")
        if args == ["show", ":2:src/x.py"]:
            return _cp(stdout="ours-version\n")
        if args == ["show", ":3:src/x.py"]:
            return _cp(stdout="theirs-version\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    context = capture_merge_conflict_context(
        repo,
        base_branch="main",
        task_branch="feat/t01",
        merge_error="merge failed",
    )

    assert context["base_branch"] == "main"
    assert context["task_branch"] == "feat/t01"
    assert context["merge_error"] == "merge failed"
    assert context["unmerged_files"] == ["src/x.py"]
    assert "UU src/x.py" in context["git_status_porcelain"]
    assert len(context["files"]) == 1

    per_file = context["files"][0]
    assert per_file["path"] == "src/x.py"
    assert "base-version" in per_file["base_version_excerpt"]
    assert "ours-version" in per_file["ours_version_excerpt"]
    assert "theirs-version" in per_file["theirs_version_excerpt"]
    assert "<<<<<<< ours" in per_file["working_excerpt"]


def test_write_conflict_artifacts_creates_expected_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)

    context = {
        "base_branch": "main",
        "task_branch": "feat/t01",
        "merge_error": "conflict",
        "git_status_porcelain": "UU src/x.py",
        "unmerged_files": ["src/x.py"],
        "files": [],
    }
    review_json = {
        "task_id": "T01",
        "confidence": "medium",
        "summary": "Conflict due to overlapping edits",
        "root_cause": "same hunk changed",
        "recommended_strategy": "manual merge",
        "per_file": [],
        "next_steps": ["resolve and commit"],
    }

    artifacts = write_conflict_artifacts(
        paths=paths,
        task_id="T01",
        context=context,
        review_json=review_json,
        review_md="# Merge conflict review\n",
        cleanup_log="$ git merge --abort\nreturncode=0\n",
    )

    assert artifacts.context_json_path.exists()
    assert artifacts.review_json_path is not None
    assert artifacts.review_json_path.exists()
    assert artifacts.review_md_path.exists()
    assert artifacts.cleanup_log_path.exists()

    saved_context = json.loads(artifacts.context_json_path.read_text(encoding="utf-8"))
    saved_review = json.loads(artifacts.review_json_path.read_text(encoding="utf-8"))
    assert saved_context["unmerged_files"] == ["src/x.py"]
    assert saved_review["task_id"] == "T01"
    assert "$ git merge --abort" in artifacts.cleanup_log_path.read_text(encoding="utf-8")


def test_review_merge_conflict_skips_when_api_key_missing(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)

    monkeypatch.setattr(
        "alysis_code.merge_conflict_reviewer.get_api_key",
        lambda: (_ for _ in ()).throw(ConfigError("missing key")),
    )

    outcome = review_merge_conflict(
        paths=paths,
        task={"id": "T01", "title": "Resolve merge conflict"},
        cfg=AppConfig(model="test-model"),
        api_key_override=None,
        context={"unmerged_files": ["src/x.py"]},
    )

    assert isinstance(outcome, ConflictReviewOutcome)
    assert outcome.review_json is None
    assert outcome.skipped_reason == "missing key"
    assert "LLM conflict review skipped" in outcome.review_markdown


def test_try_abort_merge_falls_back_to_reset_and_logs_commands(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def fake_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        if args == ["merge", "--abort"]:
            return _cp(returncode=1, stderr="not currently merging")
        if args == ["reset", "--hard"]:
            return _cp(returncode=0, stdout="HEAD is now at abc123")
        if args == ["checkout", "main"]:
            return _cp(returncode=0, stdout="Switched to branch 'main'")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, cleanup_log = try_abort_merge(repo, base_branch="main")

    assert ok is True
    assert "$ git -C " in cleanup_log
    assert "merge --abort" in cleanup_log
    assert "reset --hard" in cleanup_log
    assert "checkout main" in cleanup_log


def test_review_merge_conflict_uses_role_model_override(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)

    payload = {
        "task_id": "T01",
        "confidence": "high",
        "summary": "resolve carefully",
        "root_cause": "same hunk",
        "recommended_strategy": "manual merge",
        "per_file": [
            {
                "path": "src/x.py",
                "recommended_resolution": "manual_merge",
                "notes": "resolve markers",
                "risk": "medium",
            }
        ],
        "next_steps": ["resolve and verify"],
    }
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            captured["model"] = kwargs["model"]
            captured["temperature"] = kwargs["temperature"]
            captured["timeout_s"] = kwargs["timeout_s"]

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            return type("Resp", (), {"content": json.dumps(payload)})()

    monkeypatch.setattr(
        "alysis_code.merge_conflict_reviewer.OpenAICompatClient",
        FakeClient,
    )

    outcome = review_merge_conflict(
        paths=paths,
        task={"id": "T01", "title": "Resolve merge conflict"},
        cfg=AppConfig(
            model="default-model",
            conflict_review_temperature=0.09,
            llm_timeout_s=19.0,
        ),
        api_key_override="k",
        context={"unmerged_files": ["src/x.py"]},
        plan={"role_models": {"conflict_review": "plan-conflict-review-model"}},
    )
    assert outcome.review_json is not None
    assert captured["model"] == "plan-conflict-review-model"
    assert captured["temperature"] == 0.09
    assert captured["timeout_s"] == 19.0


def test_review_merge_conflict_warns_for_fallback_model_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    warnings_seen: list[str] = []

    def _warn(message: str, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        _ = args, kwargs
        warnings_seen.append(str(message))

    payload = {
        "task_id": "T01",
        "confidence": "high",
        "summary": "resolve carefully",
        "root_cause": "same hunk",
        "recommended_strategy": "manual merge",
        "per_file": [],
        "next_steps": ["resolve and verify"],
    }

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            return type("Resp", (), {"content": json.dumps(payload)})()

    monkeypatch.setattr("warnings.warn", _warn)
    monkeypatch.setattr(
        "alysis_code.merge_conflict_reviewer.OpenAICompatClient",
        FakeClient,
    )

    outcome = review_merge_conflict(
        paths=paths,
        task={"id": "T01", "title": "Resolve merge conflict"},
        cfg=AppConfig(model="unknown-model-xyz"),
        api_key_override="k",
        context={"unmerged_files": ["src/x.py"]},
        plan=None,
    )

    assert outcome.review_json is not None
    assert warnings_seen
    assert "unknown-model-xyz" in warnings_seen[0]


def test_review_merge_conflict_strict_model_metadata_policy_fails_before_client(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            raise AssertionError("OpenAICompatClient should not be constructed in strict mode")

    monkeypatch.setattr(
        "alysis_code.merge_conflict_reviewer.OpenAICompatClient",
        FakeClient,
    )

    with pytest.raises(ConfigError, match="model_metadata_policy=strict"):
        review_merge_conflict(
            paths=paths,
            task={"id": "T01", "title": "Resolve merge conflict"},
            cfg=AppConfig(model="unknown-model-xyz", model_metadata_policy="strict"),
            api_key_override="k",
            context={"unmerged_files": ["src/x.py"]},
            plan=None,
        )


def test_review_merge_conflict_retries_transient_request_failure_once(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    calls = {"count": 0}
    payload = {
        "task_id": "T01",
        "confidence": "high",
        "summary": "resolve carefully",
        "root_cause": "same hunk",
        "recommended_strategy": "manual merge",
        "per_file": [],
        "next_steps": ["resolve and verify"],
    }

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            calls["count"] += 1
            if calls["count"] == 1:
                raise LLMError("LLM request failed: ReadTimeout")
            return type("Resp", (), {"content": json.dumps(payload)})()

    monkeypatch.setattr(
        "alysis_code.merge_conflict_reviewer.OpenAICompatClient",
        FakeClient,
    )

    outcome = review_merge_conflict(
        paths=paths,
        task={"id": "T01", "title": "Resolve merge conflict"},
        cfg=AppConfig(model="test-model"),
        api_key_override="k",
        context={"unmerged_files": ["src/x.py"]},
    )

    assert calls["count"] == 2
    assert outcome.review_json is not None
    assert outcome.skipped_reason is None
    assert outcome.request_retry_count == 1
    assert outcome.request_retry_state == "recovered"
    assert "Request Retries: 1 transient retry before successful review." in outcome.review_markdown


def test_review_merge_conflict_retry_exhaustion_stays_skipped(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    calls = {"count": 0}

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            calls["count"] += 1
            raise LLMError("LLM request failed: ReadTimeout")

    monkeypatch.setattr(
        "alysis_code.merge_conflict_reviewer.OpenAICompatClient",
        FakeClient,
    )

    outcome = review_merge_conflict(
        paths=paths,
        task={"id": "T01", "title": "Resolve merge conflict"},
        cfg=AppConfig(model="test-model"),
        api_key_override="k",
        context={"unmerged_files": ["src/x.py"]},
    )

    assert calls["count"] == 2
    assert outcome.review_json is None
    assert outcome.request_retry_count == 1
    assert outcome.request_retry_state == "exhausted"
    assert outcome.skipped_reason == (
        "review failed after 1 transient retry: LLM request failed: ReadTimeout"
    )
    assert "Request Retries: 1 transient retry exhausted before review was skipped." in (
        outcome.review_markdown
    )


def test_review_merge_conflict_retry_exhaustion_carries_provider_category(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            raise LLMError("LLM request failed: ReadTimeout")

    monkeypatch.setattr(
        "alysis_code.merge_conflict_reviewer.OpenAICompatClient",
        FakeClient,
    )

    outcome = review_merge_conflict(
        paths=paths,
        task={"id": "T01", "title": "Resolve merge conflict"},
        cfg=AppConfig(model="test-model"),
        api_key_override="k",
        context={"unmerged_files": ["src/x.py"]},
    )

    assert outcome.review_json is None
    assert outcome.failure_category == "provider_unavailable"


def test_review_merge_conflict_auth_failure_carries_provider_error_category(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            raise LLMError("LLM error 401: invalid_api_key")

    monkeypatch.setattr(
        "alysis_code.merge_conflict_reviewer.OpenAICompatClient",
        FakeClient,
    )

    outcome = review_merge_conflict(
        paths=paths,
        task={"id": "T01", "title": "Resolve merge conflict"},
        cfg=AppConfig(model="test-model"),
        api_key_override="k",
        context={"unmerged_files": ["src/x.py"]},
    )

    assert outcome.review_json is None
    assert outcome.failure_category == "provider_error"


def test_review_merge_conflict_does_not_retry_nontransient_request_error(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    calls = {"count": 0}

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            calls["count"] += 1
            raise LLMError("LLM error 401: invalid_api_key")

    monkeypatch.setattr(
        "alysis_code.merge_conflict_reviewer.OpenAICompatClient",
        FakeClient,
    )

    outcome = review_merge_conflict(
        paths=paths,
        task={"id": "T01", "title": "Resolve merge conflict"},
        cfg=AppConfig(model="test-model"),
        api_key_override="k",
        context={"unmerged_files": ["src/x.py"]},
    )

    assert calls["count"] == 1
    assert outcome.review_json is None
    assert outcome.request_retry_count == 0
    assert outcome.request_retry_state == "none"
    assert outcome.skipped_reason == "review failed: LLM error 401: invalid_api_key"


def test_review_merge_conflict_invalid_json_is_not_retried(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    calls = {"count": 0}

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            calls["count"] += 1
            return type("Resp", (), {"content": "not-json"})()

    monkeypatch.setattr(
        "alysis_code.merge_conflict_reviewer.OpenAICompatClient",
        FakeClient,
    )

    outcome = review_merge_conflict(
        paths=paths,
        task={"id": "T01", "title": "Resolve merge conflict"},
        cfg=AppConfig(model="test-model"),
        api_key_override="k",
        context={"unmerged_files": ["src/x.py"]},
    )

    assert calls["count"] == 1
    assert outcome.review_json is None
    assert outcome.request_retry_count == 0
    assert outcome.request_retry_state == "none"
    assert outcome.skipped_reason == "review failed: model response is not a valid JSON object"


def test_review_merge_conflict_normalization_error_is_not_retried(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    calls = {"count": 0}
    payload = {
        "task_id": "T01",
        "confidence": "high",
        "summary": "resolve carefully",
        "root_cause": "",
        "recommended_strategy": "manual merge",
        "per_file": [],
        "next_steps": ["resolve and verify"],
    }

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            calls["count"] += 1
            return type("Resp", (), {"content": json.dumps(payload)})()

    monkeypatch.setattr(
        "alysis_code.merge_conflict_reviewer.OpenAICompatClient",
        FakeClient,
    )

    outcome = review_merge_conflict(
        paths=paths,
        task={"id": "T01", "title": "Resolve merge conflict"},
        cfg=AppConfig(model="test-model"),
        api_key_override="k",
        context={"unmerged_files": ["src/x.py"]},
    )

    assert calls["count"] == 1
    assert outcome.review_json is None
    assert outcome.request_retry_count == 0
    assert outcome.request_retry_state == "none"
    assert outcome.skipped_reason == "review failed: root_cause is required"


def test_review_merge_conflict_retry_then_invalid_json_is_truthful(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    calls = {"count": 0}

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            calls["count"] += 1
            if calls["count"] == 1:
                raise LLMError("LLM request failed: ReadTimeout")
            return type("Resp", (), {"content": "not-json"})()

    monkeypatch.setattr(
        "alysis_code.merge_conflict_reviewer.OpenAICompatClient",
        FakeClient,
    )

    outcome = review_merge_conflict(
        paths=paths,
        task={"id": "T01", "title": "Resolve merge conflict"},
        cfg=AppConfig(model="test-model"),
        api_key_override="k",
        context={"unmerged_files": ["src/x.py"]},
    )

    assert calls["count"] == 2
    assert outcome.review_json is None
    assert outcome.request_retry_count == 1
    assert outcome.request_retry_state == "final_failure_after_retry"
    assert outcome.skipped_reason == "review failed: model response is not a valid JSON object"
    assert (
        "Request Retries: 1 transient retry before final review failure." in outcome.review_markdown
    )
    assert "exhausted before review was skipped" not in outcome.review_markdown


def test_review_merge_conflict_retry_then_nontransient_request_error_is_truthful(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    calls = {"count": 0}

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            calls["count"] += 1
            if calls["count"] == 1:
                raise LLMError("LLM request failed: ReadTimeout")
            raise LLMError("LLM error 401: invalid_api_key")

    monkeypatch.setattr(
        "alysis_code.merge_conflict_reviewer.OpenAICompatClient",
        FakeClient,
    )

    outcome = review_merge_conflict(
        paths=paths,
        task={"id": "T01", "title": "Resolve merge conflict"},
        cfg=AppConfig(model="test-model"),
        api_key_override="k",
        context={"unmerged_files": ["src/x.py"]},
    )

    assert calls["count"] == 2
    assert outcome.review_json is None
    assert outcome.request_retry_count == 1
    assert outcome.request_retry_state == "final_failure_after_retry"
    assert (
        outcome.skipped_reason
        == "review failed after 1 transient retry: LLM error 401: invalid_api_key"
    )
    assert (
        "Request Retries: 1 transient retry before final review failure." in outcome.review_markdown
    )
    assert "exhausted before review was skipped" not in outcome.review_markdown
