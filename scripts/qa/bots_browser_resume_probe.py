"""Provider-free real Chromium draft and attachment recovery qualification."""

import hashlib
import json
import threading
from pathlib import Path
from uuid import uuid4

from bots_browser_cleanup_live import Site

from alysis_code.bots.browser import BrowserComputers
from alysis_code.bots.browser_linux import LinuxBrowserComputer
from alysis_code.bots.clarifications import AskQuestion, ClarificationStore, ReadBrowserDraft
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.store import Store
from alysis_code.cancellation import EventCancellationToken


def main():
    site = Site()
    owner = "draft-probe"
    store = Store(Path("/tmp/probe/bots.db"))
    bot = store.save_bot(
        owner, BotInput(name="Probe", purpose="Synthetic work", files_enabled=True)
    )
    chat = store.create_chat(owner, bot["id"], "Draft")
    store.enqueue(owner, chat["id"], TaskInput(message="Synthetic request", request_id=uuid4().hex))
    task = store.claim(owner)
    token = EventCancellationToken(threading.Event())
    executors = []

    def factory(bot, cancel):
        computer = LinuxBrowserComputer(bot, cancel, preview_urls=lambda: (site.url + "/",))
        computer.retire_on_release = True
        executors.append(computer)
        return computer

    adapter = BrowserComputers(owner, ComputerViews(), factory, store=store)

    def call(tool, **args):
        return adapter.call(task, tool, args, token, approved_origins=(site.url,))

    def node(view, name):
        return next(
            n["node"]
            for n in view["dom"]["documents"][0]["nodes"]
            if n.get("attributes", {}).get("id") == name
        )

    try:
        view = call("browser_navigate", url=site.url + "/draft")
        name = "Νεφέλη / 東京 / " + uuid4().hex
        call("browser_type", source_id=view["source_id"], node=node(view, "name"), text=name)
        questions = ClarificationStore(store, owner)
        q = questions.ask(task, AskQuestion(question="Which rate?"))
        draft = adapter.checkpoint_draft(task)
        assert draft["complete"], draft
        assert name in json.dumps(draft, ensure_ascii=False) and site.draft_note in json.dumps(
            draft, ensure_ascii=False
        )
        questions.save_browser_draft(task, draft)
        adapter.release(task["id"], cancelled=False)
        assert not adapter._computers and not executors[-1].alive()
        recovered = ClarificationStore(Store(store.path), owner).read_browser_draft(
            task, ReadBrowserDraft(question_id=q["id"])
        )
        assert json.loads(recovered["content"]) == draft
        assert not site.saved
        call("browser_navigate", url=site.url + "/package")
        result = call("browser_navigate", url=site.url + "/sales.csv")
        assert result["download_required"] and not result["file_saved"], result
        assert executors[-1].alive()
        observed = call("browser_observe")
        link = next(
            n
            for n in observed["dom"]["documents"][0]["nodes"]
            if n.get("attributes", {}).get("href") == "/sales.csv"
        )
        call(
            "browser_download", source_id=observed["source_id"], node=link["node"], name="sales.csv"
        )
        file = store.list_files(owner, bot["id"])[0]
        assert b"".join(store.file_chunks(owner, bot["id"], file["id"])) == site.ledger.encode()
        adapter.release(task["id"], cancelled=False)
        print(
            json.dumps(
                dict(
                    draft_recovered=True,
                    no_submission=True,
                    actual_attachment_signal=result,
                    source_page_preserved=True,
                    library_sha256=hashlib.sha256(site.ledger.encode()).hexdigest(),
                    files=len(store.list_files(owner, bot["id"])),
                    computers_reaped=not adapter._computers
                    and all(not e.alive() for e in executors),
                )
            )
        )
    finally:
        adapter.close()
        site.close()


if __name__ == "__main__":
    main()
