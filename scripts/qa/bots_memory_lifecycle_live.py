"""Opt-in live GLM approval/edit/replacement/deletion study after real compaction.

Uses the isolated Linux continuity harness. Synthetic archive pressure is explicit;
API changes exercise the same memory endpoints as the app. No production data.
Answers require semantic inspection as well as the structural checks below.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from functools import partial
from pathlib import Path
from uuid import uuid4


def exercise(client, bot, other, private, container, *, edit_delete_only=False):
    result = {
        "kind": "Live GLM/local Linux; synthetic history pressure; no Azure/native UI",
        "tasks": {},
        "states": {},
        "summaries": {},
        "checks": {},
        "failures": [],
    }
    path = f"/v1/bots/{bot['id']}/memories"

    def api(method, url, data=None):
        reply = client.request(method, url, **({"json": data} if data is not None else {}))
        if reply.status_code >= 400:
            raise RuntimeError(f"Study HTTP {reply.status_code} at {url}")
        return reply.json() if reply.content else None

    chat = api("GET", "/study/evidence")["seed"]["chat"]["id"]

    def persist():
        result["evidence"] = api("GET", "/study/evidence")
        (private / "memory-lifecycle-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )

    def check(label, value):
        result["checks"][label] = bool(value)
        print(json.dumps({"check": label, "passed": bool(value)}), flush=True)

    def ask(label, prompt):
        task = api(
            "POST", f"/v1/chats/{chat}/tasks", {"message": prompt, "request_id": uuid4().hex}
        )
        end = time.monotonic() + 600
        while time.monotonic() < end:
            task = api("GET", f"/v1/tasks/{task['id']}")
            if task["status"] in {"completed", "failed", "interrupted", "cancelled"}:
                break
            time.sleep(0.4)
        else:
            api("POST", f"/v1/tasks/{task['id']}/cancel")
            raise RuntimeError(
                "Study watchdog cancelled a stalled request, not a product deadline."
            )
        result["tasks"][label] = task
        result["states"][label] = api("GET", path)
        if task["status"] != "completed":
            result["failures"].append(
                {"label": label, "status": task["status"], "error": task.get("error")}
            )
        print(
            json.dumps(
                {
                    "task": label,
                    "status": task["status"],
                    "calls": task["calls"],
                    "answer": task["output"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        persist()
        result["summaries"][label] = result["evidence"]["summaries"]
        return task

    def active():
        return [m for m in api("GET", path) if m["status"] == "active"]

    def pending():
        return [m for m in api("GET", path) if m["status"] == "pending"]

    def probe(label, expected_active, expected_pending):
        task = ask(
            label,
            "What weekly-update preference is currently saved for me, and do I have anything awaiting review? Please don't change any saved information.",
        )
        check(
            label + "_state",
            {m["id"] for m in active()} == set(expected_active)
            and len(pending()) == expected_pending,
        )
        events = task["events"]
        check(label + "_compacted", any(e["kind"] == "context_compacted" for e in events))
        check(
            label + "_project_only_summary",
            all(
                item["kind"] == "project_context"
                for summary in result["summaries"][label]
                for item in json.loads(summary["content"])["items"]
            ),
        )
        traces = [t for t in result["evidence"]["traces"] if t["task_id"] == task["id"]]
        snapshots = []
        for trace in traces:
            messages = trace.get("request_messages", [])
            if not trace["tool_free"] or len(messages) != 2:
                continue
            try:
                payload = json.loads(messages[1]["content"])
            except (ValueError, TypeError):
                continue
            if "current_memory_state" in payload:
                snapshots.append(payload["current_memory_state"])
        check(
            label + "_authoritative_snapshots",
            len(snapshots) >= 2
            and all(
                s is not None
                and {m["id"] for m in s["approved_memories"]} == set(expected_active)
                and s["pending_count"] == expected_pending
                for s in snapshots
            ),
        )
        return task

    try:
        result["project_codename"] = "Δάφνη-" + uuid4().hex[:8]
        ask(
            "propose_initial",
            f"For our fictional pilot {result['project_codename']}, we decided it must work offline. "
            "Separately, please remember that for weekly project updates I prefer a detailed narrative with examples.",
        )
        proposals = pending()
        check("initial_pending", len(proposals) == 1 and not active())
        if len(proposals) != 1:
            raise RuntimeError("Model did not create exactly one memory proposal.")
        original = proposals[0]
        result["pressure"] = api("POST", "/study/pressure")
        probe("pending", [], 1)

        api("PATCH", path + f"/{original['id']}", {"status": "active"})
        probe("approved", [original["id"]], 0)

        edited = api(
            "PUT",
            path + f"/{original['id']}",
            {
                "title": original["title"],
                "content": "For weekly project updates, use three concise bullet points with risks first.",
            },
        )
        probe("edited", [edited["id"]], 0)

        current = edited
        if not edit_delete_only:
            ask(
                "propose_replacement",
                "Change my saved weekly-update preference to a short paragraph ending with a question. Propose this as a replacement for review.",
            )
            replacement = pending()
            check(
                "replacement_targets_current",
                len(replacement) == 1 and replacement[0]["replaces_memory_id"] == edited["id"],
            )
            if len(replacement) != 1:
                raise RuntimeError("Expected one real pending replacement.")
            probe("replacement_pending", [edited["id"]], 1)
            api("PATCH", path + f"/{replacement[0]['id']}", {"status": "rejected"})
            probe("replacement_rejected", [edited["id"]], 0)

            ask(
                "propose_accepted_replacement",
                "I'd now like my saved weekly-update preference to be a compact table of progress and blockers. Propose this change for review.",
            )
            replacement = pending()
            if len(replacement) != 1:
                raise RuntimeError("Expected one new pending replacement.")
            check(
                "accepted_replacement_targets_current",
                replacement[0]["replaces_memory_id"] == edited["id"],
            )
            current = api("PATCH", path + f"/{replacement[0]['id']}", {"status": "active"})
            probe("replacement_approved", [current["id"]], 0)

        api("DELETE", path + f"/{current['id']}")
        probe("deleted", [], 0)
        before = api("GET", "/study/evidence")
        subprocess.run(
            ["docker", "exec", container, "kill", "-KILL", str(before["pid"])],
            check=True,
            capture_output=True,
        )
        for _ in range(120):
            try:
                after = api("GET", "/study/evidence")
                if after["pid"] != before["pid"]:
                    break
            except Exception:
                pass
            time.sleep(0.25)
        else:
            raise RuntimeError("Study child failed to restart.")
        check("deleted_state_survives_restart", not active() and not pending())
        probe("deleted_after_restart", [], 0)
        ask(
            "ordinary_update_after_deletion",
            "Give me a project update from these facts: planning is complete, the prototype is ready, and testing starts tomorrow. Use my saved weekly-update preference if there is one; otherwise choose a simple neutral format. Don't save a new preference.",
        )
        check("no_implicit_memory_recreation", not active() and not pending())
        ask(
            "historical_recall",
            "Which weekly-update format did I originally ask you to remember? I am asking about the past, not asking you to save it again. Distinguish that historical preference from what is currently saved.",
        )
        check("historical_recall_does_not_recreate", not active() and not pending())
        project = ask(
            "project_context_retained",
            "What was our fictional pilot's codename and its agreed operating constraint? Don't infer these from my personal update-style preferences.",
        )
        check(
            "unrelated_project_context_retained",
            result["project_codename"] in project["output"]
            and "offline" in project["output"].casefold(),
        )
        check(
            "no_compaction_failures",
            not any(
                e["kind"] == "context_compaction_failed"
                for task in result["tasks"].values()
                for e in task["events"]
            ),
        )
    finally:
        persist()
        print(
            json.dumps(
                {
                    "checks": result["checks"],
                    "failures": result["failures"],
                    "usage": result["evidence"]["usage"],
                    "evidence": str(private),
                }
            ),
            flush=True,
        )
    if result["failures"] or not all(result["checks"].values()):
        raise RuntimeError("Live memory study has failed checks; inspect retained evidence.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--existing-network")
    parser.add_argument(
        "--edit-delete-only",
        action="store_true",
        help="Target UI-change chronology; skip the separately qualified replacement cycle",
    )
    parser.add_argument("--port", type=int, default=7094)
    args = parser.parse_args()
    if not args.execute_live:
        parser.error("Use --execute-live to authorize real saved GLM calls.")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from bots_coding_context_study import validate_plan
    from bots_computer_view import host

    from alysis_code.config import load_config, resolve_profile_api_key
    from alysis_code.profiles import get_profile

    cfg = load_config()
    profile = get_profile(cfg, "zai-coding-plan")
    validate_plan(profile)
    key = resolve_profile_api_key(cfg, "zai-coding-plan").key
    if not key:
        raise RuntimeError("Saved Coding Plan unavailable; no paid fallback.")
    host(
        args.app.resolve(),
        None,
        study_script=Path(__file__).with_name("bots_continuity_live.py"),
        bootstrap={
            "key": key,
            "base_url": profile.base_url,
            "model": "glm-5.3-flash",
            "trace_requests": True,
        },
        lazy_browser=True,
        exercise=partial(exercise, edit_delete_only=args.edit_delete_only),
        port=args.port,
        image="bots-durable-computer-20261019:dev",
        name_prefix="bots-memory-20261021-",
        existing_network=args.existing_network,
        memory="2g",
        tmpfs_size="1g",
    )
