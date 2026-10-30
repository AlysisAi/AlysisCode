import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from alysis_code.llm.metadata import build_provider_route_identity
from alysis_code.llm.types import LLMUsage, UsageConfidence, UsageSource
from alysis_code.provider_usage import (
    capture_provider_usage,
    restore_provider_usage,
    session_provider_usage,
)
from alysis_code.session_store import SessionStore


def client():
    return SimpleNamespace(
        model="model",
        base_url="https://provider.example/v1",
        provider_key="custom",
        route_identity=build_provider_route_identity(
            protocol="openai_compat",
            provider_key="custom",
            base_url="https://provider.example/v1",
            model="model",
            credential_scope="test-account",
            session_scope="first",
        ),
    )


def test_provider_reading_preserves_counts_zeroes_and_missing_values():
    usage = LLMUsage(12345, 30, 12375, cached_prompt_tokens=0, reasoning_tokens=7)
    reading = capture_provider_usage(usage, client=client())
    assert reading["input_tokens"] == 12345
    assert reading["cached_tokens"] == 0
    assert reading["cache_write_tokens"] is None
    assert reading["output_tokens"] == 30
    assert reading["reasoning_tokens"] == 7
    assert reading["total_tokens"] == 12375
    assert capture_provider_usage(None, client=client()) is None
    assert capture_provider_usage(LLMUsage(None, None, None), client=client()) is None
    for source, confidence in [
        (UsageSource.LOCAL_ESTIMATE, UsageConfidence.ESTIMATED),
        (UsageSource.PROVIDER_COUNT, UsageConfidence.AUTHORITATIVE),
        (UsageSource.MIXED, UsageConfidence.REPORTED),
        (UsageSource.PROVIDER_RESPONSE, UsageConfidence.ESTIMATED),
    ]:
        assert (
            capture_provider_usage(
                replace(usage, source=source, confidence=confidence), client=client()
            )
            is None
        )


def test_persisted_reading_retains_measurement_time_without_raw_data(tmp_path):
    route = client()
    reading = capture_provider_usage(
        LLMUsage(100, 10, 110, raw_provider_usage={"private": "do not persist"}), client=route
    )
    store = SessionStore(
        enabled=True, sessions_dir=tmp_path, session_id="chat", cwd=str(tmp_path), repo_root=None
    )
    store.append("llm_usage", {"provider_context": reading})
    store.append("llm_usage", {"operation": "compaction", "prompt_tokens": 999})
    store.close()
    restored = SimpleNamespace(client=route)
    restore_provider_usage(restored, tmp_path / "chat.jsonl")
    assert restored.last_provider_usage == reading
    result = session_provider_usage(restored)
    assert result["measured_at"] == reading["measured_at"]
    assert "route_id" not in result
    assert "private" not in str(result)
    assert "do not persist" not in (tmp_path / "chat.jsonl").read_text()
    # A fresh process/session must still recognize the same configured route.
    route.route_identity = replace(route.route_identity, session_scope="resumed")
    assert session_provider_usage(restored) == result
    route.route_identity = replace(route.route_identity, credential_scope="other-account")
    assert session_provider_usage(restored) is None


@pytest.mark.parametrize("changed", ["model", "base_url", "provider_key"])
def test_changed_route_cannot_reuse_a_provider_reading(changed):
    route = client()
    session = SimpleNamespace(
        client=route,
        last_provider_usage=capture_provider_usage(LLMUsage(100, 10, 110), client=route),
    )
    setattr(route, changed, "other")
    assert session_provider_usage(session) is None


def test_cli_details_keep_last_response_counts_separate_from_local_projection():
    from alysis_code.cli_impl.commands.startup import _chat_provider_usage_rows

    route = client()
    session = SimpleNamespace(
        client=route,
        _hud_context_cache=SimpleNamespace(context_window_tokens=10000, used_input_tokens=9000),
        last_provider_usage=capture_provider_usage(
            LLMUsage(4000, 30, 4030, cached_prompt_tokens=3000, reasoning_tokens=20), client=route
        ),
    )
    rows = {label: value for label, value, _ in _chat_provider_usage_rows(session)}
    assert rows["input (includes cache)"] == "4,000"
    assert rows["cache read"] == "3,000"
    assert rows["cache write"] == "Not reported"
    assert rows["output (includes reasoning)"] == "30"
    assert rows["total"] == "4,030"
    assert rows["window left % (last request)"] == "60.0%"
    assert rows["measured at"] == session.last_provider_usage["measured_at"]
    session.last_provider_usage["input_scope"] = "interaction"
    rows = {label: value for label, value, _ in _chat_provider_usage_rows(session)}
    assert rows["input (includes cache)"] == "4,000"
    assert "window left % (last request)" not in rows


def attributed_usage():
    return LLMUsage(
        1000,
        20,
        1020,
        raw_provider_usage={
            "attribution": {
                "request_fields": {
                    "instructions": {"input_tokens": 200, "cached_tokens": 200},
                    "tools": {"input_tokens": 300, "cached_tokens": 300},
                },
                "items": {
                    "private-item-id": {"input_tokens": 500, "content": [{"input_tokens": 500}]},
                    "output-item-id": {"input_tokens": 0, "output_tokens": 20},
                },
            }
        },
    )


def test_provider_attribution_is_a_complete_partition_without_private_ids(tmp_path):
    from alysis_code.cli_impl.commands.startup import _chat_provider_usage_rows

    route = client()
    reading = capture_provider_usage(attributed_usage(), client=route)
    assert reading["input_breakdown"] == {"instructions": 200, "tools": 300, "conversation": 500}
    assert "private-item-id" not in str(reading)
    assert "content" not in str(reading)
    session = SimpleNamespace(client=route, last_provider_usage=reading)
    assert session_provider_usage(session)["input_breakdown"] == reading["input_breakdown"]
    rows = {label: value for label, value, _ in _chat_provider_usage_rows(session)}
    assert rows["input / tools (provider)"] == "300"
    assert rows["input / conversation (provider)"] == "500"


@pytest.mark.parametrize(
    "invalid", ["missing", "partial", "inconsistent", "boolean", "negative", "aggregate"]
)
def test_partial_or_invalid_attribution_is_never_completed_with_local_estimates(invalid):
    usage = copy.deepcopy(attributed_usage())
    raw = usage.raw_provider_usage
    if invalid == "missing":
        raw.pop("attribution")
    elif invalid == "partial":
        raw["attribution"]["items"]["private-item-id"].pop("input_tokens")
    elif invalid == "aggregate":
        usage = replace(usage, input_tokens_scope="interaction")
    else:
        raw["attribution"]["request_fields"]["tools"]["input_tokens"] = {
            "inconsistent": 301,
            "boolean": True,
            "negative": -1,
        }[invalid]
    reading = capture_provider_usage(usage, client=client())
    assert reading["input_tokens"] == 1000
    assert "input_breakdown" not in reading
