"""Provider-free tab cleanup on real Linux Chromium, in an isolated QA container.

Synthetic forms and page allocations exercise resource release and unsaved-work
protection. This does not test whether a model chooses cleanup autonomously.
"""

from __future__ import annotations

import json
import threading
import time
from uuid import uuid4

import bots_browser_pages as fixture
import psutil

from alysis_code.bots.browser import BrowserComputers
from alysis_code.bots.browser_linux import LinuxBrowserComputer
from alysis_code.bots.computer import ComputerViews
from alysis_code.cancellation import EventCancellationToken

fixture.REVIEW = """<!doctype html><meta charset="utf-8"><title>Draft</title>
<label>Draft <input id="note"></label><script>
window.allocation=new Uint8Array(32*1024*1024);window.allocation.fill(17);
window.onbeforeunload=e=>{if(document.querySelector('#note').value){
e.preventDefault();e.returnValue='';}};</script>"""


def main():
    site = fixture.Site()
    views = ComputerViews()
    executors = []
    tracked = {}
    memory = {}
    event = threading.Event()
    token = EventCancellationToken(event)
    task = dict(id="cleanup-task", bot_id="cleanup-bot")

    def factory(bot, cancel):
        computer = LinuxBrowserComputer(bot, cancel, preview_urls=lambda: (site.url + "/",))
        # Exercise the cloud surface's existing completion policy using real
        # local processes. This is not an Azure provisioning test.
        computer.retire_on_release = True
        executors.append(computer)
        return computer

    adapter = BrowserComputers("cleanup", views, factory)

    def call(name, **args):
        return adapter.call(task, name, args, token, approved_origins=(site.url,))

    def inventory(count):
        for _ in range(40):
            result = call("browser_pages")
            if len(result["pages"]) == count:
                return result
            time.sleep(0.1)
        raise AssertionError(result)

    def target(view, identifier):
        return next(
            n["node"]
            for n in view["dom"]["documents"][0]["nodes"]
            if n.get("attributes", {}).get("id") == identifier
        )

    def open_tab(view, count):
        call("browser_click", source_id=view["source_id"], node=target(view, "open"))
        return inventory(count)

    def select(page_id, listing):
        return call("browser_switch_page", source_id=listing["source_id"], page_id=page_id)

    def close(page_id, listing):
        return call("browser_close_page", source_id=listing["source_id"], page_id=page_id)

    def measure(label):
        time.sleep(0.5)
        rss = pss = count = 0
        # The QA container runs only this script and its owned browser trees.
        for process in psutil.Process().children(recursive=True):
            try:
                tracked[process.pid] = process
                info = process.memory_full_info()
                rss += info.rss
                pss += info.pss
                count += 1
            except psutil.NoSuchProcess:
                pass
        memory[label] = dict(
            processes=count, rss_mib=round(rss / 2**20, 2), pss_mib=round(pss / 2**20, 2)
        )

    def assert_reaped():
        for _ in range(50):
            live = [
                p.pid
                for p in tracked.values()
                if p.is_running() and p.status() != psutil.STATUS_ZOMBIE
            ]
            if not live:
                return
            time.sleep(0.1)
        raise AssertionError({"owned_processes_still_running": live})

    try:
        root = call("browser_navigate", url=site.url + "/")
        root_id = inventory(1)["pages"][0]["page_id"]
        measure("one_tab")
        listing = open_tab(root, 2)
        first = next(p["page_id"] for p in listing["pages"] if p["page_id"] != root_id)
        root = select(root_id, listing)
        listing = open_tab(root, 3)
        second = next(
            p["page_id"] for p in listing["pages"] if p["page_id"] not in {root_id, first}
        )
        # Wait for actual page allocations rather than measuring blank popups.
        select(first, listing)
        select(second, inventory(3))
        root = select(root_id, inventory(3))
        measure("three_tabs")
        result = close(first, inventory(3))
        assert result["closed"] and len(result["pages"]) == 2, result
        select(second, result)
        result = close(second, inventory(2))
        assert result["closed"] and len(result["pages"]) == 1, result
        assert not any(p["active"] for p in result["pages"])
        root = select(root_id, result)
        assert root["dom"]["documents"][0]["title"] == "Workshop"
        measure("after_tab_cleanup")

        listing = open_tab(root, 2)
        draft_id = next(p["page_id"] for p in listing["pages"] if p["page_id"] != root_id)
        draft = select(draft_id, listing)
        marker = "Αποθήκευση / 東京 / " + uuid4().hex
        call("browser_type", source_id=draft["source_id"], node=target(draft, "note"), text=marker)
        result = close(draft_id, inventory(2))
        assert not result["closed"] and result["reason"] == "dialog_preserved_page", result
        draft = call("browser_observe")
        assert marker in json.dumps(draft, ensure_ascii=False), "Unsaved draft was lost"
        call("browser_type", source_id=draft["source_id"], node=target(draft, "note"), text="")
        assert close(draft_id, inventory(2))["closed"]
        result = close(root_id, inventory(1))
        assert result["reason"] == "last_page_retained"
        select(root_id, result)
        measure("before_completion")
        adapter.release(task["id"], cancelled=False)
        assert not adapter._computers
        assert_reaped()
        measure("after_completion")

        root = call("browser_navigate", url=site.url + "/")
        root_id = inventory(1)["pages"][0]["page_id"]
        listing = open_tab(root, 2)
        popup_id = next(p["page_id"] for p in listing["pages"] if p["page_id"] != root_id)
        result = close(root_id, listing)
        assert result["closed"], result
        select(popup_id, result)
        root = call("browser_navigate", url=site.url + "/")
        open_tab(root, 2)
        measure("before_stop")
        event.set()
        adapter.reap()
        assert not adapter._computers
        assert_reaped()
        measure("after_stop")
        print(
            json.dumps(
                dict(
                    passed=True,
                    model_calls=0,
                    cloud_calls=0,
                    memory=memory,
                    checks=[
                        "inactive and active tab close",
                        "fresh sibling selection",
                        "Unicode unsaved form retained",
                        "last tab reusable",
                        "completion reaps all owned processes",
                        "Stop reaps all owned processes",
                    ],
                )
            ),
            flush=True,
        )
    finally:
        adapter.close()
        site.close()


if __name__ == "__main__":
    main()
