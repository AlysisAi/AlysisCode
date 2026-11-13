import base64
import io
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from alysis_code.bots.hosted_azure import HostedConfig, bootstrap


def values():
    return {
        "hosted": {
            "state_directory": "/tmp/alysis-hosted-fixture",
            "database_dsn": "synthetic-private-dsn",
            "storage_url": "https://synthetic.blob.core.windows.net",
            "storage_container": "bots",
            "subscription": "synthetic",
            "resource_group": "synthetic",
            "sandbox_group": "synthetic",
            "sandbox_region": "swedencentral",
            "worker_image": "synthetic",
            "expires_at": 2000000000,
            "browser_state_key": base64.b64encode(bytes(range(32))).decode(),
        },
        "settings": {
            "token": "synthetic-api-token-" + "x" * 32,
            "api_key": "synthetic-model-secret",
            "base_url": "https://example.invalid/v1",
            "model": "fixture",
            "owner": "synthetic",
        },
    }


def test_hosted_bootstrap_keeps_secrets_out_of_representations():
    settings, hosted = bootstrap(io.BytesIO(json.dumps(values()).encode()))
    assert settings.database.parent == hosted.state_directory
    assert hosted.port == 7080
    assert hosted.max_computers == 2
    rendered = repr(settings) + repr(hosted)
    assert "synthetic-private-dsn" not in rendered
    assert "synthetic-model-secret" not in rendered
    assert values()["settings"]["token"] not in rendered
    assert values()["hosted"]["browser_state_key"] not in rendered


def test_hosted_bootstrap_rejects_oversized_request():
    with pytest.raises(ValueError, match="allowance"):
        bootstrap(io.BytesIO(b"x" * 16385))


def test_stream_origin_can_be_configured_without_copying_bootstrap_secrets(monkeypatch):
    endpoint = "wss://controller.example/internal/computer-stream"
    monkeypatch.setenv("ALYSIS_BOTS_COMPUTER_STREAM_URL", endpoint)
    _, hosted = bootstrap(io.BytesIO(json.dumps(values()).encode()))
    assert hosted.computer_stream_url == endpoint
    monkeypatch.setenv("ALYSIS_BOTS_COMPUTER_STREAM_BOTS", "0123456789abcdef0123456789abcdef")
    _, hosted = bootstrap(io.BytesIO(json.dumps(values()).encode()))
    assert hosted.computer_stream_bots == ("0123456789abcdef0123456789abcdef",)
    monkeypatch.setenv("ALYSIS_BOTS_COMPUTER_STREAM_URL", "ws://controller.example/unsafe")
    with pytest.raises(ValueError):
        bootstrap(io.BytesIO(json.dumps(values()).encode()))


def test_continuous_hosting_requires_an_explicit_null_expiry():
    data = values()
    data["hosted"]["expires_at"] = None
    _, hosted = bootstrap(io.BytesIO(json.dumps(data).encode()))
    assert hosted.expires_at is None
    del data["hosted"]["expires_at"]
    with pytest.raises(TypeError):
        bootstrap(io.BytesIO(json.dumps(data).encode()))


def test_hosted_alpha_can_remove_compute_cap_and_grant_five_gb_shared_library():
    data = values()
    data["hosted"].update(expires_at=None, max_computers=None, account_library_bytes=5_000_000_000)
    _, hosted = bootstrap(io.BytesIO(json.dumps(data).encode()))
    assert hosted.max_computers is None
    assert hosted.account_library_bytes == 5_000_000_000


def test_uncapped_cloud_application_starts_and_serves_authenticated_status(tmp_path):
    from fastapi.testclient import TestClient
    from test_bots_cloud_desktop import Provider

    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.cloud_desktop import CloudDesktopFactory
    from alysis_code.bots.compute import ComputerSpec
    from alysis_code.bots.object_storage import LocalObjectStorage
    from alysis_code.bots.store import Store

    store = Store(tmp_path / "bots.db", objects=LocalObjectStorage(tmp_path / "objects"))
    factory = CloudDesktopFactory(
        "alpha",
        Provider(),
        ComputerSpec("synthetic"),
        tmp_path / "leases",
        store=store,
        max_computers=None,
    )
    settings = Settings(
        store.path,
        "synthetic-alpha-token-" + "x" * 32,
        "unused",
        "https://example.invalid/v1",
        "fixture",
        owner="alpha",
    )
    app = create_app(
        settings,
        task_store=store,
        cloud_computers=factory,
        client_factory=lambda _: pytest.fail("No model call expected"),
    )
    with TestClient(app) as client:
        response = client.get("/v1/status", headers={"Authorization": "Bearer " + settings.token})
        assert response.status_code == 200
        assert response.json()["worker_ready"] is True
        assert response.json()["computer_capacity"]["limit"] is None
        assert response.json()["computer_capacity"]["active"] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("port", 80),
        ("port", True),
        ("max_computers", 0),
        ("max_computers", True),
        ("account_library_bytes", -1),
        ("account_library_bytes", True),
        ("expires_at", float("nan")),
        ("expires_at", float("inf")),
        ("expires_at", True),
        ("state_directory", Path("relative")),
    ],
)
def test_hosted_config_requires_bounded_operator_values(field, value):
    _, hosted = bootstrap(io.BytesIO(json.dumps(values()).encode()))
    with pytest.raises(ValueError):
        HostedConfig(**{**asdict(hosted), field: value})


@pytest.mark.parametrize(
    "certificate,key", [("certificate", None), (None, "private-key"), ("", "key"), (True, "key")]
)
def test_tls_bootstrap_requires_a_complete_private_pair(certificate, key):
    data = values()
    data["hosted"].update(tls_certificate=certificate, tls_private_key=key)
    with pytest.raises(ValueError):
        bootstrap(io.BytesIO(json.dumps(data).encode()))


def test_tls_private_key_is_not_in_hosted_representations():
    data = values()
    data["hosted"].update(
        tls_certificate="public-certificate", tls_private_key="synthetic-private-tls-key"
    )
    _, hosted = bootstrap(io.BytesIO(json.dumps(data).encode()))
    assert "synthetic-private-tls-key" not in repr(hosted)


def test_cloud_billing_credential_is_kept_out_of_hosted_representations():
    data = values()
    data["hosted"].update(
        billing_url="https://billing.invalid", billing_key="synthetic-billing-secret"
    )
    _, hosted = bootstrap(io.BytesIO(json.dumps(data).encode()))
    assert "synthetic-billing-secret" not in repr(hosted)
