from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..atomic_io import atomic_write_text
from ..config import (
    AppConfig,
    ConfigError,
    get_api_key,
    resolve_llm_enable_thinking,
    resolve_llm_timeout_s,
    resolve_model_access_api_key,
    resolve_profile_api_key,
    resolve_prompt_cache_key,
    resolve_prompt_cache_retention,
    resolve_role_temperature,
)
from ..forge import RunPaths, now_iso
from ..llm.factory import make_llm_client
from ..llm.types import LLMError, LLMResponse
from ..model_registry import ModelRegistry
from ..model_router import ROLE_COMPREHENSION, resolve_model_for_role
from ..profiles import ProfileSpec, get_profile
from ..project_data import project_artifact_path
from .index import AssetIndex
from .models import (
    AssetError,
    AssetRecord,
    ComprehensionData,
    ComprehensionRecord,
)
from .ocr import OcrProvider, OcrResult
from .paths import asset_extracted_text_path, asset_preview_path, repo_rel
from .prompts import (
    build_retry_prompt,
    build_text_comprehension_prompt,
    build_vision_comprehension_prompt,
)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_MAX_FAILURE_ERROR_CHARS = 500
_TOP_LEVEL_KEYS = {"detected_language", "language_confidence", "data"}
_DATA_KEYS = {
    "semantic_summary",
    "classification",
    "key_entities",
    "stated_facts",
    "stated_decisions",
    "stated_constraints",
    "actionable_signals",
    "open_questions",
    "relations_hint",
    "confidence",
}
_CLASSIFICATION_KEYS = {"kind", "subkind", "domain"}
_COMPREHENSION_TOOL_NAME = "record_asset_comprehension"
_COMPREHENSION_TOOL_CHOICE = {
    "type": "function",
    "function": {"name": _COMPREHENSION_TOOL_NAME},
}
_COMPREHENSION_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": _COMPREHENSION_TOOL_NAME,
        "description": "Persist the structured comprehension for a user-provided asset.",
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["detected_language", "language_confidence", "data"],
            "properties": {
                "detected_language": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "ISO 639-1 primary language code, or null if unknown.",
                },
                "language_confidence": {
                    "anyOf": [{"type": "number"}, {"type": "null"}],
                    "description": "Confidence from 0.0 to 1.0, or null if unknown.",
                },
                "data": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "semantic_summary",
                        "classification",
                        "key_entities",
                        "stated_facts",
                        "stated_decisions",
                        "stated_constraints",
                        "actionable_signals",
                        "open_questions",
                        "relations_hint",
                        "confidence",
                    ],
                    "properties": {
                        "semantic_summary": {"type": "string"},
                        "classification": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["kind", "subkind", "domain"],
                            "properties": {
                                "kind": {"type": "string"},
                                "subkind": {"type": "string"},
                                "domain": {"type": "string"},
                            },
                        },
                        "key_entities": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["type", "value"],
                                "properties": {
                                    "type": {"type": "string"},
                                    "value": {"type": "string"},
                                },
                            },
                        },
                        "stated_facts": {"type": "array", "items": {"type": "string"}},
                        "stated_decisions": {"type": "array", "items": {"type": "string"}},
                        "stated_constraints": {"type": "array", "items": {"type": "string"}},
                        "actionable_signals": {"type": "array", "items": {"type": "string"}},
                        "open_questions": {"type": "array", "items": {"type": "string"}},
                        "relations_hint": {"type": "string"},
                        "confidence": {
                            "type": "object",
                            "additionalProperties": {"type": "number"},
                        },
                    },
                },
            },
        },
    },
}
LOGGER = logging.getLogger(__name__)


class AssetComprehender:
    def __init__(
        self,
        *,
        cfg: AppConfig,
        model_registry: ModelRegistry,
        ocr_provider: OcrProvider | None,
        run_paths: RunPaths,
    ) -> None:
        self.cfg = cfg
        self.model_registry = model_registry
        self.ocr_provider = ocr_provider
        self.run_paths = run_paths
        self.index = AssetIndex(run_paths)

    def comprehend(
        self,
        asset: AssetRecord,
        *,
        previous: ComprehensionRecord | None = None,
        angle: str | None = None,
    ) -> ComprehensionRecord:
        _ = previous
        started = time.monotonic()
        try:
            if asset.kind == "text":
                return self._comprehend_text_asset(asset=asset, started=started, angle=angle)
            if asset.kind == "image":
                return self._comprehend_image_asset(asset=asset, started=started, angle=angle)
            raise AssetError(f"Unsupported asset kind: {asset.kind}")
        except Exception as exc:  # noqa: BLE001 - failures must become durable records
            failed = self._failed_record(asset=asset, started=started, error=str(exc))
            return self.index.write_comprehension(failed)

    def comprehend_async_threadable(
        self,
        asset: AssetRecord,
        callback: Callable[[ComprehensionRecord], None],
    ) -> threading.Thread:
        def _target() -> None:
            callback(self.comprehend(asset))

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        return thread

    def _comprehend_text_asset(
        self,
        *,
        asset: AssetRecord,
        started: float,
        angle: str | None,
    ) -> ComprehensionRecord:
        role, model, profile = self._primary_model()
        if not _has_inspection_angle(angle):
            cached = self._cache_hit(asset=asset, model=model, role=role, started=started)
            if cached is not None:
                return cached
        text = self._load_extracted_text(asset)
        prompt = build_text_comprehension_prompt(
            asset=asset,
            text=text,
            questioning_mode=self.cfg.assets.comprehension.questioning_mode,
            angle=angle,
        )
        record = self._llm_comprehend(
            asset=asset,
            prompt=prompt,
            source="text_only",
            model=model,
            role=role,
            profile=profile,
            started=started,
            confidence_modifier=1.0,
        )
        return self.index.write_comprehension(record)

    def _comprehend_image_asset(
        self,
        *,
        asset: AssetRecord,
        started: float,
        angle: str | None,
    ) -> ComprehensionRecord:
        role, primary_model, primary_profile = self._primary_model()
        if self.model_registry.get(primary_model).supports_vision:
            if not _has_inspection_angle(angle):
                cached = self._cache_hit(
                    asset=asset,
                    model=primary_model,
                    role=role,
                    started=started,
                )
                if cached is not None:
                    return cached
            return self._vision_comprehend(
                asset=asset,
                model=primary_model,
                role=role,
                profile=primary_profile,
                started=started,
                angle=angle,
            )

        fallback = self._vision_fallback_model()
        if fallback is not None:
            fallback_role, fallback_model, fallback_profile = fallback
            if not _has_inspection_angle(angle):
                cached = self._cache_hit(
                    asset=asset,
                    model=fallback_model,
                    role=fallback_role,
                    started=started,
                )
                if cached is not None:
                    return cached
            return self._vision_comprehend(
                asset=asset,
                model=fallback_model,
                role=fallback_role,
                profile=fallback_profile,
                started=started,
                angle=angle,
            )

        ocr_result = self._try_ocr(asset)
        if ocr_result is not None and ocr_result.text.strip():
            extracted_path = asset_extracted_text_path(self.run_paths, asset.id)
            atomic_write_text(extracted_path, ocr_result.text)
            updated_asset = self.index.update(
                replace(asset, extracted_text_path=repo_rel(self.run_paths.root, extracted_path))
            )
            if not _has_inspection_angle(angle):
                cached = self._cache_hit(
                    asset=updated_asset,
                    model=primary_model,
                    role=role,
                    started=started,
                )
                if cached is not None:
                    return cached
            prompt = build_text_comprehension_prompt(
                asset=updated_asset,
                text=ocr_result.text,
                questioning_mode=self.cfg.assets.comprehension.questioning_mode,
                angle=angle,
            )
            record = self._llm_comprehend(
                asset=updated_asset,
                prompt=prompt,
                source="ocr_only",
                model=primary_model,
                role=role,
                profile=primary_profile,
                started=started,
                confidence_modifier=0.7,
                ocr_result=ocr_result,
            )
            return self.index.write_comprehension(record)

        minimal = self._minimal_record(asset=asset, started=started)
        return self.index.write_comprehension(minimal)

    def _vision_comprehend(
        self,
        *,
        asset: AssetRecord,
        model: str,
        role: str,
        profile: ProfileSpec | None,
        started: float,
        angle: str | None,
    ) -> ComprehensionRecord:
        ocr_result: OcrResult | None = None
        combine_with_ocr = (
            self.cfg.assets.comprehension.vision_with_ocr_when_available
            and self._ocr_allowed()
            and self.ocr_provider is not None
            and self.ocr_provider.is_available()
        )
        if combine_with_ocr:
            ocr_result = self._try_ocr(asset)
            if ocr_result is not None and ocr_result.text.strip():
                extracted_path = asset_extracted_text_path(self.run_paths, asset.id)
                atomic_write_text(extracted_path, ocr_result.text)
                asset = self.index.update(
                    replace(
                        asset, extracted_text_path=repo_rel(self.run_paths.root, extracted_path)
                    )
                )
        preview_path = self._normalized_preview(asset)
        prompt = build_vision_comprehension_prompt(
            asset=asset,
            questioning_mode=self.cfg.assets.comprehension.questioning_mode,
            ocr_text=ocr_result.text if ocr_result is not None else None,
            angle=angle,
        )
        source = (
            "vision_with_ocr" if ocr_result is not None and ocr_result.text.strip() else "vision"
        )
        record = self._llm_comprehend(
            asset=asset,
            prompt=prompt,
            source=source,
            model=model,
            role=role,
            profile=profile,
            started=started,
            confidence_modifier=1.0,
            ocr_result=ocr_result,
            image_path=preview_path,
        )
        return self.index.write_comprehension(record)

    def _llm_comprehend(
        self,
        *,
        asset: AssetRecord,
        prompt: str,
        source: str,
        model: str,
        role: str,
        profile: ProfileSpec | None,
        started: float,
        confidence_modifier: float,
        ocr_result: OcrResult | None = None,
        image_path: Path | None = None,
    ) -> ComprehensionRecord:
        response = self._chat(
            model=model, role=role, profile=profile, prompt=prompt, image_path=image_path
        )
        try:
            payload = _parse_comprehension_payload(response)
        except AssetError:
            retry = self._chat(
                model=model,
                role=role,
                profile=profile,
                prompt=build_retry_prompt(prompt),
                image_path=image_path,
            )
            payload = _parse_comprehension_payload(retry)
            response = retry
        data = ComprehensionData.from_dict(payload.get("data"))
        usage = _tokens_used(response)
        return ComprehensionRecord(
            schema_version=1,
            version=0,
            asset_id=asset.id,
            status="ready",
            source=source,  # type: ignore[arg-type]
            model=model,
            role=role,
            ocr_engine=ocr_result.engine_version if ocr_result is not None else None,
            ocr_languages_used=ocr_result.languages_used if ocr_result is not None else [],
            detected_language=_optional_string(payload.get("detected_language")),
            language_confidence=_optional_float(payload.get("language_confidence")),
            confidence_modifier=confidence_modifier,
            tokens_used=usage,
            elapsed_ms=_elapsed_ms(started),
            generated_at=now_iso(),
            error=None,
            data=data,
        )

    def _chat(
        self,
        *,
        model: str,
        role: str,
        profile: ProfileSpec | None,
        prompt: str,
        image_path: Path | None,
    ) -> LLMResponse:
        api_key = self._api_key_for_profile(profile)
        client = make_llm_client(
            cfg=self.cfg,
            api_key=api_key,
            model=model,
            timeout_s=resolve_llm_timeout_s(self.cfg),
            temperature=resolve_role_temperature(self.cfg, role=role),
            prompt_cache_key=resolve_prompt_cache_key(self.cfg),
            prompt_cache_retention=resolve_prompt_cache_retention(self.cfg),
            enable_thinking=resolve_llm_enable_thinking(self.cfg),
            profile=profile,
        )
        messages = [{"role": "system", "content": "Return structured JSON only."}]
        if image_path is None:
            messages.append({"role": "user", "content": prompt})
        else:
            messages.append({"role": "user", "content": _image_content_parts(prompt, image_path)})
        try:
            return client.chat(
                messages=messages,
                temperature=resolve_role_temperature(self.cfg, role=role),
                tools=[_COMPREHENSION_TOOL],
                tool_choice=_COMPREHENSION_TOOL_CHOICE,
            )
        except LLMError as exc:
            if not _structured_tool_call_unsupported(exc):
                raise
            LOGGER.warning(
                "Structured tool-call comprehension was rejected by the provider; retrying with JSON mode."
            )
            return client.chat(
                messages=messages,
                temperature=resolve_role_temperature(self.cfg, role=role),
                response_format={"type": "json_object"},
            )

    def _api_key_for_profile(self, profile: ProfileSpec | None) -> str:
        if profile is None:
            return resolve_model_access_api_key(self.cfg, legacy_resolver=get_api_key)
        if profile.auth_provider:
            return ""
        resolved = resolve_profile_api_key(self.cfg, profile.name)
        if not resolved.key:
            raise ConfigError(f"Missing API key for profile {profile.name}.")
        return resolved.key

    def _primary_model(self) -> tuple[str, str, ProfileSpec | None]:
        role = str(self.cfg.assets.comprehension.role or ROLE_COMPREHENSION).strip().lower()
        model = resolve_model_for_role(cfg=self.cfg, role=role, plan=None)
        return role, model, None

    def _vision_fallback_model(self) -> tuple[str, str, ProfileSpec] | None:
        profile_name = str(self.cfg.assets.comprehension.vision_fallback_profile or "").strip()
        if not profile_name:
            return None
        profile = get_profile(self.cfg, profile_name)
        if profile is None or not profile.default_model:
            return None
        if not self.model_registry.get(profile.default_model).supports_vision:
            return None
        return ROLE_COMPREHENSION, profile.default_model, profile

    def _cache_hit(
        self,
        *,
        asset: AssetRecord,
        model: str | None,
        role: str | None,
        started: float,
    ) -> ComprehensionRecord | None:
        cached = self.index.find_existing_comprehension_by_sha256(
            asset.sha256,
            model=model,
            role=role,
        )
        if cached is None:
            return None
        cloned = replace(
            cached,
            version=0,
            asset_id=asset.id,
            elapsed_ms=_elapsed_ms(started),
            generated_at=now_iso(),
        )
        return self.index.write_comprehension(cloned)

    def _try_ocr(self, asset: AssetRecord) -> OcrResult | None:
        if (
            not self._ocr_allowed()
            or self.ocr_provider is None
            or not self.ocr_provider.is_available()
        ):
            return None
        try:
            return self.ocr_provider.extract_text(
                project_artifact_path(self.run_paths.root, asset.stored_path)
            )
        except Exception as exc:
            if self.cfg.assets.comprehension.ocr_enabled == "always":
                raise
            LOGGER.warning("Asset OCR failed for %s: %s", asset.id, exc)
            return None

    def _ocr_allowed(self) -> bool:
        return self.cfg.assets.comprehension.ocr_enabled != "never"

    def _load_extracted_text(self, asset: AssetRecord) -> str:
        path = asset.extracted_text_path or asset.stored_path
        return project_artifact_path(self.run_paths.root, path).read_text(encoding="utf-8")

    def _normalized_preview(self, asset: AssetRecord) -> Path:
        try:
            from PIL import Image
        except ModuleNotFoundError as e:
            raise AssetError("Pillow is required for image comprehension.") from e
        source = project_artifact_path(self.run_paths.root, asset.stored_path)
        destination = asset_preview_path(self.run_paths, asset.id)
        max_edge = int(self.cfg.assets.comprehension.image_max_edge_pixels)
        with Image.open(source) as image:
            normalized = image.convert("RGB")
            normalized.thumbnail((max_edge, max_edge))
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
            )
            temp_path = Path(temp_name)
            os.close(fd)
            try:
                normalized.save(temp_path, format="PNG")
                os.replace(temp_path, destination)
            finally:
                with suppress(FileNotFoundError):
                    temp_path.unlink()
        return destination

    def _minimal_record(self, *, asset: AssetRecord, started: float) -> ComprehensionRecord:
        has_description = bool(asset.description.strip())
        source = "user_description" if has_description else "minimal"
        confidence_modifier = 0.5 if has_description else 0.3
        summary_parts = [asset.title]
        if has_description:
            summary_parts.append(asset.description)
        return ComprehensionRecord(
            schema_version=1,
            version=0,
            asset_id=asset.id,
            status="minimal",
            source=source,
            model=None,
            role=None,
            ocr_engine=None,
            ocr_languages_used=[],
            detected_language=None,
            language_confidence=None,
            confidence_modifier=confidence_modifier,
            tokens_used={},
            elapsed_ms=_elapsed_ms(started),
            generated_at=now_iso(),
            error=None,
            data=ComprehensionData(
                semantic_summary="\n".join(part for part in summary_parts if part.strip()),
                classification={"kind": asset.kind, "subkind": "", "domain": ""},
                confidence={"summary": confidence_modifier, "classification": confidence_modifier},
            ),
        )

    def _failed_record(
        self,
        *,
        asset: AssetRecord,
        started: float,
        error: str,
    ) -> ComprehensionRecord:
        return ComprehensionRecord(
            schema_version=1,
            version=0,
            asset_id=asset.id,
            status="failed",
            source="minimal",
            model=None,
            role=None,
            ocr_engine=None,
            ocr_languages_used=[],
            detected_language=None,
            language_confidence=None,
            confidence_modifier=0.0,
            tokens_used={},
            elapsed_ms=_elapsed_ms(started),
            generated_at=now_iso(),
            error=error.strip()[:_MAX_FAILURE_ERROR_CHARS] or "unknown error",
            data=ComprehensionData(),
        )


def _parse_comprehension_payload(response: LLMResponse) -> dict[str, Any]:
    if response.tool_calls:
        payload = None
        for tool_call in response.tool_calls:
            if tool_call.name == _COMPREHENSION_TOOL_NAME:
                payload = tool_call.arguments
                break
        if payload is None:
            raise AssetError(f"LLM did not call required tool: {_COMPREHENSION_TOOL_NAME}.")
    else:
        match = _JSON_OBJECT_RE.search(response.content.strip())
        if not match:
            raise AssetError("LLM did not return a JSON object.")
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError as e:
            raise AssetError("LLM returned malformed JSON.") from e
    if not isinstance(payload, dict):
        raise AssetError("LLM comprehension payload must be a JSON object.")
    _require_exact_keys(payload, _TOP_LEVEL_KEYS, "LLM comprehension payload")
    if not isinstance(payload.get("data"), dict):
        raise AssetError("LLM comprehension payload must contain data object.")
    _validate_comprehension_data(payload["data"])
    detected_language = payload.get("detected_language")
    if detected_language is not None and not isinstance(detected_language, str):
        raise AssetError("LLM comprehension detected_language must be a string or null.")
    language_confidence = payload.get("language_confidence")
    if language_confidence is not None and not isinstance(language_confidence, int | float):
        raise AssetError("LLM comprehension language_confidence must be numeric or null.")
    return payload


def _has_inspection_angle(angle: str | None) -> bool:
    return bool(str(angle or "").strip())


def _structured_tool_call_unsupported(error: LLMError) -> bool:
    text = str(error).casefold()
    if "tool" not in text and "function" not in text:
        return False
    return any(token in text for token in ("unsupported", "unknown", "invalid", "not allowed"))


def _validate_comprehension_data(data: dict[str, Any]) -> None:
    _require_exact_keys(data, _DATA_KEYS, "LLM comprehension data")
    if not isinstance(data["semantic_summary"], str):
        raise AssetError("LLM comprehension data.semantic_summary must be a string.")
    if not isinstance(data["relations_hint"], str):
        raise AssetError("LLM comprehension data.relations_hint must be a string.")
    classification = data["classification"]
    if not isinstance(classification, dict):
        raise AssetError("LLM comprehension data.classification must be an object.")
    _require_exact_keys(classification, _CLASSIFICATION_KEYS, "LLM comprehension classification")
    if not all(isinstance(value, str) for value in classification.values()):
        raise AssetError("LLM comprehension classification values must be strings.")
    _validate_entity_list(data["key_entities"])
    for field_name in (
        "stated_facts",
        "stated_decisions",
        "stated_constraints",
        "actionable_signals",
        "open_questions",
    ):
        _validate_string_list(data[field_name], f"LLM comprehension data.{field_name}")
    confidence = data["confidence"]
    if not isinstance(confidence, dict):
        raise AssetError("LLM comprehension data.confidence must be an object.")
    for key, value in confidence.items():
        if not isinstance(key, str) or not isinstance(value, int | float):
            raise AssetError("LLM comprehension confidence must map string keys to numeric values.")


def _require_exact_keys(payload: dict[str, Any], expected: set[str], label: str) -> None:
    keys = set(payload)
    missing = sorted(expected - keys)
    unknown = sorted(keys - expected)
    if missing:
        raise AssetError(f"{label} is missing keys: {missing}")
    if unknown:
        raise AssetError(f"{label} has unexpected keys: {unknown}")


def _validate_entity_list(value: Any) -> None:
    if not isinstance(value, list):
        raise AssetError("LLM comprehension data.key_entities must be an array.")
    for item in value:
        if not isinstance(item, dict):
            raise AssetError("LLM comprehension key_entities items must be objects.")
        if set(item) != {"type", "value"}:
            raise AssetError("LLM comprehension key_entities items need type and value keys.")
        if not all(isinstance(raw_value, str) for raw_value in item.values()):
            raise AssetError("LLM comprehension key_entities values must be strings.")


def _validate_string_list(value: Any, label: str) -> None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AssetError(f"{label} must be an array of strings.")


def _tokens_used(response: LLMResponse) -> dict[str, int]:
    usage = response.usage
    if usage is None:
        return {}
    out: dict[str, int] = {}
    if usage.prompt_tokens is not None:
        out["input"] = int(usage.prompt_tokens)
    if usage.completion_tokens is not None:
        out["output"] = int(usage.completion_tokens)
    return out


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _image_content_parts(prompt: str, image_path: Path) -> list[dict[str, Any]]:
    mime, _ = mimetypes.guess_type(image_path.name)
    image_mime = mime if mime and mime.startswith("image/") else "image/png"
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return [
        {
            "type": "image_url",
            "image_url": {"url": f"data:{image_mime};base64,{encoded}"},
        },
        {"type": "text", "text": prompt},
    ]
