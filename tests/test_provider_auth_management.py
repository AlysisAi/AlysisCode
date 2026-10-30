from dataclasses import replace

import pytest

from alysis_code.cli_impl.commands import auth
from alysis_code.ide import management_protocol as management
from alysis_code.ide.protocol import ProtocolError
from alysis_code.provider_auth import ProviderAccountStatus, ProviderAuthError


def test_management_reads_fresh_provider_identity_and_revocation(monkeypatch):
    status = ProviderAccountStatus(connected=True, verified=True, credential_scope="scope-a")
    reads = []

    class Adapter:
        protocol = "openai_responses"
        display_name = "Test subscription"

        def account_status(self):
            reads.append(status)
            return status

    monkeypatch.setattr(auth, "create_provider_auth", lambda provider_id: Adapter())
    monkeypatch.setattr(auth, "credential_backend_fields", lambda: {})

    def read():
        return management.handle_management_method(
            "provider.auth.status", {"provider_id": "test-provider"}, request_id="test"
        )

    first = read()
    assert first["authenticated"] is True
    assert first["route_scope"] == "scope-a"
    status = replace(status, credential_scope="scope-b")
    assert read()["route_scope"] == "scope-b"
    status = replace(status, connected=False)
    revoked = read()
    assert revoked["authenticated"] is False
    assert revoked["route_scope"] is None
    status = replace(status, connected=True, verified=False)
    assert read()["route_scope"] is None
    assert len(reads) == 4
    assert first["secret_values_included"] is False
    assert "credential_scope" not in first
    assert "account_label" not in first


def test_management_provider_error_is_unverified_and_redacted(monkeypatch):
    def fail(provider_id):
        raise ProviderAuthError("Unavailable token=secret-fixture-value")

    monkeypatch.setattr(auth, "create_provider_auth", fail)
    monkeypatch.setattr(auth, "credential_backend_fields", lambda: {})
    answer = management.handle_management_method(
        "provider.auth.status", {"provider_id": "test-provider"}, request_id="test"
    )
    assert answer["authenticated"] is False
    assert answer["verified"] is False
    assert answer["route_scope"] is None
    assert "secret-fixture-value" not in str(answer)


def test_management_provider_status_requires_provider_id():
    with pytest.raises(ProtocolError):
        management.handle_management_method("provider.auth.status", {}, request_id="test")
    capability = management.MANAGEMENT_METHOD_CAPABILITIES["provider.auth.status"]
    assert capability["mutates"] is False
    assert capability["workspace_required"] is False
