"""Verify/stage a complete hosted release, or explicitly activate a same-schema update.

Run activation as root on the existing controller. References only in arguments;
credentials are obtained with its managed identity and remain in RAM.
"""

import argparse
import fcntl
import io
import json
import os
import re
import subprocess
import time
from pathlib import Path

import httpx

from alysis_code.bots import hosted_operations as operations
from alysis_code.bots.hosted_maintenance import MaintenanceLease
from alysis_code.bots.hosted_release import activate_release, prepare_release, verify_release


def release_path(path):
    path = Path(path)
    if path.parent != Path("/srv/alysis/releases") or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,100}", path.name
    ):
        raise ValueError("Use an immutable hosted release directory.")
    return path


def activate(candidate, previous, config):
    import psycopg
    from azure.identity import ManagedIdentityCredential

    new, old = verify_release(candidate), verify_release(previous)
    with open(config["lock_path"], "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with ManagedIdentityCredential() as credential, httpx.Client(trust_env=False) as client:
            payload = operations.fetch_bootstrap(config["secret_uri"], credential, client)
            settings, hosted = operations.bootstrap(io.BytesIO(payload))
            if time.time() >= hosted.expires_at:
                raise ValueError("Start a new approved guarded test window before activation.")
            # Read the schema before constructing a store: initialization can
            # migrate it. This path never disguises a migration as code rollback.
            with psycopg.connect(hosted.database_dsn, autocommit=True, connect_timeout=5) as db:
                schema = db.execute("SELECT version FROM bots_schema WHERE id=1").fetchone()[0]
            if new["schema"] != schema or old["schema"] != schema:
                return {"activated": False, "reason": "schema_change_requires_reviewed_upgrade"}
            with operations.maintenance_store(json.loads(payload), credential) as store:
                lease = MaintenanceLease(store)
                token = lease.acquire()
                if token is None:
                    return {"activated": False, "reason": "active_work"}
                health_timer = (
                    subprocess.run(
                        ["systemctl", "is-active", "--quiet", "alysis-bots-health.timer"]
                    ).returncode
                    == 0
                )
                try:
                    subprocess.run(
                        [
                            "systemctl",
                            "stop",
                            "alysis-bots-health.timer",
                            "alysis-bots-health.service",
                        ],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=30,
                    )

                    def install(path):
                        subprocess.run(
                            [
                                str(path / ".venv/bin/python"),
                                str(path / "scripts/bots-hosted/install.py"),
                                "--secret-uri",
                                config["secret_uri"],
                                "--source",
                                str(path),
                                "--database-container",
                                config["database_container"],
                            ],
                            env={**os.environ, "PYTHONPATH": str(path / "src")},
                            check=True,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=30,
                        )
                        subprocess.run(
                            ["systemctl", "daemon-reload"],
                            check=True,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=30,
                        )

                    def ready(_path):
                        deadline = time.monotonic() + 45
                        while time.monotonic() < deadline:
                            try:
                                operations.health(settings, hosted, client)
                                return True
                            except Exception:
                                time.sleep(0.5)
                        return False

                    result = activate_release(
                        candidate,
                        previous,
                        schema=schema,
                        install=install,
                        control=operations.unit_command,
                        ready=ready,
                    )
                    operations.record(config, "release", result)
                    return result
                finally:
                    try:
                        lease.release(token)
                    finally:
                        if health_timer:
                            subprocess.run(
                                ["systemctl", "start", "alysis-bots-health.timer"],
                                check=True,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                timeout=30,
                            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "activate"))
    parser.add_argument("--candidate", required=True, type=release_path)
    parser.add_argument("--previous", required=True, type=release_path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--overlay", type=Path)
    parser.add_argument(
        "--qualified-environment",
        type=Path,
        help="Existing environment to qualify against the candidate dependencies",
    )
    parser.add_argument("--config", type=Path, default=Path("/etc/alysis-bots/operations.json"))
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise ValueError("Run on the existing trusted controller as root.")
        if args.operation == "prepare":
            if args.manifest is None or args.overlay is None:
                raise ValueError("Provide a reviewed manifest and source delta directory.")
            result = prepare_release(
                args.previous,
                args.candidate,
                json.loads(args.manifest.read_text()),
                args.overlay,
                qualified_environment=args.qualified_environment,
            )
        else:
            result = activate(args.candidate, args.previous, json.loads(args.config.read_text()))
        print(json.dumps(result), flush=True)
        return 0 if result.get("prepared") or result.get("activated") else 1
    except Exception:
        print('{"activated":false,"reason":"hosted_release_unconfirmed"}', flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
