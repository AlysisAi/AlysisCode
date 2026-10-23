"""Tests for sampling determinism controls and response provenance.

Runnable two ways:

    python3 tests/test_sampling_config.py     # standalone, stdlib only
    pytest tests/test_sampling_config.py

The module under test is loaded directly from its file path so that importing
it never executes ``alysis_code/__init__`` or any of the package's
dependency-heavy import chain. That keeps these tests runnable in a bare
interpreter with no third-party packages installed.

The payload fixtures below are shaped like the dicts
``OpenAICompatClient.chat`` actually builds -- key order included -- because
the load-bearing claim of this PR is that an unconfigured run still sends the
exact bytes it sent before, and key order is part of "exact bytes".
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "src" / "alysis_code" / "run_provenance.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_run_provenance", _MODULE_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because the module defines dataclasses, and
    # ``dataclasses`` resolves annotations via ``sys.modules[cls.__module__]``.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rp = _load_module()


# A request payload with the key order the transport produces: model and
# messages first, then temperature, then the tool surface, then streaming.
def _reference_payload() -> dict:
    return {
        "model": "MiMo-VL-7B-RL",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.2,
        "tools": [{"type": "function", "function": {"name": "shell"}}],
        "tool_choice": "auto",
        "stream": True,
        "stream_options": {"include_usage": True},
    }


def _serialize(payload: dict) -> str:
    # sort_keys is deliberately NOT set: order-sensitive, like the wire body.
    return json.dumps(payload, ensure_ascii=True)


class SamplingParsingTests(unittest.TestCase):
    def test_nothing_configured_yields_nothing(self) -> None:
        settings = rp.resolve_sampling_settings(config_values={}, environ={})
        self.assertIsNone(settings.temperature)
        self.assertIsNone(settings.top_p)
        self.assertIsNone(settings.seed)
        self.assertFalse(settings.is_configured)
        self.assertEqual(settings.warnings, ())

    def test_env_values_are_parsed(self) -> None:
        settings = rp.resolve_sampling_settings(
            config_values={},
            environ={
                rp.SAMPLING_TEMPERATURE_ENV: "0.0",
                rp.SAMPLING_TOP_P_ENV: "1.0",
                rp.SAMPLING_SEED_ENV: "1234",
            },
        )
        self.assertEqual(settings.temperature, 0.0)
        self.assertEqual(settings.top_p, 1.0)
        self.assertEqual(settings.seed, 1234)
        self.assertTrue(settings.is_configured)
        self.assertEqual(settings.warnings, ())

    def test_zero_temperature_is_configured_not_falsy(self) -> None:
        # The regression this guards: 0.0 is the single most useful
        # determinism setting and the easiest one to drop with a truthiness test.
        settings = rp.resolve_sampling_settings(
            config_values={},
            environ={rp.SAMPLING_TEMPERATURE_ENV: "0"},
        )
        self.assertEqual(settings.temperature, 0.0)
        self.assertTrue(settings.is_configured)

    def test_seed_zero_is_configured(self) -> None:
        settings = rp.resolve_sampling_settings(
            config_values={},
            environ={rp.SAMPLING_SEED_ENV: "0"},
        )
        self.assertEqual(settings.seed, 0)
        self.assertTrue(settings.is_configured)

    def test_env_beats_config(self) -> None:
        settings = rp.resolve_sampling_settings(
            config_values={rp.SAMPLING_TEMPERATURE_CONFIG_KEY: 0.7},
            environ={rp.SAMPLING_TEMPERATURE_ENV: "0.1"},
        )
        self.assertEqual(settings.temperature, 0.1)
        self.assertEqual(settings.sources["temperature"], f"env:{rp.SAMPLING_TEMPERATURE_ENV}")

    def test_canonical_env_name_is_used(self) -> None:
        settings = rp.resolve_sampling_settings(
            config_values={},
            environ={"ALYSIS_SAMPLING_TEMPERATURE": "0.1"},
        )
        self.assertEqual(settings.temperature, 0.1)
        self.assertEqual(settings.sources["temperature"], "env:ALYSIS_SAMPLING_TEMPERATURE")

    def test_config_used_when_env_absent(self) -> None:
        settings = rp.resolve_sampling_settings(
            config_values={rp.SAMPLING_SEED_CONFIG_KEY: 99},
            environ={},
        )
        self.assertEqual(settings.seed, 99)
        self.assertEqual(settings.sources["seed"], f"config:{rp.SAMPLING_SEED_CONFIG_KEY}")

    def test_blank_env_falls_through_to_config(self) -> None:
        settings = rp.resolve_sampling_settings(
            config_values={rp.SAMPLING_TOP_P_CONFIG_KEY: 0.5},
            environ={rp.SAMPLING_TOP_P_ENV: "   "},
        )
        self.assertEqual(settings.top_p, 0.5)

    def test_config_none_is_treated_as_unset(self) -> None:
        settings = rp.resolve_sampling_settings(
            config_values={
                rp.SAMPLING_TEMPERATURE_CONFIG_KEY: None,
                rp.SAMPLING_TOP_P_CONFIG_KEY: None,
                rp.SAMPLING_SEED_CONFIG_KEY: None,
            },
            environ={},
        )
        self.assertFalse(settings.is_configured)
        self.assertEqual(settings.warnings, ())


class SamplingValidationTests(unittest.TestCase):
    """Invalid input is ignored with a warning. Nothing here may raise."""

    def test_unparseable_values_are_ignored_with_warnings(self) -> None:
        settings = rp.resolve_sampling_settings(
            config_values={},
            environ={
                rp.SAMPLING_TEMPERATURE_ENV: "warm",
                rp.SAMPLING_TOP_P_ENV: "",
                rp.SAMPLING_SEED_ENV: "3.7",
            },
        )
        self.assertFalse(settings.is_configured)
        reasons = {warning.setting: warning.reason for warning in settings.warnings}
        self.assertEqual(reasons["temperature"], "not a number")
        self.assertEqual(reasons["seed"], "not an integer")
        # A blank value is "unset", not "invalid": no warning for top_p.
        self.assertNotIn("top_p", reasons)

    def test_out_of_range_values_are_ignored(self) -> None:
        settings = rp.resolve_sampling_settings(
            config_values={},
            environ={
                rp.SAMPLING_TEMPERATURE_ENV: "5.0",
                rp.SAMPLING_TOP_P_ENV: "-0.1",
            },
        )
        self.assertIsNone(settings.temperature)
        self.assertIsNone(settings.top_p)
        self.assertEqual(len(settings.warnings), 2)
        for warning in settings.warnings:
            self.assertIn("outside the accepted range", warning.reason)

    def test_non_finite_temperature_is_ignored(self) -> None:
        for raw in ("nan", "inf", "-inf", "Infinity"):
            with self.subTest(raw=raw):
                settings = rp.resolve_sampling_settings(
                    config_values={},
                    environ={rp.SAMPLING_TEMPERATURE_ENV: raw},
                )
                self.assertIsNone(settings.temperature)
                self.assertEqual(len(settings.warnings), 1)

    def test_range_boundaries_are_inclusive(self) -> None:
        settings = rp.resolve_sampling_settings(
            config_values={},
            environ={
                rp.SAMPLING_TEMPERATURE_ENV: "2.0",
                rp.SAMPLING_TOP_P_ENV: "0.0",
            },
        )
        self.assertEqual(settings.temperature, 2.0)
        self.assertEqual(settings.top_p, 0.0)
        self.assertEqual(settings.warnings, ())

    def test_oversized_seed_is_ignored(self) -> None:
        settings = rp.resolve_sampling_settings(
            config_values={},
            environ={rp.SAMPLING_SEED_ENV: str(2**64)},
        )
        self.assertIsNone(settings.seed)
        self.assertEqual(settings.warnings[0].reason, "outside the accepted 64-bit range")

    def test_warning_echo_is_bounded(self) -> None:
        settings = rp.resolve_sampling_settings(
            config_values={},
            environ={rp.SAMPLING_SEED_ENV: "z" * 500},
        )
        echoed = settings.warnings[0].raw
        self.assertLessEqual(len(echoed), rp.MAX_WARNING_VALUE_CHARS + 3)
        self.assertTrue(echoed.endswith("..."))

    def test_warning_has_a_readable_message_and_payload(self) -> None:
        settings = rp.resolve_sampling_settings(
            config_values={},
            environ={rp.SAMPLING_TEMPERATURE_ENV: "warm"},
        )
        warning = settings.warnings[0]
        self.assertIn(rp.SAMPLING_TEMPERATURE_ENV, warning.message())
        self.assertIn("left unset", warning.message())
        self.assertEqual(
            set(warning.payload()),
            {"setting", "source", "reason", "raw"},
        )

    def test_a_garbage_environment_never_raises(self) -> None:
        settings = rp.resolve_sampling_settings(
            config_values={
                rp.SAMPLING_TEMPERATURE_CONFIG_KEY: object(),
                rp.SAMPLING_SEED_CONFIG_KEY: ["not", "a", "seed"],
            },
            environ={rp.SAMPLING_TOP_P_ENV: "\x00\x01"},
        )
        self.assertFalse(settings.is_configured)


class PayloadApplicationTests(unittest.TestCase):
    """The byte-identity guarantee, and what changes when it is switched off."""

    def test_unconfigured_leaves_the_payload_byte_identical(self) -> None:
        payload = _reference_payload()
        before = _serialize(payload)
        applied = rp.apply_sampling_to_payload(payload, rp.SamplingSettings())
        self.assertEqual(applied, ())
        self.assertEqual(_serialize(payload), before)
        self.assertEqual(list(payload), list(_reference_payload()))

    def test_unconfigured_is_a_no_op_on_every_payload_shape(self) -> None:
        # Including the shapes where the transport omitted temperature outright.
        shapes = [
            {},
            {"model": "m", "messages": []},
            {"model": "m", "messages": [], "temperature": 1.0},
            {"model": "m", "messages": [], "reasoning_effort": "high"},
            _reference_payload(),
        ]
        for shape in shapes:
            with self.subTest(keys=sorted(shape)):
                before = _serialize(shape)
                applied = rp.apply_sampling_to_payload(shape, rp.SamplingSettings())
                self.assertEqual(applied, ())
                self.assertEqual(_serialize(shape), before)

    def test_configured_fields_are_written(self) -> None:
        payload = _reference_payload()
        settings = rp.SamplingSettings(temperature=0.0, top_p=1.0, seed=7)
        applied = rp.apply_sampling_to_payload(payload, settings)
        self.assertEqual(applied, ("temperature", "top_p", "seed"))
        self.assertEqual(payload["temperature"], 0.0)
        self.assertEqual(payload["top_p"], 1.0)
        self.assertEqual(payload["seed"], 7)

    def test_temperature_override_does_not_reorder_keys(self) -> None:
        payload = _reference_payload()
        expected_order = list(payload)
        rp.apply_sampling_to_payload(payload, rp.SamplingSettings(temperature=0.0))
        self.assertEqual(list(payload), expected_order)

    def test_temperature_is_never_introduced_when_absent(self) -> None:
        # The transport omits temperature on purpose for models that reject it
        # and for providers that already 400'd on it. Re-adding it would
        # reintroduce the exact error the omission avoids.
        payload = {"model": "m", "messages": []}
        applied = rp.apply_sampling_to_payload(payload, rp.SamplingSettings(temperature=0.0))
        self.assertEqual(applied, ())
        self.assertNotIn("temperature", payload)

    def test_temperature_override_can_be_declined(self) -> None:
        payload = _reference_payload()
        applied = rp.apply_sampling_to_payload(
            payload,
            rp.SamplingSettings(temperature=0.0, seed=5),
            allow_temperature_override=False,
        )
        self.assertEqual(applied, ("seed",))
        self.assertEqual(payload["temperature"], 0.2)

    def test_top_p_and_seed_are_appended_last(self) -> None:
        payload = _reference_payload()
        rp.apply_sampling_to_payload(payload, rp.SamplingSettings(top_p=0.9, seed=3))
        self.assertEqual(list(payload)[-2:], ["top_p", "seed"])

    def test_applying_twice_is_stable(self) -> None:
        settings = rp.SamplingSettings(temperature=0.0, top_p=1.0, seed=7)
        payload = _reference_payload()
        rp.apply_sampling_to_payload(payload, settings)
        once = _serialize(payload)
        rp.apply_sampling_to_payload(payload, settings)
        self.assertEqual(_serialize(payload), once)


class SamplingTelemetryTests(unittest.TestCase):
    def test_telemetry_payload_is_present_even_when_unconfigured(self) -> None:
        payload = rp.SamplingSettings().telemetry_payload()
        self.assertIs(payload["configured"], False)
        self.assertIsNone(payload["temperature"])
        self.assertIsNone(payload["top_p"])
        self.assertIsNone(payload["seed"])
        self.assertEqual(payload["sources"], {})

    def test_session_event_payload_carries_warnings(self) -> None:
        settings = rp.resolve_sampling_settings(
            config_values={},
            environ={rp.SAMPLING_SEED_ENV: "nope"},
        )
        payload = settings.session_event_payload()
        self.assertEqual(payload["warning_count"], 1)
        self.assertEqual(payload["warnings"][0]["setting"], "seed")

    def test_payloads_are_json_serializable(self) -> None:
        settings = rp.resolve_sampling_settings(
            config_values={},
            environ={
                rp.SAMPLING_TEMPERATURE_ENV: "0.0",
                rp.SAMPLING_SEED_ENV: "bad",
            },
        )
        json.dumps(settings.session_event_payload())
        json.dumps(settings.telemetry_payload())


class ActiveSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        rp.reset_active_sampling_settings_for_tests()

    def tearDown(self) -> None:
        rp.reset_active_sampling_settings_for_tests()

    def test_installed_settings_are_returned(self) -> None:
        installed = rp.SamplingSettings(seed=42)
        rp.set_active_sampling_settings(installed)
        self.assertIs(rp.active_sampling_settings(), installed)

    def test_reset_restores_env_resolution(self) -> None:
        rp.set_active_sampling_settings(rp.SamplingSettings(seed=42))
        rp.reset_active_sampling_settings_for_tests()
        self.assertIsInstance(rp.active_sampling_settings(), rp.SamplingSettings)


class HeaderAllowlistTests(unittest.TestCase):
    def test_default_names_match(self) -> None:
        allowlist = rp.DEFAULT_RESPONSE_HEADER_ALLOWLIST
        for name in ("x-request-id", "openai-organization", "server", "x-served-by", "via"):
            with self.subTest(name=name):
                self.assertTrue(allowlist.matches(name))

    def test_matching_is_case_insensitive(self) -> None:
        self.assertTrue(rp.DEFAULT_RESPONSE_HEADER_ALLOWLIST.matches("X-Request-Id"))

    def test_model_and_version_patterns_match(self) -> None:
        allowlist = rp.DEFAULT_RESPONSE_HEADER_ALLOWLIST
        for name in ("x-model-version", "x-upstream-model", "model-id", "x-api-version"):
            with self.subTest(name=name):
                self.assertTrue(allowlist.matches(name))

    def test_unrelated_headers_do_not_match(self) -> None:
        allowlist = rp.DEFAULT_RESPONSE_HEADER_ALLOWLIST
        for name in ("content-type", "content-length", "date", "x-ratelimit-remaining"):
            with self.subTest(name=name):
                self.assertFalse(allowlist.matches(name))

    def test_credential_headers_are_denied_by_default(self) -> None:
        allowlist = rp.DEFAULT_RESPONSE_HEADER_ALLOWLIST
        for name in ("authorization", "set-cookie", "x-api-key", "proxy-authorization"):
            with self.subTest(name=name):
                self.assertFalse(allowlist.matches(name))

    def test_deny_list_beats_a_wildcard_allowlist(self) -> None:
        # An operator who asks for everything still does not get their token
        # written to a JSONL file.
        allowlist = rp.parse_response_header_allowlist("*")
        self.assertTrue(allowlist.matches("x-request-id"))
        for name in ("authorization", "set-cookie", "x-api-key", "x-session-token"):
            with self.subTest(name=name):
                self.assertFalse(allowlist.matches(name))

    def test_deny_list_beats_an_explicitly_named_secret_header(self) -> None:
        allowlist = rp.parse_response_header_allowlist("authorization, x-api-key")
        self.assertFalse(allowlist.matches("authorization"))
        self.assertFalse(allowlist.matches("x-api-key"))

    def test_blank_spec_keeps_the_default(self) -> None:
        for raw in (None, "", "   ", ","):
            with self.subTest(raw=raw):
                self.assertEqual(
                    rp.parse_response_header_allowlist(raw),
                    rp.DEFAULT_RESPONSE_HEADER_ALLOWLIST,
                )

    def test_explicit_spec_replaces_the_default(self) -> None:
        allowlist = rp.parse_response_header_allowlist("x-served-by, x-trace-id")
        self.assertTrue(allowlist.matches("x-trace-id"))
        self.assertTrue(allowlist.matches("x-served-by"))
        self.assertFalse(allowlist.matches("x-request-id"))

    def test_capture_can_be_disabled(self) -> None:
        for raw in ("none", "off", "-", "  NONE  "):
            with self.subTest(raw=raw):
                allowlist = rp.parse_response_header_allowlist(raw)
                self.assertTrue(allowlist.is_empty)
                self.assertFalse(allowlist.matches("x-request-id"))

    def test_whitespace_and_commas_both_separate(self) -> None:
        allowlist = rp.parse_response_header_allowlist("a-one  b-two,c-three")
        self.assertEqual(allowlist.names, frozenset({"a-one", "b-two", "c-three"}))

    def test_resolve_reads_the_env_var(self) -> None:
        allowlist = rp.resolve_response_header_allowlist(
            environ={rp.RESPONSE_HEADER_ALLOWLIST_ENV: "x-trace-id"}
        )
        self.assertEqual(allowlist.names, frozenset({"x-trace-id"}))


class HeaderSelectionTests(unittest.TestCase):
    def test_selects_and_lowercases_and_sorts(self) -> None:
        selected = rp.select_response_headers(
            {
                "X-Request-Id": "req-1",
                "Content-Type": "application/json",
                "Server": "nginx",
                "Authorization": "Bearer secret-value-here",
            }
        )
        self.assertEqual(selected, {"server": "nginx", "x-request-id": "req-1"})
        self.assertEqual(list(selected), ["server", "x-request-id"])

    def test_values_are_bounded(self) -> None:
        selected = rp.select_response_headers({"x-request-id": "r" * 5000})
        self.assertLessEqual(
            len(selected["x-request-id"]),
            rp.MAX_RESPONSE_HEADER_VALUE_CHARS + 3,
        )

    def test_header_count_is_bounded(self) -> None:
        headers = {f"x-model-{index}": str(index) for index in range(200)}
        selected = rp.select_response_headers(headers)
        self.assertLessEqual(len(selected), rp.MAX_RESPONSE_HEADERS)

    def test_empty_allowlist_captures_nothing(self) -> None:
        selected = rp.select_response_headers(
            {"x-request-id": "req-1"},
            allowlist=rp.ResponseHeaderAllowlist(),
        )
        self.assertEqual(selected, {})

    def test_a_broken_headers_object_yields_nothing(self) -> None:
        class Exploding:
            def items(self):
                raise RuntimeError("boom")

        self.assertEqual(rp.select_response_headers(Exploding()), {})
        self.assertEqual(rp.select_response_headers(None), {})


class ResponseFingerprintTests(unittest.TestCase):
    def test_absence_is_recorded_explicitly(self) -> None:
        payload = rp.response_fingerprint_payload()
        self.assertIsNone(payload["response_model"])
        self.assertIsNone(payload["system_fingerprint"])
        self.assertEqual(payload["response_headers"], {})
        self.assertEqual(payload["response_header_count"], 0)

    def test_header_key_avoids_the_telemetry_redactor_blind_spot(self) -> None:
        # provider_telemetry replaces any value under a key named "headers"
        # with "[omitted]", which would silently discard the whole capture.
        payload = rp.response_fingerprint_payload(headers={"x-request-id": "req-1"})
        self.assertNotIn("headers", payload)
        self.assertIn("response_headers", payload)

    def test_values_are_carried_through(self) -> None:
        payload = rp.response_fingerprint_payload(
            response_model="MiMo-VL-7B-RL",
            system_fingerprint="fp_44709d6fcb",
            headers={"x-request-id": "req-1"},
        )
        self.assertEqual(payload["response_model"], "MiMo-VL-7B-RL")
        self.assertEqual(payload["system_fingerprint"], "fp_44709d6fcb")
        self.assertEqual(payload["response_header_count"], 1)

    def test_blank_strings_normalize_to_none(self) -> None:
        payload = rp.response_fingerprint_payload(response_model="  ", system_fingerprint="")
        self.assertIsNone(payload["response_model"])
        self.assertIsNone(payload["system_fingerprint"])

    def test_system_fingerprint_is_extracted_from_a_body(self) -> None:
        self.assertEqual(
            rp.extract_system_fingerprint({"system_fingerprint": "fp_abc"}),
            "fp_abc",
        )
        self.assertIsNone(rp.extract_system_fingerprint({"model": "m"}))
        self.assertIsNone(rp.extract_system_fingerprint(None))
        self.assertIsNone(rp.extract_system_fingerprint("not-a-mapping"))


def _call(requested, response_model=None, fingerprint=None):
    """One provider-call payload, shaped as the recorder writes it."""
    return {
        "kind": "provider_call",
        "model": requested,
        "response_fingerprint": {
            "response_model": response_model,
            "system_fingerprint": fingerprint,
            "response_headers": {},
            "response_header_count": 0,
        },
    }


class FingerprintDriftTests(unittest.TestCase):
    """The rollup that answers the question the earlier investigations could not."""

    def test_an_empty_window_reports_no_drift(self) -> None:
        payload = rp.fingerprint_drift_payload([])
        self.assertEqual(payload["window_call_count"], 0)
        self.assertFalse(payload["drift_detected"])
        self.assertEqual(payload["by_requested_model"], [])

    def test_one_stable_model_reports_no_drift(self) -> None:
        calls = [_call("MiMo", "MiMo", "fp_a") for _ in range(5)]
        payload = rp.fingerprint_drift_payload(calls)
        self.assertFalse(payload["drift_detected"])
        self.assertEqual(payload["distinct_system_fingerprint_count"], 1)
        self.assertEqual(payload["window_call_count"], 5)

    def test_a_changed_fingerprint_is_drift(self) -> None:
        # Two runs of a byte-identical build scoring 80 then 71 looks exactly
        # like this from the client side.
        calls = [_call("MiMo", "MiMo", "fp_a"), _call("MiMo", "MiMo", "fp_b")]
        payload = rp.fingerprint_drift_payload(calls)
        self.assertTrue(payload["drift_detected"])
        self.assertEqual(payload["system_fingerprints"], ["fp_a", "fp_b"])
        self.assertTrue(payload["by_requested_model"][0]["drift_detected"])

    def test_a_changed_response_model_is_drift(self) -> None:
        calls = [_call("MiMo", "MiMo-v1", "fp_a"), _call("MiMo", "MiMo-v2", "fp_a")]
        payload = rp.fingerprint_drift_payload(calls)
        self.assertTrue(payload["drift_detected"])
        self.assertEqual(payload["response_models"], ["MiMo-v1", "MiMo-v2"])

    def test_two_requested_models_are_not_drift(self) -> None:
        # Grouping by requested model is what stops an ordinary multi-model run
        # from reading as drift.
        calls = [_call("MiMo", "MiMo", "fp_a"), _call("Qwen", "Qwen", "fp_b")]
        payload = rp.fingerprint_drift_payload(calls)
        self.assertFalse(payload["drift_detected"])
        self.assertEqual(len(payload["by_requested_model"]), 2)

    def test_absent_fingerprints_are_counted_not_hidden(self) -> None:
        # An endpoint that never sends one cannot be monitored this way, and
        # that has to read differently from "no drift".
        calls = [_call("MiMo", "MiMo", None), _call("MiMo", "MiMo", "fp_a")]
        payload = rp.fingerprint_drift_payload(calls)
        self.assertEqual(payload["system_fingerprint_present_call_count"], 1)
        self.assertEqual(payload["system_fingerprint_absent_call_count"], 1)
        self.assertFalse(payload["drift_detected"])

    def test_malformed_rows_are_skipped(self) -> None:
        payload = rp.fingerprint_drift_payload(
            [None, "nonsense", 7, {"model": "MiMo"}, _call("MiMo", "MiMo", "fp_a")]
        )
        self.assertEqual(payload["window_call_count"], 2)
        self.assertFalse(payload["drift_detected"])

    def test_payload_is_json_serializable(self) -> None:
        json.dumps(rp.fingerprint_drift_payload([_call("MiMo", "MiMo", "fp_a")]))


class ConfigSnapshotTests(unittest.TestCase):
    def test_secret_named_keys_are_masked(self) -> None:
        scrubbed = rp.scrub_config_values(
            {
                "model": "MiMo-VL-7B-RL",
                "api_key": "sk-live-aaaaaaaaaaaaaaaa",
                "nested": {"client_secret": "shh", "base_url": "https://example.test/v1"},
                "runtimes": [{"auth_token": "t"}],
            }
        )
        self.assertEqual(scrubbed["api_key"], rp.MASKED_VALUE)
        self.assertEqual(scrubbed["nested"]["client_secret"], rp.MASKED_VALUE)
        self.assertEqual(scrubbed["runtimes"][0]["auth_token"], rp.MASKED_VALUE)
        self.assertEqual(scrubbed["model"], "MiMo-VL-7B-RL")
        self.assertEqual(scrubbed["nested"]["base_url"], "https://example.test/v1")

    def test_token_shaped_but_innocent_keys_survive(self) -> None:
        # Masking these would gut the snapshot: they are exactly the settings
        # a post-mortem needs.
        scrubbed = rp.scrub_config_values(
            {
                "max_tokens": 4096,
                "reasoning_tokens": 512,
                "min_cacheable_tokens": 1024,
                "prompt_tokens": 10,
            }
        )
        self.assertEqual(scrubbed["max_tokens"], 4096)
        self.assertEqual(scrubbed["reasoning_tokens"], 512)
        self.assertEqual(scrubbed["min_cacheable_tokens"], 1024)
        self.assertEqual(scrubbed["prompt_tokens"], 10)

    def test_environment_capture_is_prefixed_and_masked(self) -> None:
        captured = rp.snapshot_environment(
            environ={
                "ALYSIS_MODEL": "MiMo-VL-7B-RL",
                "ALYSIS_API_KEY": "sk-live-aaaaaaaaaaaaaaaa",
                "ALYSIS_SAMPLING_SEED": "7",
                "PATH": "/usr/bin",
                "AWS_SECRET_ACCESS_KEY": "nope",
            }
        )
        self.assertEqual(captured["ALYSIS_MODEL"], "MiMo-VL-7B-RL")
        self.assertEqual(captured["ALYSIS_SAMPLING_SEED"], "7")
        self.assertEqual(captured["ALYSIS_API_KEY"], rp.MASKED_VALUE)
        self.assertNotIn("PATH", captured)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", captured)

    def test_snapshot_payload_shape(self) -> None:
        payload = rp.config_snapshot_payload(
            config_values={"model": "m", "api_key": "sk-live-aaaaaaaaaaaaaaaa"},
            version="0.10.0.dev6",
            build_info={"commit": "abc1234", "dirty": False},
            sampling=rp.SamplingSettings(seed=7),
            environ={"ALYSIS_MODEL": "m"},
        )
        self.assertEqual(payload["schema_version"], rp.CONFIG_SNAPSHOT_SCHEMA_VERSION)
        self.assertEqual(payload["version"], "0.10.0.dev6")
        self.assertEqual(payload["build"]["commit"], "abc1234")
        self.assertEqual(payload["sampling"]["seed"], 7)
        self.assertEqual(payload["config"]["api_key"], rp.MASKED_VALUE)
        self.assertEqual(payload["environment"], {"ALYSIS_MODEL": "m"})
        self.assertIn("names", payload["response_header_allowlist"])
        json.dumps(payload)

    def test_snapshot_survives_an_empty_config(self) -> None:
        payload = rp.config_snapshot_payload(environ={})
        self.assertEqual(payload["config"], {})
        self.assertEqual(payload["environment"], {})
        json.dumps(payload)

    def test_snapshot_schema_is_version_2(self) -> None:
        # Version 2 introduced the `effective` section. Consumers key on this
        # number to know the section exists, so it is pinned rather than
        # merely referenced through the constant.
        self.assertEqual(rp.CONFIG_SNAPSHOT_SCHEMA_VERSION, 2)
        self.assertEqual(rp.config_snapshot_payload(environ={})["schema_version"], 2)

    def test_snapshot_records_effective_values_verbatim(self) -> None:
        # The defect this section exists for: an 89-task campaign enforced an
        # env-provided llm timeout of 240 while every snapshot's raw config
        # dump reported the unset field default of 60.0, and the teardown
        # read the wrong one. The effective section is the caller's statement
        # of what will actually be enforced, with the source that won.
        payload = rp.config_snapshot_payload(
            config_values={"llm_timeout_s": 60.0},
            environ={"ALYSIS_LLM_TIMEOUT_S": "240"},
            effective_values={
                "llm_timeout_s": 240.0,
                "llm_timeout_source": "environment",
            },
        )
        self.assertEqual(payload["effective"]["llm_timeout_s"], 240.0)
        self.assertEqual(payload["effective"]["llm_timeout_source"], "environment")
        # The raw dump is deliberately unchanged: it answers "what does the
        # config file say", the effective section answers "what governs".
        self.assertEqual(payload["config"]["llm_timeout_s"], 60.0)
        json.dumps(payload)

    def test_snapshot_effective_defaults_to_empty_and_is_scrubbed(self) -> None:
        self.assertEqual(rp.config_snapshot_payload(environ={})["effective"], {})
        payload = rp.config_snapshot_payload(
            environ={},
            effective_values={"api_key": "sk-live-bbbbbbbbbbbbbbbb"},
        )
        # Same structural scrubbing as the config dump: a resolver that leaks
        # a credential into the effective section must not defeat PR1.
        self.assertEqual(payload["effective"]["api_key"], rp.MASKED_VALUE)


class IdempotenceTests(unittest.TestCase):
    """Resolution and shaping are pure: replaying gives the same answer."""

    def test_resolution_is_deterministic(self) -> None:
        environ = {
            rp.SAMPLING_TEMPERATURE_ENV: "0.0",
            rp.SAMPLING_SEED_ENV: "oops",
        }
        first = rp.resolve_sampling_settings(config_values={}, environ=environ)
        second = rp.resolve_sampling_settings(config_values={}, environ=environ)
        self.assertEqual(first.session_event_payload(), second.session_event_payload())

    def test_scrubbing_is_idempotent(self) -> None:
        values = {"api_key": "sk-live-aaaa", "model": "m", "nested": {"token": "t"}}
        once = rp.scrub_config_values(values)
        twice = rp.scrub_config_values(once)
        self.assertEqual(once, twice)

    def test_header_selection_is_idempotent(self) -> None:
        headers = {"X-Request-Id": "r", "Server": "s", "Authorization": "Bearer x"}
        once = rp.select_response_headers(headers)
        twice = rp.select_response_headers(once)
        self.assertEqual(once, twice)


if __name__ == "__main__":
    unittest.main(verbosity=2)
