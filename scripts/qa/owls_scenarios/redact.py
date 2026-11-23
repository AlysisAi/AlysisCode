"""Turn a real exported owl chat into a scenario, with everything personal taken out.

    python3 -m scripts.qa.owls_scenarios.redact EXPORT.json --id NEW-ID [--out PATH]

The export is JSON as the owls API returns it: ``{"chat": GET /v1/chats/{id}, "tasks":
[GET /v1/tasks/{id}, ...], "owl": GET /v1/bots/{id}}`` (``chat`` and ``owl`` optional), or one
task, or a list of tasks. Each of the person's turns becomes a ``send`` and a ``run``; its events
become the scripted model's rounds (``model_started`` starts a round, its ``tool_started``
steps are that round's calls, with the topic, file name and site the events kept), the turn's
reply its last round, and its reply checks the checker's verdicts. Checks start as what
happened: the turn's status, the tools it asked for and whether it replied.

Ids, names, e-mail addresses, the hosts and paths of web addresses and every free text
(messages, replies, errors, notes, topics) become stable placeholders: the same value always
gets the same one, so the shape of the chat survives. Before anything is written, a leak scan
reads the whole scenario for every original value and for any address, e-mail or id-like token
that is not a placeholder; if one remains, nothing is written. The scenario then needs its
``pins``, arguments and checks finished by hand (see README.md).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

EMAIL = re.compile(r"[^\s@<>()\[\]\"',;:]+@[^\s@<>()\[\]\"',;:]+\.[^\s@<>()\[\]\"',;:]+")
URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s<>\"']+")
LONG_ID = re.compile(
    r"\b[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}\b|\b[0-9a-fA-F]{24,}\b"
)
# What placeholders look like: anything else matching EMAIL, URL or LONG_ID in the output leaks.
PLACEHOLDER_EMAIL = re.compile(r"^person-\d+@example\.invalid$")
PLACEHOLDER_URL = re.compile(r"^https://site-\d+\.example(/page-\d+)?/?$")
# Positions in an export that hold the engine's own fixed words (kinds, states, tool names), not
# anything a person, a page or a model wrote. Every other string counts as personal.
STRUCTURAL = (
    ("events", "*", "kind"),
    ("events", "*", "data", "name"),
    ("events", "*", "data", "decision"),
    ("events", "*", "data", "issue"),
    ("events", "*", "data", "variant"),
    ("status",),
    ("failure", "kind"),
    ("failure", "code"),
    ("messages", "*", "role"),
    ("usage", "*", "status"),
)
# The engine's own identifiers (kinds, modes, sources) anywhere in an export: lowercase ASCII words
# joined by underscores. What the redactor replaces is checked whatever its shape.
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*")
# Shorter originals are too common to tell apart from ordinary words of the scenario format.
MIN_LEAK_CHARS = 4
DRAFT = "<a draft the export does not hold>"
# Every model call starts with model_started; these events right after it say the call was the
# reply checker's, the action reviewer's or the history summary's, not the owl's own.
OTHER_CALLS = frozenset(
    {
        "reply_review_started",
        "action_review_call",
        "context_compaction_started",
        "memory_capture_started",
    }
)
# A model call the export says failed and was sent again; its error's words are not kept.
RETRIED = {"error": {"message": "LLM error 503: temporarily unavailable", "status": 503}}
TITLE = "Replay of an exported chat (redacted)"
PINS = (
    "Replayed from a redacted export. Replace with the behaviour this scenario pins and where it "
    "came from, finish the calls' arguments, and add the checks that would fail without the fix."
)


class Leak(Exception):
    """An original value would remain in the scenario; nothing was written."""


def _script_of(text: str) -> str:
    """The writing system of a text's first letter, as a hint for the scenario's author."""
    for char in text:
        if char.isalpha():
            name = unicodedata.name(char, "")
            return name.split(" ", 1)[0].lower() if name else "letters"
    return "no letters"


class Redactor:
    """Stable placeholders per kind of value; remembers every original it replaced."""

    def __init__(self):
        self.maps: dict[str, dict[str, str]] = {}
        self.originals: set[str] = set()

    def _place(self, kind: str, value: str, make) -> str:
        table = self.maps.setdefault(kind, {})
        if value not in table:
            table[value] = make(len(table) + 1)
        self.originals.add(value)
        return table[value]

    def text(self, value: str | None) -> str:
        if not value:
            return ""
        for found in EMAIL.findall(value) + URL.findall(value):
            self.originals.add(found)
        return self._place(
            "text",
            value,
            lambda n: f"<text-{n}: {len(value)} characters, {_script_of(value)}>",
        )

    def host(self, value: str) -> str:
        return self._place("host", value.lower(), lambda n: f"site-{n}.example")

    def url(self, value: str) -> str:
        from urllib.parse import urlsplit

        parts = urlsplit(value)
        if not parts.hostname:
            return self.text(value)
        self.originals.add(value)
        base = f"https://{self.host(parts.hostname)}"
        path = parts.path.strip("/")
        if not path:
            return base + "/"
        return base + self._place("path", path, lambda n: f"/page-{n}")

    def file(self, value: str) -> str:
        stem, dot, extension = value.rpartition(".")
        keep = dot and stem and extension.isascii() and extension.isalnum() and len(extension) <= 5
        return self._place(
            "file", value, lambda n: f"file-{n}.{extension.lower()}" if keep else f"file-{n}"
        )

    def name(self, value: str, kind: str = "Owl") -> str:
        return self._place("name:" + kind, value, lambda n: f"{kind} {n}")

    def counts(self) -> dict[str, int]:
        return {kind: len(table) for kind, table in self.maps.items()}


# ---------------------------------------------------------------------------------- reading


def _tasks(export: Any) -> tuple[dict | None, dict | None, list[dict]]:
    if isinstance(export, list):
        return None, None, export
    if isinstance(export, dict) and "events" in export:
        return None, None, [export]
    if isinstance(export, dict) and isinstance(export.get("tasks"), list):
        return export.get("owl"), export.get("chat"), export["tasks"]
    raise ValueError("Expected a task, a list of tasks, or {chat, tasks, owl}.")


def _arguments(data: dict, redactor: Redactor) -> dict:
    """What a step's event kept of its arguments, as placeholders."""
    arguments: dict[str, Any] = {}
    if data.get("topic"):
        arguments["query"] = redactor.text(data["topic"])
    if data.get("file"):
        arguments["name"] = redactor.file(data["file"])
    if data.get("host"):
        arguments["url"] = f"https://{redactor.host(data['host'])}/"
    return arguments


def _rounds(task: dict, redactor: Redactor) -> list[dict]:
    """The scripted model's replies for one turn, from its events."""
    rounds: list[list[dict]] = []
    retries: list[int] = []
    events = task.get("events") or []
    for index, event in enumerate(events):
        following = events[index + 1].get("kind") if index + 1 < len(events) else None
        if event.get("kind") == "model_started" and following in OTHER_CALLS:
            continue  # The reply checker's or another helper's call, not one of the owl's.
        if event.get("kind") == "model_started":
            rounds.append([])
            retries.append(0)
        elif event.get("kind") == "provider_retry" and rounds:
            retries[-1] += 1  # That round's call failed and was sent again.
        elif event.get("kind") == "tool_started" and rounds:
            data = event.get("data") or {}
            rounds[-1].append({"tool": data.get("name"), "arguments": _arguments(data, redactor)})
    script: list[dict] = []
    for index, calls in enumerate(rounds):
        last = index == len(rounds) - 1
        script += [dict(RETRIED) for _ in range(retries[index])]
        if calls:
            script.append({"call": calls})
        elif last and task.get("status") == "failed":
            failure = task.get("failure") or {}
            error: dict[str, Any] = {"message": redactor.text(task.get("error") or "failed")}
            if failure.get("status") is not None:
                error["status"] = failure["status"]
            if failure.get("code") is not None:
                error["code"] = failure["code"]
            script.append({"error": error})
        elif last:
            script.append({"say": redactor.text(task.get("output") or "")})
        else:
            # A reply that was written again (withheld, rewritten or discarded): its words are
            # not in the export.
            script.append({"say": DRAFT})
    return script


def scenario_from(export: Any, scenario_id: str, redactor: Redactor | None = None) -> dict:
    """The redacted scenario for an export (not yet leak-scanned: see ``write``)."""
    redactor = redactor or Redactor()
    owl, chat, tasks = _tasks(export)
    for task in tasks:
        for key in ("id", "bot_id", "chat_id"):
            if task.get(key):
                redactor.originals.add(str(task[key]))
    steps: list[dict] = []
    checks: list[dict] = []
    script: dict[str, list] = {"owl": [], "reply_checker": []}
    for number, task in enumerate(tasks, 1):
        if not (task.get("message") or "").strip():
            continue  # A turn the engine started (a routine run, a check-back): not replayed.
        ref = f"turn-{number}"
        steps += [
            {"do": "send", "id": ref, "message": redactor.text(task["message"])},
            {"do": "run"},
        ]
        rounds = _rounds(task, redactor)
        script["owl"] += rounds
        for event in task.get("events") or []:
            if event.get("kind") == "reply_review_finished":
                data = event.get("data") or {}
                verdict = {
                    "decision": data.get("decision"),
                    "reason": redactor.text(data.get("reason") or "checked"),
                }
                if data.get("issue"):
                    verdict["issue"] = data["issue"]
                script["reply_checker"].append(verdict)
        checks.append({"kind": "turn", "turn": ref, "status": task.get("status")})
        asked = [{"name": call["tool"]} for r in rounds for call in r.get("call") or []]
        checks.append(
            {"kind": "tools", "turn": ref, "source": "requested", "order": "exact", "expect": asked}
        )
        checks.append({"kind": "reply", "turn": ref, "published": bool(task.get("output"))})
    if not script["reply_checker"]:
        script["reply_checker"] = "ready"
    owl_spec: dict[str, Any] = {"name": redactor.name((owl or {}).get("name") or "Owl")}
    for key in ("purpose", "instructions"):
        if (owl or {}).get(key):
            owl_spec[key] = redactor.text(owl[key])
    if chat and chat.get("title"):
        owl_spec["chat"] = redactor.name(chat["title"], "Chat")
    return {
        "id": scenario_id,
        "title": TITLE,
        "pins": PINS,
        "owl": owl_spec,
        "script": script,
        "steps": steps,
        "checks": checks,
    }


# ---------------------------------------------------------------------------------- leak scan


def _structural(path: tuple) -> bool:
    for pattern in STRUCTURAL:
        tail = path[-len(pattern) :] if len(path) >= len(pattern) else None
        if tail and all(p == "*" or p == t for p, t in zip(pattern, tail, strict=True)):
            return True
    return False


def personal_strings(export: Any) -> set[str]:
    """Every string of an export that a person, a page or a model wrote (and every address,
    e-mail and id inside one): what must never reach a scenario. The engine's own fixed words
    (STRUCTURAL positions, IDENTIFIER values) are left out."""
    found: set[str] = set()

    def walk(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, (*path, key))
        elif isinstance(value, list):
            for item in value:
                walk(item, (*path, "*"))
        elif isinstance(value, str):
            if not _structural(path) and not IDENTIFIER.fullmatch(value):
                found.add(value)
            found.update(EMAIL.findall(value))
            found.update(URL.findall(value))
            found.update(LONG_ID.findall(value))

    walk(export, ())
    return {value for value in found if len(value.strip()) >= MIN_LEAK_CHARS}


# The scenario format's own words (its fixed texts and placeholders): an original that is one of
# them alone (an owl named "Owl", a chat titled "Chat") is no evidence of a leak.
TEMPLATE_WORDS = frozenset(
    re.findall(
        r"\w+", " ".join((DRAFT, TITLE, PINS, "Owl Chat text file site page person")).casefold()
    )
)


def leaks(scenario: dict, originals: set[str]) -> list[str]:
    """Where an original value, or an address, e-mail or id that is not a placeholder, remains.
    Says where, never the value itself."""
    problems: list[str] = []
    originals = {o for o in originals if o.strip().casefold() not in TEMPLATE_WORDS}

    def walk(value, path):
        if isinstance(value, dict):
            for key, item in value.items():
                walk(key, (*path, "<key>"))
                walk(item, (*path, str(key)))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, (*path, str(index)))
        elif isinstance(value, str):
            where = ".".join(path) or "<root>"
            folded = value.casefold()
            if any(o.casefold() in folded for o in originals):
                problems.append(f"{where}: an original value remains")
            for email in EMAIL.findall(value):
                if not PLACEHOLDER_EMAIL.match(email):
                    problems.append(f"{where}: an e-mail address remains")
            for url in URL.findall(value):
                if not PLACEHOLDER_URL.match(url):
                    problems.append(f"{where}: a web address remains")
            if LONG_ID.search(value):
                problems.append(f"{where}: an id remains")

    walk(scenario, ())
    return problems


def redact(export: Any, scenario_id: str) -> dict:
    """The redacted scenario for ``export``; raises ``Leak`` when any original value remains."""
    redactor = Redactor()
    scenario = scenario_from(export, scenario_id, redactor)
    found = leaks(scenario, personal_strings(export) | redactor.originals)
    if found:
        raise Leak("; ".join(sorted(set(found))))
    return scenario


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("export", type=Path, help="The exported chat (JSON).")
    parser.add_argument("--id", required=True, help="The new scenario's id (kebab-case).")
    parser.add_argument("--out", type=Path, help="Where to write it (default: scenarios/ID.json).")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", args.id):
        parser.error("--id is lowercase words joined by hyphens.")
    out = args.out or Path(__file__).parent / "scenarios" / f"{args.id}.json"
    if out.exists():
        parser.error(f"{out} exists; choose another --id or --out.")
    export = json.loads(args.export.read_text(encoding="utf-8"))
    try:
        scenario = redact(export, args.id)
    except Leak as exc:
        sys.stderr.write(f"Nothing was written: {exc}.\n")
        return 2
    out.write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(f"Wrote {out}. Finish its pins, arguments and checks before adding it.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
