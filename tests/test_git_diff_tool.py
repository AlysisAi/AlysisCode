from __future__ import annotations

import io
import socket
import subprocess
from pathlib import Path

import httpx
import pytest
from rich.console import Console

from alysis_code.agent.tools_assembly import build_tools
from alysis_code.config import AppConfig
from alysis_code.session_store import SessionStore
from alysis_code.tools import git as git_tools
from alysis_code.tools.git import GitError, git_diff, git_status


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, tmp_path_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    private = tmp_path_factory.mktemp("git-diff-private")
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(private / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(private / "data"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    def deny_network(*_args, **_kwargs):
        pytest.fail("Git diff tests must not call a provider or use the network")

    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_network)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


def _repo(tmp_path: Path, *, changes: bool = True) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Git diff fixture")
    _git(root, "config", "user.email", "fixture@example.invalid")
    for name in ("staged.txt", "unstaged.txt"):
        (root / name).write_text("original\n")
    _git(root, "add", "staged.txt", "unstaged.txt")
    _git(root, "commit", "-qm", "Initial fixture")
    if changes:
        (root / "staged.txt").write_text("index-only change\n")
        _git(root, "add", "staged.txt")
        (root / "unstaged.txt").write_text("worktree-only change\n")
    return root


def _tool(root: Path, *, mode: str = "auto", depth: int = 0):
    return build_tools(
        root=root,
        console=Console(file=io.StringIO(), force_terminal=False),
        store=SessionStore(
            enabled=False,
            sessions_dir=root.parent / "sessions",
            session_id="git-diff-test",
            cwd=str(root),
            repo_root=str(root),
        ),
        mode=mode,
        yes=True,
        cfg=AppConfig(model="test-model"),
        non_interactive=True,
        subagent_depth=depth,
    )["git_diff"]


@pytest.mark.parametrize(("mode", "depth"), [("auto", 0), ("review", 1)])
@pytest.mark.parametrize(
    ("arguments", "view"),
    [({}, "unstaged"), ({"staged": False}, "unstaged"), ({"staged": True}, "staged")],
)
def test_assembled_diff_selects_one_view_without_changing_repo(
    tmp_path: Path, arguments: dict, view: str, mode: str, depth: int
) -> None:
    root = _repo(tmp_path)
    expected = {"staged": _git(root, "diff", "--cached"), "unstaged": _git(root, "diff")}
    assert expected["staged"] and expected["unstaged"] != expected["staged"]
    tool = _tool(root, mode=mode, depth=depth)
    paths = [root / "staged.txt", root / "unstaged.txt", root / ".git/index"]
    before = {path: path.read_bytes() for path in paths}

    result = tool.run(arguments)

    assert result["diff"] == expected[view]
    assert result["view"] == view
    assert {path: path.read_bytes() for path in paths} == before
    assert _git(root, "diff", "--cached") == expected["staged"]
    assert _git(root, "diff") == expected["unstaged"]


@pytest.mark.parametrize("staged", [False, True])
def test_empty_diff_still_identifies_selected_view(tmp_path: Path, staged: bool) -> None:
    result = _tool(_repo(tmp_path, changes=False)).run({"staged": staged})

    assert result["diff"] == ""
    assert result["view"] == ("staged" if staged else "unstaged")
    assert result["next_offset"] is None
    assert result["truncated"] is False


@pytest.mark.parametrize(
    "arguments",
    [
        {"cached": True},
        {"unknown": True},
        {"staged": True, "cached": True},
        {"staged": "false"},
        {"staged": 0},
        {"staged": 1},
        {"staged": None},
    ],
)
def test_invalid_diff_arguments_fail_before_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: dict
) -> None:
    tool = _tool(_repo(tmp_path))

    def unexpected_git(**_kwargs):
        pytest.fail("Invalid git_diff arguments reached Git")

    monkeypatch.setattr(git_tools, "_run_git_checked", unexpected_git)

    with pytest.raises((git_tools.GitError, TypeError)):
        tool.run(arguments)


def test_model_schema_and_help_describe_the_supported_view_option(tmp_path: Path) -> None:
    model_tool = _tool(_repo(tmp_path)).as_openai_tool()["function"]
    parameters = model_tool["parameters"]

    assert set(parameters["properties"]) == {"path", "staged", "offset", "diff_id"}
    assert parameters["properties"]["staged"]["type"] == "boolean"
    assert parameters["properties"]["staged"]["default"] is False
    assert parameters["required"] == []
    assert parameters["additionalProperties"] is False
    assert "staged=true" in model_tool["description"]
    assert "unstaged" in model_tool["description"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "core.autocrlf", "false")
    for name in ("first.txt", "literal[1].txt"):
        (tmp_path / name).write_text("original\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "baseline")
    return tmp_path


def test_diff_pages_reconstruct_the_complete_patch(repo: Path) -> None:
    (repo / "first.txt").write_text("line with unicode café " * 3000 + "\n", encoding="utf-8")
    first = page = git_diff(root=repo)
    parts = [page["diff"]]
    assert first["truncated"] is True
    while page["next_offset"] is not None:
        page = git_diff(root=repo, offset=page["next_offset"], diff_id=page["diff_id"])
        assert len(page["diff"]) <= 20000
        parts.append(page["diff"])
    expected = _git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-color")
    assert "".join(parts) == expected
    assert first["total_chars"] == len(expected)
    assert page["truncated"] is False


def test_diff_scopes_literal_paths_and_staged_changes(repo: Path) -> None:
    (repo / "literal[1].txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "literal[1].txt")
    (repo / "first.txt").write_text("unstaged\n", encoding="utf-8")
    assert git_diff(root=repo, path="literal[1].txt")["diff"] == ""
    page = git_diff(root=repo, path="literal[1].txt", staged=True)
    assert "+staged" in page["diff"]
    assert "first.txt" not in page["diff"]
    assert "+unstaged" in git_diff(root=repo)["diff"]
    with pytest.raises(GitError, match="escapes root"):
        git_diff(root=repo, path="../outside")


def test_diff_decodes_unicode_independently_of_windows_locale(repo: Path) -> None:
    content = "Γεια σου κόσμε — café “quoted” 👋\n"
    (repo / "first.txt").write_text(content, encoding="utf-8")
    assert "+" + content in git_diff(root=repo)["diff"]


def test_diff_requires_a_matching_page_identity(repo: Path) -> None:
    (repo / "first.txt").write_text("long line " * 3000, encoding="utf-8")
    page = git_diff(root=repo)
    with pytest.raises(GitError, match="previous page"):
        git_diff(root=repo, offset=20000)
    (repo / "first.txt").write_text("changed again\n", encoding="utf-8")
    with pytest.raises(GitError, match="changed between pages"):
        git_diff(root=repo, offset=20000, diff_id=page["diff_id"])
    for bad_offset in (-1, True, "20000"):
        with pytest.raises(GitError, match="non-negative integer"):
            git_diff(root=repo, offset=bad_offset)  # type: ignore[arg-type]


def test_review_does_not_run_configured_git_programs(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "first.txt").write_text("changed\n", encoding="utf-8")
    # Each configured program would leave a marker if Git invoked it.
    program = "echo unexpected > review-hook-ran"
    _git(repo, "config", "core.fsmonitor", program)
    _git(repo, "config", "diff.external", program)
    _git(repo, "config", "diff.review.textconv", program)
    _git(repo, "config", "filter.review.clean", program)
    _git(repo, "config", "filter.review.process", program)
    _git(repo, "config", "filter.review.required", "true")
    (repo / ".gitattributes").write_text("*.txt diff=review filter=review\n", encoding="utf-8")
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", program)
    for review in (git_diff, git_status):
        with pytest.raises(GitError, match="git_content_filter_required"):
            review(root=repo)
    assert git_diff(root=repo, staged=True)["diff"] == ""
    assert not (repo / "review-hook-ran").exists()


def test_filtered_unchanged_files_are_never_reported_as_modifications(repo: Path) -> None:
    _git(repo, "config", "filter.demo.clean", "git hash-object --stdin")
    _git(repo, "config", "filter.demo.required", "true")
    (repo / ".gitattributes").write_text("first.txt filter=demo\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "filtered baseline")
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "diff") == ""
    for review in (git_diff, git_status):
        with pytest.raises(GitError, match="cannot compare these files accurately"):
            review(root=repo)
    assert git_diff(root=repo, staged=True)["diff"] == ""
    # A global filter configuration must not block an unaffected scoped read.
    (repo / "literal[1].txt").write_text("real edit\n", encoding="utf-8")
    assert "+real edit" in git_diff(root=repo, path="literal[1].txt")["diff"]


def test_unused_filter_configuration_keeps_native_review_available(repo: Path) -> None:
    _git(repo, "config", "filter.lfs.clean", "echo unexpected > review-hook-ran")
    (repo / "first.txt").write_text("real edit\n", encoding="utf-8")
    assert "+real edit" in git_diff(root=repo)["diff"]
    assert "first.txt" in git_status(root=repo)["status"]
    assert not (repo / "review-hook-ran").exists()


def test_native_review_does_not_recurse_into_submodule_helpers(repo: Path) -> None:
    commit = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},nested")
    for review in (git_diff, git_status):
        with pytest.raises(GitError, match="git_submodule_review_required"):
            review(root=repo)
    assert "nested" in git_diff(root=repo, staged=True)["diff"]
    (repo / "first.txt").write_text("scoped change\n", encoding="utf-8")
    assert "+scoped change" in git_diff(root=repo, path="first.txt")["diff"]


def test_registered_diff_tool_forwards_page_and_scope_options(repo: Path) -> None:
    import io
    import json

    from rich.console import Console

    from alysis_code.agent.tools_assembly import build_tools
    from alysis_code.config import AppConfig
    from alysis_code.session_store import SessionStore

    (repo / "literal[1].txt").write_text("staged text " * 3000, encoding="utf-8")
    _git(repo, "add", "literal[1].txt")
    tools = build_tools(
        root=repo,
        console=Console(file=io.StringIO(), force_terminal=False),
        store=SessionStore(
            enabled=False,
            sessions_dir=repo / "sessions",
            session_id="diff-test",
            cwd=str(repo),
            repo_root=str(repo),
        ),
        mode="auto",
        yes=True,
        cfg=AppConfig(model="test-model"),
        non_interactive=True,
    )
    tool = tools["git_diff"]
    first = git_diff(root=repo, path="literal[1].txt", staged=True)
    args = {
        "path": "literal[1].txt",
        "staged": True,
        "offset": first["next_offset"],
        "diff_id": first["diff_id"],
    }
    result = tool.run(args)
    if isinstance(result, str):
        result = json.loads(result)
    assert result["offset"] == 20000
    assert result["diff"] == git_diff(root=repo, **args)["diff"]
