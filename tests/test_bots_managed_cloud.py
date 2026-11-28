import base64
import io
import json
from contextlib import ExitStack
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.cloud_account import CloudAccessError, desktop_access, discover
from alysis_code.bots.managed_cloud import AccountApplications, AccountAuthority
from alysis_code.bots.store import Store
from alysis_code.hub_apps import HubAppsError


def test_managed_app_clients_exchange_only_their_registered_account_and_owl(tmp_path):
    identity, other = str(uuid4()), str(uuid4())
    owl = "a" * 32
    calls = []
    now = [0]

    def respond(request):
        assert request.headers["authorization"] == "Bearer slk_registered_fixture"
        current = request.headers.get("X-Alysis-Owl-Apps")
        scope = request.headers.get("X-Alysis-Owl-App-Access")
        calls.append((current, scope))
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "status": "available",
                "account_id": identity,
                "plan": "plan_5",
                **({"owl_app_access": "oha1.fixture." + scope} if current else {}),
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        authority = AccountAuthority(
            Store(tmp_path / "accounts.db"),
            b"k" * 32,
            clock=lambda: now[0],
            lookup=lambda key, **opts: discover(key, client=http, **opts),
        )
        owner = authority.access("slk_registered_fixture")
        authority.remember(owner, "slk_registered_fixture")
        assert authority.hub_client(owner, owl_id=owl)._key == "oha1.fixture.run"
        assert authority.hub_client(owner, owl_id=owl)._key == "oha1.fixture.run"
        assert authority.hub_client(owner, owl_id=owl, manage=True)._key == "oha1.fixture.manage"
        assert calls == [(None, None), (owl, "run"), (owl, "manage")]
        authority.remember("account:" + other, "slk_registered_fixture")
        with pytest.raises(HubAppsError):
            authority.hub_client("account:" + other, owl_id=owl)
        now[0] = 241
        assert authority.hub_client(owner, owl_id=owl)._key == "oha1.fixture.run"
        assert calls.count((owl, "run")) == 3


def test_storage_capacity_refreshes_for_autonomous_work_after_plan_downgrade(tmp_path):
    identity, now = str(uuid4()), [0]
    value = {
        "schema_version": 1,
        "status": "available",
        "account_id": identity,
        "plan": "plan_15",
        "storage_limit_bytes": 15_000_000_000,
    }
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=value)))
    store = Store(tmp_path / "accounts.db")
    authority = AccountAuthority(
        store, b"k" * 32, lookup=lambda key: discover(key, client=client), clock=lambda: now[0]
    )
    owner = authority.access("slk_fixture")
    authority.remember(owner, "slk_fixture")
    assert store.library_usage(owner)["limit_bytes"] == 15_000_000_000
    value.update(plan="plan_5", storage_limit_bytes=3_000_000_000)
    now[0] = 16
    assert authority.allowed(owner)
    assert store.library_usage(owner)["limit_bytes"] == 3_000_000_000
    value["storage_limit_bytes"] = -1
    now[0] = 32
    assert not authority.allowed(owner)
    assert store.library_usage(owner)["limit_bytes"] == 3_000_000_000


def test_discovery_seals_credentials_to_native_main_without_printing_plaintext():
    identity, key = str(uuid4()), "slk_fixture_account_credential"
    body = {
        "schema_version": 1,
        "status": "available",
        "account_id": identity,
        "plan": "plan_5",
        "address": "https://cloud.example.test",
    }
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
    keys = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        keys.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    result = desktop_access(
        connect=True,
        stream=io.BytesIO(json.dumps({"public_key": public}).encode()),
        key_loader=lambda: {"alysis": key},
        client=client,
    )
    assert result["status"] == "available"
    assert key not in json.dumps(result)
    plaintext = keys.decrypt(
        base64.b64decode(result["sealed_credential"]),
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    assert plaintext.decode() == key
    assert "sealed_credential" not in desktop_access(
        key_loader=lambda: {"alysis": key}, client=client
    )
    assert desktop_access(key_loader=lambda: {}, client=client)["status"] == "sign_in_required"


@pytest.mark.parametrize(
    "status,body",
    [
        (401, {}),
        (503, {}),
        (
            200,
            {
                "schema_version": 1,
                "status": "available",
                "account_id": str(uuid4()),
                "plan": "alpha",
                "address": "http://unsafe.test",
            },
        ),
    ],
)
def test_discovery_fails_closed_for_revocation_outage_and_unsafe_endpoint(status, body):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body))
    )
    with pytest.raises(CloudAccessError):
        discover("slk_fixture", client=client)


def test_account_http_isolation_upgrade_gate_encrypted_recovery_and_bounded_registration(tmp_path):
    ids = [str(uuid4()) for _ in range(3)]
    credentials = ["slk_fixture_" + str(i) for i in range(3)]
    plans = {
        key: {"status": "available", "account_id": identity, "plan": "alpha"}
        for key, identity in zip(credentials, ids, strict=True)
    }
    now = [0]
    registry_store = Store(tmp_path / "accounts.sqlite3")
    authority = AccountAuthority(
        registry_store, b"k" * 32, lookup=lambda key: plans[key], clock=lambda: now[0]
    )

    def factory(owner, credential=None):
        directory = tmp_path / owner.replace(":", "-")
        settings = Settings(
            database=directory / "bots.sqlite3",
            token="synthetic-" + "t" * 40,
            api_key="fixture",
            base_url="https://example.test/v1",
            model="fixture",
            owner=owner,
        )
        app = create_app(
            settings,
            client_factory=lambda _: pytest.fail("No model calls expected"),
            work_authorizer=lambda: authority.allowed(owner),
        )
        app.state.account_token = settings.token
        return app

    manager = AccountApplications(factory("operator"), authority, factory, max_accounts=2)

    def auth(i):
        return {"Authorization": "Bearer " + credentials[i]}

    def base(i):
        return "/accounts/" + ids[i] + "/v1/bots"

    with TestClient(manager.application()) as client:
        made = []
        for i in range(2):
            response = client.post(
                base(i),
                headers=auth(i),
                json={"name": f"Bot {i} Αθήνα 東京", "purpose": "Saved work"},
            )
            assert response.status_code == 201, response.text
            made.append(response.json())
        for i in range(2):
            assert [v["id"] for v in client.get(base(i), headers=auth(i)).json()] == [made[i]["id"]]
            assert (
                client.get(
                    base(i) + "/" + made[1 - i]["id"] + "/chats", headers=auth(i)
                ).status_code
                == 404
            )
        assert client.get(base(1), headers=auth(0)).status_code == 401
        assert (
            client.get(base(0), headers={"Authorization": "Bearer device_" + "a" * 64}).status_code
            == 401
        )
        assert client.get(base(2), headers=auth(2)).status_code == 503
        with registry_store.connect() as db:
            sealed = db.execute("SELECT credential FROM cloud_accounts ORDER BY owner").fetchall()
        assert all(not any(key in row[0] for key in credentials) for row in sealed)
        plans[credentials[0]] = {"status": "upgrade_required", "account_id": ids[0]}
        now[0] = 16
        assert client.get(base(0), headers=auth(0)).status_code == 403
        assert (
            client.post(
                "/accounts/" + ids[0] + "/v1/tasks/" + "f" * 32 + "/cancel", headers=auth(0)
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/accounts/" + ids[1] + "/v1/tasks/" + "f" * 32 + "/cancel", headers=auth(0)
            ).status_code
            == 401
        )
        assert not authority.allowed("account:" + ids[0])
        assert client.get(base(1), headers=auth(1)).status_code == 200
    # Real app lifespan/SQLite workers recover registrations and Bot data.
    restored = AccountAuthority(
        registry_store, b"k" * 32, lookup=lambda key: plans[key], clock=lambda: now[0]
    )
    manager = AccountApplications(factory("operator"), restored, factory, max_accounts=2)
    with TestClient(manager.application()) as client:
        assert len(manager.apps) == 2
        assert client.get(base(1), headers=auth(1)).json()[0]["id"] == made[1]["id"]
        assert client.get(base(0), headers=auth(0)).status_code == 403
    assert (
        Store(tmp_path / ("account-" + ids[0]) / "bots.sqlite3").list_bots("account:" + ids[0])[0][
            "id"
        ]
        == made[0]["id"]
    )


@pytest.mark.parametrize("status", ["available", "unavailable"])
def test_concurrent_entitlement_checks_share_one_lookup_without_blocking_other_accounts(
    tmp_path, status
):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    owner = str(uuid4())
    entered, release, follower_started = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def lookup(key):
        calls.append(key)
        if key == "slk_shared":
            entered.set()
            assert release.wait(2)
            if status == "unavailable":
                raise CloudAccessError()
        return {"status": "available", "account_id": owner, "plan": "alpha"}

    authority = AccountAuthority(Store(tmp_path / "registry.db"), b"k" * 32, lookup=lookup)

    def follower():
        follower_started.set()
        return authority.access("slk_shared")

    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(authority.access, "slk_shared")
        try:
            assert entered.wait(1)
            second = pool.submit(follower)
            assert follower_started.wait(1)
            with pytest.raises(TimeoutError):
                second.result(0.05)
            assert pool.submit(authority.access, "slk_other").result(1) == "account:" + owner
            assert calls.count("slk_shared") == 1
        finally:
            release.set()
        for result in (first, second):
            if status == "available":
                assert result.result(1) == "account:" + owner
            else:
                with pytest.raises(CloudAccessError, match="unavailable"):
                    result.result(1)
    assert not authority.pending


def test_slow_failed_account_lookup_gets_a_backoff_after_the_failure_finishes(tmp_path):
    now, calls = [0], []

    def lookup(key):
        calls.append(key)
        now[0] += 8
        raise CloudAccessError()

    authority = AccountAuthority(
        Store(tmp_path / "registry.db"), b"k" * 32, lookup=lookup, clock=lambda: now[0]
    )
    for _ in range(2):
        with pytest.raises(CloudAccessError, match="unavailable"):
            authority.access("slk_slow")
    assert len(calls) == 1 and now[0] == 8
    now[0] = 12
    with pytest.raises(CloudAccessError):
        authority.access("slk_slow")
    assert len(calls) == 2 and not authority.pending


def test_slow_success_does_not_extend_the_existing_entitlement_freshness_window(tmp_path):
    now, calls = [0], []
    owner = str(uuid4())

    def lookup(key):
        calls.append(key)
        now[0] += 8
        return {"status": "available" if len(calls) == 1 else "unavailable", "account_id": owner}

    authority = AccountAuthority(
        Store(tmp_path / "registry.db"), b"k" * 32, lookup=lookup, clock=lambda: now[0]
    )
    assert authority.access("slk_slow") == "account:" + owner
    now[0] = 16
    with pytest.raises(CloudAccessError):
        authority.access("slk_slow")
    assert len(calls) == 2


def test_hosted_controller_uses_each_accounts_gateway_for_model_choice_and_billing(
    tmp_path, monkeypatch
):
    from alysis_code import alysis_cloud
    from alysis_code.bots import cloud_models, hosted_azure, managed_cloud
    from alysis_code.bots.owl_models import ModelUnavailable
    from alysis_code.llm.openai_responses import OpenAIResponsesClient

    first, second = str(uuid4()), str(uuid4())
    first_key, second_key, rotated = "slk_first", "slk_second", "slk_rotated"
    owners = {first_key: first, second_key: second, rotated: first}
    catalogs = {
        first_key: ["glm-5.3-flash", "glm-5.3", "gpt-6-luna"],
        second_key: ["gpt-6-luna"],
        rotated: ["glm-5.3-flash"],
    }
    store = Store(tmp_path / "shared.db")
    store.close = lambda: None
    fake_resource = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(hosted_azure, "PostgresStore", lambda *args, **kw: store)

    # A local SQLite worker owns its whole database. Give each test worker its own file,
    # while the controller shares the account registry as it does in the hosted service.
    def local_app(config, **options):
        options.pop("task_store")
        return create_app(config, **options)

    monkeypatch.setattr(hosted_azure, "create_app", local_app)
    monkeypatch.setattr(hosted_azure, "AzureBlobStorage", lambda *args, **kw: fake_resource)
    monkeypatch.setattr(hosted_azure, "AzureComputers", lambda **kw: fake_resource)
    monkeypatch.setattr(hosted_azure, "CloudDesktopFactory", lambda *args, **kw: None)
    monkeypatch.setattr(cloud_models, "gateway_models", catalogs.get)
    real_authority = managed_cloud.AccountAuthority
    monkeypatch.setattr(
        managed_cloud,
        "AccountAuthority",
        lambda db, secret: real_authority(
            db, secret, lookup=lambda k: {"status": "available", "account_id": owners[k]}
        ),
    )
    config = hosted_azure.HostedConfig(
        tmp_path,
        "fixture",
        "fixture",
        "fixture",
        "fixture",
        "fixture",
        "fixture",
        "fixture",
        "fixture",
        None,
        base64.b64encode(bytes(32)).decode(),
        managed_cloud=True,
        cloud_metering="external",
    )
    operator = Settings(
        tmp_path / "config",
        "t" * 40,
        "operator-fixture",
        "https://operator.example.invalid/v1",
        "glm-5.3-flash",
        models=("operator-only",),
        extra_headers={"x-operator-fixture": "private"},
    )

    with ExitStack() as stack:
        hosted = hosted_azure.build(operator, config, None, stack)
        api = stack.enter_context(TestClient(hosted))

        def request(owner, key, method, path, **kwargs):
            return api.request(
                method,
                f"/accounts/{owner}/v1/{path}",
                headers={"Authorization": f"Bearer {key}"},
                **kwargs,
            )

        def listed(owner, key):
            result = request(owner, key, "GET", "models")
            assert result.is_success, result.text
            return result.json()["models"]

        choices = listed(first, first_key)
        assert [row["id"] for row in choices] == catalogs[first_key]
        assert (
            "cloud_account_models"
            in request(first, first_key, "GET", "status").json()["capabilities"]
        )
        assert [row["id"] for row in listed(second, second_key)] == catalogs[second_key]
        assert (
            request(
                second, second_key, "POST", "bots", json={"name": "Other", "model": "glm-5.3"}
            ).status_code
            == 409
        )
        assert (
            request(
                first, first_key, "POST", "bots", json={"name": "Other", "model": "operator-only"}
            ).status_code
            == 409
        )

        owl = request(first, first_key, "POST", "bots", json={"name": "Apollo"}).json()
        path = "bots/" + owl["id"]
        assert request(
            first,
            first_key,
            "PUT",
            path,
            json={"name": "Apollo", "model": "glm-5.3", "reasoning_effort": "high"},
        ).is_success
        runtime = hosted.state.runtime.apps["account:" + first].state.runtime
        client, _ = runtime.turn_model("next-turn", owl["id"])
        sent = []

        def transport(request):
            sent.append(request)
            return httpx.Response(
                200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
            )

        client._transport = httpx.MockTransport(transport)
        client.chat(messages=[{"role": "user", "content": "Hi"}])
        assert str(sent[0].url).startswith(alysis_cloud.gateway_base_url() + "/")
        assert sent[0].headers["Authorization"] == "Bearer " + first_key
        assert "x-operator-fixture" not in sent[0].headers
        assert json.loads(sent[0].content)["model"] == "glm-5.3"
        assert json.loads(sent[0].content)["reasoning_effort"] == "high"

        other = request(second, second_key, "POST", "bots", json={"name": "Default"}).json()
        runtime2 = hosted.state.runtime.apps["account:" + second].state.runtime
        default, _ = runtime2.turn_model("background-turn", other["id"])
        assert isinstance(default, OpenAIResponsesClient)
        assert (default.model, default.api_key) == ("gpt-6-luna", second_key)
        assert [row["id"] for row in listed(first, rotated)] == catalogs[rotated]
        with pytest.raises(ModelUnavailable):
            runtime.turn_model("later-turn", owl["id"])
        assert request(
            first, rotated, "PUT", path, json={"name": "Apollo", "model": None}
        ).is_success
        assert runtime.turn_model("later-turn", owl["id"])[0].api_key == rotated

    # Autonomous work resumes under the owner's latest key before any desktop reconnects.
    with ExitStack() as stack:
        restored = hosted_azure.build(operator, config, None, stack)
        stack.enter_context(TestClient(restored))
        resumed = restored.state.runtime.apps["account:" + first].state.runtime
        client, _ = resumed.turn_model("after-restart", owl["id"])
        assert (client.model, client.api_key) == ("glm-5.3-flash", rotated)
