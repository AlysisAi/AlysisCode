"""Admission budgets; fixture host capacity never replaces actual production detection."""

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest

from alysis_code.bots.compute import ComputeCreationRefused, ComputerIdentity, ComputerSpec
from alysis_code.bots.computer import ComputerLocks
from alysis_code.bots.local_resources import LocalResources
from alysis_code.bots.local_vm import LocalVmComputers


def test_budget_separates_saved_bots_from_concurrent_computers():
    resources = LocalResources.parse({"cpu_budget": 4, "memory_mib": 8192}, host=(8, 16384))
    assert resources.max_computers == 2
    assert (
        LocalResources.parse({"cpu_budget": 8, "memory_mib": 12288}, host=(16, 32768)).max_computers
        == 4
    )
    assert (
        LocalResources.parse({"cpu_budget": 8, "memory_mib": 3072}, host=(16, 32768)).max_computers
        == 1
    )
    assert LocalResources.parse(None, host=(8, 16384)) == resources


@pytest.mark.parametrize(
    "value",
    [
        {"cpu_budget": True, "memory_mib": 8192},
        {"cpu_budget": 9, "memory_mib": 8192},
        {"cpu_budget": 4, "memory_mib": 16384},
        {"cpu_budget": 1, "memory_mib": 8192},
        {"cpu_budget": 4, "memory_mib": 2048},
        {"cpu_budget": 4, "memory_mib": 8192, "override": True},
    ],
)
def test_budget_cannot_exceed_hardware_or_remove_host_headroom(value):
    with pytest.raises(ValueError):
        LocalResources.parse(value, host=(8, 16384))


def test_local_startups_overlap_and_disk_admission_counts_pending_boots(tmp_path, monkeypatch):
    # Exercise the actual provider's admission/registry locks, with explicit
    # fixture copy and boot operations. No VM helper or real image is launched.
    provider = LocalVmComputers.__new__(LocalVmComputers)
    provider.root = tmp_path / "computers"
    provider.root.mkdir()
    provider.scope = str(provider.root)
    provider.image = tmp_path / "fixture-sparse-image"
    provider.image.write_bytes(b"fixture image")
    provider._lock = threading.RLock()
    provider._operations = ComputerLocks()
    provider._starting = set()
    provider._processes = {}
    monkeypatch.setattr(
        # Room for two starts (3 GiB, 4 GiB needed) but not a third (5 GiB): see disk_required.
        "alysis_code.bots.local_vm.shutil.disk_usage",
        lambda _: SimpleNamespace(free=int(4.5 * 1024**3)),
    )
    monkeypatch.setattr(
        "alysis_code.bots.local_vm.subprocess.run",
        lambda argv, **_: __import__("pathlib").Path(argv[-1]).write_bytes(b"fixture-disk"),
    )
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    identities = [ComputerIdentity("owner", str(index), uuid4().hex) for index in range(3)]

    def boot(ref, spec):
        entered[int(ref.identity.bot_id)].set()
        assert release.wait(4)

    monkeypatch.setattr(provider, "_boot", boot)
    refs = []
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(provider.create, identities[0], ComputerSpec("fixture"))
            assert entered[0].wait(2)
            second = pool.submit(provider.create, identities[1], ComputerSpec("fixture"))
            try:
                assert entered[1].wait(2), "Provider-wide startup lock serialized independent VMs"
                with pytest.raises(ComputeCreationRefused) as error:
                    provider.create(identities[2], ComputerSpec("fixture"))
                assert error.value.code == "insufficient_local_storage"
                assert not (provider.root / identities[2].allocation_id).exists()
            finally:
                release.set()
                refs.extend([first.result(timeout=2), second.result(timeout=2)])
    finally:
        release.set()
        for ref in refs:
            provider.delete(ref)
    assert not provider._starting and not list(provider.root.iterdir())


@pytest.mark.parametrize(
    "host,computers",
    [((4, 8192), 1), ((8, 16384), 2), ((16, 32768), 4), ((32, 65536), 8), ((64, 262144), 16)],
)
def test_the_default_budget_grows_with_the_machine(host, computers):
    assert LocalResources.parse(None, host=host).max_computers == computers
