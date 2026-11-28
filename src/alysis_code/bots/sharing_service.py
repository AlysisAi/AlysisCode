"""Account sharing without provisioning a Cloud Owl. The local service discovers the
first-party controller and authenticates there using the signed-in person's device."""

from __future__ import annotations

import re
import ssl
from functools import partial

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

from .. import alysis_cloud
from ..config import load_persisted_profile_keys
from .cloud_account import ACCOUNT_ID, DEVICE_KEY, CloudAccessError
from .publications import Publications, install_account_routes, public_origin
from .store import Conflict, NotFound


def sharing_location(key, *, lookup=httpx.get):
    if not isinstance(key, str) or not DEVICE_KEY.fullmatch(key):
        raise HTTPException(401, "Sign in to Alysis to use sharing.")
    try:
        response = lookup(
            alysis_cloud.owl_share_recipients_url(),
            headers={"Authorization": "Bearer " + key},
            timeout=8,
            follow_redirects=False,
        )
        if response.status_code == 401:
            raise HTTPException(401, "Sign in to Alysis to use sharing.")
        response.raise_for_status()
        data = response.json()
        origin = public_origin(data["address"])
        cert = data.get("certificate")
        if not origin or not ACCOUNT_ID.fullmatch(data["account"]):
            raise ValueError("Invalid service origin")
        context = ssl.create_default_context()
        if cert:
            if not isinstance(cert, str) or len(cert) > 32768 or "PRIVATE KEY" in cert:
                raise ValueError("Invalid service certificate")
            context = ssl.create_default_context(cadata=cert)
        return origin, context
    except (httpx.HTTPError, ValueError, TypeError, KeyError, ssl.SSLError):
        raise HTTPException(503, "Account sharing is temporarily unavailable.") from None


class RemotePublications:
    def __init__(self, key=None, location=sharing_location, client=httpx.Client):
        self.key = key or (lambda: load_persisted_profile_keys().get(alysis_cloud.PROFILE_KEY))
        self.location, self.client = location, client

    def request(self, method, suffix="", data=None):
        key = self.key()
        origin, tls = self.location(key)
        try:
            with self.client(verify=tls, timeout=15, follow_redirects=False) as client:
                response = client.request(
                    method,
                    origin + "/sharing-api/v1/sharing" + suffix,
                    headers={"Authorization": "Bearer " + key},
                    **({"json": data.model_dump()} if data else {}),
                )
            if response.status_code in (401, 403, 404, 409, 413, 422, 429):
                raise HTTPException(
                    response.status_code, "The sharing request could not be completed."
                )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise HTTPException(503, "Account sharing is temporarily unavailable.") from None

    def browse(self):
        return self.request("GET")

    def inbox(self, data):
        return self.request("POST", "/inbox", data)

    def save(self, data):
        return self.request("POST", data=data)

    def receive(self, identity):
        if not re.fullmatch(r"[a-f0-9]{32}", identity):
            raise NotFound("Shared conversation not found.")
        return self.request("GET", "/" + identity)

    def revoke(self, identity, data):
        if not re.fullmatch(r"[a-f0-9]{32}", identity):
            raise NotFound("Publication not found.")
        return self.request("POST", "/" + identity + "/revoke", data)


def sharing_gateway(authority, reader_origin):
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    def account(request: Request):
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        try:
            owner = authority.access(token, allow_upgrade=True)
        except CloudAccessError as exc:
            raise HTTPException(
                401 if exc.status == "sign_in_required" else 503, "Account sharing is unavailable."
            ) from None

        return Publications(
            authority.store,
            owner,
            partial(authority.publication_recipients, owner, credential=token),
            reader_origin,
        )

    @app.middleware("http")
    async def bounded(request: Request, call_next):
        if request.method == "POST":
            try:
                size = int(request.headers.get("content-length", "-1"))
            except ValueError:
                size = -1
            if not 0 <= size <= 512_000:
                return JSONResponse(
                    {"detail": "Invalid sharing request size."},
                    status_code=411 if size < 0 else 413,
                )
        result = await call_next(request)
        result.headers["Cache-Control"] = "no-store"
        return result

    @app.exception_handler(Conflict)
    async def conflict(_, exc):
        return JSONResponse({"detail": str(exc)}, 409)

    @app.exception_handler(NotFound)
    async def missing(_, exc):
        return JSONResponse({"detail": str(exc)}, 404)

    @app.exception_handler(RequestValidationError)
    async def invalid(_, exc):
        return JSONResponse({"detail": "Invalid publication."}, 422)

    install_account_routes(app, access=account)
    return app
