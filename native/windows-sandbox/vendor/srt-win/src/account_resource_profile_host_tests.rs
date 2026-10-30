//! One exclusive unused profile on an explicitly admitted disposable runner.
//! Partial failures preserve the SDK state and on-disk journal; no guessed repair.
use super::*;
const SENTINEL: &str = "github-hosted-unused-profile-only";
const TEST: &str = "candidate_unused_profile_disposable_lifecycle";

fn admitted(values: &[Option<String>], run: &str, attempt: &str) -> bool {
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
            .all(|(v, x)| v.as_deref() == Some(x))
        && [run, attempt]
            .iter()
            .all(|s| !s.is_empty() && s.len() <= 24 && s.bytes().all(|b| b.is_ascii_digit()))
        && cfg!(all(windows, target_arch = "x86_64"))
}
#[test]
fn unused_profile_disposable_admission_requires_every_gate() {
    let good = [
        SENTINEL,
        "true",
        "github-hosted",
        "Windows",
        "AlysisAi/alysis-code-internal",
        "workflow_dispatch",
    ]
    .map(|v| Some(v.to_owned()));
    assert!(admitted(&good, "12", "1"));
    for i in 0..good.len() {
        let mut bad = good.clone();
        bad[i] = None;
        assert!(!admitted(&bad, "12", "1"));
        bad[i] = Some("wrong".into());
        assert!(!admitted(&bad, "12", "1"));
    }
    for bad in ["", "x", "1234567890123456789012345"] {
        assert!(!admitted(&good, bad, "1"));
        assert!(!admitted(&good, "1", bad));
    }
}

fn disk(path: &std::path::Path, id: &Identity) -> Connection {
    std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .unwrap();
    let mut db = Connection::open(path).unwrap();
    db.execute_batch("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL;")
        .unwrap();
    crate::account_state::initialize_header(&mut db, &id.execution.identity.binding).unwrap();
    db
}

#[test]
#[ignore = "explicit disposable hosted unused-profile lifecycle qualification"]
fn candidate_unused_profile_disposable_lifecycle() {
    let values = [
        "ALYSIS_RUN_DISPOSABLE_UNUSED_PROFILE",
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
        admitted(&values, &run, &attempt),
        "disposable unused-profile admission refused"
    );
    let directory =
        std::path::PathBuf::from(std::env::var_os("RUNNER_TEMP").expect("CI temp required"));
    assert!(directory.is_absolute() && directory.is_dir());
    let output = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(directory.join("alysis-unused-profile-lifecycle.json"))
        .unwrap();
    let mut cases = Vec::new();
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| -> Result<()> {
        let id = identity();
        let path = directory.join(format!(
            "unused-profile-{}.db",
            super::super::super::super::random_id()?
        ));
        let db = disk(&path, &id);
        let j = live(&db, &id, true);
        let mut guard = j.prepare_unused_host_profile()?;
        reject(&db);
        ensure!(
            j.create_unused_host_profile(&mut guard).is_err(),
            "rejected intent reached SDK"
        );
        ensure!(
            guard.native.counts() == (0, 0, 0) && j.replay()?.events == 0,
            "effect before intent"
        );
        accept(&db);
        cases.push("failed_intent_prevents_sdk_birth");
        j.create_unused_host_profile(&mut guard)?;
        guard.native.unchanged()?;
        ensure!(guard.native.counts() == (1, 0, 0), "birth count mismatch");
        cases.push("exclusive_birth_retains_both_storage_surfaces");

        let collision_path = directory.join(format!(
            "unused-profile-collision-{}.db",
            super::super::super::super::random_id()?
        ));
        let collision_db = disk(&collision_path, &id);
        let collision_journal = live(&collision_db, &id, true);
        let mut collision = collision_journal.prepare_unused_host_profile()?;
        let error = collision_journal
            .create_unused_host_profile(&mut collision)
            .err()
            .context("profile collision accepted")?;
        ensure!(
            format!("{error:#}").contains("800700b7"),
            "collision failed for another reason"
        );
        ensure!(
            collision.native.counts() == (1, 0, 0) && collision_journal.replay()?.events == 1,
            "collision changed journal"
        );
        ensure!(
            collision_journal
                .create_unused_host_profile(&mut collision)
                .is_err(),
            "uncertain birth was retried"
        );
        guard.native.unchanged()?;
        cases.push("existing_profile_refused_and_original_unchanged");

        guard.operation.fault = Some(Fault::CreatedCommitted);
        ensure!(
            j.record_unused_host_profile(&mut guard).is_err(),
            "missing receipt fault"
        );
        j.record_unused_host_profile(&mut guard)?;
        ensure!(
            guard.native.counts() == (1, 0, 0),
            "receipt retry recreated profile"
        );
        for role in ALL {
            ensure!(
                j.create_intent(role, None).is_err(),
                "unused profile admitted another resource"
            );
        }
        cases.push("birth_receipt_retry_never_unlocks_execution");
        drop(j);
        drop(db);

        let db = Connection::open(&path)?;
        db.execute_batch("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL;")?;
        let j = live(&db, &id, false);
        ensure!(
            j.prepare_unused_host_profile().is_err(),
            "journal reconstructed profile ownership"
        );
        ensure!(
            stopped(&db, &id).recovered().is_err(),
            "pending profile retired from receipt alone"
        );
        guard.native.unchanged()?;
        j.record_unused_host_profile(&mut guard)?;
        cases.push("database_reopen_requires_original_guard");

        let stopped = stopped(&db, &id);
        guard.operation.fault = Some(Fault::RemoveCommitted);
        ensure!(
            stopped.retire_unused_host_profile(&mut guard).is_err(),
            "missing removal fault"
        );
        ensure!(
            guard.native.counts() == (1, 0, 0),
            "close/delete preceded removal intent"
        );
        reject(&db);
        ensure!(
            stopped.retire_unused_host_profile(&mut guard).is_err(),
            "rejected removal receipt accepted"
        );
        ensure!(
            guard.operation.phase == Phase::Deleted && guard.native.counts() == (1, 1, 1),
            "native deletion failed before receipt test"
        );
        guard.native.absent()?;
        ensure!(
            stopped.replay()?.events == 3 && stopped.recovered().is_err(),
            "unrecorded cleanup retired journal"
        );
        accept(&db);
        cases.push("checked_close_and_single_delete_precede_absence_receipt");

        guard.operation.fault = Some(Fault::RemovedCommitted);
        ensure!(
            stopped.retire_unused_host_profile(&mut guard).is_err(),
            "missing final commit fault"
        );
        stopped.retire_unused_host_profile(&mut guard)?;
        ensure!(
            guard.native.counts() == (1, 1, 1) && stopped.replay()?.events == 4,
            "receipt retry repeated effects"
        );
        ensure!(
            stopped.recovered()?.matches(&id.execution),
            "retired execution identity differs"
        );
        cases.push("exact_final_receipt_without_repeated_delete");
        Ok(())
    }));
    let report = serde_json::json!({"test": TEST, "run_id": run, "run_attempt": attempt,
        "passed": matches!(&result, Ok(Ok(()))), "completed_cases": cases,
        "synthetic_profile_limit": 1, "no_account_or_process_creation": true,
        "no_shared_acl_changes": true, "standalone_unused_only": true,
        "execution_profile_receipts_enabled": false, "cold_adoption_enabled": false,
        "collision_intent_preserved": true});
    serde_json::to_writer_pretty(output, &report).unwrap();
    match result {
        Ok(value) => value.unwrap(),
        Err(panic) => std::panic::resume_unwind(panic),
    }
}
