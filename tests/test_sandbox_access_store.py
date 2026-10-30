import json
import os
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from alysis_code.agent.sandbox_access_store import SandboxAccessStore
from alysis_code.agent.scoped_access import ScopedAccess
from alysis_code.config import ConfigError
from alysis_code.host_file_policy import HostFilePolicy
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.surface import ApprovalDecision


@pytest.fixture(params=["seatbelt", "windows-mxc"])
def retained(tmp_path, monkeypatch, request):
    root, extra = (tmp_path / name for name in ("project", "assets"))
    root.mkdir()
    extra.mkdir()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "private-config"))
    monkeypatch.setattr(
        "alysis_code.sandbox_runner.select_shell_sandbox_backend",
        lambda _: SimpleNamespace(backend=request.param),
    )
    settings = ShellSandboxSettings(backend=request.param)
    requests, records, reused = [], [], []

    def manager(session="one", policy=settings):
        return ScopedAccess(
            root=root,
            settings=policy,
            enabled=True,
            policy=HostFilePolicy((), ()),
            approve=lambda r: requests.append(r) or ApprovalDecision(True),
            record=lambda *e: records.append(e),
            events_since=lambda cursor: ([], cursor),
            access_store=SandboxAccessStore(root=root, session_id=session, settings=policy),
            validate_reuse=lambda r: reused.append(r),
        )

    return manager, extra, requests, records, reused


def request(manager, proposal=None):
    return manager.prepare(proposal, command="python build.py", tool="shell_run")


def test_session_survives_restart_not_other_chat_or_policy(retained):
    make, extra, requests, _, reused = retained
    first = make()
    grant = request(first, {"read_folders": [str(extra)], "scope": "session"})
    new = make()
    later = request(new)
    assert later.settings == grant.settings
    assert later.command is None and later.tool is None
    assert len(requests) == 1 and len(reused) == 1
    assert request(make("two")).settings == first.settings
    changed = replace(first.settings, network="on")
    assert request(make(policy=changed)).settings == changed
    path = first.access_store._path(None)
    if os.name == "nt":
        from alysis_code._windows_session_files import assert_current_user_private

        with path.open("rb") as stream:
            assert_current_user_private(stream.fileno())
    else:
        assert path.stat().st_mode & 0o777 == 0o600


def test_profile_requires_person_and_applies_without_reusing_action_approval(retained):
    make, extra, requests, _, reused = retained
    first = make()
    request(
        first,
        {
            **(
                {"network_domains": ["registry.npmjs.org"]}
                if first.settings.backend == "seatbelt"
                else {"network": True}
            ),
            "read_folders": [str(extra)],
            "save_as": "packages",
        },
    )
    assert requests[0].metadata["mandatory_explicit_approval"]
    assert request(first).settings == first.settings  # saving is not a session grant
    second = make("two")
    grant = request(second, {"profile": "packages", "scope": "session"})
    assert grant.command is None and grant.settings.network == "on"
    assert grant.settings.backend == first.settings.backend
    if first.settings.backend == "seatbelt":
        assert grant.settings.network_domains == ("registry.npmjs.org",)
        assert grant.settings.additional_read_roots == (str(extra),)
    else:
        assert grant.settings.network_domains is None
        assert grant.settings.windows_read_roots == (str(extra),)
        assert not grant.settings.additional_read_roots
    assert len(requests) == 1 and len(reused) == 1
    first.manage({"action": "revoke", "profile": "packages"})
    assert request(second).settings == second.settings
    assert second.manage({"action": "list"}) == {"session": None, "profiles": {}}


def test_profile_revocation_invalidates_turn_cache(retained):
    make, _, _, _, _ = retained
    owner, reader = make(), make("two")
    request(owner, {"network": True, "save_as": "net"})
    request(reader, {"profile": "net", "scope": "turn"})
    owner.manage({"action": "revoke", "profile": "net"})
    assert request(reader).settings == reader.settings


def test_stale_session_fails_then_allows_fresh_review(retained):
    make, extra, requests, _, _ = retained
    manager = make()
    request(manager, {"read_folders": [str(extra)], "scope": "session"})
    extra.rename(extra.with_name("previous-assets"))
    extra.mkdir()
    with pytest.raises(ConfigError, match="changed"):
        request(make())
    fresh = request(make(), {"read_folders": [str(extra)], "scope": "session"})
    assert str(extra) in (*fresh.settings.additional_read_roots, *fresh.settings.windows_read_roots)
    assert len(requests) == 2


def test_current_standing_rule_can_block_reuse(retained):
    make, _, _, _, _ = retained
    manager = make()
    request(manager, {"network": True, "scope": "session"})

    def deny(_):
        raise ConfigError("standing rule denied")

    manager.validate_reuse = deny
    with pytest.raises(ConfigError, match="standing rule denied"):
        request(manager)


def test_record_failure_and_revocation_during_review_do_not_persist(retained):
    make, _, _, _, _ = retained
    manager = make()

    def fail(*_):
        raise OSError("audit unavailable")

    manager.record = fail
    with pytest.raises(OSError, match="audit unavailable"):
        request(manager, {"network": True, "save_as": "net", "scope": "session"})
    assert manager.access_store.load() is None
    assert manager.access_store.profiles() == {}
    manager = make()
    request(manager, {"network": True, "scope": "session"})

    def revoke(_):
        make().manage({"action": "revoke"})
        return ApprovalDecision(True)

    manager.approve = revoke
    with pytest.raises(ConfigError, match="changed during approval"):
        request(manager, {"local_preview": True, "scope": "session"})
    assert manager.access_store.load() is None


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "writable", "invalid"])
def test_untrusted_storage_is_not_authority(retained, tmp_path, kind):
    make, _, _, _, _ = retained
    store = make().access_store
    store.save(None)
    path = store._path(None)
    if kind == "symlink":
        target = path.with_name("old")
        path.rename(target)
        try:
            path.symlink_to(target)
        except OSError as exc:
            if os.name == "nt" and exc.winerror == 1314:
                pytest.skip("This non-elevated Windows user cannot create file symlinks")
            raise
    elif kind == "hardlink":
        os.link(path, path.with_name("alias"))
    elif kind == "writable":
        if os.name == "nt":
            subprocess.run(
                ["icacls", str(path), "/grant", "*S-1-1-0:(W)"], check=True, capture_output=True
            )
        else:
            path.chmod(0o666)
    else:
        path.write_text("{broken")
    with pytest.raises(ConfigError):
        store.load()


@pytest.mark.skipif(os.name != "nt", reason="Windows authority handles and ACLs")
def test_windows_private_parent_acl_is_required_and_never_repaired(retained):
    make, _, _, _, _ = retained
    store = make().access_store
    store.save({"network": True})
    path = store._path(None)
    subprocess.run(
        ["icacls", str(path.parent), "/grant", "*S-1-1-0:(W)"], check=True, capture_output=True
    )
    with pytest.raises(ConfigError, match="owner-only ACL"):
        store.load()
    with pytest.raises(ConfigError, match="owner-only ACL"):
        store.save(None)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction admission")
def test_windows_authority_directory_junction_is_rejected(retained):
    make, _, _, _, _ = retained
    store = make().access_store
    store.save({"network": True})
    directory = store._path(None).parent
    original = directory.with_name("original")
    directory.rename(original)
    subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(directory), str(original)],
        check=True,
        capture_output=True,
    )
    with pytest.raises(ConfigError, match="junctions"):
        store.load()
    with pytest.raises(ConfigError, match="junctions"):
        store.save(None)


@pytest.mark.skipif(os.name != "nt", reason="Windows authority handles and ACLs")
def test_windows_authority_parent_cannot_be_retargeted_during_read(retained, monkeypatch):
    from alysis_code import _windows_session_files as native

    make, _, _, _, _ = retained
    store = make().access_store
    grant = {"network": True}
    store.save(grant)
    path = store._path(None)
    real_open = native.open_session_leaf
    observed = []

    def open_while_renaming(directory, name, **kwargs):
        with pytest.raises(OSError):
            path.parent.rename(path.parent.with_name("redirected"))
        observed.append(True)
        return real_open(directory, name, **kwargs)

    monkeypatch.setattr(native, "open_session_leaf", open_while_renaming)
    assert store.load() == grant
    assert observed


@pytest.mark.parametrize("name", ["CON", "prn", "AUX", "nul", "COM1", "LPT9"])
def test_permission_profiles_reject_windows_device_names(name):
    with pytest.raises(ConfigError):
        SandboxAccessStore.profile_name(name)


def test_transcripts_or_other_workspaces_cannot_mint_grants(retained, tmp_path):
    make, _, _, _, _ = retained
    manager = make()
    request(manager, {"network": True, "scope": "session"})
    other = tmp_path / "other"
    other.mkdir()
    store = SandboxAccessStore(root=other, session_id="one", settings=manager.settings)
    assert store.load() is None
    path = manager.access_store._path(None)
    data = json.loads(path.read_text())
    data["binding"]["inode"] += 1
    path.write_text(json.dumps(data))
    assert manager.access_store.load() is None


@pytest.mark.parametrize("profile", [None, "network"])
def test_reuse_rechecks_revocation_while_standing_rule_is_reviewed(retained, profile):
    make, _, _, _, _ = retained
    manager = make()
    proposal = {"network": True, "scope": "session"}
    if profile:
        proposal["save_as"] = profile
    request(manager, proposal)
    manager.validate_reuse = lambda _: make().manage(
        {"action": "revoke", **({"profile": profile} if profile else {})}
    )
    with pytest.raises(ConfigError, match="changed during approval"):
        request(manager, {"profile": profile} if profile else None)


def test_profile_is_not_applied_after_the_user_turn_changes_during_review(retained):
    make, _, _, _, _ = retained
    owner, reader = make(), make("reader")
    request(owner, {"network": True, "save_as": "net"})

    def next_turn(_):
        reader.events_since = lambda cursor: ([{"type": "user_message"}], cursor + 1)

    reader.validate_reuse = next_turn
    with pytest.raises(ConfigError, match="changed during approval"):
        request(reader, {"profile": "net", "scope": "session"})
    assert reader.access_store.load() is None


@pytest.mark.parametrize("scope", ["turn", "session"])
@pytest.mark.parametrize("extend", [False, True])
def test_recreating_revoked_profile_does_not_resurrect_old_consumers(retained, scope, extend):
    make, _, _, _, _ = retained
    owner, reader = make(), make("reader")
    request(owner, {"network": True, "save_as": "net"})
    request(reader, {"profile": "net", "scope": scope})
    if extend:
        request(reader, {"local_preview": True, "scope": scope})
    owner.manage({"action": "revoke", "profile": "net"})
    request(owner, {"network": True, "save_as": "net"})
    if scope == "session":
        reader = make("reader")
    assert request(reader).settings == reader.settings
    assert request(reader, {"profile": "net"}).settings.network == "on"
