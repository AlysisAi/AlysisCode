from __future__ import annotations

from hashlib import sha256
from typing import Any

from ..config import AppConfig
from .web_search_runtime import resolve_web_search_runtime, resolve_web_search_runtime_status


class WebSearchCapabilityProbe:
    """Session-local operational evidence; configuration is only a prerequisite.

    The first authorized search is the probe. Checking status never sends a query,
    switches provider, spends credits, or probes with a credential behind the user.
    """

    def __init__(self) -> None:
        self._observation: tuple[str, str, str, str | None] | None = None

    @staticmethod
    def _identity(cfg: AppConfig, api_key: str | None) -> str:
        runtime = resolve_web_search_runtime(cfg=cfg, api_key=api_key)
        return sha256(repr(runtime).encode("utf-8")).hexdigest()

    def record(
        self,
        *,
        cfg: AppConfig,
        api_key: str | None,
        result: dict[str, Any] | None = None,
        failed: bool = False,
    ) -> None:
        runtime = resolve_web_search_runtime(cfg=cfg, api_key=api_key)
        backend = str((result or {}).get("backend_adapter") or (result or {}).get("backend") or "")
        if failed:
            state, basis = "unavailable", "last_authorized_operation_failed"
        elif runtime is not None and backend and backend != runtime.provider:
            state, basis = "degraded", "authorized_fallback_succeeded"
        elif result is not None and not (result.get("sources") or result.get("citations")):
            state, basis = "degraded", "operation_returned_no_sources"
        else:
            state, basis = "ready", "authorized_operation_succeeded"
        self._observation = (self._identity(cfg, api_key), state, basis, backend or None)

    def status(self, *, cfg: AppConfig, api_key: str | None, exposed: bool) -> dict[str, Any]:
        status = resolve_web_search_runtime_status(cfg=cfg, api_key=api_key)
        payload = status.to_payload()
        if not exposed or not status.registration_ready:
            return {
                **payload,
                "state": "unavailable",
                "basis": "configuration_or_role",
                "resolution": status.setup_hint
                if exposed
                else "Not exposed in this session role or policy.",
            }
        current_identity = self._identity(cfg, api_key)
        if self._observation is not None and self._observation[0] == current_identity:
            _, state, basis, backend = self._observation
            return {
                **payload,
                "state": state,
                "basis": basis,
                "observed_backend": backend,
                "resolution": "Retry after a configuration or environment change."
                if state == "unavailable"
                else None,
            }
        return {
            **payload,
            "state": "degraded",
            "basis": "configured_but_unprobed",
            "resolution": "The next authorized search will verify this endpoint, authentication and adapter together.",
        }
