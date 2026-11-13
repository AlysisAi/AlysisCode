"""Install private-host units; config stores a versioned secret URI, never values.

Run as root only on the existing trusted pilot host after reviewing the release.
This starts no service, changes no data disk, and provisions no Azure resources.
"""

import argparse
import json
import os
import re
from pathlib import Path
from uuid import uuid4


def units(python, source, config):
    command = f"{python} -m alysis_code.bots.hosted_operations"
    common = f"Environment=PYTHONPATH={source}/src\nEnvironment=PYTHONDONTWRITEBYTECODE=1\nUMask=0077\nLimitCORE=0\n"
    return {
        "alysis-bots.service": f"""[Unit]
Description=Private Alysis Bots controller
Wants=network-online.target docker.service
After=network-online.target docker.service
RequiresMountsFor=/srv/alysis-data
StartLimitIntervalSec=600
StartLimitBurst=4
[Service]
Type=simple
User=alysis
Group=alysis
{common}ExecStart={command} run --config {config}
Restart=on-failure
RestartSec=65
TimeoutStopSec=45
KillMode=control-group
NoNewPrivileges=true
[Install]
WantedBy=multi-user.target
""",
        "alysis-bots-backup.service": f"""[Unit]
Description=Verified encrypted Alysis Bots backup (offline maintenance)
After=docker.service
RequiresMountsFor=/srv/alysis-data
[Service]
Type=oneshot
{common}ExecStart={command} backup --config {config}
ExecStopPost={command} recover --config {config}
TimeoutStartSec=30min
""",
        "alysis-bots-health.service": f"""[Unit]
Description=Alysis Bots health and hung-controller recovery
After=docker.service
[Service]
Type=oneshot
{common}ExecStart={command} health --config {config}
TimeoutStartSec=3min
""",
        "alysis-bots-backup.timer": """[Unit]
Description=Daily private Bots backup
[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
AccuracySec=1min
Unit=alysis-bots-backup.service
[Install]
WantedBy=timers.target
""",
        "alysis-bots-health.timer": """[Unit]
Description=Private Bots health monitor
[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=1s
Unit=alysis-bots-health.service
[Install]
WantedBy=timers.target
""",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secret-uri", required=True)
    parser.add_argument("--source", default="/srv/alysis/current")
    parser.add_argument("--database-container", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run on the trusted host as root.")
    if not re.fullmatch(r"/srv/alysis/[a-zA-Z0-9_/-]+", args.source):
        parser.error("Use the reviewed hosted release directory.")
    from alysis_code.bots.hosted_operations import validate_secret_uri

    validate_secret_uri(args.secret_uri)
    root = Path("/etc/alysis-bots")
    root.mkdir(mode=0o755, exist_ok=True)
    config = root / "operations.json"
    values = json.loads(config.read_text()) if config.exists() else {}
    values.update(
        {
            "secret_uri": args.secret_uri,
            "database_container": args.database_container,
        }
    )
    for key, value in {
        "backup_root": "/srv/alysis-data/operations-backups",
        "receipt_root": "/srv/alysis-data/operations",
        "lock_path": "/run/lock/alysis-bots-operations.lock",
        "restart_marker": "/run/alysis-bots-backup-restart",
    }.items():
        values.setdefault(key, value)
    pending = config.with_name("operations-" + uuid4().hex)
    pending.write_text(json.dumps(values) + "\n")
    pending.chmod(0o644)  # references only; controller must read it
    os.replace(pending, config)
    for name, content in units(args.source + "/.venv/bin/python", args.source, str(config)).items():
        Path("/etc/systemd/system", name).write_text(content)
    print(json.dumps({"installed": True, "units": 5, "started": False}))


if __name__ == "__main__":
    main()
