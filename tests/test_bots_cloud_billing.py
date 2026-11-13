import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from test_bots_cloud_desktop import Provider

from alysis_code.bots.cloud_billing import BillingSession, BillingUnavailable, CloudBilling
from alysis_code.bots.cloud_desktop import CloudDesktopFactory
from alysis_code.bots.compute import ComputerSpec

OWNER = "account:" + str(uuid4())
BOT = uuid4().hex


def receipt(**changes):
    return dict(
        schema_version=1,
        credits_per_hour=25,
        free_credits=50,
        free_credits_remaining=50,
        shared_credits_remaining=500,
        storage_limit_bytes=3_000_000_000,
        can_run=True,
        **changes,
    )


def test_ambiguous_billing_reply_retries_same_event_and_keeps_secret_private():
    requests = []

    def respond(req):
        requests.append(json.loads(req.content))
        if len(requests) == 1:
            raise httpx.ReadTimeout("synthetic-secret", request=req)
        return httpx.Response(200, json=receipt())

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        billing = CloudBilling("https://billing.invalid", "synthetic-secret", client=client)
        billing.request(OWNER, "begin", session=str(uuid4()), bot=BOT)
        assert requests[0] == requests[1]
        assert "synthetic-secret" not in repr(billing)


def test_upstream_errors_never_disclose_credentials_or_body():
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(500, text="secret"))
    ) as client:
        billing = CloudBilling("https://billing.invalid", "secret", client=client)
        with pytest.raises(BillingUnavailable) as exc:
            billing.request(OWNER, "status")
        assert "secret" not in str(exc.value)


def test_verified_plan_refresh_updates_storage_without_inferring_plan_from_client():
    limits = []
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=receipt()))
    ) as client:
        billing = CloudBilling("https://billing.invalid", "secret", client=client)
        account = billing.account(
            OWNER, SimpleNamespace(set_library_limit=lambda *args: limits.append(args))
        )
        account.admit()
        assert limits == [(OWNER, 3_000_000_000)]


def test_no_compute_is_created_when_credit_admission_fails(tmp_path):
    class NoCredits:
        def admit(self):
            raise BillingUnavailable("No credits")

    provider = Provider()
    factory = CloudDesktopFactory(
        OWNER, provider, ComputerSpec("image"), tmp_path, billing=NoCredits()
    )
    with pytest.raises(BillingUnavailable):
        factory(BOT, tmp_path, cancel=lambda: False)
    assert provider.allocations == {}


def test_readiness_starts_meter_once_and_cleanup_ends_it(tmp_path):
    calls = []

    class Meter:
        def close(self):
            calls.append("end")

    class Billing:
        def admit(self):
            calls.append("admit")

        def start(self, bot, stopped, *, running):
            assert running()
            assert any(action[-2] == "start" for action in provider.actions)
            calls.append("begin")
            return Meter()

    provider = Provider()
    factory = CloudDesktopFactory(
        OWNER, provider, ComputerSpec("image"), tmp_path, billing=Billing()
    )
    computer = factory(BOT, tmp_path, cancel=lambda: False)
    computer.close()
    assert calls == ["admit", "begin", "end"]
    assert all(state == "missing" for _, state in provider.allocations.values())


@pytest.mark.parametrize("outage", [False, True])
def test_heartbeat_exhaustion_or_outage_requests_provider_pause(outage):
    actions = []

    def request(*args, **kwargs):
        if outage:
            raise BillingUnavailable("unavailable")
        return {"can_run": False}

    session = BillingSession(
        SimpleNamespace(request=request), BOT, lambda: actions.append("pause"), running=lambda: True
    )
    session._stop = SimpleNamespace(wait=lambda _: False, set=lambda: None)
    session._run()
    assert actions == ["pause"]


def test_stop_still_completes_when_final_billing_receipt_is_unavailable():
    def request(*args, **kwargs):
        raise BillingUnavailable("unavailable")

    session = BillingSession(
        SimpleNamespace(request=request), BOT, lambda: None, running=lambda: True
    )
    session.close()
    session.close()
    assert session._ended


def test_provider_sleep_aborts_metering_instead_of_charging_an_alive_local_handle():
    actions = []

    def request(operation, **kwargs):
        actions.append(operation)
        return {"can_run": False}

    session = BillingSession(
        SimpleNamespace(request=request),
        BOT,
        lambda: actions.append("pause"),
        running=lambda: False,
    )
    session._stop = SimpleNamespace(wait=lambda _: False, set=lambda: None)
    session._run()
    assert actions == ["abort", "pause"]


def test_memory_sleep_ends_one_meter_and_resume_begins_another(tmp_path):
    from test_bots_memory_sleep import MemoryProvider

    meters = []

    class Meter:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class Billing:
        def admit(self):
            pass

        def start(self, bot, on_exhausted, *, running):
            assert running()
            meter = Meter()
            meters.append(meter)
            return meter

    provider = MemoryProvider()
    factory = CloudDesktopFactory(
        OWNER, provider, ComputerSpec("image", suspend_mode="Memory"), tmp_path, billing=Billing()
    )
    first = factory(BOT, tmp_path, cancel=lambda: False)
    first.suspend(uuid4().hex)
    assert meters[0].closed
    assert provider.status(first.ref).state == "stopped"
    second = factory(BOT, tmp_path, cancel=lambda: False)
    assert len(meters) == 2 and not meters[1].closed
    second.close()
    assert meters[1].closed


def test_credit_exhaustion_retains_memory_and_fences_tools(tmp_path):
    from test_bots_memory_sleep import MemoryProvider

    provider = MemoryProvider()
    factory = CloudDesktopFactory(
        OWNER, provider, ComputerSpec("image", suspend_mode="Memory"), tmp_path
    )
    surface = factory(BOT, tmp_path, cancel=lambda: False)
    surface._computer._billing_exhausted()
    assert not surface.alive()
    assert provider.status(surface.ref).state == "stopped"
    assert not provider.deleted
    surface.close()
