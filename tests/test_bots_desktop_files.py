"""Library authority and transfer receipts; actual child file checks have Linux QA."""

import threading

import pytest
from test_bots_desktop_runtime import study as study

from alysis_code.bots.desktop import FILE_TOOLS
from alysis_code.bots.desktop_linux import DesktopError
from alysis_code.bots.models import BotInput, FileInput
from alysis_code.bots.store import Conflict, NotFound
from alysis_code.cancellation import CooperativeCancellationError, EventCancellationToken


def test_library_transfer_preserves_actual_bytes_without_model_transcription(study):
    store, task, manager, instances = study
    manager.store = store
    content = "日本語 — Αθήνα\r\n  exact indentation\n"
    source = store.save_file("owner", task["bot_id"], FileInput(name="source.txt", content=content))
    token = EventCancellationToken(threading.Event())
    imported = manager.call(
        task, "computer_import_file", {"file_id": source["id"]}, token, approved=True
    )
    assert "content" not in imported and imported["source_file_id"] == source["id"]
    assert imported["computer_created"] is True
    (instances[0].workspace / "result.txt").write_bytes((content + "Computed\n").encode())
    exported = manager.call(
        task, "computer_save_file", {"name": "result.txt"}, token, approved=True
    )
    assert "content" not in exported and exported["saved"]
    assert exported["computer_created"] is False
    assert exported["computer_session_id"] == imported["computer_session_id"]
    saved = store.file("owner", task["bot_id"], exported["id"])
    assert saved["content"] == content + "Computed\n" and saved["source_task_id"] == task["id"]
    with pytest.raises(Conflict, match="already exists"):
        manager.call(task, "computer_save_file", {"name": "result.txt"}, token, approved=True)
    assert store.file("owner", task["bot_id"], source["id"])["content"] == content
    manager.release(task["id"], cancelled=False)
    assert not instances[0].workspace.exists()
    assert store.file("owner", task["bot_id"], exported["id"])["content"] == content + "Computed\n"


def test_export_new_name_requires_an_actual_workspace_copy(study):
    store, task, manager, instances = study
    manager.store = store
    token = EventCancellationToken(threading.Event())
    manager.call(task, "computer_run", {"command": "fixture"}, token, approved=True)
    source = instances[0].workspace / "draft.txt"
    source.write_bytes(b"actual corrected bytes\n")
    with pytest.raises(DesktopError, match="does not rename"):
        manager.call(task, "computer_save_file", {"name": "final.txt"}, token, approved=True)
    assert not store.list_files("owner", task["bot_id"])
    (source.parent / "final.txt").write_bytes(source.read_bytes())
    result = manager.call(task, "computer_save_file", {"name": "final.txt"}, token, approved=True)
    assert b"".join(store.file_chunks("owner", task["bot_id"], result["id"])) == source.read_bytes()


def test_foreign_file_and_disabled_permission_never_allocate(study):
    store, task, manager, instances = study
    manager.store = store
    peer = store.save_bot("owner", BotInput(name="Peer", purpose="Private files"))
    source = store.save_file("owner", peer["id"], FileInput(name="private.txt", content="Private"))
    token = EventCancellationToken(threading.Event())
    with pytest.raises(NotFound):
        manager.call(task, "computer_import_file", {"file_id": source["id"]}, token, approved=True)
    # Setup edits are blocked during active tasks; inject a persisted revocation
    # to ensure the transfer boundary still consults current authority.
    with store.connect(write=True) as db:
        db.execute("UPDATE bots SET files_enabled=0 WHERE id=?", (task["bot_id"],))
    with pytest.raises(DesktopError, match="unavailable"):
        manager.call(task, "computer_import_file", {"file_id": source["id"]}, token, approved=True)
    task["config"]["files_enabled"] = False
    assert not FILE_TOOLS.intersection(
        s["function"]["name"] for s in manager.schemas(task, True, approved=True)
    )
    assert not instances


@pytest.mark.parametrize("case", ["symlink", "cancelled", "changed"])
def test_invalid_or_cancelled_export_cannot_publish_to_library(study, case, monkeypatch):
    import os

    from alysis_code.bots import workspace_files

    store, task, manager, instances = study
    manager.store = store
    event = threading.Event()
    token = EventCancellationToken(event)
    manager.call(task, "computer_run", {"command": "fixture"}, token, approved=True)
    path = instances[0].workspace / "result.bin"
    if case == "symlink":
        path.symlink_to("/etc/passwd")
    else:
        path.write_bytes(b"x" * 600000)
        original = os.read

        def read(fd, size):
            data = original(fd, size)
            if size == workspace_files.FILE_CHUNK_BYTES:
                if case == "cancelled":
                    event.set()
                else:
                    with path.open("ab") as f:
                        f.write(b"changed")
            return data

        monkeypatch.setattr(workspace_files.os, "read", read)
    with pytest.raises(CooperativeCancellationError if case == "cancelled" else DesktopError):
        manager.call(task, "computer_save_file", {"name": "result.bin"}, token, approved=True)
    assert not store.list_files("owner", task["bot_id"])


def test_injected_computer_cannot_bind_another_library_database(tmp_path):
    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.computer import ComputerViews
    from alysis_code.bots.desktop import DesktopComputers
    from alysis_code.bots.store import Store

    settings = Settings(
        tmp_path / "app.db",
        "t" * 40,
        "fixture",
        "https://example.invalid",
        "fixture",
        owner="owner",
    )
    other_store = Store(tmp_path / "other.db")
    manager = DesktopComputers("owner", ComputerViews(), store=other_store)
    with pytest.raises(ValueError, match="database"):
        create_app(settings, desktop_computers=manager)
    manager.close()


@pytest.mark.parametrize("case", ["invalid", "changed", "valid"])
def test_office_export_requires_validation_of_the_published_bytes(study, case):
    from docx import Document

    from alysis_code.bots.artifact_validation import check_file

    store, task, manager, instances = study
    manager.store = store
    token = EventCancellationToken(threading.Event())
    manager.call(task, "computer_run", {"command": "fixture"}, token, approved=True)
    executor = instances[0]
    path = executor.workspace / "report.docx"
    if case == "invalid":
        path.write_bytes(b"This is not an Office package")
    else:
        document = Document()
        document.add_paragraph("Synthetic report 東京")
        document.save(path)
    original = executor.call

    def call(owner, operation, arguments, **kwargs):
        if operation != "check_file":
            return original(owner, operation, arguments, **kwargs)
        report = check_file(executor.workspace, arguments["name"])
        if case == "changed":
            path.write_bytes(b"changed after the check")
        return report

    executor.call = call
    if case == "changed":
        with pytest.raises(DesktopError, match="changed after validation"):
            manager.call(task, "computer_save_file", {"name": path.name}, token, approved=True)
    else:
        result = manager.call(task, "computer_save_file", {"name": path.name}, token, approved=True)
        assert result["saved"] is (case == "valid")
        assert result["validation"]["items"][0]["status"] == (
            "failed" if case == "invalid" else "passed"
        )
        if case == "valid":
            assert result["sha256"] == result["validation"]["documentSha256"]
    assert len(store.list_files("owner", task["bot_id"])) == (1 if case == "valid" else 0)
