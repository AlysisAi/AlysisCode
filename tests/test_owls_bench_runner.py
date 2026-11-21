"""The owls benchmark runner, offline: the reference command and the owls service flow.

The owls service is replaced by an in-memory fake answering the same endpoints, passed to
the client as its URL opener, so no socket or model is used.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shlex
import sys
import urllib.error
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from scripts.qa.owls_bench import builders, run
from scripts.qa.owls_bench.owls_client import OwlsClient
from scripts.qa.owls_bench.score import score_record
from scripts.qa.owls_bench.tasks import parse_task

TOKEN = "t" * 40
WRITE_ANSWER = (
    "import pathlib, sys; "
    "pathlib.Path(sys.argv[2], 'answer.txt').write_text(sys.argv[1], encoding='utf-8'); "
    "print('Έτοιμο: ' + sys.argv[3])"
)
HOSTILE_PROMPT = (
    'Γράψε "x" $(touch PWNED) ; rm -rf {out}/.. `id` && echo $HOME > leak.txt\n'
    "second line {task_id} {inputs} {unknown} 'quoted'"
)


def _task(tmp_path: Path, **overrides):
    raw = {
        "id": "sample-task",
        "category": "research_to_spreadsheet",
        "language": "el",
        "split": "dev",
        "prompt": "Βρες 3 πράγματα και γράψε τα σε .xlsx.",
        "expect": {"deliverable": "xlsx", "min_rows": 2, "reply_language": "el"},
    }
    raw.update(overrides)
    task, problems = parse_task(raw, base=tmp_path)
    assert not problems, problems
    return task


# --- Reference command -------------------------------------------------------------------


def test_placeholders_are_replaced_once_inside_their_own_words():
    argv = run.parse_reference_command("tool run --prompt {prompt} --out={out} --id {task_id}")
    built = run.build_reference_argv(
        argv,
        {"prompt": "a {out} b; rm -rf /", "out": "/tmp/o", "task_id": "t1", "inputs": "/tmp/i"},
    )
    assert built == ["tool", "run", "--prompt", "a {out} b; rm -rf /", "--out=/tmp/o", "--id", "t1"]


def test_reference_command_needs_the_prompt_and_a_valid_form():
    with pytest.raises(ValueError, match=r"\{prompt\}"):
        run.parse_reference_command("tool --out {out}")
    with pytest.raises(ValueError, match="JSON"):
        run.parse_reference_command('["tool", "{prompt}"')
    with pytest.raises(ValueError, match="empty"):
        run.parse_reference_command("   ")
    assert run.parse_reference_command('["my tool", "{prompt}"]') == ["my tool", "{prompt}"]


@pytest.mark.parametrize("form", ["json", "words"])
def test_reference_app_gets_the_prompt_verbatim_without_a_shell(tmp_path, form):
    if form == "json":
        template = json.dumps(
            [sys.executable, "-c", WRITE_ANSWER, "{prompt}", "{out}", "{task_id}"]
        )
    else:
        template = " ".join(
            [
                shlex.quote(sys.executable),
                "-c",
                shlex.quote(WRITE_ANSWER),
                "{prompt}",
                "{out}",
                "{task_id}",
            ]
        )
    task = _task(tmp_path, prompt=HOSTILE_PROMPT)
    runner = run.ReferenceRunner(template, label="Reference app", timeout_s=60)
    record_dir = tmp_path / "runs" / "reference" / task.id / "run-1"
    record = runner.run(task, 1, record_dir)
    attempt = record["attempts"][0]
    assert attempt["status"] == "completed" and attempt["exit_code"] == 0
    assert attempt["files"] == ["files/attempt-1/answer.txt"]
    assert (record_dir / attempt["files"][0]).read_text(encoding="utf-8") == HOSTILE_PROMPT
    assert attempt["reply"] == "Έτοιμο: sample-task"
    assert record["program"] == Path(sys.executable).name
    assert not list(tmp_path.rglob("PWNED")) and not list(tmp_path.rglob("leak.txt"))


def test_reference_app_sees_attachments_in_its_inputs_folder(tmp_path):
    (tmp_path / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    task = _task(tmp_path, attachments=["data.csv"])
    script = (
        "import pathlib, shutil, sys; "
        "shutil.copy(pathlib.Path(sys.argv[2], 'data.csv'), pathlib.Path(sys.argv[1], 'copy.csv'))"
    )
    template = json.dumps([sys.executable, "-c", script, "{out}", "{inputs}", "{prompt}"])
    record = run.ReferenceRunner(template).run(task, 1, tmp_path / "r")
    assert record["attempts"][0]["files"] == ["files/attempt-1/copy.csv"]


def test_reference_failures_timeouts_and_missing_programs_are_recorded(tmp_path):
    task = _task(tmp_path)
    failing = run.ReferenceRunner(
        json.dumps([sys.executable, "-c", "import sys; print('no'); sys.exit(3)", "{prompt}"])
    ).run(task, 1, tmp_path / "a")
    assert failing["attempts"][0]["status"] == "exit_nonzero"
    assert failing["attempts"][0]["exit_code"] == 3 and failing["attempts"][0]["failures"] == 1
    slow = run.ReferenceRunner(
        json.dumps([sys.executable, "-c", "import time; time.sleep(30)", "{prompt}"]), timeout_s=1
    ).run(task, 1, tmp_path / "b")
    assert slow["attempts"][0]["status"] == "timeout"
    assert slow["attempts"][0]["wall_s"] < 15
    missing = run.ReferenceRunner(json.dumps([str(tmp_path / "no-such-program"), "{prompt}"])).run(
        task, 1, tmp_path / "c"
    )
    assert missing["attempts"][0]["status"] == "error"


def test_repeat_jobs_run_the_reference_twice(tmp_path):
    task = _task(
        tmp_path,
        category="repeat_job",
        expect={"deliverable": "text", "repeat": 2, "reply_language": "el"},
    )
    template = json.dumps([sys.executable, "-c", WRITE_ANSWER, "{prompt}", "{out}", "{task_id}"])
    record = run.ReferenceRunner(template).run(task, 1, tmp_path / "r")
    assert [attempt["files"] for attempt in record["attempts"]] == [
        ["files/attempt-1/answer.txt"],
        ["files/attempt-2/answer.txt"],
    ]


# --- Owls service ------------------------------------------------------------------------


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


GREEK_REPLY = "Έφτιαξα τον πίνακα με τρία μουσεία και τις πηγές τους στο αρχείο μουσεία.xlsx."


class FakeOwls:
    """The owls endpoints the runner uses, with scripted task progress."""

    def __init__(self, scenario: str = "complete", *, capabilities=None, worker_ready=True):
        self.scenario = scenario
        self.capabilities = capabilities or ["message_files", "owl_models", "conversations"]
        self.worker_ready = worker_ready
        self.calls: list[tuple[str, str, object]] = []
        self.tasks: dict[str, dict] = {}
        self.files: dict[str, dict] = {}
        self.cancelled: list[str] = []
        self.deleted: list[str] = []
        self.uploads = 0

    def __call__(self, request, timeout):
        assert request.get_header("Authorization") == f"Bearer {TOKEN}"
        method = request.get_method()
        path = urlsplit(request.full_url).path
        body = json.loads(request.data) if request.data else None
        if method in ("POST", "PUT", "PATCH"):
            assert request.data is not None  # the service requires a Content-Length
        self.calls.append((method, path, body))
        status, payload = self.handle(method, path, body)
        if status >= 400:
            raise urllib.error.HTTPError(
                request.full_url,
                status,
                "error",
                {},
                io.BytesIO(json.dumps({"detail": payload}).encode()),
            )
        return _Response(b"" if payload is None else json.dumps(payload).encode())

    def _finish(self, task: dict) -> None:
        workbook = builders.xlsx_bytes(
            {
                "Μουσεία": [
                    ["Όνομα", "Πηγή"],
                    ["Α", "https://a.example/1"],
                    ["Β", "https://b.example/2"],
                    ["Γ", "https://c.example/3"],
                ]
            }
        )
        file_id = f"file-{task['id']}"
        self.files[file_id] = {
            "id": file_id,
            "name": "μουσεία.xlsx",
            "source_task_id": task["id"],
            "size_bytes": len(workbook),
            "sha256": hashlib.sha256(workbook).hexdigest(),
            "revision": 1,
            "data": workbook,
        }
        task.update(
            status="completed",
            output=GREEK_REPLY,
            updated_at=task["created_at"] + 42.0,
            events=[
                {"kind": "queued", "data": {}, "created_at": 1000.0},
                {"kind": "running", "data": {}, "created_at": 1001.0},
                {"kind": "tools_offered", "data": {"mode": "apps_by_lookup", "tools": 130}},
                {"kind": "tool_started", "data": {"name": "browser_open", "host": "a.example"}},
                {"kind": "tool_failed", "data": {"name": "browser_open"}},
                {"kind": "tool_started", "data": {"name": "save_file", "file": "μουσεία.xlsx"}},
                {"kind": "completed", "data": {}, "created_at": 1037.5},
            ],
            usage=[
                {
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "provider_cost_usd": 0.001,
                    }
                },
                {
                    "usage": {
                        "prompt_tokens": 300,
                        "completion_tokens": 40,
                        "provider_cost_usd": 0.002,
                    }
                },
                {"usage": None},
            ],
        )

    def _advance(self, task: dict) -> None:
        task["polls"] += 1
        if task["status"] in run.TERMINAL:
            return
        if self.scenario == "complete" and task["polls"] >= 3:
            self._finish(task)
        elif self.scenario == "question" and task["polls"] >= 2:
            task.update(status="completed", output="Ποια πόλη εννοείς;")
            task["events"] = [{"kind": "clarification_requested", "data": {"question_id": "q-1"}}]
        elif self.scenario == "card" and task["polls"] >= 2:
            task["status"] = "waiting"
            task["events"] = [
                {"kind": "action_approval_requested", "data": {"name": "browser_type"}}
            ]
        elif task["polls"] >= 1:
            task["status"] = "running"

    def handle(self, method, path, body):
        if (method, path) == ("GET", "/v1/status"):
            return 200, {
                "worker_ready": self.worker_ready,
                "mode": "local",
                "model": "place-model",
                "platform": {"os": "Linux"},
                "capabilities": self.capabilities,
            }
        if (method, path) == ("GET", "/v1/models"):
            return 200, {
                "models": [
                    {
                        "id": "place-model",
                        "label": "Place",
                        "model": "m1",
                        "default": True,
                        "image_input": False,
                        "context_window": 128000,
                    },
                    {
                        "id": "vision-model",
                        "label": "Vision",
                        "model": "m2",
                        "default": False,
                        "image_input": True,
                        "context_window": 200000,
                    },
                ]
            }
        if (method, path) == ("POST", "/v1/bots"):
            assert len(body["name"]) <= 80 and len(body["request_id"]) >= 8
            return 201, {"id": "bot-1", "name": body["name"], "model": body.get("model")}
        if (method, path) == ("POST", "/v1/bots/bot-1/chats"):
            return 201, {"id": "chat-1"}
        if (method, path) == ("POST", "/v1/bots/bot-1/files/binary"):
            self.uploads += 1
            base64.b64decode(body["data_base64"], validate=True)
            return 201, {"id": f"upload-{self.uploads}", "name": body["name"]}
        if (method, path) == ("POST", "/v1/chats/chat-1/tasks"):
            task_id = f"task-{len(self.tasks) + 1}"
            self.tasks[task_id] = {
                "id": task_id,
                "status": "queued",
                "message": body["message"],
                "file_ids": body["file_ids"],
                "created_at": 1000.0,
                "updated_at": 1000.0,
                "events": [],
                "usage": [],
                "output": "",
                "polls": 0,
            }
            return 202, {"id": task_id, "status": "queued"}
        if method == "GET" and path.startswith("/v1/tasks/"):
            task = self.tasks[path.rsplit("/", 1)[1]]
            self._advance(task)
            return 200, {key: value for key, value in task.items() if key != "polls"}
        if method == "POST" and path.endswith("/cancel"):
            task = self.tasks[path.split("/")[3]]
            self.cancelled.append(task["id"])
            task["status"] = "cancelled"
            return 200, {"id": task["id"], "status": "cancelled"}
        if (method, path) == ("GET", "/v1/needs-you"):
            items = [
                {
                    "kind": "approval",
                    "id": "review-1",
                    "task_id": task["id"],
                    "text": None,
                    "action": {"tool": "browser_type", "arguments": {}},
                }
                for task in self.tasks.values()
                if task["status"] == "waiting"
            ]
            items += [
                {"kind": "question", "id": "q-1", "task_id": task["id"], "text": task["output"]}
                for task in self.tasks.values()
                if self.scenario == "question" and task["status"] == "completed"
            ]
            items.append({"kind": "failed", "id": "other", "task_id": "task-elsewhere"})
            return 200, {"items": items, "count": len(items)}
        if (method, path) == ("POST", "/v1/bots/bot-1/files/page"):
            items = [
                {k: v for k, v in item.items() if k != "data"}
                for item in self.files.values()
                if item["source_task_id"] == body["source_task_id"]
            ]
            return 200, {"items": items, "next_before": None}
        if method == "GET" and path.endswith("/download"):
            item = self.files[path.split("/")[5]]
            return 200, {"data_base64": base64.b64encode(item["data"]).decode()}
        if (method, path) == ("GET", "/v1/chats/chat-1"):
            return 200, {
                "id": "chat-1",
                "messages": [{"role": "assistant", "content": GREEK_REPLY}],
            }
        if (method, path) == ("DELETE", "/v1/bots/bot-1"):
            self.deleted.append("bot-1")
            return 204, None
        return 404, f"no route for {method} {path}"


class FakeClock:
    def __init__(self, step: float):
        self.now, self.step = 0.0, step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _runner(service: FakeOwls, **options) -> run.OwlsRunner:
    client = OwlsClient("http://127.0.0.1:7080", TOKEN, opener=service)
    return run.OwlsRunner(client, poll_s=0.0, sleep=lambda _seconds: None, **options)


def test_owls_run_records_reply_files_usage_and_steps(tmp_path):
    (tmp_path / "data.csv").write_text("Πόλη,Τιμή\nΑθήνα,1\n", encoding="utf-8")
    task = _task(tmp_path, attachments=["data.csv"])
    service = FakeOwls()
    runner = _runner(service, model="vision-model", delete_owls=True)
    assert runner.preflight([task]) == []
    assert runner.place["owl_model"]["image_input"] is True
    record_dir = tmp_path / "runs" / "owls" / task.id / "run-1"
    record = runner.run(task, 1, record_dir)
    assert record["error"] is None
    assert record["owl"]["model"] == "vision-model"
    sent = service.tasks["task-1"]
    assert sent["message"] == task.prompt and sent["file_ids"] == ["upload-1"]
    attempt = record["attempts"][0]
    assert attempt["status"] == "completed" and attempt["reply"] == GREEK_REPLY
    assert attempt["files"] == ["files/attempt-1/μουσεία.xlsx"]
    assert attempt["model_calls"] == 3
    assert attempt["tokens"] == {
        "input": 400,
        "output": 60,
        "total": 460,
        "reported_calls": 2,
        "unreported_calls": 1,
    }
    assert attempt["cost_usd"] is None  # one call did not report its cost
    assert attempt["failures"] == 1 and attempt["approvals"] == 0
    assert attempt["tool_steps"] == {"browser_open": 1, "save_file": 1}
    assert attempt["tool_modes"] == ["apps_by_lookup"]
    assert attempt["hosts"] == ["a.example"]
    # The service's own events time the turn; the poll interval adds nothing.
    assert attempt["server_wall_s"] == attempt["wall_s"] == 37.5
    assert (record_dir / "transcript.json").exists()
    assert service.deleted == ["bot-1"] and service.cancelled == []
    (record_dir / "record.json").write_text(json.dumps(record), encoding="utf-8")
    result = score_record(task, record_dir)
    assert result["deterministic"]["all_passed"], result["checks"]


def test_owls_run_stops_on_a_card_without_answering_it(tmp_path):
    task = _task(tmp_path)
    service = FakeOwls("card")
    record = _runner(service).run(task, 1, tmp_path / "r")
    attempt = record["attempts"][0]
    assert attempt["status"] == "blocked"
    assert attempt["stopped_on"] == [
        {"kind": "approval", "id": "review-1", "text": None, "tool": "browser_type"}
    ]
    assert attempt["approvals"] == 1
    assert service.cancelled == ["task-1"]
    answered = [
        call for call in service.calls if "/action-reviews/" in call[1] or "/questions/" in call[1]
    ]
    assert answered == []


def test_owls_run_that_ends_with_a_question_is_recorded_as_asking(tmp_path):
    task = _task(tmp_path)
    service = FakeOwls("question")
    record_dir = tmp_path / "r"
    record = _runner(service).run(task, 1, record_dir)
    attempt = record["attempts"][0]
    assert attempt["status"] == "asked" and attempt["questions"] == 1
    assert attempt["stopped_on"] == [
        {"kind": "question", "id": "q-1", "text": "Ποια πόλη εννοείς;"}
    ]
    assert service.cancelled == []
    (record_dir / "record.json").write_text(json.dumps(record), encoding="utf-8")
    finished = score_record(task, record_dir)["checks"][0]
    assert finished["status"] == "fail" and "asking the person" in finished["detail"]


def test_owls_run_times_out_and_cancels(tmp_path):
    task = _task(tmp_path)
    service = FakeOwls("never")
    runner = _runner(service, timeout_s=100.0, clock=FakeClock(30.0))
    attempt = runner.run(task, 1, tmp_path / "r")["attempts"][0]
    assert attempt["status"] == "timeout"
    assert service.cancelled == ["task-1"]


def test_repeat_jobs_send_the_same_task_twice_to_one_owl(tmp_path):
    task = _task(
        tmp_path,
        category="repeat_job",
        expect={"deliverable": "xlsx", "repeat": 2, "reply_language": "el"},
    )
    service = FakeOwls()
    record = _runner(service).run(task, 1, tmp_path / "r")
    assert [attempt["status"] for attempt in record["attempts"]] == ["completed", "completed"]
    assert [attempt["files"] for attempt in record["attempts"]] == [
        ["files/attempt-1/μουσεία.xlsx"],
        ["files/attempt-2/μουσεία.xlsx"],
    ]
    creates = [call for call in service.calls if call[:2] == ("POST", "/v1/bots")]
    assert len(creates) == 1 and len(service.tasks) == 2


def test_preflight_refuses_an_unready_service_an_unknown_model_and_missing_capabilities(tmp_path):
    (tmp_path / "data.csv").write_text("a\n", encoding="utf-8")
    task = _task(tmp_path, attachments=["data.csv"])
    problems = _runner(
        FakeOwls(capabilities=["conversations"], worker_ready=False), model="any"
    ).preflight([task])
    assert len(problems) == 3
    problems = _runner(FakeOwls(), model="not-offered").preflight([task])
    assert problems and "place-model, vision-model" in problems[0]


def test_owls_client_refuses_unsafe_addresses_and_keeps_the_token_out_of_reports():
    with pytest.raises(ValueError):
        OwlsClient("file:///tmp/socket", TOKEN)
    with pytest.raises(ValueError):
        OwlsClient("http://user:pass@127.0.0.1:7080", TOKEN)
    with pytest.raises(ValueError):
        OwlsClient("http://127.0.0.1:7080", "")
    assert TOKEN not in json.dumps(OwlsClient("http://127.0.0.1:7080", TOKEN).describe())


# --- Whole command -----------------------------------------------------------------------


def test_command_runs_scores_reports_and_resumes(tmp_path, capsys):
    folder = tmp_path / "tasks"
    folder.mkdir()
    tasks = [
        {
            "id": "notes-el",
            "category": "comparison_to_document",
            "language": "el",
            "split": "dev",
            "prompt": "Γράψε μια σύντομη σύγκριση σε αρχείο κειμένου.",
            "expect": {"deliverable": "text", "reply_language": "el"},
        },
        {
            "id": "notes-en",
            "category": "comparison_to_document",
            "language": "en",
            "split": "heldout",
            "prompt": "Write a short comparison as a text file.",
            "expect": {"deliverable": "text", "reply_language": "en"},
        },
    ]
    (folder / "set.json").write_text(json.dumps({"schema": 1, "tasks": tasks}), encoding="utf-8")
    template = json.dumps([sys.executable, "-c", WRITE_ANSWER, "{prompt}", "{out}", "{task_id}"])
    out = tmp_path / "out"
    arguments = [
        "--tasks",
        str(folder),
        "--out",
        str(out),
        "--split",
        "all",
        "--runs",
        "2",
        "--reference-command",
        template,
        "--reference-label",
        "Reference app",
        "--no-judge",
    ]
    assert run.main(arguments) == 0
    assert len(list((out / "runs" / "reference").glob("*/run-*/score.json"))) == 4
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    splits = report["systems"]["reference"]["splits"]
    assert set(splits) == {"dev", "heldout"}
    assert splits["dev"]["summary"]["runs"] == 2
    assert (out / "report.md").read_text(encoding="utf-8").count("split") >= 2
    assert json.loads((out / "run.json").read_text(encoding="utf-8"))["systems"][0]["program"]
    assert run.main(arguments) == 2
    assert "already has a result" in capsys.readouterr().err
    assert run.main([*arguments, "--resume"]) == 0
