from alysis_code.ide.health import health_payload


def test_ide_bridge_advertises_alysis_identity() -> None:
    payload = health_payload()

    assert payload["ok"] is True
    assert payload["name"] == "alysis-ide-bridge"
