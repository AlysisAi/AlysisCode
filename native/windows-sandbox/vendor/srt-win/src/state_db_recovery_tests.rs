//! Real disposable filesystem fixtures with an in-memory ACL ledger. These do
//! not open the installed state database or use a sandbox account/runner.
use super::tests::with_mem_db;
use super::*;

const FIXTURE_SID: &str = "S-1-5-21-1-2-3-987654";
struct Fixture {
    root: PathBuf,
    base: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let engine = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .ancestors()
            .nth(4)
            .unwrap()
            .canonicalize()
            .unwrap();
        let mut base = engine.clone();
        for component in [".qa-results", "0142-integration", "recovery-fixtures"] {
            base.push(component);
            match std::fs::create_dir(&base) {
                Ok(()) => {}
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
                Err(error) => panic!("create recovery fixture parent: {error}"),
            }
            base = base.canonicalize().unwrap();
            assert!(
                base.starts_with(&engine),
                "fixture ancestry must remain within engine checkout"
            );
        }
        let root = base.join(format!(
            "{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&root).unwrap();
        let owner = crate::sid::current_user_sid().unwrap();
        acl::set_path_dacl_from_sddl(
            root.to_str().unwrap(),
            &format!("D:P(A;OICI;FA;;;{owner})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"),
            "private recovery fixture",
        )
        .unwrap();
        Self { root, base }
    }
    fn cleanup(self) {
        let checked = self.root.canonicalize().unwrap();
        assert!(checked.starts_with(&self.base) && checked != self.base);
        std::fs::remove_dir_all(checked).unwrap();
    }
}
fn canonical(path: &std::path::Path) -> String {
    path_id::canonicalize_path(path.to_str().unwrap())
        .unwrap()
        .0
}
fn dacl(path: &std::path::Path) -> (bool, Vec<u8>) {
    let (descriptor, raw) = acl::read_file_dacl(path.to_str().unwrap()).unwrap();
    assert!(!raw.is_null());
    let bytes =
        unsafe { std::slice::from_raw_parts(raw.cast::<u8>(), (*raw).AclSize as usize) }.to_vec();
    (acl::sd_dacl_protected(&descriptor).unwrap(), bytes)
}
fn fixture_aces(path: &std::path::Path) -> usize {
    let (_descriptor, raw) = acl::read_file_dacl(path.to_str().unwrap()).unwrap();
    let sid = crate::sid::sid_bytes(FIXTURE_SID).unwrap();
    acl::filter_aces(raw, |_, body| acl::ace_sid_is(body, &sid))
        .unwrap()
        .0
        .len()
}
fn grants(db: &Locked) -> i64 {
    db.conn
        .query_row(
            "SELECT COUNT(*) FROM working_aces WHERE kind = 'grant'",
            [],
            |row| row.get(0),
        )
        .unwrap()
}
fn orphan(db: &Locked) {
    db.conn.execute("DELETE FROM ace_holders", []).unwrap();
}

#[test]
fn recovery_absence_only_retires_noninheriting_traverse_grants() {
    require_absent_grant_is_object_only("traverse").unwrap();
    for mask in ["read", "modify", "denyRead", "", "unknown"] {
        assert!(require_absent_grant_is_object_only(mask).is_err(), "{mask}");
    }
}

#[test]
fn recovery_moved_file_release_revokes_exact_object_and_preserves_replacement_dacl() {
    let fixture = Fixture::new();
    let original = fixture.root.join("original");
    let moved = fixture.root.join("moved");
    std::fs::write(&original, b"original identity").unwrap();
    let canon = canonical(&original);
    with_mem_db(|db| {
        db.register_broker().unwrap();
        let _witness = db
            .ensure_ace(&canon, SbAce::Grant(acl::GrantMask::Modify), FIXTURE_SID)
            .unwrap();
        let expected = path_id::capture_file_id(&canon).unwrap();
        assert!(fixture_aces(&original) > 0);
        std::fs::rename(&original, &moved).unwrap();
        std::fs::write(&original, b"replacement identity").unwrap();
        let replacement_id = path_id::capture_file_id(original.to_str().unwrap()).unwrap();
        assert_ne!(expected, replacement_id);
        let replacement = dacl(&original);
        let result = db.release_one_ace(&canon, "grant", FIXTURE_SID).unwrap();
        assert!(matches!(result, AceRelease::Relocated { .. }));
        assert_eq!(
            path_id::capture_file_id(moved.to_str().unwrap()).unwrap(),
            expected
        );
        assert_eq!(fixture_aces(&moved), 0);
        assert_eq!(dacl(&original), replacement);
        assert_eq!(grants(db), 0);
        assert!(db.my_ace_holds(None).unwrap().is_empty());
    });
    fixture.cleanup();
}

#[test]
fn recovery_moved_tree_orphan_revokes_live_original_and_preserves_foreign_tree() {
    let fixture = Fixture::new();
    let original = fixture.root.join("original");
    let moved = fixture.root.join("moved");
    std::fs::create_dir(&original).unwrap();
    std::fs::write(original.join("child"), b"original descendant").unwrap();
    let canon = canonical(&original);
    with_mem_db(|db| {
        db.register_broker().unwrap();
        let _witness = db
            .ensure_ace(&canon, SbAce::Grant(acl::GrantMask::ReadOnly), FIXTURE_SID)
            .unwrap();
        let expected = path_id::capture_file_id(&canon).unwrap();
        assert!(fixture_aces(&original.join("child")) > 0);
        orphan(db);
        std::fs::rename(&original, &moved).unwrap();
        std::fs::create_dir(&original).unwrap();
        std::fs::write(original.join("child"), b"foreign descendant").unwrap();
        let replacement = dacl(&original);
        let replacement_child = dacl(&original.join("child"));
        recover_orphan(&db.conn, &canon, "grant", &expected.as_bytes(), FIXTURE_SID).unwrap();
        assert_eq!(fixture_aces(&moved), 0);
        assert_eq!(fixture_aces(&moved.join("child")), 0);
        assert_eq!(dacl(&original), replacement);
        assert_eq!(dacl(&original.join("child")), replacement_child);
        assert_eq!(grants(db), 0);
    });
    fixture.cleanup();
}

#[test]
fn recovery_deleted_root_keeps_valid_identity_ledger_and_moved_descendant_evidence() {
    for as_orphan in [false, true] {
        let fixture = Fixture::new();
        let original = fixture.root.join("original");
        let moved_child = fixture.root.join("moved-child");
        std::fs::create_dir(&original).unwrap();
        std::fs::write(original.join("child"), b"must not lose descendant evidence").unwrap();
        let canon = canonical(&original);
        with_mem_db(|db| {
            db.register_broker().unwrap();
            let _witness = db
                .ensure_ace(&canon, SbAce::Grant(acl::GrantMask::Modify), FIXTURE_SID)
                .unwrap();
            let expected = path_id::capture_file_id(&canon).unwrap();
            std::fs::rename(original.join("child"), &moved_child).unwrap();
            assert!(
                fixture_aces(&moved_child) > 0,
                "same-volume move retains inherited grant"
            );
            let child_before = dacl(&moved_child);
            std::fs::remove_dir(&original).unwrap();
            std::fs::create_dir(&original).unwrap();
            assert_ne!(
                path_id::capture_file_id(original.to_str().unwrap()).unwrap(),
                expected
            );
            let replacement = dacl(&original);
            let error = if as_orphan {
                orphan(db);
                recover_orphan(&db.conn, &canon, "grant", &expected.as_bytes(), FIXTURE_SID)
                    .unwrap_err()
            } else {
                let error = db
                    .release_one_ace(&canon, "grant", FIXTURE_SID)
                    .unwrap_err();
                assert_eq!(db.my_ace_holds(None).unwrap().len(), 1);
                error
            };
            assert!(
                matches!(
                    error.downcast_ref::<path_id::RecoveryLookupError>(),
                    Some(
                        path_id::RecoveryLookupError::UnverifiableIdentity
                            | path_id::RecoveryLookupError::DeletePending
                    )
                ) || error
                    .to_string()
                    .contains("descendant identity inventory is unavailable"),
                "unexpected refusal: {error:#}"
            );
            eprintln!("deleted root refusal (orphan={as_orphan}): {error:#}");
            assert_eq!(grants(db), 1, "root-only grant record must remain");
            let recorded: Vec<u8> = db
                .conn
                .query_row(
                    "SELECT file_id FROM working_aces WHERE canonical_path = ?1",
                    [&canon],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(recorded, expected.as_bytes());
            assert_eq!(
                dacl(&moved_child),
                child_before,
                "no invented descendant cleanup"
            );
            assert_eq!(dacl(&original), replacement, "replacement DACL untouched");
        });
        fixture.cleanup();
    }
}

#[test]
fn recovery_deleted_object_only_grant_distinguishes_pending_and_unverifiable_identity() {
    use windows::Win32::Storage::FileSystem::{
        CreateFileW, FILE_FLAG_BACKUP_SEMANTICS, FILE_SHARE_DELETE, FILE_SHARE_READ,
        FILE_SHARE_WRITE, OPEN_EXISTING,
    };

    for is_directory in [false, true] {
        for as_orphan in [false, true] {
            let fixture = Fixture::new();
            let original = fixture.root.join("original");
            let moved = fixture.root.join("moved");
            let create = |path: &std::path::Path| {
                if is_directory {
                    std::fs::create_dir(path).unwrap();
                } else {
                    std::fs::write(path, b"object-only grant identity").unwrap();
                }
            };
            create(&original);
            let canon = canonical(&original);
            with_mem_db(|db| {
                db.register_broker().unwrap();
                let _witness = db
                    .ensure_ace(&canon, SbAce::Grant(acl::GrantMask::Traverse), FIXTURE_SID)
                    .unwrap();
                let expected = path_id::capture_file_id(&canon).unwrap();
                let lookup = || path_id::locate_by_file_id_checked(&expected);
                assert_eq!(
                    path_id::capture_file_id(&lookup().unwrap().unwrap()).unwrap(),
                    expected
                );
                std::fs::rename(&original, &moved).unwrap();
                assert_eq!(
                    path_id::capture_file_id(&lookup().unwrap().unwrap()).unwrap(),
                    expected
                );
                std::fs::rename(&moved, &original).unwrap();

                let wide = crate::util::wstr(&canon);
                let held = crate::util::OwnedHandle(
                    unsafe {
                        CreateFileW(
                            crate::util::pcwstr(&wide),
                            0,
                            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                            None,
                            OPEN_EXISTING,
                            FILE_FLAG_BACKUP_SEMANTICS,
                            None,
                        )
                    }
                    .unwrap(),
                );
                assert_eq!(path_id::file_id_from_handle(held.raw()).unwrap(), expected);
                if as_orphan {
                    orphan(db);
                }
                let release = || {
                    if as_orphan {
                        recover_orphan(&db.conn, &canon, "grant", &expected.as_bytes(), FIXTURE_SID)
                    } else {
                        db.release_one_ace(&canon, "grant", FIXTURE_SID).map(|_| ())
                    }
                };
                if is_directory {
                    std::fs::remove_dir(&original).unwrap();
                } else {
                    std::fs::remove_file(&original).unwrap();
                }
                let error = release().unwrap_err();
                assert_eq!(
                    error.downcast_ref::<path_id::RecoveryLookupError>(),
                    Some(&path_id::RecoveryLookupError::DeletePending),
                    "held object: {error:#}"
                );
                assert_eq!(grants(db), 1);
                assert_eq!(
                    db.my_ace_holds(None).unwrap().len(),
                    usize::from(!as_orphan)
                );
                drop(held);

                // Final deletion is a separate observation. A fresh object at
                // the pathname never substitutes for the original identity.
                create(&original);
                assert_ne!(path_id::capture_file_id(&canon).unwrap(), expected);
                let replacement = dacl(&original);
                match lookup() {
                    Ok(None) => {
                        release().unwrap();
                        assert_eq!(grants(db), 0);
                        assert!(db.my_ace_holds(None).unwrap().is_empty());
                        eprintln!(
                            "object-only deleted identity: directory={is_directory}, orphan={as_orphan}, definitive_absence=true"
                        );
                    }
                    Err(lookup_error) => {
                        assert_eq!(
                            lookup_error.downcast_ref::<path_id::RecoveryLookupError>(),
                            Some(&path_id::RecoveryLookupError::UnverifiableIdentity),
                            "deleted lookup: {lookup_error:#}"
                        );
                        let error = release().unwrap_err();
                        assert_eq!(
                            error.downcast_ref::<path_id::RecoveryLookupError>(),
                            Some(&path_id::RecoveryLookupError::UnverifiableIdentity)
                        );
                        assert_eq!(grants(db), 1);
                        assert_eq!(
                            db.my_ace_holds(None).unwrap().len(),
                            usize::from(!as_orphan)
                        );
                        let recorded: Vec<u8> = db
                            .conn
                            .query_row(
                                "SELECT file_id FROM working_aces WHERE canonical_path = ?1",
                                [&canon],
                                |row| row.get(0),
                            )
                            .unwrap();
                        assert_eq!(recorded, expected.as_bytes());
                        eprintln!(
                            "object-only deleted identity: directory={is_directory}, orphan={as_orphan}, unverifiable_identity_retained=true"
                        );
                    }
                    Ok(Some(_)) => panic!("fully deleted identity unexpectedly resolved"),
                }
                assert_eq!(dacl(&original), replacement, "replacement DACL untouched");
            });
            fixture.cleanup();
        }
    }
}

// This independent low-level Win32 query observes stored ACL/control, rather
// than the inheritance view reconstructed by GetNamedSecurityInfo.
fn raw_fixture_dacl(path: &std::path::Path) -> (u16, Vec<u8>) {
    use windows::Win32::Security::{
        DACL_SECURITY_INFORMATION, GetFileSecurityW, GetSecurityDescriptorControl,
        GetSecurityDescriptorDacl, PSECURITY_DESCRIPTOR,
    };
    let mut words = vec![0usize; 128 * 1024 / std::mem::size_of::<usize>()];
    let descriptor = PSECURITY_DESCRIPTOR(words.as_mut_ptr().cast());
    let mut needed = 0;
    unsafe {
        GetFileSecurityW(
            crate::util::pcwstr(&crate::util::wstr(path.to_str().unwrap())),
            DACL_SECURITY_INFORMATION.0,
            Some(descriptor),
            (words.len() * std::mem::size_of::<usize>()) as u32,
            &mut needed,
        )
        .ok()
        .unwrap();
    }
    assert!((20..=words.len() * std::mem::size_of::<usize>()).contains(&(needed as usize)));
    let mut control = 0;
    let mut revision = 0;
    let mut present = false.into();
    let mut defaulted = false.into();
    let mut dacl = std::ptr::null_mut();
    unsafe {
        GetSecurityDescriptorControl(descriptor, &mut control, &mut revision).unwrap();
        GetSecurityDescriptorDacl(descriptor, &mut present, &mut dacl, &mut defaulted).unwrap();
    }
    assert!(present.as_bool() && !dacl.is_null());
    (
        control,
        unsafe { std::slice::from_raw_parts(dacl.cast::<u8>(), (*dacl).AclSize as usize) }.to_vec(),
    )
}

fn unsupported_fixture_dacl(path: &std::path::Path, preserve_grant: bool) {
    use windows::Win32::Security::{DACL_SECURITY_INFORMATION, SetFileSecurityW};
    let owner = crate::sid::current_user_sid().unwrap();
    let grant = if preserve_grant {
        format!("(A;OICI;0x1301bf;;;{FIXTURE_SID})")
    } else {
        String::new()
    };
    // A real stored object ACE with an unrelated trustee; no token, account,
    // installed helper or setup database is used by this fixture.
    let descriptor = crate::util::OwnedSd::from_sddl(&format!(
        "D:P(A;OICI;FA;;;{owner})(A;OICI;FA;;;SY){grant}(OA;;FR;11111111-1111-1111-1111-111111111111;;BU)"
    )).unwrap();
    unsafe {
        SetFileSecurityW(
            crate::util::pcwstr(&crate::util::wstr(path.to_str().unwrap())),
            DACL_SECURITY_INFORMATION,
            descriptor.ptr,
        )
    }
    .unwrap();
}

fn all_acl_rows(db: &Locked) -> (i64, i64) {
    (
        db.conn
            .query_row("SELECT COUNT(*) FROM ace_holders", [], |r| r.get(0))
            .unwrap(),
        db.conn
            .query_row("SELECT COUNT(*) FROM working_aces", [], |r| r.get(0))
            .unwrap(),
    )
}

#[test]
fn recovery_preflight_refuses_new_unsupported_grant_and_deny_without_intent() {
    let fixture = Fixture::new();
    let target = fixture.root.join("unsupported");
    std::fs::create_dir(&target).unwrap();
    unsupported_fixture_dacl(&target, false);
    let canon = canonical(&target);
    let before = raw_fixture_dacl(&target);
    with_mem_db(|db| {
        db.register_broker().unwrap();
        for requested in [
            SbAce::Grant(acl::GrantMask::ReadOnly),
            SbAce::Grant(acl::GrantMask::Modify),
            SbAce::Deny(acl::DenyMask::ReadDeny),
            SbAce::Deny(acl::DenyMask::WriteDeny),
            SbAce::DenyFdc,
            SbAce::DenyDelete,
        ] {
            let error = db.ensure_ace(&canon, requested, FIXTURE_SID).unwrap_err();
            assert!(
                format!("{error:#}").contains("ordinary bounded"),
                "{error:#}"
            );
            assert_eq!(
                all_acl_rows(db),
                (0, 0),
                "refused admission creates no ACL intent"
            );
            assert_eq!(raw_fixture_dacl(&target), before);
        }
    });
    fixture.cleanup();
}

#[test]
fn recovery_unsupported_drift_preserves_existing_intent_and_acl() {
    assert_refused_drift_preserves_intent(|path| unsupported_fixture_dacl(path, true));
}

#[test]
fn recovery_critical_target_drift_preserves_existing_intent_and_acl() {
    assert_refused_drift_preserves_intent(|path| critical_target_fixture_dacl(path, true));
}

fn assert_refused_drift_preserves_intent(drift: impl FnOnce(&std::path::Path)) {
    let fixture = Fixture::new();
    let target = fixture.root.join("drift");
    std::fs::create_dir(&target).unwrap();
    let canon = canonical(&target);
    with_mem_db(|db| {
        db.register_broker().unwrap();
        let _witness = db
            .ensure_ace(&canon, SbAce::Grant(acl::GrantMask::Modify), FIXTURE_SID)
            .unwrap();
        let id = path_id::capture_file_id(&canon).unwrap();
        assert_eq!(all_acl_rows(db), (1, 1));
        drift(&target);
        let drifted = raw_fixture_dacl(&target);
        assert!(
            fixture_aces(&target) > 0,
            "fixture retains the recorded grant"
        );

        // A refused reapply must not overwrite the existing holder's mask.
        assert!(
            db.ensure_ace(&canon, SbAce::Grant(acl::GrantMask::ReadOnly), FIXTURE_SID)
                .is_err()
        );
        assert_eq!(all_acl_rows(db), (1, 1));
        let held: String = db
            .conn
            .query_row(
                "SELECT want_mask FROM ace_holders WHERE canonical_path = ?1",
                [&canon],
                |r| r.get(0),
            )
            .unwrap();
        let working: (String, Vec<u8>) = db
            .conn
            .query_row(
                "SELECT mask, file_id FROM working_aces WHERE canonical_path = ?1",
                [&canon],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(held, "modify");
        assert_eq!(working, ("modify".into(), id.as_bytes().to_vec()));
        assert_eq!(raw_fixture_dacl(&target), drifted);

        assert!(db.release_one_ace(&canon, "grant", FIXTURE_SID).is_err());
        assert_eq!(
            all_acl_rows(db),
            (1, 1),
            "failed release retains holder and intent"
        );
        assert_eq!(raw_fixture_dacl(&target), drifted);
        orphan(db);
        assert!(recover_orphan(&db.conn, &canon, "grant", &id.as_bytes(), FIXTURE_SID).is_err());
        assert_eq!(all_acl_rows(db), (0, 1), "orphan refusal retains intent");
        assert_eq!(raw_fixture_dacl(&target), drifted);
    });
    fixture.cleanup();
}

fn critical_target_fixture_dacl(path: &std::path::Path, preserve_grant: bool) {
    use windows::Win32::Security::{DACL_SECURITY_INFORMATION, SetFileSecurityW};
    let owner = crate::sid::current_user_sid().unwrap();
    let grant = if preserve_grant {
        format!("(A;OICI;0x1301bf;;;{FIXTURE_SID})")
    } else {
        String::new()
    };
    let descriptor = crate::util::OwnedSd::from_sddl(&format!(
        "D:P(A;OICI;FA;;;{owner})(A;OICI;FA;;;SY){grant}(A;OICICR;FR;;;{FIXTURE_SID})"
    ))
    .unwrap();
    unsafe {
        SetFileSecurityW(
            crate::util::pcwstr(&crate::util::wstr(path.to_str().unwrap())),
            DACL_SECURITY_INFORMATION,
            descriptor.ptr,
        )
        .unwrap();
    }
}

#[test]
fn recovery_preflight_refuses_new_critical_target_without_intent() {
    let fixture = Fixture::new();
    let target = fixture.root.join("critical");
    std::fs::create_dir(&target).unwrap();
    critical_target_fixture_dacl(&target, false);
    let canon = canonical(&target);
    let before = raw_fixture_dacl(&target);
    with_mem_db(|db| {
        db.register_broker().unwrap();
        for requested in [
            SbAce::Grant(acl::GrantMask::Modify),
            SbAce::DenyFdc,
            SbAce::DenyDelete,
        ] {
            let error = db.ensure_ace(&canon, requested, FIXTURE_SID).unwrap_err();
            assert!(
                format!("{error:#}").contains("critical_target"),
                "{error:#}"
            );
            assert_eq!(all_acl_rows(db), (0, 0));
            assert_eq!(raw_fixture_dacl(&target), before);
        }
    });
    fixture.cleanup();
}
