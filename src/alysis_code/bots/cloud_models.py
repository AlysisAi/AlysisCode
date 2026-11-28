"""The managed Cloud Owl's model connection belongs to its signed-in Alysis account."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from functools import lru_cache
from typing import TYPE_CHECKING

from .. import alysis_cloud
from ..account_login import gateway_models
from .cloud_account import DEVICE_KEY
from .owl_models import CachedCatalog, CatalogModel, ModelConnection, ModelUnavailable

if TYPE_CHECKING:
    from .app import Settings


def account_settings(settings: Settings, key: str | None) -> Settings:
    if not key or not DEVICE_KEY.fullmatch(key):
        raise ModelUnavailable("Sign in to Alysis to use models with this Cloud Owl.")
    # Operator routing headers, credentials and image-generation settings belong to another
    # connection. Use exactly the first-party route used by the signed-in desktop agent.
    return replace(
        settings,
        base_url=alysis_cloud.gateway_base_url(),
        api_key=key,
        provider_key=alysis_cloud.PROFILE_KEY,
        protocol="openai_compat",
        extra_headers={},
        reasoning_effort=None,
        image_generation=None,
        models=(),
    )


def account_connections(settings: Settings, credential: Callable[[], str | None]):
    # This cache belongs to one account app. Rotating its key starts a fresh catalog.
    @lru_cache(maxsize=1)
    def catalog(key: str) -> CachedCatalog:
        def read():
            models = gateway_models(key)
            return None if models is None else [CatalogModel(model, model) for model in models]

        return CachedCatalog(read)

    def connections() -> list[ModelConnection]:
        key = credential()
        if not key or not DEVICE_KEY.fullmatch(key):
            return []
        models = [entry.model for entry in catalog(key)().models]
        if not models:
            return []
        # Keep existing Cloud Owl model IDs and the old default if the account can still use it.
        default = settings.model if settings.model in models else models[0]
        account = account_settings(settings, key)
        return [
            ModelConnection(model, model, replace(account, model=model), default=model == default)
            for model in models
        ]

    return connections
