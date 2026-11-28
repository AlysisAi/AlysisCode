"""Select one chart in an immutable HTML attachment; publish only its targeted revision."""

import json
import threading

import pytest
from test_bots_runtime import response, run_claimed

from alysis_code.bots.files import FileQuery
from alysis_code.bots.interactive_revision import InteractiveRevision, Replacement, replace_source
from alysis_code.bots.models import (
    INTERACTIVE_HTML_BYTES,
    BotInput,
    FileInput,
    InteractiveStateWrite,
    TaskInput,
)
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Conflict, Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import ToolCall

HTML = (
    '\ufeff<!doctype html>\r\n<main><h2 id="nord">Ventes</h2>\n<h2 id="sud">Ventes</h2></main>\r\n'
)
OLD = '<h2 id="sud">Ventes</h2>'
NEW = '<h2 id="sud">Ventes — Sud</h2>'
NOTE = "Ajoute « Sud » au titre sélectionné. Garde le premier graphique."
REGION = {"x": 0.51, "y": 0.2, "width": 0.4, "height": 0.32}


def selected(tmp_path, *, uploaded=False, source=HTML, state=None):
    store = Store(tmp_path / "selection.sqlite3")
    bot = store.save_bot("alice", BotInput(name="Bilan"))["id"]
    chat = store.create_chat("alice", bot, "Comparaison")["id"]
    file = (
        store.save_bytes(
            "alice", bot, "bilan.htm", source.encode("utf-8") if isinstance(source, str) else source
        )
        if uploaded
        else store.save_file("alice", bot, FileInput(name="bilan.htm", content=source))
    )
    saved = store.interactive_state(
        "alice",
        bot,
        file["id"],
        InteractiveStateWrite(
            revision=1,
            sha256=file["sha256"],
            expected_version=0,
            data=state or {"période": "été"},
        ),
    )
    target = {
        "file_id": file["id"],
        "revision": 1,
        "sha256": file["sha256"],
        "state_version": saved["version"],
        "region": REGION,
        "element": {"label": "Ventes", "path": "main > h2#sud", "region": REGION},
        "viewport": {"width": 1000, "height": 600},
    }
    task = store.enqueue(
        "alice",
        chat,
        TaskInput(
            message=NOTE,
            request_id="revise-selection",
            file_ids=[file["id"]],
            interactive_target=target,
        ),
    )
    return store, bot, file, task


@pytest.mark.parametrize("uploaded", [False, True])
def test_selected_table_uses_exact_revision_and_filtered_state_in_ordinary_owl_chat(
    tmp_path, uploaded
):
    store, bot, file, task = selected(tmp_path, uploaded=uploaded)
    offered = []

    class Model:
        model = "fixture"
        supports_tool_calling = True

        def chat(self, *, messages, tools, **kwargs):
            offered.append([t["function"]["name"] for t in tools])
            assert set(offered[-1]) == {"read_file", "save_file"}
            if len(offered) == 1:
                message = next(
                    m["content"] for m in messages if m["role"] == "user" and NOTE in m["content"]
                )
                data = json.loads(
                    message.split("Selected interactive result (reference data):\n")[1]
                )
                assert data["series_id"] == file["series_id"] and data["file_id"] == file["id"]
                assert data["sha256"] == file["sha256"] and data["revision"] == 1
                assert data["state_version"] == 1 and data["state"] == {"période": "été"}
                assert data["element"]["path"] == "main > h2#sud" and data["region"] == REGION
                assert "untrusted reference data" in messages[0]["content"]
                return response(
                    "", [ToolCall("r", "read_file", {"file_id": file["id"], "find": "sud"})]
                )
            receipt = json.loads(
                next(m["content"] for m in reversed(messages) if m["role"] == "tool")
            )
            if len(offered) == 2:
                assert receipt["found"] and OLD in receipt["content"]
                return response(
                    "",
                    [
                        ToolCall(
                            "s",
                            "save_file",
                            {
                                "name": "bilan.htm",
                                "previous_file_id": file["id"],
                                "replacements": [{"old": OLD, "new": NEW}],
                            },
                        )
                    ],
                )
            if len(offered) == 3:
                assert (
                    receipt["previous_sha256"] == file["sha256"]
                    and receipt["visual_review"] == "not_performed"
                )
                return response(
                    "", [ToolCall("v", "read_file", {"file_id": receipt["id"], "find": NEW})]
                )
            assert receipt["found"] and NEW in receipt["content"]
            return response("Le titre du graphique Sud a été modifié dans le fichier joint.")

    final = run_claimed(store, Model())
    assert final["status"] == "completed", final.get("error")
    assert len(offered) == 4
    (result,) = store.files_page("alice", bot, FileQuery(source_task_id=task["id"]))["items"]
    assert result["revision"] == 2 and result["series_id"] == file["series_id"]
    assert store.file_data("alice", bot, result["id"]) == HTML.replace(OLD, NEW).encode("utf-8")
    assert store.file_data("alice", bot, file["id"]) == HTML.encode("utf-8")
    with store.connect() as db:
        starts = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT data FROM events WHERE task_id=? AND kind='tool_started' ORDER BY seq",
                (task["id"],),
            )
        ]
    assert [(step["name"], step.get("file")) for step in starts] == [
        ("read_file", file["name"]),
        ("save_file", file["name"]),
        ("read_file", file["name"]),
    ]
    assert (
        store.conversation("alice", task["chat_id"])["messages"][-1]["content"] == final["output"]
    )


def test_large_upload_scoped_search_stale_state_and_recovery(tmp_path):
    source = HTML + "<!--" + "あ" * 760000 + "-->"
    store, bot, file, _ = selected(
        tmp_path, uploaded=True, source=source, state={"note": "x" * 6500}
    )
    token = EventCancellationToken(threading.Event())
    task = store.claim("alice")
    revision = InteractiveRevision(store, "alice", task)
    selection = revision.selection()
    assert selection["state_truncated"] and len(selection["state_excerpt"]) == 6000
    assert "state" not in selection
    other = store.save_bytes("alice", bot, "other.html", b"private")
    runtime = BotRuntime(store, "alice", lambda _bot: None)
    for unavailable in (other["id"], "missing"):
        assert runtime._step_started(
            task, "read_file", {"file_id": unavailable}, revision=revision
        ) == {"name": "read_file"}
    with pytest.raises(Conflict, match="selected HTML revision"):
        revision.call("read_file", {"file_id": other["id"]}, token)
    edit = {
        "name": file["name"],
        "previous_file_id": file["id"],
        "replacements": [{"old": OLD, "new": NEW}],
    }
    with pytest.raises(Conflict, match="Read the selected source"):
        revision.call("save_file", edit, token)
    revision.call("read_file", {"file_id": file["id"], "find": "sud"}, token)
    with pytest.raises(Conflict, match="exact selected"):
        revision.call("save_file", {**edit, "name": "other.html"}, token)
    saved = revision.call("save_file", edit, token)
    assert (
        b"".join(store.file_chunks("alice", bot, saved["id"])) == source.replace(OLD, NEW).encode()
    )
    recovered = InteractiveRevision(Store(store.path), "alice", task)
    assert recovered.selection()["already_saved"]["id"] == saved["id"]
    assert recovered.call("save_file", edit, token)["already_saved"]
    assert store.files_page("alice", bot, FileQuery(source_task_id=task["id"]))["total_count"] == 1


def test_state_change_after_read_prevents_file_publication(tmp_path):
    store, bot, file, _ = selected(tmp_path)
    token = EventCancellationToken(threading.Event())
    task = store.claim("alice")
    revision = InteractiveRevision(store, "alice", task)
    revision.call("read_file", {"file_id": file["id"]}, token)
    store.interactive_state(
        "alice",
        bot,
        file["id"],
        InteractiveStateWrite(
            revision=1, sha256=file["sha256"], expected_version=1, data={"période": "hiver"}
        ),
    )
    with pytest.raises(Conflict, match="state changed"):
        revision.call(
            "save_file",
            {
                "previous_file_id": file["id"],
                "name": file["name"],
                "replacements": [{"old": OLD, "new": NEW}],
            },
            token,
        )
    assert store.files_page("alice", bot, FileQuery(source_task_id=task["id"]))["items"] == []


@pytest.mark.parametrize(
    "source,error", [(b"\xff", "UTF-8"), (b"<!doctype html>\x00", "binary data")]
)
def test_unreadable_uploaded_html_has_no_partial_output(tmp_path, source, error):
    store, bot, file, _ = selected(tmp_path, uploaded=True, source=source)
    task = store.claim("alice")
    revision = InteractiveRevision(store, "alice", task)
    with pytest.raises(Conflict, match=error):
        revision.call(
            "read_file", {"file_id": file["id"]}, EventCancellationToken(threading.Event())
        )
    assert store.files_page("alice", bot, FileQuery(source_task_id=task["id"]))["total_count"] == 0


def test_edit_that_crosses_preview_byte_limit_leaves_original_unchanged(tmp_path):
    original = OLD.encode().ljust(INTERACTIVE_HTML_BYTES, b" ")
    store, bot, file, _ = selected(tmp_path, uploaded=True, source=original)
    task = store.claim("alice")
    revision = InteractiveRevision(store, "alice", task)
    token = EventCancellationToken(threading.Event())
    revision.call("read_file", {"file_id": file["id"], "length": len(OLD)}, token)
    with pytest.raises(Conflict, match="8 MiB"):
        revision.call(
            "save_file",
            {
                "previous_file_id": file["id"],
                "name": file["name"],
                "replacements": [{"old": OLD, "new": NEW}],
            },
            token,
        )
    assert b"".join(store.file_chunks("alice", bot, file["id"])) == original
    assert store.files_page("alice", bot, FileQuery(source_task_id=task["id"]))["total_count"] == 0


@pytest.mark.parametrize(
    "changes", [[("Ventes", "Bilan")], [(OLD, NEW), ("sud", "sud2")], [("missing", "present")]]
)
def test_ambiguous_missing_and_overlapping_fragments_fail(changes):
    with pytest.raises(Conflict):
        replace_source(HTML, [Replacement(old=old, new=new) for old, new in changes])
