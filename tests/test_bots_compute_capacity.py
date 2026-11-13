"""Actual PostgreSQL contention; optional actual two-computer Linux qualification."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from alysis_code.bots.allocation_ledger import AllocationLedger
from alysis_code.bots.cloud_desktop import CloudDesktopFactory
from alysis_code.bots.compute import ComputerSpec
from alysis_code.bots.compute_docker import DockerComputers
from alysis_code.bots.desktop_linux import DesktopError
from alysis_code.bots.models import BotInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.postgres import PostgresStore
from alysis_code.bots.schema import SCHEMA_VERSION
from alysis_code.bots.worker_lease import WorkerLease


@pytest.fixture
def isolated(tmp_path):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    base = os.environ.get("ALYSIS_BOTS_TEST_POSTGRES_DSN")
    if not base:
        pytest.skip("A disposable PostgreSQL 18 admin DSN is required")
    name = "bots_capacity_" + uuid4().hex
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} ENCODING 'UTF8'").format(sql.Identifier(name)))
    dsn = make_conninfo(base, dbname=name)
    stores, leases = [], []

    def make(owner):
        store = PostgresStore(
            dsn,
            objects=LocalObjectStorage(tmp_path / "objects"),
            state_directory=tmp_path / uuid4().hex,
        )
        stores.append(store)
        lease = WorkerLease(store, owner)
        worker = lease.acquire()
        leases.append(lease)
        return worker

    try:
        yield make
    finally:
        for lease in reversed(leases):
            lease.close()
        for store in stores:
            store.close()
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def intent(scope, *, state="creating"):
    return {"scope": scope, "allocation_id": uuid4().hex, "state": state}


def test_shared_capacity_is_atomic_across_accounts_and_unknown_allocations(isolated):
    owners = [(owner, isolated(owner)) for owner in ("account-a", "account-b", "account-c")]
    scope = "provider-scope-" + uuid4().hex
    requests = []
    for owner, store in owners:
        for _ in range(8):
            bot = store.save_bot(owner, BotInput(name="Concurrent", purpose="Capacity study"))
            requests.append((AllocationLedger(store, owner), bot["id"], intent(scope)))

    def reserve(request):
        ledger, bot, record = request
        try:
            ledger.reserve(bot, record, max_active=2)
            return True
        except DesktopError as error:
            assert "busy" in str(error)
            return False

    with ThreadPoolExecutor(max_workers=24) as pool:
        accepted = list(pool.map(reserve, requests))
    assert sum(accepted) == 2
    winners = [r for r, accepted in zip(requests, accepted, strict=True) if accepted]
    loser = next(r for r, accepted in zip(requests, accepted, strict=True) if not accepted)
    ledger, bot, record = winners[0]
    # Creating, unknown and stopped computers all retain capacity. No timed
    # expiry releases budget while the provider might still bill the allocation.
    for state in ("creating", "unknown", "stopped"):
        ledger.save(bot, {**record, "state": state})
        assert reserve(loser) is False
    with pytest.raises(DesktopError, match="configuration differs"):
        loser[0].reserve(loser[1], loser[2], max_active=3)
    ledger.save(bot, {**record, "state": "deleted"})
    assert reserve(loser) is True
    with pytest.raises(DesktopError, match="cleanup"):
        loser[0].reserve(loser[1], intent(scope), max_active=2)
    # Other provider scopes have independent budgets.
    other_bot = owners[0][1].save_bot("account-a", BotInput(name="Other", purpose="Other scope"))
    AllocationLedger(owners[0][1], "account-a").reserve(
        other_bot["id"], intent("other-scope"), max_active=1
    )


def test_uncapped_shared_policy_keeps_atomic_ownership_without_a_two_computer_limit(isolated):
    owners = [(owner, isolated(owner)) for owner in ("alpha-a", "alpha-b")]
    scope = "uncapped-" + uuid4().hex
    requests = []
    for owner, store in owners:
        for _ in range(3):
            bot = store.save_bot(
                owner, BotInput(name="Parallel", purpose="Uncapped internal Alpha")
            )
            requests.append((AllocationLedger(store, owner), bot["id"], intent(scope)))
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda item: item[0].reserve(item[1], item[2], max_active=None), requests))
    ledger, bot_id, _ = requests[0]
    assert ledger.capacity(scope) == 6
    with pytest.raises(DesktopError, match="cleanup"):
        ledger.reserve(bot_id, intent(scope), max_active=None)
    other = owners[0][1].save_bot(
        "alpha-a", BotInput(name="Policy", purpose="Do not silently change capacity")
    )
    with pytest.raises(DesktopError, match="configuration differs"):
        ledger.reserve(other["id"], intent(scope), max_active=2)


def test_schema_37_capacity_upgrade_preserves_policy_and_allocation(isolated):
    store = isolated("existing-alpha")
    bot = store.save_bot(
        "existing-alpha", BotInput(name="Saved", purpose="Preserve during upgrade")
    )
    ledger = AllocationLedger(store, "existing-alpha")
    record = intent("existing-scope", state="unknown")
    ledger.save(bot["id"], record)
    with store.connect(write=True) as db:
        db.execute("INSERT INTO computer_capacity VALUES(?,?)", ("existing-scope", 2))
        db.execute("ALTER TABLE computer_capacity ALTER COLUMN max_active SET NOT NULL")
        db.execute("UPDATE bots_schema SET version=37 WHERE id=1")
    reopened = isolated("other-alpha")
    assert reopened.bot("existing-alpha", bot["id"])["name"] == "Saved"
    assert ledger.get(bot["id"]) == record
    with reopened.connect(write=True) as db:
        assert (
            db.execute(
                "SELECT max_active FROM computer_capacity WHERE scope=?", ("existing-scope",)
            ).fetchone()[0]
            == 2
        )
        db.execute(
            "UPDATE computer_capacity SET max_active=NULL WHERE scope=?", ("existing-scope",)
        )
        assert (
            db.execute("SELECT version FROM bots_schema WHERE id=1").fetchone()[0] == SCHEMA_VERSION
        )


@pytest.mark.parametrize("previous_version", [19, 20])
def test_additive_upgrade_keeps_allocations_and_bot_state(isolated, previous_version):
    store = isolated("migration")
    bot = store.save_bot("migration", BotInput(name="Αθήνα 東京", purpose="Keep existing state"))
    record = intent("legacy-scope", state="unknown")
    ledger = AllocationLedger(store, "migration")
    ledger.save(bot["id"], record)
    with store.connect(write=True) as db:
        if previous_version == 19:
            db.execute("DROP TABLE computer_capacity")
        db.execute("DROP TABLE memory_changes")
        db.execute("UPDATE bots_schema SET version=? WHERE id=1", (previous_version,))
    store._initialize_schema()
    assert store.bot("migration", bot["id"])["name"] == "Αθήνα 東京"
    assert ledger.get(bot["id"]) == record
    with store.connect() as db:
        assert (
            db.execute("SELECT version FROM bots_schema WHERE id=1").fetchone()[0] == SCHEMA_VERSION
        )
        assert db.execute("SELECT count(*) FROM computer_capacity").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM memory_changes").fetchone()[0] == 0


def test_actual_two_linux_computers_share_capacity_across_account_controllers(isolated, tmp_path):
    if os.environ.get("ALYSIS_BOTS_TEST_DOCKER") != "1":
        pytest.skip("Enable actual isolated Linux computers explicitly")
    root = Path(__file__).resolve().parents[1]
    provider = DockerComputers(
        "desktop-linux", namespace_profile=root / "scripts/cloud-dev/seccomp.json"
    )
    entries, surfaces = [], []
    report = {"passed": False, "kind": "Real local PostgreSQL and Linux; no model calls"}
    try:
        for owner in ("first", "second", "third"):
            store = isolated(owner)
            bot = store.save_bot(owner, BotInput(name="Linux", purpose="Real capacity check"))
            factory = CloudDesktopFactory(
                owner,
                provider,
                ComputerSpec("alysis-bots-shared-state-20261020:qa"),
                tmp_path / owner,
                store=store,
                max_computers=2,
            )
            entries.append((store, bot, factory))

        def allocate(entry):
            _, bot, factory = entry
            return factory(bot["id"], tmp_path, cancel=lambda: False)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(allocate, entry) for entry in entries[:2]]
            errors = []
            for future in futures:
                try:
                    surfaces.append(future.result())
                except Exception as error:
                    errors.append(error)
            if errors:
                raise errors[0]
        first, second = surfaces
        assert all(provider.status(surface.ref).state == "running" for surface in surfaces)
        with pytest.raises(DesktopError, match="busy"):
            allocate(entries[2])
        assert AllocationLedger(entries[2][0], "third").get(entries[2][1]["id"]) is None
        contents = {}
        # The same path on simultaneous computers contains different real bytes.
        for surface in surfaces:
            result = surface.call(
                surface.owner,
                "run",
                {
                    "command": "python3 - <<'PY'\nfrom pathlib import Path\nimport os,hashlib\nd=os.urandom(257);Path('private.bin').write_bytes(d);print(hashlib.sha256(d).hexdigest())\nPY",
                    "timeout": 10,
                },
            )
            assert result["exit_code"] == 0
            contents[surface.owner] = result["output"].strip()
        assert len(set(contents.values())) == 2
        first.close()
        assert provider.status(first.ref).state == "missing"
        third = allocate(entries[2])
        surfaces.append(third)
        assert provider.status(second.ref).state == "running"
        result = third.call(third.owner, "run", {"command": "test ! -e private.bin", "timeout": 10})
        assert result["exit_code"] == 0
        preserved = second.call(
            second.owner, "run", {"command": "sha256sum private.bin", "timeout": 10}
        )
        assert (
            preserved["exit_code"] == 0 and preserved["output"].split()[0] == contents[second.owner]
        )
        report.update(
            passed=True,
            max_simultaneous=2,
            third_account_blocked_before_allocation=True,
            slot_reused_after_deletion=True,
            isolated_workspace_hashes=contents,
        )
    finally:
        errors = []
        for surface in reversed(surfaces):
            try:
                surface.close()
            except Exception:
                errors.append("surface_cleanup")
        report["computers"] = [
            {"id": surface.ref.resource_id, "state": provider.status(surface.ref).state}
            for surface in surfaces
        ]
        clean = not errors and all(item["state"] == "missing" for item in report["computers"])
        report["passed"] = report["passed"] and clean
        evidence = os.environ.get("ALYSIS_BOTS_CAPACITY_RECEIPT")
        if evidence:
            Path(evidence).write_text(json.dumps(report, indent=2) + "\n")
        assert clean
