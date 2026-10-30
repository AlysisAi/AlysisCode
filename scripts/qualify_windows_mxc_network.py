"""Replay local TCP/UDP receiver qualification with an exactly pinned MXC helper.

Creates only a new synthetic output directory. No model calls, ACL changes,
firewall changes, remote peer scans, or dependency on the installed engine.
This qualifies local host boundaries, not separate-machine LAN/remote ingress.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

# Resolve only our checked-out helpers, including under isolated Python (-I).
# The cwd, user site and installed engine do not supply qualification modules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
# qualify_windows_mxc adds the trusted src directory before the contract import.
# isort: off
from qualify_windows_mxc import owner  # noqa: E402
from windows_mxc_network import qualify  # noqa: E402

from alysis_code.windows_mxc_contract import WINDOWS_RUNTIME_DIRECTORIES, helper_contract  # noqa: E402
# isort: on


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--node", type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Native Windows is required")
    helper = args.helper.resolve(strict=True)
    if digest(helper) != args.sha256.lower():
        parser.error("Helper digest mismatch; no helper was executed")
    node_name = args.node or shutil.which("node")
    if not node_name:
        parser.error("Node.js is required for the bounded sender")
    node = Path(node_name).resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(exist_ok=False)
    work, private = output / "workspace", output / "private"
    work.mkdir()
    private.mkdir()
    (private / "canary").write_text("synthetic-only", encoding="utf-8")
    (work / ".git").mkdir()
    system = Path(os.environ["SystemRoot"])
    env = {key: os.environ[key] for key in ("SystemRoot", "LOCALAPPDATA")}
    env.update(
        PATH=os.pathsep.join([str(system / "System32"), str(node.parent)]),
        NODE_OPTIONS="--preserve-symlinks --preserve-symlinks-main",
    )
    sources = [
        Path(__file__),
        Path(__file__).with_name("windows_mxc_network.py"),
        Path(__file__).with_name("qualify_windows_mxc.py"),
        Path(__file__).resolve().parents[1] / "src/alysis_code/windows_mxc_contract.py",
    ]
    source_dir = output / "sources"
    source_dir.mkdir()
    for source in sources:
        shutil.copyfile(source, source_dir / source.name)
    report = {
        "scope": "local host receiver evidence; not separate-machine LAN or remote ingress",
        "helper_sha256": digest(helper),
        "node_sha256": digest(node),
        "source_sha256": {source.name: digest(source) for source in sources},
        "windows": list(sys.getwindowsversion()),
        "python": sys.version,
        "events": [],
        "passed": False,
    }

    def save():
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    def record(event):
        report["events"].append(event)
        save()
        if event.get("stage") == "qualified" or event["name"].endswith("udp_denied"):
            print(json.dumps(event), flush=True)

    def request(argv, **overrides):
        value = {
            "protocol": 4,
            "cwd": str(work),
            "argv": list(map(str, argv)),
            "read": [
                str(work),
                str(node.parent),
                *[
                    str(system / name)
                    for name in WINDOWS_RUNTIME_DIRECTORIES
                    if (system / name).exists()
                ],
            ],
            "write": [str(work)],
            "deny_read": [str(private)],
            "deny_write": [str(work / ".git")],
            "env": list(env.items()),
            "network": "off",
            "timeout_ms": 15000,
            **owner(),
        }
        value.update(overrides)
        return value

    try:
        protocol = subprocess.run(
            [str(helper), "mxc-protocol"], capture_output=True, timeout=15, check=True
        )
        actual = json.loads(protocol.stdout)
        for key, expected in helper_contract().items():
            if actual.get(key) != expected:
                raise ValueError(f"Helper contract mismatch: {key}")
        report["helper_contract"] = actual
        qualify(helper, node, work, request, record)
        if any(event.get("qualified") is False for event in report["events"]):
            raise AssertionError(
                "A required endpoint is unavailable; partial results are not complete"
            )
        report["passed"] = True
        return 0
    except Exception:
        report["error"] = traceback.format_exc()
        print(report["error"], file=sys.stderr)
        return 1
    finally:
        save()


if __name__ == "__main__":
    raise SystemExit(main())
