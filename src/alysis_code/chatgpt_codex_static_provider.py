from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

from .model_metadata_utils import parse_positive_int

CHATGPT_CODEX_SUBSCRIPTION_CATALOG_SOURCE = "bundled_chatgpt_codex_subscription_snapshot"
_CATALOG_PACKAGE = "alysis_code.model_catalog"
_CATALOG_FILENAME = "chatgpt_codex_subscription_snapshot.json"


@dataclass(frozen=True, slots=True)
class ChatGPTCodexStaticModel:
    id: str
    label: str
    description: str
    priority: int
    context_window_tokens: int | None
    max_output_tokens: int | None
    input_modalities: tuple[str, ...]
    reasoning_efforts: tuple[tuple[str, str], ...]
    default_reasoning_effort: str | None


def _clean_string(value: Any) -> str:
    return str(value or "").strip()


def _parse_model(raw: Any) -> ChatGPTCodexStaticModel | None:
    if not isinstance(raw, dict):
        return None
    model_id = _clean_string(raw.get("id"))
    if not model_id:
        return None
    try:
        priority = int(raw.get("priority") or 9999)
    except (TypeError, ValueError):
        priority = 9999

    modalities_raw = raw.get("input_modalities")
    modalities = (
        tuple(
            dict.fromkeys(
                _clean_string(item).casefold() for item in modalities_raw if _clean_string(item)
            )
        )
        if isinstance(modalities_raw, list)
        else ()
    )
    if not modalities:
        modalities = ("text",)

    efforts: list[tuple[str, str]] = []
    efforts_raw = raw.get("reasoning_efforts")
    if isinstance(efforts_raw, list):
        for item in efforts_raw:
            if not isinstance(item, dict):
                continue
            effort_id = _clean_string(item.get("id"))
            if effort_id:
                efforts.append((effort_id, _clean_string(item.get("description"))))

    default_effort = _clean_string(raw.get("default_reasoning_effort")) or None
    return ChatGPTCodexStaticModel(
        id=model_id,
        label=_clean_string(raw.get("label")) or model_id,
        description=_clean_string(raw.get("description")),
        priority=priority,
        context_window_tokens=parse_positive_int(raw.get("context_window_tokens")),
        max_output_tokens=parse_positive_int(raw.get("max_output_tokens")),
        input_modalities=modalities,
        reasoning_efforts=tuple(efforts),
        default_reasoning_effort=default_effort,
    )


@lru_cache(maxsize=1)
def load_chatgpt_codex_static_models() -> tuple[ChatGPTCodexStaticModel, ...]:
    try:
        text = (
            resources.files(_CATALOG_PACKAGE)
            .joinpath(_CATALOG_FILENAME)
            .read_text(encoding="utf-8")
        )
        payload = json.loads(text)
    except (
        FileNotFoundError,
        ModuleNotFoundError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return ()
    if not isinstance(payload, dict):
        return ()
    if payload.get("source") != CHATGPT_CODEX_SUBSCRIPTION_CATALOG_SOURCE:
        return ()
    raw_models = payload.get("models")
    if not isinstance(raw_models, list):
        return ()
    models = tuple(model for raw in raw_models if (model := _parse_model(raw)) is not None)
    # Equal priorities keep the order OpenAI listed them in (sorted() is stable).
    return tuple(sorted(models, key=lambda model: model.priority))


def resolve_chatgpt_codex_static_model(model_name: str) -> ChatGPTCodexStaticModel | None:
    requested = _clean_string(model_name).casefold()
    if not requested:
        return None
    candidates = {requested}
    if "/" in requested:
        candidates.add(requested.rsplit("/", 1)[-1])
    return next(
        (
            model
            for model in load_chatgpt_codex_static_models()
            if model.id.casefold() in candidates
        ),
        None,
    )


__all__ = [
    "CHATGPT_CODEX_SUBSCRIPTION_CATALOG_SOURCE",
    "ChatGPTCodexStaticModel",
    "load_chatgpt_codex_static_models",
    "resolve_chatgpt_codex_static_model",
]
