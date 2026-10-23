"""Exercise an installed CLI's default login without using a real account."""

from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-root", type=Path, required=True)
    parser.add_argument("--live-start", action="store_true", help="Request one expiring code only.")
    args = parser.parse_args()
    for name in list(os.environ):
        if name.startswith(("ALYSIS_", "SYLLIPTOR_")):
            del os.environ[name]
    os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
    with tempfile.TemporaryDirectory(prefix="alysis-login-smoke-") as directory:
        os.environ["ALYSIS_CONFIG_DIR"] = str(Path(directory) / "config")
        os.environ["ALYSIS_DATA_DIR"] = str(Path(directory) / "data")
        from typer.testing import CliRunner

        from alysis_code import __version__, account_login
        from alysis_code import alysis_cloud as cloud
        from alysis_code.cli import app
        from alysis_code.config import load_config, load_persisted_profile_keys

        assert Path(cloud.__file__).resolve().is_relative_to(args.expected_root.resolve()), (
            "Smoke test imported a different installation",
            cloud.__file__,
        )
        base = cloud.cloud_api_url()
        assert base and urlsplit(base).scheme == "https", "Missing default hosted API"
        assert cloud.gateway_base_url() == f"{base}/llm/v1"
        assert cloud.account_status_url() == f"{base}/account-status"
        assert cloud.feedback_url() == f"{base}/feedback"
        assert cloud.public_client_key() == "", "Public build must not bundle project keys"
        if args.live_start:
            # Do not approve, poll, persist, or print any live code or credential.
            grant = account_login._request_device_code()
            assert grant.device_code and grant.user_code
            assert grant.verification_url.startswith(cloud.activate_url() + "?")
            print(
                json.dumps({"version": __version__, "live_login_started": True, "approved": False})
            )
            return

        requests = []
        opened = []
        synthetic_key = "slk_synthetic-installed-wheel-smoke"

        def respond(request, **kwargs):
            assert request.get_method() == "POST"
            assert request.get_header("Authorization") is None
            assert request.get_header("Apikey") is None
            requests.append(request.full_url)
            payload = json.loads(request.data)
            if request.full_url == f"{base}/device-code":
                assert payload["installation_id"]
                reply = {"device_code": "synthetic-device", "user_code": "TEST-CODE", "interval": 0}
            elif request.full_url == f"{base}/device-token":
                assert payload == {"device_code": "synthetic-device"}
                reply = {"status": "approved", "key": synthetic_key}
            else:
                raise AssertionError(f"Unexpected login request: {request.full_url}")
            return io.BytesIO(json.dumps(reply).encode())

        with (
            patch("urllib.request.urlopen", respond),
            patch.object(account_login, "_open_browser", lambda url: opened.append(url) or True),
        ):
            result = CliRunner().invoke(app, ["login"])
        assert result.exit_code == 0, result.output
        assert requests == [f"{base}/device-code", f"{base}/device-token"]
        assert opened == [f"{cloud.activate_url()}?code=TEST-CODE"]
        cfg = load_config()
        status = account_login.login_status(cfg)
        assert status.logged_in and status.active
        assert status.base_url == cloud.gateway_base_url()
        assert load_persisted_profile_keys()["alysis"] == synthetic_key
        print(json.dumps({"version": __version__, "default_cli_login": "passed", "http_calls": 2}))


if __name__ == "__main__":
    main()
