"""Real installed CLI subprocess/storage check; no model or Desktop claimed."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
import uuid
from pathlib import Path


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    root = Path(sys.argv[1]).resolve()
    root.mkdir(exist_ok=False)
    env = {
        **os.environ,
        "ALYSIS_MEMORY_HOME": str(root / "memory"),
        "ALYSIS_CONFIG_DIR": str(root / "config"),
        "ALYSIS_DATA_DIR": str(root / "data"),
        "PYTHONUTF8": "1",
    }
    env.pop("ALYSIS_MEMORY_DISABLED", None)
    cli = Path(sys.executable).with_name("alysis.exe")
    report = {
        "cli": str(cli),
        "evidenceType": "real CLI subprocess and SQLite/Markdown storage; no model or app",
        "calls": [],
    }

    def request(value, success=True):
        process = subprocess.run(
            [str(cli), "memory", "request"],
            input=json.dumps(value),
            text=True,
            encoding="utf-8",
            capture_output=True,
            env=env,
            cwd=root,
            timeout=45,
        )
        report["calls"].append(
            {
                "request": value,
                "exitCode": process.returncode,
                "stdout": process.stdout,
                "stderr": process.stderr,
            }
        )
        (root / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        assert (process.returncode == 0) == success, process.stdout + process.stderr
        return json.loads(process.stdout if success else process.stderr)

    try:
        initial = request({"action": "read"})
        assert initial["enabled"] is True and initial["entries"] == []
        token = "Προτίμηση 日本語 " + str(uuid.uuid4())
        saved = request({"action": "save", "content": token, "this_computer": True})
        entry = saved["snapshot"]["entries"][0]
        assert entry["content"] == token and entry["device_id"] == initial["device_id"]
        project_id = str(uuid.uuid4())
        project = request({"action": "read", "project_id": project_id})["project"]
        assert project["include_general"] is False
        result = request(
            {
                "action": "project_write",
                "project_id": project_id,
                "content": "# Isolated project\n" + str(uuid.uuid4()),
                "revision": project["revision"],
            }
        )
        written = result["snapshot"]["project"]
        assert Path(written["path"]).read_text(encoding="utf-8") == written["content"]
        request(
            {
                "action": "project_write",
                "project_id": project_id,
                "content": "stale",
                "revision": project["revision"],
            },
            False,
        )
        request(
            {
                "action": "project_settings",
                "project_id": project_id,
                "enabled": True,
                "include_general": True,
                "revision": written["policy_revision"],
            }
        )
        assert (
            request({"action": "read", "project_id": project_id})["project"]["include_general"]
            is True
        )
        assert (
            request({"action": "read", "project_id": str(uuid.uuid4())})["project"]["content"] == ""
        )
        request(
            {
                "action": "save",
                "id": entry["id"],
                "revision": entry["revision"],
                "content": "Edited preference",
            }
        )
        request(
            {
                "action": "save",
                "id": entry["id"],
                "revision": entry["revision"],
                "content": "Stale preference",
            },
            False,
        )
        latest = request({"action": "read"})["entries"][0]
        assert latest["content"] == "Edited preference"
        request({"action": "delete", "id": latest["id"], "revision": latest["revision"]})
        assert request({"action": "read"})["entries"] == []
        request(
            {
                "action": "save",
                "id": latest["id"],
                "revision": latest["revision"],
                "content": "Revival",
            },
            False,
        )
        exported = request({"action": "export"})
        assert exported["entries"][0]["deleted"] == 1
        assert "Edited preference" not in json.dumps(exported)
        assert request({"action": "read"})["entries"] == []
        report["status"] = "pass"
    except Exception:
        report["status"] = "fail"
        report["traceback"] = traceback.format_exc()
    (root / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps({"status": report["status"], "calls": len(report["calls"]), "root": str(root)})
    )
    return int(report["status"] != "pass")


if __name__ == "__main__":
    raise SystemExit(main())
