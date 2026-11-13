"""Private Azure host operations. Credentials are fetched into RAM, never files.

systemd owns host restart and the two timers. Backups deliberately use the
existing offline maintenance protocol; they do not snapshot live writers or
automatically activate a restored deployment. No external notifications are sent.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import io
import json
import os
import re
import signal
import ssl
import subprocess
import sys
import tarfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .backup import create_backup, dump_database, verify_backup
from .hosted_azure import bootstrap
from .hosted_supervisor import supervise

UNIT = "alysis-bots.service"
MAGIC = b"ALYSIS-BOTS-BACKUP\x01"
CHUNK = 262144


def validate_secret_uri(uri):
    if not re.fullmatch(
        r"https://[a-zA-Z0-9-]{3,24}\.vault\.azure\.net/secrets/[a-zA-Z0-9-]+/[a-fA-F0-9]{32}",
        uri,
    ):
        raise ValueError("An explicit Azure Key Vault secret version is required.")


def fetch_bootstrap(uri, credential, client):
    """Pin an explicit Key Vault version, with no redirects or upstream logging."""
    validate_secret_uri(uri)
    token = credential.get_token("https://vault.azure.net/.default").token
    with client.stream(
        "GET",
        uri,
        params={"api-version": "2025-07-01"},
        headers={"Authorization": "Bearer " + token},
        timeout=15,
        follow_redirects=False,
    ) as response:
        if response.status_code != 200:
            raise RuntimeError("Private bootstrap could not be retrieved.")
        raw = bytearray()
        for chunk in response.iter_bytes():
            raw.extend(chunk)
            if len(raw) > 32768:
                raise ValueError("Secret response exceeds its allowance.")
    bundle = json.loads(raw)
    if bundle.get("id") != uri or bundle.get("attributes", {}).get("enabled") is not True:
        raise ValueError("Private bootstrap identity is not confirmed.")
    payload = bundle["value"].encode() + b"\n"
    if len(payload) > 16384:
        raise ValueError("Hosted bootstrap exceeds its allowance.")
    bootstrap(io.BytesIO(payload))
    return payload


def encrypt_backup(directory, destination, key):
    """Stream a verified backup to an authenticated encrypted tar.gz envelope."""
    verify_backup(directory)
    nonce = os.urandom(12)
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(MAGIC)
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(MAGIC + nonce)

        class Writer:
            def write(self, data):
                output.write(encryptor.update(data))
                return len(data)

        with tarfile.open(fileobj=Writer(), mode="w|gz") as archive:
            archive.add(directory, arcname="backup")
        output.write(encryptor.finalize())
        output.write(encryptor.tag)
        output.flush()
        os.fsync(output.fileno())
    return fingerprint(destination)


def decrypt_backup(source, destination, key):
    """Operator restore aid: publish plaintext only after GCM authentication."""
    from cryptography.exceptions import InvalidTag

    source, destination = Path(source), Path(destination)
    pending = destination.with_name(destination.name + ".pending-" + uuid4().hex)
    if destination.exists():
        raise FileExistsError("Use a new private restore destination.")
    try:
        with source.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            if stream.read(len(MAGIC)) != MAGIC or size < len(MAGIC) + 28:
                raise ValueError("Invalid encrypted backup envelope.")
            nonce = stream.read(12)
            stream.seek(-16, os.SEEK_END)
            tag = stream.read(16)
            decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
            decryptor.authenticate_additional_data(MAGIC)
            stream.seek(len(MAGIC) + 12)
            remaining = size - len(MAGIC) - 28
            descriptor = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                while remaining:
                    data = stream.read(min(CHUNK, remaining))
                    if not data:
                        raise ValueError("Incomplete encrypted backup.")
                    remaining -= len(data)
                    output.write(decryptor.update(data))
                output.write(decryptor.finalize())
                output.flush()
                os.fsync(output.fileno())
            os.link(pending, destination)
    except InvalidTag:
        raise ValueError("Encrypted backup authentication failed.") from None
    finally:
        pending.unlink(missing_ok=True)


def fingerprint(path):
    digest, size = hashlib.sha256(), 0
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(CHUNK), b""):
            digest.update(data)
            size += len(data)
    return {"sha256": digest.hexdigest(), "size": size}


def unit_command(*arguments):
    result = subprocess.run(
        ["systemctl", *arguments, UNIT],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=100,
    )
    if result.returncode:
        raise RuntimeError("Hosted service operation failed.")


def unit_active():
    return (
        subprocess.run(
            ["systemctl", "is-active", "--quiet", UNIT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).returncode
        == 0
    )


def unit_failed(name=UNIT):
    return (
        subprocess.run(
            ["systemctl", "is-failed", "--quiet", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).returncode
        == 0
    )


def maintenance(operation, *, stop=unit_command, active=unit_active, restart_allowed=lambda: True):
    was_active = active()
    if was_active:
        stop("stop")
    try:
        return operation()
    finally:
        if was_active and restart_allowed():
            stop("start")


def backup_due(config, *, now=None):
    """Keep the last verified backup receipt separate from later attempts."""
    try:
        receipt = json.loads((Path(config["receipt_root"]) / "backup.json").read_text())
        age = (time.time() if now is None else now) - receipt["checked_at"]
        return receipt["state"] != "verified" or not 0 <= age < 86400
    except (OSError, ValueError, KeyError, TypeError):
        return True


@contextmanager
def maintenance_store(values, credential):
    from .object_storage_azure import AzureBlobStorage
    from .postgres import PostgresStore

    hosted = values["hosted"]
    objects = AzureBlobStorage(hosted["storage_url"], hosted["storage_container"], credential)
    store = None
    try:
        store = PostgresStore(
            hosted["database_dsn"],
            objects=objects,
            state_directory=Path(hosted["state_directory"]),
        )
        yield store
    finally:
        if store is not None:
            store.close()
        objects.close()


@contextmanager
def backup_admission(values, credential, *, marker=None):
    from .hosted_maintenance import MaintenanceLease

    with maintenance_store(values, credential) as store:
        lease = MaintenanceLease(store)
        token = lease.acquire()
        try:
            if token is not None and marker is not None:
                descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "w") as output:
                    json.dump({"lease_token": token}, output)
            yield token is not None
        finally:
            if token is not None:
                lease.release(token)


def health(settings, hosted, client, *, backup_failed=False):
    if hosted.tls_certificate is not None:
        context = ssl.create_default_context(cadata=hosted.tls_certificate)
        with httpx.Client(verify=context, trust_env=False) as secure_client:
            return _health(settings, hosted, secure_client, "https", backup_failed=backup_failed)
    return _health(settings, hosted, client, "http", backup_failed=backup_failed)


def _health(settings, hosted, client, scheme, *, backup_failed):
    with client.stream(
        "GET",
        f"{scheme}://127.0.0.1:{hosted.port}/v1/status",
        headers={"Authorization": "Bearer " + settings.token},
        timeout=5,
        follow_redirects=False,
    ) as response:
        if response.status_code != 200:
            raise RuntimeError("Hosted health probe failed.")
        raw = bytearray()
        for chunk in response.iter_bytes():
            raw.extend(chunk)
            if len(raw) > 32768:
                raise ValueError("Health response exceeds its allowance.")
    state = json.loads(raw)
    if state.get("worker_ready") is not True or state.get("state_backend") != "postgresql":
        raise RuntimeError("Hosted worker is unavailable.")
    cleanup = state.get("storage_cleanup", {})
    result = {
        "state": "degraded"
        if state.get("browser_cleanup_pending") or cleanup.get("pending") or backup_failed
        else "healthy",
        "browser_cleanup_pending": bool(state.get("browser_cleanup_pending")),
    }
    if cleanup.get("pending"):
        result["storage_cleanup_pending"] = True
    if state.get("maintenance_pending"):
        result["maintenance_pending"] = True
    capacity = state.get("computer_capacity")
    if isinstance(capacity, dict):
        result["computer_capacity"] = {
            key: capacity[key] for key in ("active", "limit", "queued", "cleanup_pending")
        }
    if backup_failed:
        result["backup_failed"] = True
    return result


def backup(values, config, credential):
    from azure.storage.blob import ContainerClient

    from .object_storage_azure import AzureBlobStorage

    hosted = values["hosted"]
    key = base64.b64decode(values["operations"]["backup_key"], validate=True)
    if len(key) != 32:
        raise ValueError("A stable 32-byte backup encryption key is required.")
    container = config["database_container"]
    if not re.fullmatch(r"alysis-bots-[a-z0-9-]{1,80}", container):
        raise ValueError("Use the dedicated pilot database container.")
    root = Path(config["backup_root"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = root / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid4().hex)

    def dump(dsn, snapshot, path):
        from psycopg.conninfo import conninfo_to_dict

        database = conninfo_to_dict(dsn)["dbname"]

        def run(command, _, **streams):
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    "-i",
                    "--user",
                    "postgres",
                    container,
                    *command,
                    "--username=postgres",
                    "--dbname=" + database,
                ],
                stderr=subprocess.DEVNULL,
                timeout=300,
                **streams,
            )
            if result.returncode:
                raise RuntimeError("Private database archive operation failed.")

        dump_database(dsn, snapshot, path, run=run)

    objects = AzureBlobStorage(hosted["storage_url"], hosted["storage_container"], credential)
    destination = ContainerClient(
        hosted["storage_url"],
        hosted["storage_container"],
        credential=credential,
        retry_total=0,
        connection_timeout=5,
        read_timeout=30,
    )
    try:
        manifest = create_backup(hosted["database_dsn"], objects, directory, dump=dump)
        encrypted = directory.with_suffix(".tgz.aesgcm")
        checksum = encrypt_backup(directory, encrypted, key)
        blob_name = "operations-backups/v1/" + encrypted.name
        with encrypted.open("rb") as stream:
            destination.upload_blob(blob_name, stream, length=checksum["size"], overwrite=False)
        digest, received = hashlib.sha256(), 0
        for chunk in destination.download_blob(blob_name).chunks():
            digest.update(chunk)
            received += len(chunk)
        if {"size": received, "sha256": digest.hexdigest()} != checksum:
            raise RuntimeError("Off-host backup readback failed.")
        return {
            "state": "verified",
            "blob": blob_name,
            **checksum,
            "objects": len(manifest["objects"]),
            "schema": manifest["schema"],
        }
    finally:
        destination.close()
        objects.close()


def record(config, kind, result):
    root = Path(config["receipt_root"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    value = {"kind": kind, "checked_at": time.time(), **result}
    path = root / (kind + ".json")
    pending = root / (kind + "." + uuid4().hex)
    descriptor = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream)
    os.replace(pending, path)
    print(json.dumps(value), flush=True)


def operations_issues(config, hosted, result, *, now=None):
    """Only trusted status codes and timestamps enter operational alerts."""
    from cryptography import x509

    now = time.time() if now is None else now
    issues = []
    if result.get("backup_failed"):
        issues.append("backup_failed")
    if result.get("browser_cleanup_pending"):
        issues.append("browser_cleanup_pending")
    if result.get("storage_cleanup_pending"):
        issues.append("storage_cleanup_pending")
    if result.get("maintenance_pending"):
        issues.append("maintenance_pending")
    if result.get("state") in ("inactive", "failed") and (
        hosted.expires_at is None or now < hosted.expires_at
    ):
        issues.append("controller_inactive")
    if result.get("computer_capacity", {}).get("cleanup_pending"):
        issues.append("computer_cleanup_pending")
    capacity = result.get("computer_capacity", {})
    if capacity and capacity["limit"] is not None and capacity["active"] > capacity["limit"]:
        issues.append("computer_capacity_exceeded")
    if backup_due(config, now=now):
        issues.append("backup_overdue")
    if hosted.tls_certificate:
        certificate = x509.load_pem_x509_certificate(hosted.tls_certificate.encode())
        if certificate.not_valid_after_utc.timestamp() <= now:
            issues.append("tls_certificate_expired")
        elif certificate.not_valid_after_utc.timestamp() - now < 14 * 86400:
            issues.append("tls_certificate_expiring")
    return issues


def alert_transition(config, issues):
    root = Path(config["receipt_root"])
    try:
        previous = json.loads((root / "alerts.json").read_text()).get("issues", [])
    except (OSError, ValueError, TypeError):
        previous = None
    if previous != issues:
        record(
            config,
            "alerts",
            {
                "state": "attention" if issues else "clear",
                "issues": issues,
            },
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("run", "backup", "health", "recover"))
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--force", action="store_true", help="Run a due backup now; still protects active work"
    )
    args = parser.parse_args()
    if args.force and args.operation != "backup":
        parser.error("--force is only valid for backup")
    config = None
    try:
        from azure.identity import ManagedIdentityCredential

        config = json.loads(args.config.read_text())  # references/paths only
        marker = Path(config["restart_marker"])
        if args.operation == "recover":
            if marker.exists():
                raw = marker.read_text()
                if raw:
                    from .hosted_maintenance import MaintenanceLease

                    token = json.loads(raw)["lease_token"]
                    if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
                        raise ValueError("Invalid backup admission receipt.")
                    with (
                        ManagedIdentityCredential() as credential,
                        httpx.Client(trust_env=False) as client,
                    ):
                        payload = fetch_bootstrap(config["secret_uri"], credential, client)
                        with maintenance_store(json.loads(payload), credential) as store:
                            MaintenanceLease(store).release(token)
                unit_command("start")  # run mode independently enforces expiry
                marker.unlink()
            return 0
        with ManagedIdentityCredential() as credential, httpx.Client(trust_env=False) as client:
            payload = fetch_bootstrap(config["secret_uri"], credential, client)
            settings, hosted = bootstrap(io.BytesIO(payload))
            if args.operation == "run":
                if hosted.expires_at is not None and time.time() >= hosted.expires_at:
                    print('{"event":"pilot_expired"}', flush=True)
                    return 0
                stop = threading.Event()
                for signum in (signal.SIGTERM, signal.SIGINT):
                    signal.signal(signum, lambda *_: stop.set())
                return supervise(
                    [sys.executable, "-m", "alysis_code.bots.hosted_azure"],
                    payload,
                    expires_at=hosted.expires_at,
                    stop=stop,
                    emit=lambda event: print(json.dumps(event), flush=True),
                )
            # Serialize maintenance and health so a backup cannot look like a
            # hung controller. The lock contains no credentials or task data.
            with open(config["lock_path"], "a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return 0
                if args.operation == "backup":
                    if not args.force and not backup_due(config):
                        return 0
                    was_active = unit_active()
                    with backup_admission(
                        json.loads(payload),
                        credential,
                        marker=marker if was_active else None,
                    ) as admitted:
                        if not admitted:
                            record(
                                config,
                                "backup-attempt",
                                {"state": "deferred", "reason": "active_work"},
                            )
                            return 0
                        result = maintenance(
                            lambda: backup(json.loads(payload), config, credential),
                            stop=unit_command,
                            active=lambda: was_active,
                            restart_allowed=lambda: (
                                hosted.expires_at is None or time.time() < hosted.expires_at
                            ),
                        )
                    # Remove the recovery receipt only after admission release
                    # succeeded. ExecStopPost can release it after a killed backup.
                    if (
                        not was_active
                        or unit_active()
                        or (hosted.expires_at is not None and time.time() >= hosted.expires_at)
                    ):
                        marker.unlink(missing_ok=True)
                    record(config, "backup-attempt", result)
                elif not unit_active():
                    result = {
                        "state": "failed"
                        if unit_failed() or unit_failed("alysis-bots-backup.service")
                        else "inactive"
                    }
                else:
                    try:
                        result = health(
                            settings,
                            hosted,
                            client,
                            backup_failed=unit_failed("alysis-bots-backup.service"),
                        )
                    except Exception:
                        unit_command("restart")
                        alert_transition(config, ["worker_unavailable"])
                        record(config, "health", {"state": "unhealthy", "restart_requested": True})
                        return 1
                if args.operation == "health":
                    result["issues"] = operations_issues(config, hosted, result)
                    if result["issues"] and result["state"] == "healthy":
                        result["state"] = "degraded"
                    alert_transition(config, result["issues"])
                record(config, args.operation, result)
                if result["state"] in ("failed", "degraded"):
                    return 1
        return 0
    except Exception:
        # Raw transport/SQL errors, values and child output may contain secrets.
        try:
            if config is not None and args.operation != "run":
                record(
                    config,
                    "backup-attempt" if args.operation == "backup" else args.operation,
                    {"state": "failed"},
                )
                alert_transition(
                    config,
                    ["backup_failed" if args.operation == "backup" else "health_check_failed"],
                )
            else:
                print('{"event":"host_operation_failed"}', flush=True)
        except Exception:
            print('{"event":"host_operation_failed"}', flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
