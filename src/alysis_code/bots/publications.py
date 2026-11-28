"""Explicit, revocable snapshots published by an authenticated person in managed Owls."""

from __future__ import annotations

import html
import re
import time
from typing import Literal
from uuid import UUID, uuid4

from fastapi import Depends, HTTPException
from pydantic import Field, field_validator
from starlette.responses import HTMLResponse

from .models import Input
from .routine_access import website_origin
from .store import Conflict, NotFound


class Publication(Input):
    kind: Literal["conversation", "dashboard"]
    source: str = Field(pattern=r"^[a-f0-9]{32}$")
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=450_000)
    version: int = Field(ge=0)
    revocation_epoch: int = Field(ge=0)
    expected_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    recipients: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("content")
    @classmethod
    def bounded(cls, value):
        if "\x00" in value or len(value.encode("utf-8")) > 450_000:
            raise ValueError("The publication is too large or contains invalid text.")
        return value

    @field_validator("recipients")
    @classmethod
    def addresses(cls, values):
        result = []
        for value in values:
            email = value.strip().lower()
            if len(email) > 254 or not re.fullmatch(r"[^\s@,]+@[^\s@,]+", email):
                raise ValueError("Enter the recipients' Alysis account emails.")
            if email not in result:
                result.append(email)
        return result


class PublicationVersion(Input):
    version: int = Field(ge=1)


class InboxPage(Input):
    before_time: float = Field(ge=0, allow_inf_nan=False)
    before_id: str = Field(pattern=r"^[a-f0-9]{32}$")


def public_origin(value):
    """Only the controller's configured HTTPS origin can appear in a share link."""
    if not isinstance(value, str) or not value.lower().startswith("https://") or "\\" in value:
        return None
    try:
        return website_origin(value)
    except ValueError:
        return None


class Publications:
    def __init__(self, store, owner, recipients, origin):
        self.store, self.owner, self.recipients = store, owner, recipients
        self.origin = public_origin(origin)

    def summary(self, row):
        result = {k: row[k] for k in ("id", "kind", "title", "version", "updated_at")}
        if row["kind"] == "dashboard" and self.origin:
            result["url"] = f"{self.origin}/published/{row['id']}"
        return result

    def browse(self):
        with self.store.connect() as db:
            epoch = self._epoch(db)
            by_id = {}
            # One statement keeps the recipient list and publication version in the
            # same snapshot even when another controller changes them during a refresh.
            for row in db.execute(
                "SELECT p.id,p.kind,p.source,p.title,p.version,p.updated_at,r.address "
                "FROM owl_publications p LEFT JOIN owl_publication_readers r ON r.publication=p.id "
                "WHERE p.owner=? ORDER BY p.updated_at DESC,p.id,r.address",
                (self.owner,),
            ):
                if row["id"] not in by_id:
                    by_id[row["id"]] = dict(self.summary(row), source=row["source"])
                if row["address"] is not None:
                    by_id[row["id"]].setdefault("recipients", []).append(row["address"])
        return {
            "sent": list(by_id.values()),
            **self.inbox(),
            "can_publish": bool(self.origin),
            "revocation_epoch": epoch,
        }

    def _epoch(self, db):
        row = db.execute(
            "SELECT revocation_epoch FROM owl_publication_owners WHERE owner=?", (self.owner,)
        ).fetchone()
        return row[0] if row else 0

    def inbox(self, cursor=None):
        clause = " AND (p.updated_at<? OR (p.updated_at=? AND p.id<?))" if cursor else ""
        args = (
            (self.owner, cursor.before_time, cursor.before_time, cursor.before_id)
            if cursor
            else (self.owner,)
        )
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT p.id,p.kind,p.title,p.version,p.updated_at FROM owl_publications p "
                "JOIN owl_publication_readers r ON r.publication=p.id WHERE r.account=?"
                + clause
                + " ORDER BY p.updated_at DESC,p.id DESC LIMIT 51",
                args,
            ).fetchall()
        items = [self.summary(row) for row in rows[:50]]
        return {
            "received": items,
            "next": {"before_time": items[-1]["updated_at"], "before_id": items[-1]["id"]}
            if len(rows) > 50
            else None,
        }

    def _lock_owner(self, db):
        # All mutations serialize per sender so quota, compare-and-swap and revocation
        # also hold across independent controllers connected to PostgreSQL.
        db.execute(
            "INSERT INTO owl_publication_owners(owner) VALUES(?) ON CONFLICT(owner) DO NOTHING",
            (self.owner,),
        )
        db.execute("UPDATE owl_publication_owners SET owner=owner WHERE owner=?", (self.owner,))

    def save(self, data):
        if data.kind == "dashboard":
            if not self.origin:
                raise HTTPException(503, "Dashboard publishing is unavailable on this host.")
            if data.recipients:
                raise HTTPException(422, "A public dashboard does not accept private recipients.")
            readers = {}
        else:
            if not data.recipients:
                raise HTTPException(422, "Choose at least one recipient.")
            # The account service returns immutable verified account IDs. Later email changes
            # cannot transfer an old invitation to a different person.
            readers = self.recipients(data.recipients)
            if len(readers) != len(data.recipients):
                raise HTTPException(422, "One or more recipients could not be added.")
            readers = {
                "account:" + str(UUID(account)): address
                for account, address in zip(readers, data.recipients, strict=True)
            }
        with self.store.connect(write=True) as db:
            self._lock_owner(db)
            existing = db.execute(
                "SELECT * FROM owl_publications WHERE owner=? AND kind=? AND source=?",
                (self.owner, data.kind, data.source),
            ).fetchone()
            if data.version == 0:
                if data.expected_id or data.revocation_epoch != self._epoch(db):
                    raise Conflict("Sharing has changed. Reopen sharing before publishing.")
            elif not existing or existing["id"] != data.expected_id:
                raise Conflict("This publication was withdrawn. Reopen sharing before saving.")
            if (existing["version"] if existing else 0) != data.version:
                if (
                    existing
                    and existing["version"] == data.version + 1
                    and existing["content"] == data.content
                    and existing["title"] == data.title
                ):
                    previous = {
                        r["account"]: r["address"]
                        for r in db.execute(
                            "SELECT account,address FROM owl_publication_readers WHERE publication=?",
                            (existing["id"],),
                        )
                    }
                    if previous == readers:
                        return self.summary(existing)
                raise Conflict("This publication changed. Reopen sharing before saving.")
            if existing:
                identity = existing["id"]
                db.execute(
                    "UPDATE owl_publications SET title=?,content=?,version=version+1,updated_at=? WHERE id=?",
                    (data.title, data.content, time.time(), identity),
                )
            else:
                if (
                    db.execute(
                        "SELECT COUNT(*) FROM owl_publications WHERE owner=?", (self.owner,)
                    ).fetchone()[0]
                    >= 64
                ):
                    raise Conflict("Remove an older publication before sharing another.")
                identity = uuid4().hex
                db.execute(
                    "INSERT INTO owl_publications(id,owner,source,kind,title,content,version,updated_at) VALUES(?,?,?,?,?,?,1,?)",
                    (
                        identity,
                        self.owner,
                        data.source,
                        data.kind,
                        data.title,
                        data.content,
                        time.time(),
                    ),
                )
            db.execute("DELETE FROM owl_publication_readers WHERE publication=?", (identity,))
            for reader, address in readers.items():
                db.execute(
                    "INSERT INTO owl_publication_readers(publication,account,address) VALUES(?,?,?)",
                    (identity, reader, address),
                )
            return self.summary(
                db.execute("SELECT * FROM owl_publications WHERE id=?", (identity,)).fetchone()
            )

    def receive(self, identity):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT p.* FROM owl_publications p JOIN owl_publication_readers r "
                "ON r.publication=p.id WHERE p.id=? AND r.account=? AND p.kind='conversation'",
                (identity, self.owner),
            ).fetchone()
            if not row:
                raise NotFound("Shared conversation not found.")
            return dict(self.summary(row), content=row["content"])

    def revoke(self, identity, data):
        with self.store.connect(write=True) as db:
            self._lock_owner(db)
            count = db.execute(
                "DELETE FROM owl_publications WHERE id=? AND owner=? AND version=?",
                (identity, self.owner, data.version),
            ).rowcount
            if not count:
                raise Conflict("The publication has already changed.")
            db.execute(
                "UPDATE owl_publication_owners SET revocation_epoch=revocation_epoch+1 WHERE owner=?",
                (self.owner,),
            )
            return {"revoked": True, "revocation_epoch": self._epoch(db)}


def install_account_routes(app, publications=None, *, access=None):
    app.state.owl_sharing = True
    dependency = Depends(access if access is not None else lambda: publications)

    @app.get("/v1/sharing")
    def sharing(service=dependency):
        return service.browse()

    @app.post("/v1/sharing")
    def save(data: Publication, service=dependency):
        return service.save(data)

    @app.post("/v1/sharing/inbox")
    def more(data: InboxPage, service=dependency):
        return service.inbox(data)

    @app.get("/v1/sharing/{identity}")
    def received(identity: str, service=dependency):
        return service.receive(identity)

    @app.post("/v1/sharing/{identity}/revoke")
    def revoke(identity: str, data: PublicationVersion, service=dependency):
        return service.revoke(identity, data)


def install_reader(app, store):
    @app.get("/published/{identity}", include_in_schema=False)
    def published(identity: str):
        if not re.fullmatch(r"[a-f0-9]{32}", identity):
            raise HTTPException(404)
        with store.connect() as db:
            row = db.execute(
                "SELECT title,content FROM owl_publications WHERE id=? AND kind='dashboard'",
                (identity,),
            ).fetchone()
        if not row:
            raise HTTPException(404)
        # No same-origin privilege, outbound requests, top navigation, forms or popups.
        # Both layers have a policy. The outer page contains no executable user markup.
        policy = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'"
        inner = (
            '<meta http-equiv="Content-Security-Policy" content="'
            + html.escape(policy, quote=True)
            + '">'
            + row["content"]
        )
        title = html.escape(row["title"], quote=True)
        page = (
            f'<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>'
            "<style>html,body,iframe{width:100%;height:100%;margin:0;border:0;display:block}</style></head>"
            f'<body><iframe sandbox="allow-scripts" referrerpolicy="no-referrer" title="{title}" srcdoc="{html.escape(inner, quote=True)}"></iframe></body></html>'
        )
        return HTMLResponse(
            page,
            headers={
                "Content-Security-Policy": policy
                + "; frame-src 'self' about:; frame-ancestors 'none'; sandbox allow-scripts",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                "X-Robots-Tag": "noindex, nofollow",
            },
        )
