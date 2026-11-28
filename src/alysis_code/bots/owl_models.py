"""The model each owl thinks with, picked from the models its place offers.

A place (this computer or the Cloud) runs with one model connection: the default for its owls.
It may offer others: on this computer, the person's other saved model connections; in the Cloud,
the models the service offers on its endpoint (``Settings.models``). An owl that picked one
(``bots.model``, null for the default) thinks with it in every turn. When its connection is no
longer offered (its key was removed, the service changed), the turn fails with failure kind
"model" and says so; it never thinks with another connection instead.

A connection may also have a catalog: the models its provider lists for the person (their
account's models on the account gateway, a provider's reviewed models). Each is offered as
``<connection>/<model>``, through the same connection with only the model changed, so its key,
endpoint, usage accounting and plan limits are the connection's own. The connection's id alone
still means its own model. A catalog model the catalog no longer lists, or no longer opens to
the person, makes the turn think with that connection's own model and say so in a plain notice
(``ModelFallback``); one whose catalog can't be read just now is used as picked, and its
provider decides.

What a model can do is read from its declared capacities (the shared model registry), never
from its name.
"""

from __future__ import annotations

import copy
import dataclasses
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from ..config import AppConfig
from ..llm.base import ChatClient
from ..llm.factory import resolve_model_protocol
from ..llm.protocols import (
    ANTHROPIC_MESSAGES_PROTOCOL,
    OPENAI_COMPAT_PROTOCOL,
    OPENAI_RESPONSES_PROTOCOL,
)
from ..model_reasoning import (
    contract_reasoning_capabilities,
    reasoning_client_options,
    route_reasoning_contract,
)
from ..model_registry import ModelRegistry
from ..profile_presets import find_preset_for_base_url
from ..reasoning_contracts import ReasoningContract, resolve_reasoning_provider
from .browser_images import declared_image_support
from .store import Conflict

#: Why a catalog model can't be picked now, as the app words it in the person's language: the
#: catalog lists it but doesn't open it to this account yet, or that couldn't be checked just now.
PENDING = "pending"
UNCHECKED = "unchecked"
#: Why a turn didn't think with its owl's catalog model: its catalog no longer lists it.
REMOVED = "removed"


@dataclass(frozen=True)
class CatalogModel:
    """One model a connection's provider lists for the person."""

    model: str
    label: str
    # None: the person can think with it now; otherwise PENDING or UNCHECKED.
    unavailable: str | None = None


@dataclass(frozen=True)
class ModelCatalog:
    """The models a connection's provider lists; ``read`` is False when that couldn't be read."""

    models: tuple[CatalogModel, ...] = ()
    read: bool = True


@dataclass(frozen=True)
class ModelConnection:
    """One model a place offers: the id an owl keeps, what people see, and how to reach it."""

    id: str
    label: str
    # The service settings for this model: its endpoint and credentials. Never listed.
    settings: Any = field(repr=False)
    # The place runs with this one; owls without a model of their own think with it.
    default: bool = False
    # The other models its provider lists for the person, read when needed (catalog_id); None:
    # it offers only its own model. Settings with another model must be a dataclass.
    catalog: Callable[[], ModelCatalog] | None = field(default=None, repr=False, compare=False)


def catalog_id(connection_id: str, model: str) -> str:
    """The id an owl keeps for a model of a connection's catalog. Connection ids hold no "/"
    (saved connection names are lowercase letters, digits, "_" and "-"); model ids may."""
    return f"{connection_id}/{model}"


@dataclass(frozen=True)
class ModelFallback:
    """An owl's catalog model that its connection no longer opens to it: the turn thinks with
    the connection's own model instead, and says so."""

    picked: str
    used: str
    # PENDING: listed but not open to this account now; REMOVED: no longer listed.
    reason: str
    # The model was the routine's (a routine's run), not the owl's.
    routine: bool = False

    def event(self) -> dict:
        """The turn's notice: plain words for older apps, and the parts the app words itself."""
        why = (
            "isn't open to your account right now"
            if self.reason == PENDING
            else "isn't offered any more"
        )
        where = "for this routine in Routines" if self.routine else "in the owl's settings"
        return {
            "message": f"{self.picked} {why}, so this used {self.used} instead. Pick another "
            f"model {where}.",
            "picked": self.picked,
            "used": self.used,
            "reason": self.reason,
            **({"routine": True} if self.routine else {}),
        }


@dataclass(frozen=True)
class ModelResolution:
    """The model one turn of an owl thinks with, and the notice when it isn't the one picked."""

    connection: ModelConnection
    fallback: ModelFallback | None = None


class ModelUnavailable(Conflict):
    """An owl's model is not offered in its place (any more)."""

    @classmethod
    def for_owl(cls, model_id: str) -> ModelUnavailable:
        return cls(
            f"This owl's model ({model_id}) isn't available here any more, so it didn't start "
            "this. Choose another model in the owl's settings, or set that model up again."
        )

    @classmethod
    def for_routine(cls, model_id: str) -> ModelUnavailable:
        return cls(
            f"This routine's model ({model_id}) isn't available here any more, so this run didn't "
            "start. Choose another model for the routine in Routines, or set that model up again."
        )

    @classmethod
    def for_choice(cls, model_id: str) -> ModelUnavailable:
        return cls(f"{model_id} isn't available here. Choose one of the models this place offers.")


def model_provider(settings) -> str | None:
    # A saved profile can be renamed without changing its endpoint's route.
    preset = find_preset_for_base_url(getattr(settings, "base_url", ""))
    return preset.provider_key if preset else settings.provider_key


def model_protocol(settings) -> str:
    """The wire protocol for this model on its endpoint, as the desktop agent routes it."""
    return resolve_model_protocol(
        provider_key=model_provider(settings), model=settings.model, protocol=settings.protocol
    )


def image_input(settings) -> bool:
    """Whether turns with this model can be shown images (screens, pictures in files).

    The adapters below accept the bounded user image block from browser_images. The model's
    trusted catalog entry must also declare image input for its endpoint.
    """
    return model_protocol(settings) in {
        OPENAI_COMPAT_PROTOCOL,
        OPENAI_RESPONSES_PROTOCOL,
        ANTHROPIC_MESSAGES_PROTOCOL,
    } and declared_image_support(settings.model, settings.base_url)


def declared_image_generation(settings) -> bool:
    """Whether owls with this model can make and change pictures (owl_images.py)."""
    from .owl_images import declared

    return declared(settings) is not None


def context_window(settings) -> int:
    """The model's declared context window, in tokens (the registry's default when unknown)."""
    return (
        ModelRegistry(cfg=AppConfig(model=settings.model, base_url=settings.base_url))
        .get(settings.model, include_provider_auth=False)
        .context_window_tokens
    )


#: How hard an owl thinks while it works on its computer, when its model connection sets no
#: reasoning effort of its own and the model declares that it reasons (pending decision D3).
WORK_REASONING_EFFORT = "medium"

# What a saved connection holds when no effort was chosen; the engine's config reads these the
# same way (config._coerce_reasoning_effort, profiles: "auto" is no effort of its own).
_NO_EFFORT = frozenset(("", "auto", "default"))


def declares_reasoning(model: str, base_url: str) -> bool:
    """Whether the model's declared capacities (the shared model registry) say it reasons.

    A model the registry doesn't know is not assumed to.
    """
    return (
        ModelRegistry(cfg=AppConfig(model=model, base_url=base_url))
        .get(model, include_provider_auth=False)
        .supports_reasoning
        is True
    )


def work_reasoning_effort(connection_effort: str | None, model: str, base_url: str) -> str | None:
    """The reasoning effort an owl thinks with (its turns, its work on its computer, its coding
    work); None means no thinking.

    The model connection's own effort when it sets one; otherwise WORK_REASONING_EFFORT for a
    model that declares reasoning; otherwise none.
    """
    own = unicodedata.normalize("NFKC", str(connection_effort or "")).strip().casefold()
    if own not in _NO_EFFORT:
        return own
    return WORK_REASONING_EFFORT if declares_reasoning(model, base_url) else None


@lru_cache(maxsize=4)
def _openrouter_contracts(base_url: str, minute: int) -> dict[str, ReasoningContract]:
    """Share one public gateway lookup across the models in a picker and subsequent turns."""
    from ..profiles import ProfileSpec
    from ..provider_model_catalog import ProviderModelCatalogError, discover_provider_models

    profile = ProfileSpec(name="openrouter", base_url=base_url)
    try:
        models = discover_provider_models(profile=profile, api_key=None, timeout_s=3)
    except ProviderModelCatalogError:
        return {}
    return {
        item.id: item.reasoning_contract for item in models if item.reasoning_contract is not None
    }


def effort_choices(settings) -> list[dict]:
    preset = find_preset_for_base_url(settings.base_url)
    route = preset.provider_key if preset else settings.provider_key
    provider = resolve_reasoning_provider(route, settings.model)
    protocol = resolve_model_protocol(
        provider_key=route, model=settings.model, protocol=settings.protocol
    )
    if provider == "openrouter" and protocol == OPENAI_COMPAT_PROTOCOL:
        from ..provider_model_catalog import openrouter_reasoning_contract

        declared = (
            _openrouter_contracts(settings.base_url, int(time.monotonic() // 60))
            if preset and preset.provider_key == "openrouter"
            else {}
        )
        contract = declared.get(settings.model) or openrouter_reasoning_contract(
            None, model=settings.model
        )
        if contract is None:
            return []
    else:
        contract = route_reasoning_contract(
            provider, settings.model, protocol, preset.key if preset else None
        )
    return contract_reasoning_capabilities(contract)["reasoning_efforts"]


def owl_effort_client(client: ChatClient, choice: str, settings) -> ChatClient:
    # Connections can change after saving. Apply only a choice the current route takes.
    if not any(option["id"] == choice for option in effort_choices(settings)):
        return client
    selected = copy.copy(client)
    selected.reasoning_effort, selected.enable_thinking = reasoning_client_options(choice)
    selected._alysis_owl_effort = True
    return selected


def work_client(client: ChatClient) -> ChatClient:
    """The client an owl works with: its connection's, at work strength.

    Every model request of an owl's turn (its answers and the checks around them), its work on
    its computer and its coding work think with it. A connection's client carries the
    connection's reasoning effort (app.model_client), and the engine's provider clients read it
    on every call. When the owl should think harder than the connection asks, it gets a copy set
    to the work effort, the way the engine's chat switches effort on a client; the connection's
    own client is never changed. Each of the engine's provider clients accepts
    WORK_REASONING_EFFORT; the compatible one leaves out of the request an effort the model's
    provider does not document.
    """
    if getattr(client, "_alysis_owl_effort", False):
        return client
    effort = work_reasoning_effort(
        getattr(client, "reasoning_effort", None),
        client.model,
        str(getattr(client, "base_url", AppConfig.model_fields["base_url"].default)),
    )
    if effort is None or effort == getattr(client, "reasoning_effort", None):
        return client
    thinking = copy.copy(client)
    thinking.enable_thinking = True
    thinking.reasoning_effort = effort
    return thinking


#: What decides where a model call goes and what it asks for: the settings a client is made from.
_ROUTE_FIELDS = (
    "model",
    "base_url",
    "api_key",
    "protocol",
    "provider_key",
    "reasoning_effort",
    "extra_headers",
)


def same_route(first, second) -> bool:
    """Whether two connection settings make the same model client."""
    return all(getattr(first, name, None) == getattr(second, name, None) for name in _ROUTE_FIELDS)


def _catalog(connection: ModelConnection) -> ModelCatalog:
    return connection.catalog() if connection.catalog is not None else ModelCatalog()


def _with_model(connection: ModelConnection, model: str) -> Any | None:
    """The connection's settings with another model; None when the service couldn't use it."""
    try:
        return dataclasses.replace(connection.settings, model=model)
    except (TypeError, ValueError):
        return None


class PlaceModels:
    """The models this place offers, read fresh each time: keys and connections change."""

    def __init__(
        self,
        connections: Callable[[], list[ModelConnection]],
        client: Callable[[str, Any], ChatClient],
        *,
        account_scoped: bool = False,
    ) -> None:
        # Managed Cloud offers exact gateway IDs. Its default also follows the current account.
        self.account_scoped = account_scoped
        self._connections = connections
        self._client = client

    @staticmethod
    def _row(
        connection: ModelConnection,
        model_id: str,
        label: str,
        settings,
        model_label: str,
        *,
        default: bool = False,
        unavailable: str | None = None,
    ) -> dict:
        return {
            "id": model_id,
            "label": label,
            "model": settings.model,
            "default": default,
            # Declared capacities from the model registry, for this model on this endpoint.
            "image_input": image_input(settings),
            "context_window": context_window(settings),
            # The image generation its connection declares (owl_images.py).
            "image_generation": declared_image_generation(settings),
            "reasoning": declares_reasoning(settings.model, settings.base_url),
            "reasoning_efforts": effort_choices(settings),
            # The connection it thinks through (the picker's group), and the model's own name.
            "connection": connection.id,
            "connection_label": connection.label,
            "model_label": model_label,
            "available": unavailable is None,
            "unavailable": unavailable,
        }

    def list(self) -> list[dict]:
        """What an owl can pick from here, with what each model can do: each connection with its
        own model first, then its catalog's other models; the default connection first."""
        rows = []
        for item in sorted(self._connections(), key=lambda item: not item.default):
            catalog = _catalog(item)
            names = {entry.model: entry.label for entry in catalog.models}
            own = item.settings.model
            rows.append(
                self._row(
                    item,
                    item.id,
                    item.label,
                    item.settings,
                    names.get(own, own),
                    default=item.default,
                )
            )
            for entry in catalog.models:
                settings = None if entry.model == own else _with_model(item, entry.model)
                if settings is not None:
                    rows.append(
                        self._row(
                            item,
                            catalog_id(item.id, entry.model),
                            entry.label,
                            settings,
                            entry.label,
                            unavailable=entry.unavailable,
                        )
                    )
        return rows

    def offers(self, model_id: str) -> bool:
        """Whether an owl may newly pick ``model_id`` here: a connection, or a catalog model
        open to the person now (a catalog that couldn't be read offers none of its models)."""
        connections = self._connections()
        if any(item.id == model_id for item in connections):
            return True
        if self.account_scoped:
            return False
        pick = _CatalogPick.of(connections, model_id)
        return (
            pick is not None
            and (pick.own or (pick.entry is not None and pick.entry.unavailable is None))
            and _with_model(pick.connection, pick.model) is not None
        )

    def resolve(self, model_id: str | None) -> ModelResolution | None:
        """What an owl that picked ``model_id`` thinks with here; None when its connection isn't
        offered here (any more)."""
        if model_id is None:
            default = self.default()
            return ModelResolution(default) if default is not None else None
        connections = self._connections()
        exact = next((item for item in connections if item.id == model_id), None)
        if exact is not None:
            return ModelResolution(exact)
        if self.account_scoped:
            return None
        pick = _CatalogPick.of(connections, model_id)
        if pick is None:
            return None
        item = pick.connection
        if pick.own:
            return ModelResolution(item)
        # Open now, or not known just now (its provider decides): think with it as picked.
        unknown = not pick.catalog.read or (
            pick.entry is not None and pick.entry.unavailable == UNCHECKED
        )
        if unknown or (pick.entry is not None and pick.entry.unavailable is None):
            settings = _with_model(item, pick.model)
            if settings is not None:
                return ModelResolution(ModelConnection(model_id, pick.label, settings))
        names = {entry.model: entry.label for entry in pick.catalog.models}
        return ModelResolution(
            item,
            ModelFallback(
                picked=pick.label,
                used=names.get(item.settings.model, item.settings.model),
                reason=PENDING if pick.entry is not None else REMOVED,
            ),
        )

    def keep(
        self, model_id: str | None, model: str | None, *, explicit: bool = False
    ) -> str | None:
        """The model an owl moving here keeps: the same id when it is offered here, otherwise
        the one offer here of the same model, otherwise none (this place's own).

        With ``explicit`` (a routine's model, where none means its owl's), this place's own
        model is kept by its id.
        """
        if model_id and self.offers(model_id):
            return model_id
        if not model:
            return None
        rows = self.list()
        own = next((row for row in rows if row["default"] and row["model"] == model), None)
        if own is not None:
            return own["id"] if explicit else None
        same = [row["id"] for row in rows if row["model"] == model and row["available"]]
        return same[0] if len(same) == 1 else None

    def supports_effort(
        self, model_id: str | None, effort: str, *, model_name: str | None = None
    ) -> bool:
        """Check the chosen model itself; a catalog fallback cannot inherit its effort."""
        if model_id is None:
            connection = self.default()
        else:
            resolved = self.resolve(model_id)
            connection = resolved.connection if resolved and not resolved.fallback else None
        return bool(
            connection
            and (model_name is None or connection.settings.model == model_name)
            and any(option["id"] == effort for option in effort_choices(connection.settings))
        )

    def model_of(self, model_id: str) -> str | None:
        """The model ``model_id`` names here, without reading any catalog; None when unknown."""
        connections = self._connections()
        exact = next((item for item in connections if item.id == model_id), None)
        if exact is not None:
            return exact.settings.model
        name, separator, model = model_id.partition("/")
        if separator and model and any(item.id == name for item in connections):
            return model
        return None

    def default(self) -> ModelConnection | None:
        """The place's own model connection: the one owls without a model of their own use."""
        return next((item for item in self._connections() if item.default), None)

    def client(self, task_id: str, connection: ModelConnection) -> ChatClient:
        return self._client(task_id, connection.settings)


@dataclass(frozen=True)
class _CatalogPick:
    """A ``<connection>/<model>`` id: its connection, the model, and what the catalog says."""

    connection: ModelConnection
    model: str
    catalog: ModelCatalog
    entry: CatalogModel | None

    @classmethod
    def of(cls, connections: list[ModelConnection], model_id: str) -> _CatalogPick | None:
        name, separator, model = model_id.partition("/")
        item = next((item for item in connections if item.id == name), None)
        if item is None or not separator or not model:
            return None
        if model == item.settings.model:
            return cls(item, model, ModelCatalog(), None)
        catalog = _catalog(item)
        entry = next((entry for entry in catalog.models if entry.model == model), None)
        return cls(item, model, catalog, entry)

    @property
    def own(self) -> bool:
        """The connection's own model, named by its catalog id."""
        return self.model == self.connection.settings.model

    @property
    def label(self) -> str:
        return self.entry.label if self.entry is not None else self.model


class CachedCatalog:
    """A provider's catalog for one account, read at most once per ``fresh_s``.

    While a new read fails, the last good one stands (retried after ``retry_s``); before any
    read worked, the catalog is unread. One read at a time.
    """

    def __init__(
        self,
        read: Callable[[], list[CatalogModel] | None],
        *,
        fresh_s: float = 60.0,
        retry_s: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read, self._fresh_s, self._retry_s, self._clock = read, fresh_s, retry_s, clock
        self._lock = threading.Lock()
        self._catalog = ModelCatalog(read=False)
        self._next = float("-inf")

    def __call__(self) -> ModelCatalog:
        with self._lock:
            now = self._clock()
            if now >= self._next:
                models = self._read()
                if models is not None:
                    self._catalog = ModelCatalog(tuple(models))
                    self._next = now + self._fresh_s
                else:
                    self._next = now + self._retry_s
            return self._catalog
