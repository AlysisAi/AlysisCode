"""Provider-free real Linux library ↔ offline workspace transfer qualification."""

import hashlib
import json
import tempfile
from pathlib import Path

from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.desktop_linux import DesktopError
from alysis_code.bots.models import BotInput, FileInput, TaskInput
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store


def main():
    checks = []
    with tempfile.TemporaryDirectory(prefix="bots-linux-night-20261019-files-") as temp:
        store = Store(Path(temp) / "bots.db")
        bot = store.save_bot(
            "owner", BotInput(name="Transfer test", purpose="Synthetic UTF-8 files")
        )
        chat = store.create_chat("owner", bot["id"], "Transfer study")
        store.enqueue(
            "owner", chat["id"], TaskInput(message="Transfer test", request_id="transfer-study")
        )
        task = store.claim("owner")
        # No provider is called. This exercises the manager/actual executor under
        # a trusted fixture grant; production grant/echo has separate API tests.
        token = BotRuntime(store, "owner", lambda _: None).task_token(task["id"])
        manager = DesktopComputers("owner", ComputerViews(), store=store)
        content = '日本語 — Αθήνα — Café\r\n\tquoted \\""\n' * 1600
        assert 64000 < len(content.encode()) <= 100000
        source = store.save_file("owner", bot["id"], FileInput(name="入力.txt", content=content))

        def call(name, args):
            return manager.call(task, name, args, token, approved=True)

        def denied(name, args):
            try:
                call(name, args)
            except DesktopError:
                return
            raise AssertionError("Unsafe transfer was accepted")

        try:
            imported = call("computer_import_file", {"file_id": source["id"]})
            workspace = manager._computers[bot["id"]].executor.workspace
            assert (workspace / "入力.txt").read_bytes() == content.encode()
            assert imported["sha256"] == hashlib.sha256(content.encode()).hexdigest()
            assert "content" not in imported and imported["written"]
            checks.append("large exact Unicode/CRLF/indentation import with receipt")
            denied("computer_import_file", {"file_id": source["id"]})
            assert (workspace / "入力.txt").read_bytes() == content.encode()
            checks.append("existing workspace file is never overwritten")
            command = "python3 - <<'CODE'\nfrom pathlib import Path\nPath('/workspace/result.txt').write_bytes(Path('/workspace/入力.txt').read_bytes()+b'Actual suffix\\n')\nCODE"
            assert call("computer_run", {"command": command})["exit_code"] == 0
            saved = call("computer_save_file", {"name": "result.txt"})
            assert "content" not in saved
            assert (
                store.file("owner", bot["id"], saved["id"])["content"]
                == content + "Actual suffix\n"
            )
            assert store.file("owner", bot["id"], saved["id"])["source_task_id"] == task["id"]
            checks.append("actual command output exported without model transcription")
            assert (
                call(
                    "computer_run",
                    {
                        "command": "ln -s /etc/passwd link.txt; mkdir folder; mkfifo pipe; head -c 2097153 /dev/zero > oversized.txt"
                    },
                )["exit_code"]
                == 0
            )
            for name in ("link.txt", "folder", "pipe", "oversized.txt"):
                denied("computer_save_file", {"name": name})
            assert len(store.list_files("owner", bot["id"])) == 2
            checks.append("symlink/directory/FIFO/oversize exports rejected without hanging")
            assert call("computer_run", {"command": "printf ok"})["output"] == "ok"
            manager.release(task["id"], cancelled=False)
            assert not workspace.exists()
            assert (
                Store(store.path).file("owner", bot["id"], saved["id"])["content"]
                == content + "Actual suffix\n"
            )
            checks.append("saved artifact survives task-end computer cleanup and store reopen")
        finally:
            manager.close()
    print(json.dumps({"passed": True, "checks": checks, "model_calls": 0}))


if __name__ == "__main__":
    main()
