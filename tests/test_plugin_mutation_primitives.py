"""Unwired P4 primitives: temporary roots only, no installer/config integration."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from alysis_code.extensions import _mutation_primitives as p


def test_regular_snapshot_is_sealed_and_counts_directories(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub/a").write_bytes(b"abc")
    with p.RootHandle.open(tmp_path.resolve()) as root:
        sealed = root.snapshot()
        assert root.read_regular(("sub", "a"), limit=3) == b"abc"
        identity = root.identity
    assert sealed.root_identity == identity
    assert sealed.entry_count == 3 and sealed.total_bytes == 3
    assert next(e.content for e in sealed.entries if e.parts == ("sub", "a")) == b"abc"
    (tmp_path / "sub/a").write_bytes(b"changed")
    assert next(e.content for e in sealed.entries if e.parts == ("sub", "a")) == b"abc"


@pytest.mark.parametrize("kind", ["leaf_symlink", "parent_symlink", "source_hardlink", "fifo"])
def test_no_follow_or_special_file_read(tmp_path, kind):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "sentinel").write_bytes(b"private-fixture")
    source = tmp_path / "source"
    source.mkdir()
    if kind == "leaf_symlink":
        (source / "x").symlink_to(foreign / "sentinel")
    elif kind == "parent_symlink":
        (source / "dir").symlink_to(foreign, target_is_directory=True)
    elif kind == "source_hardlink":
        os.link(foreign / "sentinel", source / "x")
    else:
        os.mkfifo(source / "x")
    with p.RootHandle.open(source.resolve()) as root:
        with pytest.raises(p.MutationError) as error:
            root.snapshot()
        assert str(error.value) == "Plugin mutation refused: unsafe_path."
    assert (foreign / "sentinel").read_bytes() == b"private-fixture"


def test_root_and_ancestor_links_refused(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    for path in [tmp_path / "link", tmp_path / "link/sub"]:
        with pytest.raises(p.MutationError, match="unsafe_path"):
            p.RootHandle.open(path)


@pytest.mark.parametrize("parts", [(), ("..",), ("a/b",), ("",), (".",), ("a\x00b",), ("\ud800",)])
def test_relative_paths_are_closed(tmp_path, parts):
    with p.RootHandle.open(tmp_path.resolve()) as root:
        with pytest.raises(p.MutationError, match="unsafe_path"):
            root.read_regular(parts)


def test_open_root_stays_anchored_after_path_replacement(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "x").write_bytes(b"owned")
    other = tmp_path / "other"
    other.mkdir()
    (other / "x").write_bytes(b"foreign")
    with p.RootHandle.open(source) as root:
        source.rename(tmp_path / "old")
        source.symlink_to(other, target_is_directory=True)
        assert root.read_regular(("x",)) == b"owned"


def test_leaf_swap_at_open_is_refused(tmp_path, monkeypatch):
    (tmp_path / "x").write_bytes(b"owned")
    foreign = tmp_path.parent / "fixture-foreign"
    foreign.write_bytes(b"foreign")
    with p.RootHandle.open(tmp_path.resolve()) as root:
        real_open = os.open

        def swap(path, flags, *a, **kw):
            if path == "x":
                (tmp_path / "x").unlink()
                (tmp_path / "x").symlink_to(foreign)
            return real_open(path, flags, *a, **kw)

        monkeypatch.setattr(os, "open", swap)
        with pytest.raises(p.MutationError, match="unsafe_path"):
            root.read_regular(("x",))


@pytest.mark.parametrize(
    "field,value", [("entries", 2), ("depth", 0), ("file_bytes", 2), ("total_bytes", 2)]
)
def test_snapshot_limits_refuse_without_truncation(tmp_path, field, value):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub/x").write_bytes(b"abc")
    with p.RootHandle.open(tmp_path.resolve()) as root:
        with pytest.raises(p.MutationError, match="resource_limit"):
            root.snapshot(p.SnapshotLimits(**{field: value}))
        assert root.snapshot().total_bytes == 3


def test_exact_file_and_aggregate_bounds(tmp_path):
    (tmp_path / "x").write_bytes(b"abc")
    with p.RootHandle.open(tmp_path.resolve()) as root:
        assert (
            root.snapshot(
                p.SnapshotLimits(entries=2, depth=1, file_bytes=3, total_bytes=3)
            ).total_bytes
            == 3
        )
        with pytest.raises(p.MutationError, match="resource_limit"):
            root.read_regular(("x",), limit=2)
    with pytest.raises(p.MutationError, match="unsupported"):
        p.SnapshotLimits(file_bytes=8 * 1024 * 1024 + 1)


def test_closed_handle_and_unsupported_platform_refuse(tmp_path, monkeypatch):
    root = p.RootHandle.open(tmp_path.resolve())
    root.close()
    root.close()
    with pytest.raises(p.MutationError, match="unavailable"):
        root.snapshot()
    monkeypatch.setattr(p, "_supported", lambda: False)
    with pytest.raises(p.MutationError, match="unsupported"):
        p.RootHandle.open(tmp_path.resolve())


def test_locks_order_timeout_cancel_and_release(tmp_path):
    with p.RootHandle.open(tmp_path.resolve()) as root:
        keys = [p.LockKey(2, root.identity, ("config",)), p.LockKey(0, root.identity, ())]
        with p.ordered_locks(root, keys) as held:
            assert held.keys == tuple(sorted(keys))
            with pytest.raises(p.MutationError, match="busy"):
                with p.ordered_locks(root, keys, timeout=0.01):
                    pass
            with pytest.raises(p.MutationError, match="cancelled"):
                with p.ordered_locks(root, keys, cancelled=lambda: True):
                    pass
        with p.ordered_locks(root, keys, timeout=0.1):
            pass
        with pytest.raises(p.MutationError, match="unsupported"):
            with p.ordered_locks(root, keys + keys):
                pass


def test_lock_wait_cancellation_releases_earlier_handles(tmp_path):
    with p.RootHandle.open(tmp_path.resolve()) as root:
        first = p.LockKey(0, root.identity, ())
        last = p.LockKey(2, root.identity, ("config",))
        cancel = threading.Event()
        entered = threading.Event()
        outcomes = []

        def waiter():
            entered.set()
            try:
                with p.ordered_locks(root, [first, last], cancelled=cancel.is_set):
                    pass
            except p.MutationError as error:
                outcomes.append(error.reason)

        with p.ordered_locks(root, [last]):
            thread = threading.Thread(target=waiter)
            thread.start()
            assert entered.wait(1)
            cancel.set()
            thread.join(1)
            assert not thread.is_alive()
        assert outcomes == ["cancelled"]
        with p.ordered_locks(root, [first, last], timeout=0.1):
            pass


def test_lock_leaf_symlink_and_hardlink_refused(tmp_path):
    foreign = tmp_path / "foreign"
    foreign.write_bytes(b"foreign")
    with p.RootHandle.open(tmp_path.resolve()) as root:
        key = p.LockKey(0, root.identity, ())
        leaf = root.path / p.lock_filename(key)
        leaf.symlink_to(foreign)
        with pytest.raises(p.MutationError, match="unsafe_path"):
            with p.ordered_locks(root, [key]):
                pass
        leaf.unlink()
        os.link(foreign, leaf)
        with pytest.raises(p.MutationError, match="unsafe_path"):
            with p.ordered_locks(root, [key]):
                pass
    assert foreign.read_bytes() == b"foreign"


def test_fixed_limits_and_errors_do_not_echo_input():
    assert p.MAX_LOCK_SECONDS == 5 and p.POLL_SECONDS == 0.05 and p.PUBLICATION_SECONDS == 30
    assert (
        p.MAX_COMPONENTS,
        p.MAX_RESOURCES,
        p.MAX_CONFIG_BYTES,
        p.MAX_JOURNAL_BYTES,
        p.MAX_RECOVERY_BYTES,
    ) == (256, 1024, 4 * 1024**2, 1024**2, 256 * 1024**2)
    assert str(p.MutationError("secret-fixture-path")) == "Plugin mutation refused: unavailable."


def test_kernel_lock_excludes_separate_process_and_releases(tmp_path):
    import subprocess
    import sys

    script = """
import sys
from pathlib import Path
from alysis_code.extensions import _mutation_primitives as p
with p.RootHandle.open(Path(sys.argv[1])) as root:
    try:
        with p.ordered_locks(root,[p.LockKey(0,root.identity,())],timeout=.05): print('acquired')
    except p.MutationError as error: print(error.reason)
"""
    env = {**os.environ, "PYTHONPATH": str(Path(p.__file__).parents[2])}

    def child():
        return subprocess.run(
            [sys.executable, "-c", script, str(tmp_path.resolve())],
            env=env,
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        ).stdout.strip()

    with p.RootHandle.open(tmp_path.resolve()) as root:
        with p.ordered_locks(root, [p.LockKey(0, root.identity, ())]):
            assert child() == "busy"
        assert child() == "acquired"


def test_file_changes_during_bounded_read_refuse(tmp_path, monkeypatch):
    file = tmp_path / "x"
    file.write_bytes(b"abc")
    original = os.read
    with p.RootHandle.open(tmp_path.resolve()) as root:

        def changed(fd, size):
            result = original(fd, size)
            if result:
                file.write_bytes(b"changed")
            return result

        monkeypatch.setattr(os, "read", changed)
        with pytest.raises(p.MutationError):
            root.read_regular(("x",), limit=8)


def test_directory_case_collisions_refuse_when_filesystem_supports_them(tmp_path):
    (tmp_path / "Case").write_bytes(b"a")
    (tmp_path / "case").write_bytes(b"b")
    if len(list(tmp_path.iterdir())) != 2:
        pytest.skip("Native temporary filesystem folds case; cannot create this source collision")
    with p.RootHandle.open(tmp_path.resolve()) as root:
        with pytest.raises(p.MutationError, match="unsafe_path"):
            root.snapshot()


def test_cross_device_descendant_refused(tmp_path, monkeypatch):
    (tmp_path / "x").write_bytes(b"abc")
    with p.RootHandle.open(tmp_path.resolve()) as root:
        real = os.fstat

        def different_device(fd):
            result = real(fd)
            if result.st_ino != root.identity[1]:
                values = list(result)
                values[2] = root.identity[0] + 1
                return os.stat_result(values)
            return result

        monkeypatch.setattr(os, "fstat", different_device)
        with pytest.raises(p.MutationError, match="unsafe_path"):
            root.read_regular(("x",))
