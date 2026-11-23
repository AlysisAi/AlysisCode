"""Run owl scenarios against the real owls runtime with a scripted model, then check them.

    python3 -m scripts.qa.owls_scenarios.run                 # every scenario
    python3 -m scripts.qa.owls_scenarios.run --scenario ID   # one (repeatable)
    python3 -m scripts.qa.owls_scenarios.run --evidence DIR  # also write each run's evidence

Each run builds a fresh store in a temporary folder, sets up the owl as the scenario says, and
runs its steps through ``BotRuntime`` wired as the owls service wires it (the reply checker
and the action reviewer on), with the scripted model as every model call and fakes for the web,
websites and Cloud computers. It then freezes the evidence (``freeze``) and evaluates the
scenario's checks on it. Run from the engine repository root with the engine importable
(``PYTHONPATH=src``). No model is called and nothing reaches the network.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import checks as checking
from . import scenario as scenarios
from .fakes import CloudProvider, LocalBrowsers, Web, offline_web
from .model import ScriptedModel

OWNER = "scenario-owner"
# A run step runs at most this many turns, so a script that keeps queueing work still ends.
MAX_TURNS_PER_RUN = 12
# The model capacities a scenario may declare, as the model registry reads them.
MODEL_ENV = {
    "context_window": "ALYSIS_CONTEXT_WINDOW",
    "max_output_tokens": "ALYSIS_MAX_OUTPUT_TOKENS",
    "image_input": "ALYSIS_SUPPORTS_VISION",
    "reasoning": "ALYSIS_SUPPORTS_REASONING",
}


@dataclass
class Result:
    id: str
    title: str
    seconds: float
    evidence: dict
    verdicts: list[dict]
    problems: list[str] = field(default_factory=list)
    # The chat as the owls API returns it (GET /v1/bots/{id}, /v1/chats/{id}, /v1/tasks/{id}).
    export: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.problems and all(v["passed"] for v in self.verdicts)


class ScenarioFailure(Exception):
    """The scenario could not be set up or one of its steps could not be taken."""


def _jsonable(value: Any) -> Any:
    observation = getattr(value, "observation", None)
    if observation is not None and not isinstance(value, dict):
        value = observation
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return str(value)


@contextlib.contextmanager
def model_capacities(model: dict):
    """The model registry reads exactly the capacities the scenario declares, whatever the
    shell running it has set."""
    saved = {name: os.environ.get(name) for name in MODEL_ENV.values()}
    try:
        for name in MODEL_ENV.values():
            os.environ.pop(name, None)
        for key, name in MODEL_ENV.items():
            if key in model:
                value = model[key]
                os.environ[name] = (
                    ("true" if value else "false") if isinstance(value, bool) else str(value)
                )
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class Run:
    """One scenario's run: its store, owl, runtime, fakes and what was seen."""

    def __init__(self, scenario: dict, folder: Path, web: Web):
        from alysis_code.bots.computer import ComputerViews
        from alysis_code.bots.models import (
            BotInput,
            FileInput,
            MemoryInput,
            TimeZoneSettings,
        )
        from alysis_code.bots.scheduling import ScheduleInput
        from alysis_code.bots.store import Store

        self.scenario = scenario
        self.store = Store(folder / "owls.sqlite3")
        settings = scenario.get("settings") or {}
        if settings.get("time_zone"):
            self.store.time_zone_settings(OWNER, TimeZoneSettings(time_zone=settings["time_zone"]))
        owl = scenario.get("owl") or {}
        self.bot = self.store.save_bot(
            OWNER,
            BotInput(
                name=owl.get("name", "Owl"),
                purpose=owl.get("purpose", ""),
                instructions=owl.get("instructions", ""),
                memory_enabled=owl.get("memory", True),
                files_enabled=owl.get("files", True),
            ),
        )
        self.chat = self.store.create_chat(OWNER, self.bot["id"], owl.get("chat", "Chat"))
        self.files: dict[str, str] = {}
        for item in scenario.get("library") or []:
            saved = self.store.save_file(
                OWNER, self.bot["id"], FileInput(name=item["name"], content=item["content"])
            )
            self.files[item["name"]] = saved["id"]
        for item in scenario.get("memories") or []:
            self.store.save_memory(OWNER, self.bot["id"], MemoryInput(**item))
        for item in scenario.get("routines") or []:
            self.store.save_schedule(
                OWNER,
                self.bot["id"],
                ScheduleInput.model_validate(
                    {k: v for k, v in item.items() if k != "minutes_since_saved"}
                ),
                now=time.time() - 60 * item.get("minutes_since_saved", 60),
            )

        self.model = ScriptedModel(
            scenario.get("script") or {},
            model=(scenario.get("model") or {}).get("id", "scripted-owl-model"),
            image_input=(scenario.get("model") or {}).get("image_input", False),
            tool_calling=(scenario.get("model") or {}).get("tool_calling", True),
        )
        self.model.before_owl_reply = self._owl_call
        self.model.current_task = lambda: self._running
        fakes = scenario.get("fakes") or {}
        self.web = web
        self.views = ComputerViews()
        self.browsers = self.cloud = self.cloud_factory = None
        self.lifecycle: list[str] = []
        browser_computers = desktop_computers = None
        if "browser" in fakes:
            from alysis_code.bots.browser import BrowserComputers

            self.browsers = LocalBrowsers(fakes["browser"])
            browser_computers = BrowserComputers(OWNER, self.views, self.browsers, store=self.store)
        if "cloud_computer" in fakes:
            from alysis_code.bots.browser import BrowserComputers
            from alysis_code.bots.cloud_desktop import CloudDesktopFactory, CloudDesktopFiles
            from alysis_code.bots.compute import ComputerSpec
            from alysis_code.bots.desktop import DesktopComputers

            # As the service builds a Cloud owl's computers (app.create_app).
            self.cloud = CloudProvider(fakes["cloud_computer"])
            self.cloud_factory = CloudDesktopFactory(
                OWNER,
                self.cloud,
                ComputerSpec("scenario-image"),
                folder / "computer-ledger",
                browser_enabled=True,
                store=self.store,
                max_computers=None,
            )
            self.cloud_factory.lifecycle = self._lifecycle
            desktop_computers = DesktopComputers(
                OWNER,
                self.views,
                factory=self.cloud_factory,
                max_computers=None,
                store=self.store,
                file_transfer=CloudDesktopFiles(),
            )
            browser_computers = BrowserComputers(
                OWNER, self.views, self.cloud_factory.browser, max_computers=None, store=self.store
            )
        self.browser_computers, self.desktop_computers = browser_computers, desktop_computers
        self.runtime = self._runtime(settings)

        self.refs: dict[str, str] = {}  # task id -> the scenario's name for its turn
        self.named: dict[str, str] = {}  # the scenario's name -> task id
        self.tool_runs: list[dict] = []
        self.waits: list[float | None] = []
        self.step_errors: list[str] = []
        self._hooks: list[dict] = []
        self._owl_base = 0
        self._tool_count = 0
        self._running: str | None = None
        self.clock = [time.time()]

    def _lifecycle(self, bot_id, phase):
        self.lifecycle.append(phase)
        self.views.set_lifecycle(OWNER, bot_id, phase)

    def _runtime(self, settings: dict):
        from alysis_code.bots.check_backs import CheckBacks
        from alysis_code.bots.reply_review import ReplyReview
        from alysis_code.bots.runtime import BotRuntime, RuntimeLimits

        limits = RuntimeLimits.for_model(model=self.model.model, base_url=self.model.base_url)
        runtime = BotRuntime(
            self.store,
            OWNER,
            lambda _task_id: self.model,
            limits=limits,
            browser_computers=self.browser_computers,
            desktop_computers=self.desktop_computers,
        )
        # As the owls service wires its runtime (app.create_app).
        if settings.get("reply_checker", True):
            runtime.reply_review = ReplyReview(runtime)
        runtime.action_review_enabled = settings.get("action_review", True)
        # Check-backs read pages through the fake web, on the scenario's clock, inline.
        runtime.check_backs = CheckBacks(
            self.store,
            OWNER,
            clock=lambda: self.clock[0],
            read_page=lambda url: self.web.page_text(url),
            run_check=lambda work: work(),
        )
        # What reaches a tool, in order, with its arguments: the owl's own tools and the computers'.
        runtime._tool = self._recorded("owl", runtime._tool)
        for manager in (self.browser_computers, self.desktop_computers):
            if manager is not None:
                manager.call = self._recorded("computer", manager.call)
        return runtime

    # ------------------------------------------------------------------ while the owl works

    def _recorded(self, kind: str, original):
        def call(task, name, arguments, *args, **kwargs):
            self._tool_count += 1
            self._fire("tool", self._tool_count)
            entry = {
                "task_id": task["id"],
                "turn": self.refs.get(task["id"]),
                "name": name,
                "arguments": _jsonable(
                    arguments.model_dump() if hasattr(arguments, "model_dump") else arguments
                ),
                "via": kind,
            }
            self.tool_runs.append(entry)
            try:
                result = original(task, name, arguments, *args, **kwargs)
            except BaseException as exc:
                entry["raised"] = type(exc).__name__
                entry["error"] = str(exc)[:600]
                raise
            entry["result"] = _jsonable(result)
            return result

        return call

    def _owl_call(self, number: int) -> None:
        self._fire("owl_call", number - self._owl_base)

    def _fire(self, trigger: str, number: int) -> None:
        for hook in list(self._hooks):
            if hook.get(trigger) == number:
                self._hooks.remove(hook)
                try:
                    self._act(hook)
                except Exception as exc:  # noqa: BLE001 - reported as the scenario's problem
                    self.step_errors.append(f"during {hook['do']}: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ steps

    def take(self, step: dict) -> None:
        if step["do"] == "run":
            self._hooks = [dict(hook) for hook in step.get("during") or []]
            self._owl_base = self.model.counts["owl"]
            self._tool_count = 0
            try:
                self._run_turns()
            finally:
                for hook in self._hooks:
                    self.step_errors.append(
                        f"during {hook['do']} never happened (its owl_call/tool never came)."
                    )
                self._hooks = []
            return
        self._act(step)

    def _name(self, task: dict, name: str | None = None) -> None:
        name = name or f"turn:{len(self.refs) + 1}"
        self.refs[task["id"]] = name
        self.named[name] = task["id"]

    def _task_id(self, name: str | None) -> str:
        if name is None:
            if self._running is None:
                raise ScenarioFailure("No turn is running.")
            return self._running
        if name not in self.named:
            raise ScenarioFailure(f"No turn named {name!r}.")
        return self.named[name]

    def _routine(self, name: str | None) -> dict:
        routines = self.store.list_schedules(OWNER, self.bot["id"])
        if name is not None:
            routines = [r for r in routines if r["name"] == name]
        if len(routines) != 1:
            raise ScenarioFailure(f"Expected one routine named {name!r}, found {len(routines)}.")
        return routines[0]

    def _revision(self, task_id: str) -> int:
        for task in self.store.conversation(OWNER, self.chat["id"])["tasks"]:
            if task["id"] == task_id:
                return (task.get("queued_message") or {}).get("revision", 0)
        raise ScenarioFailure("That message is not in the chat.")

    def _act(self, step: dict) -> None:
        from alysis_code.bots.models import FileInput, TaskInput
        from alysis_code.bots.queued_messages import QueuedMessageChange, QueuedMessages

        kind = step["do"]
        if kind == "send":
            files = [self.files[name] for name in step.get("files") or []]
            task = self.store.enqueue(
                OWNER,
                self.chat["id"],
                TaskInput(message=step["message"], request_id=uuid4().hex, file_ids=files),
            )
            self._name(task, step.get("id"))
        elif kind == "steer":
            task_id = self._task_id(step["message"])
            QueuedMessages(self.store, OWNER).change(
                task_id,
                QueuedMessageChange(action="steer", expected_revision=self._revision(task_id)),
            )
        elif kind in ("turn_on_routine", "pause_routine"):
            routine = self._routine(step.get("routine"))
            state = "active" if kind == "turn_on_routine" else "paused"
            self.store.decide_schedule(
                OWNER, self.bot["id"], routine["id"], state, routine["revision"]
            )
        elif kind == "cancel":
            self.runtime.cancel(self._task_id(step.get("turn")))
        elif kind == "run_routine":
            routine = self._routine(step.get("routine"))
            due = max(time.time(), routine.get("next_run_at") or 0)
            if self.store.enqueue_due_schedules(OWNER, max_calls=None, now=due) != 1:
                raise ScenarioFailure("The routine's run did not start.")
        elif kind == "advance_clock":
            self.clock[0] += step["seconds"]
        elif kind == "tick_check_backs":
            self.runtime.check_backs.tick()
        elif kind == "add_file":
            saved = self.store.save_file(
                OWNER, self.bot["id"], FileInput(name=step["name"], content=step["content"])
            )
            self.files[step["name"]] = saved["id"]
        elif kind == "mark_read":
            chat = next(
                c
                for c in self.store.list_chats(OWNER, self.bot["id"])
                if c["id"] == self.chat["id"]
            )
            self.store.mark_chat_read(OWNER, chat["id"], chat["activity_seq"])
        else:
            raise ScenarioFailure(f"Unknown step {kind!r}.")

    def _token(self, task_id: str):
        token = self.runtime.task_token(task_id)

        def wait(timeout=None):
            # A pause the engine takes (a provider's retry-after, a backoff) passes at once.
            self.waits.append(timeout)
            return token.is_cancelled

        token.wait = wait
        return token

    def _run_turns(self) -> None:
        for _ in range(MAX_TURNS_PER_RUN):
            task, background = self.store.claim(OWNER), False
            if task is None:
                task, background = self.runtime.background.claim(), True
            if task is None:
                return
            if task["id"] not in self.refs:
                self._name(task, self._auto_name(task))
            self._running = task["id"]
            try:
                self.runtime.run(task, self._token(task["id"]), background=background)
            finally:
                self._running = None

    def _auto_name(self, task: dict) -> str:
        config = task.get("config") or {}
        if self.store.schedule_context(OWNER, task["id"]):
            prefix = "routine"
        elif config.get("check_back"):
            prefix = "check-back"
        else:
            prefix = "turn"
        count = sum(1 for name in self.named if name.startswith(prefix + ":")) + 1
        return f"{prefix}:{count}" if prefix != "turn" else f"turn:{len(self.refs) + 1}"

    # ------------------------------------------------------------------ evidence

    def freeze(self) -> dict:
        """Everything the checks may read, as plain JSON, once the steps are done."""
        conversation = self.store.conversation(OWNER, self.chat["id"])
        bot_id = self.bot["id"]
        # The cards as the app reads them for this owl (its approvals, cards and questions).
        listed = {
            "action_reviews": self.runtime.action_reviews.list(bot_id, include_history=True),
            "proposals": self.runtime.proposals.list(bot_id),
            "questions": self.runtime.clarifications.list(bot_id),
        }
        turns = []
        for shown in conversation["tasks"]:
            task = self.store.task(OWNER, shown["id"])
            if task["id"] not in self.refs:
                self._name(task, self._auto_name(task))
            cards = {
                **{
                    key: [card for card in items if card.get("task_id") == task["id"]]
                    for key, items in listed.items()
                },
                "routine_suggestions": [shown["routine_suggestion"]]
                if shown.get("routine_suggestion")
                else [],
                "check_backs": shown.get("check_backs") or [],
                "app_needs": shown.get("app_needs") or [],
            }
            turns.append(
                {
                    "ref": self.refs[task["id"]],
                    "task_id": task["id"],
                    "message": task.get("message"),
                    "status": task["status"],
                    "error": task.get("error"),
                    "failure": task.get("failure"),
                    "output": task.get("output"),
                    "calls": task.get("calls"),
                    "events": [{"kind": e["kind"], "data": e["data"]} for e in task["events"]],
                    "usage": [u["status"] for u in task["usage"]],
                    "cards": _jsonable(cards),
                    "queued_message": _jsonable(shown.get("queued_message")),
                    "checked_back": _jsonable(shown.get("checked_back")),
                }
            )
        replies = [
            {"task_id": m["task_id"], "turn": self.refs.get(m["task_id"]), "text": m["content"]}
            for m in conversation["messages"]
            if m["role"] == "assistant"
        ]
        model_calls, requested = [], []
        for call in self.model.calls:
            turn = self.refs.get(call["task_id"])
            model_calls.append(
                {
                    "channel": call["channel"],
                    "number": call["number"],
                    "task_id": call["task_id"],
                    "turn": turn,
                    "tools": call["tools"],
                    "messages": call["messages"],
                    "reply": call.get("reply"),
                }
            )
            if call["channel"] == "owl":
                requested += [
                    {"owl_call": call["number"], "task_id": call["task_id"], "turn": turn, **made}
                    for made in call.get("made") or []
                ]
        routines = [
            {
                "name": r["name"],
                "state": r["state"],
                "kind": r.get("kind"),
                "pause_code": r.get("pause_code"),
                "nothing_new": bool((r.get("last_run") or {}).get("nothing_new_at")),
                "last_run": _jsonable(r.get("last_run")),
            }
            for r in self.store.list_schedules(OWNER, self.bot["id"])
        ]
        needs = self.store.needs_you(OWNER)
        chat = next(
            c for c in self.store.list_chats(OWNER, self.bot["id"]) if c["id"] == self.chat["id"]
        )
        evidence = {
            "scenario": self.scenario["id"],
            "turns": turns,
            "replies": replies,
            "model_calls": _jsonable(model_calls),
            "requested_calls": _jsonable(requested),
            "tool_runs": self.tool_runs,
            "reply_checks": [
                {"turn": turn["ref"], **event["data"]}
                for turn in turns
                for event in turn["events"]
                if event["kind"] == "reply_review_finished"
            ],
            "routines": routines,
            "needs_you": [
                {"kind": item.get("kind"), "task_id": item.get("task_id")}
                for item in needs.get("items", [])
            ],
            "chat": {"unread": _jsonable(chat.get("unread"))},
            "waits": self.waits,
            "web": self.web.seen,
            "script": {"unused": self.model.unused(), "errors": list(self.model.errors)},
            "step_errors": list(self.step_errors),
        }
        if self.browsers is not None:
            evidence["browser"] = self.browsers.evidence()
        if self.cloud is not None:
            evidence["cloud_computer"] = {**self.cloud.evidence(), "lifecycle": self.lifecycle}
        return _jsonable(evidence)

    def api_export(self) -> dict:
        """The owl, its chat and each of its turns as the owls API returns them."""
        conversation = self.store.conversation(OWNER, self.chat["id"])
        owl = next(b for b in self.store.list_bots(OWNER) if b["id"] == self.bot["id"])
        return _jsonable(
            {
                "owl": owl,
                "chat": conversation,
                "tasks": [self.store.task(OWNER, task["id"]) for task in conversation["tasks"]],
            }
        )

    def close(self) -> None:
        for manager in (self.browser_computers, self.desktop_computers):
            if manager is not None:
                with contextlib.suppress(Exception):
                    manager.close()


def run_scenario(scenario: dict) -> Result:
    """Run one scenario and check it."""
    started = time.monotonic()
    wrong = scenarios.problems(scenario)
    if wrong:
        return Result(scenario.get("id", "?"), scenario.get("title", ""), 0.0, {}, [], wrong)
    problems: list[str] = []
    fakes = scenario.get("fakes") or {}
    with (
        tempfile.TemporaryDirectory(prefix="owls-scenario-", ignore_cleanup_errors=True) as folder,
        offline_web(Web(fakes.get("web"))) as web,
        model_capacities(scenario.get("model") or {}),
    ):
        run = Run(scenario, Path(folder), web)
        try:
            for index, step in enumerate(scenario["steps"], 1):
                try:
                    run.take(step)
                except ScenarioFailure as exc:
                    problems.append(f"steps[{index}] {step['do']}: {exc}")
                    break
            evidence = run.freeze()
            export = run.api_export()
        finally:
            run.close()
    problems += [f"Script: {error}" for error in evidence["script"]["errors"]]
    problems += [
        f"Script: {count} scripted {channel} repl{'y was' if count == 1 else 'ies were'} never used."
        for channel, count in evidence["script"]["unused"].items()
    ]
    problems += [f"Step: {error}" for error in evidence["step_errors"]]
    verdicts = checking.evaluate(evidence, scenario["checks"])
    return Result(
        scenario["id"],
        scenario.get("title", ""),
        time.monotonic() - started,
        evidence,
        verdicts,
        problems,
        export,
    )


def run_all(ids: list[str] | None = None) -> list[Result]:
    paths = scenarios.all_paths()
    if ids:
        known = {scenarios.load(path)["id"]: path for path in paths}
        missing = [i for i in ids if i not in known]
        if missing:
            raise SystemExit(f"Unknown scenario(s): {', '.join(missing)}")
        paths = [known[i] for i in ids]
    return [run_scenario(scenarios.load(path)) for path in paths]


def report(results: list[Result]) -> str:
    """A short markdown report: one line per scenario, details for those that failed."""
    passed = sum(1 for r in results if r.passed)
    total = sum(r.seconds for r in results)
    lines = [
        "# Owl scenarios",
        "",
        f"{passed} of {len(results)} passed in {total:.1f} s (no model calls).",
        "",
        "| Scenario | Result | Checks | Time |",
        "|---|---|---|---|",
    ]
    for r in results:
        ok = sum(1 for v in r.verdicts if v["passed"])
        state = "pass" if r.passed else "FAIL"
        lines.append(f"| `{r.id}` | {state} | {ok}/{len(r.verdicts)} | {r.seconds:.2f} s |")
    for r in results:
        if r.passed:
            continue
        lines += ["", f"## `{r.id}`: {r.title}", ""]
        lines += [f"- Problem: {problem}" for problem in r.problems]
        for verdict in r.verdicts:
            if not verdict["passed"]:
                why = verdict["check"].get("why")
                lines.append(
                    f"- Failed `{verdict['check']['kind']}`"
                    + (f" ({why})" if why else "")
                    + f": {verdict['detail']}"
                )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scenario", action="append", help="Run only this scenario id.")
    parser.add_argument("--evidence", type=Path, help="Write each run's evidence JSON here.")
    args = parser.parse_args(argv)
    results = run_all(args.scenario)
    if args.evidence:
        args.evidence.mkdir(parents=True, exist_ok=True)
        for r in results:
            out = {"id": r.id, "problems": r.problems, "verdicts": r.verdicts, **r.evidence}
            (args.evidence / f"{r.id}.json").write_text(
                json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    sys.stdout.write(report(results))
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
