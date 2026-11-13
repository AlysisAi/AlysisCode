"""Isolated real HTTP/runtime smoke with a scripted provider; zero external calls.

Run against this checkout in a disposable Linux container. Only a temporary
SQLite database and the process's own loopback listener are used.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn

from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.reply_review import REVIEW_INSTRUCTIONS
from alysis_code.bots.store import Store
from alysis_code.llm.types import LLMResponse, ToolCall


def verify():
    with tempfile.TemporaryDirectory(prefix="alysis-peer-clarifications-") as directory:
        settings = Settings(
            Path(directory) / "study.db",
            "fixture-" + uuid4().hex,
            "fixture",
            "https://example.invalid",
            "scripted-fixture",
            max_calls=12,
        )
        store = Store(settings.database)
        label = "Νεφέλη / 東京 " + uuid4().hex
        draft = "Draft: [DATE TBD]\n" + label
        answer = "6 November, 10:30\n" + label
        original = (
            "Prepare a guide; ask for missing date and keep independent work moving. " + label
        )

        class Client:
            model = "scripted-fixture"
            supports_tool_calling = True

            def __init__(self, task_id):
                self.task = store.task(settings.owner, task_id)
                self.step = 0

            def chat(self, **kw):
                if kw["messages"][0].get("content") == REVIEW_INSTRUCTIONS:
                    # Scripted review response for transport/state qualification;
                    # this is not a live-model semantic quality check.
                    return LLMResponse('{"decision":"ready","reason":"scripted fixture"}', [], {})
                self.step += 1
                if self.task["message"] == original:
                    if self.step == 1:
                        return LLMResponse(
                            "",
                            [
                                ToolCall(
                                    "q",
                                    "ask_clarification",
                                    {
                                        "question": "Which date and time?",
                                        "options": ["Keep placeholders"],
                                    },
                                )
                            ],
                            {},
                        )
                    if self.step == 2:
                        return LLMResponse(
                            "",
                            [
                                ToolCall(
                                    "draft", "save_file", {"name": "guide.md", "content": draft}
                                )
                            ],
                            {},
                        )
                    return LLMResponse("Draft saved. Date is pending.", [], {})
                if self.task["message"] == answer and self.step == 1:
                    assert original in str(kw["messages"])
                    first = store.list_files(settings.owner, self.task["bot_id"], latest_only=True)[
                        0
                    ]
                    return LLMResponse(
                        "",
                        [
                            ToolCall(
                                "revision",
                                "save_file",
                                {
                                    "name": "guide.md",
                                    "content": answer,
                                    "previous_file_id": first["id"],
                                },
                            )
                        ],
                        {},
                    )
                return LLMResponse(
                    "56" if self.task["message"] == "Unrelated arithmetic" else "Revision saved.",
                    [],
                    {},
                )

        app = create_app(settings, client_factory=Client)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=7101, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 8
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server.started
            with httpx.Client(
                base_url="http://127.0.0.1:7101",
                headers={"Authorization": "Bearer " + settings.token},
            ) as http:

                def request(method, path, body=None):
                    result = http.request(method, path, json=body)
                    result.raise_for_status()
                    return result.json()

                def settled(task_id):
                    end = time.monotonic() + 8
                    while time.monotonic() < end:
                        task = request("GET", f"/v1/tasks/{task_id}")
                        if task["status"] in ("completed", "failed", "interrupted", "cancelled"):
                            assert task["status"] == "completed", task
                            return task
                        time.sleep(0.02)
                    raise AssertionError("Fixture task did not settle")

                bot = request(
                    "POST",
                    "/v1/bots",
                    {
                        "name": "Revision smoke",
                        "purpose": "Synthetic HTTP check",
                        "memory_enabled": False,
                    },
                )
                base = f"/v1/bots/{bot['id']}"
                chat = request("POST", base + "/chats", {})

                def send(text):
                    return request(
                        "POST",
                        f"/v1/chats/{chat['id']}/tasks",
                        {"message": text, "request_id": uuid4().hex},
                    )

                settled(send(original)["id"])
                first = request("GET", base + "/files")[0]
                settled(send("Unrelated arithmetic")["id"])
                question = request("GET", base + "/questions")[0]
                assert question["status"] == "pending"
                decision = {"action": "answer", "answer": answer, "expected_revision": 1}
                path = base + "/questions/" + question["id"]
                reply = request("PATCH", path, decision)
                assert request("PATCH", path, decision)["answer_task_id"] == reply["answer_task_id"]
                settled(reply["answer_task_id"])
                files = request("GET", base + "/files")
                assert len(files) == 2
                latest = next(f for f in files if f["is_latest"])
                assert request("GET", base + "/files/" + first["id"])["content"] == draft
                assert request("GET", base + "/files/" + latest["id"])["content"] == answer
                assert latest["revision"] == 2
                assert request("GET", base + "/questions")[0]["status"] == "answered"
                calls = request("GET", "/v1/status")["usage"]["calls"]
                return {
                    "passed": True,
                    "evidence": "Real Linux HTTP and worker; scripted provider only",
                    "fixture_calls": calls,
                    "external_model_calls": 0,
                    "versions": 2,
                    "checks": [
                        "pending question survives unrelated chat",
                        "identical answer is idempotent",
                        "original request reaches answer task",
                        "same-name revision retains both exact contents",
                    ],
                }
        finally:
            server.should_exit = True
            thread.join(timeout=8)
            assert not thread.is_alive()


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False))
