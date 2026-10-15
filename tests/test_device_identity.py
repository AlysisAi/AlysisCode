from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest

from alysis_code import account_login, device_identity
from alysis_code.config import load_config


def test_identity_survives_repeated_login_logout_and_hostname_change(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    requests = []

    def post(_url, body):
        requests.append(body)
        return {"device_code": "secret", "user_code": "ABCD-EFGH"}

    monkeypatch.setattr(account_login, "_post_json", post)
    for _ in range(5):
        account_login._request_device_code()
    account_login.logout(load_config())
    monkeypatch.setattr(account_login.platform, "node", lambda: "renamed-host")
    account_login._request_device_code()
    identities = {body["installation_id"] for body in requests}
    assert len(identities) == 1
    assert UUID(identities.pop()).version == 4
    assert requests[-1]["client_name"] == "alysis-cli @ renamed-host"


def test_same_hostname_different_installations_have_different_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "first"))
    first = device_identity.get_installation_id()
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "second"))
    assert device_identity.get_installation_id() != first


def test_concurrent_first_logins_share_one_complete_id(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path))
    with ThreadPoolExecutor(max_workers=8) as pool:
        identities = list(pool.map(lambda _: device_identity.get_installation_id(), range(32)))
    assert len(set(identities)) == 1
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cli-installation-id"]


def test_corrupt_identity_does_not_silently_register_another_device(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path))
    (tmp_path / "cli-installation-id").write_text("broken", encoding="ascii")
    monkeypatch.setattr(account_login, "_post_json", lambda *_: pytest.fail("must not start login"))
    with pytest.raises(account_login.AlysisLoginError, match="installation identity"):
        account_login._request_device_code()
    assert (tmp_path / "cli-installation-id").read_text() == "broken"


def test_unwritable_identity_does_not_start_login(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path))

    def denied(*args, **kwargs):
        raise PermissionError("read-only")

    monkeypatch.setattr(device_identity.tempfile, "mkstemp", denied)
    monkeypatch.setattr(account_login, "_post_json", lambda *_: pytest.fail("must not start login"))
    with pytest.raises(account_login.AlysisLoginError, match="installation identity"):
        account_login._request_device_code()
