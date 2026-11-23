"""The scenario format: loading a scenario file and saying what is wrong with one.

A scenario is one JSON object (see README.md for a full example):

- ``id``, ``title``, ``pins`` (plain words: the behaviour it pins and where that came from),
  ``language`` (the language the person writes in);
- ``owl``: ``name``, ``purpose``, ``instructions``, ``memory`` and ``files`` (on by default);
- ``settings``: ``time_zone``, ``reply_checker`` and ``action_review`` (on, as in the service);
- ``library`` (files: ``name``, ``content``), ``memories`` (``title``, ``content``) and
  ``routines`` (``name``, ``prompt``, ``timing``, ``needs``) the owl already has;
- ``model``: the scripted model's declared capabilities: ``id``, ``image_input``,
  ``context_window``, ``max_output_tokens``, ``tool_calling``;
- ``script``: the replies per channel (``owl``, ``reply_checker``, ``action_reviewer``, ...),
  see model.py;
- ``fakes``: ``web``, ``browser`` or ``cloud_computer`` (fakes.py) for what would reach outside;
- ``steps``: what happens, in order (STEPS); a ``run`` step may carry ``during``: actions taken
  while the owl works, each at ``owl_call`` N (while the model writes its N-th reply of that
  run) or ``tool`` N (just before the N-th tool of that run starts);
- ``checks``: checks.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from .checks import problems as check_problems
from .model import CHANNELS, CUT_OFF_WIRES, OWL, OWL_REPLY_KINDS

SCENARIOS = Path(__file__).parent / "scenarios"

# What each step does, with the fields it needs.
STEPS = {
    "send": ("message",),  # The person sends a message (``id`` names its turn; ``files``).
    "run": (),  # The owl works: every turn that can start runs to its end.
    "steer": ("message",),  # The person presses Send now on a waiting message (its ``id``).
    "turn_on_routine": (),  # The person turns a routine on (``routine``: its name).
    "pause_routine": (),  # The person pauses a routine (``routine``: its name).
    "cancel": (),  # The person presses Stop (``turn``: its id; default the running turn).
    "run_routine": (),  # A routine (``routine``) comes due and its run starts.
    "advance_clock": ("seconds",),  # Time passes for check-backs.
    "tick_check_backs": (),  # The service checks waiting check-backs once.
    "add_file": ("name", "content"),  # A file arrives in the owl's Library.
    "mark_read": (),  # The person reads the chat.
}
DURING = frozenset({"send", "steer", "turn_on_routine", "pause_routine", "cancel", "add_file"})
TOP_LEVEL = frozenset(
    {
        "id",
        "title",
        "pins",
        "language",
        "owl",
        "settings",
        "library",
        "memories",
        "routines",
        "model",
        "script",
        "fakes",
        "steps",
        "checks",
    }
)


def load(path: Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        scenario = json.load(handle)
    scenario.setdefault("id", path.stem)
    return scenario


def all_paths() -> list[Path]:
    return sorted(SCENARIOS.glob("*.json"))


def _owl_reply_problems(entry, where: str) -> list[str]:
    if not isinstance(entry, dict):
        return [f"{where}: a reply is an object."]
    kinds = [key for key in OWL_REPLY_KINDS if key in entry]
    if "call" in kinds and "say" in kinds:
        kinds.remove("say")  # The text before the calls.
    if len(kinds) != 1:
        return [f"{where}: one of {OWL_REPLY_KINDS} (found {kinds})."]
    found = []
    calls = entry.get("call") or []
    if "cut_off" in entry:
        cut = entry["cut_off"]
        if not isinstance(cut, dict) or cut.get("wire", "chat_completions") not in CUT_OFF_WIRES:
            found.append(f"{where}: cut_off.wire is one of {CUT_OFF_WIRES}.")
        calls = (cut.get("call") or []) if isinstance(cut, dict) else []
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("tool"), str):
            found.append(f"{where}: each call names its tool.")
    if not isinstance(entry.get("repeat", 1), int) or entry.get("repeat", 1) < 1:
        found.append(f"{where}: repeat is a positive number.")
    return found


def problems(scenario: dict) -> list[str]:
    """What is wrong with a scenario (empty when it can run)."""
    found = [f"Unknown key {key!r}." for key in scenario if key not in TOP_LEVEL]
    for key in ("id", "pins", "steps", "checks"):
        if not scenario.get(key):
            found.append(f"A scenario needs {key!r}.")
    script = scenario.get("script") or {}
    for channel in script:
        if channel not in CHANNELS:
            found.append(f"Unknown script channel {channel!r}; known: {sorted(CHANNELS)}.")
    for channel, entries in script.items():
        if not isinstance(entries, list) and not (
            channel == "reply_checker" and entries == "ready"
        ):
            found.append(
                f"script.{channel} is a list of replies"
                + (' or "ready".' if channel == "reply_checker" else ".")
            )
    for index, entry in enumerate(script.get(OWL) or [], 1):
        found += _owl_reply_problems(entry, f"script.owl[{index}]")
    for index, step in enumerate(scenario.get("steps") or [], 1):
        kind = step.get("do") if isinstance(step, dict) else None
        if kind not in STEPS:
            found.append(f"steps[{index}]: unknown step {kind!r}; known: {sorted(STEPS)}.")
            continue
        found += [
            f"steps[{index}]: {kind} needs {key!r}." for key in STEPS[kind] if key not in step
        ]
        for hook in step.get("during") or []:
            if kind != "run":
                found.append(f"steps[{index}]: only a run step has during.")
            elif hook.get("do") not in DURING:
                found.append(f"steps[{index}]: during takes {sorted(DURING)}.")
            elif [key in hook for key in ("owl_call", "tool")].count(True) != 1:
                found.append(f"steps[{index}]: each during action has owl_call or tool.")
            else:
                found += [
                    f"steps[{index}]: {hook['do']} needs {key!r}."
                    for key in STEPS[hook["do"]]
                    if key not in hook
                ]
    for index, check in enumerate(scenario.get("checks") or [], 1):
        found += [f"checks[{index}]: {problem}" for problem in check_problems(check)]
    return found
