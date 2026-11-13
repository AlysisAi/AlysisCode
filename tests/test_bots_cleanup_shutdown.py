"""Actual app/runtime and cleanup thread with local object bytes; no model calls."""

import asyncio
import threading
import time

import pytest

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.models import BotInput
from alysis_code.bots.object_storage import LocalObjectStorage, ObjectStorageError
from alysis_code.bots.retention import ObjectRetention
from alysis_code.bots.store import Store
from alysis_code.bots.transfers import FileTransfers


@pytest.mark.parametrize(
    "phase,fail,repeated_cancel",
    [
        ("delete", False, False),
        ("delete", True, False),
        ("expire", False, False),
        ("delete", False, True),
    ],
    ids=["delete-success", "delete-failure", "upload-expiry", "repeated-cancel"],
)
def test_lifespan_waits_for_cleanup_and_does_not_start_later_deletions(
    tmp_path, monkeypatch, phase, fail, repeated_cancel
):
    objects = LocalObjectStorage(tmp_path / "objects")
    store = Store(tmp_path / "db", objects=objects)
    bot = store.save_bot("owner", BotInput(name="Cleanup", purpose="Synthetic shutdown"))["id"]
    refs = [store.put_object("owner", bot, [value], len(value)) for value in (b"one", b"two")]
    # Eligibility fixture only; the production cleanup loop and I/O thread run.
    with store.connect(write=True) as db:
        db.execute("UPDATE stored_objects SET unreferenced_at=?", (time.time() - 86401,))

    def no_model(_):
        pytest.fail("Cleanup must not create a model client.")

    app = create_app(
        Settings(
            store.path,
            "t" * 40,
            "fixture",
            "https://example.invalid",
            "fixture",
            owner="owner",
            max_calls=0,
        ),
        task_store=store,
        client_factory=no_model,
    )
    entered, resume, finished = threading.Event(), threading.Event(), threading.Event()
    attempts = []
    real_delete, real_expire = objects.delete, FileTransfers.expire

    def hold():
        entered.set()
        assert resume.wait(5), "Test did not release its held cleanup operation."

    def delete(ref):
        attempts.append(ref)
        if phase == "delete" and len(attempts) == 1:
            try:
                hold()
                if fail:
                    raise ObjectStorageError("Synthetic delete unavailable")
                real_delete(ref)
            finally:
                finished.set()
        else:
            real_delete(ref)

    def expire(self):
        if phase == "expire":
            try:
                hold()
                real_expire(self)
            finally:
                finished.set()
        else:
            real_expire(self)

    monkeypatch.setattr(objects, "delete", delete)
    monkeypatch.setattr(FileTransfers, "expire", expire)

    async def scenario():
        tick = asyncio.Event()
        real_sleep = asyncio.sleep

        async def scheduled_sleep(seconds):
            if seconds == 60:
                await tick.wait()
                tick.clear()
            else:
                await real_sleep(seconds)

        monkeypatch.setattr("alysis_code.bots.app.asyncio.sleep", scheduled_sleep)
        context = app.router.lifespan_context(app)
        shutdown = None
        await context.__aenter__()
        try:
            tick.set()
            until = time.monotonic() + 3
            while not entered.is_set() and time.monotonic() < until:
                await real_sleep(0.01)
            assert entered.is_set(), "Real cleanup thread did not enter its held operation."
            shutdown = asyncio.create_task(context.__aexit__(None, None, None))
            await real_sleep(0.05)
            assert not shutdown.done(), "Lifespan returned while cleanup I/O was still running."
            assert not finished.is_set()
            if repeated_cancel:
                shutdown.cancel()
                await real_sleep(0.05)
                assert not shutdown.done(), "Repeated cancellation detached the cleanup thread."
        finally:
            resume.set()
            if shutdown is None:
                shutdown = asyncio.create_task(context.__aexit__(None, None, None))
            await asyncio.wait_for(shutdown, timeout=3)
        assert finished.is_set()
        assert app.state.runtime._thread is None

    asyncio.run(scenario())
    assert len(attempts) == (1 if phase == "delete" else 0)
    assert sum((objects.root / ref.key).exists() for ref in refs) == (
        1 if phase == "delete" and not fail else 2
    )
    # An orderly shutdown leaves unfinished intents immediately recoverable.
    monkeypatch.setattr(objects, "delete", real_delete)
    result = ObjectRetention(store, "owner").sweep()
    assert result == {"deleted": 1 if phase == "delete" and not fail else 2, "pending": 0}
    assert all(not (objects.root / ref.key).exists() for ref in refs)
