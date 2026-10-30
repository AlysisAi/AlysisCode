//! Disposable filesystem + in-memory SQL tests only. No installed accounts,
//! credentials, setup keys, shared journal or helper execution is accessed.
use super::*;
use crate::account_install::{AccountMode, synthetic_ready};
use rusqlite::Connection;
use std::{
    path::PathBuf,
    sync::atomic::{AtomicUsize, Ordering},
};

struct Fixture {
    root: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let root = crate::util::test_temp_dir().join(format!(
            "alysis-object-grants-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&root).unwrap();
        let owner = crate::sid::current_user_sid().unwrap();
        let acl = build_allow_dacl(&[
            Allow(&owner, Mask::FILE_ALL, OICI),
            Allow(SID_SYSTEM, Mask::FILE_ALL, OICI),
        ])
        .unwrap();
        write_file_dacl(root.to_str().unwrap(), acl.as_ptr(), Protection::Protected).unwrap();
        Self { root }
    }
    fn text(&self) -> &str {
        self.root.to_str().unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.root).unwrap();
    }
}

fn identity() -> CandidateJournalIdentity {
    let binding = synthetic_ready().binding(AccountMode::Offline).unwrap();
    CandidateJournalIdentity::new(
        binding,
        "Alysis.Execution.abcdef0123456789abcdef0123456789".into(),
    )
    .unwrap()
}

fn database(identity: &CandidateJournalIdentity) -> Connection {
    let mut connection = Connection::open_in_memory().unwrap();
    crate::account_state::initialize_header(&mut connection, &identity.binding).unwrap();
    connection
}

fn recorded_objects(connection: &Connection) -> Vec<String> {
    let mut statement = connection
        .prepare("SELECT record_json FROM candidate_read_objects ORDER BY ordinal")
        .unwrap();
    statement
        .query_map([], |row| row.get(0))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap()
}

fn observation_handle(path: &Path) -> OwnedHandle {
    use windows::Win32::Storage::FileSystem::FILE_SHARE_DELETE;
    OwnedHandle(
        unsafe {
            CreateFileW(
                pcwstr(&wstr(&strict_path(path.to_str().unwrap()).unwrap())),
                Mask::READ_CONTROL.bits(),
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
        }
        .unwrap(),
    )
}

// Independent oracle: native query plus Windows descriptor accessors, rather
// than Snapshot::read's bounded self-relative parsing. No mutation authority.
fn raw_observation(handle: HANDLE) -> (Vec<usize>, usize) {
    use windows::Wdk::Storage::FileSystem::NtQuerySecurityObject;
    let mut words = vec![0usize; 128 * 1024 / size_of::<usize>()];
    let mut returned = 0;
    unsafe {
        NtQuerySecurityObject(
            handle,
            (DACL_SECURITY_INFORMATION | OWNER_SECURITY_INFORMATION).0,
            Some(PSECURITY_DESCRIPTOR(words.as_mut_ptr().cast())),
            std::mem::size_of_val(words.as_slice()) as u32,
            &mut returned,
        )
    }
    .ok()
    .unwrap();
    assert!((20..=std::mem::size_of_val(words.as_slice())).contains(&(returned as usize)));
    (words, returned as usize)
}

fn observe_descriptor(descriptor: PSECURITY_DESCRIPTOR) -> Snapshot {
    use windows::Win32::Security::{GetSecurityDescriptorDacl, GetSecurityDescriptorOwner};
    let mut owner = PSID::default();
    let mut defaulted = false.into();
    let mut present = false.into();
    let mut dacl = std::ptr::null_mut();
    let mut control = 0;
    let mut revision = 0;
    unsafe {
        GetSecurityDescriptorOwner(descriptor, &mut owner, &mut defaulted).unwrap();
        GetSecurityDescriptorDacl(descriptor, &mut present, &mut dacl, &mut defaulted).unwrap();
        GetSecurityDescriptorControl(descriptor, &mut control, &mut revision).unwrap();
    }
    assert!(!owner.0.is_null() && present.as_bool() && !dacl.is_null());
    let mut aces = Vec::new();
    let kept = filter_aces(dacl, |_, bytes| {
        aces.push(bytes.to_vec());
        true
    })
    .unwrap();
    Snapshot {
        revision: kept.2.0,
        control: control & DACL_CONTROL,
        owner: crate::sid::psid_to_string(owner).unwrap(),
        aces,
    }
}

fn snapshot(path: &Path) -> Snapshot {
    let handle = observation_handle(path);
    let (words, _) = raw_observation(handle.raw());
    observe_descriptor(PSECURITY_DESCRIPTOR(words.as_ptr().cast_mut().cast()))
}

fn reconstructed_snapshot(path: &Path) -> Snapshot {
    let handle = observation_handle(path);
    let mut descriptor = PSECURITY_DESCRIPTOR::default();
    win32_ok(
        unsafe {
            GetSecurityInfo(
                handle.raw(),
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION | OWNER_SECURITY_INFORMATION,
                None,
                None,
                None,
                None,
                Some(&mut descriptor),
            )
        },
        "observe reconstructed test descriptor",
    )
    .unwrap();
    let descriptor = OwnedSd::from_raw(descriptor);
    observe_descriptor(descriptor.ptr)
}

fn raw_preservation_fixture(mode: &str) -> (Fixture, PathBuf) {
    use windows::Win32::Security::SetFileSecurityW;
    let fixture = Fixture::new();
    let owner = crate::sid::current_user_sid().unwrap();
    // Distinct masks for one unrelated trustee expose legacy reconstruction.
    let allows = format!(
        "(A;OICI;FA;;;{owner})(A;OICI;FA;;;SY)(A;OICI;0x1301bf;;;BU)(A;OICI;0x1200a9;;;BU)"
    );
    set_path_dacl_from_sddl(
        fixture.text(),
        &format!("D:P{allows}"),
        "raw candidate fixture",
    )
    .unwrap();
    let parent = fixture.root.join("parent");
    std::fs::create_dir(&parent).unwrap();
    let control = match mode {
        "legacy" => "",
        "automatic" => "ARAI",
        "protected" => "PARAI",
        _ => panic!("unknown raw candidate fixture mode"),
    };
    let descriptor = OwnedSd::from_sddl(&format!("D:{control}{allows}")).unwrap();
    unsafe {
        SetFileSecurityW(
            pcwstr(&wstr(&strict_path(parent.to_str().unwrap()).unwrap())),
            DACL_SECURITY_INFORMATION,
            descriptor.ptr,
        )
    }
    .unwrap();
    assert_eq!(
        snapshot(&parent).control,
        match mode {
            "legacy" => 4,
            "automatic" => 0x404,
            _ => 0x1404,
        }
    );
    (fixture, parent)
}

fn record(target: &Pinned, identity: &CandidateJournalIdentity) -> Record {
    let before = Snapshot::read(target.handle.raw()).unwrap();
    let added = additions(&before, identity).unwrap();
    Record {
        version: RECORD_VERSION,
        path: target.path.clone(),
        file_id: target.id.as_bytes(),
        directory: target.directory,
        before,
        added,
    }
}

#[test]
fn candidate_object_raw_legacy_automatic_and_protected_acl_round_trip() {
    for mode in ["legacy", "automatic", "protected"] {
        let (_fixture, parent) = raw_preservation_fixture(mode);
        let before = snapshot(&parent);
        if mode == "legacy" {
            assert_ne!(
                before.aces,
                reconstructed_snapshot(&parent).aces,
                "fixture must expose the reconstructed Win32 view"
            );
        }
        let child = parent.join("child");
        let grandchild = child.join("existing.txt");
        std::fs::create_dir(&child).unwrap();
        std::fs::write(&grandchild, b"raw preservation").unwrap();
        let child_before = snapshot(&child);
        let grandchild_before = snapshot(&grandchild);
        let target = Pinned::open(parent.to_str().unwrap(), None, true).unwrap();
        let row = record(&target, &identity());
        assert_eq!(row.version, 2);
        assert_eq!(row.before, before, "{mode}: journal must use the raw view");
        target.write(&row.before, &row.after(), true).unwrap();
        assert_eq!(snapshot(&parent), row.after());
        assert_eq!(snapshot(&child), child_before);
        assert_eq!(snapshot(&grandchild), grandchild_before);
        let later = parent.join("later");
        std::fs::create_dir(&later).unwrap();
        assert_eq!(
            snapshot(&later),
            child_before,
            "{mode}: no grant inheritance"
        );
        target.write(&row.after(), &row.before, false).unwrap();
        assert_eq!(snapshot(&parent), before, "{mode}: exact raw restoration");
        assert_eq!(snapshot(&child), child_before);
        assert_eq!(snapshot(&grandchild), grandchild_before);
        assert_eq!(snapshot(&later), child_before);
    }
}

#[test]
fn candidate_object_setter_preserves_inheritance_and_never_propagates() {
    let fixture = Fixture::new();
    let parent = fixture.root.join("parent");
    let child = parent.join("child");
    let grandchild = child.join("existing.txt");
    std::fs::create_dir_all(&child).unwrap();
    std::fs::write(&grandchild, b"existing").unwrap();
    let child_before = snapshot(&child);
    let grandchild_before = snapshot(&grandchild);
    let target = Pinned::open(parent.to_str().unwrap(), None, true).unwrap();
    let record = record(&target, &identity());
    assert_ne!(
        record.before.control & 0x400,
        0,
        "fixture must exercise AUTO_INHERITED"
    );
    assert!(record.before.aces.iter().any(|ace| ace[1] & 0x10 != 0));
    target.write(&record.before, &record.after(), true).unwrap();
    assert_eq!(Snapshot::read(target.handle.raw()).unwrap(), record.after());
    assert_eq!(snapshot(&child), child_before);
    assert_eq!(snapshot(&grandchild), grandchild_before);
    let later = parent.join("later");
    std::fs::create_dir(&later).unwrap();
    assert_eq!(
        snapshot(&later),
        child_before,
        "new child inherits no added grant"
    );
    target
        .write(&record.after(), &record.before, false)
        .unwrap();
    assert_eq!(Snapshot::read(target.handle.raw()).unwrap(), record.before);
    assert_eq!(snapshot(&grandchild), grandchild_before);
}

#[test]
fn candidate_object_tree_journals_every_identity_and_excludes_later_descendants() {
    let fixture = Fixture::new();
    let directory = fixture.root.join("directory");
    let leaf = directory.join("leaf.txt");
    std::fs::create_dir(&directory).unwrap();
    std::fs::write(&leaf, b"unchanged").unwrap();
    let paths = [&fixture.root, &directory, &leaf];
    let before: Vec<_> = paths.iter().map(|path| snapshot(path)).collect();
    let identity = identity();
    let tree = PreparedReadTree::prepare(fixture.text(), &identity).unwrap();
    assert_eq!(tree.object_count(), 3);
    let late = directory.join("after-inventory.txt");
    std::fs::write(&late, b"not granted").unwrap();
    let late_before = snapshot(&late);
    let connection = database(&identity);
    let journal = ReadTreeJournal::fixture(&connection, &identity).unwrap();
    tree.grant(&journal).unwrap();
    assert_eq!(
        connection
            .query_row("SELECT count(*) FROM candidate_read_objects", [], |r| r
                .get::<_, i64>(0))
            .unwrap(),
        3
    );
    assert_eq!(snapshot(&late), late_before);
    for (pin, row) in &tree.objects {
        assert_eq!(Snapshot::read(pin.handle.raw()).unwrap(), row.after());
    }
    assert!(std::fs::rename(&directory, fixture.root.join("moved")).is_err());
    drop(tree); // Release the original inventory before independent recovery.
    journal.recover().unwrap();
    journal.recover().unwrap();
    for (path, before) in paths.iter().zip(before) {
        assert_eq!(snapshot(path), before);
    }
    assert_eq!(snapshot(&late), late_before);
    assert_eq!(std::fs::read(&leaf).unwrap(), b"unchanged");
}

#[test]
fn candidate_object_write_ahead_failure_changes_no_acl() {
    let fixture = Fixture::new();
    std::fs::write(fixture.root.join("leaf.txt"), b"unchanged").unwrap();
    let identity = identity();
    let tree = PreparedReadTree::prepare(fixture.text(), &identity).unwrap();
    let connection = database(&identity);
    let journal = ReadTreeJournal::fixture(&connection, &identity).unwrap();
    connection
        .execute_batch(
            "CREATE TRIGGER fail_object_intent BEFORE INSERT ON candidate_read_objects
        BEGIN SELECT RAISE(ABORT, 'synthetic intent failure'); END;",
        )
        .unwrap();
    assert!(
        tree.grant(&journal)
            .unwrap_err()
            .to_string()
            .contains("synthetic intent failure")
    );
    for (target, row) in &tree.objects {
        assert_eq!(Snapshot::read(target.handle.raw()).unwrap(), row.before);
    }
    assert_eq!(
        connection
            .query_row("SELECT count(*) FROM candidate_read_runs", [], |r| r
                .get::<_, i64>(0))
            .unwrap(),
        0
    );
}

#[test]
fn candidate_object_recovery_handles_interrupted_apply_and_moved_descendant() {
    let fixture = Fixture::new();
    let tree_path = fixture.root.join("tree");
    std::fs::create_dir(&tree_path).unwrap();
    let original = tree_path.join("leaf.txt");
    std::fs::write(&original, b"original").unwrap();
    let identity = identity();
    let tree = PreparedReadTree::prepare(tree_path.to_str().unwrap(), &identity).unwrap();
    let connection = database(&identity);
    let journal = ReadTreeJournal::fixture(&connection, &identity).unwrap();
    journal.record(&tree.objects).unwrap();
    // Crash after one non-root mutation, with all intentions already durable.
    let (pin, row) = tree.objects.iter().find(|(_, r)| !r.directory).unwrap();
    let before = row.before.clone();
    pin.write(&row.before, &row.after(), true).unwrap();
    drop(tree);
    let moved = fixture.root.join("moved.txt");
    std::fs::rename(&original, &moved).unwrap();
    std::fs::write(&original, b"replacement").unwrap();
    let replacement_before = snapshot(&original);
    journal.recover().unwrap();
    assert_eq!(snapshot(&moved), before);
    assert_eq!(snapshot(&original), replacement_before);
    assert_eq!(std::fs::read(&moved).unwrap(), b"original");
}

#[test]
fn candidate_object_hardlinks_refused_before_grant_but_revoked_from_exact_object() {
    let fixture = Fixture::new();
    let original = fixture.root.join("original.txt");
    let alias = fixture.root.join("alias.txt");
    std::fs::write(&original, b"same object").unwrap();
    let before = snapshot(&original);
    std::fs::hard_link(&original, &alias).unwrap();
    assert!(
        PreparedReadTree::prepare(original.to_str().unwrap(), &identity())
            .err()
            .unwrap()
            .to_string()
            .contains("multiply linked")
    );
    assert_eq!(snapshot(&original), before);
    std::fs::remove_file(&alias).unwrap();
    let identity = identity();
    let tree = PreparedReadTree::prepare(original.to_str().unwrap(), &identity).unwrap();
    let connection = database(&identity);
    let journal = ReadTreeJournal::fixture(&connection, &identity).unwrap();
    tree.grant(&journal).unwrap();
    // Windows allows link creation even with a no-delete-share data handle.
    std::fs::hard_link(&original, &alias).unwrap();
    drop(tree);
    journal.recover().unwrap();
    assert_eq!(snapshot(&original), before);
    assert_eq!(snapshot(&alias), before);
}

#[test]
fn candidate_object_final_sweep_refuses_late_hardlink_and_recovers_both_names() {
    let fixture = Fixture::new();
    let root = fixture.root.join("tree");
    std::fs::create_dir(&root).unwrap();
    for name in ["first.txt", "second.txt"] {
        std::fs::write(root.join(name), b"same object").unwrap();
    }
    let identity = identity();
    let tree = PreparedReadTree::prepare(root.to_str().unwrap(), &identity).unwrap();
    assert_eq!(tree.object_count(), 3);
    // Use the actual inventory order, not a filesystem enumeration assumption.
    let index = tree
        .objects
        .iter()
        .position(|(_, row)| !row.directory)
        .unwrap();
    assert!(index < tree.objects.len() - 1);
    let original = PathBuf::from(&tree.objects[index].1.path);
    let original_before = tree.objects[index].1.before.clone();
    let original_id = tree.objects[index].1.file_id;
    let before: Vec<_> = tree
        .objects
        .iter()
        .map(|(_, row)| (PathBuf::from(&row.path), row.before.clone()))
        .collect();
    let alias = fixture.root.join("outside-inventory-alias.txt");
    let connection = database(&identity);
    let journal = ReadTreeJournal::fixture(&connection, &identity).unwrap();
    let mut saved = Vec::new();
    let error = tree
        .grant_impl(&journal, || {
            for (pin, row) in &tree.objects {
                assert_eq!(snapshot(Path::new(&pin.path)), row.after());
            }
            saved = recorded_objects(&connection);
            assert_eq!(saved.len(), tree.object_count());
            // All writes have passed their per-object checks. A retained
            // no-delete-share handle still allows this new name for an earlier
            // object; only the final sweep can refuse this observed drift.
            std::fs::hard_link(&original, &alias).unwrap();
        })
        .unwrap_err();
    assert!(error.to_string().contains("multiply linked"), "{error:#}");
    assert_eq!(recorded_objects(&connection), saved);
    assert_eq!(snapshot(&alias), tree.objects[index].1.after());
    assert_eq!(
        crate::path_id::capture_file_id(alias.to_str().unwrap())
            .unwrap()
            .as_bytes(),
        original_id
    );
    drop(tree);
    // Recovery is by exact identity and permits multiple links. Do not remove
    // the alias to make recovery pass or guess a path-based compensation.
    journal.recover().unwrap();
    for (path, descriptor) in before {
        assert_eq!(snapshot(&path), descriptor);
    }
    assert_eq!(snapshot(&alias), original_before);
    assert_eq!(std::fs::read(&original).unwrap(), b"same object");
    assert_eq!(std::fs::read(&alias).unwrap(), b"same object");
    assert!(recorded_objects(&connection).is_empty());
    assert_eq!(
        connection
            .query_row("SELECT count(*) FROM candidate_read_runs", [], |row| row
                .get::<_, i64>(0))
            .unwrap(),
        0
    );
}

#[test]
fn candidate_object_descriptor_drift_keeps_recovery_records() {
    let fixture = Fixture::new();
    let identity = identity();
    let tree = PreparedReadTree::prepare(fixture.text(), &identity).unwrap();
    let connection = database(&identity);
    let journal = ReadTreeJournal::fixture(&connection, &identity).unwrap();
    tree.grant(&journal).unwrap();
    let (pin, row) = &tree.objects[0];
    let mut drifted = row.after();
    let extra = build_allow_dacl(&[Allow(
        SID_BUILTIN_USERS,
        Mask::FILE_GENERIC_READ,
        NO_INHERIT,
    )])
    .unwrap();
    filter_aces(extra.as_ptr(), |_, bytes| {
        drifted.aces.push(bytes.to_vec());
        true
    })
    .unwrap();
    pin.write(&row.after(), &drifted, false).unwrap();
    drop(tree);
    assert!(format!("{:#}", journal.recover().unwrap_err()).contains("descriptor changed"));
    assert_eq!(snapshot(&fixture.root), drifted);
    assert_eq!(
        connection
            .query_row("SELECT count(*) FROM candidate_read_objects", [], |r| r
                .get::<_, i64>(0))
            .unwrap(),
        1
    );
}

#[test]
fn candidate_object_record_policy_rejects_broad_grants_and_aliases() {
    for bad in [
        r"C:\",
        r"relative",
        r"C:\x\..\y",
        r"C:\x\file:stream",
        r"C:\x.\y",
        r"\\server\share\file",
        r"\\.\C:\x",
        r"C:\x\y ",
        r"C:\x\\y",
    ] {
        assert!(strict_path(bad).is_err(), "accepted {bad}");
    }
    let fixture = Fixture::new();
    let identity = identity();
    let tree = PreparedReadTree::prepare(fixture.text(), &identity).unwrap();
    let mut row = tree.objects[0].1.clone();
    row.added[0][4..8].copy_from_slice(&Mask::FILE_ALL.bits().to_le_bytes());
    assert!(
        row.validate(&identity)
            .unwrap_err()
            .to_string()
            .contains("exact read-only policy")
    );
}

#[test]
fn candidate_object_native_api_contrast_detects_child_propagation() {
    let fixture = Fixture::new();
    for native in [true, false] {
        let parent = fixture.root.join(if native { "native" } else { "win32" });
        std::fs::create_dir(&parent).unwrap();
        let child = parent.join("child.txt");
        std::fs::write(&child, b"synthetic API control").unwrap();
        let before_child = snapshot(&child);
        let target = Pinned::open(parent.to_str().unwrap(), None, true).unwrap();
        let row = record(&target, &identity());
        assert_ne!(row.before.control & 0x400, 0);
        let mut changed = row.after();
        for ace in &mut changed.aces {
            if row.added.contains(ace) {
                ace[1] |= (OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE).0 as u8;
            }
        }
        // Production write() refuses these entries. This deliberately wider
        // synthetic API control proves the native function is not recursively
        // copying ACEs, and that the fixture can detect the Win32 alternative.
        if native {
            set_native_dacl(target.handle.raw(), &changed).unwrap();
            assert_eq!(Snapshot::read(target.handle.raw()).unwrap(), changed);
            assert_eq!(snapshot(&child), before_child);
            set_native_dacl(target.handle.raw(), &row.before).unwrap();
        } else {
            for wanted in [&changed, &row.before] {
                let acl = wanted.acl().unwrap();
                win32_ok(
                    unsafe {
                        SetSecurityInfo(
                            target.handle.raw(),
                            SE_FILE_OBJECT,
                            DACL_SECURITY_INFORMATION,
                            None,
                            None,
                            Some(acl.as_ptr()),
                            None,
                        )
                    },
                    "synthetic propagating control",
                )
                .unwrap();
                if std::ptr::eq(wanted, &changed) {
                    assert_ne!(snapshot(&child), before_child);
                }
            }
        }
        assert_eq!(Snapshot::read(target.handle.raw()).unwrap(), row.before);
        assert_eq!(snapshot(&child), before_child);
    }
}

#[test]
fn candidate_object_cwd_sharing_allows_pinned_grant_and_recovery() {
    let fixture = Fixture::new();
    let before = snapshot(&fixture.root);
    // A cwd-like data/list handle denies DELETE sharing. Exact minimum rights
    // remain compatible; this used to fail with MAXIMUM_ALLOWED.
    let held = OwnedHandle(
        unsafe {
            CreateFileW(
                pcwstr(&wstr(fixture.text())),
                1,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS,
                None,
            )
        }
        .unwrap(),
    );
    let identity = identity();
    let tree = PreparedReadTree::prepare(fixture.text(), &identity).unwrap();
    let connection = database(&identity);
    let journal = ReadTreeJournal::fixture(&connection, &identity).unwrap();
    tree.grant(&journal).unwrap();
    assert_ne!(snapshot(&fixture.root), before);
    drop(tree);
    journal.recover().unwrap();
    assert_eq!(snapshot(&fixture.root), before);
    drop(held);
}

#[test]
fn candidate_object_setter_refuses_inheritable_ace_changes_before_mutation() {
    let fixture = Fixture::new();
    let child = fixture.root.join("child.txt");
    std::fs::write(&child, b"unchanged").unwrap();
    let child_before = snapshot(&child);
    let target = Pinned::open(fixture.text(), None, true).unwrap();
    let row = record(&target, &identity());
    let mut after = row.after();
    let inserted = after
        .aces
        .iter_mut()
        .find(|ace| row.added.contains(ace))
        .unwrap();
    inserted[1] |= (OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE).0 as u8;
    assert!(
        target
            .write(&row.before, &after, true)
            .unwrap_err()
            .to_string()
            .contains("inheritable ACEs")
    );
    assert_eq!(Snapshot::read(target.handle.raw()).unwrap(), row.before);
    assert_eq!(snapshot(&child), child_before);
}

#[test]
fn candidate_object_deleted_root_does_not_hide_moved_descendant_record() {
    let fixture = Fixture::new();
    let root = fixture.root.join("tree");
    std::fs::create_dir(&root).unwrap();
    let leaf = root.join("leaf.txt");
    std::fs::write(&leaf, b"survives deleted root").unwrap();
    let before = snapshot(&leaf);
    let identity = identity();
    let tree = PreparedReadTree::prepare(root.to_str().unwrap(), &identity).unwrap();
    let connection = database(&identity);
    let journal = ReadTreeJournal::fixture(&connection, &identity).unwrap();
    tree.grant(&journal).unwrap();
    drop(tree);
    let moved = fixture.root.join("surviving.txt");
    std::fs::rename(&leaf, &moved).unwrap();
    std::fs::remove_dir(&root).unwrap();
    let result = journal.recover();
    assert_eq!(
        snapshot(&moved),
        before,
        "surviving descendant must be cleaned independently"
    );
    let remaining: i64 = connection
        .query_row("SELECT count(*) FROM candidate_read_objects", [], |r| {
            r.get(0)
        })
        .unwrap();
    if let Err(error) = result {
        // Windows may retain the deleted directory internally or reject its ID
        // with 87. Neither observation may discard the ambiguous object's row.
        assert_eq!(remaining, 1, "{error:#}");
    } else {
        assert_eq!(remaining, 0); // Only a definitive checked-ID absence permits this.
    }
}

#[test]
fn candidate_object_reparse_descendant_is_refused_before_any_mutation() {
    use windows::Win32::{Storage::FileSystem::FILE_SHARE_DELETE, System::IO::DeviceIoControl};
    let fixture = Fixture::new();
    let tree = fixture.root.join("tree");
    let outside = fixture.root.join("outside");
    let junction = tree.join("junction");
    std::fs::create_dir_all(&junction).unwrap();
    std::fs::create_dir(&outside).unwrap();
    std::fs::write(outside.join("private.txt"), b"unchanged").unwrap();
    let handle = OwnedHandle(
        unsafe {
            CreateFileW(
                pcwstr(&wstr(junction.to_str().unwrap())),
                0x40000000,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
        }
        .unwrap(),
    );
    let substitute = wstr(&format!(r"\??\{}", outside.display()));
    let print = wstr(outside.to_str().unwrap());
    let mut buffer = Vec::new();
    buffer.extend_from_slice(&0xa0000003u32.to_le_bytes());
    buffer.extend_from_slice(&((8 + 2 * (substitute.len() + print.len())) as u16).to_le_bytes());
    buffer.extend_from_slice(&0u16.to_le_bytes());
    for value in [
        0,
        ((substitute.len() - 1) * 2) as u16,
        (substitute.len() * 2) as u16,
        ((print.len() - 1) * 2) as u16,
    ] {
        buffer.extend_from_slice(&value.to_le_bytes());
    }
    for value in substitute.into_iter().chain(print) {
        buffer.extend_from_slice(&value.to_le_bytes());
    }
    let mut returned = 0;
    unsafe {
        DeviceIoControl(
            handle.raw(),
            0x000900a4,
            Some(buffer.as_ptr().cast()),
            buffer.len() as u32,
            None,
            0,
            Some(&mut returned),
            None,
        )
    }
    .unwrap();
    drop(handle);
    let before = snapshot(&outside);
    let error = PreparedReadTree::prepare(tree.to_str().unwrap(), &identity())
        .err()
        .unwrap();
    assert!(error.to_string().contains("reparse point"), "{error:#}");
    assert_eq!(snapshot(&outside), before);
    assert_eq!(
        std::fs::read(outside.join("private.txt")).unwrap(),
        b"unchanged"
    );
    std::fs::remove_dir(&junction).unwrap();
}

#[test]
fn candidate_object_inherited_deny_is_not_overridden_by_explicit_allow() {
    let fixture = Fixture::new();
    let owner = crate::sid::current_user_sid().unwrap();
    let guest = LocalPsid::from_string("S-1-5-32-546").unwrap();
    let allow = build_allow_dacl(&[Allow(&owner, Mask::FILE_ALL, OICI)]).unwrap();
    let kept = filter_aces(allow.as_ptr(), |_, _| true).unwrap();
    let acl = rebuild_acl(
        ACL_REVISION,
        &[NewAce::Deny(
            guest.as_psid(),
            Mask::FILE_READ_EXEC.bits(),
            OICI,
        )],
        &kept,
        &[],
    )
    .unwrap();
    write_file_dacl(fixture.text(), acl.as_ptr(), Protection::Protected).unwrap();
    let child = fixture.root.join("child");
    std::fs::create_dir(&child).unwrap();
    let before = snapshot(&child);
    assert!(
        before
            .aces
            .iter()
            .any(|ace| ace[0] == 1 && ace[1] & INHERITED_ACE != 0)
    );
    let error = PreparedReadTree::prepare(child.to_str().unwrap(), &identity())
        .err()
        .unwrap();
    assert!(error.to_string().contains("inherited deny"));
    assert_eq!(snapshot(&child), before);
}

#[test]
fn candidate_object_disjoint_roots_share_intent_and_overlaps_are_refused() {
    let fixture = Fixture::new();
    let left = fixture.root.join("left.txt");
    let right = fixture.root.join("right.txt");
    std::fs::write(&left, b"left").unwrap();
    std::fs::write(&right, b"right").unwrap();
    let identity = identity();
    assert!(
        PreparedReadTree::prepare_roots(&[fixture.text(), left.to_str().unwrap()], &identity)
            .err()
            .unwrap()
            .to_string()
            .contains("overlapping")
    );
    let before = (snapshot(&left), snapshot(&right));
    let tree = PreparedReadTree::prepare_roots(
        &[left.to_str().unwrap(), right.to_str().unwrap()],
        &identity,
    )
    .unwrap();
    let connection = database(&identity);
    let journal = ReadTreeJournal::fixture(&connection, &identity).unwrap();
    tree.grant(&journal).unwrap();
    assert_eq!(tree.object_count(), 2);
    drop(tree);
    journal.recover().unwrap();
    assert_eq!((snapshot(&left), snapshot(&right)), before);
}

#[test]
fn candidate_object_raw_descriptor_bounds_refuse_malformed_owner_and_acl() {
    let fixture = Fixture::new();
    let handle = observation_handle(&fixture.root);
    let (original, length) = raw_observation(handle.raw());
    let mut shifted = vec![0usize; original.len() + 1];
    let shifted_bytes =
        unsafe { std::slice::from_raw_parts_mut(shifted.as_mut_ptr().cast::<u8>(), length + 1) };
    let original_bytes =
        unsafe { std::slice::from_raw_parts(original.as_ptr().cast::<u8>(), length) };
    shifted_bytes[1..].copy_from_slice(original_bytes);
    assert!(
        Snapshot::from_raw_descriptor(&shifted_bytes[1..])
            .unwrap_err()
            .to_string()
            .contains("DWORD aligned")
    );
    for malformed in [
        "short",
        "absolute",
        "owner_null",
        "owner_header",
        "owner_end",
        "owner_revision",
        "owner_count",
        "acl_null",
        "acl_end",
        "acl_size",
        "ace_count",
        "ace_size",
    ] {
        let mut words = original.clone();
        let bytes =
            unsafe { std::slice::from_raw_parts_mut(words.as_mut_ptr().cast::<u8>(), length) };
        let owner = u32::from_le_bytes(bytes[4..8].try_into().unwrap()) as usize;
        let acl = u32::from_le_bytes(bytes[16..20].try_into().unwrap()) as usize;
        match malformed {
            "short" => {
                assert!(Snapshot::from_raw_descriptor(&bytes[..19]).is_err());
                continue;
            }
            "absolute" => bytes[3] &= 0x7f,
            "owner_null" => bytes[4..8].copy_from_slice(&0u32.to_le_bytes()),
            "owner_header" => bytes[4..8].copy_from_slice(&4u32.to_le_bytes()),
            "owner_end" => bytes[4..8].copy_from_slice(&(length as u32).to_le_bytes()),
            "owner_revision" => bytes[owner] = 0,
            "owner_count" => bytes[owner + 1] = 16,
            "acl_null" => bytes[16..20].copy_from_slice(&0u32.to_le_bytes()),
            "acl_end" => bytes[16..20].copy_from_slice(&(length as u32).to_le_bytes()),
            "acl_size" => bytes[acl + 2..acl + 4].copy_from_slice(&u16::MAX.to_le_bytes()),
            "ace_count" => bytes[acl + 4..acl + 6].copy_from_slice(&u16::MAX.to_le_bytes()),
            "ace_size" => bytes[acl + 10..acl + 12].copy_from_slice(&u16::MAX.to_le_bytes()),
            _ => unreachable!(),
        }
        assert!(
            Snapshot::from_raw_descriptor(bytes).is_err(),
            "accepted {malformed}"
        );
    }
    assert_eq!(
        snapshot(&fixture.root),
        observe_descriptor(PSECURITY_DESCRIPTOR(original.as_ptr().cast_mut().cast()))
    );
}

#[test]
fn candidate_object_raw_only_drift_retains_exact_acl_and_journal() {
    let (_fixture, parent) = raw_preservation_fixture("legacy");
    let identity = identity();
    let tree = PreparedReadTree::prepare(parent.to_str().unwrap(), &identity).unwrap();
    assert_eq!(tree.object_count(), 1);
    let connection = database(&identity);
    let journal = ReadTreeJournal::fixture(&connection, &identity).unwrap();
    tree.grant(&journal).unwrap();
    let (pin, row) = &tree.objects[0];
    let raw = snapshot(&parent);
    let view = reconstructed_snapshot(&parent);
    assert_eq!(raw, row.after());
    assert_ne!(raw.aces, view.aces, "fixture must expose masked raw drift");
    let mut drifted = raw.clone();
    drifted.aces = view.aces.clone();
    // Deliberate trusted-host drift, using only this owned synthetic object.
    set_native_dacl(pin.handle.raw(), &drifted).unwrap();
    assert_eq!(snapshot(&parent), drifted);
    assert_eq!(
        reconstructed_snapshot(&parent),
        view,
        "Win32 reconstruction must conceal this raw-only drift"
    );
    let saved: String = connection
        .query_row("SELECT record_json FROM candidate_read_objects", [], |r| {
            r.get(0)
        })
        .unwrap();
    drop(tree);
    assert!(format!("{:#}", journal.recover().unwrap_err()).contains("descriptor changed"));
    assert_eq!(snapshot(&parent), drifted);
    assert_eq!(
        connection
            .query_row("SELECT record_json FROM candidate_read_objects", [], |r| {
                r.get::<_, String>(0)
            },)
            .unwrap(),
        saved
    );
    assert_eq!(
        connection
            .query_row("SELECT count(*) FROM candidate_read_runs", [], |r| r
                .get::<_, i64>(0),)
            .unwrap(),
        1
    );
}

#[test]
fn candidate_object_final_sweep_refuses_raw_only_drift_without_compensation() {
    let (_fixture, parent) = raw_preservation_fixture("legacy");
    let child = parent.join("child.txt");
    std::fs::write(&child, b"unchanged").unwrap();
    let child_before = snapshot(&child);
    let identity = identity();
    let tree = PreparedReadTree::prepare(parent.to_str().unwrap(), &identity).unwrap();
    assert_eq!(tree.object_count(), 2);
    assert_eq!(
        tree.objects[0].1.path,
        strict_path(parent.to_str().unwrap()).unwrap()
    );
    let connection = database(&identity);
    let journal = ReadTreeJournal::fixture(&connection, &identity).unwrap();
    let mut saved = Vec::new();
    let mut drifted = None;
    let error = tree
        .grant_impl(&journal, || {
            for (pin, row) in &tree.objects {
                assert_eq!(snapshot(Path::new(&pin.path)), row.after());
            }
            saved = recorded_objects(&connection);
            let (pin, row) = &tree.objects[0];
            let raw = snapshot(&parent);
            let view = reconstructed_snapshot(&parent);
            assert_eq!(raw, row.after());
            assert_ne!(raw.aces, view.aces, "fixture must expose masked raw drift");
            let mut changed = raw;
            changed.aces = view.aces.clone();
            // Use the independent raw oracle: the reconstructed Win32 view
            // remains equal even though the descriptor's actual ACEs change.
            set_native_dacl(pin.handle.raw(), &changed).unwrap();
            assert_eq!(snapshot(&parent), changed);
            assert_eq!(reconstructed_snapshot(&parent), view);
            drifted = Some(changed);
        })
        .unwrap_err();
    assert!(
        error.to_string().contains("descriptor changed after grants"),
        "{error:#}"
    );
    let drifted = drifted.unwrap();
    assert_eq!(saved.len(), tree.object_count());
    assert_eq!(recorded_objects(&connection), saved);
    assert_eq!(snapshot(&parent), drifted);
    assert_eq!(snapshot(&child), tree.objects[1].1.after());
    drop(tree);
    // Independent recovery can retire the unchanged child's exact row, but
    // must keep the drifted root descriptor and its original durable intent.
    assert!(format!("{:#}", journal.recover().unwrap_err()).contains("descriptor changed"));
    assert_eq!(snapshot(&parent), drifted);
    assert_eq!(snapshot(&child), child_before);
    assert_eq!(std::fs::read(&child).unwrap(), b"unchanged");
    assert_eq!(recorded_objects(&connection), vec![saved[0].clone()]);
    assert_eq!(
        connection
            .query_row("SELECT count(*) FROM candidate_read_runs", [], |row| row
                .get::<_, i64>(0))
            .unwrap(),
        1
    );
}

#[test]
fn candidate_object_v1_reconstruction_is_refused_before_any_recovery_write() {
    let (_fixture, parent) = raw_preservation_fixture("legacy");
    let first = parent.join("first.txt");
    let second = parent.join("second.txt");
    std::fs::write(&first, b"first").unwrap();
    std::fs::write(&second, b"second").unwrap();
    let identity = identity();
    let old_view = reconstructed_snapshot(&parent);
    assert_ne!(old_view.aces, snapshot(&parent).aces);
    let tree = PreparedReadTree::prepare(parent.to_str().unwrap(), &identity).unwrap();
    assert_eq!(tree.object_count(), 3);
    let connection = database(&identity);
    let journal = ReadTreeJournal::fixture(&connection, &identity).unwrap();
    tree.grant(&journal).unwrap();
    let (pin, current) = &tree.objects[0];
    assert_eq!(
        Path::new(&current.path),
        Path::new(&strict_path(parent.to_str().unwrap()).unwrap())
    );
    let mut legacy = current.clone();
    legacy.version = 1;
    legacy.before = old_view;
    legacy.added = additions(&legacy.before, &identity).unwrap();
    // Reproduce the old setter's reconstructed after-state. Even when it
    // matches the stored v1 after-state, original raw before-state is unknown.
    set_native_dacl(pin.handle.raw(), &legacy.after()).unwrap();
    assert_eq!(snapshot(&parent), legacy.after());
    let legacy_json = serde_json::to_string(&legacy).unwrap();
    connection
        .execute(
            "UPDATE candidate_read_objects SET record_json = ?1 WHERE ordinal = 0",
            [&legacy_json],
        )
        .unwrap();
    let paths = [&parent, &first, &second];
    let before: Vec<_> = paths.iter().map(|path| snapshot(path)).collect();
    let saved_rows: Vec<(i64, String)> = connection
        .prepare("SELECT ordinal, record_json FROM candidate_read_objects ORDER BY ordinal")
        .unwrap()
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap();
    drop(tree);
    // Recovery orders rows descending: valid rows 2 and 1 must NOT be cleaned
    // before discovering the v1 row 0. All records are validated first.
    let error = format!("{:#}", journal.recover().unwrap_err());
    assert!(error.contains("unsupported object grant record"), "{error}");
    for (path, expected) in paths.iter().zip(&before) {
        assert_eq!(snapshot(path), *expected);
    }
    let retained_rows: Vec<(i64, String)> = connection
        .prepare("SELECT ordinal, record_json FROM candidate_read_objects ORDER BY ordinal")
        .unwrap()
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap();
    assert_eq!(retained_rows, saved_rows);
    assert_eq!(
        connection
            .query_row("SELECT count(*) FROM candidate_read_runs", [], |r| r
                .get::<_, i64>(0),)
            .unwrap(),
        1
    );
}

#[test]
fn candidate_object_critical_only_ace_is_immutable_and_round_trips_raw() {
    let fixture = Fixture::new();
    let owner = crate::sid::current_user_sid().unwrap();
    set_path_dacl_from_sddl(
        fixture.text(),
        &format!("D:P(A;OICI;FA;;;{owner})(A;OICI;FA;;;SY)(A;CR;FR;;;BU)"),
        "candidate critical-only fixture",
    )
    .unwrap();
    let before = snapshot(&fixture.root);
    assert_eq!(before.aces.iter().filter(|ace| ace[1] == 0x20).count(), 1);
    let target = Pinned::open(fixture.text(), None, true).unwrap();
    let row = record(&target, &identity());
    let mut removed = row.after();
    removed
        .aces
        .retain(|ace| ace[1] & CRITICAL_ACE_FLAG as u8 == 0);
    assert!(
        target
            .write(&row.before, &removed, true)
            .unwrap_err()
            .to_string()
            .contains("critical ACEs")
    );
    assert_eq!(snapshot(&fixture.root), before);
    target.write(&row.before, &row.after(), true).unwrap();
    assert_eq!(snapshot(&fixture.root), row.after());
    target.write(&row.after(), &row.before, false).unwrap();
    assert_eq!(snapshot(&fixture.root), before);
}

#[test]
fn candidate_object_raw_journal_cold_recovery_restores_all_control_modes() {
    for mode in ["legacy", "automatic", "protected"] {
        let (fixture, parent) = raw_preservation_fixture(mode);
        let child = parent.join("child");
        let leaf = child.join("leaf.txt");
        std::fs::create_dir(&child).unwrap();
        std::fs::write(&leaf, b"cold raw recovery").unwrap();
        let paths = [&parent, &child, &leaf];
        let before: Vec<_> = paths
            .iter()
            .map(|path| {
                (
                    snapshot(path),
                    crate::path_id::capture_file_id(path.to_str().unwrap()).unwrap(),
                )
            })
            .collect();
        let identity = identity();
        // An actual durable SQLite journal under a synthetic owned fixture.
        // Production PrivateDatabase/typed-authority admission is qualified by
        // the separate execution disk tests, not bypassed by a production API.
        let database_path =
            strict_path(fixture.root.join("raw-journal.db").to_str().unwrap()).unwrap();
        {
            let mut connection = Connection::open_with_flags_and_vfs(
                &database_path,
                rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE
                    | rusqlite::OpenFlags::SQLITE_OPEN_CREATE,
                "win32-longpath",
            )
            .unwrap();
            connection
                .execute_batch("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL;")
                .unwrap();
            crate::account_state::initialize_header(&mut connection, &identity.binding).unwrap();
            let tree = PreparedReadTree::prepare(parent.to_str().unwrap(), &identity).unwrap();
            let journal = ReadTreeJournal::fixture(&connection, &identity).unwrap();
            tree.grant(&journal).unwrap();
            assert_eq!(
                connection
                    .query_row("SELECT count(*) FROM candidate_read_objects", [], |r| r
                        .get::<_, i64>(0),)
                    .unwrap(),
                3
            );
            for (pin, row) in &tree.objects {
                assert_eq!(snapshot(Path::new(&pin.path)), row.after());
                assert_eq!(
                    row.before,
                    before
                        .iter()
                        .find(|(_, id)| id.as_bytes() == row.file_id)
                        .unwrap()
                        .0
                );
            }
            drop(tree);
            drop(journal);
            // The connection also closes here, before independent recovery.
        }
        {
            let connection = Connection::open_with_flags_and_vfs(
                &database_path,
                rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE,
                "win32-longpath",
            )
            .unwrap();
            let journal = ReadTreeJournal::fixture(&connection, &identity).unwrap();
            journal.recover().unwrap();
            journal.recover().unwrap();
            for (path, (expected, id)) in paths.iter().zip(&before) {
                assert_eq!(snapshot(path), *expected, "{mode}: raw restoration");
                assert_eq!(
                    crate::path_id::capture_file_id(path.to_str().unwrap()).unwrap(),
                    *id
                );
            }
            assert_eq!(std::fs::read(&leaf).unwrap(), b"cold raw recovery");
            for table in ["candidate_read_objects", "candidate_read_runs"] {
                assert_eq!(
                    connection
                        .query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r
                            .get::<_, i64>(0),)
                        .unwrap(),
                    0
                );
            }
        }
    }
}
