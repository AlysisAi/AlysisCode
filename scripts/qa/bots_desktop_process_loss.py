"""Actual Linux process loss, persistent partial files and fresh-computer recovery.

Runs only in an isolated network-none lab. No model provider is called. Faults
target processes created by this study, never another service or computer.
"""

import json
import os
import signal
import tempfile
import time
from pathlib import Path

from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.desktop_linux import DesktopError, OfflineLinuxDesktop
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.proposals import ActionDecision, ActionProposal, ProposalStore
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store


def descendants(root):
    rows = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            rows[int(entry.name)] = (int(fields[1]), (entry / "comm").read_text().strip())
        except (FileNotFoundError, ProcessLookupError):
            pass
    owned = {root}
    while True:
        more = {pid for pid, (parent, _) in rows.items() if parent in owned}
        if more <= owned:
            return {pid: rows[pid][1] for pid in owned if pid in rows}
        owned |= more


def study(fault, require_receipts):
    with tempfile.TemporaryDirectory(prefix="bots-linux-night-20261019-process-loss-") as temporary:
        database = Path(temporary) / "study.db"
        store = Store(database)
        bot = store.save_bot("owner", BotInput(name="Study", purpose="Recover partial work"))
        other = store.save_bot("owner", BotInput(name="Other", purpose="Remain separate"))
        chat = store.create_chat("owner", bot["id"], "Recovery study")
        store.enqueue(
            "owner", chat["id"], TaskInput(message="Prepare", request_id="prepare-recovery")
        )
        preparation = store.claim("owner")
        proposals = ProposalStore(store, "owner")
        proposal = proposals.propose(
            preparation,
            ActionProposal(
                question="Use offline computer?",
                instructions="Save partial work and recover.",
                offline_computer=True,
            ),
        )
        store.finish("owner", preparation["id"], "completed", content="Review")
        proposals.decide(
            bot["id"],
            proposal["id"],
            ActionDecision(
                action="accept",
                expected_revision=1,
                offline_computer=True,
            ),
        )
        task = store.claim("owner")
        instances = []

        def factory(*args, **kwargs):
            executor = OfflineLinuxDesktop(*args, **kwargs)
            instances.append(executor)
            return executor

        views = ComputerViews()
        manager = DesktopComputers("owner", views, factory=factory, store=store)
        runtime = BotRuntime(store, "owner", lambda _: None, desktop_computers=manager)
        token = runtime.task_token(task["id"])

        def call(tool, **arguments):
            return manager.call(task, tool, arguments, token, approved=True, image_input=True)

        try:
            first = call(
                "computer_run",
                command="printf 'Café — Αθήνα' > partial.txt; printf scratch > unsaved.txt",
            )
            assert first["exit_code"] == 0
            saved = call("computer_save_file", name="partial.txt")
            before = call("computer_observe").observation
            old_executor = instances[0]
            old_workspace = old_executor.workspace
            old_session = views.frame("owner", bot["id"])["session_id"]
            assert views.frame("owner", other["id"]) == {"state": "not_running"}
            owned = descendants(old_executor._process.pid)
            if fault == "executor":
                target = old_executor._process.pid
            else:
                targets = [pid for pid, name in owned.items() if name == "Xvfb"]
                assert len(targets) == 1, owned
                target = targets[0]
            os.kill(target, signal.SIGKILL)
            if fault == "executor":
                old_executor._process.wait(timeout=3)
            else:
                time.sleep(0.15)
            errors = []
            try:
                after = call("computer_observe").observation
            except DesktopError as exc:
                errors.append(str(exc))
                after = call("computer_observe").observation
            assert len(instances) == 2
            assert not old_executor.alive() and not old_workspace.exists()
            assert views.frame("owner", bot["id"])["session_id"] != old_session
            assert views.frame("owner", other["id"]) == {"state": "not_running"}
            try:
                call("computer_click", source_id=before["source_id"], x=10, y=10)
            except DesktopError as exc:
                assert "expired" in str(exc)
            else:
                raise AssertionError("Previous computer image accepted")
            # The saved library survives actual child loss and reopening SQLite.
            persisted = Store(database).file("owner", bot["id"], saved["id"])
            assert persisted["content"] == "Café — Αθήνα"
            assert persisted["source_task_id"] == task["id"]
            assert not any(instances[-1].workspace.iterdir())
            restored = call("computer_import_file", file_id=saved["id"])
            assert restored["sha256"] == saved["sha256"]
            actual = call("computer_run", command="cat partial.txt; test ! -e unsaved.txt")
            assert actual["exit_code"] == 0 and actual["output"] == persisted["content"]
            announced = bool(after.get("computer_created") and after.get("computer_session_id"))
            if require_receipts:
                assert first["computer_created"] is True
                assert before["computer_created"] is False
                assert (
                    saved["computer_session_id"]
                    == before["computer_session_id"]
                    == first["computer_session_id"]
                )
                assert announced and after["computer_session_id"] != before["computer_session_id"]
                assert (
                    restored["computer_session_id"]
                    == actual["computer_session_id"]
                    == after["computer_session_id"]
                )
                assert restored["computer_created"] is actual["computer_created"] is False
            manager.release(task["id"], cancelled=False)
            assert not instances[-1].alive() and not instances[-1].workspace.exists()
            assert views.frame("owner", bot["id"]) == {"state": "not_running"}
            return {
                "fault": fault,
                "saved_partial_file_survived": True,
                "unsaved_workspace_removed": True,
                "old_frame_rejected": True,
                "new_workspace_restored_from_library": True,
                "other_bot_view_unavailable": True,
                "all_computers_closed": True,
                "new_session_announced_to_model": announced,
                "errors": errors,
                "live_model_calls": store.usage("owner")["calls"],
            }
        finally:
            manager.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-session-receipts", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            {
                "checks": [
                    study(fault, args.require_session_receipts) for fault in ("executor", "display")
                ]
            }
        )
    )
