"""Optional anonymous CLI active-day counts; never part of model/billing traffic."""

from __future__ import annotations

import base64
import json
import os
import platform
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

_lock = threading.Lock()
_sent_day: str | None = None


def _routing_key(api: str) -> str:
    """Discover public routing configuration without bundling deployment keys."""
    import httpx

    from .alysis_cloud import DEFAULT_CLOUD_API_URL, public_client_key, site_url

    key = public_client_key()
    if not key:
        if api != DEFAULT_CLOUD_API_URL:
            return ""  # Custom deployments must supply their own routing configuration.
        with httpx.stream(
            "GET",
            site_url() + "/.well-known/alysis-client.json",
            timeout=3.0,
            follow_redirects=False,
        ) as response:
            if response.status_code != 200 or "application/json" not in response.headers.get(
                "content-type", ""
            ):
                return ""
            body = bytearray()
            for chunk in response.iter_bytes(chunk_size=512):
                body.extend(chunk)
                if len(body) > 4096:
                    return ""
        config = json.loads(body)
        if config.get("schema_version") != 1 or config.get("api_base") != api:
            return ""
        key = config.get("anon_key", "")
    if not isinstance(key, str) or len(key) > 1024 or len(key.split(".")) != 3:
        return ""
    # The HTTPS endpoint supplies configuration; the gateway verifies its signature.
    # Never forward a service-role key or an account token supplied by misconfiguration.
    payload = key.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return key if claims.get("role") == "anon" else ""


def _enabled() -> bool:
    return (
        os.environ.get("ALYSIS_USAGE_METRICS") == "1"
        and os.environ.get("DO_NOT_TRACK") != "1"
        and os.environ.get("ALYSIS_CLIENT_KIND", "cli") == "cli"
        and not os.environ.get("ALYSIS_APP_ROOT")
        and not os.environ.get("ALYSIS_APP_SERVER_MARKER")
    )


def _record_active_day() -> None:
    global _sent_day
    if not _enabled() or not _lock.acquire(blocking=False):
        return
    try:
        import httpx

        from . import __version__
        from .alysis_cloud import cloud_api_url
        from .config import _data_dir

        api = cloud_api_url()
        day = datetime.now(timezone.utc).date().isoformat()
        if _sent_day == day:
            return
        path = _data_dir() / "cli-usage-metrics.json"
        state: dict = {}
        try:
            if path.stat().st_size > 2048:
                return
            state = json.loads(path.read_text(encoding="utf-8"))
            if UUID(state["id"]).version != 4:
                return
        except FileNotFoundError:
            state = {"id": str(uuid4()), "sent_day": None}
        if state.get("sent_day") == day:
            _sent_day = day
            return
        key = _routing_key(api)
        if not key:
            return
        # Persist identity before sending. Network failures/retries keep the same ID.
        _write_state(path, state, create_only=True)
        state = json.loads(path.read_text(encoding="utf-8"))
        response = httpx.post(
            api + "/client-measurement",
            headers={
                "apikey": key,
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
            },
            json={
                "kind": "cli",
                "installation_id": state["id"],
                "fresh_install": False,
                "platform": {"Darwin": "macOS"}.get(platform.system(), platform.system()),
                "version": __version__,
            },
            timeout=3.0,
            follow_redirects=False,
        )
        if response.status_code == 204:
            _sent_day = day
            _write_state(path, {**state, "sent_day": day})
    except Exception:
        pass  # Measurement can never prevent a task, print private data, or trigger auth.
    finally:
        _lock.release()


def _write_state(path: Path, state: dict, *, create_only: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".cli-usage-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        if create_only:
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        else:
            os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def record_cli_activity() -> None:
    """Called on submitted tasks, never login/status/idle prompt polling."""
    if _enabled():
        threading.Thread(target=_record_active_day, name="alysis-usage", daemon=True).start()
