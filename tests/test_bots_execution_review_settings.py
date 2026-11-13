"""The real background execution loop honors the task's saved review choice."""

from dataclasses import replace

import pytest
from test_bots_browser_runtime import ClientFixture, response
from test_bots_desktop_runtime import run_task
from test_bots_desktop_runtime import study as execution_study

from alysis_code.bots.models import ReplyReviewSettings
from alysis_code.bots.reply_review import ReplyReview
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import encode

study = execution_study


@pytest.mark.parametrize("enabled", [False, True, None])
def test_background_report_respects_snapshot_and_exact_call_allowance(study, enabled):
    store, task, manager, instances = study
    if enabled is None:
        task["config"].pop("reply_review_enabled", None)  # Legacy tasks default on.
    else:
        task["config"]["reply_review_enabled"] = enabled
    with store.connect(write=True) as db:
        db.execute("UPDATE tasks SET config=? WHERE id=?", (encode(task["config"]), task["id"]))
    # A later account change must not alter a queued/approved task's choice.
    store.reply_review_settings("owner", ReplyReviewSettings(enabled=enabled is False))
    expected = enabled is not False
    answer = "The supplied fixture contains no file contents to inspect."
    client = ClientFixture(
        [
            response(answer),
            *(
                [response('{"decision":"ready","reason":"Accurate blocker report."}')]
                if expected
                else []
            ),
        ]
    )
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    runtime.reply_review = ReplyReview(runtime)
    runtime.limits = replace(
        runtime.limits, compaction_enabled=False, calls_per_task=2 if expected else 1
    )
    run_task(runtime, task)
    final = store.task("owner", task["id"])
    assert final["status"] == "completed", final["error"]
    assert final["output"] == answer
    assert final["calls"] == len(client.requests) == (2 if expected else 1)
    assert sum(e["kind"] == "reply_review_finished" for e in final["events"]) == int(expected)
    assert runtime.reply_review is not None  # Do not disable other concurrent tasks.
    assert not instances


def test_stop_during_background_generation_with_review_off_withholds_answer(study):
    store, task, manager, _ = study
    task["config"]["reply_review_enabled"] = False
    with store.connect(write=True) as db:
        db.execute("UPDATE tasks SET config=? WHERE id=?", (encode(task["config"]), task["id"]))

    class Client:
        model = "fixture"
        supports_tool_calling = True
        calls = 0

        def chat(self, **kwargs):
            self.calls += 1
            runtime.cancel(task["id"])
            return response("This reply must not be published.")

    client = Client()
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    runtime.reply_review = ReplyReview(runtime)
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    run_task(runtime, task)
    final = store.task("owner", task["id"])
    assert final["status"] == "cancelled"
    assert client.calls == 1
    assert "This reply must not be published." not in (final["output"] or "")
    assert not any(e["kind"] == "reply_review_finished" for e in final["events"])
