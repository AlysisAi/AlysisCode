"""Pictures made with the Alysis account: the image service the account declares.

The account's image service, beside the model gateway (alysis_cloud.images_base_url), answers
``GET /capabilities`` for an account key with what that account may use:

    {"available": true,
     "models": {"generate": "<model id>", "edit": "<model id>"},
     "qualities": ["low", "medium", ...],
     "max_images_per_call": 1, "timeout_s": 300}

or ``{"available": false, "reason": "..."}``. The models and qualities come from that answer;
nothing here names a model. The person's own picture quality setting is kept and applied when a
request goes out (tools/image_generation.resolve_quality).

Which image generation a model connection declares (connection_image_generation):
1. The person's own image provider, when image generation is on with an endpoint of its own.
2. For a connection to the account gateway, what the account declares, with that connection's
   key.
3. The person's image settings, when they are on, for a connection the request can reach: one
   that speaks the OpenAI-compatible protocol, as the image request does.
4. Otherwise, when the person is signed in to Alysis, what their account declares, whatever
   model they think with: the request carries the saved account key, never the connection's.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from . import alysis_cloud as cloud
from .config import IMAGE_QUALITIES, ImageGenerationConfig
from .llm.protocols import OPENAI_COMPAT_PROTOCOL
from .provider_url import is_alysis_gateway_url

_TIMEOUT_S = 4.0
_MAX_BYTES = 65_536
# An answer, or the lack of one, is kept this long per key: the plan rarely changes, and a
# session or owl turn must not wait on the image service each time.
_KEEP_S = 600.0
_MAX_MODEL_ID = 200

Fetch = Callable[[str], "dict[str, Any] | None"]

_lock = threading.Lock()
_answers: dict[str, tuple[float, dict[str, Any] | None]] = {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        return None


def _fetch(key: str) -> dict[str, Any] | None:
    """The image service's answer for ``key``; None when it could not be read."""
    try:
        request = urllib.request.Request(
            f"{cloud.images_base_url()}/capabilities",
            method="GET",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
        with urllib.request.build_opener(_NoRedirect()).open(
            request, timeout=_TIMEOUT_S
        ) as response:
            raw = response.read(_MAX_BYTES + 1)
    except (
        cloud.AlysisCloudConfigError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
    ):
        return None
    if len(raw) > _MAX_BYTES:
        return None
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return body if isinstance(body, dict) else None


def _model_id(value: Any) -> str | None:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > _MAX_MODEL_ID or any(c.isspace() for c in text):
        return None
    return text


def saved_account_key() -> str | None:
    """The key the person signed in to Alysis with, if any."""
    from .config import load_persisted_profile_keys

    try:
        key = load_persisted_profile_keys().get(cloud.PROFILE_KEY)
    except Exception:  # noqa: BLE001 - an unreadable key store means not signed in
        return None
    return key.strip() if isinstance(key, str) and key.strip() else None


def declared_by_account(
    answer: dict[str, Any] | None,
    base: ImageGenerationConfig,
    *,
    key_from: str = "connection",
) -> ImageGenerationConfig | None:
    """The image generation an account answer declares, keeping the person's settings (their
    quality, limits and timeout); None when it declares none or the answer is malformed.
    ``key_from``: "connection" when the model connection is the account's own gateway,
    "account" when the saved account key goes with the request (any other connection)."""
    if not isinstance(answer, dict) or answer.get("available") is not True:
        return None
    models = answer.get("models")
    if not isinstance(models, dict):
        return None
    generate = _model_id(models.get("generate"))
    edit = _model_id(models.get("edit")) if models.get("edit") is not None else None
    if generate is None or (models.get("edit") is not None and edit is None):
        return None
    raw_qualities = answer.get("qualities")
    if not isinstance(raw_qualities, list) or not raw_qualities:
        return None
    qualities = tuple(
        dict.fromkeys(item.strip().casefold() for item in raw_qualities if isinstance(item, str))
    )
    # A quality this engine does not know is left out, not guessed at.
    qualities = tuple(item for item in qualities if item in IMAGE_QUALITIES)
    if not qualities:
        return None
    # The service may make fewer pictures per request than the person allows, and may need longer
    # than the person's timeout for its most detailed quality: the stricter count, the longer wait.
    most = answer.get("max_images_per_call")
    count = min(base.max_images_per_call, most) if type(most) is int and most >= 1 else None
    wait = answer.get("timeout_s")
    timeout = (
        min(max(base.timeout_s, float(wait)), 900.0)
        if type(wait) in (int, float) and wait > 0
        else base.timeout_s
    )
    try:
        return ImageGenerationConfig.model_validate(
            {
                **base.model_dump(),
                **({"max_images_per_call": count} if count is not None else {}),
                "timeout_s": timeout,
                "enabled": True,
                "model": generate,
                "edit_model": edit,
                "qualities": qualities,
                "base_url": cloud.images_base_url(),
                # The account key goes with the request, never another provider's.
                "api_key_env": None,
                "key_from": key_from,
            }
        )
    except (ValueError, cloud.AlysisCloudConfigError):
        return None


def account_answer(key: str, *, fetch: Fetch | None = None, now: float | None = None):
    """The image service's answer for ``key``, read at most once per _KEEP_S."""
    slot = hashlib.sha256(key.encode("utf-8")).hexdigest()
    moment = time.monotonic() if now is None else now
    with _lock:
        kept = _answers.get(slot)
        if kept is not None and moment - kept[0] < _KEEP_S:
            return kept[1]
    answer = (fetch or _fetch)(key)
    with _lock:
        _answers[slot] = (moment, answer)
    return answer


def picture_models(key: str, *, fetch: Fetch | None = None) -> frozenset[str]:
    """The models the account's image service declares for ``key``: they make pictures, so a
    model picker leaves them out. Empty when it declares none or could not be read."""
    answer = account_answer(key, fetch=fetch)
    if not isinstance(answer, dict) or answer.get("available") is not True:
        return frozenset()
    models = answer.get("models")
    if not isinstance(models, dict):
        return frozenset()
    return frozenset(
        model
        for model in (_model_id(models.get("generate")), _model_id(models.get("edit")))
        if model
    )


def forget_account_answers() -> None:
    """Read every account's answer again next time (after signing in or a plan change)."""
    with _lock:
        _answers.clear()


def connection_image_generation(
    config: ImageGenerationConfig,
    *,
    base_url: str | None,
    api_key: str | None,
    protocol: str = OPENAI_COMPAT_PROTOCOL,
    fetch: Fetch | None = None,
) -> ImageGenerationConfig | None:
    """The image generation a model connection declares, or None (see the module docstring)."""
    if config.enabled and config.base_url:
        return config
    if is_alysis_gateway_url(base_url):
        if not api_key:
            return None
        return declared_by_account(account_answer(api_key, fetch=fetch), config)
    if config.enabled and protocol == OPENAI_COMPAT_PROTOCOL:
        return config
    return signed_in_image_generation(config, fetch=fetch)


def signed_in_image_generation(
    config: ImageGenerationConfig, *, fetch: Fetch | None = None
) -> ImageGenerationConfig | None:
    """What the person's Alysis account declares, for any model connection; None when they are
    not signed in or the account declares no pictures."""
    key = saved_account_key()
    if not key:
        return None
    return declared_by_account(account_answer(key, fetch=fetch), config, key_from="account")


def session_image_generation(cfg: Any, *, fetch: Fetch | None = None) -> ImageGenerationConfig:
    """The image generation a chat session uses with its active model connection.

    The person's own image provider, or their image settings when they are on; on the account
    gateway, what the account declares; on any other connection without settings of their own,
    what their signed-in account declares.
    """
    from .config import resolve_profile_api_key
    from .profiles import get_active_profile, resolve_effective_base_url

    current = cfg.image_generation
    if current.enabled and current.base_url:
        return current
    try:
        profile = get_active_profile(cfg)
        base_url = resolve_effective_base_url(cfg=cfg, profile=profile)
    except Exception:  # noqa: BLE001 - a session without a usable profile keeps its settings
        return current
    if is_alysis_gateway_url(base_url):
        key = resolve_profile_api_key(cfg, profile.name).key
        declared = declared_by_account(account_answer(key, fetch=fetch), current) if key else None
        return declared or current.model_copy(update={"enabled": False})
    if current.enabled:
        return current
    # Any other connection: the signed-in account's pictures, with the account's own key.
    return signed_in_image_generation(current, fetch=fetch) or current
