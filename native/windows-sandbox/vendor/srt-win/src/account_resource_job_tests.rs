//! Current-user empty jobs and synthetic SQLite only. No process is assigned
//! or launched; no profiles, accounts, WFP or installed state are touched.
use super::*;
use crate::account_install::{AccountMode, synthetic_ready};
use windows::Win32::{
    Foundation::{ERROR_ACCESS_DENIED, HANDLE_FLAGS, SetHandleInformation},
    Security::{
        Authorization::SetSecurityInfo, PROTECTED_DACL_SECURITY_INFORMATION,
        UNPROTECTED_DACL_SECURITY_INFORMATION,
    },
};

fn identity() -> Identity {
    let mut binding = synthetic_ready().binding(AccountMode::Offline).unwrap();
    binding.owner_sid = crate::sid::current_user_sid().unwrap();
    let candidate = crate::account_state::CandidateJournalIdentity::new(
        binding,
        format!(
            "Alysis.Execution.{}",
            super::super::super::random_id().unwrap()
        ),
    )
    .unwrap();
    let mut execution = super::super::super::fixture_record(candidate);
    execution.scope = RESOURCE_SCOPE.into();
    let mut session = 0;
    unsafe {
        ProcessIdToSessionId(std::process::id(), &mut session).unwrap();
    }
    Identity {
        execution,
        session_id: session,
    }
}
fn database(identity: &Identity) -> Connection {
    let mut db = Connection::open_in_memory().unwrap();
    crate::account_state::initialize_header(&mut db, &identity.execution.identity.binding).unwrap();
    db
}
fn live<'a>(db: &'a Connection, identity: &Identity, create: bool) -> ResourceJournal<'a> {
    ResourceJournal::open(db, identity.clone(), Authority::FixtureLive, create).unwrap()
}
fn stopped<'a>(db: &'a Connection, identity: &Identity) -> ResourceJournal<'a> {
    ResourceJournal::open(db, identity.clone(), Authority::FixtureStopped, false).unwrap()
}
fn state(db: &Connection, identity: &Identity) -> Replay {
    stopped(db, identity).replay().unwrap()
}
fn handle(guard: &EmptyBootstrapJob) -> HANDLE {
    let Phase::Open(handle) = &guard.phase else {
        panic!("fixture lacks original handle")
    };
    handle.raw()
}
fn fail_inserts(db: &Connection) {
    db.execute_batch("CREATE TRIGGER fail_empty_job BEFORE INSERT ON candidate_resource_events BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
}
fn resume_inserts(db: &Connection) {
    db.execute_batch("DROP TRIGGER fail_empty_job").unwrap();
}

#[test]
fn empty_job_birth_requires_exact_actor_and_live_authority_before_effect() {
    for defect in ["pid", "created", "session", "owner"] {
        let mut identity = identity();
        match defect {
            "pid" => identity.execution.pid += 100000,
            "created" => identity.execution.created += 1,
            "session" => identity.session_id += 1,
            "owner" => {
                identity.execution.owner = "S-1-5-21-1-2-3-9001".into();
                identity.execution.identity.binding.owner_sid = identity.execution.owner.clone();
            }
            _ => unreachable!(),
        }
        let db = database(&identity);
        let journal = live(&db, &identity, true);
        assert!(
            journal.create_empty_bootstrap_job().is_err(),
            "accepted {defect}"
        );
        assert_eq!(journal.replay().unwrap().events, 0);
        require_absent(&identity.name(ROLE)).unwrap();
    }
    let identity = identity();
    let db = database(&identity);
    drop(live(&db, &identity, true));
    assert!(
        stopped(&db, &identity)
            .create_empty_bootstrap_job()
            .is_err()
    );
    assert_eq!(state(&db, &identity).events, 0);
}

#[test]
fn empty_job_exact_native_birth_and_stopped_retirement() {
    let identity = identity();
    let db = database(&identity);
    let journal = live(&db, &identity, true);
    let mut guard = journal.create_empty_bootstrap_job().unwrap();
    guard.verify_open().unwrap();
    assert!(matches!(
        journal.replay().unwrap().states[&ROLE],
        State::Creating { .. }
    ));
    assert!(journal.retire_empty_bootstrap_job(&mut guard).is_err());
    assert!(
        stopped(&db, &identity)
            .record_empty_bootstrap_job(&mut guard)
            .is_err()
    );
    journal.record_empty_bootstrap_job(&mut guard).unwrap();
    journal.record_empty_bootstrap_job(&mut guard).unwrap();
    assert_eq!(journal.replay().unwrap().events, 2);
    drop(journal);
    let recovery = stopped(&db, &identity);
    recovery.retire_empty_bootstrap_job(&mut guard).unwrap();
    recovery.retire_empty_bootstrap_job(&mut guard).unwrap();
    assert_eq!(guard.close_calls, 1);
    assert_eq!(recovery.replay().unwrap().events, 4);
    recovery.recovered().unwrap();
    require_absent(&identity.name(ROLE)).unwrap();
}

#[test]
fn empty_job_collision_is_not_configured_or_terminated() {
    let identity = identity();
    let name = wstr(&identity.name(ROLE));
    let original = OwnedHandle(unsafe { CreateJobObjectW(None, pcwstr(&name)).unwrap() });
    let db = database(&identity);
    let journal = live(&db, &identity, true);
    assert!(journal.create_empty_bootstrap_job().is_err());
    let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
    unsafe {
        QueryInformationJobObject(
            Some(original.raw()),
            JobObjectExtendedLimitInformation,
            (&mut limits as *mut JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
            size_of_val(&limits) as u32,
            None,
        )
        .unwrap();
    }
    assert_eq!(
        limits.BasicLimitInformation.LimitFlags.0, 0,
        "collision must remain unconfigured"
    );
    assert!(matches!(
        journal.replay().unwrap().states[&ROLE],
        State::Creating { .. }
    ));
    assert!(stopped(&db, &identity).recovered().is_err());
    drop(original);
    require_absent(&identity.name(ROLE)).unwrap();
}

#[test]
fn empty_job_write_failures_preserve_owned_handle_and_exact_nonce() {
    let identity = identity();
    let db = database(&identity);
    let journal = live(&db, &identity, true);
    fail_inserts(&db);
    assert!(journal.create_empty_bootstrap_job().is_err());
    require_absent(&identity.name(ROLE)).unwrap();
    assert_eq!(journal.replay().unwrap().events, 0);
    resume_inserts(&db);
    let mut guard = journal.create_empty_bootstrap_job().unwrap();
    fail_inserts(&db);
    assert!(journal.record_empty_bootstrap_job(&mut guard).is_err());
    guard.verify_open().unwrap();
    assert!(matches!(
        journal.replay().unwrap().states[&ROLE],
        State::Creating { .. }
    ));
    resume_inserts(&db);
    guard.fault = Some(Fault::CreatedAfterCommit);
    assert!(journal.record_empty_bootstrap_job(&mut guard).is_err());
    journal.record_empty_bootstrap_job(&mut guard).unwrap();
    assert_eq!(journal.replay().unwrap().events, 2);
    drop(journal);
    let recovery = stopped(&db, &identity);
    fail_inserts(&db);
    assert!(recovery.retire_empty_bootstrap_job(&mut guard).is_err());
    let nonce = guard.remove_nonce.clone().unwrap();
    assert_eq!(guard.close_calls, 0);
    guard.verify_open().unwrap();
    resume_inserts(&db);
    guard.fault = Some(Fault::RemoveIntentAfterCommit);
    assert!(recovery.retire_empty_bootstrap_job(&mut guard).is_err());
    assert_eq!(guard.remove_nonce.as_ref(), Some(&nonce));
    assert_eq!(guard.close_calls, 0);
    guard.fault = Some(Fault::RemovedAfterCommit);
    assert!(recovery.retire_empty_bootstrap_job(&mut guard).is_err());
    assert_eq!(guard.close_calls, 1);
    recovery.retire_empty_bootstrap_job(&mut guard).unwrap();
    assert_eq!(guard.close_calls, 1);
    assert_eq!(recovery.replay().unwrap().events, 4);
}

#[test]
fn empty_job_retained_reference_and_foreign_replacement_keep_close_proof_pending() {
    let identity = identity();
    let db = database(&identity);
    let journal = live(&db, &identity, true);
    let mut guard = journal.create_empty_bootstrap_job().unwrap();
    journal.record_empty_bootstrap_job(&mut guard).unwrap();
    let name = wstr(&identity.name(ROLE));
    let retained =
        OwnedHandle(unsafe { OpenJobObjectW(JOB_OBJECT_QUERY, false, pcwstr(&name)).unwrap() });
    drop(journal);
    let recovery = stopped(&db, &identity);
    assert!(recovery.retire_empty_bootstrap_job(&mut guard).is_err());
    assert!(matches!(guard.phase, Phase::Closed));
    assert_eq!(guard.close_calls, 1);
    drop(retained);
    let replacement = OwnedHandle(unsafe { CreateJobObjectW(None, pcwstr(&name)).unwrap() });
    assert!(recovery.retire_empty_bootstrap_job(&mut guard).is_err());
    assert_eq!(guard.close_calls, 1);
    assert!(matches!(
        recovery.replay().unwrap().states[&ROLE],
        State::Removing { .. }
    ));
    drop(replacement);
    fail_inserts(&db);
    assert!(recovery.retire_empty_bootstrap_job(&mut guard).is_err());
    assert_eq!(guard.close_calls, 1);
    resume_inserts(&db);
    recovery.retire_empty_bootstrap_job(&mut guard).unwrap();
    assert_eq!(guard.close_calls, 1);
}

#[test]
fn empty_job_changed_policy_and_history_are_never_closed_as_empty() {
    let identity = identity();
    let db = database(&identity);
    let journal = live(&db, &identity, true);
    let mut guard = journal.create_empty_bootstrap_job().unwrap();
    journal.record_empty_bootstrap_job(&mut guard).unwrap();
    let ui = JOBOBJECT_BASIC_UI_RESTRICTIONS {
        UIRestrictionsClass: JOB_OBJECT_UILIMIT_NONE,
    };
    unsafe {
        SetInformationJobObject(
            handle(&guard),
            JobObjectBasicUIRestrictions,
            (&ui as *const JOBOBJECT_BASIC_UI_RESTRICTIONS).cast(),
            size_of_val(&ui) as u32,
        )
        .unwrap();
    }
    let recovery = stopped(&db, &identity);
    assert!(recovery.retire_empty_bootstrap_job(&mut guard).is_err());
    assert_eq!(guard.close_calls, 0);
    assert_eq!(recovery.replay().unwrap().events, 2);
    let ui = JOBOBJECT_BASIC_UI_RESTRICTIONS {
        UIRestrictionsClass: ui_policy(),
    };
    unsafe {
        SetInformationJobObject(
            handle(&guard),
            JobObjectBasicUIRestrictions,
            (&ui as *const JOBOBJECT_BASIC_UI_RESTRICTIONS).cast(),
            size_of_val(&ui) as u32,
        )
        .unwrap();
    }
    for (total, active) in [(1, 0), (0, 1), (1, 1)] {
        let mut accounting = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
        accounting.TotalProcesses = total;
        accounting.ActiveProcesses = active;
        assert!(require_empty_history(&accounting).is_err());
    }
    recovery.retire_empty_bootstrap_job(&mut guard).unwrap();
}

#[test]
fn empty_job_loss_of_original_guard_never_retires_from_name_absence() {
    for acknowledge in [false, true] {
        let identity = identity();
        let db = database(&identity);
        let journal = live(&db, &identity, true);
        let mut guard = journal.create_empty_bootstrap_job().unwrap();
        if acknowledge {
            journal.record_empty_bootstrap_job(&mut guard).unwrap();
        }
        drop(guard);
        drop(journal);
        require_absent(&identity.name(ROLE)).unwrap();
        let recovery = stopped(&db, &identity);
        assert!(recovery.recovered().is_err());
        assert_eq!(
            recovery.replay().unwrap().events,
            if acknowledge { 2 } else { 1 }
        );
        // No deserialize/open-by-name constructor exists to produce the
        // original EmptyBootstrapJob needed by the retirement method.
        assert!(
            live(&db, &identity, false)
                .create_empty_bootstrap_job()
                .is_err()
        );
    }
}

#[test]
fn empty_job_original_guard_survives_file_database_cold_reopen() {
    let identity = identity();
    let root = crate::util::test_temp_dir().join(format!(
        "alysis-empty-job-{}",
        super::super::super::random_id().unwrap()
    ));
    std::fs::create_dir(&root).unwrap();
    let path = root.join("state.db");
    let path = format!(
        r"\\?\{}",
        path.to_str().unwrap().trim_start_matches(r"\\?\")
    );
    let mut guard;
    {
        let mut db = Connection::open_with_flags_and_vfs(
            &path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE | rusqlite::OpenFlags::SQLITE_OPEN_CREATE,
            "win32-longpath",
        )
        .unwrap();
        db.execute_batch("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL;")
            .unwrap();
        crate::account_state::initialize_header(&mut db, &identity.execution.identity.binding)
            .unwrap();
        let journal = live(&db, &identity, true);
        guard = journal.create_empty_bootstrap_job().unwrap();
        journal.record_empty_bootstrap_job(&mut guard).unwrap();
    }
    {
        let db = Connection::open_with_flags_and_vfs(
            &path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE,
            "win32-longpath",
        )
        .unwrap();
        let recovery = stopped(&db, &identity);
        recovery.retire_empty_bootstrap_job(&mut guard).unwrap();
        recovery.recovered().unwrap();
        assert_eq!(recovery.replay().unwrap().events, 4);
    }
    assert_eq!(guard.close_calls, 1);
    std::fs::remove_file(&path).unwrap();
    std::fs::remove_dir(root).unwrap();
}

#[test]
fn empty_job_uncertain_close_never_retries_or_retires_from_absence() {
    let identity = identity();
    let db = database(&identity);
    let journal = live(&db, &identity, true);
    let mut guard = journal.create_empty_bootstrap_job().unwrap();
    journal.record_empty_bootstrap_job(&mut guard).unwrap();
    drop(journal);
    let recovery = stopped(&db, &identity);
    let nonce = super::super::super::random_id().unwrap();
    recovery
        .append(Event::RemoveIntent {
            resource: ROLE,
            nonce: nonce.clone(),
        })
        .unwrap();
    guard.remove_nonce = Some(nonce);
    let Phase::Open(handle) = std::mem::replace(&mut guard.phase, Phase::CloseUncertain) else {
        unreachable!()
    };
    // Model the uncertainty boundary after one attempted close. The test
    // knows its own close succeeded; production is deliberately not allowed
    // to infer that from later name absence if its close result was uncertain.
    unsafe {
        CloseHandle(handle.into_raw()).unwrap();
    }
    guard.close_calls = 1;
    require_absent(&identity.name(ROLE)).unwrap();
    assert!(recovery.retire_empty_bootstrap_job(&mut guard).is_err());
    assert!(recovery.retire_empty_bootstrap_job(&mut guard).is_err());
    assert_eq!(guard.close_calls, 1);
    assert_eq!(recovery.replay().unwrap().events, 3);
    assert!(recovery.recovered().is_err());
}

#[test]
fn empty_job_guard_must_match_original_nonce_and_complete_execution() {
    let identity = identity();
    let db = database(&identity);
    let journal = live(&db, &identity, true);
    let mut guard = journal.create_empty_bootstrap_job().unwrap();
    let original_nonce = guard.create_nonce.clone();
    guard.create_nonce = super::super::super::random_id().unwrap();
    assert!(journal.record_empty_bootstrap_job(&mut guard).is_err());
    assert_eq!(journal.replay().unwrap().events, 1);
    guard.create_nonce = original_nonce;
    journal.record_empty_bootstrap_job(&mut guard).unwrap();
    let mut foreign = identity.clone();
    foreign.execution.pending_id[0] ^= 0x55;
    let foreign_db = database(&foreign);
    drop(live(&foreign_db, &foreign, true));
    assert!(
        stopped(&foreign_db, &foreign)
            .retire_empty_bootstrap_job(&mut guard)
            .is_err()
    );
    assert_eq!(guard.close_calls, 0);
    guard.verify_open().unwrap();
    stopped(&db, &identity)
        .retire_empty_bootstrap_job(&mut guard)
        .unwrap();
}

// Only exact handles born in the current test are ever changed here. No name
// lookup acquires mutation authority, and each fixture owns its cleanup handle.
fn set_owned_fixture_dacl(handle: HANDLE, descriptor: &OwnedSd, protected: bool) {
    let mut present = false.into();
    let mut defaulted = false.into();
    let mut dacl = std::ptr::null_mut();
    unsafe {
        GetSecurityDescriptorDacl(descriptor.ptr, &mut present, &mut dacl, &mut defaulted).unwrap();
    }
    assert!(present.as_bool() && !dacl.is_null());
    win32_ok(
        unsafe {
            SetSecurityInfo(
                handle,
                SE_KERNEL_OBJECT,
                DACL_SECURITY_INFORMATION
                    | if protected {
                        PROTECTED_DACL_SECURITY_INFORMATION
                    } else {
                        UNPROTECTED_DACL_SECURITY_INFORMATION
                    },
                None,
                None,
                Some(dacl),
                None,
            )
        },
        "mutate exact owned empty-job test fixture",
    )
    .unwrap();
}

#[test]
fn empty_job_actual_descriptor_and_inheritance_drift_refuse_before_close() {
    for defect in ["dacl", "protection", "handle_inheritance"] {
        let identity = identity();
        let db = database(&identity);
        let journal = live(&db, &identity, true);
        let mut guard = journal.create_empty_bootstrap_job().unwrap();
        journal.record_empty_bootstrap_job(&mut guard).unwrap();
        let original = descriptor(&identity).unwrap();
        match defect {
            "dacl" => {
                let changed = OwnedSd::from_sddl(&format!(
                    "O:{}D:P(A;;0x{JOB_ALL:x};;;{})",
                    identity.execution.owner, identity.execution.owner
                ))
                .unwrap();
                set_owned_fixture_dacl(handle(&guard), &changed, true);
            }
            "protection" => set_owned_fixture_dacl(handle(&guard), &original, false),
            "handle_inheritance" => unsafe {
                SetHandleInformation(handle(&guard), HANDLE_FLAG_INHERIT.0, HANDLE_FLAG_INHERIT)
                    .unwrap();
            },
            _ => unreachable!(),
        }
        assert!(guard.verify_open().is_err(), "unobserved {defect} drift");
        let recovery = stopped(&db, &identity);
        assert!(
            recovery.retire_empty_bootstrap_job(&mut guard).is_err(),
            "retired {defect} drift"
        );
        assert_eq!(guard.close_calls, 0);
        assert!(matches!(guard.phase, Phase::Open(_)));
        assert_eq!(recovery.replay().unwrap().events, 2);
        assert!(guard.remove_nonce.is_none());
        if defect == "handle_inheritance" {
            unsafe {
                SetHandleInformation(handle(&guard), HANDLE_FLAG_INHERIT.0, HANDLE_FLAGS(0))
                    .unwrap();
            }
        } else {
            set_owned_fixture_dacl(handle(&guard), &original, true);
        }
        guard.verify_open().unwrap();
        recovery.retire_empty_bootstrap_job(&mut guard).unwrap();
        assert_eq!(guard.close_calls, 1);
        recovery.recovered().unwrap();
    }
}

#[test]
fn empty_job_access_denied_name_query_preserves_original_close_proof() {
    let identity = identity();
    let db = database(&identity);
    let journal = live(&db, &identity, true);
    let mut guard = journal.create_empty_bootstrap_job().unwrap();
    journal.record_empty_bootstrap_job(&mut guard).unwrap();
    let name = wstr(&identity.name(ROLE));
    let retained =
        OwnedHandle(unsafe { OpenJobObjectW(JOB_OBJECT_QUERY, false, pcwstr(&name)).unwrap() });
    let recovery = stopped(&db, &identity);
    assert!(recovery.retire_empty_bootstrap_job(&mut guard).is_err());
    assert_eq!(guard.close_calls, 1);
    assert!(matches!(guard.phase, Phase::Closed));
    drop(retained);
    let replacement = OwnedHandle(unsafe { CreateJobObjectW(None, pcwstr(&name)).unwrap() });
    assert_ne!(unsafe { GetLastError() }, ERROR_ALREADY_EXISTS);
    let inaccessible = OwnedSd::from_sddl(&format!(
        "O:{}D:P(D;;0x4;;;{})(A;;0x{JOB_ALL:x};;;SY)(A;;0x{JOB_ALL:x};;;{})",
        identity.execution.owner, identity.execution.owner, identity.execution.owner
    ))
    .unwrap();
    set_owned_fixture_dacl(replacement.raw(), &inaccessible, true);
    // Establish the actual native denial independently of require_absent.
    let error = unsafe { OpenJobObjectW(JOB_OBJECT_QUERY, false, pcwstr(&name)) }.unwrap_err();
    assert_eq!(error.code(), ERROR_ACCESS_DENIED.to_hresult());
    assert!(recovery.retire_empty_bootstrap_job(&mut guard).is_err());
    assert!(matches!(guard.phase, Phase::Closed));
    assert_eq!(guard.close_calls, 1);
    assert_eq!(recovery.replay().unwrap().events, 3);
    assert!(recovery.recovered().is_err());
    drop(replacement); // Test-owned reference; the adapter never adopts it.
    recovery.retire_empty_bootstrap_job(&mut guard).unwrap();
    assert_eq!(guard.close_calls, 1);
    recovery.recovered().unwrap();
}

#[test]
fn empty_job_consistently_rewritten_history_cannot_rebind_retained_proof() {
    let identity = identity();
    let db = database(&identity);
    let journal = live(&db, &identity, true);
    let mut guard = journal.create_empty_bootstrap_job().unwrap();
    journal.record_empty_bootstrap_job(&mut guard).unwrap();
    // Mutate matching pairs so replay is internally valid. The original
    // retained guard must still reject the different durable operation.
    let replace_nonce = |first: usize, nonce: &str| {
        for sequence in first..first + 2 {
            let json: String = db.query_row(
                "SELECT event_json FROM candidate_resource_events WHERE container=?1 AND sequence=?2",
                params![identity.execution.identity.container, sequence], |row| row.get(0)).unwrap();
            let mut event: Event = serde_json::from_str(&json).unwrap();
            match &mut event {
                Event::CreateIntent { nonce: value, .. }
                | Event::Created { nonce: value, .. }
                | Event::RemoveIntent { nonce: value, .. }
                | Event::Removed { nonce: value, .. } => *value = nonce.into(),
            }
            db.execute("UPDATE candidate_resource_events SET event_json=?1 WHERE container=?2 AND sequence=?3",
                params![serde_json::to_string(&event).unwrap(), identity.execution.identity.container, sequence]).unwrap();
        }
    };
    replace_nonce(0, &super::super::super::random_id().unwrap());
    assert!(matches!(
        journal.replay().unwrap().states[&ROLE],
        State::Present { .. }
    ));
    assert!(journal.record_empty_bootstrap_job(&mut guard).is_err());
    assert!(
        stopped(&db, &identity)
            .retire_empty_bootstrap_job(&mut guard)
            .is_err()
    );
    assert_eq!(guard.close_calls, 0);
    replace_nonce(0, &guard.create_nonce);
    let recovery = stopped(&db, &identity);
    recovery.retire_empty_bootstrap_job(&mut guard).unwrap();
    replace_nonce(2, &super::super::super::random_id().unwrap());
    assert!(matches!(
        recovery.replay().unwrap().states[&ROLE],
        State::Removed
    ));
    assert!(recovery.retire_empty_bootstrap_job(&mut guard).is_err());
    assert_eq!(guard.close_calls, 1);
    replace_nonce(2, guard.remove_nonce.as_ref().unwrap());
    recovery.retire_empty_bootstrap_job(&mut guard).unwrap();
    assert_eq!(guard.close_calls, 1);
}
