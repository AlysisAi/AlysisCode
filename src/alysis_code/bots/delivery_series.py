"""Immutable file identities for conversational follow-ups in the same Owl chat."""

from pathlib import PurePath

from .store import Conflict, encode


def previous_delivery(store, owner, task):
    """The last completed delivery by this Owl in this exact chat, excluding uploads."""
    with store.connect() as db:
        prior = db.execute(
            """SELECT prior.id FROM tasks active
               JOIN bots b ON b.id=active.bot_id AND b.owner=?
               JOIN tasks prior ON prior.chat_id=active.chat_id AND prior.bot_id=active.bot_id
               WHERE active.id=? AND active.bot_id=? AND prior.status='completed'
                 AND prior.conversation_visible=1
                 AND (prior.created_at<active.created_at OR (prior.created_at=active.created_at AND prior.id<active.id))
                 AND EXISTS (SELECT 1 FROM files f WHERE f.source_task_id=prior.id AND f.bot_id=prior.bot_id
                             AND NOT EXISTS (SELECT 1 FROM owl_withdrawn_results w WHERE w.file_id=f.id))
               ORDER BY prior.created_at DESC,prior.id DESC LIMIT 1""",
            (owner, task["id"], task["bot_id"]),
        ).fetchone()
        if not prior:
            return []
        files = db.execute(
            """SELECT f.id,f.name,f.series_id,f.revision,q.file_id AS data_result
               FROM files f LEFT JOIN owl_query_files q ON q.file_id=f.id
               WHERE f.source_task_id=? AND f.bot_id=?
               AND NOT EXISTS (SELECT 1 FROM owl_withdrawn_results w WHERE w.file_id=f.id)
               AND NOT EXISTS (
                 SELECT 1 FROM files later WHERE later.source_task_id=f.source_task_id
                 AND later.series_id=f.series_id AND later.revision>f.revision
                 AND NOT EXISTS (SELECT 1 FROM owl_withdrawn_results w WHERE w.file_id=later.id))
               ORDER BY f.created_at DESC,f.id DESC LIMIT 10""",
            (prior[0], task["bot_id"]),
        ).fetchall()
    return [
        {
            "name": f["name"],
            "previous_file_id": f["id"],
            "series_id": f["series_id"],
            "revision": f["revision"],
            "kind": "query" if f["data_result"] else file_kind(f["name"]),
        }
        for f in files
    ]


def file_kind(name):
    extension = PurePath(name).suffix.lower()
    return "html" if extension == ".htm" else extension.lstrip(".") or "file"


def check_delivery_target(store, owner, task, name, args):
    if args.previous_file_id:
        if args.new_series:
            raise Conflict("Choose an existing previous_file_id or new_series, not both.")
        return
    if args.new_series:
        return
    kind = "query" if name == "present_table" else file_kind(args.name)
    candidates = [f for f in previous_delivery(store, owner, task) if f["kind"] == kind]
    if candidates:
        raise Conflict(
            "This chat previously delivered the following results of this kind. For a correction, "
            "including a request to preserve the original, use its exact name and previous_file_id; "
            "version history preserves the original. For a genuinely distinct deliverable, retry "
            "with new_series=true. " + encode(candidates)
        )
