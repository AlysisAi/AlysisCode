"""Opt-in real GLM/Bots Linux study with explicitly synthetic large reference stores.

Seed records are fixtures; model retrieval, note consolidation, compaction and
answers use the actual runtime. No Azure or paid provider fallback.
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


def exercise(client, bot, other, private, container, *, extended=False):
    result = {
        "kind": "Real GLM/local Linux with synthetic reference/archive fixtures; not Azure/native UI",
        "tasks": {},
        "checks": {},
        "container": container,
    }
    root = f"/v1/bots/{bot['id']}"

    def api(method, path, data=None):
        response = client.request(method, path, **({"json": data} if data is not None else {}))
        if response.status_code >= 400:
            raise RuntimeError(f"Study HTTP {response.status_code} at {path}")
        return response.json() if response.content else None

    def persist():
        result["evidence"] = api("GET", "/study/evidence")
        (private / "memory-capacity-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )

    def ask(label, prompt):
        task = api(
            "POST", f"/v1/chats/{chat}/tasks", {"message": prompt, "request_id": uuid4().hex}
        )
        end = time.monotonic() + 600
        while time.monotonic() < end:
            task = api("GET", f"/v1/tasks/{task['id']}")
            if task["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                break
            time.sleep(0.4)
        else:
            api("POST", f"/v1/tasks/{task['id']}/cancel")
            raise RuntimeError("Study watchdog cancelled stalled test, not product limit.")
        result["tasks"][label] = task
        result["checks"][label + "_completed"] = task["status"] == "completed"
        persist()
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
        return task

    chat = api("GET", "/study/evidence")["seed"]["chat"]["id"]
    try:
        preference = api(
            "POST",
            root + "/memories",
            {
                "title": "Communication preference",
                "content": "For all messages and documents, use Greek, even if I write my request in English, unless I explicitly ask for a different language. Prefer short paragraphs to bullet lists.",
            },
        )
        result["preference"] = preference
        for n in range(119):
            api(
                "POST",
                root + "/memories",
                {
                    "title": f"Fictional project reference {n}",
                    "content": f"The archived project case {n} uses receipt {uuid4().hex[:12]}. This is project-specific reference data, not a general communication preference.",
                },
            )
        notes = []
        for title, content in [
            (
                "Aster Notes initial brief",
                "Codename LANTERN-27. Audience was independent designers. Email integration excluded. Next action was a five-designer pilot. Budget unknown.",
            ),
            (
                "Aster Notes updated decisions",
                "Latest correction: audience is independent translators, replacing designers. Offline import is sufficient. Three-translator pilot is complete, but its result is unknown. Next action: prepare a feedback interview guide. No launch date or budget agreed.",
            ),
        ]:
            notes.append(api("POST", root + "/notes", {"title": title, "content": content}))
        for n in range(78):
            api(
                "POST",
                root + "/notes",
                {
                    "title": f"Unrelated project {n}",
                    "content": f"Synthetic project reference {n}; next step is review of docket {uuid4().hex[:12]}. No connection to Aster Notes.",
                },
            )
        result["checks"]["more_than_old_caps"] = (
            len(api("GET", root + "/memories")) == 120 and len(api("GET", root + "/notes")) == 80
        )
        ask(
            "unrelated_old_preference",
            "Why is the sky blue? Answer briefly without saving anything.",
        )
        ask(
            "implicit_old_preference",
            "Draft a short welcome message for a new volunteer workshop. Do not invent its location or date. Do not save anything.",
        )
        ask(
            "explicit_preference",
            "Rewrite that welcome using my currently saved communication preferences. Do not change memory or notes.",
        )
        ask(
            "consolidate",
            "For Aster Notes, consolidate the overlapping project notes into one short current note. Preserve the originals for inspection and distinguish current decisions from superseded ones and unknowns. Do not use unrelated projects or save personal preferences as project notes.",
        )
        allnotes = api("GET", root + "/notes")
        result["notes_after_consolidation"] = allnotes
        archived = [n for n in allnotes if n.get("consolidated_into")]
        result["checks"]["two_originals_archived"] = len(archived) == 2 and {
            n["id"] for n in archived
        } == {n["id"] for n in notes}
        result["checks"]["original_bodies_preserved"] = all(
            any(n["id"] == source["id"] and n["content"] == source["content"] for n in allnotes)
            for source in notes
        )
        current = [
            n
            for n in allnotes
            if not n.get("consolidated_into")
            and n["id"] not in {x["id"] for x in notes}
            and any(a.get("consolidated_into") == n["id"] for a in archived)
        ]
        if len(current) == 1:
            note = current[0]
            api(
                "PUT",
                root + f"/notes/{note['id']}",
                {
                    "title": note["title"],
                    "content": "Aster Notes, LANTERN-27. Current audience: independent translators; independent designers was the old audience. Email integration excluded. Offline import is sufficient. CORRECTION: the pilot has not happened; it is postponed, with no date or results known. Next action remains preparing a feedback interview guide. No launch date or budget agreed. Original notes remain historical sources.",
                    "expected_revision": note["revision"],
                },
            )
        result["pressure"] = api("POST", "/study/pressure")
        ask(
            "corrected_note_after_compaction",
            "Where do we stand on Aster Notes now? What has actually happened, what should we do next, and what is still unknown?",
        )
        ask(
            "historical_note",
            "What audience did Aster Notes originally target, and where can I inspect that earlier decision? Keep it clearly separate from the current audience. Do not change anything.",
        )
        api(
            "PUT",
            root + f"/memories/{preference['id']}",
            {
                "title": preference["title"],
                "content": "For all messages and documents, use French, even if I write in English, unless I explicitly request another language. Prefer short paragraphs.",
            },
        )
        ask(
            "corrected_preference",
            "Draft a brief thank-you message for volunteers after a workshop. Do not invent details or change saved information.",
        )
        api("DELETE", root + f"/memories/{preference['id']}")
        ask(
            "deleted_preference",
            "Is a general language preference currently saved for me? Check the current saved state. Do not recreate one from old messages.",
        )
        ask("post_delete_neutral", "Explain evaporation briefly without saving anything.")
        result["checks"]["deleted_stays_deleted"] = all(
            m["id"] != preference["id"] for m in api("GET", root + "/memories")
        )
        result["checks"]["unrelated_memories_retained"] = len(api("GET", root + "/memories")) == 119
        if extended:
            original = notes[0]
            restored = api(
                "POST",
                root + "/notes/restore",
                {"note_id": original["id"], "expected_revision": original["revision"]},
            )
            result["checks"]["restoration_preserves_content_time"] = (
                restored["updated_at"] == original["updated_at"]
            )
            ask(
                "restored_source_is_historical",
                "I restored an original project note so I can inspect it. What is the current audience and pilot status for Aster Notes? Restoring the old note was not a new project decision. Do not edit anything.",
            )
            ask(
                "new_preference_proposal",
                "Please remember that for future replies I prefer Japanese. For this message only, answer in English and tell me where I can approve the saved preference. Do not save this as a project note.",
            )
            pending = [m for m in api("GET", root + "/memories") if m["status"] == "pending"]
            result["checks"]["one_pending_preference"] = len(pending) == 1
            if len(pending) != 1:
                raise RuntimeError("Model did not produce one inspectable memory proposal")
            proposal = pending[0]
            ask("pending_not_default", "Briefly explain why ice floats, without saving anything.")
            api("PATCH", root + f"/memories/{proposal['id']}", {"status": "active"})
            ask(
                "approved_new_preference",
                "Briefly explain why leaves change colour, without saving anything.",
            )
            before = api("GET", "/study/evidence")
            before_memories = api("GET", root + "/memories")
            subprocess.run(
                ["docker", "exec", container, "kill", "-KILL", str(before["pid"])],
                check=True,
                capture_output=True,
            )
            for _ in range(120):
                try:
                    restarted = api("GET", "/study/evidence")
                    if restarted["pid"] != before["pid"]:
                        break
                except Exception:
                    pass
                time.sleep(0.25)
            else:
                raise RuntimeError("Study child did not restart")
            result["checks"]["memory_index_survives_restart"] = (
                api("GET", root + "/memories") == before_memories
            )
            ask(
                "restart_preference",
                "What is photosynthesis? Keep it brief without saving anything.",
            )
            api(
                "PUT",
                root + f"/memories/{proposal['id']}",
                {
                    "title": "応答方針",
                    "content": "今後は、私が別の言語を明示的に指定しない限り、すべての返信をスペイン語で、短い段落にしてください。",
                },
            )
            ask(
                "multilingual_correction",
                "Explain how a rainbow forms in a short paragraph, without saving anything.",
            )
            api("DELETE", root + f"/memories/{proposal['id']}")
            ask("second_deletion", "Explain how a compass works briefly, without saving anything.")
            result["checks"]["second_deletion_retains_other_memories"] = (
                len(api("GET", root + "/memories")) == 119
            )
        persist()
    finally:
        persist()
        print(
            json.dumps(
                {
                    "checks": result["checks"],
                    "usage": result["evidence"]["usage"],
                    "evidence": str(private),
                }
            ),
            flush=True,
        )
    if not all(result["checks"].values()):
        raise RuntimeError(
            "Study recorded failures; inspect retained evidence, including semantic answer quality."
        )


def summary_sources(client, bot, other, private, container):
    """Small real-model study; pressure history is explicitly synthetic."""
    result = {"kind": "Real GLM/local Linux; synthetic archive pressure", "tasks": {}, "checks": {}}
    root = f"/v1/bots/{bot['id']}"

    def api(method, path, data=None):
        reply = client.request(method, path, **({"json": data} if data is not None else {}))
        reply.raise_for_status()
        return reply.json() if reply.content else None

    chat = api("GET", "/study/evidence")["seed"]["chat"]["id"]

    def persist():
        result["evidence"] = api("GET", "/study/evidence")
        (private / "summary-sources-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )

    def ask(label, prompt):
        task = api(
            "POST", f"/v1/chats/{chat}/tasks", {"message": prompt, "request_id": uuid4().hex}
        )
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            task = api("GET", f"/v1/tasks/{task['id']}")
            if task["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                break
            time.sleep(0.3)
        else:
            api("POST", f"/v1/tasks/{task['id']}/cancel")
            raise RuntimeError("Study watchdog expired; not a product deadline")
        result["tasks"][label] = task
        result["checks"][label + "_completed"] = task["status"] == "completed"
        persist()
        print(
            json.dumps(
                {"task": label, "status": task["status"], "answer": task["output"]},
                ensure_ascii=False,
            ),
            flush=True,
        )
        assert task["status"] == "completed", task.get("error")

    try:
        docket = "東京-" + uuid4().hex[:10]
        ask(
            "original",
            f"For project {docket}, the pilot has finished and its report is pending. Keep this in the conversation only; do not save memory, notes or files. Briefly acknowledge.",
        )
        note = api(
            "POST",
            root + "/notes",
            {
                "title": f"{docket} current decision",
                "content": f"Correction for {docket}: the pilot has not happened and is postponed. No date or results are known. Next action is prepare the interview guide.",
            },
        )
        result["corrected_note"] = note
        api("POST", "/study/pressure")
        ask(
            "corrected",
            f"What is the current pilot status for {docket}, and what should happen next? Check the current project sources and do not change anything.",
        )
        state = api("GET", "/study/evidence")
        summaries = [s for s in state["summaries"] if s["bot_id"] == bot["id"] and s["through_id"]]
        result["first_summaries"] = summaries
        refs = [
            ref
            for s in summaries
            for item in json.loads(s["content"])["items"]
            for ref in item.get("source_note_references", [])
        ]
        result["checks"]["actual_summary_note_reference"] = {
            "note_id": note["id"],
            "revision": 1,
        } in refs
        updated = api(
            "PUT",
            root + f"/notes/{note['id']}",
            {
                "title": note["title"],
                "content": f"Current decision for {docket}: the pilot is cancelled, not postponed. No pilot took place. Next action is archive the interview guide; do not recruit participants.",
                "expected_revision": 1,
            },
        )
        result["updated_note"] = updated
        ask(
            "second_correction",
            f"What is the current plan for {docket}? Mention the status and next action, distinguishing the previous plan. Do not change anything.",
        )
        persist()
        latest = [
            s
            for s in result["evidence"]["summaries"]
            if s["bot_id"] == bot["id"] and s["through_id"]
        ]
        refs = [
            ref
            for s in latest
            for item in json.loads(s["content"])["items"]
            for ref in item.get("source_note_references", [])
        ]
        result["checks"]["new_summary_cites_current_revision"] = {
            "note_id": note["id"],
            "revision": 2,
        } in refs
        result["checks"]["old_note_revision_not_reused"] = not any(
            ref["revision"] == 1 and ref["note_id"] == note["id"] for ref in refs
        )
    finally:
        persist()
        print(
            json.dumps(
                {
                    "checks": result["checks"],
                    "usage": result["evidence"]["usage"],
                    "evidence": str(private),
                }
            ),
            flush=True,
        )
    if not all(result["checks"].values()):
        raise RuntimeError("Source study checks incomplete; retain and inspect actual summaries")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-live", action="store_true")
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--existing-network", required=True)
    parser.add_argument("--extended-lifecycle", action="store_true")
    parser.add_argument("--summary-sources", action="store_true")
    parser.add_argument("--model", choices=("glm-5.3-flash", "glm-5.3"), default="glm-5.3-flash")
    parser.add_argument("--port", type=int, default=7094)
    args = parser.parse_args()
    if not args.execute_live:
        parser.error("Explicit --execute-live required")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from bots_coding_context_study import validate_plan
    from bots_computer_view import host

    from alysis_code.config import load_config, resolve_profile_api_key
    from alysis_code.profiles import get_profile

    config = load_config()
    profile = get_profile(config, "zai-coding-plan")
    validate_plan(profile)
    key = resolve_profile_api_key(config, "zai-coding-plan").key
    if not key:
        raise RuntimeError("Saved Coding Plan unavailable; no fallback")
    host(
        args.app.resolve(),
        None,
        study_script=Path(__file__).with_name("bots_continuity_live.py"),
        bootstrap={
            "key": key,
            "base_url": profile.base_url,
            "model": args.model,
            "trace_requests": True,
            "max_calls": 30 if args.summary_sources else 120,
        },
        lazy_browser=True,
        exercise=summary_sources
        if args.summary_sources
        else partial(exercise, extended=args.extended_lifecycle),
        port=args.port,
        image="bots-durable-computer-20261019:dev",
        name_prefix="bots-memory-capacity-20261021-",
        existing_network=args.existing_network,
        memory="2g",
        tmpfs_size="1g",
    )
