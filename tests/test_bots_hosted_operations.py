"""Secret-boundary, real encryption and offline maintenance failure checks."""

import io
import json
import os
import tarfile
import time
from types import SimpleNamespace

import httpx
import pytest
from test_bots_hosted_azure import values

from alysis_code.bots import hosted_operations as operations
from alysis_code.bots.schema import SCHEMA_VERSION

URI = "https://private-fixture.vault.azure.net/secrets/bootstrap/" + "a" * 32


def test_versioned_bootstrap_uses_private_bearer_and_rejects_changed_identity():
    payload = values()
    payload["operations"] = {"backup_key": "synthetic-backup-secret"}
    credential = SimpleNamespace(get_token=lambda _: SimpleNamespace(token="synthetic-token"))
    changed = False

    def respond(request):
        assert request.headers["authorization"] == "Bearer synthetic-token"
        assert request.url.params["api-version"] == "2025-07-01"
        return httpx.Response(
            200,
            json={
                "id": URI + ("wrong" if changed else ""),
                "attributes": {"enabled": True},
                "value": json.dumps(payload),
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert json.loads(operations.fetch_bootstrap(URI, credential, client)) == payload
        changed = True
        with pytest.raises(ValueError, match="identity"):
            operations.fetch_bootstrap(URI, credential, client)


@pytest.mark.parametrize(
    "uri",
    [
        "http://127.0.0.1/secrets/bootstrap",
        "https://private-fixture.vault.azure.net/secrets/bootstrap",
        URI + "?api-version=wrong",
        URI.replace(".vault.azure.net", ".vault.azure.net.attacker.invalid"),
    ],
)
def test_unpinned_or_foreign_secret_uri_never_requests_credentials(uri):
    with pytest.raises(ValueError, match="version"):
        operations.fetch_bootstrap(uri, None, None)


@pytest.mark.parametrize("status", [302, 401, 403, 500])
def test_secret_failure_never_follows_redirect_or_exposes_response(status):
    credential = SimpleNamespace(get_token=lambda _: SimpleNamespace(token="synthetic-token"))
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                status,
                text="synthetic-private-upstream-secret",
                headers={"Location": "https://example.invalid"},
            )
        )
    ) as client:
        with pytest.raises(RuntimeError) as error:
            operations.fetch_bootstrap(URI, credential, client)
    assert "synthetic-private" not in str(error.value)


def test_real_streaming_encryption_and_authentication_before_restore_publication(tmp_path):
    directory = tmp_path / "backup"
    directory.mkdir(mode=0o700)
    raw = b"PGDMP" + os.urandom(700000)  # exercise multiple streaming chunks
    (directory / "database.dump").write_bytes(raw)
    manifest = {
        "format": 1,
        "schema": SCHEMA_VERSION,
        "database": operations.fingerprint(directory / "database.dump"),
        "objects": [],
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    key = os.urandom(32)
    encrypted = tmp_path / "encrypted"
    checksum = operations.encrypt_backup(directory, encrypted, key)
    assert checksum == operations.fingerprint(encrypted)
    assert encrypted.stat().st_mode & 0o777 == 0o600
    assert raw[:100] not in encrypted.read_bytes()
    restored = tmp_path / "restored.tgz"
    operations.decrypt_backup(encrypted, restored, key)
    with tarfile.open(restored) as archive:
        assert archive.extractfile("backup/database.dump").read() == raw
    with pytest.raises(FileExistsError):
        operations.decrypt_backup(encrypted, restored, key)
    bad = bytearray(encrypted.read_bytes())
    bad[-20] ^= 1
    encrypted.write_bytes(bad)
    for wrong_key in (key, os.urandom(32)):
        target = tmp_path / "untrusted.tgz"
        with pytest.raises(ValueError, match="authentication"):
            operations.decrypt_backup(encrypted, target, wrong_key)
        assert not target.exists()
        assert not list(tmp_path.glob("*.pending-*"))


@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("was_active", [False, True])
def test_maintenance_preserves_previous_service_state_even_after_backup_failure(fails, was_active):
    calls = []

    def operation():
        calls.append("backup")
        if fails:
            raise RuntimeError("synthetic archive failure")
        return "verified"

    def run():
        return operations.maintenance(operation, stop=calls.append, active=lambda: was_active)

    if fails:
        with pytest.raises(RuntimeError):
            run()
    else:
        assert run() == "verified"
    assert calls == (["stop", "backup", "start"] if was_active else ["backup"])


def test_expired_pilot_cannot_restart_after_maintenance():
    calls = []
    operations.maintenance(
        lambda: None, stop=calls.append, active=lambda: True, restart_allowed=lambda: False
    )
    assert calls == ["stop"]


@pytest.mark.parametrize("expiry,expected", [(None, True), (2000000000, True), (1, False)])
def test_inactive_controller_alert_respects_explicit_hosting_lifetime(tmp_path, expiry, expected):
    data = values()
    data["hosted"]["expires_at"] = expiry
    _, hosted = operations.bootstrap(io.BytesIO(json.dumps(data).encode()))
    config = {"receipt_root": str(tmp_path)}
    operations.record(config, "backup", {"state": "verified"})
    issues = operations.operations_issues(config, hosted, {"state": "inactive"}, now=1000)
    assert ("controller_inactive" in issues) is expected


def test_health_checks_actual_worker_and_keeps_history_out_of_receipt():
    settings, hosted = operations.bootstrap(io.BytesIO(json.dumps(values()).encode()))
    state = {
        "worker_ready": True,
        "state_backend": "postgresql",
        "history": "private-conversation",
        "browser_cleanup_pending": False,
    }

    def respond(request):
        assert request.url.host == "127.0.0.1" and request.url.port == hosted.port
        assert request.headers["authorization"] == "Bearer " + settings.token
        return httpx.Response(200, json=state)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert operations.health(settings, hosted, client) == {
            "state": "healthy",
            "browser_cleanup_pending": False,
        }
        degraded = operations.health(settings, hosted, client, backup_failed=True)
        assert degraded["state"] == "degraded" and degraded["backup_failed"] is True
        state["worker_ready"] = False
        with pytest.raises(RuntimeError, match="unavailable"):
            operations.health(settings, hosted, client)


def test_backup_retry_preserves_verified_receipt_and_alerts_only_on_transitions(tmp_path, capsys):
    config = {"receipt_root": str(tmp_path)}
    assert operations.backup_due(config, now=100000)
    operations.record(config, "backup", {"state": "verified", "sha256": "checked"})
    verified = json.loads((tmp_path / "backup.json").read_text())
    operations.record(config, "backup-attempt", {"state": "deferred", "reason": "active_work"})
    assert not operations.backup_due(config, now=verified["checked_at"] + 100)
    assert operations.backup_due(config, now=verified["checked_at"] + 86400)
    assert json.loads((tmp_path / "backup.json").read_text()) == verified
    capsys.readouterr()
    operations.alert_transition(config, ["backup_overdue"])
    operations.alert_transition(config, ["backup_overdue"])
    operations.alert_transition(config, [])
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(events) == 2
    assert events[0]["issues"] == ["backup_overdue"] and events[1]["state"] == "clear"


@pytest.mark.parametrize(
    "remaining_days,expected",
    [(90, []), (7, ["tls_certificate_expiring"]), (-1, ["tls_certificate_expired"])],
)
def test_real_certificate_expiry_is_structured_and_does_not_restart_worker(
    tmp_path, remaining_days, expected
):
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    now = datetime.now(UTC)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "private-fixture")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now + timedelta(days=remaining_days))
        .sign(key, hashes.SHA256())
    )
    config = {"receipt_root": str(tmp_path)}
    operations.record(config, "backup", {"state": "verified"})
    hosted = SimpleNamespace(tls_certificate=cert.public_bytes(serialization.Encoding.PEM).decode())
    assert operations.operations_issues(config, hosted, {}, now=time.time()) == expected


@pytest.mark.parametrize("busy", [True, False])
@pytest.mark.parametrize("expiry", [None, 2000000000])
def test_force_backup_protects_real_active_work_and_gates_new_claims(
    tmp_path, monkeypatch, busy, expiry
):
    from contextlib import contextmanager, nullcontext
    from uuid import uuid4

    import azure.identity

    from alysis_code.bots.models import BotInput, TaskInput
    from alysis_code.bots.store import Store

    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Protected", purpose="Backup test"))
    chat = store.create_chat("owner", bot["id"], "Work")
    task = store.enqueue("owner", chat["id"], TaskInput(message="Work", request_id=uuid4().hex))
    if busy:
        store.claim("owner")
    config = {
        "secret_uri": URI,
        "restart_marker": str(tmp_path / "restart"),
        "receipt_root": str(tmp_path / "receipts"),
        "lock_path": str(tmp_path / "lock"),
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))

    @contextmanager
    def database(*_):
        yield store

    calls = []

    def verified_backup(*_):
        assert not busy
        assert store.claim("owner") is None
        return {"state": "verified", "sha256": "verified-fixture"}

    monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", lambda: nullcontext(None))
    data = values()
    data["hosted"]["expires_at"] = expiry
    monkeypatch.setattr(operations, "fetch_bootstrap", lambda *_: json.dumps(data).encode())
    monkeypatch.setattr(operations, "maintenance_store", database)
    monkeypatch.setattr(operations, "unit_active", lambda: True)
    monkeypatch.setattr(operations, "unit_command", calls.append)
    monkeypatch.setattr(operations, "backup", verified_backup)
    monkeypatch.setattr(
        "sys.argv", ["hosted_operations", "backup", "--force", "--config", str(path)]
    )
    assert operations.main() == 0
    assert calls == ([] if busy else ["stop", "start"])
    assert not (tmp_path / "restart").exists()
    if busy:
        assert store.task("owner", task["id"])["status"] == "running"
        assert (
            json.loads((tmp_path / "receipts/backup-attempt.json").read_text())["state"]
            == "deferred"
        )
    else:
        assert store.claim("owner")["id"] == task["id"]


def test_killed_backup_recovery_releases_only_its_maintenance_lease(tmp_path, monkeypatch):
    from contextlib import contextmanager, nullcontext

    import azure.identity

    from alysis_code.bots.hosted_maintenance import MaintenanceLease
    from alysis_code.bots.store import Store

    store = Store(tmp_path / "bots.db")
    token = MaintenanceLease(store).acquire()
    marker = tmp_path / "restart"
    marker.write_text(json.dumps({"lease_token": token}))
    config = {"secret_uri": URI, "restart_marker": str(marker)}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))

    @contextmanager
    def database(*_):
        yield store

    calls = []
    monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", lambda: nullcontext(None))
    monkeypatch.setattr(operations, "fetch_bootstrap", lambda *_: json.dumps(values()).encode())
    monkeypatch.setattr(operations, "maintenance_store", database)
    monkeypatch.setattr(operations, "unit_command", calls.append)
    monkeypatch.setattr("sys.argv", ["hosted_operations", "recover", "--config", str(path)])
    assert operations.main() == 0 and calls == ["start"] and not marker.exists()
    assert MaintenanceLease(store).acquire()
