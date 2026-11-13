"""Opt-in real GLM/Linux repair and reporting study, without a native app.

The study injects one real source regression immediately before independent
verification. Neither verification results nor model responses are fabricated.
This qualifies bounded recovery from that injected failure, not failure rates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

TESTS = """import unittest
from notes import normalize_text
class ExistingChecks(unittest.TestCase):
    def test_greek(self): self.assertEqual(normalize_text("ε\\u0301"), "έ")
    def test_japanese(self): self.assertEqual(normalize_text("日本語"), "日本語")
    def test_whitespace(self): self.assertEqual(normalize_text("  Ω\\r\\n"), "  Ω\\r\\n")
    def test_empty(self): self.assertEqual(normalize_text(""), "")
"""


def linux():
    import uvicorn

    from alysis_code.bots import app as app_module
    from alysis_code.bots import coding_worker
    from alysis_code.bots.app import Settings, create_app
    from alysis_code.bots.coding import CodingResult, WorkspaceRegistration
    from alysis_code.bots.models import BotInput

    config = json.loads(sys.stdin.readline())
    inject_regression = config.get("inject_regression", True)
    provider_errors = []
    original_client = app_module.OpenAICompatClient

    class ObservedClient(original_client):
        def chat(self, **kwargs):
            try:
                return super().chat(**kwargs)
            except Exception as exc:
                chain = []
                current = exc
                while current is not None and len(chain) < 6:
                    chain.append(type(current).__name__)
                    current = current.__cause__
                provider_errors.append({"types": chain})
                raise

    app_module.OpenAICompatClient = ObservedClient
    root = Path("/tmp/coding-repair")
    root.mkdir()
    settings = Settings(
        root / "bots.db",
        config["token"],
        config["key"],
        config["base_url"],
        "glm-5.3-flash",
        max_calls=16 if inject_regression else 12,
        output_tokens=2048,
        provider_key="zai_coding_plan",
        reasoning_effort="low",
        coding_token=config["coding_token"],
        coding_calls=12 if inject_regression else 8,
    )
    app = create_app(settings)
    store, coding = app.state.store, app.state.runtime.coding
    bot = store.save_bot(
        settings.owner,
        BotInput(name="Computer view test", purpose="Maintain a Unicode normalization project."),
    )
    store.save_bot(settings.owner, BotInput(name="No computer test", purpose="Remain idle."))
    scope, worker_id = uuid4().hex, uuid4().hex
    coding.register(
        WorkspaceRegistration(id=scope, worker_id=worker_id, name="Synthetic Unicode project")
    )
    coding.bind(bot["id"], scope)
    workspace, state = root / "workspace", root / "state"
    workspace.mkdir()
    state.mkdir()
    (workspace / "notes.py").write_text("def normalize_text(text):\n    return text.strip()\n")
    (workspace / "test_existing.py").write_text(TESTS)
    (workspace / "README.md").write_text(
        "Normalize to Unicode NFC, preserving whitespace. Use Python standard library only. Run python -m unittest discover -v.\n"
    )
    broker = coding_worker.Broker("http://127.0.0.1:7080", config["coding_token"], worker_id)
    runner = coding_worker.ScopedLinuxRunner(workspace=workspace, close_stdin=True)
    stop = threading.Event()
    checks = []
    original_verify = coding_worker.verify_workspace

    def verification(commands, **kwargs):
        injected = inject_regression and not checks
        if injected:
            # An explicit fault in the synthetic workspace, after actual model work.
            (workspace / "notes.py").write_text("def normalize_text(text):\n    return text\n")
        result = original_verify(commands, **kwargs)
        checks.append(dict(injected_source_regression=injected, **result))
        return result

    coding_worker.verify_workspace = verification
    failures = []
    sessions = []
    original_make_session = coding_worker.make_session

    def capture_session(*args):
        session = original_make_session(*args)
        sessions.append(session)
        return session

    coding_worker.make_session = capture_session

    def heartbeat():
        while not stop.is_set():
            coding.heartbeat(scope, worker_id)
            stop.wait(1)

    def work():
        try:
            while not stop.is_set():
                job = coding.claim(scope, worker_id)
                if job:
                    result = coding_worker.execute(
                        job,
                        workspace=workspace,
                        state=state,
                        broker=broker,
                        settings=dict(
                            base_url=config["base_url"],
                            model="glm-5.3-flash",
                            verify=["python -m unittest discover -v"],
                        ),
                        stop=stop,
                        runner=runner,
                    )
                    coding.finish(job["id"], CodingResult(worker_id=worker_id, **result))
                stop.wait(0.25)
        except Exception as exc:
            failures.append(type(exc).__name__)

    thread = threading.Thread(target=work, daemon=True)
    beat = threading.Thread(target=heartbeat, daemon=True)
    beat.start()
    thread.start()

    @app.get("/study/evidence")
    def evidence():
        return dict(
            checks=checks,
            injected_regression=inject_regression,
            failures=failures,
            provider_errors=provider_errors,
            usage=store.usage(settings.owner),
            original_tests_unchanged=hashlib.sha256(
                (workspace / "test_existing.py").read_bytes()
            ).hexdigest()
            == hashlib.sha256(TESTS.encode()).hexdigest(),
            source=(workspace / "notes.py").read_text(),
            # Synthetic workspace only. Preserve tool diagnostics and completion
            # decisions so a failed live run can be investigated without replay.
            sessions=[
                dict(messages=session.messages, events=session.store.events_snapshot())
                for session in sessions
            ],
        )

    try:
        uvicorn.run(app, host="0.0.0.0", port=7080, access_log=False, log_level="warning")
    finally:
        stop.set()
        thread.join(5)
        beat.join(2)
        broker.client.close()


def exercise(client, bot, other, private, container):
    result = {
        "kind": "Real GLM, HTTP broker and bwrap coding completion study",
        "passed": False,
    }

    def api(method, path, data=None):
        response = client.request(method, path, **({"json": data} if data is not None else {}))
        response.raise_for_status()
        return response.json()

    try:
        chat = api("POST", f"/v1/bots/{bot['id']}/chats", {})
        task = api(
            "POST",
            f"/v1/chats/{chat['id']}/tasks",
            dict(
                request_id=uuid4().hex,
                message="Fix normalize_text in the existing project: normalize arbitrary Unicode strings to NFC while preserving all whitespace. Keep the original tests unchanged, use only the standard library, and run the full tests. Report actual changes and verification results in English.",
            ),
        )
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            task = api("GET", f"/v1/tasks/{task['id']}")
            if task["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                break
            time.sleep(0.25)
        result["task"] = task
        evidence = api("GET", "/study/evidence")
        assert not evidence["failures"], evidence["failures"]
        assert evidence["original_tests_unchanged"]
        if evidence["injected_regression"]:
            assert len(evidence["checks"]) == 2, evidence["checks"]
            assert not evidence["checks"][0]["passed"] and evidence["checks"][1]["passed"]
        else:
            assert len(evidence["checks"]) == 1 and evidence["checks"][0]["passed"]
        assert task["status"] == "completed", task["error"]
        receipt = task["coding_job"]["result"]
        result["overall_job_completed"] = receipt["status"] == "completed"
        result["overall_verified_success"] = receipt["outcome"].get("verified_success", False)
        assert result["overall_job_completed"], receipt["outcome"]
        assert result["overall_verified_success"], receipt["outcome"]
        assert receipt["verification"]["repair_attempts"] == int(evidence["injected_regression"])
        assert receipt["verification"]["complete"] and receipt["verification"]["passed"]
        result["passed"] = True
    finally:
        result["evidence"] = api("GET", "/study/evidence")
        (private / "coding-repair-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
        print(
            json.dumps(
                dict(
                    passed=result["passed"],
                    usage=result["evidence"]["usage"],
                    evidence=str(private),
                )
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--linux", action="store_true")
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--app", type=Path)
    parser.add_argument("--existing-network")
    parser.add_argument(
        "--ordinary",
        action="store_true",
        help="No injected fault; product's default eight execution calls",
    )
    args = parser.parse_args()
    if args.linux:
        linux()
    elif args.execute_live and args.app:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
        import secrets

        from bots_coding_context_study import validate_plan
        from bots_computer_view import host

        from alysis_code.config import load_config, resolve_profile_api_key
        from alysis_code.profiles import get_profile

        cfg = load_config()
        profile = get_profile(cfg, "zai-coding-plan")
        validate_plan(profile)
        key = resolve_profile_api_key(cfg, "zai-coding-plan").key
        if not key:
            raise RuntimeError("Saved Z.ai plan unavailable; no fallback.")
        host(
            args.app.resolve(),
            None,
            study_script=Path(__file__).resolve(),
            bootstrap=dict(
                key=key,
                base_url=profile.base_url,
                coding_token=secrets.token_urlsafe(36),
                inject_regression=not args.ordinary,
            ),
            lazy_browser=True,
            exercise=exercise,
            port=7092,
            image="bots-durable-computer-20261019:dev",
            name_prefix="bots-coding-repair-20261019-",
            existing_network=args.existing_network,
            memory="2g",
        )
    else:
        parser.error("Explicit --execute-live and --app required (16 calls maximum).")
