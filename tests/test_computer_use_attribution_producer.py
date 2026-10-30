"""Bounded producer scenarios from frozen T.7, using fake clocks/transports only."""

import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_computer_use_attribution_contract import B, V

from alysis_code import computer_use_attribution as a
from alysis_code import token_budget


@pytest.fixture(autouse=True)
def isolated_encoder_owner(monkeypatch):
    monkeypatch.setattr(a, "_encoder_prior_unowned", False)


def collector(kind="startupContext", emit=None):
    clock = [0.0]
    return a.Collector(B[kind], clock=lambda: clock[0], emit=emit), clock


def test_A02_suppressed_parent_never_rewritten_to_root():
    c, clock = collector(emit=lambda r: False if r["recordOrdinal"] == 1 else True)
    with a.bind_collector(c), a.phase("prompt_assembly"):
        clock[0] = 0.002
        with a.phase("first_encoder_acquisition") as child:
            child.acquisition = "fallback"
            clock[0] = 0.006
        clock[0] = 0.010
    summary = c.finish("returned")
    assert c.records == []
    assert summary["detailDropped"] == 4
    for metric, duration in V["producer_scenarios"][1]["aggregateTotalDurationMs"].items():
        assert c.cell(metric)["ended"] == 1
        assert c.cell(metric)["totalDurationMs"] == duration


def test_A03_reserved_end_survives_capacity():
    c, _ = collector("turnContext")
    owner = c.begin("model_call")
    for i in range(126):
        c.refusal(a.FAMILIES[0], i + 1, 1)
    assert len(c.records) + c.reserved == 128
    child = c.begin("first_encoder_acquisition")
    c.end(child)
    c.end(owner)
    assert len(c.records) == 128 and c.records[-1]["phase"] == "end"
    assert c.finish("returned") is not None


def test_A04_reserved_identities_survive_exhaustion():
    c, _ = collector("turnContext")
    owner = c.begin("model_call")
    c.ordinal = 999997
    child = c.begin("first_encoder_acquisition")
    assert not child.detailed and c.ordinal == 999998
    c.end(child)
    assert c.ordinal == 999998
    c.end(owner)
    assert c.records[-1]["recordOrdinal"] == 999999
    summary = c.finish("returned")
    assert summary["recordOrdinal"] == 1000000 and summary["identityExhausted"]
    assert summary["coverage"] == "partial" and c.finish("returned") is None


def test_A05_active_frame_capacity():
    c, _ = collector()
    spans = [c.begin("config") for _ in range(32)]
    assert c.begin("config") is None and len(c.active) == 32
    for span in spans:
        c.end(span)
    summary = c.finish("returned")
    assert summary["unrecordedIntervals"] == 1 and summary["coverage"] == "partial"


def test_A06_A07_saturating_aggregates():
    c, clock = collector()
    c.summary["clockInvalid"] = a.N
    c.inc(c.summary, "clockInvalid")
    assert c.summary["clockInvalid"] == a.N
    span = c.begin("config")
    c.cell("config")["totalDurationMs"] = a.SUM
    clock[0] = 0.001
    c.end(span)
    assert c.cell("config")["totalDurationMs"] == a.SUM
    assert c.cell("config")["saturated"]
    summary = c.finish("returned")
    assert summary["countersSaturated"] and summary["coverage"] == "partial"


@pytest.mark.parametrize("end", [9, float("nan"), float("inf"), 100000000])
def test_A08_invalid_clock_is_not_zero(end):
    c, clock = collector()
    clock[0] = 10
    span = c.begin("config")
    clock[0] = end
    c.end(span)
    assert c.records[-1]["durationMs"] is None
    assert c.cell("config")["timed"] == c.cell("config")["totalDurationMs"] == 0
    assert c.finish("returned")["clockInvalid"] == 1


@pytest.mark.parametrize("fallback", [False, True])
def test_A09_A10_owned_encoder_cache_hits_do_not_reacquire(monkeypatch, fallback):
    calls = []

    def encoding(name):
        calls.append(name)
        if fallback:
            raise RuntimeError("private fake tokenizer error")
        return object()

    monkeypatch.setitem(sys.modules, "tiktoken", SimpleNamespace(get_encoding=encoding))
    token_budget._token_encoder.cache_clear()
    try:
        c, _ = collector()
        with a.bind_collector(c):
            first = token_budget._token_encoder()
        s = c.finish("returned")
        assert s["encoderAcquisition"]["fallback" if fallback else "acquired"] == 1
        c2, _ = collector()
        with a.bind_collector(c2):
            assert token_budget._token_encoder() is first
        assert len(calls) == 1 and c2.records == []
        assert c2.finish("returned")["encoderAcquisition"] == {
            "acquired": 0,
            "fallback": 0,
            "unknown": 0,
            "priorUnowned": False,
        }
    finally:
        token_budget._token_encoder.cache_clear()


def test_A11_unowned_encoder_never_retroactively_timed(monkeypatch):
    monkeypatch.setitem(sys.modules, "tiktoken", SimpleNamespace(get_encoding=lambda _: None))
    token_budget._token_encoder.cache_clear()
    try:
        token_budget._token_encoder()
        c, _ = collector()
        with a.bind_collector(c):
            token_budget._token_encoder()
        summary = c.finish("returned")
        assert summary["coverage"] == "partial"
        assert summary["encoderAcquisition"]["priorUnowned"]
        assert c.cell("first_encoder_acquisition")["availability"] == "unavailable"
        assert not c.records
    finally:
        token_budget._token_encoder.cache_clear()


def test_A12_A15_summary_is_single_replacement():
    admission = a.Admission(B["turnContext"])
    for name in ["modelBegin", "modelEnd", "refusal", "turnSummary"]:
        assert admission.accept(deepcopy(B[name]))
    assert not admission.accept(deepcopy(B["turnSummary"]))
    assert not admission.accept(deepcopy(B["modelBegin"]))


def test_A13_telemetry_exception_preserves_original_error(monkeypatch):
    c, _ = collector()
    original = deepcopy(B["startupErrorReply"])
    original.pop("computer_use_attribution")
    monkeypatch.setattr(
        c, "finish", lambda _: (_ for _ in ()).throw(RuntimeError("private failure"))
    )
    result = a.rpc_reply(c, original) or original
    assert result == original


def test_A14_oversize_sidecar_rejected_without_mutating_business():
    reply = deepcopy(B["startupSuccessReply"])
    reply["computer_use_attribution"]["records"] = [B["configBegin"]] * 129
    before = deepcopy(reply)
    assert not a.valid_sidecar(
        reply,
        {"requestId": reply["id"], "method": "session.create", "context": B["startupContext"]},
    )
    assert reply == before


@pytest.mark.parametrize("record", [None, [], 1, "private", {"kind": []}])
def test_malformed_event_is_dropped_without_cursor_change(record):
    envelope = deepcopy(B["modelBeginEvent"])
    envelope["payload"]["record"] = record
    binding = {
        "context": B["turnContext"],
        "sessionId": "s",
        "jobId": "j",
        "runId": "r",
        "lastSequence": 0,
    }
    before = deepcopy(binding)
    assert a.accept_event(envelope, binding, a.Admission(B["turnContext"])) is False
    assert binding == before


def test_active_unknown_transport_is_honest_and_late_callbacks_inert():
    c, _ = collector("turnContext")
    span = c.begin("model_call")
    summary = c.finish("cancelled")
    assert summary["transportUnknownCalls"] == 1
    assert summary["modelCategories"]["pending"] == 1
    before = deepcopy(c.summary)
    c.attempt(span)
    c.end(span)
    assert c.summary == before and c.finish("returned") is None


@pytest.mark.parametrize("error_name", ["failure", "cancelled", "deadline"])
def test_model_exception_classification_and_private_error(error_name):
    from alysis_code.cancellation import CooperativeCancellationError
    from alysis_code.execution_deadline import DeadlineExhausted

    error = {
        "failure": RuntimeError("PRIVATE"),
        "cancelled": CooperativeCancellationError("PRIVATE"),
        "deadline": DeadlineExhausted("PRIVATE"),
    }[error_name]
    c, _ = collector("turnContext")

    @a.model_call
    def invoke(**kwargs):
        raise error

    with a.bind_collector(c), pytest.raises(type(error)) as caught:
        invoke(tools=[])
    assert caught.value is error
    end = c.records[-1]
    assert end["responseClass"] == "no_response" and end["category"] == "other"
    assert (
        end["outcome"]
        == {"failure": "failed", "cancelled": "cancelled", "deadline": "deadline"}[error_name]
    )
    assert "PRIVATE" not in str(c.records)


def test_detail_emission_failure_never_changes_wrapped_result():
    c, _ = collector(
        emit=lambda _: (_ for _ in ()).throw(RuntimeError("private transport failure"))
    )
    with a.bind_collector(c):
        assert a.measured("config", lambda: "original result") == "original result"
    summary = c.finish("returned")
    assert summary["detailDropped"] == 2 and summary["spans"][0]["ended"] == 1


def test_collection_overhead_receipt(tmp_path):
    """Non-gating microbenchmark: no wall-time threshold or product speedup claim."""
    import json
    from time import perf_counter

    samples = []
    for _ in range(5):
        results = {}
        for enabled in (False, True):
            c, _ = collector("turnContext")

            @a.model_call
            def invoke(**kwargs):
                return SimpleNamespace(tool_calls=[])

            started = perf_counter()
            with a.bind_collector(c if enabled else None):
                for _ in range(1000):
                    invoke(tools=[])
            elapsed = perf_counter() - started
            if enabled:
                summary = c.finish("returned")
                assert summary["spans"][6]["ended"] == 1000 and len(c.records) == 128
            results["enabled_seconds" if enabled else "disabled_seconds"] = elapsed
        samples.append(results)
    (tmp_path / "attribution-overhead.json").write_text(
        json.dumps({"calls_per_sample": 1000, "samples": samples})
    )


def test_late_closed_encoder_acquisition_has_no_owned_receipt(monkeypatch):
    monkeypatch.setattr(a, "_encoder_prior_unowned", False)
    monkeypatch.setitem(sys.modules, "tiktoken", SimpleNamespace(get_encoding=lambda _: object()))
    token_budget._token_encoder.cache_clear()
    try:
        owner = a.Collector(B["turnContext"])
        owner.finish("cancelled")
        with a.bind_collector(owner):
            token_budget._token_encoder()
        later = a.Collector(B["startupContext"])
        assert later.finish("returned")["encoderAcquisition"]["priorUnowned"] is True
    finally:
        token_budget._token_encoder.cache_clear()


def test_parent_does_not_emit_end_while_child_active():
    c = a.Collector(B["startupContext"])
    with a.bind_collector(c), a.phase("prompt_assembly") as parent:
        child = c.begin("first_encoder_acquisition")
        c.end(parent)
        assert not any(r["phase"] == "end" for r in c.records)
        c.end(child)
    assert c.finish("returned") is not None


def test_rpc_request_identity_does_not_coerce_boolean_to_integer():
    reply = deepcopy(B["startupSuccessReply"])
    reply["id"] = 1
    binding = {"requestId": True, "method": "session.create", "context": B["startupContext"]}
    assert not a.valid_sidecar(reply, binding)


def test_none_encoder_return_is_not_acquired(monkeypatch):
    monkeypatch.setattr(a, "_encoder_prior_unowned", False)
    monkeypatch.setitem(sys.modules, "tiktoken", SimpleNamespace(get_encoding=lambda _: None))
    token_budget._token_encoder.cache_clear()
    try:
        owner = a.Collector(B["startupContext"])
        with a.bind_collector(owner):
            assert token_budget._token_encoder() is None
        counters = owner.finish("returned")["encoderAcquisition"]
        assert counters == {"acquired": 0, "fallback": 0, "unknown": 1, "priorUnowned": False}
    finally:
        token_budget._token_encoder.cache_clear()


def test_exhausted_provider_never_reuses_previous_refusal_correlation(monkeypatch):
    monkeypatch.setattr(a, "_encoder_prior_unowned", False)
    c = a.Collector(B["turnContext"])
    first = c.begin("model_call")
    c.end(first)
    c.span_ordinal = a.N
    assert c.begin("model_call") is None

    @a.tool_call(c)
    def refuse():
        a.scope_refusal(a.FAMILIES[0])

    refuse()
    assert not any(r["kind"] == "scope_refusal" for r in c.records)
    assert c.finish("returned")["scopeRefusals"][a.FAMILIES[0]] == 1
