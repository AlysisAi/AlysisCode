"""The sites an owl's browser holds a sign-in for, read from the browser's own saved storage.

A browser keeps a site's sign-in in that site's cookies and site storage. This reads the
owl's saved browser profile (the archive ``BrowserStates`` keeps) as the browser wrote it: its
cookie databases, per-site storage folders, its record of storage buckets, and its history of
visited pages, which tells a site the owl used apart from one that only set cookies from
inside other pages. Never page text and never rules for particular sites.

Where one site ends comes from the browser too: a cookie set for a whole domain
(``.example.co.uk``) makes that domain one site, because browsers refuse such cookies for
public suffixes. A host no such cookie covers is a site of its own.

Signing out of a site removes its cookies (including cookies other sites keep partitioned
under it), its IndexedDB folders and its storage buckets, and nothing of any other site.
Local Storage is one database shared by every site, in the browser's own format, so it is not
edited here; a running browser that supports it (owl computer images that declare the
"site_data" feature) clears the site's storage of every kind itself
(``ManagedBrowserService.clear_site_data``).
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
import tarfile
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from .models import Input
from .store import Conflict
from .workspace_archive import (
    MAX_ARCHIVE_BYTES,
    MAX_ENTRIES,
    WorkspaceArchiveError,
    _name,
)

# The browser counts time in microseconds since 1601-01-01 UTC.
_EPOCH_1601 = 11_644_473_600
_INDEXED_DB = re.compile(r"(https?)_(.+)_(\d+)\.indexeddb\.(?:leveldb|blob)")
_BUCKET = re.compile(r"[0-9]{1,18}")
# Cookie databases, in the browser's current and earlier places within a profile.
_COOKIE_FILES = (("Network", "Cookies"), ("Cookies",))
_BUCKET_FILES = (("WebStorage", "QuotaManager"), ("QuotaManager",))
_DELETE_BATCH = 500


class SignInsUnavailable(Conflict):
    """The saved browser storage cannot be read or changed safely; nothing was changed."""


class SignOutInput(Input):
    """Sign the owl out of one site (its ``site`` from the list) or out of every site."""

    site: str | None = Field(default=None, min_length=1, max_length=253)
    everywhere: bool = False

    @model_validator(mode="after")
    def one_choice(self):
        if (self.site is None) == (self.everywhere is False):
            raise ValueError("Give either a site or everywhere.")
        return self


def unix_time(value) -> float | None:
    if type(value) is not int or value <= 0:
        return None
    return value / 1_000_000 - _EPOCH_1601


def browser_time(seconds: float) -> int:
    return int((seconds + _EPOCH_1601) * 1_000_000)


def normal_host(value) -> str:
    """A host as the browser stores it (ASCII), without a domain cookie's leading dot."""
    return str(value or "").strip().strip(".").lower()


def display_name(host: str) -> str:
    """The domain as people read it: internationalized labels in their own script."""
    labels = []
    for label in host.split("."):
        if label.startswith("xn--"):
            try:
                label = label[4:].encode("ascii").decode("punycode")
            except (UnicodeError, ValueError):
                return host
        labels.append(label)
    return ".".join(labels)


def _partition_host(value) -> str:
    """The top-level site a partitioned cookie belongs to, from its partition key."""
    text = str(value or "").split("^", 1)[0]
    if not text:
        return ""
    try:
        return normal_host(urlsplit(text).hostname)
    except ValueError:
        return ""


class Sites:
    """Groups hosts into the sites the browser itself draws from its domain cookies."""

    def __init__(self, domains):
        # Broadest first, so every host of a site maps to the same, widest domain. A dotless
        # domain is never a boundary: it could only group unrelated sites together.
        self.domains = sorted(
            {domain for domain in domains if "." in domain}, key=lambda d: (d.count("."), d)
        )

    def of(self, host: str) -> str:
        for domain in self.domains:
            if host == domain or host.endswith("." + domain):
                return domain
        return host


@dataclass
class _Site:
    hosts: set = field(default_factory=set)
    lasting: bool = False
    visited: bool = False
    last_used: float | None = None

    def used(self, when):
        if when is not None and (self.last_used is None or when > self.last_used):
            self.last_used = when


class Profile:
    """A browser profile archive held in memory: readable, and rewritable without the parts
    removed. Bounded like every saved workspace archive."""

    def __init__(self, archive: bytes):
        if not isinstance(archive, bytes | bytearray) or len(archive) > MAX_ARCHIVE_BYTES:
            raise SignInsUnavailable("The saved browser storage is not a readable archive.")
        self.members: list[tuple[tarfile.TarInfo, bytes | None]] = []
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
                for member in tar:
                    _name(member.name)
                    if len(self.members) >= MAX_ENTRIES:
                        raise SignInsUnavailable("The saved browser storage has too many entries.")
                    if member.isdir():
                        self.members.append((member, None))
                    elif member.isreg():
                        payload = tar.extractfile(member)
                        self.members.append((member, payload.read() if payload else b""))
                    else:
                        raise SignInsUnavailable(
                            "The saved browser storage has unsupported entries."
                        )
        except (WorkspaceArchiveError, tarfile.TarError, EOFError, OSError):
            raise SignInsUnavailable(
                "The saved browser storage is not a readable archive."
            ) from None
        self.files = {member.name: data for member, data in self.members if data is not None}
        self.changed: dict[str, bytes] = {}
        self.removed: set[str] = set()

    def paths(self, parts) -> list[str]:
        """Files at ``<profile>/<parts...>`` in every browser profile of the archive."""
        return sorted(
            name
            for name in self.files
            if len(name.split("/")) == len(parts) + 1 and tuple(name.split("/")[1:]) == parts
        )

    def database(self, path) -> sqlite3.Connection:
        # A clean shutdown leaves no pending journal. One that is not empty means the database
        # file alone is not the browser's last state, so it is neither read nor changed.
        if any(self.files.get(path + suffix) for suffix in ("-journal", "-wal")):
            raise SignInsUnavailable("The saved browser storage was not closed cleanly.")
        db = sqlite3.connect(":memory:")
        try:
            db.deserialize(self.files[path])
            db.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.Error:
            db.close()
            raise SignInsUnavailable("The saved browser storage could not be read.") from None
        return db

    @staticmethod
    def columns(db, table) -> set[str]:
        return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}

    def save(self, path, db):
        db.commit()
        db.execute("VACUUM")
        self.changed[path] = db.serialize()

    def remove(self, prefix):
        self.removed.add(prefix)

    def archive(self) -> bytes:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for member, data in self.members:
                if any(
                    member.name == prefix or member.name.startswith(prefix + "/")
                    for prefix in self.removed
                ):
                    continue
                info = tarfile.TarInfo(member.name)
                info.type, info.mode, info.mtime = member.type, member.mode, member.mtime
                if data is None:
                    tar.addfile(info)
                    continue
                data = self.changed.get(member.name, data)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        result = output.getvalue()
        if len(result) > MAX_ARCHIVE_BYTES:
            raise SignInsUnavailable("The changed browser storage exceeds its allowance.")
        return result

    # What the browser holds, per host.

    def cookies(self):
        """(database path, rowid, host, partition host, lasting, last access) per cookie."""
        now = browser_time(time.time())
        found = []
        for path in [p for parts in _COOKIE_FILES for p in self.paths(parts)]:
            with closing(self.database(path)) as db:
                columns = self.columns(db, "cookies")
                if "host_key" not in columns:
                    continue
                pick = [
                    name if name in columns else "NULL"
                    for name in (
                        "top_frame_site_key",
                        "is_persistent",
                        "has_expires",
                        "expires_utc",
                        "last_access_utc",
                    )
                ]
                for row in db.execute(f"SELECT rowid,host_key,{','.join(pick)} FROM cookies"):
                    rowid, host_key, partition, persistent, has_expires, expires, access = row
                    # Cookies without an expiry end with the browser session; expired ones are
                    # gone when the browser next reads them. Neither keeps anyone signed in.
                    persistent = bool(has_expires if persistent is None else persistent)
                    lasting = persistent and not (
                        type(expires) is int and 0 < expires <= now and has_expires != 0
                    )
                    found.append(
                        (
                            path,
                            rowid,
                            str(host_key or ""),
                            _partition_host(partition),
                            lasting,
                            unix_time(access),
                        )
                    )
        return found

    def indexed_db(self):
        """(folder, host) for every IndexedDB folder of a web origin."""
        found = []
        for name in {
            "/".join(name.split("/")[:3])
            for name in self.files
            if len(name.split("/")) >= 3 and name.split("/")[1] == "IndexedDB"
        }:
            match = _INDEXED_DB.fullmatch(name.split("/")[2])
            if match:
                found.append((name, normal_host(match.group(2))))
        return sorted(found)

    def buckets(self):
        """(database path, bucket id, host, last access) for every storage bucket on record."""
        found = []
        for path in [p for parts in _BUCKET_FILES for p in self.paths(parts)]:
            with closing(self.database(path)) as db:
                columns = self.columns(db, "buckets")
                if not {"id", "host"} <= columns:
                    continue
                access = "last_accessed" if "last_accessed" in columns else "NULL"
                for bucket, host, accessed in db.execute(f"SELECT id,host,{access} FROM buckets"):
                    found.append((path, bucket, normal_host(host), unix_time(accessed)))
        return found

    def visits(self):
        """Hosts of pages opened in the browser, with when; None when it keeps no history."""
        paths = self.paths(("History",))
        if not paths:
            return None
        found = []
        for path in paths:
            with closing(self.database(path)) as db:
                columns = self.columns(db, "urls")
                if "url" not in columns:
                    continue
                visited = "last_visit_time" if "last_visit_time" in columns else "NULL"
                for url, when in db.execute(f"SELECT url,{visited} FROM urls"):
                    try:
                        parts = urlsplit(str(url))
                        host = normal_host(parts.hostname)
                    except ValueError:
                        continue
                    if parts.scheme in {"http", "https"} and host:
                        found.append((host, unix_time(when)))
        return found

    def sites(self, cookies=None) -> Sites:
        cookies = self.cookies() if cookies is None else cookies
        return Sites(normal_host(host) for _, _, host, _, _, _ in cookies if host.startswith("."))


def sign_ins(archive: bytes) -> list[dict]:
    """The sites the saved browser holds a lasting sign-in for: cookies that outlive the
    browser session, or site storage, on a site the browser opened pages of. Newest first."""
    profile = Profile(archive)
    cookies = profile.cookies()
    grouping = profile.sites(cookies)
    sites: dict[str, _Site] = {}

    def site(host) -> _Site:
        return sites.setdefault(grouping.of(host), _Site())

    for _, _, host, partition, lasting, access in cookies:
        host = normal_host(host)
        if not host:
            continue
        entry = site(host)
        entry.hosts.add(host)
        # A cookie kept for another top-level site is that site's, not a sign-in here.
        if lasting and (not partition or grouping.of(partition) == grouping.of(host)):
            entry.lasting = True
            entry.used(access)
    for _, host in profile.indexed_db():
        entry = site(host)
        entry.hosts.add(host)
        entry.lasting = True
    for _, _, host, accessed in profile.buckets():
        if host:
            entry = site(host)
            entry.hosts.add(host)
            entry.lasting = True
            entry.used(accessed)
    visits = profile.visits()
    for host, when in visits or ():
        if grouping.of(host) in sites:
            sites[grouping.of(host)].visited = True
            sites[grouping.of(host)].used(when)
    listed = [
        {"site": name, "name": display_name(name), "last_used_at": entry.last_used}
        for name, entry in sites.items()
        if entry.lasting and (visits is None or entry.visited)
    ]
    listed.sort(key=lambda item: (-(item["last_used_at"] or 0), item["name"]))
    return listed


def remove_site(archive: bytes, site: str) -> bytes | None:
    """The archive without one site's cookies and site storage, or None when it holds none."""
    site = normal_host(site)
    if not site:
        raise ValueError("A site is required.")
    profile = Profile(archive)
    cookies = profile.cookies()
    grouping = profile.sites(cookies)
    doomed: dict[str, list[int]] = {}
    for path, rowid, host, partition, _, _ in cookies:
        if grouping.of(normal_host(host)) == site or (partition and grouping.of(partition) == site):
            doomed.setdefault(path, []).append(rowid)
    for path, rowids in doomed.items():
        with closing(profile.database(path)) as db:
            # Removed values must not stay readable in the database's free pages.
            db.execute("PRAGMA secure_delete=ON")
            for start in range(0, len(rowids), _DELETE_BATCH):
                batch = rowids[start : start + _DELETE_BATCH]
                db.execute(
                    f"DELETE FROM cookies WHERE rowid IN ({','.join('?' * len(batch))})", batch
                )
            profile.save(path, db)
    for folder, host in profile.indexed_db():
        if grouping.of(host) == site:
            profile.remove(folder)
    buckets: dict[str, list] = {}
    for path, bucket, host, _ in profile.buckets():
        if host and grouping.of(host) == site:
            buckets.setdefault(path, []).append(bucket)
    for path, ids in buckets.items():
        with closing(profile.database(path)) as db:
            db.execute("PRAGMA secure_delete=ON")
            db.executemany("DELETE FROM buckets WHERE id=?", [(bucket,) for bucket in ids])
            profile.save(path, db)
        # In WebStorage, each bucket's data sits in a folder named by its id.
        folder = path.rsplit("/", 1)[0]
        if folder.endswith("/WebStorage"):
            for bucket in ids:
                if _BUCKET.fullmatch(str(bucket)):
                    profile.remove(f"{folder}/{bucket}")
    if not profile.changed and not profile.removed:
        return None
    return profile.archive()


def site_hosts(archive: bytes, site: str | None) -> set[str]:
    """Every host the saved browser knows for a site (all of them when ``site`` is None): from
    its cookies, its site storage and the pages it opened."""
    profile = Profile(archive)
    cookies = profile.cookies()
    grouping = profile.sites(cookies)
    hosts = {normal_host(host) for _, _, host, _, _, _ in cookies}
    hosts |= {host for _, host in profile.indexed_db()}
    hosts |= {host for _, _, host, _ in profile.buckets()}
    hosts |= {host for host, _ in profile.visits() or ()}
    return {host for host in hosts if host and (site is None or grouping.of(host) == site)}


def live_sign_out(cookies: list[dict], site: str | None) -> tuple[list[dict], set[str]]:
    """Which of a running browser's cookies (identities, as ``site_cookies`` lists them) belong
    to a site, or all of them when ``site`` is None, and the site's hosts among them. A cookie
    another site keeps partitioned under this one goes too, but its host is not this site's."""
    grouping = Sites(
        normal_host(cookie["domain"]) for cookie in cookies if cookie["domain"].startswith(".")
    )
    doomed, hosts = [], set()
    for cookie in cookies:
        host = normal_host(cookie["domain"])
        partition = cookie.get("partition")
        top = partition.get("topLevelSite") if isinstance(partition, dict) else partition
        if site is None or grouping.of(host) == site:
            doomed.append(cookie)
            hosts.add(host)
        elif top and grouping.of(_partition_host(top)) == site:
            doomed.append(cookie)
    return doomed, {host for host in hosts if host}


# Site data a running browser clears per request: few enough, and small enough encoded, to
# stay well within one request to the owl's computer.
_LIVE_BATCH = 100
_LIVE_BATCH_BYTES = 24_000


def _batches(items):
    batch, size = [], 0
    for item in items:
        cost = len(json.dumps(item, ensure_ascii=False).encode()) + 2
        if batch and (len(batch) >= _LIVE_BATCH or size + cost > _LIVE_BATCH_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += cost
    if batch:
        yield batch


class SignIns:
    """An owl's signed-in sites, from the browser storage its next browser starts from, and
    signing it out of them. The owl's computer stays closed while its storage changes."""

    def __init__(self, store, owner, computers):
        if getattr(computers, "browser_states", None) is None:
            raise ValueError("Sign-ins need the owl's saved browser storage.")
        self.store, self.owner, self.computers = store, owner, computers
        self.states = computers.browser_states
        self._lock = threading.Lock()
        self._listed: dict[str, tuple[object, list[dict]]] = {}

    def _sites(self, bot_id, ref) -> list[dict]:
        if ref is None:
            return []
        with self._lock:
            known = self._listed.get(bot_id)
        if known is not None and known[0] == ref:
            return known[1]
        sites = sign_ins(self.states.read(self.owner, bot_id, ref))
        with self._lock:
            self._listed[bot_id] = (ref, sites)
        return sites

    def list(self, bot_id) -> dict:
        ref = self.states.saved(self.owner, bot_id)
        return {
            "sites": self._sites(bot_id, ref),
            # The list is what the owl's browser saved when it last closed.
            "browser_open": self.computers.computer_open(bot_id),
        }

    def sign_out(self, bot_id, site=None, *, cancel=lambda: False) -> dict:
        """Remove one site's sign-in, or every sign-in when ``site`` is None."""
        self.store.bot(self.owner, bot_id)
        with self.computers.closed_computer(bot_id) as open_now:
            if open_now:
                live = self.computers.live_browser(bot_id)
                if live is None:
                    # Its browser holds newer storage and saves it when it closes, which would
                    # bring the sign-in back. Nothing is changed.
                    return {"signed_out": False, "reason": "computer_in_use", **self.list(bot_id)}
                # Signed out where it runs now; the saved copies below keep it signed out if
                # its work then stops or fails.
                self._sign_out_live(live, bot_id, site, cancel)
            if site is None:
                self.states.clear(self.owner, bot_id)
            else:
                self.states.edit(
                    self.owner, bot_id, lambda data: remove_site(data, site), cancel=cancel
                )
            return {"signed_out": True, **self.list(bot_id)}

    def _sign_out_live(self, live, bot_id, site, cancel):
        ref = self.states.saved(self.owner, bot_id)
        known = (
            site_hosts(self.states.read(self.owner, bot_id, ref, cancel=cancel), site)
            if ref is not None
            else set()
        )
        cookies = live.site_data({"method": "site_cookies"}, cancel=cancel)["cookies"]
        doomed, hosts = live_sign_out(cookies, site)
        origins = sorted(
            f"{scheme}://{host}" for host in hosts | known for scheme in ("https", "http")
        )
        for batch in _batches(doomed):
            live.site_data(
                {"method": "clear_site_data", "cookies": batch, "origins": []}, cancel=cancel
            )
        for batch in _batches(origins):
            live.site_data(
                {"method": "clear_site_data", "cookies": [], "origins": batch}, cancel=cancel
            )
