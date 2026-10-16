from pathlib import Path

from alysis_code.cli_impl.tui import prompt_history
from alysis_code.cli_impl.tui.prompt_history import PromptEntry, PromptHistory


def test_memory_history_never_loads_or_touches_existing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "profile"))
    persistent = PromptHistory(tmp_path)
    persistent.accept(PromptEntry("existing private body"))
    before = persistent.path.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("Memory history must not open files or acquire locks")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", forbidden)
        patch.setattr(prompt_history, "_history_lock", forbidden)
        memory = PromptHistory(tmp_path, persistence=False)
        assert memory.entries == []
        memory.accept(PromptEntry("new private body"))
        memory._load()
        assert [entry.text for entry in memory.entries] == ["new private body"]
    assert persistent.path.read_bytes() == before


def test_memory_history_creates_no_profile_directory_and_retains_opaque_payloads(
    tmp_path, monkeypatch
):
    profile = tmp_path / "absent-profile"
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(profile))
    history = PromptHistory(tmp_path, persistence=False, limit=2)
    entry = PromptEntry(
        "draft [pasted #1 +3 lines]", pastes=((1, "opaque\n/secret-command\nbody"),)
    )
    history.accept(entry)
    history.accept(entry)
    assert history.entries == [entry]
    assert history.search("secret-command")[0][1] is entry
    history.accept(PromptEntry("two"))
    history.accept(PromptEntry("three"))
    assert [item.text for item in history.entries] == ["two", "three"]
    assert history._pending == []
    assert history.persistence_error is None
    assert not profile.exists()


def test_memory_history_retains_byte_bound(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "profile"))
    history = PromptHistory(tmp_path, persistence=False, byte_limit=200)
    history.accept(PromptEntry("small"))
    history.accept(PromptEntry("x" * 201))
    assert [item.text for item in history.entries] == ["small"]
    assert not history.path.parent.exists()


def test_default_still_persists_and_reloads(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "profile"))
    history = PromptHistory(tmp_path)
    history.accept(PromptEntry("normal persisted body"))
    assert history.path.is_file()
    assert PromptHistory(tmp_path).entries == history.entries
