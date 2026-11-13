"""Controller-only Cloud compute metering. Account credentials never enter a VM.

The billing database owns time, plan conversion, balances and idempotency. Five
second heartbeats bound unfunded work; uncertain intervals and cleanup are
operator costs, never reconstructed from a task's lifetime or a UI screenshot.

Rollout: apply the Cloud billing migration and update the cloud-bots entitlement
function first. Supply billing_url and billing_key through the hosted controller's
private bootstrap, then deploy the controller and desktop. Never put the billing
credential in an account response, desktop configuration or guest environment.
"""

from __future__ import annotations

import math
import threading
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from .desktop_linux import DesktopError


class BillingUnavailable(DesktopError):
    pass


class CloudBilling:
    def __init__(self, url, key, *, client=None):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Use an HTTPS Cloud billing service URL.")
        if not isinstance(key, str) or not key:
            raise ValueError("Cloud billing requires a controller credential.")
        self._url = url.rstrip("/") + "/rest/v1/rpc/cloud_computer_billing"
        self._key = key
        self._client = client or httpx.Client(timeout=5, follow_redirects=False)

    def close(self):
        self._client.close()

    def account(self, owner, store):
        return AccountBilling(self, owner, store)

    def request(self, owner, operation, *, session=None, bot=None):
        try:
            if not owner.startswith("account:"):
                raise ValueError()
            user = str(UUID(owner[8:]))
            body = {"p_user": user, "p_operation": operation}
            if operation != "status":
                body.update(p_session=session, p_event=str(uuid4()), p_bot=bot)
            # Retry the identical event after an ambiguous transport failure.
            # The database returns its original receipt, without another debit.
            for attempt in range(2):
                try:
                    response = self._client.post(
                        self._url,
                        json=body,
                        headers={"apikey": self._key, "Authorization": "Bearer " + self._key},
                    )
                    response.raise_for_status()
                    value = response.json()
                    if value.get("schema_version") != 1 or type(value.get("can_run")) is not bool:
                        raise ValueError()
                    for field in (
                        "free_credits",
                        "free_credits_remaining",
                        "shared_credits_remaining",
                        "storage_limit_bytes",
                    ):
                        if (
                            type(value.get(field)) not in (int, float)
                            or not math.isfinite(value[field])
                            or value[field] < 0
                        ):
                            raise ValueError()
                    return value
                except (httpx.TransportError, httpx.HTTPStatusError):
                    if attempt:
                        raise
        except Exception:
            raise BillingUnavailable(
                "Cloud computer billing could not be confirmed. Your saved work is retained."
            ) from None


class AccountBilling:
    def __init__(self, service, owner, store):
        self.service, self.owner, self.store = service, owner, store
        self._limit = None

    def request(self, operation, **kwargs):
        value = self.service.request(self.owner, operation, **kwargs)
        limit = int(value["storage_limit_bytes"])
        if limit != self._limit:
            try:
                self.store.set_library_limit(self.owner, limit)
            except Exception:
                raise BillingUnavailable(
                    "Cloud storage allowance could not be confirmed."
                ) from None
            self._limit = limit
        return value

    def status(self):
        return self.request("status")

    def admit(self):
        if not self.status()["can_run"]:
            raise BillingUnavailable(
                "No Cloud computer credits are available. Check your plan allowance or wait for its monthly reset."
            )

    def start(self, bot, on_exhausted, *, running):
        session = BillingSession(self, bot, on_exhausted, running=running)
        session.start()
        return session


class BillingSession:
    def __init__(self, account, bot, on_exhausted, *, running):
        self.account, self.bot, self.on_exhausted = account, bot, on_exhausted
        self.id = str(uuid4())
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._ended = False
        self.running = running

    def _confirmed_running(self):
        try:
            return self.running() is True
        except Exception:
            return False

    def start(self):
        if not self.account.request("begin", session=self.id, bot=self.bot)["can_run"]:
            raise BillingUnavailable("No Cloud computer credits are available.")
        self.thread = threading.Thread(target=self._run, name="alysis-cloud-meter", daemon=True)
        self.thread.start()

    def _run(self):
        while not self._stop.wait(5):
            try:
                with self._lock:
                    if self._ended:
                        return
                    confirmed = self._confirmed_running()
                    allowed = self.account.request(
                        "tick" if confirmed else "abort", session=self.id
                    )["can_run"]
            except Exception:
                allowed = False
            if not allowed:
                self._stop.set()
                # No billing lock is held while the provider is stopped.
                self.on_exhausted()
                return

    def close(self):
        self._stop.set()
        with self._lock:
            if self._ended:
                return
            try:
                self.account.request(
                    "end" if self._confirmed_running() else "abort", session=self.id
                )
            except BillingUnavailable:
                # Never delay provider cleanup for a billing outage. The durable
                # receipt retains the last confirmed point; the gap is unbilled.
                pass
            self._ended = True
