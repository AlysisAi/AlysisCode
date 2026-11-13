"""Real isolated PostgreSQL allocation state; explicitly synthetic compute.

No model, Docker, cloud resource or service-restart calls. A synthetic provider
creates one owned inventory entry but loses its receipt and initially hides it.
"""

import json
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from test_bots_backup import databases  # noqa: F401
from test_bots_cloud_desktop import Provider

from alysis_code.bots.allocation_ledger import AllocationLedger
from alysis_code.bots.cloud_desktop import CloudDesktopFactory
from alysis_code.bots.compute import ComputerSpec, ComputeUncertain
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.desktop_linux import DesktopError, DesktopStartupCleanupError
from alysis_code.bots.models import BotInput
from alysis_code.bots.worker_lease import WorkerLease
from alysis_code.bots.workspaces import WorkspaceError
from alysis_code.cancellation import EventCancellationToken


class LateVisibleProvider(Provider):
    def __init__(self):
        super().__init__()
        self.scope = "late-visible-fixture-" + uuid4().hex
        self.hidden = True
        self.lose_receipt = True
        self.creates = []

    def create(self, identity, spec):
        self.creates.append(identity.allocation_id)
        ref = super().create(identity, spec)
        if self.lose_receipt:
            raise ComputeUncertain("Synthetic lost create receipt.")
        return ref

    def find(self, identity):
        return None if self.hidden else super().find(identity)


@pytest.fixture
def shared_factories(databases, tmp_path):  # noqa: F811
    store = databases()
    provider = LateVisibleProvider()
    leases, factories = [], []

    def make():
        owner = "startup-cleanup-" + uuid4().hex
        lease = WorkerLease(store, owner)
        worker = lease.acquire()
        leases.append(lease)
        bot = worker.save_bot(owner, BotInput(name="Late receipt", purpose="Synthetic cleanup"))
        factory = CloudDesktopFactory(
            owner,
            provider,
            ComputerSpec("synthetic-image"),
            tmp_path / owner,
            store=worker,
            max_computers=1,
        )
        factories.append(factory)
        return factory, bot["id"], AllocationLedger(worker, owner)

    try:
        yield make, provider
    finally:
        provider.hidden = provider.lose_receipt = False
        errors = []
        for factory in factories:
            for computer in [
                *factory._pending,
                *(entry[0] for entry in factory._instances.values()),
            ]:
                try:
                    computer.close()
                except Exception:
                    errors.append("synthetic computer cleanup failed")
        # Also settle fixture inventory if an assertion failed before the
        # constructor could return a handle; no actual provider is involved.
        for ref, state in list(provider.allocations.values()):
            if state == "running":
                provider.stop(ref)
            if state != "missing":
                provider.delete(ref)
        for lease in reversed(leases):
            lease.close()
        assert not errors


def test_unknown_shared_create_retains_handle_and_global_slot_until_observed_cleanup(
    shared_factories,
):
    make, provider = shared_factories
    factory, bot, ledger = make()
    other, other_bot, other_ledger = make()
    with pytest.raises(DesktopStartupCleanupError) as caught:
        factory(bot, None, cancel=lambda: False)
    pending = caught.value.executor
    assert factory._pending == [pending] and not pending._cleaned
    assert not pending._record.exists(), "Shared ownership must not require local allocation JSON."
    intent = ledger.get(bot)
    assert intent["state"] == "creating" and "resource_id" not in intent
    assert intent["allocation_id"] == pending.identity.allocation_id
    with pytest.raises(WorkspaceError):
        factory.leases.acquire(factory.owner, bot)
    with pytest.raises(DesktopError, match="busy"):
        other(other_bot, None, cancel=lambda: False)
    assert other_ledger.get(other_bot) is None and len(provider.creates) == 1

    # Even misleading old local scratch cannot override the shared intent.
    pending._record.write_text(json.dumps({"state": "deleted"}))
    with pytest.raises(DesktopError, match="unknown"):
        pending.close()
    assert not pending._cleaned and ledger.get(bot) == intent
    assert len(provider.creates) == 1

    provider.hidden = provider.lose_receipt = False
    pending.close()
    assert pending._cleaned and ledger.get(bot)["state"] == "deleted"
    assert provider.status(pending.ref).state == "missing"
    assert len(provider.creates) == 1, "Cleanup must not replay creation."
    acquired = factory.leases.acquire(factory.owner, bot)
    acquired.cleanup()
    replacement = other(other_bot, None, cancel=lambda: False)
    assert len(provider.creates) == len(set(provider.creates)) == 2
    assert other_ledger.get(other_bot)["state"] == "allocated"
    replacement.close()
    assert other_ledger.get(other_bot)["state"] == "deleted"


def test_shared_cleanup_read_failure_keeps_uncertainty_and_lease_until_retry(
    shared_factories, monkeypatch
):
    make, provider = shared_factories
    factory, bot, ledger = make()
    with pytest.raises(DesktopStartupCleanupError) as caught:
        factory(bot, None, cancel=lambda: False)
    pending = caught.value.executor
    original = pending._shared_ledger.get

    def unavailable(_bot):
        raise RuntimeError("Synthetic shared database read unavailable.")

    monkeypatch.setattr(pending._shared_ledger, "get", unavailable)
    with pytest.raises(RuntimeError, match="read unavailable"):
        pending.close()
    assert not pending._cleaned and pending._lease._fd is not None
    assert ledger.get(bot)["state"] == "creating"
    assert len(provider.creates) == 1
    monkeypatch.setattr(pending._shared_ledger, "get", original)
    with pytest.raises(DesktopError, match="unknown"):
        pending.close()
    assert not pending._cleaned
    provider.hidden = provider.lose_receipt = False
    pending.close()
    assert pending._cleaned and ledger.get(bot)["state"] == "deleted"
    assert len(provider.creates) == 1


def test_desktop_manager_reaps_shared_startup_handle_after_inventory_becomes_visible(
    shared_factories,
):
    make, provider = shared_factories
    factory, bot, ledger = make()
    views = ComputerViews()
    manager = DesktopComputers(factory.owner, views, factory=factory, max_computers=1)
    task = {"id": uuid4().hex, "bot_id": bot}
    try:
        with pytest.raises(DesktopError, match="startup failed; cleanup is pending"):
            manager.call(
                task,
                "computer_run",
                {"command": "true"},
                EventCancellationToken(threading.Event()),
                approved=True,
            )
        retained = manager._computers[bot]
        workspace = Path(retained.directory.name)
        assert retained.retired and retained.generation is None
        assert retained.executor is factory._pending[0]
        assert workspace.exists()
        with pytest.raises(DesktopError, match="unknown"):
            manager.reap()
        assert manager._computers[bot] is retained
        with pytest.raises(ValueError, match="busy"):
            views.claim(factory.owner, bot, "another-task", "browser")
        assert ledger.get(bot)["state"] == "creating" and len(provider.creates) == 1

        provider.hidden = provider.lose_receipt = False
        assert manager.reap() is True
        assert not manager._computers and not workspace.exists()
        assert retained.executor._cleaned
        assert ledger.get(bot)["state"] == "deleted"
        assert provider.status(retained.executor.ref).state == "missing"
        assert len(provider.creates) == 1
        views.claim(factory.owner, bot, "another-task", "browser")
        views.release_claim(factory.owner, bot, "another-task", "browser")

        # Factory capacity drops the cleaned pending handle on the next acquire.
        replacement = factory(bot, None, cancel=lambda: False)
        assert not factory._pending and len(provider.creates) == 2
        replacement.close()
        assert ledger.get(bot)["state"] == "deleted"
    finally:
        provider.hidden = provider.lose_receipt = False
        manager.close()
