"""One approval per deleted file, and 'always' that never stuck.

Three stacked defects, three fixes under test here:
1. The TUI surface had NO session-grant store - the modal's "[a] always"
   decision was returned and forgotten. TuiSurface now keeps exact-scope and
   folder grants and auto-approves matches without re-prompting.
2. An exact-file-set scope can never cover the NEXT file of a batch. The new
   "[d] always for this folder" grant (approval_scope dir-grant helpers)
   covers same-operation deletes under one directory, opt-in per session.
3. fs_delete was single-file only, so N files = N prompts. It now accepts a
   ``paths`` list handled behind ONE approval that lists every file.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from alysis_code.agent_loop import build_tools
from alysis_code.approval_scope import (
    approval_dir_grant_candidate,
    exact_file_set_scope,
    request_matches_dir_grants,
)
from alysis_code.config import AppConfig
from alysis_code.session_store import SessionStore
from alysis_code.surface.types import ApprovalDecision, ApprovalRequest


def _delete_request(files: list[str], **metadata) -> ApprovalRequest:
    return ApprovalRequest(
        kind="fs_delete",
        reason="file deletion requires confirmation",
        preview="Delete file",
        files=files,
        metadata=dict(metadata),
        allow_for_session_scope=exact_file_set_scope(files, operation="fs_delete"),
    )


# ------------------------- dir-grant scope helpers -------------------------


def test_dir_grant_candidate_single_directory():
    request = _delete_request(["pkg/__pycache__/a.cpython-312.pyc"])
    assert approval_dir_grant_candidate(request) == "pkg/__pycache__/"


def test_dir_grant_candidate_rejects_mixed_dirs_root_and_traversal():
    assert approval_dir_grant_candidate(_delete_request(["a/x.pyc", "b/y.pyc"])) is None
    assert approval_dir_grant_candidate(_delete_request(["rootfile.txt"])) is None
    assert approval_dir_grant_candidate(_delete_request(["../evil/x.pyc"])) is None
    other_kind = ApprovalRequest(kind="fs_write", reason="r", preview="p", files=["d/x.py"])
    assert approval_dir_grant_candidate(other_kind) is None


def test_dir_grant_candidate_never_offered_for_sensitive_approvals():
    request = _delete_request(["secrets/creds.env"], allow_for_session_disabled=True)
    assert approval_dir_grant_candidate(request) is None


def test_dir_grant_candidate_kill_switch(monkeypatch):
    monkeypatch.setenv("ALYSIS_APPROVAL_DIR_GRANTS", "0")
    request = _delete_request(["pkg/__pycache__/a.pyc"])
    assert approval_dir_grant_candidate(request) is None


def test_dir_grant_matching_covers_nested_and_rejects_outside():
    grants = {("fs_delete", "pkg/__pycache__/")}
    inside = _delete_request(["pkg/__pycache__/b.cpython-312.pyc"])
    nested = _delete_request(["pkg/__pycache__/sub/c.pyc"])
    outside = _delete_request(["pkg/src/keep.py"])
    mixed = _delete_request(["pkg/__pycache__/d.pyc", "pkg/src/keep.py"])
    assert request_matches_dir_grants(inside, grants) is True
    assert request_matches_dir_grants(nested, grants) is True
    assert request_matches_dir_grants(outside, grants) is False
    assert request_matches_dir_grants(mixed, grants) is False


def test_dir_grant_matching_kill_switch(monkeypatch):
    monkeypatch.setenv("ALYSIS_APPROVAL_DIR_GRANTS", "off")
    grants = {("fs_delete", "pkg/__pycache__/")}
    inside = _delete_request(["pkg/__pycache__/b.pyc"])
    assert request_matches_dir_grants(inside, grants) is False


# ------------------------- TUI surface session grants -------------------------


def _tui_surface(decisions: list[ApprovalDecision]):
    from alysis_code.cli_impl.tui.surface import TuiSurface
    from alysis_code.cli_impl.tui.transcript import TuiTranscript

    calls: list[ApprovalRequest] = []

    def _ui(request: ApprovalRequest) -> ApprovalDecision:
        calls.append(request)
        return decisions[min(len(calls) - 1, len(decisions) - 1)]

    return TuiSurface(TuiTranscript(), request_approval_ui=_ui), calls


def test_tui_always_now_sticks_for_identical_request():
    # The headline defect: the TUI surface never stored 'always'. Same request
    # twice must prompt exactly once.
    surface, calls = _tui_surface([ApprovalDecision(allow=True, allow_for_session=True)])
    request = _delete_request(["pkg/__pycache__/a.pyc"])

    first = surface.request_approval(request)
    second = surface.request_approval(request)

    assert first.allow and second.allow
    assert len(calls) == 1  # pre-fix: 2 (every identical approval re-prompted)


def test_tui_folder_grant_covers_next_file_in_directory():
    surface, calls = _tui_surface([ApprovalDecision(allow=True, allow_for_session_dir=True)])
    first = surface.request_approval(_delete_request(["pkg/__pycache__/a.pyc"]))
    second = surface.request_approval(_delete_request(["pkg/__pycache__/b.pyc"]))
    third = surface.request_approval(_delete_request(["pkg/__pycache__/sub/c.pyc"]))

    assert first.allow and second.allow and third.allow
    assert len(calls) == 1  # one human decision covered the whole folder


def test_tui_folder_grant_does_not_cover_other_directories():
    surface, calls = _tui_surface(
        [
            ApprovalDecision(allow=True, allow_for_session_dir=True),
            ApprovalDecision(allow=False),
        ]
    )
    first = surface.request_approval(_delete_request(["pkg/__pycache__/a.pyc"]))
    outside = surface.request_approval(_delete_request(["pkg/src/keep.py"]))

    assert first.allow
    assert outside.allow is False
    assert len(calls) == 2  # the out-of-folder delete was prompted (and denied)


def test_tui_always_downgrades_when_no_safe_scope():
    # Sensitive-style approvals withhold the exact scope; 'a' must become a
    # one-time allow instead of leaking allow_for_session=True into guards
    # that reject it with a runtime error.
    surface, calls = _tui_surface([ApprovalDecision(allow=True, allow_for_session=True)])
    request = ApprovalRequest(
        kind="fs_delete",
        reason="sensitive files require an explicit one-time approval",
        preview="Delete file",
        files=["secrets/creds.env"],
        metadata={"mandatory_explicit_approval": True, "allow_for_session_disabled": True},
        allow_for_session_scope=None,
    )

    decision = surface.request_approval(request)

    assert decision.allow is True
    assert decision.allow_for_session is False
    assert len(calls) == 1
    # And nothing stuck: the same request prompts again.
    surface.request_approval(request)
    assert len(calls) == 2


def test_tui_dir_grant_kill_switch(monkeypatch):
    monkeypatch.setenv("ALYSIS_APPROVAL_DIR_GRANTS", "0")
    surface, calls = _tui_surface(
        [
            ApprovalDecision(allow=True, allow_for_session_dir=True),
            ApprovalDecision(allow=True),
        ]
    )
    surface.request_approval(_delete_request(["pkg/__pycache__/a.pyc"]))
    surface.request_approval(_delete_request(["pkg/__pycache__/b.pyc"]))
    assert len(calls) == 2  # switch off: nothing widened


# ------------------------- batch fs_delete executor -------------------------


class _RecordingSurface:
    def __init__(self, decisions: list[ApprovalDecision] | None = None) -> None:
        self.requests: list[ApprovalRequest] = []
        self._decisions = decisions or [ApprovalDecision(allow=True)]

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return self._decisions[min(len(self.requests) - 1, len(self._decisions) - 1)]


def _store(root: Path) -> SessionStore:
    return SessionStore(
        enabled=False,
        sessions_dir=root / "sessions",
        session_id="fs-delete-batch-test",
        cwd=str(root),
        repo_root=str(root),
    )


def _build_tools(tmp_path: Path, *, surface=None, yes: bool = True):
    return build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO(), force_terminal=False),
        surface=surface,
        store=_store(tmp_path),
        mode="auto",
        yes=yes,
        cfg=AppConfig(model="test-model"),
        non_interactive=surface is None,
    )


def _make_pyc_files(tmp_path: Path, count: int) -> list[str]:
    cache = tmp_path / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    paths = []
    for index in range(count):
        target = cache / f"mod{index}.cpython-312.pyc"
        target.write_bytes(b"pyc")
        paths.append(f"pkg/__pycache__/{target.name}")
    return paths


def test_batch_delete_single_approval_lists_every_file(tmp_path: Path) -> None:
    paths = _make_pyc_files(tmp_path, 4)
    surface = _RecordingSurface()
    tools = _build_tools(tmp_path, surface=surface, yes=False)

    result = tools["fs_delete"].run({"paths": paths})

    assert len(surface.requests) == 1  # pre-fix: four prompts, one per file
    request = surface.requests[0]
    assert sorted(request.files) == sorted(paths)
    assert request.preview.startswith("Delete 4 files")
    assert result["ok"] is True
    assert result["deleted_count"] == 4
    assert all(not (tmp_path / path).exists() for path in paths)


def test_batch_delete_declined_deletes_nothing(tmp_path: Path) -> None:
    paths = _make_pyc_files(tmp_path, 2)
    surface = _RecordingSurface([ApprovalDecision(allow=False)])
    tools = _build_tools(tmp_path, surface=surface, yes=False)

    with pytest.raises(Exception, match="declined|denied|approval"):
        tools["fs_delete"].run({"paths": paths})

    assert all((tmp_path / path).exists() for path in paths)


def test_single_path_form_unchanged(tmp_path: Path) -> None:
    paths = _make_pyc_files(tmp_path, 1)
    tools = _build_tools(tmp_path)

    result = tools["fs_delete"].run({"path": paths[0]})

    assert result.get("path") == paths[0]
    assert "results" not in result
    assert not (tmp_path / paths[0]).exists()


def test_path_and_paths_together_name_one_list(tmp_path: Path) -> None:
    paths = _make_pyc_files(tmp_path, 3)
    tools = _build_tools(tmp_path)

    result = tools["fs_delete"].run({"path": paths[0], "paths": paths[1:] + [paths[0]]})

    assert result["ok"] is True and result["deleted_count"] == 3
    assert [entry["path"] for entry in result["results"]] == paths
    assert not any((tmp_path / path).exists() for path in paths)


def test_a_folder_is_named_as_one_and_the_files_beside_it_are_removed(tmp_path: Path) -> None:
    paths = _make_pyc_files(tmp_path, 2)
    (tmp_path / "build" / "obj").mkdir(parents=True)
    (tmp_path / "build" / "obj" / "a.o").write_bytes(b"o")
    tools = _build_tools(tmp_path)

    with pytest.raises(Exception, match="build is a folder.*one shell command"):
        tools["fs_delete"].run({"path": "build"})
    assert (tmp_path / "build" / "obj" / "a.o").exists()

    result = tools["fs_delete"].run({"paths": ["build", *paths, "build/obj"]})

    assert result["ok"] is False
    assert result["deleted_count"] == 2 and result["requested_count"] == 4
    by_path = {entry["path"]: entry for entry in result["results"]}
    assert all(by_path[path]["ok"] for path in paths)
    assert "is a folder" in by_path["build"]["error"] and not by_path["build"]["ok"]
    assert "is a folder" in by_path["build/obj"]["error"]
    assert not any((tmp_path / path).exists() for path in paths)
    assert (tmp_path / "build" / "obj" / "a.o").exists()  # nothing inside a folder is touched

    only_folders = tools["fs_delete"].run({"paths": ["build"]})
    assert only_folders["ok"] is False and only_folders["deleted_count"] == 0


def test_one_missing_file_does_not_undo_the_rest_of_a_list(tmp_path: Path) -> None:
    paths = _make_pyc_files(tmp_path, 2)
    tools = _build_tools(tmp_path)

    result = tools["fs_delete"].run({"paths": [paths[0], "pkg/gone.pyc", paths[1]]})

    assert result["ok"] is False and result["deleted_count"] == 2
    by_path = {entry["path"]: entry for entry in result["results"]}
    assert by_path[paths[0]]["ok"] and by_path[paths[1]]["ok"]
    assert not by_path["pkg/gone.pyc"]["ok"] and by_path["pkg/gone.pyc"]["error"]
    assert not any((tmp_path / path).exists() for path in paths)


def test_the_line_shown_for_the_call_names_every_file(tmp_path: Path) -> None:
    from alysis_code.tools.registry import _preview_delete_paths

    shown = _preview_delete_paths({"path": "src/important.py", "paths": ["a.pyc", "b.pyc"]})
    assert shown == "3 files: src/important.py, a.pyc, b.pyc"
    assert _preview_delete_paths({"path": "a.pyc", "paths": ["a.pyc"]}) == "a.pyc"


def test_schema_offers_paths_and_no_longer_requires_path(tmp_path: Path) -> None:
    tools = _build_tools(tmp_path)
    schema = tools["fs_delete"].as_openai_tool()["function"]["parameters"]

    assert "paths" in schema["properties"]
    assert "required" not in schema or "path" not in schema.get("required", [])


def test_a_folder_is_refused_with_what_to_do_instead(tmp_path: Path) -> None:
    """It used to fail with the operating system's "Access is denied"."""
    _make_pyc_files(tmp_path, 2)
    surface = _RecordingSurface()
    tools = _build_tools(tmp_path, surface=surface, yes=False)

    with pytest.raises(Exception, match=r"deletes files, and pkg.__pycache__ is a folder"):
        tools["fs_delete"].run({"path": "pkg/__pycache__"})

    # Nobody is asked to approve something that cannot happen.
    assert surface.requests == []
    assert len(list((tmp_path / "pkg" / "__pycache__").iterdir())) == 2


def test_a_folder_among_files_is_not_shown_for_approval(tmp_path: Path) -> None:
    """The file beside it is approved and removed; the folder is that entry's error."""
    paths = _make_pyc_files(tmp_path, 2)
    surface = _RecordingSurface()
    tools = _build_tools(tmp_path, surface=surface, yes=False)

    result = tools["fs_delete"].run({"paths": [paths[0], "pkg"]})

    (request,) = surface.requests
    assert list(request.files) == [paths[0]]
    by_path = {entry["path"]: entry for entry in result["results"]}
    assert by_path[paths[0]]["ok"] and not (tmp_path / paths[0]).exists()
    assert not by_path["pkg"]["ok"]
    assert "deletes files, and pkg is a folder" in by_path["pkg"]["error"]
    assert (tmp_path / paths[1]).exists()  # not listed, so not touched
