"""Frozen V: one immutable capture, exact selectors, private entry control."""

from __future__ import annotations

import copy
import re
from collections import Counter
from contextlib import contextmanager

from ..computer_use_call_diagnostics import RESOLVED_START_REFUSAL_CODES
from ..llm import resolved_entry as lexical
from ..llm.types import LLMError
from .browser_start import (
    COMPUTER_ACTIONS,
    COMPUTER_TARGET_UNVERIFIED,
    BrowserStartHandoff,
    build_computer_start_tools,
    computer_plan_problem,
    computer_start_problem,
    native_unsupported_start_actions,
    valid_unverified_target_refusal,
)
from .tool_definition import ToolDef

NAME = lexical.NAME
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
HOST_CODES = frozenset(
    {
        "computer_use_target_unverified",
        "computer_use_target_stale",
        "computer_use_inventory_unavailable",
        "computer_use_activation_required",
        "computer_use_chrome_scope_required",
        "computer_use_start_request_invalid",
        "browser_start_unavailable",
        "browser_start_refused",
        "computer_start_owner_invalid",
        "computer_start_capability_lost",
    }
)
FRESH = frozenset(
    {
        "computer_use_target_stale",
        "computer_use_inventory_unavailable",
        "computer_start_inventory_incomplete",
        "computer_start_inventory_empty",
        "computer_start_owner_invalid",
        "computer_start_capability_lost",
        "browser_start_unavailable",
    }
)
RETAIN = frozenset(
    {
        "computer_use_target_unverified",
        "computer_use_activation_required",
        "computer_use_chrome_scope_required",
        "computer_use_start_request_invalid",
    }
)
CORRECTIONS = frozenset(
    {
        "computer_start_invalid_selection",
        "computer_start_ambiguous_selection",
        "computer_start_unsupported_native_actions",
        "computer_start_scope_inconsistent",
    }
)


def text(value, maximum, *, minimum=1):
    return (
        type(value) is str
        and minimum <= len(value.encode("utf-16-le", errors="surrogatepass")) // 2 <= maximum
        and not any(0xD800 <= ord(c) <= 0xDFFF for c in value)
    )


def label(value):
    return (
        text(value, 128)
        and any(not c.isspace() and c != "\ufeff" for c in value)
        and len(value.encode("utf-8")) <= 512
        and not any(
            ord(c) < 32
            or 127 <= ord(c) <= 159
            or ord(c) in {0x61C, 0x200E, 0x200F}
            or 0x202A <= ord(c) <= 0x202E
            or 0x2066 <= ord(c) <= 0x2069
            for c in value
        )
    )


def valid_descriptor(value):
    try:
        if type(value) is not dict or set(value) != {
            "version",
            "operation_id",
            "host_nonce",
            "generation_nonce",
            "inventory_nonce",
            "discovery",
        }:
            return False
        if (
            type(value["version"]) is not int
            or value["version"] != 1
            or not isinstance(value["operation_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value["operation_id"])
        ):
            return False
        if any(
            not isinstance(value[k], str) or not UUID.fullmatch(value[k])
            for k in ["host_nonce", "generation_nonce", "inventory_nonce"]
        ):
            return False
        d = value["discovery"]
        if type(d) is not dict or set(d) != {
            "version",
            "coverage",
            "application_lifecycle_version",
            "targets",
        }:
            return False
        if (
            type(d["version"]) is not int
            or d["version"] != 1
            or type(d["application_lifecycle_version"]) is not int
            or d["application_lifecycle_version"] not in (0, 1)
            or d["coverage"] not in ("complete", "incomplete", "unavailable")
        ):
            return False
        targets = d["targets"]
        if (
            type(targets) is not list
            or len(targets) > 128
            or d["coverage"] != "complete"
            and targets
        ):
            return False
        ids = set()
        for row in targets:
            if type(row) is not dict or set(row) not in (
                {"target_id", "name", "route"},
                {"target_id", "name", "route", "availability"},
            ):
                return False
            if (
                not isinstance(row["target_id"], str)
                or not UUID.fullmatch(row["target_id"])
                or row["target_id"] in ids
                or not label(row["name"])
                or row["route"] not in ("native", "chrome")
            ):
                return False
            if "availability" in row and (
                row["route"] != "native" or row["availability"] not in ("installed", "no_window")
            ):
                return False
            ids.add(row["target_id"])
        return len(lexical.canonical(d)) <= 65536 and len(lexical.canonical(value)) <= 131072
    except (KeyError, TypeError, ValueError, UnicodeError):
        return False


class Correction(dict):
    """Only the owning controller can grant the bounded selection continuation."""

    def __init__(self, value, owner):
        super().__init__(value)
        self.owner = owner


class Refusal(dict):
    def __init__(self, code, dispatch="not_dispatched"):
        super().__init__(
            version=1,
            kind="refused",
            code=code,
            dispatch=dispatch,
            fresh_turn_required=code in FRESH,
        )


class ProtocolUnavailable(ValueError):
    def __init__(self):
        super().__init__(
            "The selected model protocol does not support resolved Computer Use entry."
        )


class Entry:
    def __init__(self, descriptor, request, client, stream, cfg):
        from ..computer_use_platform_policy import refuse_unavailable_mac_w7_computer_use

        refuse_unavailable_mac_w7_computer_use()
        if not valid_descriptor(descriptor):
            raise ValueError("Invalid resolved Computer Use descriptor")
        if not lexical.eligible(client):
            raise ProtocolUnavailable()
        self.descriptor = copy.deepcopy(descriptor)
        self.request = request
        self.client = client
        self.route = self.client_route(client)
        self.stream = stream
        self.cfg_fingerprint = self.fingerprint(cfg)
        self.owner = None
        self.dispatch_call = None
        self.mode = None
        self.retired = None
        self._terminal_refusal = None
        self._terminal_refusal_value = None
        self.failed = set()
        self.corrections = 0
        self.exchange_deadline = None

    @staticmethod
    def client_route(client):
        return tuple(
            copy.deepcopy(getattr(client, k, None))
            for k in ("route_identity", "model", "base_url", "stream_assembly", "reasoning_effort")
        ) + (id(getattr(client, "_transport", None)),)

    @staticmethod
    def fingerprint(cfg):
        # Private equality only: never log config/credential values.
        return copy.deepcopy(cfg)

    def live(self, client, stream, cfg):
        return (
            self.retired is None
            and client is self.client
            and self.client_route(client) == self.route
            and stream == self.stream
            and cfg == self.cfg_fingerprint
            and lexical.eligible(client)
        )

    def retire(self, code):
        self._terminal_refusal = None
        self._terminal_refusal_value = None
        self.retired = code
        self.descriptor = None
        self.dispatch_call = None
        self.owner = None
        self.failed.clear()

    @contextmanager
    def model_request(self, client, stream, cfg):
        if not self.live(client, stream, cfg):
            self.retire("computer_start_capability_lost")
            raise LLMError("computer_start_capability_lost")
        self.owner = object()
        with lexical.parser_scope(self.owner):
            yield

    def choose_browser(self):
        if self.retired or self.mode not in (None, "browser"):
            return False
        self.mode = "browser"
        return True

    def refusal(self, code, dispatch="not_dispatched"):
        if code not in RETAIN:
            self.retire(code)
        value = Refusal(code, dispatch)
        if code not in RETAIN and code in HOST_CODES | FRESH | {
            "computer_start_response_invalid",
            "browser_start_timeout",
            "computer_selection_recovery_exhausted",
        }:
            self._terminal_refusal = value
            self._terminal_refusal_value = dict(value)
        return value

    def owns_terminal_refusal(self, value):
        # Identity and an immutable-at-issuance snapshot, not a class/JSON check.
        # A subsequent retirement invalidates the previous receipt.
        return (
            type(value) is Refusal
            and value is self._terminal_refusal
            and value == self._terminal_refusal_value
            and self.retired == value.get("code")
        )

    def terminal_refusal_evidence_code(self, value):
        """Project only the unchanged original owned receipt into closed evidence."""
        if not self.owns_terminal_refusal(value):
            return None
        code = value.get("code")
        return code if type(code) is str and code in RESOLVED_START_REFUSAL_CODES else None

    def projection(self):
        rows = self.descriptor["discovery"]["targets"]
        counts = Counter(t["name"] for t in rows)
        return [
            {
                "app": t["name"],
                "route": t["route"],
                "selection": "unique" if counts[t["name"]] == 1 else "ambiguous",
                **({"availability": t["availability"]} if "availability" in t else {}),
            }
            for t in rows
        ]

    def correction(self, code):
        self.corrections += 1
        if self.corrections > 2:
            return self.refusal("computer_selection_recovery_exhausted")
        value = dict(
            version=1,
            kind="selection_correction",
            code=code,
            not_dispatched=True,
            fresh_turn_required=False,
            available_targets=self.projection(),
        )
        if len(lexical.canonical(value)) > 65536:
            return self.refusal("computer_start_inventory_incomplete")
        return Correction(value, self)

    def valid_correction(self, value):
        return (
            type(value) is Correction
            and value.owner is self
            and not self.retired
            and value
            == dict(
                version=1,
                kind="selection_correction",
                code=value.get("code"),
                not_dispatched=True,
                fresh_turn_required=False,
                available_targets=self.projection(),
            )
            and isinstance(value["code"], str)
            and value["code"] in CORRECTIONS
        )

    def dispatch(self, call, arguments, client, stream, cfg):
        from ..computer_use_platform_policy import refuse_unavailable_mac_w7_computer_use

        refuse_unavailable_mac_w7_computer_use()
        if not self.live(client, stream, cfg):
            return self.refusal("computer_start_capability_lost")
        if not lexical.valid_proof(call, self.owner, arguments):
            return self.refusal("computer_start_response_invalid")
        self.dispatch_call = call
        try:
            return self.run(arguments)
        finally:
            self.dispatch_call = None

    def run(self, plan):
        from ..computer_use_platform_policy import refuse_unavailable_mac_w7_computer_use

        refuse_unavailable_mac_w7_computer_use()
        if self.dispatch_call is None:
            return self.refusal("computer_start_response_invalid")
        if self.mode not in (None, "resolved"):
            return self.refusal("computer_start_owner_invalid")
        self.mode = "resolved"
        discovery = self.descriptor["discovery"]
        if discovery["coverage"] != "complete":
            return self.refusal(
                "computer_start_inventory_incomplete"
                if discovery["coverage"] == "incomplete"
                else "computer_use_inventory_unavailable"
            )
        rows = discovery["targets"]
        if not rows:
            return self.refusal("computer_start_inventory_empty")
        counts = Counter(t["name"] for t in rows)
        by_label = {t["name"]: t for t in rows}
        if computer_start_problem(plan, by_label):
            return self.correction("computer_start_invalid_selection")
        if (
            not text(plan["goal"], 2000)
            or not any(not c.isspace() and c != "\ufeff" for c in plan["goal"])
            or any(not text(c, 1000) for c in plan["constraints"])
            or any(not label(s["app"]) for s in plan["app_steps"])
        ):
            return self.correction("computer_start_invalid_selection")
        if any(counts[s["app"]] != 1 for s in plan["app_steps"]):
            return self.correction("computer_start_ambiguous_selection")
        selected = [by_label[n] for n in dict.fromkeys(s["app"] for s in plan["app_steps"])]
        if any(t["target_id"] in self.failed for t in selected):
            return self.refusal(COMPUTER_TARGET_UNVERIFIED)
        actions = [a for a in COMPUTER_ACTIONS if any(a in s["actions"] for s in plan["app_steps"])]
        if (
            any(t["route"] == "native" for t in selected)
            and native_unsupported_start_actions().intersection(actions)
            or "quit_application" in actions
            and (
                discovery["application_lifecycle_version"] != 1
                or any(t["route"] != "native" for t in selected)
            )
        ):
            return self.correction("computer_start_unsupported_native_actions")
        if computer_plan_problem(plan, by_label):
            return self.correction("computer_start_scope_inconsistent")
        selection = dict(
            target_ids=[t["target_id"] for t in selected],
            allowed_actions=actions,
            goal=plan["goal"],
            constraints=list(plan["constraints"]),
        )
        result = self.request(copy.deepcopy(plan), selection, self.dispatch_call.id)
        refuse_unavailable_mac_w7_computer_use()
        if type(result) is BrowserStartHandoff:
            self.retire("consumed")
            return result
        # Only a process-local bridge result establishes in-flight loss. A wire
        # denial remains a plain dictionary with pre-commit dispatch semantics.
        if (
            type(result) is Refusal
            and result.get("code")
            in ("computer_start_owner_invalid", "computer_start_capability_lost")
            and result == Refusal(result["code"], "unknown")
        ):
            return self.refusal(result["code"], "unknown")
        if (
            type(result) is not dict
            or not isinstance(result.get("error"), str)
            or result.get("error")
            not in HOST_CODES
            | {
                "browser_start_timeout",
                "computer_start_response_invalid",
            }
        ):
            return self.refusal("computer_start_response_invalid", "unknown")
        code = result["error"]
        if code == COMPUTER_TARGET_UNVERIFIED:
            if not valid_unverified_target_refusal(
                result, tuple(selection["target_ids"]), frozenset(t["target_id"] for t in rows)
            ):
                return self.refusal("computer_start_response_invalid")
            self.failed.update(result["unverified_target_ids"])
        dispatch = (
            "unknown"
            if code in {"browser_start_timeout", "computer_start_response_invalid"}
            else "not_dispatched"
        )
        return self.refusal(code, dispatch)

    def tool(self):
        from ..computer_use_platform_policy import refuse_unavailable_mac_w7_computer_use

        refuse_unavailable_mac_w7_computer_use()
        # Reuse the unchanged plan schema, removing inventory enumeration from delivery.
        schema = build_computer_start_tools(lambda _: None, {"targets": []})[
            "computer_start"
        ].as_openai_tool()["function"]["parameters"]
        schema = copy.deepcopy(schema)
        schema["properties"]["app_steps"]["items"]["properties"]["app"] = {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
        }
        schema["properties"]["app_steps"]["items"]["properties"]["actions"]["items"]["enum"] = list(
            COMPUTER_ACTIONS
        )
        from ..skills.computer_use import ordinary_entry_guidance

        return ToolDef(
            name=NAME,
            description=ordinary_entry_guidance(resolved=True),
            parameters=schema,
            run=self.run,
            metadata={"host_owned": True, "ephemeral_io": True},
        )
