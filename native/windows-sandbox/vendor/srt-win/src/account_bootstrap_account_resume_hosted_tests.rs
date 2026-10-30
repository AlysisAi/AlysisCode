//! Real account execution remains a fixed read-only self-check. These fixtures
//! never dispatch a user command, touch installed credentials or claim readiness.
use super::*;
use bootstrap_plan::lifecycle::resume::logon::delivery::birth::{
    BIRTH_NATIVE_CHECKPOINT, HANDOFF_SELF_CHECK, PRIVATE_STATION_SELF_CHECK,
    UNUSED_PROFILE_SELF_CHECK, account_resume::ACCOUNT_RESUME_CHECKPOINT,
    account_resume::account_profile::ACCOUNT_PROFILE_CHECKPOINT,
};
use std::{cell::RefCell, rc::Rc};

pub(super) fn run(principals: &Principals, password: &Password, case: &str) -> Result<()> {
    struct Reset;
    impl Drop for Reset {
        fn drop(&mut self) {
            HANDOFF_SELF_CHECK.with(|s| s.set(false));
            PRIVATE_STATION_SELF_CHECK.with(|s| s.set(false));
            UNUSED_PROFILE_SELF_CHECK.with(|s| s.set(false));
            ACCOUNT_PROFILE_CHECKPOINT.with(|s| {
                s.borrow_mut().take();
            });
            BIRTH_NATIVE_CHECKPOINT.with(|s| {
                s.borrow_mut().take();
            });
            ACCOUNT_RESUME_CHECKPOINT.with(|s| {
                s.borrow_mut().take();
            });
        }
    }
    let fixture = bound_fixture(
        principals.offline.as_deref().unwrap(),
        principals.online.as_deref().unwrap(),
        principals.group.as_deref().unwrap(),
    )?;
    let db = protected_database(&fixture);
    let ready = ReadyInstallation::fixture(fixture.setup.clone(), &fixture.root)?;
    let active = atomic_fixture_active(&fixture, &db);
    let marker = std::fs::read(fixture.path())?;
    let image = PinnedRunnerImage::prepare(&ready)?;
    let (server, address) =
        ControlServer::prepare(&ready.binding(AccountMode::Offline)?.account_sid)?;
    let plan = bootstrap_plan::CommittedPlan::prepare(&db, &active, &image, &ready, &address)?;
    let job = plan.create_job()?;
    let observer = Rc::new(RefCell::new(None));
    let captured = observer.clone();
    let selected = case.to_owned();
    let _reset = Reset;
    HANDOFF_SELF_CHECK.with(|s| s.set(case == "self-check-handoff"));
    PRIVATE_STATION_SELF_CHECK.with(|s| s.set(case == "private-station-success"));
    UNUSED_PROFILE_SELF_CHECK.with(|s| s.set(case.starts_with("account-profile-")));
    BIRTH_NATIVE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, exit, sql| {
            if phase == "observed" {
                ensure!(
                    captured.borrow().is_none(),
                    "self-check child observed twice"
                );
                *captured.borrow_mut() = Some(exit);
            }
            if phase == "acknowledged" && selected == "self-check-intent-failure" {
                sql.execute_batch("PRAGMA query_only=ON")?;
            }
            Ok(())
        }));
    });
    let selected = case.to_owned();
    ACCOUNT_RESUME_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, seq, sql| {
            if phase == "committed"
                && ((seq == 0 && selected == "self-check-resumed-receipt-failure")
                    || (seq == 1 && selected == "self-check-exit-receipt-failure"))
            {
                sql.execute_batch("PRAGMA query_only=ON").unwrap();
            }
            if phase == "committed" && seq == 1 && selected == "account-profile-intent-failure" {
                sql.execute_batch("PRAGMA query_only=ON").unwrap();
            }
            if phase == "committed" && seq == 0 && selected == "self-check-intent-readback-drift" {
                sql.execute_batch("DROP TRIGGER atomic_account_resume_no_update")
                    .unwrap();
            }
        }));
    });
    let selected = case.to_owned();
    ACCOUNT_PROFILE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, seq, sql| {
            if phase == "committed"
                && matches!(
                    (seq, selected.as_str()),
                    (0, "account-profile-created-receipt-failure")
                        | (1, "account-profile-remove-intent-failure")
                        | (2, "account-profile-removed-receipt-failure")
                )
            {
                sql.execute_batch("PRAGMA query_only=ON").unwrap();
            }
            if phase == "committed"
                && seq == 0
                && selected == "account-profile-intent-readback-drift"
            {
                sql.execute_batch("DROP TRIGGER atomic_account_profile_no_update")
                    .unwrap();
            }
        }));
    });
    // Only the bound loaded-profile birth status has its dedicated60s wait;
    // bootstrap/credentials/ordinary IPC and retirement retain their10s bound.
    let timeout = Duration::from_secs(10);
    let started = std::time::Instant::now();
    let result = job.fixture_run_account_self_check(server, &password.0, timeout);
    eprintln!(
        "fixed_account_case={case} elapsed_ms={} passed={}",
        started.elapsed().as_millis(),
        result.is_ok()
    );
    ensure!(
        atomic_job_counts(active.record()?).is_none(),
        "self-check job remains open"
    );
    // Preserve a pre-observation refusal instead of replacing its native cause
    // with the expected consequence that no original child was offered.
    let observed = observer.borrow_mut().take();
    let child_exit = match observed {
        Some(child) => child.fixture_wait_code()?,
        None => {
            result.context("self-check refused before original child observation")?;
            anyhow::bail!("successful self-check lacks original child observation");
        }
    };
    ensure!(
        std::fs::read(fixture.path())? == marker,
        "self-check marker changed"
    );
    let rows: usize = db.connection()?.query_row(
        "SELECT count(*) FROM candidate_atomic_account_resume",
        [],
        |r| r.get(0),
    )?;
    let retired: usize = db.connection()?.query_row(
        "SELECT count(*) FROM candidate_atomic_job_retirement",
        [],
        |r| r.get(0),
    )?;
    let profile_rows: usize = db.connection()?.query_row(
        "SELECT count(*) FROM candidate_atomic_account_profile",
        [],
        |r| r.get(0),
    )?;
    if matches!(
        case,
        "self-check-success"
            | "self-check-handoff"
            | "account-profile-success"
            | "private-station-success"
    ) {
        let observation = result.with_context(|| format!(
            "fixed self-check refused after original job closure: resume_rows={rows}, profile_rows={profile_rows}, retirement_rows={retired}, original_child_exit=0x{child_exit:08x}"))?;
        ensure!(
            profile_rows
                == if case == "account-profile-success" {
                    4
                } else {
                    0
                },
            "account profile completion prefix differs"
        );
        ensure!(
            observation.image_id == image.file_identity()? && rows == 3 && retired == 2,
            "self-check completion evidence incomplete"
        );
        let last: String = db.connection()?.query_row(
            "SELECT event_json FROM candidate_atomic_account_resume WHERE sequence=2",
            [],
            |r| r.get(0),
        )?;
        let value: serde_json::Value = serde_json::from_str(&last)?;
        ensure!(value["kind"] == "exited" && value["code"] == crate::account_control::bootstrap::entry::credential_wire::child::probe::EXIT_CODE,
            "self-check original exit not recorded");
    } else {
        let error = result
            .err()
            .context("self-check fault incorrectly succeeded")?;
        let expected = match case {
            "self-check-intent-failure" => 0,
            "self-check-resumed-receipt-failure" | "self-check-intent-readback-drift" => 1,
            "self-check-exit-receipt-failure" => 2,
            _ if case.starts_with("account-profile-") => 2,
            _ => anyhow::bail!("unknown self-check case"),
        };
        ensure!(
            rows == expected && retired == 0,
            "self-check failed prefix differs"
        );
        if case.starts_with("account-profile-") {
            let expected = match case {
                "account-profile-intent-failure" => 0,
                "account-profile-created-receipt-failure"
                | "account-profile-intent-readback-drift" => 1,
                "account-profile-remove-intent-failure" => 2,
                "account-profile-removed-receipt-failure" => 3,
                _ => anyhow::bail!("unknown account profile fault"),
            };
            ensure!(
                profile_rows == expected,
                "account profile fault prefix differs: {profile_rows}"
            );
        }
        if case == "self-check-intent-readback-drift"
            || case == "account-profile-intent-readback-drift"
        {
            let expected = if case == "account-profile-intent-readback-drift" {
                "account profile schema differs"
            } else {
                "account resume schema differs"
            };
            ensure!(
                error.to_string().contains(expected),
                "self-check drift failed for other reason"
            );
        } else {
            ensure!(error.chain().any(|c| c.downcast_ref::<rusqlite::Error>().is_some_and(|e|
                matches!(e,rusqlite::Error::SqliteFailure(e,_) if e.code == rusqlite::ErrorCode::ReadOnly))),
                "self-check write fault failed for another reason: {error:#}");
        }
    }
    Ok(())
}
