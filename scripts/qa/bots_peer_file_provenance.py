"""Real offline file export -> explicitly selected, durable peer snapshot.

Requires integrated desktop modules on PYTHONPATH in a network-none Linux lab.
All state and input are synthetic and temporary. No model provider is called.
"""

import hashlib
import json
import tempfile
from pathlib import Path

from alysis_code.bots import desktop
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.cooperation import PeerRequest, PeerStore
from alysis_code.bots.models import BotInput, FileInput, TaskInput
from alysis_code.bots.proposals import ActionDecision, ActionProposal, ProposalStore
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store


def main():
    with tempfile.TemporaryDirectory(prefix="alysis-peer-20261019-files-") as temporary:
        store = Store(Path(temporary) / "study.db")
        bot = store.save_bot("owner", BotInput(name="Source", purpose="Compute requested work"))
        peer = store.save_bot("owner", BotInput(name="Reviewer", purpose="Review selected work"))
        chat = store.create_chat("owner", bot["id"], "Synthetic file provenance")

        def claim(message, request_id):
            store.enqueue("owner", chat["id"], TaskInput(message=message, request_id=request_id))
            task = store.claim("owner")
            assert task is not None
            return task

        preparation = claim("Prepare the offline computation.", "prepare-offline")
        proposals = ProposalStore(store, "owner")
        proposal = proposals.propose(
            preparation,
            ActionProposal(
                question="Allow offline work?",
                instructions="Compute the CSV total and preserve the actual result file.",
                offline_computer=True,
            ),
        )
        store.finish("owner", preparation["id"], "completed", content="Review the task.")
        proposals.decide(
            bot["id"],
            proposal["id"],
            ActionDecision(action="accept", expected_revision=1, offline_computer=True),
        )
        task = store.claim("owner")
        source = store.save_file(
            "owner",
            bot["id"],
            FileInput(name="入力.csv", content="label,quantity,price\r\nΝεφέλη / 東京,3,2.50\r\n"),
        )
        private = store.save_file(
            "owner",
            bot["id"],
            FileInput(name="unselected.txt", content="UNSELECTED_PRIVATE_DATA"),
        )
        manager = desktop.DesktopComputers("owner", ComputerViews(), store=store)
        runtime = BotRuntime(store, "owner", lambda _: None, desktop_computers=manager)
        token = runtime.task_token(task["id"])

        def call(name, arguments):
            return manager.call(task, name, arguments, token, approved=True)

        try:
            imported = call("computer_import_file", {"file_id": source["id"]})
            assert imported["source_file_id"] == source["id"]
            command = """python3 - <<'PY'
import csv
from decimal import Decimal
from pathlib import Path
with Path('/workspace/入力.csv').open(newline='') as source:
    rows = list(csv.DictReader(source))
with Path('/workspace/result.csv').open('w', newline='') as target:
    writer = csv.writer(target)
    writer.writerow(['label', 'total'])
    for row in rows:
        writer.writerow([row['label'], Decimal(row['quantity']) * Decimal(row['price'])])
PY"""
            assert call("computer_run", {"command": command})["exit_code"] == 0
            exported = call("computer_save_file", {"name": "result.csv"})
            expected = "label,total\r\nΝεφέλη / 東京,7.50\r\n"
            expected_hash = hashlib.sha256(expected.encode()).hexdigest()
            saved = store.file("owner", bot["id"], exported["id"])
            assert saved["content"] == expected and saved["source_task_id"] == task["id"]
            assert exported["sha256"] == expected_hash and exported["bytes"] == len(
                expected.encode()
            )
            assert "content" not in exported
            workspace = manager._computers[bot["id"]].executor.workspace
            manager.release(task["id"], cancelled=False)
            assert not workspace.exists()
            store.finish("owner", task["id"], "completed", content="Saved result.csv.")
        finally:
            manager.close()

        peers = PeerStore(store, "owner")
        no_files = claim("Ask the reviewer a question without sharing files.", "no-files-request")
        empty = peers.submit(
            no_files,
            PeerRequest(recipient_bot_id=peer["id"], brief="Review this brief only."),
            {},
            8,
        )
        assert peers.for_task(no_files["id"])["shared_files"] == []
        store.cancel("owner", no_files["id"])
        selected = claim("Ask the reviewer to check the saved result.csv.", "selected-file-request")
        receipt = peers.submit(
            selected,
            PeerRequest(
                recipient_bot_id=peer["id"],
                brief="Review the computed totals.",
                shared_file_ids=[exported["id"]],
            ),
            {},
            8,
        )
        assert receipt["id"] != empty["id"]
        assert receipt["shared_files"] == [
            {"id": exported["id"], "name": "result.csv", "sha256": expected_hash}
        ]
        assert not store.list_files("owner", peer["id"])
        store.delete_item("owner", bot["id"], exported["id"], kind="files")
        restarted = PeerStore(Store(store.path), "owner")
        item, messages, _ = restarted.context(receipt["id"])
        assert item["shared_files"] == [
            {
                "id": exported["id"],
                "name": "result.csv",
                "content": expected,
                "sha256": expected_hash,
            }
        ]
        context = json.dumps(messages, ensure_ascii=False)
        assert private["content"] not in context and source["id"] not in context
        assert receipt["shared_files"] == restarted.read(peer["id"], receipt["id"])["shared_files"]
        assert store.usage("owner")["calls"] == 0
        print(
            json.dumps(
                {
                    "passed": True,
                    "kind": "Real Linux file computation and durable peer store; no model calls",
                    "export_source_task_matches": saved["source_task_id"] == task["id"],
                    "review_source_differs_from_export_task": selected["id"] != task["id"],
                    "exact_utf8_sha256": expected_hash,
                    "explicit_selection_only": True,
                    "snapshot_survives_library_delete_and_store_reopen": True,
                    "recipient_library_unchanged": True,
                    "workspace_removed": not workspace.exists(),
                    "live_model_calls": 0,
                }
            )
        )


if __name__ == "__main__":
    main()
