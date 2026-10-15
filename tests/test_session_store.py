from __future__ import annotations

import errno
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.execution_deadline import ExecutionDeadline
from alysis_code.session_store import (
    SessionStore,
    SessionStoreUnavailableError,
    list_sessions,
    make_session_id,
    read_session_events,
)
from alysis_code.web_research import (
    build_web_research_artifact_from_events,
    canonicalize_web_url_input,
    extract_public_web_urls,
    normalize_web_url,
)


def _write_web_artifact_ahead_of_log(
    *,
    sessions_dir: Path,
    session_id: str,
    logged_events: list[dict[str, object]],
    artifact_only_events: list[dict[str, object]],
) -> None:
    (sessions_dir / f"{session_id}.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=True) + "\n" for event in logged_events),
        encoding="utf-8",
    )
    artifact_root = sessions_dir / session_id
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_payload = build_web_research_artifact_from_events(logged_events + artifact_only_events)
    (artifact_root / "web_research_sources.json").write_text(
        json.dumps(artifact_payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def test_session_store_writes_jsonl(tmp_path: Path) -> None:
    sid = make_session_id()
    store = SessionStore(
        enabled=True,
        sessions_dir=tmp_path,
        session_id=sid,
        cwd=".",
        repo_root=".",
    )
    store.append("user_message", {"content": "hi"})
    store.append("final", {"content": "bye"})
    store.close()

    p = tmp_path / f"{sid}.jsonl"
    assert p.exists()
    events = list(read_session_events(p))
    assert [e["type"] for e in events] == ["user_message", "final"]
    assert all("cwd" in event for event in events)
    assert all("repo_root" in event for event in events)


def test_session_store_assigns_monotonic_event_ids(tmp_path: Path) -> None:
    store = SessionStore(
        enabled=True,
        sessions_dir=tmp_path,
        session_id="event-id-test",
        cwd=".",
        repo_root=".",
    )
    store.append("user_message", {"content": "hi"})
    store.append("final", {"content": "bye"})
    store.close()

    events = list(read_session_events(tmp_path / "event-id-test.jsonl"))

    assert [event["event_id"] for event in events] == ["event-id-test:1", "event-id-test:2"]


def test_session_store_events_since_returns_only_new_events_and_advances_cursor(
    tmp_path: Path,
) -> None:
    store = SessionStore(
        enabled=False,
        sessions_dir=tmp_path,
        session_id="incremental-events",
        cwd=".",
        repo_root=".",
    )
    store.append("user_message", {"content": "one"})
    first, cursor = store.events_since(0)
    store.append("assistant_message", {"content": "two"})
    second, cursor = store.events_since(cursor)

    assert [event["type"] for event in first] == ["user_message"]
    assert [event["type"] for event in second] == ["assistant_message"]
    assert cursor == 2
    assert store.events_since(99) == ([], 2)


def test_session_store_events_since_concurrent_appends_are_conserved(
    tmp_path: Path,
) -> None:
    store = SessionStore(
        enabled=False,
        sessions_dir=tmp_path,
        session_id="concurrent-events",
        cwd=".",
        repo_root=".",
    )
    expected_count = 500
    writer_done = threading.Event()

    def append_events() -> None:
        for index in range(expected_count):
            store.append("event", {"index": index})
        writer_done.set()

    writer = threading.Thread(target=append_events)
    writer.start()
    cursor = 0
    observed_ids: list[str] = []
    while not writer_done.is_set() or cursor < expected_count:
        events, cursor = store.events_since(cursor)
        observed_ids.extend(str(event["event_id"]) for event in events)
        if not events:
            writer_done.wait(0.001)
    writer.join(timeout=2.0)

    assert not writer.is_alive()
    assert observed_ids == [f"concurrent-events:{index}" for index in range(1, 501)]


def test_session_store_refuses_to_exist_when_sessions_dir_is_read_only(
    tmp_path: Path, monkeypatch
) -> None:
    """A durable log was requested (``enabled=True``) and cannot be opened:
    the store refuses at construction instead of silently degrading into
    what would look like a deliberate ``--no-log`` session."""

    sid = make_session_id()
    sessions_dir = tmp_path / "sessions"
    real_mkdir = Path.mkdir

    def fail_sessions_dir_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == sessions_dir:
            raise OSError(errno.EROFS, "Read-only file system", os.fspath(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_sessions_dir_mkdir)

    with pytest.raises(SessionStoreUnavailableError) as excinfo:
        SessionStore(
            enabled=True,
            sessions_dir=sessions_dir,
            session_id=sid,
            cwd=".",
            repo_root=".",
        )
    assert excinfo.value.outcome == "unavailable"
    assert "Read-only file system" in str(excinfo.value)
    assert not (sessions_dir / f"{sid}.jsonl").exists()

    # Deliberate no-log operation is unchanged: the same directory problem is
    # irrelevant because no durable log was requested.
    store = SessionStore(
        enabled=False,
        sessions_dir=sessions_dir,
        session_id=sid,
        cwd=".",
        repo_root=".",
    )
    store.append("user_message", {"content": "hi"})
    store.close()
    assert store.enabled is False
    assert not (sessions_dir / f"{sid}.jsonl").exists()


def test_no_log_keeps_session_log_disabled_but_allows_explicit_diagnostics(
    tmp_path: Path,
) -> None:
    diagnostic_log = tmp_path / "diagnostics" / "events.jsonl"
    deadline = ExecutionDeadline.from_absolute(
        started_at_monotonic=10.0,
        deadline_monotonic=10.0,
        configured_duration_seconds=1.0,
        clock=lambda: 10.0,
    )
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=True,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        execution_deadline=deadline,
        crash_diagnostic_log_path=diagnostic_log,
    )

    try:
        # Budget stop: a normal outcome, so a clean exit.
        assert session.run_turn("Do the task.") == 0
        session_log_path = session.store.path
    finally:
        session.close()

    assert session_log_path.exists() is False
    events = [json.loads(line) for line in diagnostic_log.read_text(encoding="utf-8").splitlines()]
    event_types = [event["event_type"] for event in events]
    assert "run_started" in event_types
    assert "turn_started" in event_types
    assert "deadline_exhausted" in event_types
    assert "run_finished" in event_types


def test_normalize_web_url_preserves_valid_parenthesized_path() -> None:
    assert (
        normalize_web_url("https://docs.example.com/Function_(mathematics)")
        == "https://docs.example.com/Function_(mathematics)"
    )


def test_normalize_web_url_preserves_bracket_query_params() -> None:
    assert (
        normalize_web_url("https://docs.example.com/path?foo[bar]=1")
        == "https://docs.example.com/path?foo[bar]=1"
    )
    assert (
        normalize_web_url("https://docs.example.com/path?arr[]=1")
        == "https://docs.example.com/path?arr[]=1"
    )


def test_canonicalize_web_url_input_preserves_legal_trailing_exclamation() -> None:
    assert (
        canonicalize_web_url_input("https://docs.example.com/Yahoo!")
        == "https://docs.example.com/Yahoo!"
    )


def test_extract_public_web_urls_preserves_valid_parenthesized_path() -> None:
    extracted = extract_public_web_urls("Read https://docs.example.com/Function_(mathematics) now.")

    assert extracted == [
        {
            "url": "https://docs.example.com/Function_(mathematics)",
            "normalized_url": "https://docs.example.com/Function_(mathematics)",
            "domain": "docs.example.com",
        }
    ]


def test_extract_public_web_urls_preserves_bracket_query_params() -> None:
    cases = [
        (
            "Please read https://docs.example.com/path?foo[bar]=1 before changes.",
            "https://docs.example.com/path?foo[bar]=1",
        ),
        (
            "See https://docs.example.com/path?arr[]=1 now.",
            "https://docs.example.com/path?arr[]=1",
        ),
    ]

    for text, expected_url in cases:
        assert extract_public_web_urls(text) == [
            {
                "url": expected_url,
                "normalized_url": expected_url,
                "domain": "docs.example.com",
            }
        ]


def test_extract_public_web_urls_preserves_exclamation_and_apostrophes() -> None:
    cases = [
        (
            "Please read https://docs.example.com/Yahoo! before changes.",
            "https://docs.example.com/Yahoo!",
        ),
        (
            "Read https://docs.example.com/O'Connor now.",
            "https://docs.example.com/O'Connor",
        ),
        (
            "Read https://docs.example.com/path?name=O'Connor now.",
            "https://docs.example.com/path?name=O'Connor",
        ),
    ]

    for text, expected_url in cases:
        assert extract_public_web_urls(text) == [
            {
                "url": expected_url,
                "normalized_url": expected_url,
                "domain": "docs.example.com",
            }
        ]


def test_extract_public_web_urls_parses_markdown_link_targets() -> None:
    cases = [
        (
            "Please read ([docs](https://docs.example.com/spec)) before changes.",
            "https://docs.example.com/spec",
        ),
        (
            "[docs](https://docs.example.com/spec)",
            "https://docs.example.com/spec",
        ),
        (
            "[docs](https://docs.example.com/spec).",
            "https://docs.example.com/spec",
        ),
    ]

    for text, expected_url in cases:
        assert extract_public_web_urls(text) == [
            {
                "url": expected_url,
                "normalized_url": expected_url,
                "domain": "docs.example.com",
            }
        ]


def test_extract_public_web_urls_preserves_structured_markdown_target_punctuation() -> None:
    cases = [
        ("[docs](https://docs.example.com/path.)", "https://docs.example.com/path."),
        ("[docs](https://docs.example.com/path:)", "https://docs.example.com/path:"),
        ("[docs](https://docs.example.com/path;)", "https://docs.example.com/path;"),
        ("[docs](https://docs.example.com/path,)", "https://docs.example.com/path,"),
        ("<https://docs.example.com/path.>", "https://docs.example.com/path."),
    ]

    for text, expected_url in cases:
        assert extract_public_web_urls(text) == [
            {
                "url": expected_url,
                "normalized_url": expected_url,
                "domain": "docs.example.com",
            }
        ]


def test_extract_public_web_urls_preserves_markdown_link_title_parenthesized_targets() -> None:
    cases = [
        (
            '[Function](https://docs.example.com/Function_(mathematics) "Function docs")',
            "https://docs.example.com/Function_(mathematics)",
        ),
        (
            '[docs](https://docs.example.com/path_(foo) "Title")',
            "https://docs.example.com/path_(foo)",
        ),
        (
            "[Function](https://docs.example.com/Function_(mathematics))",
            "https://docs.example.com/Function_(mathematics)",
        ),
        (
            '[Function](<https://docs.example.com/Function_(mathematics)> "Function docs")',
            "https://docs.example.com/Function_(mathematics)",
        ),
    ]

    for text, expected_url in cases:
        assert extract_public_web_urls(text) == [
            {
                "url": expected_url,
                "normalized_url": expected_url,
                "domain": "docs.example.com",
            }
        ]


def test_extract_public_web_urls_strips_sentence_punctuation_from_generic_prose() -> None:
    assert extract_public_web_urls(
        "Please read https://docs.example.com/spec. before changes."
    ) == [
        {
            "url": "https://docs.example.com/spec",
            "normalized_url": "https://docs.example.com/spec",
            "domain": "docs.example.com",
        }
    ]


def test_extract_public_web_urls_preserves_order_across_plain_and_markdown_links() -> None:
    extracted = extract_public_web_urls(
        "Read https://docs.example.com/first then [second](https://docs.example.com/second)."
    )

    assert [entry["url"] for entry in extracted] == [
        "https://docs.example.com/first",
        "https://docs.example.com/second",
    ]


def test_extract_public_web_urls_strips_common_markdown_wrappers() -> None:
    cases = [
        "Please read `https://docs.example.com/spec`.",
        "See *https://docs.example.com/spec* now.",
        "See **https://docs.example.com/spec** now.",
        "See _https://docs.example.com/spec_ now.",
        "See __https://docs.example.com/spec__ now.",
    ]

    for text in cases:
        assert extract_public_web_urls(text) == [
            {
                "url": "https://docs.example.com/spec",
                "normalized_url": "https://docs.example.com/spec",
                "domain": "docs.example.com",
            }
        ]


def test_canonicalize_web_url_input_preserves_legal_trailing_parenthesis() -> None:
    assert (
        canonicalize_web_url_input("https://docs.example.com/path?x=a)")
        == "https://docs.example.com/path?x=a)"
    )


def test_extract_public_web_urls_preserves_legal_trailing_parenthesis_in_markdown_link() -> None:
    extracted = extract_public_web_urls(
        "Please read [docs](https://docs.example.com/path?x=a)) before changes."
    )

    assert extracted == [
        {
            "url": "https://docs.example.com/path?x=a)",
            "normalized_url": "https://docs.example.com/path?x=a)",
            "domain": "docs.example.com",
        }
    ]


def test_extract_public_web_urls_preserves_legal_trailing_parenthesis_in_plain_prose() -> None:
    extracted = extract_public_web_urls(
        "Please read https://docs.example.com/path?x=a) before changes."
    )

    assert extracted == [
        {
            "url": "https://docs.example.com/path?x=a)",
            "normalized_url": "https://docs.example.com/path?x=a)",
            "domain": "docs.example.com",
        }
    ]


def test_session_store_writes_workspace_metadata_additively(tmp_path: Path) -> None:
    sid = make_session_id()
    store = SessionStore(
        enabled=True,
        sessions_dir=tmp_path,
        session_id=sid,
        cwd="/repo/pkg",
        repo_root="/repo/pkg",
        workspace_root="/repo",
        focus_dir="/repo/pkg",
        git_root="/repo",
        workspace_kind="git_repo",
        binding_source="cwd",
        binding_requested_path="/repo/pkg",
        binding_risk_level="healthy",
        binding_created_path=False,
    )
    store.append("session_start", {"mode": "review"})
    store.close()

    [event] = list(read_session_events(tmp_path / f"{sid}.jsonl"))
    assert event["cwd"] == "/repo/pkg"
    assert event["repo_root"] == "/repo/pkg"
    assert event["workspace_root"] == "/repo"
    assert event["focus_dir"] == "/repo/pkg"
    assert event["git_root"] == "/repo"
    assert event["workspace_kind"] == "git_repo"
    assert event["binding_source"] == "cwd"
    assert event["binding_requested_path"] == "/repo/pkg"
    assert event["binding_risk_level"] == "healthy"
    assert event["binding_created_path"] is False


def test_list_sessions(tmp_path: Path) -> None:
    sid = make_session_id()
    (tmp_path / f"{sid}.jsonl").write_text("{}", encoding="utf-8")
    infos = list_sessions(tmp_path)
    assert any(i.session_id == sid for i in infos)


def test_read_session_last_event_ts_returns_latest(tmp_path: Path) -> None:
    from alysis_code.session_store import read_session_last_event_ts

    p = tmp_path / "s.jsonl"
    events = [
        {"type": "session_start", "ts": "2026-01-01T10:00:00+00:00", "payload": {}},
        {"type": "user_message", "ts": "2026-01-01T10:05:00+00:00", "payload": {"content": "hi"}},
        {"type": "final", "ts": "2026-01-01T10:09:00+00:00", "payload": {"content": "bye"}},
    ]
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    assert read_session_last_event_ts(p) == "2026-01-01T10:09:00+00:00"


def test_read_session_last_event_ts_handles_empty_corrupt_missing(tmp_path: Path) -> None:
    from alysis_code.session_store import read_session_last_event_ts

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert read_session_last_event_ts(empty) is None

    corrupt = tmp_path / "corrupt.jsonl"
    corrupt.write_text("not json\n{bad\n", encoding="utf-8")
    assert read_session_last_event_ts(corrupt) is None

    assert read_session_last_event_ts(tmp_path / "missing.jsonl") is None


def test_read_session_last_event_ts_skips_trailing_partial_line(tmp_path: Path) -> None:
    from alysis_code.session_store import read_session_last_event_ts

    p = tmp_path / "s.jsonl"
    good = json.dumps({"type": "final", "ts": "2026-01-01T10:09:00+00:00", "payload": {}})
    # A crash can leave a half-written final line; the reader should walk back to
    # the last fully parseable event.
    p.write_text(good + "\n" + '{"type": "tool_call", "ts": "2026-', encoding="utf-8")
    assert read_session_last_event_ts(p) == "2026-01-01T10:09:00+00:00"


def test_read_session_first_event_workspace_toplevel_and_payload(tmp_path: Path) -> None:
    from alysis_code.session_store import read_session_first_event_workspace

    top = tmp_path / "top.jsonl"
    top.write_text(
        json.dumps(
            {
                "type": "session_start",
                "ts": "2026-01-01T10:00:00+00:00",
                "workspace_root": "/repo",
                "git_root": "/repo",
                "payload": {"mode": "auto"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert read_session_first_event_workspace(top) == ("/repo", "/repo")

    payload_only = tmp_path / "payload.jsonl"
    payload_only.write_text(
        json.dumps(
            {
                "type": "session_start",
                "ts": "2026-01-01T10:00:00+00:00",
                "payload": {"workspace_root": "/w", "git_root": "/g"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert read_session_first_event_workspace(payload_only) == ("/w", "/g")

    none = tmp_path / "none.jsonl"
    none.write_text(json.dumps({"type": "x", "payload": {}}) + "\n", encoding="utf-8")
    assert read_session_first_event_workspace(none) == (None, None)


def test_session_belongs_to_workspace_predicate(tmp_path: Path) -> None:
    from alysis_code.session_store import (
        SessionInfo,
        canonical_workspace_path,
        session_belongs_to_workspace,
    )

    ws = tmp_path / "repo"
    other = tmp_path / "other"
    ws.mkdir()
    other.mkdir()
    current = canonical_workspace_path(str(ws))

    # Primary workspace_root match (trailing separator proves normalization).
    match = SessionInfo(
        session_id="m", path=tmp_path / "m.jsonl", mtime=1.0, workspace_root=str(ws) + os.sep
    )
    assert session_belongs_to_workspace(match, current) is True

    # Different workspace is excluded.
    diff = SessionInfo(
        session_id="d", path=tmp_path / "d.jsonl", mtime=1.0, workspace_root=str(other)
    )
    assert session_belongs_to_workspace(diff, current) is False

    # Legacy log (no workspace_root) matches on git_root.
    legacy = SessionInfo(session_id="l", path=tmp_path / "l.jsonl", mtime=1.0, git_root=str(ws))
    assert session_belongs_to_workspace(legacy, current, canonical_workspace_path(str(ws))) is True
    # Legacy log with no current git_root -> excluded.
    assert session_belongs_to_workspace(legacy, current, None) is False

    # No identity at all -> excluded.
    stray = SessionInfo(session_id="s", path=tmp_path / "s.jsonl", mtime=1.0)
    assert session_belongs_to_workspace(stray, current) is False


def test_list_sessions_enriches_workspace_and_last_event_ts(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    events = [
        {
            "type": "session_start",
            "ts": "2026-01-01T10:00:00+00:00",
            "workspace_root": "/repo",
            "git_root": "/repo",
            "payload": {},
        },
        {"type": "final", "ts": "2026-01-01T10:09:00+00:00", "payload": {"content": "done"}},
    ]
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    [info] = list_sessions(tmp_path)
    assert info.workspace_root == "/repo"
    assert info.git_root == "/repo"
    assert info.last_event_ts == "2026-01-01T10:09:00+00:00"


def test_session_store_reopen_preserves_user_provided_web_url_classification(
    tmp_path: Path,
) -> None:
    sid = "resume-user-url"
    store = SessionStore(
        enabled=True,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id=sid,
        cwd=".",
        repo_root=".",
    )
    store.append(
        "user_message",
        {"content": "Please inspect https://docs.example.com/spec before changing anything."},
    )
    store.close()

    reopened = SessionStore(
        enabled=False,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id=sid,
        cwd=".",
        repo_root=".",
    )

    assert reopened.classify_web_fetch_url("https://docs.example.com/spec") == "user_provided"


def test_session_store_fetchable_urls_lists_search_sources_then_user_urls(
    tmp_path: Path,
) -> None:
    store = SessionStore(
        enabled=True,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id="fetchable-urls",
        cwd=".",
        repo_root=".",
    )
    store.append(
        "user_message",
        {"content": "Check https://user.example.com/page later."},
    )
    store.append(
        "tool_result",
        {
            "name": "web_search",
            "step": 1,
            "result": {
                "query": "world cup news",
                "backend": "openrouter_web",
                "sources": [
                    {"title": "FIFA", "url": "https://www.fifa.com/worldcup/news"},
                    {"title": "UEFA", "url": "https://www.uefa.com/news"},
                ],
            },
        },
    )

    fetchable = store.fetchable_web_fetch_urls()

    # web_search sources first (in result order), then the user-provided URL.
    assert fetchable == [
        "https://www.fifa.com/worldcup/news",
        "https://www.uefa.com/news",
        "https://user.example.com/page",
    ]
    # Every listed URL is genuinely authorized for web_fetch.
    for url in fetchable:
        assert store.classify_web_fetch_url(url) is not None
    # The bound is honored.
    assert store.fetchable_web_fetch_urls(limit=1) == ["https://www.fifa.com/worldcup/news"]


def test_session_store_trusted_domain_allowlist_classification(tmp_path: Path) -> None:
    store = SessionStore(
        enabled=False,
        sessions_dir=tmp_path,
        session_id="trusted-domains",
        cwd=".",
        repo_root=".",
    )
    # No allowlist configured: unknown URLs stay unfetchable.
    assert store.classify_web_fetch_url("https://registry.npmjs.org/vite/latest") is None

    store.configure_web_fetch_trusted_domains(
        [
            "https://Registry.NPMJS.org/some/path",  # URL form, mixed case
            "*.example-registry.dev",  # wildcard form
            "  ",  # dropped
            "user@host.example",  # credentials: dropped
        ]
    )

    assert (
        store.classify_web_fetch_url("https://registry.npmjs.org/vite/latest")
        == "trusted_domain_allowlist"
    )
    # Subdomains of a configured host match.
    assert (
        store.classify_web_fetch_url("https://mirror.registry.npmjs.org/vite")
        == "trusted_domain_allowlist"
    )
    assert (
        store.classify_web_fetch_url("https://sub.example-registry.dev/pkg")
        == "trusted_domain_allowlist"
    )
    # Suffix spoofing does not match: the allowlisted host is not a dot-suffix here.
    assert store.classify_web_fetch_url("https://registry.npmjs.org.evil.com/x") is None
    assert store.classify_web_fetch_url("https://evilregistry.npmjs.org/x") is None
    # Session provenance evidence still wins over the static allowlist label.
    store.append(
        "user_message",
        {"content": "Fetch https://registry.npmjs.org/vite/latest for me."},
    )
    assert store.classify_web_fetch_url("https://registry.npmjs.org/vite/latest") == "user_provided"
    # Reconfiguring replaces the previous allowlist.
    store.configure_web_fetch_trusted_domains([])
    assert store.classify_web_fetch_url("https://sub.example-registry.dev/pkg") is None
    # Allowlisted domains are a host set, not URLs: fetchable_web_fetch_urls only
    # lists concrete session evidence (here, the user-provided npm URL).
    assert store.fetchable_web_fetch_urls(limit=1) == ["https://registry.npmjs.org/vite/latest"]


def test_session_store_reopen_preserves_search_returned_web_url_classification(
    tmp_path: Path,
) -> None:
    sid = "resume-search-url"
    store = SessionStore(
        enabled=True,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id=sid,
        cwd=".",
        repo_root=".",
    )
    store.append(
        "tool_result",
        {
            "name": "web_search",
            "step": 1,
            "result": {
                "query": "docs example",
                "backend": "openai_responses",
                "sources": [
                    {
                        "title": "Guide",
                        "url": "https://docs.example.com/guide",
                        "snippet": "Official guide",
                    }
                ],
            },
        },
    )
    store.close()

    reopened = SessionStore(
        enabled=False,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id=sid,
        cwd=".",
        repo_root=".",
    )

    assert (
        reopened.classify_web_fetch_url("https://docs.example.com/guide")
        == "returned_by_web_search"
    )


def test_session_store_reopen_keeps_cumulative_web_artifact_after_new_events(
    tmp_path: Path,
) -> None:
    sid = "resume-cumulative-web"
    store = SessionStore(
        enabled=True,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id=sid,
        cwd=".",
        repo_root=".",
    )
    store.append(
        "user_message",
        {"content": "Please inspect https://docs.example.com/spec before changing anything."},
    )
    store.append(
        "tool_result",
        {
            "name": "web_search",
            "step": 1,
            "result": {
                "query": "docs example",
                "backend": "openai_responses",
                "sources": [
                    {
                        "title": "Guide",
                        "url": "https://docs.example.com/guide",
                        "snippet": "Official guide",
                    }
                ],
            },
        },
    )
    store.close()

    reopened = SessionStore(
        enabled=False,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id=sid,
        cwd=".",
        repo_root=".",
    )
    reopened.append(
        "tool_call",
        {"name": "web_fetch", "step": 2, "arguments": {"url": "https://docs.example.com/guide"}},
    )
    reopened.append(
        "tool_result",
        {
            "name": "web_fetch",
            "step": 2,
            "result": {
                "url": "https://docs.example.com/guide",
                "final_url": "https://docs.example.com/final",
                "status_code": 200,
                "content_type": "text/html",
                "title": "Final",
                "backend": "httpx",
            },
        },
    )
    reopened.close()

    payload = json.loads((tmp_path / sid / "web_research_sources.json").read_text(encoding="utf-8"))

    assert payload["deduped_normalized_user_urls"] == ["https://docs.example.com/spec"]
    assert payload["deduped_normalized_search_source_urls"] == ["https://docs.example.com/guide"]
    assert payload["deduped_normalized_fetch_urls"] == ["https://docs.example.com/guide"]
    assert payload["deduped_normalized_final_fetch_urls"] == ["https://docs.example.com/final"]
    assert payload["fetches"][0]["final_url"] == "https://docs.example.com/final"


def test_session_store_web_artifact_dedupes_fetch_url_spelling_variants(
    tmp_path: Path,
) -> None:
    sid = "fetch-url-canonical-dedupe"
    store = SessionStore(
        enabled=False,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id=sid,
        cwd=".",
        repo_root=".",
    )
    store.append(
        "user_message",
        {"content": "Please inspect https://docs.example.com/spec. before changing anything."},
    )
    store.append(
        "tool_call",
        {"name": "web_fetch", "step": 1, "arguments": {"url": "https://docs.example.com/spec."}},
    )
    store.append(
        "tool_result",
        {
            "name": "web_fetch",
            "step": 1,
            "result": {
                "url": "https://docs.example.com/spec",
                "final_url": "https://docs.example.com/spec",
                "status_code": 200,
                "content_type": "text/html",
                "title": "Spec",
                "backend": "httpx",
                "raw_input_url": "https://docs.example.com/spec.",
            },
        },
    )
    store.append(
        "tool_call",
        {"name": "web_fetch", "step": 2, "arguments": {"url": "https://docs.example.com/spec"}},
    )
    store.append(
        "tool_result",
        {
            "name": "web_fetch",
            "step": 2,
            "result": {
                "url": "https://docs.example.com/spec",
                "final_url": "https://docs.example.com/spec",
                "status_code": 200,
                "content_type": "text/html",
                "title": "Spec",
                "backend": "httpx",
            },
        },
    )

    payload = store.web_research_artifact_payload()
    metrics = store.web_research_metrics_payload()

    assert payload["deduped_normalized_user_urls"] == ["https://docs.example.com/spec"]
    assert payload["deduped_normalized_fetch_urls"] == ["https://docs.example.com/spec"]
    assert [entry["requested_url"] for entry in payload["fetches"]] == [
        "https://docs.example.com/spec",
        "https://docs.example.com/spec",
    ]
    assert metrics["unique_web_fetch_urls"] == 1
    assert metrics["duplicate_web_fetches"] == 1


def test_session_store_resolves_bracket_query_user_url_to_clean_canonical_url(
    tmp_path: Path,
) -> None:
    store = SessionStore(
        enabled=False,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id="bracket-query-user-url",
        cwd=".",
        repo_root=".",
    )
    clean_url = "https://docs.example.com/path?foo[bar]=1"
    truncated_url = "https://docs.example.com/path?foo"
    store.append(
        "user_message",
        {"content": f"Please read {clean_url} before changes."},
    )

    assert store.resolve_web_fetch_url(clean_url) == (
        "user_provided",
        clean_url,
    )
    assert store.classify_web_fetch_url(truncated_url) is None


def test_session_store_resolves_exclamation_user_url_to_exact_canonical_url(
    tmp_path: Path,
) -> None:
    store = SessionStore(
        enabled=False,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id="exclamation-user-url",
        cwd=".",
        repo_root=".",
    )
    clean_url = "https://docs.example.com/Yahoo!"
    truncated_url = "https://docs.example.com/Yahoo"
    store.append(
        "user_message",
        {"content": f"Please read {clean_url} before changes."},
    )

    assert store.resolve_web_fetch_url(clean_url) == (
        "user_provided",
        clean_url,
    )
    assert store.classify_web_fetch_url(truncated_url) is None


def test_session_store_resolves_parenthesized_markdown_link_to_clean_target_url(
    tmp_path: Path,
) -> None:
    store = SessionStore(
        enabled=False,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id="parenthesized-markdown-link-url",
        cwd=".",
        repo_root=".",
    )
    clean_url = "https://docs.example.com/spec"
    corrupted_url = "https://docs.example.com/spec)"
    store.append(
        "user_message",
        {"content": "Please read ([docs](https://docs.example.com/spec)) before changes."},
    )

    assert store.resolve_web_fetch_url(clean_url) == (
        "user_provided",
        clean_url,
    )
    assert store.classify_web_fetch_url(corrupted_url) is None


def test_session_store_resolves_markdown_link_title_parenthesized_target_url(
    tmp_path: Path,
) -> None:
    store = SessionStore(
        enabled=False,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id="markdown-link-title-parenthesized-target",
        cwd=".",
        repo_root=".",
    )
    clean_url = "https://docs.example.com/Function_(mathematics)"
    truncated_url = "https://docs.example.com/Function_(mathematics"
    store.append(
        "user_message",
        {
            "content": (
                "Please read [Function](https://docs.example.com/Function_(mathematics) "
                '"Function docs") before changes.'
            )
        },
    )

    assert store.resolve_web_fetch_url(clean_url) == (
        "user_provided",
        clean_url,
    )
    assert store.classify_web_fetch_url(truncated_url) is None

    payload = store.web_research_artifact_payload()
    assert payload["deduped_normalized_user_urls"] == [clean_url]
    assert truncated_url not in payload["deduped_normalized_user_urls"]


def test_session_store_resolves_backtick_wrapped_user_url_to_clean_canonical_url(
    tmp_path: Path,
) -> None:
    store = SessionStore(
        enabled=False,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id="wrapped-user-url",
        cwd=".",
        repo_root=".",
    )
    store.append(
        "user_message",
        {"content": "Please read `https://docs.example.com/spec` and use it."},
    )

    assert store.resolve_web_fetch_url("https://docs.example.com/spec") == (
        "user_provided",
        "https://docs.example.com/spec",
    )


def test_session_store_resolves_markdown_link_url_with_legal_trailing_parenthesis(
    tmp_path: Path,
) -> None:
    store = SessionStore(
        enabled=False,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id="markdown-link-trailing-parenthesis",
        cwd=".",
        repo_root=".",
    )
    clean_url = "https://docs.example.com/path?x=a)"
    store.append(
        "user_message",
        {"content": f"Please read [docs]({clean_url}) before changes."},
    )

    assert store.resolve_web_fetch_url(clean_url) == (
        "user_provided",
        clean_url,
    )
    assert store.classify_web_fetch_url("https://docs.example.com/path?x=a") is None


def test_session_store_web_artifact_preserves_structured_markdown_target_punctuation(
    tmp_path: Path,
) -> None:
    sid = "structured-markdown-target-punctuation"
    store = SessionStore(
        enabled=False,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id=sid,
        cwd=".",
        repo_root=".",
    )
    clean_url = "https://docs.example.com/path."
    truncated_url = "https://docs.example.com/path"
    store.append(
        "user_message",
        {
            "content": (
                "Please inspect [docs](https://docs.example.com/path.) and "
                "<https://docs.example.com/path.> before changes."
            )
        },
    )
    store.append(
        "tool_call",
        {"name": "web_fetch", "step": 1, "arguments": {"url": clean_url}},
    )
    store.append(
        "tool_result",
        {
            "name": "web_fetch",
            "step": 1,
            "result": {
                "url": clean_url,
                "final_url": clean_url,
                "status_code": 200,
                "content_type": "text/html",
                "title": "Docs",
                "backend": "httpx",
            },
        },
    )

    payload = store.web_research_artifact_payload()
    metrics = store.web_research_metrics_payload()

    assert payload["deduped_normalized_user_urls"] == [clean_url]
    assert payload["deduped_normalized_fetch_urls"] == [clean_url]
    assert truncated_url not in payload["deduped_normalized_user_urls"]
    assert payload["fetches"][0]["requested_url"] == clean_url
    assert metrics["unique_web_fetch_urls"] == 1
    assert metrics["duplicate_web_fetches"] == 0


def test_session_store_web_artifact_dedupes_parenthesized_markdown_link_target(
    tmp_path: Path,
) -> None:
    sid = "parenthesized-markdown-link-dedupe"
    store = SessionStore(
        enabled=False,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id=sid,
        cwd=".",
        repo_root=".",
    )
    clean_url = "https://docs.example.com/spec"
    corrupted_url = "https://docs.example.com/spec)"
    store.append(
        "user_message",
        {
            "content": (
                "Please inspect ([docs](https://docs.example.com/spec)) and "
                "https://docs.example.com/spec before changes."
            )
        },
    )
    store.append(
        "tool_call",
        {"name": "web_fetch", "step": 1, "arguments": {"url": clean_url}},
    )
    store.append(
        "tool_result",
        {
            "name": "web_fetch",
            "step": 1,
            "result": {
                "url": clean_url,
                "final_url": clean_url,
                "status_code": 200,
                "content_type": "text/html",
                "title": "Spec",
                "backend": "httpx",
            },
        },
    )

    payload = store.web_research_artifact_payload()
    metrics = store.web_research_metrics_payload()

    assert payload["deduped_normalized_user_urls"] == [clean_url]
    assert payload["deduped_normalized_fetch_urls"] == [clean_url]
    assert corrupted_url not in payload["deduped_normalized_user_urls"]
    assert payload["fetches"][0]["requested_url"] == clean_url
    assert metrics["unique_web_fetch_urls"] == 1
    assert metrics["duplicate_web_fetches"] == 0


def test_session_store_web_artifact_dedupes_markdown_wrapped_fetch_url_variants(
    tmp_path: Path,
) -> None:
    sid = "fetch-url-markdown-wrapper-dedupe"
    store = SessionStore(
        enabled=False,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id=sid,
        cwd=".",
        repo_root=".",
    )
    raw_wrapped_url = "`https://docs.example.com/spec`"
    clean_url = "https://docs.example.com/spec"
    store.append(
        "user_message",
        {
            "content": (
                "Please inspect `https://docs.example.com/spec`, "
                "*https://docs.example.com/spec*, and _https://docs.example.com/spec_."
            )
        },
    )
    store.append(
        "tool_call",
        {"name": "web_fetch", "step": 1, "arguments": {"url": raw_wrapped_url}},
    )
    store.append(
        "tool_result",
        {
            "name": "web_fetch",
            "step": 1,
            "result": {
                "url": clean_url,
                "final_url": clean_url,
                "status_code": 200,
                "content_type": "text/html",
                "title": "Spec",
                "backend": "httpx",
                "raw_input_url": raw_wrapped_url,
            },
        },
    )
    store.append(
        "tool_call",
        {"name": "web_fetch", "step": 2, "arguments": {"url": clean_url}},
    )
    store.append(
        "tool_result",
        {
            "name": "web_fetch",
            "step": 2,
            "result": {
                "url": clean_url,
                "final_url": clean_url,
                "status_code": 200,
                "content_type": "text/html",
                "title": "Spec",
                "backend": "httpx",
            },
        },
    )

    payload = store.web_research_artifact_payload()
    metrics = store.web_research_metrics_payload()
    first_fetch = payload["fetches"][0]

    assert payload["deduped_normalized_user_urls"] == [clean_url]
    assert payload["deduped_normalized_fetch_urls"] == [clean_url]
    assert [entry["requested_url"] for entry in payload["fetches"]] == [clean_url, clean_url]
    assert first_fetch["raw_input_url"] == raw_wrapped_url
    assert first_fetch["provenance_classification"] == "user_provided"
    assert metrics["unique_web_fetch_urls"] == 1
    assert metrics["duplicate_web_fetches"] == 1


def test_session_store_web_artifact_dedupes_urls_with_legal_trailing_parenthesis(
    tmp_path: Path,
) -> None:
    sid = "fetch-url-trailing-parenthesis-dedupe"
    store = SessionStore(
        enabled=False,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id=sid,
        cwd=".",
        repo_root=".",
    )
    clean_url = "https://docs.example.com/path?x=a)"
    truncated_url = "https://docs.example.com/path?x=a"
    store.append(
        "user_message",
        {
            "content": (
                f"Please read [docs]({clean_url}) and then {clean_url} again before changes."
            )
        },
    )
    store.append(
        "tool_call",
        {"name": "web_fetch", "step": 1, "arguments": {"url": clean_url}},
    )
    store.append(
        "tool_result",
        {
            "name": "web_fetch",
            "step": 1,
            "result": {
                "url": clean_url,
                "final_url": clean_url,
                "status_code": 200,
                "content_type": "text/html",
                "title": "Docs",
                "backend": "httpx",
            },
        },
    )

    payload = store.web_research_artifact_payload()
    metrics = store.web_research_metrics_payload()

    assert payload["deduped_normalized_user_urls"] == [clean_url]
    assert payload["deduped_normalized_fetch_urls"] == [clean_url]
    assert truncated_url not in payload["deduped_normalized_user_urls"]
    assert payload["fetches"][0]["requested_url"] == clean_url
    assert metrics["unique_web_fetch_urls"] == 1
    assert metrics["duplicate_web_fetches"] == 0


def test_session_store_web_artifact_preserves_bracket_query_url_without_truncated_entry(
    tmp_path: Path,
) -> None:
    sid = "fetch-url-bracket-query"
    store = SessionStore(
        enabled=False,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id=sid,
        cwd=".",
        repo_root=".",
    )
    clean_url = "https://docs.example.com/path?foo[bar]=1"
    truncated_url = "https://docs.example.com/path?foo"
    store.append(
        "user_message",
        {"content": f"Please inspect {clean_url} and `https://docs.example.com/path?arr[]=1`."},
    )
    store.append(
        "tool_call",
        {"name": "web_fetch", "step": 1, "arguments": {"url": clean_url}},
    )
    store.append(
        "tool_result",
        {
            "name": "web_fetch",
            "step": 1,
            "result": {
                "url": clean_url,
                "final_url": clean_url,
                "status_code": 200,
                "content_type": "text/html",
                "title": "Docs",
                "backend": "httpx",
            },
        },
    )

    payload = store.web_research_artifact_payload()
    metrics = store.web_research_metrics_payload()

    assert payload["deduped_normalized_user_urls"] == [
        clean_url,
        "https://docs.example.com/path?arr[]=1",
    ]
    assert payload["deduped_normalized_fetch_urls"] == [clean_url]
    assert truncated_url not in payload["deduped_normalized_user_urls"]
    assert payload["fetches"][0]["requested_url"] == clean_url
    assert metrics["unique_web_fetch_urls"] == 1
    assert metrics["duplicate_web_fetches"] == 0


def test_session_store_web_artifact_preserves_exclamation_url_without_truncated_entry(
    tmp_path: Path,
) -> None:
    sid = "fetch-url-exclamation"
    store = SessionStore(
        enabled=False,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id=sid,
        cwd=".",
        repo_root=".",
    )
    clean_url = "https://docs.example.com/Yahoo!"
    truncated_url = "https://docs.example.com/Yahoo"
    store.append(
        "user_message",
        {"content": f"Please inspect {clean_url} before changes."},
    )
    store.append(
        "tool_call",
        {"name": "web_fetch", "step": 1, "arguments": {"url": clean_url}},
    )
    store.append(
        "tool_result",
        {
            "name": "web_fetch",
            "step": 1,
            "result": {
                "url": clean_url,
                "final_url": clean_url,
                "status_code": 200,
                "content_type": "text/html",
                "title": "Yahoo",
                "backend": "httpx",
            },
        },
    )

    payload = store.web_research_artifact_payload()
    metrics = store.web_research_metrics_payload()

    assert payload["deduped_normalized_user_urls"] == [clean_url]
    assert payload["deduped_normalized_fetch_urls"] == [clean_url]
    assert truncated_url not in payload["deduped_normalized_user_urls"]
    assert payload["fetches"][0]["requested_url"] == clean_url
    assert metrics["unique_web_fetch_urls"] == 1
    assert metrics["duplicate_web_fetches"] == 0


def test_session_store_preserves_parenthesized_web_search_source_url(
    tmp_path: Path,
) -> None:
    sid = "parenthesized-search-source"
    store = SessionStore(
        enabled=False,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id=sid,
        cwd=".",
        repo_root=".",
    )
    store.append(
        "tool_result",
        {
            "name": "web_search",
            "step": 1,
            "result": {
                "query": "function mathematics docs",
                "backend": "openai_responses",
                "sources": [
                    {
                        "title": "Function (mathematics)",
                        "url": "https://docs.example.com/Function_(mathematics)",
                        "snippet": "Balanced parenthesized path",
                    }
                ],
            },
        },
    )

    payload = store.web_research_artifact_payload()

    assert payload["deduped_normalized_search_source_urls"] == [
        "https://docs.example.com/Function_(mathematics)"
    ]
    assert payload["searches"][0]["returned_sources"][0]["normalized_url"] == (
        "https://docs.example.com/Function_(mathematics)"
    )


def test_session_store_classifies_dashscope_parenthesized_search_url_as_returned_by_web_search(
    tmp_path: Path,
) -> None:
    sid = "dashscope-parenthesized-search-source"
    store = SessionStore(
        enabled=False,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id=sid,
        cwd=".",
        repo_root=".",
    )
    url = "https://docs.example.com/Function_(mathematics)"
    store.append(
        "tool_result",
        {
            "name": "web_search",
            "step": 1,
            "result": {
                "query": "function mathematics docs",
                "backend": "dashscope_chat",
                "sources": [
                    {
                        "title": "Function (mathematics)",
                        "url": url,
                        "snippet": "Balanced parenthesized path",
                    }
                ],
            },
        },
    )
    store.append(
        "tool_call",
        {
            "name": "web_fetch",
            "step": 2,
            "arguments": {"url": url},
        },
    )
    store.append(
        "tool_result",
        {
            "name": "web_fetch",
            "step": 2,
            "result": {
                "url": url,
                "final_url": url,
                "status_code": 200,
                "backend": "httpx",
            },
        },
    )

    payload = store.web_research_artifact_payload()
    fetch = payload["fetches"][0]

    assert fetch["requested_url"] == url
    assert fetch["normalized_requested_url"] == url
    assert fetch["provenance_classification"] == "returned_by_web_search"
    assert payload["deduped_normalized_search_source_urls"] == [url]


def test_session_store_reopen_merges_newer_web_artifact_ahead_of_log(
    tmp_path: Path,
) -> None:
    sid = "resume-artifact-ahead"
    logged_events: list[dict[str, object]] = [
        {
            "type": "tool_result",
            "session_id": sid,
            "payload": {
                "name": "web_search",
                "step": 1,
                "result": {
                    "query": "docs example start",
                    "backend": "openai_responses",
                    "sources": [
                        {
                            "title": "Start",
                            "url": "https://docs.example.com/start",
                            "snippet": "Initial source",
                        }
                    ],
                },
            },
        }
    ]
    artifact_only_events: list[dict[str, object]] = [
        {
            "type": "tool_result",
            "session_id": sid,
            "payload": {
                "name": "web_search",
                "step": 2,
                "result": {
                    "query": "docs example guide",
                    "backend": "openai_responses",
                    "sources": [
                        {
                            "title": "Guide",
                            "url": "https://docs.example.com/guide",
                            "snippet": "Artifact-only source",
                        }
                    ],
                },
            },
        }
    ]
    _write_web_artifact_ahead_of_log(
        sessions_dir=tmp_path,
        session_id=sid,
        logged_events=logged_events,
        artifact_only_events=artifact_only_events,
    )

    reopened = SessionStore(
        enabled=False,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id=sid,
        cwd=".",
        repo_root=".",
    )

    assert (
        reopened.classify_web_fetch_url("https://docs.example.com/start")
        == "returned_by_web_search"
    )
    assert (
        reopened.classify_web_fetch_url("https://docs.example.com/guide")
        == "returned_by_web_search"
    )
    payload = reopened.web_research_artifact_payload()
    assert payload["deduped_normalized_search_source_urls"] == [
        "https://docs.example.com/start",
        "https://docs.example.com/guide",
    ]
    assert [entry["normalized_query"] for entry in payload["searches"]] == [
        "docs example start",
        "docs example guide",
    ]


def test_session_store_artifact_only_reopen_preserves_search_url_classification(
    tmp_path: Path,
) -> None:
    sid = "resume-artifact-only"
    artifact_root = tmp_path / sid
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_payload = build_web_research_artifact_from_events(
        [
            {
                "type": "tool_result",
                "session_id": sid,
                "payload": {
                    "name": "web_search",
                    "step": 1,
                    "result": {
                        "query": "docs example",
                        "backend": "openai_responses",
                        "sources": [
                            {
                                "title": "Guide",
                                "url": "https://docs.example.com/guide",
                                "snippet": "Artifact-only source",
                            }
                        ],
                    },
                },
            }
        ]
    )
    (artifact_root / "web_research_sources.json").write_text(
        json.dumps(artifact_payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )

    reopened = SessionStore(
        enabled=False,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id=sid,
        cwd=".",
        repo_root=".",
    )

    assert (
        reopened.classify_web_fetch_url("https://docs.example.com/guide")
        == "returned_by_web_search"
    )


def test_session_store_reopen_rewrites_cumulative_artifact_when_newer_artifact_preexisted(
    tmp_path: Path,
) -> None:
    sid = "resume-artifact-ahead-append"
    logged_events: list[dict[str, object]] = [
        {
            "type": "user_message",
            "session_id": sid,
            "payload": {"content": "Please inspect https://docs.example.com/spec"},
        }
    ]
    artifact_only_events: list[dict[str, object]] = [
        {
            "type": "tool_result",
            "session_id": sid,
            "payload": {
                "name": "web_search",
                "step": 2,
                "result": {
                    "query": "docs example guide",
                    "backend": "openai_responses",
                    "sources": [
                        {
                            "title": "Guide",
                            "url": "https://docs.example.com/guide",
                            "snippet": "Artifact-only source",
                        }
                    ],
                },
            },
        }
    ]
    _write_web_artifact_ahead_of_log(
        sessions_dir=tmp_path,
        session_id=sid,
        logged_events=logged_events,
        artifact_only_events=artifact_only_events,
    )

    reopened = SessionStore(
        enabled=False,
        artifact_persistence_enabled=True,
        sessions_dir=tmp_path,
        session_id=sid,
        cwd=".",
        repo_root=".",
    )
    reopened.append(
        "tool_call",
        {"name": "web_fetch", "step": 3, "arguments": {"url": "https://docs.example.com/guide"}},
    )
    reopened.append(
        "tool_result",
        {
            "name": "web_fetch",
            "step": 3,
            "result": {
                "url": "https://docs.example.com/guide",
                "final_url": "https://docs.example.com/final",
                "status_code": 200,
                "content_type": "text/html",
                "title": "Final",
                "backend": "httpx",
            },
        },
    )
    reopened.close()

    payload = json.loads((tmp_path / sid / "web_research_sources.json").read_text(encoding="utf-8"))
    assert payload["deduped_normalized_user_urls"] == ["https://docs.example.com/spec"]
    assert payload["deduped_normalized_search_source_urls"] == ["https://docs.example.com/guide"]
    assert payload["deduped_normalized_fetch_urls"] == ["https://docs.example.com/guide"]
    assert payload["deduped_normalized_final_fetch_urls"] == ["https://docs.example.com/final"]
    assert [entry["normalized_query"] for entry in payload["searches"]] == ["docs example guide"]


def test_local_session_owner_is_deterministic_and_nonempty() -> None:
    from alysis_code.session_store import local_session_owner

    owner = local_session_owner()
    assert owner is not None
    assert "@" in owner
    # Deterministic: two calls on the same account/host agree.
    assert local_session_owner() == owner


def test_local_session_owner_uses_direct_nonshell_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import alysis_code.session_store as session_store_module

    monkeypatch.setattr(session_store_module.getpass, "getuser", lambda: "Alice")
    monkeypatch.setattr(session_store_module.socket, "gethostname", lambda: "Workstation")

    assert session_store_module.local_session_owner() == "Alice@Workstation"


def test_session_store_stamps_owner_on_events(tmp_path: Path) -> None:
    from alysis_code.session_store import (
        SessionStore,
        list_sessions,
        local_session_owner,
        read_session_events,
    )

    store = SessionStore(
        enabled=True,
        sessions_dir=tmp_path,
        session_id="owned",
        cwd=str(tmp_path),
        repo_root=None,
        workspace_root=str(tmp_path),
    )
    store.append("session_start", {"mode": "auto"})
    store.close()

    events = list(read_session_events(tmp_path / "owned.jsonl"))
    assert events and events[0].get("owner") == local_session_owner()

    infos = list_sessions(tmp_path)
    assert [info.session_id for info in infos] == ["owned"]
    assert infos[0].owner == local_session_owner()


def test_read_session_first_event_scope_reads_owner(tmp_path: Path) -> None:
    from alysis_code.session_store import read_session_first_event_scope

    log = tmp_path / "s.jsonl"
    log.write_text(
        json.dumps(
            {
                "type": "session_start",
                "ts": "2026-01-01T10:00:00+00:00",
                "workspace_root": "/repo",
                "git_root": "/repo",
                "owner": "alice@laptop",
                "payload": {"mode": "auto"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert read_session_first_event_scope(log) == ("/repo", "/repo", "alice@laptop")

    legacy = tmp_path / "legacy.jsonl"
    legacy.write_text(
        json.dumps(
            {
                "type": "session_start",
                "ts": "2026-01-01T10:00:00+00:00",
                "workspace_root": "/repo",
                "payload": {"mode": "auto"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert read_session_first_event_scope(legacy) == ("/repo", None, None)


def test_session_belongs_to_owner_predicate(tmp_path: Path) -> None:
    from alysis_code.session_store import SessionInfo, session_belongs_to_owner

    def info(owner: str | None) -> SessionInfo:
        return SessionInfo(session_id="s", path=tmp_path / "s.jsonl", mtime=1.0, owner=owner)

    # Legacy logs (no recorded owner) stay visible for any local identity.
    assert session_belongs_to_owner(info(None), "alice@laptop") is True
    assert session_belongs_to_owner(info(None), None) is True

    # Recorded owner must match the local identity (case-insensitively:
    # Windows usernames and DNS hostnames are not case-significant).
    assert session_belongs_to_owner(info("alice@laptop"), "alice@laptop") is True
    assert session_belongs_to_owner(info("Alice@LAPTOP"), "alice@laptop") is True
    assert session_belongs_to_owner(info("mallory@other-pc"), "alice@laptop") is False

    # A foreign-stamped log never surfaces on an unidentifiable account.
    assert session_belongs_to_owner(info("alice@laptop"), None) is False
    assert session_belongs_to_owner(info("alice@laptop"), "   ") is False


def test_filter_sessions_to_local_owner_drops_foreign_sessions(tmp_path: Path) -> None:
    from alysis_code.session_store import (
        SessionInfo,
        filter_sessions_to_local_owner,
        local_session_owner,
    )

    mine = SessionInfo(
        session_id="mine", path=tmp_path / "mine.jsonl", mtime=3.0, owner=local_session_owner()
    )
    foreign = SessionInfo(
        session_id="foreign",
        path=tmp_path / "foreign.jsonl",
        mtime=2.0,
        owner="someone-else@another-host",
    )
    legacy = SessionInfo(session_id="legacy", path=tmp_path / "legacy.jsonl", mtime=1.0)

    kept = filter_sessions_to_local_owner([mine, foreign, legacy])
    assert [info.session_id for info in kept] == ["mine", "legacy"]


def test_session_belongs_to_owner_last_owner_self_heal(tmp_path: Path) -> None:
    from alysis_code.session_store import SessionInfo, session_belongs_to_owner

    # Identity drift (e.g. hostname rename): the creator stamp no longer
    # matches, but an explicit resume re-stamped the tail with the new
    # identity, so the session lists again for its rightful owner.
    healed = SessionInfo(
        session_id="s",
        path=tmp_path / "s.jsonl",
        mtime=1.0,
        owner="alice@old-host",
        last_owner="alice@new-host",
    )
    assert session_belongs_to_owner(healed, "alice@new-host") is True
    assert session_belongs_to_owner(healed, "alice@old-host") is True

    # A foreign log stays hidden either way: both stamps are foreign.
    assert session_belongs_to_owner(healed, "bob@somewhere") is False

    # A legacy log resumed post-upgrade carries an owner only at the tail;
    # it belongs to that account and is no longer visible to everyone.
    adopted = SessionInfo(
        session_id="a",
        path=tmp_path / "a.jsonl",
        mtime=1.0,
        owner=None,
        last_owner="alice@laptop",
    )
    assert session_belongs_to_owner(adopted, "alice@laptop") is True
    assert session_belongs_to_owner(adopted, "bob@somewhere") is False


def test_list_sessions_reads_late_owner_stamp_and_last_owner(tmp_path: Path) -> None:
    """A pre-upgrade log resumed post-upgrade carries its owner stamp only on
    later events. The first-event scan must keep looking (within its bound)
    instead of classifying the log as legacy, and the tail read must surface
    the newest event's owner."""
    from alysis_code.session_store import list_sessions

    log = tmp_path / "mixed.jsonl"
    events = [
        # Two pre-upgrade events: workspace recorded, no owner stamp.
        {
            "type": "session_start",
            "ts": "2026-01-01T10:00:00+00:00",
            "workspace_root": "/repo",
            "payload": {"mode": "auto"},
        },
        {
            "type": "user_message",
            "ts": "2026-01-01T10:00:01+00:00",
            "workspace_root": "/repo",
            "payload": {"content": "old"},
        },
        # Post-upgrade resume appends stamped events.
        {
            "type": "session_start",
            "ts": "2026-02-01T10:00:00+00:00",
            "workspace_root": "/repo",
            "owner": "alice@laptop",
            "payload": {"mode": "auto"},
        },
        {
            "type": "user_message",
            "ts": "2026-02-01T10:00:01+00:00",
            "workspace_root": "/repo",
            "owner": "alice@new-laptop",
            "payload": {"content": "new"},
        },
    ]
    log.write_text(
        "".join(json.dumps(event, ensure_ascii=True) + "\n" for event in events),
        encoding="utf-8",
    )

    infos = list_sessions(tmp_path)
    assert len(infos) == 1
    assert infos[0].owner == "alice@laptop"
    assert infos[0].last_owner == "alice@new-laptop"
    assert infos[0].last_event_ts == "2026-02-01T10:00:01+00:00"


def test_read_session_last_event_fields_owner_from_newest_event(tmp_path: Path) -> None:
    from alysis_code.session_store import read_session_last_event_fields

    log = tmp_path / "s.jsonl"
    log.write_text(
        json.dumps({"type": "a", "ts": "2026-01-01T10:00:00+00:00", "owner": "old@host"})
        + "\n"
        + json.dumps({"type": "b", "ts": "2026-01-02T10:00:00+00:00", "owner": "new@host"})
        + "\n",
        encoding="utf-8",
    )
    assert read_session_last_event_fields(log) == ("2026-01-02T10:00:00+00:00", "new@host")

    legacy = tmp_path / "legacy.jsonl"
    legacy.write_text(
        json.dumps({"type": "a", "ts": "2026-01-01T10:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )
    assert read_session_last_event_fields(legacy) == ("2026-01-01T10:00:00+00:00", None)


def test_sessions_list_cli_hides_foreign_sessions_unless_all(tmp_path: Path) -> None:
    import os as _os
    import uuid

    from typer.testing import CliRunner

    from alysis_code import cli as cli_mod
    from alysis_code.session_store import local_session_owner

    runner = CliRunner()
    cfg_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    sessions_dir = data_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    def _write_log(session_id: str, owner: str | None, ts: str) -> None:
        stamp = {"owner": owner} if owner else {}
        event = {
            "type": "session_start",
            "ts": ts,
            "session_id": session_id,
            **stamp,
            "payload": {"mode": "auto"},
        }
        (sessions_dir / f"{session_id}.jsonl").write_text(
            json.dumps(event) + "\n", encoding="utf-8"
        )

    _write_log("sess_own", local_session_owner(), "2026-07-10T12:00:00+00:00")
    _write_log(
        "sess_foreign",
        f"foreign-user@foreign-host-{uuid.uuid4().hex}",
        "2026-07-11T12:00:00+00:00",
    )

    env = {
        "ALYSIS_CONFIG_DIR": _os.fspath(cfg_dir),
        "ALYSIS_DATA_DIR": _os.fspath(data_dir),
    }

    scoped = runner.invoke(cli_mod.app, ["sessions", "list"], env=env, terminal_width=200)
    assert scoped.exit_code == 0
    assert "sess_own" in scoped.output
    assert "sess_foreign" not in scoped.output
    assert "hidden" in scoped.output

    unscoped = runner.invoke(
        cli_mod.app, ["sessions", "list", "--all"], env=env, terminal_width=200
    )
    assert unscoped.exit_code == 0
    assert "sess_own" in unscoped.output
    assert "sess_foreign" in unscoped.output


def test_private_transcript_permissions_on_create_and_legacy_append(tmp_path: Path) -> None:
    import stat

    path = tmp_path / "private.jsonl"
    for legacy in (False, True):
        if legacy:
            path.chmod(0o644)
        store = SessionStore(
            enabled=True, sessions_dir=tmp_path, session_id="private", cwd=".", repo_root="."
        )
        store.append("final", {"content": "synthetic"})
        store.close()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
