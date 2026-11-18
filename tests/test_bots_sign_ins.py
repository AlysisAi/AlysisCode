"""An owl's signed-in sites come from its browser's own saved storage, and signing out of one
site removes that site's sign-in and nothing else."""

import io
import json
import os
import sqlite3
import tarfile
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from alysis_code.bots.browser_sign_ins import (
    SignIns,
    SignInsUnavailable,
    browser_time,
    display_name,
    live_sign_out,
    remove_site,
    sign_ins,
    site_hosts,
)
from alysis_code.bots.browser_state import BrowserStates
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.store import Store
from alysis_code.bots.workspace_archive import MAX_ARCHIVE_BYTES, restore_workspace
from alysis_code.bots.workspace_objects import WorkspaceObjects

GREEK = "παράδειγμα.δοκιμή".encode("idna").decode()
JAPANESE = "例え.テスト".encode("idna").decode()
NOW = time.time()
DAY = 86400.0

COOKIE_COLUMNS = (
    "creation_utc INTEGER NOT NULL,host_key TEXT NOT NULL,top_frame_site_key TEXT NOT NULL,"
    "name TEXT NOT NULL,value TEXT NOT NULL,encrypted_value BLOB NOT NULL,path TEXT NOT NULL,"
    "expires_utc INTEGER NOT NULL,is_secure INTEGER NOT NULL,is_httponly INTEGER NOT NULL,"
    "last_access_utc INTEGER NOT NULL,has_expires INTEGER NOT NULL,"
    "is_persistent INTEGER NOT NULL,priority INTEGER NOT NULL,samesite INTEGER NOT NULL,"
    "source_scheme INTEGER NOT NULL,source_port INTEGER NOT NULL,"
    "last_update_utc INTEGER NOT NULL"
)


def database(build) -> bytes:
    db = sqlite3.connect(":memory:")
    build(db)
    db.commit()
    data = db.serialize()
    db.close()
    return data


def cookie(
    host, name, *, used=NOW, expires=NOW + 30 * DAY, persistent=True, partition="", value=None
):
    return (
        browser_time(used - DAY),
        host,
        partition,
        name,
        "",
        (value or f"secret-{name}-{host}").encode(),
        "/",
        browser_time(expires) if persistent else 0,
        1,
        1,
        browser_time(used),
        int(persistent),
        int(persistent),
        1,
        0,
        2,
        443,
        browser_time(used),
    )


def cookies_db(rows) -> bytes:
    def build(db):
        db.execute(f"CREATE TABLE cookies({COOKIE_COLUMNS})")
        db.executemany(f"INSERT INTO cookies VALUES({','.join('?' * 18)})", rows)

    return database(build)


def history_db(urls) -> bytes:
    def build(db):
        db.execute(
            "CREATE TABLE urls(id INTEGER PRIMARY KEY,url LONGVARCHAR,title LONGVARCHAR,"
            "visit_count INTEGER DEFAULT 0,typed_count INTEGER DEFAULT 0,"
            "last_visit_time INTEGER NOT NULL,hidden INTEGER DEFAULT 0)"
        )
        db.executemany(
            "INSERT INTO urls(url,title,last_visit_time) VALUES(?,?,?)",
            [(url, "", browser_time(when)) for url, when in urls],
        )

    return database(build)


def buckets_db(rows) -> bytes:
    def build(db):
        db.execute(
            "CREATE TABLE buckets(id INTEGER PRIMARY KEY,storage_key TEXT NOT NULL,"
            "host TEXT NOT NULL,type INTEGER NOT NULL,name TEXT NOT NULL,"
            "use_count INTEGER NOT NULL,last_accessed INTEGER NOT NULL,"
            "last_modified INTEGER NOT NULL,expiration INTEGER NOT NULL,"
            "quota INTEGER NOT NULL,persistent INTEGER NOT NULL,durability INTEGER NOT NULL)"
        )
        db.executemany(
            "INSERT INTO buckets VALUES(?,?,?,0,'default',1,?,?,0,0,0,0)",
            [
                (bucket, f"https://{host}/", host, browser_time(when), browser_time(when))
                for bucket, host, when in rows
            ],
        )

    return database(build)


def archive(files: dict[str, bytes]) -> bytes:
    """A profile archive as the owl's computer packs it: directories, then regular files."""
    output = io.BytesIO()
    directories = sorted(
        {"/".join(name.split("/")[:i]) for name in files for i in range(1, name.count("/") + 1)}
    )
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in directories:
            info = tarfile.TarInfo(name)
            info.type, info.mode = tarfile.DIRTYPE, 0o700
            tar.addfile(info)
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o600
            tar.addfile(info, io.BytesIO(data))
    return output.getvalue()


def members(data: bytes) -> dict[str, bytes | None]:
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        return {
            m.name: (tar.extractfile(m).read() if m.isreg() else None) for m in tar.getmembers()
        }


def cookie_hosts(data: bytes) -> list[str]:
    db = sqlite3.connect(":memory:")
    db.deserialize(members(data)["Default/Network/Cookies"])
    return sorted(row[0] for row in db.execute("SELECT host_key FROM cookies"))


def profile(**extra) -> bytes:
    cookies = [
        # One site across hosts: a whole-domain cookie draws the site's edge.
        cookie(".example.co.uk", "sid", used=NOW - DAY),
        cookie("accounts.example.co.uk", "login", used=NOW - 2 * DAY),
        cookie(f".{GREEK}", "σύνδεση", used=NOW - 3 * DAY),
        cookie(JAPANESE, "session", used=NOW - 4 * DAY),
        # Set from inside other pages and never opened: not a sign-in to list.
        cookie(".tracker.example", "uid"),
        # Ends with the browser session, or already ended: nothing that lasts.
        cookie("session-only.example", "tmp", persistent=False),
        cookie(".expired.example", "old", expires=NOW - DAY),
        # Kept for example.co.uk when another site's page was embedded there.
        cookie("widget.example", "pref", partition="https://example.co.uk"),
    ]
    files = {
        "Local State": b"{}",
        "Default/Preferences": b"{}",
        "Default/Network/Cookies": cookies_db(cookies),
        "Default/Network/Cookies-journal": b"",
        "Default/History": history_db(
            [
                ("https://www.example.co.uk/inbox", NOW - DAY / 2),
                (f"https://{GREEK}/", NOW - 3 * DAY),
                (f"https://{JAPANESE}/home", NOW - 4 * DAY),
                ("https://session-only.example/", NOW),
                ("https://expired.example/", NOW),
                ("https://notes.example/", NOW - 5 * DAY),
                ("https://widget.example/", NOW - 6 * DAY),
            ]
        ),
        # A site that keeps its sign-in in IndexedDB only.
        "Default/IndexedDB/https_notes.example_0.indexeddb.leveldb/CURRENT": b"MANIFEST-1\n",
        "Default/IndexedDB/https_notes.example_0.indexeddb.leveldb/000003.log": b"token",
        f"Default/IndexedDB/https_{GREEK}_0.indexeddb.leveldb/CURRENT": b"MANIFEST-1\n",
        "Default/WebStorage/QuotaManager": buckets_db(
            [(7, "accounts.example.co.uk", NOW - DAY / 4), (8, JAPANESE, NOW - 9 * DAY)]
        ),
        "Default/WebStorage/7/IndexedDB/indexeddb.leveldb/CURRENT": b"MANIFEST-1\n",
        "Default/WebStorage/8/IndexedDB/indexeddb.leveldb/CURRENT": b"MANIFEST-1\n",
        "Default/Local Storage/leveldb/000003.log": b"shared by every site",
    }
    files.update(extra)
    return archive(files)


def test_lists_sites_with_lasting_sign_ins_from_the_browsers_own_storage():
    listed = sign_ins(profile())
    assert [item["site"] for item in listed] == [
        "example.co.uk",
        GREEK,
        JAPANESE,
        "notes.example",
    ]
    names = {item["site"]: item["name"] for item in listed}
    assert names[GREEK] == "παράδειγμα.δοκιμή"
    assert names[JAPANESE] == "例え.テスト"
    assert names["example.co.uk"] == "example.co.uk"
    used = {item["site"]: item["last_used_at"] for item in listed}
    # The newest of its cookies, storage and visits.
    assert used["example.co.uk"] == pytest.approx(NOW - DAY / 4, abs=1e-3)
    assert used[GREEK] == pytest.approx(NOW - 3 * DAY, abs=1e-3)
    assert used["notes.example"] == pytest.approx(NOW - 5 * DAY, abs=1e-3)


def test_without_a_history_every_site_with_lasting_storage_is_listed():
    data = profile()
    files = {name: value for name, value in members(data).items() if value is not None}
    del files["Default/History"]
    sites = {item["site"] for item in sign_ins(archive(files))}
    assert "tracker.example" in sites
    assert "session-only.example" not in sites and "expired.example" not in sites


def test_display_name_reads_internationalized_labels_and_keeps_other_hosts():
    assert display_name(GREEK) == "παράδειγμα.δοκιμή"
    assert display_name("mail.example.com") == "mail.example.com"
    assert display_name("xn--invalid-!.example") == "xn--invalid-!.example"


def test_signing_out_of_a_site_removes_its_sign_in_and_leaves_other_sites():
    before = profile()
    after = remove_site(before, "example.co.uk")
    assert after is not None
    assert cookie_hosts(after) == sorted(
        [f".{GREEK}", JAPANESE, ".tracker.example", "session-only.example", ".expired.example"]
    )
    files = members(after)
    # Its storage bucket goes with it; other sites' storage stays.
    assert "Default/WebStorage/7/IndexedDB/indexeddb.leveldb/CURRENT" not in files
    assert "Default/WebStorage/7" not in files
    assert "Default/WebStorage/8/IndexedDB/indexeddb.leveldb/CURRENT" in files
    assert "Default/IndexedDB/https_notes.example_0.indexeddb.leveldb/000003.log" in files
    assert files["Default/Local Storage/leveldb/000003.log"] == b"shared by every site"
    assert files["Default/Preferences"] == b"{}"
    db = sqlite3.connect(":memory:")
    db.deserialize(files["Default/WebStorage/QuotaManager"])
    assert [row[0] for row in db.execute("SELECT host FROM buckets")] == [JAPANESE]
    # The removed values are gone from the file, not only from its rows.
    assert b"secret-sid-.example.co.uk" not in after
    assert b"secret-sid-.example.co.uk" in before
    assert [item["site"] for item in sign_ins(after)] == [GREEK, JAPANESE, "notes.example"]


@pytest.mark.parametrize("site", [GREEK, JAPANESE, "notes.example"])
def test_signing_out_of_an_internationalized_or_storage_only_site(site):
    after = remove_site(profile(), site)
    remaining = [item["site"] for item in sign_ins(after)]
    assert site not in remaining
    assert len(remaining) == 3
    files = members(after)
    assert not any(f"https_{site}_0" in name for name in files)


@pytest.mark.skipif(os.name != "posix", reason="The owl's computer restores profiles on Linux.")
def test_the_owls_computer_restores_the_changed_profile(tmp_path):
    changed = tmp_path / "changed.tar"
    changed.write_bytes(remove_site(profile(), "example.co.uk"))
    target = tmp_path / "profile"
    target.mkdir(mode=0o700)
    restore_workspace(changed, target)
    assert (target / "Default" / "Preferences").read_bytes() == b"{}"
    db = sqlite3.connect(target / "Default" / "Network" / "Cookies")
    assert "accounts.example.co.uk" not in {
        row[0] for row in db.execute("SELECT host_key FROM cookies")
    }


def test_signing_out_of_a_site_the_browser_holds_nothing_for_changes_nothing():
    assert remove_site(profile(), "other.example") is None


def test_storage_that_was_not_closed_cleanly_is_neither_read_nor_changed():
    data = profile(**{"Default/Network/Cookies-journal": b"pending transaction"})
    with pytest.raises(SignInsUnavailable):
        sign_ins(data)
    with pytest.raises(SignInsUnavailable):
        remove_site(data, "example.co.uk")


def test_a_damaged_or_oversized_archive_is_refused():
    with pytest.raises(SignInsUnavailable):
        sign_ins(b"not an archive")
    with pytest.raises(SignInsUnavailable):
        sign_ins(b"\0" * (MAX_ARCHIVE_BYTES + 1))


# Saved storage: what the owl's next browser starts from.


def saved_store(tmp_path):
    store = Store(tmp_path / "bots.db", objects=LocalObjectStorage(tmp_path / "objects"))
    bot = store.save_bot("owner", BotInput(name="Γλαύκα"))
    return store, bot, BrowserStates(store, b"k" * 32)


def finished_task(store, bot, status="completed"):
    chat = store.create_chat("owner", bot["id"], "Chat")
    store.enqueue("owner", chat["id"], TaskInput(message="Work", request_id=uuid4().hex))
    task = store.claim("owner")
    return task, lambda: store_status(store, task, status)


def store_status(store, task, status):
    with store.connect(write=True) as db:
        db.execute("UPDATE tasks SET status=? WHERE id=?", (status, task["id"]))


def test_a_person_signing_in_while_only_viewing_the_computer_is_kept(tmp_path):
    store, bot, states = saved_store(tmp_path)
    session = uuid4().hex
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO bot_computer_control VALUES(?,?,?,'suspending',?)",
            (bot["id"], None, session, time.time()),
        )
    WorkspaceObjects(store).stage_pause(
        "owner",
        bot["id"],
        None,
        session,
        [b"w"],
        1,
        browser_states=states,
        browser_data=profile(),
    )
    assert states.current("owner", bot["id"]) == profile()


def test_a_newer_task_copy_replaces_the_baseline(tmp_path):
    store, bot, states = saved_store(tmp_path)
    old = states.prepare("owner", bot["id"], b"old")
    with store.connect(write=True) as db:
        states.stage_baseline(db, "owner", bot["id"], old)
    assert states.current("owner", bot["id"]) == b"old"
    task, finish = finished_task(store, bot)
    states.stage("owner", bot["id"], task["id"], b"new")
    finish()
    assert states.current("owner", bot["id"]) == b"new"


def test_signing_out_edits_what_new_work_and_each_waiting_task_start_from(tmp_path):
    store, bot, states = saved_store(tmp_path)
    done, finish = finished_task(store, bot)
    states.stage("owner", bot["id"], done["id"], profile())
    finish()
    waiting, _ = finished_task(store, bot)
    states.stage("owner", bot["id"], waiting["id"], profile())
    store_status(store, waiting, "waiting")
    assert states.edit("owner", bot["id"], lambda data: remove_site(data, GREEK))
    for task_id in (None, waiting["id"]):
        saved = states.current("owner", bot["id"], task_id=task_id) if task_id else None
        if task_id is None:
            with store.connect() as db:
                saved = states.read("owner", bot["id"], states._select(db, bot["id"], strict=False))
        assert GREEK not in {item["site"] for item in sign_ins(saved)}
        assert "example.co.uk" in {item["site"] for item in sign_ins(saved)}
    with store.connect() as db:
        # Older copies that still held the sign-in are gone.
        assert db.execute("SELECT count(*) FROM browser_states").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM browser_baselines").fetchone()[0] == 1


class Computers:
    """The owl's computers as sign-ins see them: open or not, held closed, and a running
    browser that can clear site data (a newer owl computer image) or none."""

    def __init__(self, states, open_now=False, live=None):
        self.browser_states, self.open_now, self.held, self.live = states, open_now, [], live

    def computer_open(self, bot_id):
        return self.open_now

    def live_browser(self, bot_id):
        return self.live

    @contextmanager
    def closed_computer(self, bot_id):
        self.held.append(bot_id)
        yield self.open_now


def test_sign_out_waits_for_an_open_computer_and_changes_nothing(tmp_path):
    store, bot, states = saved_store(tmp_path)
    task, finish = finished_task(store, bot)
    states.stage("owner", bot["id"], task["id"], profile())
    finish()
    computers = Computers(states, open_now=True)
    result = SignIns(store, "owner", computers).sign_out(bot["id"], "example.co.uk")
    assert result["signed_out"] is False and result["reason"] == "computer_in_use"
    assert result["browser_open"] is True
    assert "example.co.uk" in {item["site"] for item in result["sites"]}
    assert states.current("owner", bot["id"]) == profile()


def test_sign_out_of_one_site_then_everywhere(tmp_path):
    store, bot, states = saved_store(tmp_path)
    task, finish = finished_task(store, bot)
    states.stage("owner", bot["id"], task["id"], profile())
    finish()
    computers = Computers(states)
    sign = SignIns(store, "owner", computers)
    assert [item["site"] for item in sign.list(bot["id"])["sites"]][0] == "example.co.uk"
    result = sign.sign_out(bot["id"], "example.co.uk")
    assert result["signed_out"] is True
    assert [item["site"] for item in result["sites"]] == [GREEK, JAPANESE, "notes.example"]
    assert computers.held == [bot["id"]]
    result = sign.sign_out(bot["id"])
    assert result == {"signed_out": True, "sites": [], "browser_open": False}
    assert states.current("owner", bot["id"]) is None


LIVE_COOKIES = [
    {"name": "sid", "domain": ".example.co.uk", "path": "/", "partition": None},
    {"name": "login", "domain": "accounts.example.co.uk", "path": "/", "partition": None},
    {"name": "σύνδεση", "domain": f".{GREEK}", "path": "/", "partition": None},
    {
        "name": "pref",
        "domain": "widget.example",
        "path": "/",
        "partition": {"topLevelSite": "https://example.co.uk", "hasCrossSiteAncestor": False},
    },
]


def test_a_running_browser_signs_out_of_a_site_by_the_same_site_edges():
    doomed, hosts = live_sign_out(LIVE_COOKIES, "example.co.uk")
    assert [cookie["name"] for cookie in doomed] == ["sid", "login", "pref"]
    # The partitioned cookie goes, but its own site's storage stays.
    assert hosts == {"example.co.uk", "accounts.example.co.uk"}
    doomed, hosts = live_sign_out(LIVE_COOKIES, GREEK)
    assert [cookie["name"] for cookie in doomed] == ["σύνδεση"] and hosts == {GREEK}
    doomed, hosts = live_sign_out(LIVE_COOKIES, None)
    assert len(doomed) == 4 and "widget.example" in hosts


def test_saved_hosts_of_a_site_include_its_storage_and_visited_pages():
    assert site_hosts(profile(), "example.co.uk") == {
        "example.co.uk",
        "accounts.example.co.uk",
        "www.example.co.uk",
    }
    assert site_hosts(profile(), "notes.example") == {"notes.example"}
    assert "tracker.example" in site_hosts(profile(), None)


class LiveBrowser:
    def __init__(self, cookies):
        self.cookies, self.calls = cookies, []

    def site_data(self, arguments, *, cancel=lambda: False):
        self.calls.append(arguments)
        if arguments["method"] == "site_cookies":
            return {"cookies": self.cookies}
        return {"cookies": len(arguments["cookies"]), "origins": len(arguments["origins"])}


def test_an_open_computer_with_a_newer_browser_signs_out_now_and_in_saved_storage(tmp_path):
    store, bot, states = saved_store(tmp_path)
    task, finish = finished_task(store, bot)
    states.stage("owner", bot["id"], task["id"], profile())
    finish()
    live = LiveBrowser(LIVE_COOKIES * 60)
    result = SignIns(store, "owner", Computers(states, open_now=True, live=live)).sign_out(
        bot["id"], "example.co.uk"
    )
    assert result["signed_out"] is True and result["browser_open"] is True
    assert "example.co.uk" not in {item["site"] for item in result["sites"]}
    assert live.calls[0] == {"method": "site_cookies"}
    cleared = [call for call in live.calls[1:] if call["cookies"]]
    # Each request stays small: the cookies go in batches, then the site's origins.
    assert [len(call["cookies"]) for call in cleared] == [100, 80]
    # Long cookie names make smaller batches: no request outgrows what the computer accepts.
    long = LiveBrowser([{**cookie, "name": "ν" * 2000} for cookie in LIVE_COOKIES * 30])
    SignIns(store, "owner", Computers(states, open_now=True, live=long)).sign_out(
        bot["id"], "example.co.uk"
    )
    sizes = [len(json.dumps(call, ensure_ascii=False).encode()) for call in long.calls[1:]]
    assert sum(len(call["cookies"]) for call in long.calls[1:]) == 90 and max(sizes) < 30_000
    origins = [origin for call in live.calls[1:] for origin in call["origins"]]
    assert sorted(origins) == sorted(
        f"{scheme}://{host}"
        for host in ("example.co.uk", "accounts.example.co.uk", "www.example.co.uk")
        for scheme in ("https", "http")
    )


def test_the_computer_says_whether_it_is_open_and_its_browser_can_sign_out_live(tmp_path):
    from test_bots_cloud_desktop import Provider

    from alysis_code.bots.cloud_desktop import CloudDesktop, CloudDesktopFactory
    from alysis_code.bots.compute import ComputerSpec

    store, bot, _ = saved_store(tmp_path)
    factory = CloudDesktopFactory(
        "owner",
        Provider(),
        ComputerSpec("fixture-image"),
        tmp_path / "leases",
        store=store,
        browser_state_key=b"k" * 32,
    )
    assert factory.computer_open(bot["id"]) is False and factory.live_browser(bot["id"]) is None
    # What a computer's browser can do is what it declared when it started.
    requests = []

    def started(features):
        computer = SimpleNamespace(_browser_started=False, _browser_states=None, _store=None)
        computer._request = lambda operation, arguments, cancel: (
            requests.append(operation) or {"ready": True, **features}
        )
        CloudDesktop.start_browser(computer, (), lambda: False)
        computer._closed = False
        return computer

    older = started({})
    assert older.browser_features == frozenset() and requests == ["browser_start"]
    newer = started({"features": ["site_data", 7]})
    assert newer.browser_features == frozenset({"site_data"})
    factory._instances[bot["id"]] = (older, {"browser"})
    assert factory.computer_open(bot["id"]) is True and factory.live_browser(bot["id"]) is None
    factory._instances[bot["id"]] = (newer, {"browser"})
    assert factory.live_browser(bot["id"]) is newer
    # Held closed, no computer of the owl can start or close meanwhile.
    admitted = []
    with factory.closed_computer(bot["id"]) as open_now:
        assert open_now is True
        worker = threading.Thread(
            target=lambda: admitted.append(
                factory._operations.hold(bot["id"], blocking=False).__enter__()
            )
        )
        worker.start()
        worker.join()
    assert admitted == [False]
    del factory._instances[bot["id"]]
