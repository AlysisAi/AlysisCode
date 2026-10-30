"""Synthetic owner-registry and WMI broker checks; no private host data is queried.

An available host positive control is mandatory. Successful WMI creation without
an outside marker is inconclusive, not proof that a broker child stayed contained.
Only the exact owned registry leaf is removed; filesystem evidence is retained.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import time
import uuid
from pathlib import Path


def qualify(node: Path, work: Path, request, contained, record):
    source = Path(__file__).resolve().parents[1] / "tests/fixtures"
    case = work / ("platform-" + uuid.uuid4().hex)
    case.mkdir()
    outside = work.parent / ("platform-outside-" + uuid.uuid4().hex)
    outside.mkdir()
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    fixture = case / "platform.ps1"
    child = case / "platform-child.cjs"
    shutil.copyfile(source / "windows_mxc_platform.ps1", fixture)
    shutil.copyfile(source / "windows_mxc_platform_child.cjs", child)
    registry = {
        "subkey": "Software\\AlysisMxcPlatform_" + uuid.uuid4().hex,
        "owner_nonce": secrets.token_hex(32),
        "canary": secrets.token_hex(32),
        "mutable": secrets.token_hex(32),
    }
    record(
        {
            "name": "platform_boundary_scope",
            "registry_key": "HKCU\\" + registry["subkey"],
            "outside": str(outside),
            "limits": (
                "Exact synthetic HKCU/HKU leaf and WMI Win32_Process.Create only; "
                "does not qualify other registry hives, other brokers, UI or nested PSEC. "
                "Broker creation without an outside write remains inconclusive."
            ),
        }
    )

    def invoke(name, action, config, *, network=None):
        config_path = case / (name + ".json")
        config_path.write_text(json.dumps(config), encoding="utf-8")
        argv = [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            fixture,
            "-Action",
            action,
            "-Config",
            config_path,
        ]
        if network is None:
            try:
                process = subprocess.run(
                    list(map(str, argv)), cwd=work, capture_output=True, timeout=20
                )
            except subprocess.TimeoutExpired:
                record({"name": name, "ok": False, "reason": "host_control_timeout"})
                raise
            event = {
                "name": name,
                "exit_code": process.returncode,
                "stdout": process.stdout.decode(errors="replace"),
                "stderr": process.stderr.decode(errors="replace"),
            }
            record(event)
            assert process.returncode == 0, event
        else:
            # The callback preserves the actual helper owner identity and normal
            # source/binary evidence. No extra read or registry grants are added.
            denied = [*request([])["deny_read"], str(outside)]
            event = contained(name, argv, deny_read=denied, network=network, timeout_ms=15000)
        payload = json.loads(event["stdout"])
        assert isinstance(payload, dict) and payload["action"] == action, event
        return payload

    def validate(name, expected):
        row = invoke(name, "Validate", {**registry, "mutable": expected})
        assert row["owner_exact"] and row["canary_exact"] and row["mutable_exact"], row
        assert row["private_acl"] and row["only_expected_values"], row

    # Cleanup is attempted even if setup's transport failed after creating the
    # leaf. The fixture refuses deletion unless both ownership canaries match.
    failure = None
    try:
        setup = invoke("host_registry_setup", "Setup", registry)
        assert setup["created"] and setup["private_acl"], setup
        registry["sid"] = setup["sid"]
        validate("host_registry_initial_state", registry["mutable"])
        for root in ["HKCU", "HKU"]:
            host_attempt = secrets.token_hex(32)
            control = invoke(
                f"host_registry_{root}_before",
                "Registry",
                {**registry, "root": root, "attempt": host_attempt},
            )
            assert control["read_succeeded"] and control["canary_exact"], control
            assert control["write_succeeded"], control
            validate(f"host_registry_{root}_control_write", host_attempt)
            probe = invoke(
                f"contained_registry_{root}",
                "Registry",
                {**registry, "root": root, "attempt": secrets.token_hex(32)},
                network="off",
            )
            # Record the host observation before rejecting any successful read
            # or write, retaining evidence of mutations if there is a bypass.
            observed = invoke(
                f"host_registry_{root}_after_containment",
                "Validate",
                {**registry, "mutable": host_attempt},
            )
            assert not probe["read_succeeded"] and not probe["write_succeeded"], probe
            assert probe["read_result"] in {"access_denied", "not_found"}, probe
            assert probe["write_result"] in {"access_denied", "not_found"}, probe
            assert all(
                observed[key]
                for key in [
                    "owner_exact",
                    "canary_exact",
                    "mutable_exact",
                    "private_acl",
                    "only_expected_values",
                ]
            ), observed
            after_attempt = secrets.token_hex(32)
            after = invoke(
                f"host_registry_{root}_after",
                "Registry",
                {**registry, "root": root, "attempt": after_attempt},
            )
            assert after["read_succeeded"] and after["canary_exact"] and after["write_succeeded"], (
                after
            )
            validate(f"host_registry_{root}_after_write", after_attempt)
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            cleanup = invoke("host_registry_cleanup", "Cleanup", registry)
            assert cleanup["removed"] or cleanup["already_absent"], cleanup
        except Exception as error:
            record(
                {"name": "registry_cleanup_failed", "reason": str(error), "key": registry["subkey"]}
            )
            if failure is None:
                raise

    def broker(name, *, network=None):
        nonce = secrets.token_hex(32)
        marker = outside / (name + ".marker")
        acknowledgement = case / (name + ".child.json")
        child_config = case / (name + ".child-input.json")
        child_config.write_text(
            json.dumps({"nonce": nonce, "marker": str(marker), "ack": str(acknowledgement)}),
            encoding="utf-8",
        )
        command = subprocess.list2cmdline(
            list(
                map(
                    str,
                    [node, "--preserve-symlinks", "--preserve-symlinks-main", child, child_config],
                )
            )
        )
        row = invoke(name, "Wmi", {"command": command, "cwd": str(case)}, network=network)
        deadline = time.monotonic() + (8 if row.get("return_value") == 0 else 1)
        child_row = None
        while time.monotonic() < deadline:
            if acknowledgement.exists():
                try:
                    child_row = json.loads(acknowledgement.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass
                else:
                    break
            time.sleep(0.05)
        marker_exists = marker.exists()
        marker_exact = marker_exists and marker.read_text(encoding="ascii") == nonce
        observed = {
            "name": name + "_host_observation",
            "broker": row,
            "outside_marker_exists": marker_exists,
            "outside_marker_exact": marker_exact,
            "child_ack": child_row,
        }
        record(observed)
        if network is None:
            assert row.get("return_value") == 0 and marker_exact, observed
            assert child_row and child_row.get("nonce") == nonce, observed
            assert child_row.get("outside_written") is True, observed
        else:
            assert not marker_exists, observed
            # Absence alone is not a negative control: successful broker
            # creation requires separate descendant/job qualification.
            assert row.get("return_value") != 0, {
                **observed,
                "reason": "broker_created_child_containment_unqualified",
            }
            assert row.get("return_value") in {2, 3} or row.get("result") == "access_denied", (
                observed
            )
        return marker

    broker("host_wmi_before")
    denied_markers = []
    for network in ["off", "on"]:
        denied_markers.append(broker("contained_wmi_" + network, network=network))
    broker("host_wmi_after")
    # A late RPC completion must not silently become a passing observation.
    assert all(not marker.exists() for marker in denied_markers), "late WMI outside marker"
    record({"name": "platform_boundary_passed", "ok": True})
