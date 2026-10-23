from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from alysis_code.cli_impl.tui.session_navigation import SessionPickerProvider
from alysis_code.ide.session_search import SessionSearchError


def _log(
    root: Path, workspace: Path, name: str, *, owner: str = "local", legacy: bool = False
) -> Path:
    root.mkdir(exist_ok=True)
    path = root / f"{name}.jsonl"
    event = {"type": "user_message", "owner": owner, "payload": {"content": "Find Ελληνικά Straße"}}
    event["git_root" if legacy else "workspace_root"] = str(workspace)
    path.write_text(json.dumps(event) + "\n{malformed\n[]\n", encoding="utf-8")
    return path


def test_picker_scope_rename_and_unicode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("alysis_code.session_store.local_session_owner", lambda: "local")
    sessions, workspace = tmp_path / "sessions", tmp_path / "workspace"
    path = _log(sessions, workspace, "ours")
    _log(sessions, workspace, "foreign", owner="other")
    _log(sessions, tmp_path / "other", "elsewhere")
    provider = SessionPickerProvider(sessions, workspace)
    assert [row.session_id for row in provider.search("Ελληνικά").rows] == ["ours"]
    assert provider.search("missing").rows == ()
    path.with_suffix(".meta.json").write_text('{"summary": "old", "keep": 3}', encoding="utf-8")
    assert provider.rename("ours", "  Useful new title ").title == "Useful new title"
    assert provider.search("Useful").rows[0].title == "Useful new title"
    assert json.loads(path.with_suffix(".meta.json").read_text())["keep"] == 3
    for foreign in ("foreign", "elsewhere", "../ours", str(path)):
        with pytest.raises(SessionSearchError):
            provider.rename(foreign, "Changed")
    monkeypatch.setattr("alysis_code.session_store.local_session_owner", lambda: "other")
    with pytest.raises(SessionSearchError):
        provider.resolve("ours")


def test_picker_legacy_malformed_and_invalid_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("alysis_code.session_store.local_session_owner", lambda: "local")
    sessions, workspace = tmp_path / "sessions", tmp_path / "workspace"
    path = _log(sessions, workspace, "legacy", legacy=True)
    path.with_suffix(".meta.json").write_text("[]", encoding="utf-8")
    assert SessionPickerProvider(sessions, workspace).search().rows == ()
    provider = SessionPickerProvider(sessions, workspace, workspace)
    assert provider.search("Ελληνικά").rows[0].session_id == "legacy"
    assert provider.resolve("legacy").path == path
    with pytest.raises(SessionSearchError):
        SessionPickerProvider(sessions, "")
    with pytest.raises(SessionSearchError):
        provider.rename("legacy", " ")


def test_picker_bounds_titles_and_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("alysis_code.session_store.local_session_owner", lambda: "local")
    sessions, workspace = tmp_path / "sessions", tmp_path / "workspace"
    for index in range(52):
        _log(sessions, workspace, str(index))
    result = SessionPickerProvider(sessions, workspace).search()
    assert len(result.rows) == 50
    assert result.truncated


def test_search_reaches_past_initial_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("alysis_code.session_store.local_session_owner", lambda: "local")
    sessions, workspace = tmp_path / "sessions", tmp_path / "workspace"
    oldest = _log(sessions, workspace, "oldest")
    oldest.write_text(oldest.read_text().replace("Find", "unique-content"), encoding="utf-8")
    oldest.with_suffix(".meta.json").write_text('{"custom_name":"unique-title"}', encoding="utf-8")
    os.utime(oldest, (1, 1))
    for index in range(51):
        _log(sessions, workspace, str(index))
    provider = SessionPickerProvider(sessions, workspace)
    assert "oldest" not in [row.session_id for row in provider.search().rows]
    for query in ("unique-content", "unique-title"):
        result = provider.search(query)
        assert [row.session_id for row in result.rows] == ["oldest"]
        assert not result.truncated
    for index in range(51, 102):
        _log(sessions, workspace, str(index))
    result = provider.search("unique-content")
    assert not result.rows
    assert result.truncated
