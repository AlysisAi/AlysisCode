//! No machine effects unless the explicit disposable-host admission succeeds.
//! No accounts/profiles/ACL grants outside fresh kernel roots, no child process.
use super::*;
use windows::Win32::Security::Authorization::{SE_KERNEL_OBJECT, SetSecurityInfo};
const TEST_NAME: &str = "candidate_namespace_disposable_host_lifecycle";
const SENTINEL: &str = "github-hosted-empty-namespace-only";
fn admission(values: &[Option<String>], run: &str, attempt: &str) -> bool {
    let expected = [
        SENTINEL,
        "true",
        "github-hosted",
        "Windows",
        "AlysisAi/alysis-code-internal",
        "workflow_dispatch",
    ];
    values.len() == expected.len()
        && values
            .iter()
            .zip(expected)
            .all(|(v, w)| v.as_deref() == Some(w))
        && [run, attempt]
            .into_iter()
            .all(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()))
        && cfg!(all(windows, target_arch = "x86_64"))
}
#[test]
fn candidate_namespace_disposable_admission_requires_every_explicit_gate() {
    let good = [
        SENTINEL,
        "true",
        "github-hosted",
        "Windows",
        "AlysisAi/alysis-code-internal",
        "workflow_dispatch",
    ]
    .map(|v| Some(v.into()));
    assert!(admission(&good, "123", "1"));
    for i in 0..good.len() {
        let mut bad = good.clone();
        bad[i] = None;
        assert!(!admission(&bad, "123", "1"));
        bad[i] = Some("wrong".into());
        assert!(!admission(&bad, "123", "1"));
    }
    assert!(!admission(&good, "", "1"));
    assert!(!admission(&good, "123", "a"));
}
fn root(guard: &EmptyNamespaceRoot) -> HANDLE {
    match &guard.phase {
        Phase::Open(h) => h.raw(),
        _ => panic!("owned open fixture required"),
    }
}
fn duplicate(handle: HANDLE) -> OwnedHandle {
    let mut copy = HANDLE::default();
    unsafe {
        DuplicateHandle(
            GetCurrentProcess(),
            handle,
            GetCurrentProcess(),
            &mut copy,
            0,
            false,
            DUPLICATE_SAME_ACCESS,
        )
        .unwrap();
    }
    OwnedHandle(copy)
}
fn born(db: &Connection, id: &Identity) -> EmptyNamespaceRoot {
    let journal = live(db, id, true);
    let mut guard = journal.create_empty_namespace_root().unwrap();
    journal.record_empty_namespace_root(&mut guard).unwrap();
    guard
}
fn finished(db: &Connection, id: &Identity, guard: &mut EmptyNamespaceRoot) {
    stopped(db, id).retire_empty_namespace_root(guard).unwrap();
    assert_eq!(guard.close_calls, 1);
    stopped(db, id).recovered().unwrap();
}
fn round_trip() {
    let id = identity();
    let db = database(&id);
    let mut guard = born(&db, &id);
    assert!(
        live(&db, &id, false)
            .retire_empty_namespace_root(&mut guard)
            .is_err()
    );
    assert!(
        stopped(&db, &id)
            .record_empty_namespace_root(&mut guard)
            .is_err()
    );
    // Reopen journal object while original root/parent handles stay with caller.
    finished(&db, &id, &mut guard);
    finished(&db, &id, &mut guard);
    assert_eq!(stopped(&db, &id).replay().unwrap().events, 4);
}
fn collision() {
    let id = identity();
    let parent = open_parent(&id).unwrap();
    let original = create_native(&id, parent.raw()).unwrap();
    let before = native::query_security(original.raw()).unwrap();
    let db = database(&id);
    assert!(live(&db, &id, true).create_empty_namespace_root().is_err());
    assert_eq!(before, native::query_security(original.raw()).unwrap());
    verify_native(original.raw(), parent.raw(), &id).unwrap();
    assert!(matches!(
        stopped(&db, &id).replay().unwrap().states[&ROLE],
        State::Creating { .. }
    ));
    assert!(stopped(&db, &id).recovered().is_err());
    drop(original);
    require_absent(parent.raw(), &id).unwrap();
}
fn receipt_failures() {
    for point in [
        Fault::CreatedAfterCommit,
        Fault::RemoveIntentAfterCommit,
        Fault::RemovedAfterCommit,
    ] {
        let id = identity();
        let db = database(&id);
        let journal = live(&db, &id, true);
        let mut guard = journal.create_empty_namespace_root().unwrap();
        if point == Fault::CreatedAfterCommit {
            guard.fault = Some(point);
            assert!(journal.record_empty_namespace_root(&mut guard).is_err());
        }
        journal.record_empty_namespace_root(&mut guard).unwrap();
        drop(journal);
        if point != Fault::CreatedAfterCommit {
            guard.fault = Some(point);
            assert!(
                stopped(&db, &id)
                    .retire_empty_namespace_root(&mut guard)
                    .is_err()
            );
        }
        finished(&db, &id, &mut guard);
    }
    let id = identity();
    let db = database(&id);
    let journal = live(&db, &id, true);
    let parent = open_parent(&id).unwrap();
    db.execute_batch("CREATE TRIGGER fail_namespace_intent BEFORE INSERT ON candidate_resource_events BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    assert!(journal.create_empty_namespace_root().is_err());
    assert_eq!(journal.replay().unwrap().events, 0);
    require_absent(parent.raw(), &id).unwrap();
}
fn fail_inserts(db: &Connection) {
    db.execute_batch("CREATE TRIGGER fail_namespace_receipt BEFORE INSERT ON candidate_resource_events BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
}
fn resume_inserts(db: &Connection) {
    db.execute_batch("DROP TRIGGER fail_namespace_receipt;")
        .unwrap();
}
fn rejected_writes_keep_ownership() {
    let id = identity();
    let db = database(&id);
    let journal = live(&db, &id, true);
    let mut guard = journal.create_empty_namespace_root().unwrap();
    fail_inserts(&db);
    assert!(journal.record_empty_namespace_root(&mut guard).is_err());
    assert_eq!(journal.replay().unwrap().events, 1);
    guard.verify_open().unwrap();
    resume_inserts(&db);
    journal.record_empty_namespace_root(&mut guard).unwrap();
    let extra = duplicate(root(&guard));
    fail_inserts(&db);
    assert!(
        stopped(&db, &id)
            .retire_empty_namespace_root(&mut guard)
            .is_err()
    );
    assert_eq!(guard.close_calls, 0);
    assert_eq!(journal.replay().unwrap().events, 2);
    let nonce = guard.remove_nonce.clone();
    resume_inserts(&db);
    assert!(
        stopped(&db, &id)
            .retire_empty_namespace_root(&mut guard)
            .is_err()
    );
    assert_eq!(guard.close_calls, 1);
    assert_eq!(guard.remove_nonce, nonce);
    drop(extra);
    fail_inserts(&db);
    assert!(
        stopped(&db, &id)
            .retire_empty_namespace_root(&mut guard)
            .is_err()
    );
    assert_eq!(guard.close_calls, 1);
    assert_eq!(journal.replay().unwrap().events, 3);
    resume_inserts(&db);
    finished(&db, &id, &mut guard);
}
fn original_nonce_and_file_reopen() {
    let id = identity();
    let directory = std::path::PathBuf::from(std::env::var_os("RUNNER_TEMP").unwrap());
    let path = directory.join(format!(
        "alysis-namespace-{}.db",
        super::super::super::super::random_id().unwrap()
    ));
    assert!(!path.exists());
    let mut guard;
    {
        let mut db = Connection::open(&path).unwrap();
        db.execute_batch("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL;")
            .unwrap();
        crate::account_state::initialize_header(&mut db, &id.execution.identity.binding).unwrap();
        let journal = live(&db, &id, true);
        guard = journal.create_empty_namespace_root().unwrap();
        let nonce = guard.create_nonce.clone();
        guard.create_nonce = super::super::super::super::random_id().unwrap();
        assert!(journal.record_empty_namespace_root(&mut guard).is_err());
        assert_eq!(journal.replay().unwrap().events, 1);
        guard.create_nonce = nonce;
        journal.record_empty_namespace_root(&mut guard).unwrap();
    }
    let mut foreign = id.clone();
    foreign.execution.pending_id[0] ^= 0x55;
    let foreign_db = database(&foreign);
    drop(live(&foreign_db, &foreign, true));
    assert!(
        stopped(&foreign_db, &foreign)
            .retire_empty_namespace_root(&mut guard)
            .is_err()
    );
    assert_eq!(guard.close_calls, 0);
    guard.verify_open().unwrap();
    {
        let db = Connection::open(&path).unwrap();
        finished(&db, &id, &mut guard);
        assert_eq!(stopped(&db, &id).replay().unwrap().events, 4);
    }
    std::fs::remove_file(path).unwrap();
}
fn lost_guard_does_not_authorize_recovery() {
    for acknowledged in [false, true] {
        let id = identity();
        let db = database(&id);
        let journal = live(&db, &id, true);
        let mut guard = journal.create_empty_namespace_root().unwrap();
        if acknowledged {
            journal.record_empty_namespace_root(&mut guard).unwrap();
        }
        let parent = duplicate(guard.parent.raw());
        drop(guard);
        require_absent(parent.raw(), &id).unwrap();
        assert!(stopped(&db, &id).recovered().is_err());
        assert!(journal.create_empty_namespace_root().is_err());
        assert_eq!(
            journal.replay().unwrap().events,
            if acknowledged { 2 } else { 1 }
        );
    }
}
fn extra_reference_and_replacement() {
    let id = identity();
    let db = database(&id);
    let mut guard = born(&db, &id);
    let extra = duplicate(root(&guard));
    assert!(
        stopped(&db, &id)
            .retire_empty_namespace_root(&mut guard)
            .is_err()
    );
    assert_eq!(guard.close_calls, 1);
    assert!(matches!(guard.phase, Phase::Closed));
    assert!(stopped(&db, &id).recovered().is_err());
    drop(extra);
    let replacement = create_native(&id, guard.parent.raw()).unwrap();
    let before = native::query_security(replacement.raw()).unwrap();
    assert!(
        stopped(&db, &id)
            .retire_empty_namespace_root(&mut guard)
            .is_err()
    );
    assert_eq!(guard.close_calls, 1);
    assert_eq!(before, native::query_security(replacement.raw()).unwrap());
    drop(replacement);
    finished(&db, &id, &mut guard);
}
fn unexpected_child_and_inheritance() {
    let id = identity();
    let db = database(&id);
    let journal = live(&db, &id, true);
    let mut guard = journal.create_empty_namespace_root().unwrap();
    unsafe {
        SetHandleInformation(root(&guard), HANDLE_FLAG_INHERIT.0, HANDLE_FLAG_INHERIT).unwrap();
    }
    assert!(journal.record_empty_namespace_root(&mut guard).is_err());
    unsafe {
        SetHandleInformation(root(&guard), HANDLE_FLAG_INHERIT.0, HANDLE_FLAGS(0)).unwrap();
    }
    journal.record_empty_namespace_root(&mut guard).unwrap();
    drop(journal);
    let child = native::child_fixture(root(&guard), &id).unwrap();
    assert!(
        stopped(&db, &id)
            .retire_empty_namespace_root(&mut guard)
            .is_err()
    );
    assert_eq!(guard.close_calls, 0);
    assert!(matches!(
        stopped(&db, &id).replay().unwrap().states[&ROLE],
        State::Present { .. }
    ));
    drop(child);
    finished(&db, &id, &mut guard);
}
fn security_drift() {
    let id = identity();
    let db = database(&id);
    let mut guard = born(&db, &id);
    let descriptor = OwnedSd::from_sddl("D:P(A;;0xf000f;;;SY)").unwrap();
    let mut acl = std::ptr::null_mut();
    let mut present = false.into();
    let mut defaulted = false.into();
    unsafe {
        GetSecurityDescriptorDacl(descriptor.ptr, &mut present, &mut acl, &mut defaulted).unwrap();
        SetSecurityInfo(
            root(&guard),
            SE_KERNEL_OBJECT,
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            Some(acl),
            None,
        )
        .ok()
        .unwrap();
    }
    let changed = native::query_security(root(&guard)).unwrap();
    assert!(
        stopped(&db, &id)
            .retire_empty_namespace_root(&mut guard)
            .is_err()
    );
    assert_eq!(guard.close_calls, 0);
    assert_eq!(changed, native::query_security(root(&guard)).unwrap());
    let expected = native::descriptor(&id).unwrap();
    unsafe {
        GetSecurityDescriptorDacl(expected.ptr, &mut present, &mut acl, &mut defaulted).unwrap();
        SetSecurityInfo(
            root(&guard),
            SE_KERNEL_OBJECT,
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            Some(acl),
            None,
        )
        .ok()
        .unwrap();
    }
    finished(&db, &id, &mut guard);
}
fn label_drift() {
    let id = identity();
    let db = database(&id);
    let mut guard = born(&db, &id);
    let change_label = |descriptor: &OwnedSd| {
        let mut acl = std::ptr::null_mut();
        let mut present = false.into();
        let mut defaulted = false.into();
        unsafe {
            GetSecurityDescriptorSacl(descriptor.ptr, &mut present, &mut acl, &mut defaulted)
                .unwrap();
            assert!(present.as_bool() && !acl.is_null());
            SetSecurityInfo(
                root(&guard),
                SE_KERNEL_OBJECT,
                LABEL_SECURITY_INFORMATION,
                None,
                None,
                None,
                Some(acl),
            )
            .ok()
            .unwrap();
        }
    };
    let changed = OwnedSd::from_sddl("S:P(ML;;NW;;;ME)").unwrap();
    change_label(&changed);
    let bytes = native::query_security(root(&guard)).unwrap();
    assert!(
        stopped(&db, &id)
            .retire_empty_namespace_root(&mut guard)
            .is_err()
    );
    assert_eq!(guard.close_calls, 0);
    assert_eq!(bytes, native::query_security(root(&guard)).unwrap());
    // Only this test-owned original handle is repaired by the fixture.
    let expected = native::descriptor(&id).unwrap();
    let mut acl = std::ptr::null_mut();
    let mut present = false.into();
    let mut defaulted = false.into();
    unsafe {
        GetSecurityDescriptorSacl(expected.ptr, &mut present, &mut acl, &mut defaulted).unwrap();
        SetSecurityInfo(
            root(&guard),
            SE_KERNEL_OBJECT,
            LABEL_SECURITY_INFORMATION,
            None,
            None,
            None,
            Some(acl),
        )
        .ok()
        .unwrap();
    }
    finished(&db, &id, &mut guard);
}
fn uncertain_close() {
    let id = identity();
    let db = database(&id);
    let mut guard = born(&db, &id);
    let nonce = stopped(&db, &id).remove_intent(ROLE).unwrap();
    guard.remove_nonce = Some(nonce);
    let Phase::Open(handle) = std::mem::replace(&mut guard.phase, Phase::CloseUncertain) else {
        unreachable!()
    };
    unsafe {
        CloseHandle(handle.into_raw()).unwrap();
    }
    guard.close_calls = 1;
    assert!(
        stopped(&db, &id)
            .retire_empty_namespace_root(&mut guard)
            .is_err()
    );
    assert!(
        stopped(&db, &id)
            .retire_empty_namespace_root(&mut guard)
            .is_err()
    );
    assert_eq!(guard.close_calls, 1);
    assert!(stopped(&db, &id).recovered().is_err());
    require_absent(guard.parent.raw(), &id).unwrap();
}
fn private_birth_stages() {
    use native::BirthStage::{Labelled, Private, Published};
    for failure in [Some(Private), Some(Labelled), Some(Published), None] {
        let id = identity();
        let db = database(&id);
        let journal = live(&db, &id, true);
        journal.create_intent(ROLE, None).unwrap();
        let parent = open_parent(&id).unwrap();
        let account = crate::sid::sid_bytes(&id.execution.identity.binding.account_sid).unwrap();
        let package = crate::sid::sid_bytes(&id.execution.identity.package_sid).unwrap();
        let low = native::security_shape(&security_bytes(
            &OwnedSd::from_sddl("O:SYD:P(A;;0xf000f;;;SY)S:(ML;;NW;;;LW)").unwrap(),
        ))
        .unwrap()
        .label;
        let mut stages = Vec::new();
        let result = native::fixture_create_stages(&id, parent.raw(), |stage, handle| {
            let shape = native::security_shape(&native::query_security(handle)?)?;
            let published = stage == Published;
            // Independent Windows readback verifies both untrusted subjects are
            // absent before label completion and present only at publication.
            assert_eq!(
                shape.dacl.windows(account.len()).any(|v| v == account),
                published
            );
            assert_eq!(
                shape.dacl.windows(package.len()).any(|v| v == package),
                published
            );
            assert_eq!(
                u16::from_le_bytes(shape.dacl[4..6].try_into().unwrap()),
                if published { 4 } else { 2 }
            );
            if stage != Private {
                assert_eq!(shape.label, low);
            }
            stages.push(stage);
            if failure == Some(stage) {
                anyhow::bail!("injected owned birth stage failure");
            }
            Ok(())
        });
        assert_eq!(result.is_err(), failure.is_some());
        if let Ok(handle) = result {
            drop(handle);
        }
        let expected = match failure {
            Some(Private) => vec![Private],
            Some(Labelled) => vec![Private, Labelled],
            _ => vec![Private, Labelled, Published],
        };
        assert_eq!(stages, expected);
        require_absent(parent.raw(), &id).unwrap();
        assert_eq!(journal.replay().unwrap().events, 1);
        assert!(stopped(&db, &id).recovered().is_err());
    }
}
#[test]
#[ignore = "explicit disposable hosted namespace-only qualification"]
fn candidate_namespace_disposable_host_lifecycle() {
    let values = [
        "ALYSIS_RUN_DISPOSABLE_NAMESPACE",
        "GITHUB_ACTIONS",
        "RUNNER_ENVIRONMENT",
        "RUNNER_OS",
        "GITHUB_REPOSITORY",
        "GITHUB_EVENT_NAME",
    ]
    .map(|name| std::env::var(name).ok());
    let run = std::env::var("GITHUB_RUN_ID").unwrap_or_default();
    let attempt = std::env::var("GITHUB_RUN_ATTEMPT").unwrap_or_default();
    assert!(
        admission(&values, &run, &attempt),
        "disposable hosted namespace admission refused; no local override"
    );
    let directory = std::path::PathBuf::from(
        std::env::var_os("RUNNER_TEMP").expect("CI temp directory required"),
    );
    assert!(directory.is_absolute() && directory.is_dir());
    let path = directory.join("alysis-namespace-resources.json");
    assert!(!path.exists(), "preserve prior evidence");
    let mut completed = Vec::new();
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        for (name, case) in [
            ("private_birth_stages", private_birth_stages as fn()),
            ("exclusive_birth_and_retirement", round_trip as fn()),
            ("collision_preserved", collision),
            ("durable_receipt_failures", receipt_failures),
            (
                "rejected_writes_keep_ownership",
                rejected_writes_keep_ownership,
            ),
            (
                "original_nonce_and_file_reopen",
                original_nonce_and_file_reopen,
            ),
            (
                "lost_guard_refuses_absence_recovery",
                lost_guard_does_not_authorize_recovery,
            ),
            (
                "extra_reference_and_replacement",
                extra_reference_and_replacement,
            ),
            (
                "unexpected_child_and_inheritance",
                unexpected_child_and_inheritance,
            ),
            ("security_drift_preserved", security_drift),
            ("label_drift_preserved", label_drift),
            ("uncertain_close_no_retry", uncertain_close),
        ] {
            case();
            completed.push(name);
        }
    }));
    let report = serde_json::json!({"test":TEST_NAME,"run_id":run,"run_attempt":attempt,"passed":result.is_ok(),
        "completed_cases":completed,"no_accounts_profiles_or_processes":true,"no_shared_parent_acl_changes":true,
        "no_schannel_or_activation_claim":true});
    std::fs::write(path, serde_json::to_vec_pretty(&report).unwrap()).unwrap();
    if let Err(panic) = result {
        std::panic::resume_unwind(panic);
    }
}
