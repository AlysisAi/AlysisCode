"""Real Linux application cleanup and durable-file checks, with no provider calls.

Run only in an isolated disposable Linux QA container. The optional --probe
records the pre-change missing stop capability rather than claiming a pass.
"""

import argparse
import hashlib
import json
import tempfile
import threading
import time
from pathlib import Path

import psutil

from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.workspaces import WorkspaceStore
from alysis_code.cancellation import EventCancellationToken


def main(probe=False):
    token = EventCancellationToken(threading.Event())
    samples, tracked = [], {}

    def measure(stage):
        pss = 0
        for process in psutil.Process().children(recursive=True):
            try:
                tracked[process.pid] = process
                pss += process.memory_full_info().pss
            except psutil.NoSuchProcess:
                pass
        samples.append(dict(stage=stage, pss_bytes=pss))

    with tempfile.TemporaryDirectory(prefix="housekeeping-study-") as directory:
        manager = DesktopComputers(
            "owner", ComputerViews(), workspaces=WorkspaceStore(Path(directory))
        )
        task = dict(id="one", bot_id="bot")

        def call(name, **arguments):
            return manager.call(task, name, arguments, token, approved=True)

        try:
            first = call(
                "computer_run",
                command="python -c \"from pathlib import Path; import os; Path('source.bin').write_bytes(os.urandom(64)); Path('/tmp/task-scratch').write_bytes(b'x' * 1048576)\"",
            )
            workspace = Path(manager._computers["bot"].directory.name)
            original = (workspace / "source.bin").read_bytes()
            # A distinct application remains needed while another is finished.
            keep = call("computer_launch", command="exec sleep 300")
            finished = call(
                "computer_launch",
                command="exec python -c \"from pathlib import Path; import time; data=bytearray(48*1024*1024); Path('allocation-ready').write_text('ready'); time.sleep(300)\"",
            )
            for _ in range(50):
                if (workspace / "allocation-ready").exists():
                    break
                time.sleep(0.1)
            assert (workspace / "allocation-ready").exists()
            measure("two_applications")
            try:
                stopped = call("computer_stop_process", process_id=finished["process_id"])
            except Exception as exc:
                if not probe:
                    raise
                print(
                    json.dumps(
                        dict(supported=False, error=str(exc), final_task_cleanup_required=True)
                    )
                )
                return
            assert stopped["stopped"] and not stopped["running"], stopped
            assert call("computer_process", process_id=keep["process_id"])["running"]
            assert call("computer_stop_process", process_id=finished["process_id"])["stopped"]
            try:
                call("computer_stop_process", process_id="unknown-handle")
            except Exception:
                pass
            else:
                raise AssertionError("Unowned process accepted")
            # Slot reuse while the same computer and needed application survive.
            for _ in range(6):
                launched = call("computer_launch", command="exec sleep 300")
                assert call("computer_stop_process", process_id=launched["process_id"])["stopped"]
            measure("after_selective_cleanup")
            assert samples[0]["pss_bytes"] - samples[1]["pss_bytes"] > 32 * 1024 * 1024
            assert call("computer_process", process_id=keep["process_id"])["running"]
            assert (workspace / "source.bin").read_bytes() == original
            old = manager._computers["bot"].executor
            old_tmp = Path(old._directory.name)
            manager.release(task["id"], cancelled=False)
            assert not old.alive() and not old_tmp.exists()
            assert (workspace / "source.bin").read_bytes() == original
            task = dict(id="two", bot_id="bot")
            renewed = call(
                "computer_run", command="test ! -e /tmp/task-scratch && sha256sum source.bin"
            )
            assert renewed["computer_session_id"] != first["computer_session_id"]
            assert renewed["exit_code"] == 0
            assert hashlib.sha256(original).hexdigest() in renewed["output"]
            # Old handles cannot affect the new session.
            try:
                call("computer_stop_process", process_id=keep["process_id"])
            except Exception:
                pass
            else:
                raise AssertionError("Retired process handle accepted")
            call("computer_launch", command="exec sleep 300")
            measure("before_stop")
            manager.release(task["id"], cancelled=True)
            for _ in range(40):
                if not any(
                    p.is_running() and p.status() != psutil.STATUS_ZOMBIE for p in tracked.values()
                ):
                    break
                time.sleep(0.1)
            assert not any(
                p.is_running() and p.status() != psutil.STATUS_ZOMBIE for p in tracked.values()
            )
            assert (workspace / "source.bin").read_bytes() == original
            measure("after_stop")
            print(
                json.dumps(
                    dict(
                        passed=True,
                        samples=samples,
                        provider_calls=0,
                        cloud_calls=0,
                        selective_cleanup=True,
                        other_application_preserved=True,
                        workspace_preserved=True,
                        scratch_removed=True,
                        stale_handle_rejected=True,
                    )
                )
            )
        finally:
            manager.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true")
    main(parser.parse_args().probe)
