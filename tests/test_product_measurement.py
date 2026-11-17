import json
from types import SimpleNamespace

from alysis_code import product_measurement as metrics


def test_opt_in_and_desktop_exclusion(monkeypatch):
    for name in (
        "ALYSIS_USAGE_METRICS",
        "DO_NOT_TRACK",
        "ALYSIS_CLIENT_KIND",
        "ALYSIS_APP_ROOT",
        "ALYSIS_APP_SERVER_MARKER",
    ):
        monkeypatch.delenv(name, raising=False)
    assert not metrics._enabled()
    monkeypatch.setenv("ALYSIS_USAGE_METRICS", "1")
    assert metrics._enabled()
    monkeypatch.setenv("ALYSIS_CLIENT_KIND", "desktop")
    assert not metrics._enabled()
    monkeypatch.setenv("ALYSIS_CLIENT_KIND", "cli")
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    assert not metrics._enabled()


def test_retries_and_process_restarts_keep_identity_and_deduplicate_day(monkeypatch, tmp_path):
    import httpx

    import alysis_code.config as config

    monkeypatch.setattr(config, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(metrics, "_enabled", lambda: True)
    monkeypatch.setattr(metrics, "_sent_day", None)
    monkeypatch.setattr(metrics, "_routing_key", lambda api: "public-test-routing-key")
    events = []
    statuses = iter((503, 204))

    def post(url, **kwargs):
        events.append(kwargs["json"])
        return SimpleNamespace(status_code=next(statuses))

    monkeypatch.setattr(httpx, "post", post)
    metrics._record_active_day()
    metrics._record_active_day()
    metrics._record_active_day()
    monkeypatch.setattr(metrics, "_sent_day", None)
    metrics._record_active_day()
    assert len(events) == 2
    assert events[0]["installation_id"] == events[1]["installation_id"]
    assert events[0]["kind"] == "cli"
    assert events[0]["fresh_install"] is False
    assert set(events[0]) == {"kind", "installation_id", "fresh_install", "platform", "version"}
    assert json.loads((tmp_path / "cli-usage-metrics.json").read_text())["sent_day"]


def test_discovery_is_bounded_and_rejects_wrong_destination_and_privileged_keys(monkeypatch):
    import base64

    import httpx

    import alysis_code.alysis_cloud as cloud

    monkeypatch.setattr(cloud, "public_client_key", lambda: "")

    def token(role):
        return (
            "header."
            + base64.urlsafe_b64encode(json.dumps({"role": role}).encode()).decode().rstrip("=")
            + ".signature"
        )

    config = {
        "schema_version": 1,
        "api_base": cloud.DEFAULT_CLOUD_API_URL,
        "anon_key": token("anon"),
    }
    calls = []

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_bytes(self, **kwargs):
            yield json.dumps(config).encode()

    def stream(*args, **kwargs):
        calls.append((args, kwargs))
        return Response()

    monkeypatch.setattr(httpx, "stream", stream)
    assert metrics._routing_key(cloud.DEFAULT_CLOUD_API_URL) == token("anon")
    assert calls[0][1]["follow_redirects"] is False
    config["anon_key"] = token("service_role")
    assert metrics._routing_key(cloud.DEFAULT_CLOUD_API_URL) == ""
    config["api_base"] = "https://other.example.test"
    assert metrics._routing_key(cloud.DEFAULT_CLOUD_API_URL) == ""
    config["padding"] = "x" * 4096
    assert metrics._routing_key(cloud.DEFAULT_CLOUD_API_URL) == ""
    count = len(calls)
    assert metrics._routing_key("https://custom.example.test") == ""
    assert len(calls) == count
