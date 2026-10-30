"""Section T: bounded, opt-in diagnostic attribution; never task authority."""

from __future__ import annotations

import json
import math
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from functools import wraps
from time import perf_counter

N = 1_000_000
D = 86_400_000
SUM = 86_400_000_000
METRICS = (
    "config",
    "model_metadata",
    "client_construction",
    "prompt_assembly",
    "fixed_tools",
    "first_encoder_acquisition",
    "model_call",
)
FAMILIES = (
    "operates_true_without_input",
    "operates_false_with_input",
    "app_switch_without_activation",
    "initial_open_without_activation",
)
OUTCOMES = ("returned", "failed", "cancelled", "deadline", "unknown")
CATEGORIES = {
    "discover_only": "discover",
    "start_only": "start",
    "mixed": "other",
    "other_tools": "other",
    "no_tools": "other",
    "invalid_tools": "other",
    "no_response": "other",
}
_current = ContextVar("cu_attribution_owner", default=None)
_parent = ContextVar("cu_attribution_parent", default=None)
_tool = ContextVar("cu_attribution_tool", default=None)
_encoder_prior_unowned = False


def _integer(value, maximum=N, minimum=0):
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and value == int(value)
        and minimum <= value <= maximum
    )


def _data(value, depth=0):
    if depth > 12:
        return False
    if type(value) is dict:
        return len(value) <= 64 and all(
            type(k) is str and _data(v, depth + 1) for k, v in value.items()
        )
    if type(value) is list:
        return len(value) <= 128 and all(_data(v, depth + 1) for v in value)
    return value is None or type(value) in (str, bool) or _integer(value, SUM)


def _closed_validation(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return bool(fn(*args, **kwargs))
        except Exception:
            return False

    return wrapped


@_closed_validation
def valid_context(value):
    return (
        type(value) is dict
        and set(value) == {"contextOrdinal", "generationOrdinal", "producerRole", "contextKind"}
        and _integer(value["contextOrdinal"], 256, 1)
        and _integer(value["generationOrdinal"], 16, 1)
        and (
            (value["producerRole"] == "management" and value["contextKind"] == "management")
            or (
                value["producerRole"] in ("ordinary", "protected")
                and value["contextKind"] in ("session_startup", "turn")
            )
        )
    )


@_closed_validation
def valid_identity(value, *, numeric=False, nullable=False):
    return (
        (nullable and value is None)
        or (type(value) is str and bool(value.strip()) and len(value.encode("utf-8")) <= 256)
        or (
            numeric
            and type(value) in (int, float)
            and math.isfinite(value)
            and value == int(value)
            and abs(value) <= 2**53 - 1
        )
    )


def applicable(context):
    kind = context["contextKind"]
    if kind == "management":
        return {"config"}
    if kind == "turn":
        return {"model_call", "first_encoder_acquisition"}
    return set(METRICS[:-1]) - (
        {"fixed_tools"} if context["producerRole"] != "protected" else set()
    )


@_closed_validation
def valid_feature(value):
    return (
        type(value) is dict
        and set(value) == {"supported", "version"}
        and value["supported"] is True
        and _integer(value["version"], 1, 1)
    )


def opt_in(value, method):
    if not (
        type(value) is dict
        and set(value) == {"version", "context"}
        and _integer(value["version"], 1, 1)
        and valid_context(value["context"])
    ):
        return None
    expected = {
        "session.create": "session_startup",
        "chat.send": "turn",
        "config.get": "management",
    }.get(method)
    return (
        dict(value["context"]) if expected and value["context"]["contextKind"] == expected else None
    )


def _counts(value, keys):
    return (
        type(value) is dict and set(value) == set(keys) and all(_integer(v) for v in value.values())
    )


@_closed_validation
def valid_summary(value, context):
    keys = {
        "recordOrdinal",
        "kind",
        "closeReason",
        "coverage",
        "detailEligible",
        "detailEmitted",
        "detailDropped",
        "clockInvalid",
        "unrecordedIntervals",
        "countersSaturated",
        "identityExhausted",
        "spans",
        "modelCategories",
        "scopeRefusals",
        "transportAttempts",
        "transportUnknownCalls",
        "transportAttemptsSaturated",
        "encoderAcquisition",
    }
    if not (
        type(value) is dict
        and set(value) == keys
        and _data(value)
        and valid_context(context)
        and value["kind"] == "summary"
        and _integer(value["recordOrdinal"], N, 1)
        and value["closeReason"] in (*OUTCOMES, "retired")
        and value["coverage"] in ("complete", "partial", "unavailable")
    ):
        return False
    for key in ("countersSaturated", "identityExhausted", "transportAttemptsSaturated"):
        if type(value[key]) is not bool:
            return False
    for key in (
        "detailEligible",
        "detailEmitted",
        "detailDropped",
        "clockInvalid",
        "unrecordedIntervals",
        "transportAttempts",
        "transportUnknownCalls",
    ):
        if not _integer(value[key]):
            return False
    cells = value["spans"]
    if type(cells) is not list or len(cells) != 7:
        return False
    numbers = {
        "started",
        "ended",
        "returned",
        "failed",
        "cancelled",
        "deadline",
        "unknown",
        "activeAtClose",
        "timed",
        "totalDurationMs",
    }
    allowed = applicable(context)
    incomplete = (
        value["clockInvalid"]
        or value["unrecordedIntervals"]
        or value["identityExhausted"]
        or value["countersSaturated"]
        or value["transportUnknownCalls"]
    )
    available = 0
    for metric, cell in zip(METRICS, cells, strict=True):
        if (
            type(cell) is not dict
            or set(cell) != numbers | {"metric", "availability", "saturated"}
            or cell["metric"] != metric
            or type(cell["saturated"]) is not bool
            or cell["availability"] not in ("available", "not_applicable", "unavailable")
            or any(not _integer(cell[k], SUM if k == "totalDurationMs" else N) for k in numbers)
        ):
            return False
        if (metric not in allowed) != (cell["availability"] == "not_applicable"):
            return False
        if cell["availability"] != "available":
            if any(cell[k] for k in numbers) or cell["saturated"]:
                return False
            incomplete |= cell["availability"] == "unavailable"
        else:
            available += 1
        if cell["saturated"] and not value["countersSaturated"]:
            return False
        if not cell["saturated"]:
            if cell["started"] != cell["ended"] + cell["activeAtClose"] or cell["ended"] != sum(
                cell[k] for k in OUTCOMES
            ):
                return False
            if cell["timed"] > cell["ended"] or (
                cell["timed"] == 0 and cell["totalDurationMs"] != 0
            ):
                return False
        incomplete |= cell["activeAtClose"] or cell["saturated"]
    if not _counts(
        value["modelCategories"], ("discover", "start", "other", "pending")
    ) or not _counts(value["scopeRefusals"], FAMILIES):
        return False
    encoder = value["encoderAcquisition"]
    if not (
        type(encoder) is dict
        and set(encoder) == {"acquired", "fallback", "unknown", "priorUnowned"}
        and type(encoder["priorUnowned"]) is bool
        and all(_integer(encoder[k]) for k in ("acquired", "fallback", "unknown"))
    ):
        return False
    if encoder["priorUnowned"] and cells[5]["availability"] != "unavailable":
        return False
    incomplete |= encoder["priorUnowned"] or value["transportAttemptsSaturated"]
    if value["transportAttemptsSaturated"] and not value["countersSaturated"]:
        return False
    if not value["countersSaturated"]:
        if (
            value["detailEligible"] != value["detailEmitted"] + value["detailDropped"]
            or sum(value["modelCategories"][k] for k in ("discover", "start", "other"))
            != cells[6]["ended"]
            or value["modelCategories"]["pending"] != cells[6]["activeAtClose"]
            or sum(encoder[k] for k in ("acquired", "fallback", "unknown")) != cells[5]["ended"]
        ):
            return False
    if context["producerRole"] != "ordinary" or context["contextKind"] != "turn":
        if any(value["scopeRefusals"].values()):
            return False
    if context["producerRole"] == "protected" and (
        value["modelCategories"]["discover"] or value["modelCategories"]["start"]
    ):
        return False
    if context["contextKind"] != "turn" and (
        any(value["modelCategories"].values())
        or value["transportAttempts"]
        or value["transportUnknownCalls"]
    ):
        return False
    if value["coverage"] == "complete" and (incomplete or not available):
        return False
    if value["coverage"] == "unavailable" and available:
        return False
    return len(json.dumps(value, ensure_ascii=False).encode()) <= 16384


@_closed_validation
def valid_record(value, context):
    if not (valid_context(context) and type(value) is dict and _data(value)):
        return False
    if value.get("kind") == "summary":
        return valid_summary(value, context)
    if not _integer(value.get("recordOrdinal"), N, 1):
        return False
    if value.get("kind") == "scope_refusal":
        return (
            set(value) == {"recordOrdinal", "kind", "callOrdinal", "providerOrdinal", "family"}
            and context["producerRole"] == "ordinary"
            and context["contextKind"] == "turn"
            and _integer(value["callOrdinal"], N, 1)
            and _integer(value["providerOrdinal"], N, 1)
            and value["family"] in FAMILIES
        )
    keys = {"recordOrdinal", "kind", "metric", "spanOrdinal", "parentSpanOrdinal", "phase"}
    metric = value.get("metric")
    phase = value.get("phase")
    if (
        value.get("kind") != "span"
        or metric not in applicable(context)
        or phase not in ("begin", "end")
    ):
        return False
    if metric == "model_call":
        keys.add("providerOrdinal")
        if value.get("parentSpanOrdinal") is not None or not _integer(
            value.get("providerOrdinal"), N, 1
        ):
            return False
    if not _integer(value.get("spanOrdinal"), N, 1):
        return False
    parent = value.get("parentSpanOrdinal")
    if parent is not None and not _integer(parent, value["spanOrdinal"] - 1, 1):
        return False
    if phase == "end":
        keys |= {"durationMs", "outcome"}
        if value.get("outcome") not in OUTCOMES or (
            value.get("durationMs") is not None and not _integer(value["durationMs"], D)
        ):
            return False
        if metric == "model_call":
            keys |= {"category", "responseClass", "transportAttempts", "transportAttemptsSaturated"}
            cls = value.get("responseClass")
            if cls not in CATEGORIES or value.get("category") != CATEGORIES[cls]:
                return False
            if (cls == "no_response") == (value["outcome"] == "returned"):
                return False
            if context["producerRole"] == "protected" and cls not in (
                "other_tools",
                "no_tools",
                "invalid_tools",
                "no_response",
            ):
                return False
            if type(value.get("transportAttemptsSaturated")) is not bool:
                return False
            if value.get("transportAttempts") is None:
                if value["transportAttemptsSaturated"]:
                    return False
            elif not _integer(value["transportAttempts"]):
                return False
        if metric == "first_encoder_acquisition":
            keys.add("acquisitionResult")
            result = value.get("acquisitionResult")
            if result not in ("acquired", "fallback", "unknown") or (
                result != "unknown" and value["outcome"] != "returned"
            ):
                return False
    return set(value) == keys and len(json.dumps(value).encode()) <= 2048


class Admission:
    """Bounded ordered record validation, also used for the paired corpus."""

    def __init__(self, context):
        self.context = dict(context)
        self.ordinal = self.span = self.provider = self.call = 0
        self.active = {}
        self.finished_provider = 0
        self.closed = False

    def accept(self, record):
        if (
            self.closed
            or not valid_record(record, self.context)
            or record["recordOrdinal"] <= self.ordinal
        ):
            return False
        if record["kind"] == "summary":
            self.closed = True
            self.active.clear()
        elif record["kind"] == "scope_refusal":
            if (
                record["callOrdinal"] <= self.call
                or record["providerOrdinal"] != self.finished_provider
            ):
                return False
            self.call = record["callOrdinal"]
        elif record["phase"] == "begin":
            ordinal = record["spanOrdinal"]
            parent = record["parentSpanOrdinal"]
            if ordinal <= self.span or len(self.active) >= 32:
                return False
            if parent is not None:
                owner = self.active.get(parent)
                if owner is None or (
                    owner["metric"] == "model_call"
                    and record["metric"] != "first_encoder_acquisition"
                ):
                    return False
            if record["metric"] == "model_call":
                if record["providerOrdinal"] <= self.provider:
                    return False
                self.provider = record["providerOrdinal"]
            self.span = ordinal
            self.active[ordinal] = dict(record)
        else:
            begin = self.active.get(record["spanOrdinal"])
            if begin is None or any(
                record.get(k) != begin.get(k)
                for k in ("metric", "parentSpanOrdinal", "providerOrdinal")
            ):
                return False
            if any(r["parentSpanOrdinal"] == record["spanOrdinal"] for r in self.active.values()):
                return False
            del self.active[record["spanOrdinal"]]
            if record["metric"] == "model_call":
                self.finished_provider = record["providerOrdinal"]
        self.ordinal = record["recordOrdinal"]
        return True


def _safe(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            return None

    return wrapped


@dataclass
class Span:
    metric: str
    ordinal: int | None
    parent: int | None
    start: float | None
    provider: int | None = None
    detailed: bool = False
    reserved: bool = False
    attempts: int = 0
    attempts_known: bool = False
    acquisition: str = "unknown"


class Collector:
    def __init__(self, context, *, emit=None, clock=perf_counter):
        if not valid_context(context):
            raise ValueError("invalid attribution context")
        self.context = dict(context)
        self.emit, self.clock = emit, clock
        self.lock = threading.RLock()
        self.records = []
        self.active = {}
        self.ordinal = self.span_ordinal = self.provider_ordinal = self.call_ordinal = 0
        self.reserved = 0
        self.closed = False
        self.last_provider = 0
        self.summary = {
            "recordOrdinal": 1,
            "kind": "summary",
            "closeReason": "unknown",
            "coverage": "complete",
            **dict.fromkeys(
                (
                    "detailEligible",
                    "detailEmitted",
                    "detailDropped",
                    "clockInvalid",
                    "unrecordedIntervals",
                    "transportAttempts",
                    "transportUnknownCalls",
                ),
                0,
            ),
            "countersSaturated": False,
            "identityExhausted": False,
            "transportAttemptsSaturated": False,
            "modelCategories": dict.fromkeys(("discover", "start", "other", "pending"), 0),
            "scopeRefusals": dict.fromkeys(FAMILIES, 0),
            "encoderAcquisition": {
                "acquired": 0,
                "fallback": 0,
                "unknown": 0,
                "priorUnowned": False,
            },
            "spans": [],
        }
        for metric in METRICS:
            self.summary["spans"].append(
                {
                    "metric": metric,
                    "availability": "available"
                    if metric in applicable(context)
                    else "not_applicable",
                    **dict.fromkeys(
                        (
                            "started",
                            "ended",
                            *OUTCOMES,
                            "activeAtClose",
                            "timed",
                            "totalDurationMs",
                        ),
                        0,
                    ),
                    "saturated": False,
                }
            )
        if _encoder_prior_unowned and "first_encoder_acquisition" in applicable(context):
            self.summary["encoderAcquisition"]["priorUnowned"] = True
            self.cell("first_encoder_acquisition")["availability"] = "unavailable"

    def cell(self, metric):
        return self.summary["spans"][METRICS.index(metric)]

    def inc(self, obj, key, amount=1, maximum=N):
        total = obj[key] + amount
        obj[key] = min(maximum, total)
        if total > maximum:
            self.summary["countersSaturated"] = True
            if "saturated" in obj:
                obj["saturated"] = True
            if key == "transportAttempts":
                self.summary["transportAttemptsSaturated"] = True

    def tick(self):
        try:
            return self.clock()
        except Exception:
            return None

    def identity(self, *, reserved=False):
        if self.ordinal >= N - (0 if reserved else self.reserved + 1):
            self.summary["identityExhausted"] = True
            return None
        self.ordinal += 1
        return self.ordinal

    def detail(self, record, deliver):
        self.inc(self.summary, "detailEligible")
        sent = False
        if deliver and record["recordOrdinal"] is not None and valid_record(record, self.context):
            try:
                if self.emit is None or self.emit(record) is not False:
                    self.records.append(dict(record))
                    sent = True
            except Exception:
                pass
        self.inc(self.summary, "detailEmitted" if sent else "detailDropped")
        return sent

    @_safe
    def begin(self, metric):
        with self.lock:
            if (
                self.closed
                or metric not in applicable(self.context)
                or self.cell(metric)["availability"] != "available"
            ):
                return None
            if metric == "model_call":
                # An untracked response must never inherit an earlier provider.
                self.last_provider = 0
            cell = self.cell(metric)
            self.inc(cell, "started")
            if len(self.active) >= 32:
                self.inc(self.summary, "unrecordedIntervals")
                return None
            self.span_ordinal = min(N + 1, self.span_ordinal + 1)
            if metric == "model_call":
                self.provider_ordinal = min(N + 1, self.provider_ordinal + 1)
            if self.span_ordinal > N or self.provider_ordinal > N:
                self.summary["identityExhausted"] = True
                self.inc(self.summary, "unrecordedIntervals")
                return None
            owner = _parent.get()
            if owner is not None and id(owner) not in self.active:
                owner = None
            parent = owner.ordinal if owner is not None else None
            span = Span(
                metric,
                self.span_ordinal,
                parent,
                self.tick(),
                self.provider_ordinal if metric == "model_call" else None,
            )
            budget = len(self.records) + self.reserved + 2 <= 128
            credits = N - self.ordinal - self.reserved - 1 >= 2
            eligible = budget and credits and (owner is None or owner.detailed)
            ordinal = self.identity()
            if eligible:
                self.reserved += 1
                span.reserved = True
            record = {
                "recordOrdinal": ordinal,
                "kind": "span",
                "metric": metric,
                "spanOrdinal": span.ordinal,
                "parentSpanOrdinal": parent,
                "phase": "begin",
            }
            if span.provider is not None:
                record["providerOrdinal"] = span.provider
            span.detailed = self.detail(record, eligible)
            self.active[id(span)] = span
            return span

    @_safe
    def attempt(self, span):
        with self.lock:
            if self.closed or span is None or id(span) not in self.active:
                return
            span.attempts_known = True
            span.attempts = min(N + 1, span.attempts + 1)
            self.inc(self.summary, "transportAttempts")

    @_safe
    def end(self, span, *, outcome="returned", category="other", response_class="no_tools"):
        with self.lock:
            if self.closed or span is None or id(span) not in self.active:
                return
            if any(child.parent == span.ordinal for child in self.active.values()):
                # Keep the unresolved interval active; never fabricate an end.
                return
            del self.active[id(span)]
            cell = self.cell(span.metric)
            self.inc(cell, "ended")
            self.inc(cell, outcome)
            end = self.tick()
            elapsed = None
            if type(end) in (int, float) and type(span.start) in (int, float):
                delta = (end - span.start) * 1000
                if math.isfinite(delta) and 0 <= delta <= D:
                    elapsed = int(delta)
            if elapsed is None:
                self.inc(self.summary, "clockInvalid")
            else:
                self.inc(cell, "timed")
                self.inc(cell, "totalDurationMs", elapsed, SUM)
            if span.reserved:
                self.reserved -= 1
            record = {
                "recordOrdinal": self.identity(reserved=span.reserved),
                "kind": "span",
                "metric": span.metric,
                "spanOrdinal": span.ordinal,
                "parentSpanOrdinal": span.parent,
                "phase": "end",
                "durationMs": elapsed,
                "outcome": outcome,
            }
            if span.metric == "model_call":
                self.last_provider = span.provider
                self.inc(self.summary["modelCategories"], category)
                if not span.attempts_known:
                    self.inc(self.summary, "transportUnknownCalls")
                record.update(
                    providerOrdinal=span.provider,
                    category=category,
                    responseClass=response_class,
                    transportAttempts=min(N, span.attempts) if span.attempts_known else None,
                    transportAttemptsSaturated=span.attempts > N,
                )
            if span.metric == "first_encoder_acquisition":
                record["acquisitionResult"] = span.acquisition
                self.inc(self.summary["encoderAcquisition"], span.acquisition)
            self.detail(record, span.detailed)

    @_safe
    def refusal(self, family, call, provider):
        with self.lock:
            if (
                self.closed
                or self.context["contextKind"] != "turn"
                or self.context["producerRole"] != "ordinary"
                or family not in FAMILIES
            ):
                return
            self.inc(self.summary["scopeRefusals"], family)
            record = {
                "recordOrdinal": self.identity(),
                "kind": "scope_refusal",
                "callOrdinal": call,
                "providerOrdinal": provider,
                "family": family,
            }
            self.detail(record, len(self.records) + self.reserved < 128)

    @_safe
    def finish(self, reason):
        with self.lock:
            if self.closed:
                return None
            self.closed = True
            self.summary["closeReason"] = reason
            for cell in self.summary["spans"]:
                cell["activeAtClose"] = cell["started"] - cell["ended"]
            for span in self.active.values():
                if span.metric == "model_call" and not span.attempts_known:
                    self.inc(self.summary, "transportUnknownCalls")
            self.summary["modelCategories"]["pending"] = self.cell("model_call")["activeAtClose"]
            self.reserved = 0
            self.summary["recordOrdinal"] = self.identity(reserved=True)
            incomplete = (
                self.summary["clockInvalid"]
                or self.summary["unrecordedIntervals"]
                or self.summary["identityExhausted"]
                or self.summary["countersSaturated"]
                or self.summary["transportUnknownCalls"]
                or self.summary["encoderAcquisition"]["priorUnowned"]
                or any(
                    c["activeAtClose"] or c["availability"] == "unavailable"
                    for c in self.summary["spans"]
                )
            )
            self.summary["coverage"] = "partial" if incomplete else "complete"
            if not any(c["availability"] == "available" for c in self.summary["spans"]):
                self.summary["coverage"] = "unavailable"
            self.active.clear()
            if not valid_summary(self.summary, self.context):
                return None
            result = deepcopy(self.summary)
            if self.emit is not None:
                try:
                    self.emit(result)
                except Exception:
                    pass
            self.emit = None
            return result


def current():
    return _current.get()


@contextmanager
def bind_collector(collector):
    token = _current.set(collector)
    parent = _parent.set(None)
    try:
        yield
    finally:
        _parent.reset(parent)
        _current.reset(token)


def exception_outcome(error):
    # Use the existing exception classes, never text or elapsed-time heuristics.
    from .cancellation import CooperativeCancellationError
    from .execution_deadline import DeadlineExhausted

    if isinstance(error, DeadlineExhausted):
        return "deadline"
    if isinstance(error, (CooperativeCancellationError, KeyboardInterrupt)):
        return "cancelled"
    return "failed"


@contextmanager
def phase(metric):
    c = current()
    span = c.begin(metric) if c else None
    token = _parent.set(span)
    try:
        yield span
    except BaseException as error:
        if c:
            c.end(span, outcome=exception_outcome(error))
        raise
    else:
        if c:
            c.end(span)
    finally:
        _parent.reset(token)


def measured(metric, fn, *args, **kwargs):
    with phase(metric):
        return fn(*args, **kwargs)


def classify(names, registered):
    if type(names) is not list or any(type(n) is not str or n not in registered for n in names):
        return "other", "invalid_tools"
    if not names:
        return "other", "no_tools"
    kinds = set(names)
    if kinds == {"computer_discover"}:
        return "discover", "discover_only"
    if kinds == {"computer_start"}:
        return "start", "start_only"
    return (
        ("other", "mixed")
        if kinds & {"computer_start", "computer_discover"}
        else ("other", "other_tools")
    )


def model_call(fn):
    @wraps(fn)
    def wrapped(**kwargs):
        c = current()
        if c is None:
            return fn(**kwargs)
        span = c.begin("model_call")
        token = _parent.set(span)
        try:
            result = fn(**kwargs)
        except BaseException as error:
            c.end(
                span,
                outcome=exception_outcome(error),
                category="other",
                response_class="no_response",
            )
            raise
        else:
            try:
                calls = result.tool_calls
                names = [x.name for x in calls] if type(calls) is list else None
                registered = {t["function"]["name"] for t in (kwargs.get("tools") or [])}
                category, cls = classify(names, registered)
            except Exception:
                category, cls = "other", "invalid_tools"
            c.end(span, category=category, response_class=cls)
            return result
        finally:
            _parent.reset(token)

    return wrapped


@_safe
def transport_attempt():
    c, span = current(), _parent.get()
    if c and span and span.metric == "model_call":
        c.attempt(span)


def tool_call(collector):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            c = collector
            if c is None or c.closed:
                return fn(*args, **kwargs)
            with c.lock:
                c.call_ordinal = min(N + 1, c.call_ordinal + 1)
                ordinal = c.call_ordinal
                if ordinal > N:
                    c.summary["identityExhausted"] = True
            token = _tool.set([c, ordinal, c.last_provider, False])
            try:
                return fn(*args, **kwargs)
            finally:
                _tool.reset(token)

        return wrapped

    return decorate


@_safe
def scope_refusal(family):
    binding = _tool.get()
    if binding and not binding[3]:
        binding[3] = True
        binding[0].refusal(family, binding[1], binding[2])


@contextmanager
def encoder_acquisition():
    global _encoder_prior_unowned
    with phase("first_encoder_acquisition") as span:
        if span is None:
            # Includes a late cache miss after the captured owner closed.
            _encoder_prior_unowned = True
        yield span


@_safe
def request_context(raw, method):
    """Inspect duplicates only in attribution; leave business JSON parsing intact."""

    class Pairs(dict):
        def __init__(self, pairs):
            super().__init__(pairs)
            self.duplicates = set()
            seen = set()
            for key, _ in pairs:
                if key in seen:
                    self.duplicates.add(key)
                seen.add(key)

    parsed = json.loads(raw, object_pairs_hook=Pairs)
    if "computer_use_attribution" in parsed.duplicates:
        return None
    value = parsed.get("computer_use_attribution")

    def plain(node):
        if isinstance(node, Pairs):
            if node.duplicates:
                raise ValueError("duplicate attribution key")
            return {k: plain(v) for k, v in node.items()}
        if type(node) is list:
            return [plain(x) for x in node]
        return node

    return opt_in(plain(value), method)


@_safe
def rpc_reply(collector, envelope):
    if collector is None or collector.context["contextKind"] == "turn":
        return envelope
    summary = collector.finish("returned" if envelope.get("ok") is True else "failed")
    if summary is None:
        return envelope
    sidecar = {
        "version": 1,
        "context": dict(collector.context),
        "accepted": True,
        "records": list(collector.records),
        "summary": summary,
    }
    if len(json.dumps(sidecar, ensure_ascii=False).encode()) <= 65536:
        return {**envelope, "computer_use_attribution": sidecar}
    return envelope


def job_turn(fn):
    @wraps(fn)
    def wrapped(bridge, session, job, *args, **kwargs):
        collector = job.computer_use_attribution
        with bind_collector(collector):
            try:
                return fn(bridge, session, job, *args, **kwargs)
            except BaseException as error:
                if collector:
                    collector.finish(exception_outcome(error))
                raise
            finally:
                if collector and not collector.closed:
                    collector.finish("unknown")

    return wrapped


@_closed_validation
def valid_sidecar(reply, binding):
    """Validate only attribution in an authenticated RPC reply; never rewrite business data."""
    if (
        type(reply) is not dict
        or reply.get("id") != binding["requestId"]
        or not valid_identity(reply.get("id"), numeric=True)
        or not valid_identity(binding["requestId"], numeric=True)
    ):
        return False
    sidecar = reply.get("computer_use_attribution")
    context = binding["context"]
    if not (
        type(sidecar) is dict
        and _data(sidecar)
        and valid_context(context)
        and sidecar.get("context") == context
        and _integer(sidecar.get("version"), 1, 1)
        and sidecar.get("accepted") is True
    ):
        return False
    if len(json.dumps(sidecar, ensure_ascii=False).encode()) > 65536:
        return False
    if context["contextKind"] == "turn":
        return (
            set(sidecar) == {"version", "context", "accepted"}
            and binding["method"] == "chat.send"
            and reply.get("ok") is True
            and binding.get("newJob") is True
            and valid_identity(reply.get("result", {}).get("session_id"))
            and valid_identity(reply.get("result", {}).get("job_id"))
        )
    if (
        set(sidecar) != {"version", "context", "accepted", "records", "summary"}
        or binding["method"]
        != ("config.get" if context["contextKind"] == "management" else "session.create")
        or type(sidecar["records"]) is not list
        or len(sidecar["records"]) > 128
    ):
        return False
    admission = Admission(context)
    return all(admission.accept(r) for r in sidecar["records"]) and admission.accept(
        sidecar["summary"]
    )


@_closed_validation
def accept_event(envelope, binding, admission):
    """Closed authenticated event admission; rejected telemetry never advances cursors."""
    from datetime import datetime

    if (
        type(envelope) is not dict
        or set(envelope)
        != {
            "protocol_version",
            "session_id",
            "job_id",
            "run_id",
            "sequence",
            "timestamp",
            "type",
            "payload",
        }
        or envelope["protocol_version"] != "1"
        or envelope["type"] != "computer_use_attribution"
        or len(json.dumps(envelope, ensure_ascii=False).encode()) > 20480
        or not binding.get("bridgeIsCurrent", True)
        or binding.get("state") == "finalized"
        or not _integer(envelope["sequence"], 2**53 - 1, 1)
        or envelope["sequence"] <= binding.get("lastSequence", 0)
    ):
        return False
    for key, field in [("session_id", "sessionId"), ("job_id", "jobId")]:
        if not valid_identity(envelope[key]) or envelope[key] != binding[field]:
            return False
    run = envelope["run_id"]
    if not valid_identity(run, nullable=True) or (
        binding.get("runId") is not None and binding["runId"] != run
    ):
        return False
    timestamp = envelope["timestamp"]
    if (
        type(timestamp) is not str
        or len(timestamp.encode()) > 64
        or datetime.fromisoformat(timestamp.replace("Z", "+00:00")).tzinfo is None
    ):
        return False
    payload = envelope["payload"]
    if (
        type(payload) is not dict
        or set(payload) != {"version", "source", "context", "record"}
        or not _integer(payload["version"], 1, 1)
        or payload["source"] != "engine"
        or payload["context"] != binding["context"]
        or payload["context"]["contextKind"] != "turn"
    ):
        return False
    summary = payload["record"].get("kind") == "summary"
    if (binding.get("state") == "closing" and not summary) or (
        summary and binding.get("summaryAccepted")
    ):
        return False
    if not admission.accept(payload["record"]):
        return False
    binding["lastSequence"] = envelope["sequence"]
    if run is not None:
        binding["runId"] = run
    if summary:
        binding["summaryAccepted"] = True
    return True
