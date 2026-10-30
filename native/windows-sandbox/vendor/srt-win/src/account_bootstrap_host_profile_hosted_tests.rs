//! Disposable-only host SDK profile effects around a real suspended account.
//! Deliberate failures retain unresolved SDK state, never guessed cleanup.
use super::*;
use crate::account_profile_filesystem::RetainedProfileFilesystem;
use crate::account_state::execution::resources::station::Station;
use bootstrap_plan::host_profile::HOST_PROFILE_CHECKPOINT;
use bootstrap_plan::lifecycle::resume::logon::delivery::birth::{
    BIRTH_NATIVE_CHECKPOINT, retirement::RETIREMENT_CHECKPOINT,
};
use std::{cell::RefCell, rc::Rc};

pub(super) fn run(principals: &Principals, password: &Password, case: &str) -> Result<()> {
    super::station::require_station_host()?;
    ensure!(
        std::env::var("ALYSIS_RUN_DISPOSABLE_ATOMIC_PROFILE")
            .ok()
            .as_deref()
            == Some("github-hosted-atomic-profile-only"),
        "atomic host profile effect admission refused"
    );
    struct Reset;
    impl Drop for Reset {
        fn drop(&mut self) {
            HOST_PROFILE_CHECKPOINT.with(|s| {
                s.borrow_mut().take();
            });
            BIRTH_NATIVE_CHECKPOINT.with(|s| {
                s.borrow_mut().take();
            });
            RETIREMENT_CHECKPOINT.with(|s| {
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
    let record = active.record()?.clone();
    let marker = std::fs::read(fixture.path())?;
    let image = PinnedRunnerImage::prepare(&ready)?;
    let (server, address) =
        ControlServer::prepare(&ready.binding(AccountMode::Offline)?.account_sid)?;
    let plan = bootstrap_plan::CommittedPlan::prepare(&db, &active, &image, &ready, &address)?;
    let before = Station::fixture_snapshot_current()?;
    let after = crate::account_state::execution::resources::station::proposed(
        &before,
        &record.identity.package_sid,
    )?;
    let job = plan.create_job()?.with_empty_namespace()?;
    let native_before =
        crate::account_state::execution::resources::NativeHostProfile::fixture_effect_counts();
    let phases = Rc::new(RefCell::new(Vec::<String>::new()));
    let captured_phases = phases.clone();
    let selected = case.to_owned();
    let use_profile = case.starts_with("host-profile-used-");
    let success = matches!(
        case,
        "host-profile-success" | "host-profile-used-success" | "host-profile-used-registry-success"
    );
    let canary = fixture.root.join("profile-outside-canary");
    std::fs::write(&canary, b"untouched profile canary")?;
    let retained_canary = canary.clone();
    let _reset = Reset;
    HOST_PROFILE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, sql, used| {
            captured_phases.borrow_mut().push(phase.into());
            if phase == "using" {
                if selected.starts_with("host-profile-used-registry-") {
                    used.context("original used profile missing at mutation")?
                        .fixture_add_registry_data(
                            selected == "host-profile-used-registry-refusal",
                        )?;
                }
                let path = RetainedProfileFilesystem::fixture_ac(
                    &record.identity.container,
                    &record.owner,
                )?
                .join("atomic-runtime-data.bin");
                if selected == "host-profile-used-capture-refusal" {
                    std::fs::hard_link(&retained_canary, &path)?;
                } else {
                    std::fs::write(&path, b"bounded synthetic profile data")?;
                }
            }
            if matches!(
                phase,
                "observed_after_use"
                    | "captured"
                    | "retiring"
                    | "closed"
                    | "deleted"
                    | "absent"
                    | "completed"
            ) {
                ensure!(
                    atomic_job_counts(&record).is_none(),
                    "host profile retirement preceded job exit"
                );
                let n: usize =
                    sql.query_row("SELECT count(*) FROM candidate_atomic_namespace", [], |r| {
                        r.get(0)
                    })?;
                let d: usize =
                    sql.query_row("SELECT count(*) FROM candidate_atomic_desktop", [], |r| {
                        r.get(0)
                    })?;
                let s: usize =
                    sql.query_row("SELECT count(*) FROM candidate_atomic_station", [], |r| {
                        r.get(0)
                    })?;
                ensure!(
                    (n, d, s) == (2, 2, 2),
                    "host profile retirement lost enclosing native resources"
                );
            }
            if phase == "born" && selected == "host-profile-birth-receipt-failure"
                || phase == "absent"
                    && matches!(
                        selected.as_str(),
                        "host-profile-removal-receipt-failure"
                            | "host-profile-used-removal-receipt-failure"
                    )
            {
                sql.execute_batch("PRAGMA query_only=ON")?;
            }
            ensure!(
                !(phase == "closed" && selected == "host-profile-post-close-refusal"),
                "injected post-close refusal"
            );
            Ok(())
        }));
    });
    let exit = Rc::new(RefCell::new(None));
    let captured = exit.clone();
    BIRTH_NATIVE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, observer, _| {
            if phase == "observed" {
                ensure!(captured.borrow().is_none(), "account observed twice");
                *captured.borrow_mut() = Some(observer);
            }
            Ok(())
        }));
    });
    if matches!(
        case,
        "host-profile-removal-intent-failure" | "host-profile-used-capture-receipt-failure"
    ) {
        RETIREMENT_CHECKPOINT.with(|s| {
            *s.borrow_mut() = Some(Box::new(|sequence, sql| {
                if sequence == 1 {
                    sql.execute_batch("PRAGMA query_only=ON").unwrap();
                }
            }));
        });
    }
    if case == "host-profile-intent-failure" {
        db.connection()?.execute_batch("PRAGMA query_only=ON")?;
    }
    let result = if use_profile {
        job.with_host_profile_for_use()
    } else {
        job.with_unused_host_profile()
    }
    .and_then(|j| j.with_station())
    .and_then(|j| j.with_unused_desktop())
    .and_then(|j| j.fixture_observe_account_birth(server, &password.0, Duration::from_secs(10)));
    drop(_reset);
    ensure!(
        result.is_ok() == success,
        "unexpected host profile composition outcome"
    );
    if let Err(error) = &result {
        if case == "host-profile-post-close-refusal" {
            ensure!(
                format!("{error:#}").contains("injected post-close refusal"),
                "post-close refusal had another cause"
            );
        } else if case == "host-profile-used-capture-refusal" {
            ensure!(
                format!("{error:#}").contains("profile hardlinked file refused"),
                "post-use refusal had another cause"
            );
        } else if case == "host-profile-used-registry-refusal" {
            ensure!(
                format!("{error:#}")
                    .contains("unsupported registry value type or extent (links refused)"),
                "registry capture refusal had another cause"
            );
        } else {
            ensure!(
                error.chain().any(|e| e
                    .downcast_ref::<rusqlite::Error>()
                    .is_some_and(|e| e.sqlite_error_code() == Some(rusqlite::ErrorCode::ReadOnly))),
                "host profile write failure had another cause"
            );
        }
    }
    ensure!(
        atomic_job_counts(active.record()?).is_none(),
        "host profile path retained original job"
    );
    let before_child = matches!(
        case,
        "host-profile-intent-failure" | "host-profile-birth-receipt-failure"
    );
    if before_child {
        ensure!(
            exit.borrow().is_none(),
            "account born before profile receipt"
        );
    } else {
        exit.borrow_mut()
            .take()
            .context("original account child unobserved")?
            .wait(Duration::from_secs(5))?;
    }
    let sql = db.connection()?;
    sql.execute_batch("PRAGMA query_only=OFF")?;
    let rows: usize = sql.query_row(
        "SELECT count(*) FROM candidate_atomic_host_profile",
        [],
        |r| r.get(0),
    )?;
    let (expected, expected_phases): (_, &[&str]) = match case {
        "host-profile-success" => (
            4,
            &[
                "born",
                "ready",
                "retiring",
                "closed",
                "deleted",
                "absent",
                "completed",
            ],
        ),
        "host-profile-intent-failure" => (0, &[]),
        "host-profile-birth-receipt-failure" => (1, &["born"]),
        "host-profile-removal-intent-failure" => (2, &["born", "ready"]),
        "host-profile-removal-receipt-failure" => (
            3,
            &["born", "ready", "retiring", "closed", "deleted", "absent"],
        ),
        "host-profile-post-close-refusal" => (3, &["born", "ready", "retiring", "closed"]),
        "host-profile-used-success" | "host-profile-used-registry-success" => (
            6,
            &[
                "born",
                "ready",
                "using",
                "observed_after_use",
                "captured",
                "retiring",
                "closed",
                "deleted",
                "absent",
                "completed",
            ],
        ),
        "host-profile-used-capture-refusal" | "host-profile-used-registry-refusal" => {
            (3, &["born", "ready", "using"])
        }
        "host-profile-used-capture-receipt-failure" => {
            (3, &["born", "ready", "using", "observed_after_use"])
        }
        "host-profile-used-removal-receipt-failure" => (
            5,
            &[
                "born",
                "ready",
                "using",
                "observed_after_use",
                "captured",
                "retiring",
                "closed",
                "deleted",
                "absent",
            ],
        ),
        _ => anyhow::bail!("unknown host profile case"),
    };
    ensure!(
        rows == expected
            && phases
                .borrow()
                .iter()
                .map(String::as_str)
                .collect::<Vec<_>>()
                == expected_phases,
        "host profile durable prefix or native phase differs"
    );
    let native_after =
        crate::account_state::execution::resources::NativeHostProfile::fixture_effect_counts();
    ensure!(
        (
            native_after.0 - native_before.0,
            native_after.1 - native_before.1,
            native_after.2 - native_before.2
        ) == (
            usize::from(case != "host-profile-intent-failure"),
            usize::from(expected_phases.contains(&"closed")),
            usize::from(expected_phases.contains(&"deleted"))
        ),
        "host profile native effect count differs"
    );
    if matches!(
        case,
        "host-profile-birth-receipt-failure"
            | "host-profile-removal-intent-failure"
            | "host-profile-post-close-refusal"
            | "host-profile-used-capture-receipt-failure"
    ) {
        // Diagnostic observation only. Never feed reopened objects into cleanup.
        let original = active.record()?;
        RetainedProfileFilesystem::open(&original.identity.container, &original.owner)?.close()?;
    }
    bootstrap_plan::namespace::fixture_require_absent(&plan)?;
    ensure!(
        Station::fixture_snapshot_current()?
            == if before_child || success {
                before
            } else {
                after
            },
        "host profile path station descriptor differs"
    );
    ensure!(
        std::fs::read(fixture.path())? == marker,
        "host profile path changed pending marker"
    );
    ensure!(
        std::fs::read(&canary)? == b"untouched profile canary",
        "profile refusal or cleanup changed outside canary"
    );
    Ok(())
}
