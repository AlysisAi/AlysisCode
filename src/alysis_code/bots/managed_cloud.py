"""Account-scoped hosted runtimes sharing the existing fenced VM budget.

Device credentials stay encrypted at rest and outside every Bot's workspace.
Only the product's account service grants access, never a desktop label or URL.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import threading
import time
from concurrent.futures import Future
from contextlib import AsyncExitStack, asynccontextmanager

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI
from starlette.responses import JSONResponse

from .cloud_account import ACCOUNT_ID, DEVICE_KEY, CloudAccessError, discover


class AccountAuthority:
    def __init__(self, store, key, *, lookup=discover, clock=time.monotonic):
        self.store, self.lookup, self.clock = store, lookup, clock
        self.cipher = AESGCM(hashlib.sha256(b"alysis-cloud-account-v1\0" + key).digest())
        self.cache = {}
        self.pending = {}
        self.hub_delegations = {}
        self.lock = threading.Lock()

    def access(self, credential, *, allow_upgrade=False):
        if not isinstance(credential, str) or not DEVICE_KEY.fullmatch(credential):
            raise CloudAccessError("sign_in_required")
        digest = hashlib.sha256(credential.encode()).digest()
        # Do not hold a shared lock across a different account's network I/O.
        with self.lock:
            now = self.clock()
            self.cache = {k: v for k, v in self.cache.items() if v[0] > now}
            cached = self.cache.get(digest)
            pending = self.pending.get(digest)
            leader = cached is None and pending is None
            if leader:
                pending = self.pending[digest] = Future()
        if cached is not None:
            value = cached[1]
        elif leader:
            try:
                value = self.lookup(credential)
                if value["status"] == "available" and "storage_limit_bytes" in value:
                    # Refresh capacity with entitlement, including autonomous
                    # work that saves files without waking a computer.
                    self.store.set_library_limit(
                        "account:" + value["account_id"], value["storage_limit_bytes"]
                    )
            except CloudAccessError as exc:
                value = {"status": exc.status}
            except Exception:
                value = {"status": "unavailable"}
            with self.lock:
                self.cache[digest] = (
                    now + 15
                    if value["status"] in ("available", "upgrade_required")
                    else self.clock() + 3,
                    value,
                )
                self.pending.pop(digest)
            pending.set_result(value)
        else:
            # Share only this credential's in-flight verification. An outage
            # must neither authorize stale access nor serialize other accounts.
            try:
                value = pending.result(timeout=10)
            except TimeoutError:
                raise CloudAccessError() from None
        if value["status"] != "available" and not (
            allow_upgrade and value["status"] == "upgrade_required"
        ):
            raise CloudAccessError(value["status"])
        return "account:" + value["account_id"]

    def remember(self, owner, credential):
        with self.store.connect() as db:
            old = db.execute(
                "SELECT credential FROM cloud_accounts WHERE owner=?", (owner,)
            ).fetchone()
        if old and self._open(owner, old[0]) == credential:
            return
        nonce = os.urandom(12)
        sealed = nonce + self.cipher.encrypt(nonce, credential.encode(), owner.encode())
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO cloud_accounts VALUES(?,?,?) ON CONFLICT(owner) DO UPDATE "
                "SET credential=excluded.credential,updated_at=excluded.updated_at",
                (owner, base64.b64encode(sealed).decode(), time.time()),
            )

    def _open(self, owner, sealed):
        data = base64.b64decode(sealed, validate=True)
        return self.cipher.decrypt(data[:12], data[12:], owner.encode()).decode()

    def credential(self, owner):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT credential FROM cloud_accounts WHERE owner=?", (owner,)
            ).fetchone()
        return self._open(owner, row[0]) if row is not None else None

    def allowed(self, owner):
        try:
            key = self.credential(owner)
            return key is not None and self.access(key) == owner
        except Exception:
            # An outage/revocation pauses new claims without replaying a task.
            return False

    def hub_client(self, owner, *, owl_id, timeout_s=20, manage=False):
        from ..hub_apps import HubAppsClient, HubAppsError

        try:
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT credential FROM cloud_accounts WHERE owner=?", (owner,)
                ).fetchone()
            if row is None:
                raise HubAppsError("signed_out")
            identity = (owner, owl_id, manage, hashlib.sha256(row[0].encode()).digest())
            with self.lock:
                entry = self.hub_delegations.get(identity)
            if not entry or entry[0] <= self.clock():
                registered = self.lookup(self._open(owner, row[0]), owl_id=owl_id, manage=manage)
                if (
                    registered.get("status") != "available"
                    or "account:" + registered["account_id"] != owner
                ):
                    raise HubAppsError("signed_out")
                entry = self.clock() + 240, registered["owl_app_access"]
                with self.lock:
                    if len(self.hub_delegations) >= 256:
                        self.hub_delegations.clear()
                    self.hub_delegations[identity] = entry
            return HubAppsClient(entry[1], owl_id=owl_id, timeout_s=timeout_s)
        except CloudAccessError as exc:
            raise HubAppsError(
                "signed_out" if exc.status == "sign_in_required" else "unavailable"
            ) from None

    def owners(self):
        with self.store.connect() as db:
            return [row[0] for row in db.execute("SELECT owner FROM cloud_accounts ORDER BY owner")]

    def publication_recipients(self, owner, emails, *, credential=None):
        import httpx
        from fastapi import HTTPException

        from ..alysis_cloud import owl_share_recipients_url

        if credential is None:
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT credential FROM cloud_accounts WHERE owner=?", (owner,)
                ).fetchone()
            if not row:
                raise HTTPException(401, "Sign in to share a conversation.")
            credential = self._open(owner, row[0])
        try:
            response = httpx.post(
                owl_share_recipients_url(),
                json={"emails": emails},
                headers={"Authorization": "Bearer " + credential},
                timeout=8,
                follow_redirects=False,
            )
            if response.status_code == 422:
                return []
            if response.status_code == 429:
                raise HTTPException(429, "Too many sharing requests. Try again later.")
            response.raise_for_status()
            result = response.json()
            if (
                "account:" + result["owner"] != owner
                or not isinstance(result["accounts"], list)
                or any(
                    not isinstance(v, str) or not ACCOUNT_ID.fullmatch(v)
                    for v in result["accounts"]
                )
            ):
                raise ValueError("Invalid account response")
            return result["accounts"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise HTTPException(503, "Account sharing is temporarily unavailable.") from None


class AccountApplications:
    def __init__(self, operator, authority, factory, *, max_accounts=8, reader_origin=None):
        self.operator, self.authority, self.factory = operator, authority, factory
        self.max_accounts = max_accounts
        self.reader_origin = reader_origin
        self.apps = {}
        self.lock = asyncio.Lock()
        self.resources = AsyncExitStack()

    @property
    def is_running(self):
        return self.operator.state.runtime.is_running and all(
            app.state.runtime.is_running for app in self.apps.values()
        )

    async def account(self, owner, credential=None):
        if owner in self.apps:
            return self.apps[owner]
        async with self.lock:
            if owner not in self.apps:
                if len(self.apps) >= self.max_accounts:
                    raise CloudAccessError()
                if credential is None:
                    credential = await asyncio.to_thread(self.authority.credential, owner)
                app = await asyncio.to_thread(self.factory, owner, credential)
                from functools import partial

                from .publications import Publications, install_account_routes

                install_account_routes(
                    app,
                    access=lambda: Publications(
                        self.authority.store,
                        owner,
                        partial(self.authority.publication_recipients, owner),
                        self.reader_origin,
                    ),
                )
                await self.resources.enter_async_context(app.router.lifespan_context(app))
                self.apps[owner] = app
            return self.apps[owner]

    @asynccontextmanager
    async def lifespan(self, _app):
        async with self.resources:
            await self.resources.enter_async_context(
                self.operator.router.lifespan_context(self.operator)
            )
            for owner in await asyncio.to_thread(self.authority.owners):
                if not owner.startswith("account:") or not ACCOUNT_ID.fullmatch(owner[8:]):
                    raise RuntimeError("Invalid hosted account registration.")
                await self.account(owner)
            yield

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        account_id = scope["path_params"].get("account_id", "")
        owner = "account:" + account_id
        headers = dict(scope.get("headers", []))
        bearer = headers.get(b"authorization", b"").decode("latin1")
        credential = bearer.removeprefix("Bearer ") if bearer.startswith("Bearer ") else ""
        failure = None
        try:
            if not ACCOUNT_ID.fullmatch(account_id):
                raise CloudAccessError("sign_in_required")
            # Native workers have account-bound pairing grants. Their child app
            # still checks the exact route and credential against its own store.
            if credential.startswith("device_"):
                from starlette.routing import get_route_path

                app = self.apps.get(owner)
                if app is None or not get_route_path(scope).startswith("/v1/local-computers/"):
                    raise CloudAccessError("sign_in_required")
                if not await asyncio.to_thread(self.authority.allowed, owner):
                    raise CloudAccessError("upgrade_required")
            else:
                from starlette.routing import get_route_path

                cleanup = (
                    scope["method"] == "POST"
                    and re.fullmatch(r"/v1/tasks/[a-f0-9]{32}/cancel", get_route_path(scope))
                    is not None
                )
                verified = await asyncio.to_thread(
                    self.authority.access, credential, allow_upgrade=cleanup
                )
                if verified != owner:
                    raise CloudAccessError("sign_in_required")
                app = self.apps.get(owner) if cleanup else await self.account(owner, credential)
                if app is None:
                    raise CloudAccessError("upgrade_required")
                await asyncio.to_thread(self.authority.remember, owner, credential)
                headers[b"authorization"] = ("Bearer " + app.state.account_token).encode()
        except CloudAccessError as exc:
            failure = exc.status
        except Exception:
            failure = "unavailable"
        if failure is not None:
            status = {"sign_in_required": 401, "upgrade_required": 403}.get(failure, 503)
            detail = {
                401: "Sign in to Alysis to use Cloud Bots.",
                403: "Upgrade your plan to use Cloud Bots.",
                503: "Cloud Bots is temporarily unavailable. Your saved work is retained.",
            }[status]
            await JSONResponse(
                {"detail": detail, "code": failure},
                status_code=status,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        await app({**scope, "headers": list(headers.items())}, receive, send)

    def application(self):
        app = FastAPI(lifespan=self.lifespan, docs_url=None, redoc_url=None, openapi_url=None)
        from .publications import install_reader, public_origin
        from .sharing_service import sharing_gateway

        if public_origin(self.reader_origin):
            install_reader(app, self.authority.store)
        app.mount("/sharing-api", sharing_gateway(self.authority, self.reader_origin))
        app.mount("/accounts/{account_id}", self)
        app.mount("/", self.operator)
        app.state.runtime = self
        app.state.store = self.operator.state.store
        return app
