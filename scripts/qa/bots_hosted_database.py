"""Prepare the private pilot database without persisting a bootstrap password.

The operator starts the pinned image with ``server_command`` and a dedicated
private data mount at /var/lib/postgresql. Host authentication initially rejects
every TCP connection; preparation uses Docker's local postgres Unix identity.
A JSON password arrives over stdin,
never arguments, image environment, files or receipts. This is pilot setup, not
a public database service or a backup system.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time

IMAGE = "postgres@sha256:41fc5342eefba6cc2ccda736aaf034bbbb7c3df0fdb81516eba1ba33f360162c"
PGDATA = "/var/lib/postgresql/18/pilot"


def server_command():
    # Bypass the image's password-in-environment bootstrap. A fresh cluster has
    # peer-only Unix administration and no TCP access until configure succeeds.
    # The trusted host creates a root-owned 0700 bind directory. Make only this
    # dedicated layout's ancestors traversable by postgres before dropping UID;
    # never recursively change existing cluster or unrelated file ownership.
    return [
        "bash",
        "-ceu",
        'test "$PGDATA" = /var/lib/postgresql/18/pilot; '
        "test ! -L /var/lib/postgresql; test ! -L /var/lib/postgresql/18; "
        'test ! -L "$PGDATA"; '
        'mkdir -p "$PGDATA"; '
        "chown postgres:postgres /var/lib/postgresql /var/lib/postgresql/18; "
        "chmod 0700 /var/lib/postgresql /var/lib/postgresql/18; "
        'if [ ! -e "$PGDATA/PG_VERSION" ]; then '
        'test -z "$(ls -A "$PGDATA")"; '
        'chown postgres:postgres "$PGDATA"; '
        'gosu postgres initdb -D "$PGDATA" --encoding=UTF8 '
        "--auth-local=peer --auth-host=reject; fi; "
        'exec gosu postgres postgres -D "$PGDATA" '
        "-c listen_addresses=* -c log_statement=none -c log_min_error_statement=panic",
    ]


def run(container, args, *, data=None):
    result = subprocess.run(
        ["docker", "exec", "-i", "--user", "postgres", container, *args],
        input=data,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode:
        # psql errors can include the failed SQL, including credential literals.
        raise RuntimeError("Private database setup failed; no credentials were logged.")
    return result.stdout.strip()


def wait_ready(container):
    # Docker's start receipt only confirms process creation. Initial initdb can
    # still be running. Retry only this read-only readiness probe, never SQL.
    deadline = time.monotonic() + 30
    while True:
        try:
            run(container, ["pg_isready", "-U", "postgres", "-t", "1"])
            return
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise RuntimeError("Private database did not become ready.") from None
            time.sleep(0.25)


def configure(container, password):
    if not re.fullmatch(r"alysis-bots-[a-z0-9-]{1,80}", container):
        raise ValueError("Use the dedicated pilot database container.")
    if not isinstance(password, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", password):
        raise ValueError("Supply a new random URL-safe password of at least 32 characters.")
    wait_ready(container)
    psql = ["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "postgres"]
    version = int(run(container, psql, data="SHOW server_version_num;\n"))
    if version < 180000:
        raise ValueError("The pilot needs PostgreSQL 18 or later.")
    run(
        container,
        psql,
        data="""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='alysis') THEN
            CREATE ROLE alysis LOGIN;
          END IF;
        END $$;
    """,
    )
    # Password validation above permits no SQL delimiters. The statement travels
    # only through stdin; the cluster disables statement/error-statement logging.
    run(
        container,
        psql,
        data=(
            "SET log_statement='none'; SET log_min_error_statement='panic'; "
            "SET password_encryption='scram-sha-256'; "
            "ALTER ROLE alysis LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 24 PASSWORD '" + password + "';\n"
        ),
    )
    exists = run(container, psql, data="SELECT 1 FROM pg_database WHERE datname='alysis_bots';\n")
    if not exists:
        run(container, psql, data="CREATE DATABASE alysis_bots OWNER alysis ENCODING 'UTF8';\n")
    owner = run(
        container,
        psql,
        data=("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='alysis_bots';\n"),
    )
    if owner != "alysis":
        raise RuntimeError("Existing pilot database has an unexpected owner; it was not replaced.")
    run(
        container,
        psql,
        data=(
            "REVOKE ALL ON DATABASE alysis_bots FROM PUBLIC; "
            "GRANT CONNECT,TEMPORARY ON DATABASE alysis_bots TO alysis;\n"
        ),
    )
    # No trust-auth bootstrap window, and no TCP superuser login. The container
    # port must additionally be published on host loopback only.
    run(
        container,
        ["sh", "-ceu", 'cat > "$PGDATA/pg_hba.conf"'],
        data=("local all postgres peer\nhost alysis_bots alysis all scram-sha-256\n"),
    )
    errors = run(
        container, psql, data=("SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL;\n")
    )
    if errors != "0":
        raise RuntimeError("Database authentication configuration was not accepted.")
    if run(container, psql, data="SELECT pg_reload_conf();\n") != "t":
        raise RuntimeError("Database authentication reload was not confirmed.")
    return {"prepared": True, "server_version": version, "role": "alysis", "auth": "scram-sha-256"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.readline(1025)
        if len(raw) > 1024:
            raise ValueError("Bootstrap exceeds allowance.")
        result = configure(args.container, json.loads(raw)["password"])
        print(json.dumps(result))
    except Exception:
        raise SystemExit(
            "Private database preparation failed; inspect only sanitized receipts."
        ) from None


if __name__ == "__main__":
    main()
