//! Disposable native qualification for the never-resumed system initializer.
//! Fixed synthetic account credentials and the pre-existing protected fixture.
use super::*;
use crate::account_state::execution::resources::station::Station;
use bootstrap_plan::lifecycle::resume::logon::delivery::birth::{
    BIRTH_NATIVE_CHECKPOINT, account_resume::ACCOUNT_RESUME_CHECKPOINT,
    retirement::RETIREMENT_CHECKPOINT,
};
use bootstrap_plan::{host_profile::HOST_PROFILE_CHECKPOINT, lifecycle::INITIALIZER_CHECKPOINT};
use std::{cell::RefCell, rc::Rc};

pub(super) fn run(principals: &Principals, password: &Password, case: &str) -> Result<()> {
    FAILURE_NAMESPACE.with(|s| s.borrow_mut().take());
    let result = run_inner(principals, password, case);
    if let Err(error) = &result {
        // This disposable fixture uses only synthetic accounts/workspaces. Keep
        // diagnostic extent bounded; never include credential/frame contents.
        let message: String = format!("{error:#}").chars().take(2048).collect();
        eprintln!("initializer fixture {case} failed: {message}");
    }
    result
}
fn run_inner(principals: &Principals, password: &Password, case: &str) -> Result<()> {
    super::station::require_station_host()?;
    ensure!(
        std::env::var("ALYSIS_RUN_DISPOSABLE_ATOMIC_PROFILE")
            .ok()
            .as_deref()
            == Some("github-hosted-atomic-profile-only"),
        "initializer hosted admission refused"
    );
    struct Reset;
    impl Drop for Reset {
        fn drop(&mut self) {
            bootstrap_plan::INITIALIZER_OMIT_PROFILE_ENVIRONMENT.with(|s| s.set(false));
            bootstrap_plan::lifecycle::RPC_SELF_CHECK.with(|s| s.set(false));
            INITIALIZER_CHECKPOINT.with(|s| s.borrow_mut().take());
            BIRTH_NATIVE_CHECKPOINT.with(|s| s.borrow_mut().take());
            ACCOUNT_RESUME_CHECKPOINT.with(|s| s.borrow_mut().take());
            RETIREMENT_CHECKPOINT.with(|s| s.borrow_mut().take());
            HOST_PROFILE_CHECKPOINT.with(|s| s.borrow_mut().take());
        }
    }
    let _reset = Reset;
    bootstrap_plan::lifecycle::RPC_SELF_CHECK
        .with(|s| s.set(case.starts_with("initializer-account-rpc-")));
    bootstrap_plan::INITIALIZER_OMIT_PROFILE_ENVIRONMENT
        .with(|s| s.set(case == "initializer-missing-profile-environment"));
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
    let global_before =
        crate::account_state::execution::resources::namespace_native::fixture_global_security()?;
    let after = crate::account_state::execution::resources::station::proposed(
        &before,
        &record.identity.package_sid,
    )?;
    let phases = Rc::new(RefCell::new(Vec::<String>::new()));
    let recorded = phases.clone();
    let selected = case.to_owned();
    let expected_record = record.clone();
    INITIALIZER_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, sql| {
            recorded.borrow_mut().push(phase.into());
            if matches!(phase, "releasing" | "closed" | "completed") {
                ensure!(
                    atomic_job_counts(&expected_record).is_none(),
                    "initializer keeper closed before original job exit"
                );
                for table in [
                    "candidate_atomic_namespace",
                    "candidate_atomic_desktop",
                    "candidate_atomic_station",
                ] {
                    let count: usize =
                        sql.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0))?;
                    ensure!(
                        count == 2,
                        "initializer lost outer resources before release"
                    );
                }
                let profile: usize = sql.query_row(
                    "SELECT count(*) FROM candidate_atomic_host_profile",
                    [],
                    |r| r.get(0),
                )?;
                ensure!(
                    profile == 3,
                    "initializer token released after profile capture"
                );
            }
            if phase == "born" && selected == "initializer-birth-receipt-failure"
                || phase == "stopped" && selected == "initializer-stopped-receipt-failure"
                || phase == "rpc-intent" && selected == "initializer-rpc-intent-failure"
                || phase == "rpc-granted" && selected == "initializer-rpc-receipt-failure"
                || phase == "closed" && selected == "initializer-release-receipt-failure"
            {
                sql.execute_batch("PRAGMA query_only=ON")?;
            }
            Ok(())
        }))
    });
    let exit = Rc::new(RefCell::new(None));
    let retained = exit.clone();
    let selected = case.to_owned();
    BIRTH_NATIVE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, observer, sql| {
            if phase == "observed" {
                ensure!(
                    retained.borrow().is_none(),
                    "initializer account child observed twice"
                );
                *retained.borrow_mut() = Some(observer);
            }
            if phase == "acknowledged" && selected == "initializer-account-rpc-intent-failure" {
                sql.execute_batch("PRAGMA query_only=ON")?;
            }
            Ok(())
        }))
    });
    let profile_phases = Rc::new(RefCell::new(Vec::<String>::new()));
    let captured = profile_phases.clone();
    HOST_PROFILE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, _, _| {
            captured.borrow_mut().push(phase.into());
            Ok(())
        }))
    });
    if case == "initializer-release-intent-failure" {
        RETIREMENT_CHECKPOINT.with(|s| {
            *s.borrow_mut() = Some(Box::new(|sequence, sql| {
                if sequence == 1 {
                    sql.execute_batch("PRAGMA query_only=ON").unwrap();
                }
            }))
        });
    }
    let selected = case.to_owned();
    ACCOUNT_RESUME_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, seq, sql| {
            if phase == "committed"
                && (seq == 0 && selected == "initializer-account-rpc-resumed-receipt-failure"
                    || seq == 1 && selected == "initializer-account-rpc-exit-receipt-failure")
            {
                sql.execute_batch("PRAGMA query_only=ON").unwrap();
            }
        }));
    });
    let result = plan
        .create_job()?
        .with_empty_namespace()?
        .with_host_profile_for_use()?
        .with_station()?
        .with_unused_desktop()?
        .fixture_observe_initialized_account_birth(server, &password.0, Duration::from_secs(10));
    drop(_reset);
    if case == "initializer-missing-profile-environment" {
        eprintln!(
            "initializer omitted-profile diagnostic: result_ok={}, phases={:?}",
            result.is_ok(),
            phases.borrow()
        );
    }
    let success = matches!(
        case,
        "initializer-success" | "initializer-account-rpc-self-check"
    );
    if success {
        if let Err(error) = &result {
            eprintln!("bounded initializer fixture failed: {error:#}");
        }
        result.context("initializer success fixture failed")?;
    } else {
        let error = result
            .err()
            .context("initializer fault injection did not refuse")?;
        let expected_error = if case == "initializer-missing-profile-environment" {
            error.chain().any(|e| {
                e.downcast_ref::<windows::core::Error>().is_some_and(|e| {
                    e.code() == windows::Win32::Foundation::ERROR_ENVVAR_NOT_FOUND.to_hresult()
                })
            })
        } else {
            error.chain().any(|e| {
                e.downcast_ref::<rusqlite::Error>()
                    .is_some_and(|e| e.sqlite_error_code() == Some(rusqlite::ErrorCode::ReadOnly))
            })
        };
        if !expected_error {
            eprintln!("bounded initializer fixture refused for another cause: {error:#}");
            return Err(error)
                .context("initializer fault injection did not reach expected journal write");
        }
    }
    ensure!(
        atomic_job_counts(&record).is_none(),
        "initializer original job survived return"
    );
    let before_account = matches!(
        case,
        "initializer-birth-receipt-failure"
            | "initializer-stopped-receipt-failure"
            | "initializer-missing-profile-environment"
            | "initializer-rpc-intent-failure"
            | "initializer-rpc-receipt-failure"
    );
    if before_account {
        ensure!(
            exit.borrow().is_none(),
            "initializer receipt failure allowed account child birth"
        );
    } else {
        exit.borrow_mut()
            .take()
            .context("initializer account child was not observed")?
            .wait(Duration::from_secs(5))?;
    }
    let sql = db.connection()?;
    sql.execute_batch("PRAGMA query_only=OFF")?;
    let count = |table: &str| -> Result<usize> {
        Ok(sql.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0))?)
    };
    let expected_resume = match case {
        "initializer-account-rpc-self-check" => 3,
        "initializer-account-rpc-resumed-receipt-failure" => 1,
        "initializer-account-rpc-exit-receipt-failure" => 2,
        _ => 0,
    };
    ensure!(
        count("candidate_atomic_account_resume")? == expected_resume,
        "initializer account self-check durable resume prefix differs"
    );
    let (rows, expected): (_, &[&str]) = match case {
        "initializer-success" | "initializer-account-rpc-self-check" => (
            7,
            &[
                "intent",
                "born",
                "stopped",
                "rpc-intent",
                "rpc-granted",
                "ready",
                "releasing",
                "closed",
                "completed",
            ],
        ),
        "initializer-birth-receipt-failure" => (1, &["intent", "born"]),
        "initializer-stopped-receipt-failure" => (2, &["intent", "born", "stopped"]),
        "initializer-missing-profile-environment" => (1, &["intent"]),
        "initializer-rpc-intent-failure" => (3, &["intent", "born", "stopped", "rpc-intent"]),
        "initializer-rpc-receipt-failure" => (
            4,
            &["intent", "born", "stopped", "rpc-intent", "rpc-granted"],
        ),
        "initializer-release-intent-failure"
        | "initializer-account-rpc-intent-failure"
        | "initializer-account-rpc-resumed-receipt-failure"
        | "initializer-account-rpc-exit-receipt-failure" => (
            5,
            &[
                "intent",
                "born",
                "stopped",
                "rpc-intent",
                "rpc-granted",
                "ready",
            ],
        ),
        "initializer-release-receipt-failure" => (
            6,
            &[
                "intent",
                "born",
                "stopped",
                "rpc-intent",
                "rpc-granted",
                "ready",
                "releasing",
                "closed",
            ],
        ),
        _ => anyhow::bail!("unknown initializer case"),
    };
    ensure!(
        count("candidate_atomic_initializer")? == rows
            && phases
                .borrow()
                .iter()
                .map(String::as_str)
                .collect::<Vec<_>>()
                == expected,
        "initializer durable prefix/native phases differ"
    );
    ensure!(
        count("candidate_atomic_host_profile")? == if success { 6 } else { 3 },
        "initializer failure allowed profile retirement"
    );
    ensure!(
        count("candidate_atomic_account_birth")? == if before_account { 0 } else { 2 },
        "initializer account birth prefix differs"
    );
    ensure!(
        count("candidate_atomic_job_retirement")?
            == if before_account || case.starts_with("initializer-account-rpc-") && !success {
                0
            } else {
                2
            },
        "initializer job retirement prefix differs"
    );
    ensure!(
        profile_phases.borrow().iter().any(|p| p == "deleted") == success,
        "initializer failure reached SDK deletion"
    );
    for table in [
        "candidate_atomic_namespace",
        "candidate_atomic_desktop",
        "candidate_atomic_station",
    ] {
        ensure!(
            count(table)? == if success { 4 } else { 2 },
            "initializer outer-resource retirement differs"
        );
    }
    if let Err(original_error) = bootstrap_plan::namespace::fixture_wait_absent(&plan) {
        let mut session = 0;
        unsafe {
            windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
                std::process::id(),
                &mut session,
            )
        }?;
        let settled =
            bootstrap_plan::namespace::fixture_observe_failed_initializer_settlement(&plan);
        let diagnostic = serde_json::json!({
            "case":case,"session":session,"package_sid":record.identity.package_sid,
            "broker_pid":record.pid,"broker_created":record.created,
            "settled_intervals":settled.as_ref().ok(),
            "settlement_error":settled.err().map(|e|e.to_string())
        });
        eprintln!("initializer namespace retirement diagnostic: {diagnostic}");
        FAILURE_NAMESPACE.with(|slot| *slot.borrow_mut() = Some(diagnostic));
        // A delayed observation never converts the original absence failure
        // into a passing gate. Cold observations convey no cleanup authority.
        return Err(original_error);
    }
    ensure!(
        Station::fixture_snapshot_current()? == if success { before } else { after },
        "initializer changed station outside explicit retirement"
    );
    ensure!(
        crate::account_state::execution::resources::namespace_native::fixture_global_security()?
            == global_before,
        "initializer changed shared Global target security"
    );
    ensure!(
        std::fs::read(fixture.path())? == marker,
        "initializer changed pending marker"
    );
    Ok(())
}

thread_local! { static FAILURE_NAMESPACE: RefCell<Option<serde_json::Value>> = const { RefCell::new(None) }; }
pub(super) fn failure_namespace() -> serde_json::Value {
    FAILURE_NAMESPACE.with(|s| s.borrow().clone().unwrap_or(serde_json::Value::Null))
}
#[test]
#[ignore = "read-only follow-up to failed initializer in disposable hosted VM"]
fn initializer_failed_creation_post_exit_observation() {
    super::require_host().unwrap();
    let root = std::path::PathBuf::from(std::env::var_os("RUNNER_TEMP").unwrap());
    let bytes = std::fs::read(root.join("alysis-account-birth.json")).unwrap();
    assert!(bytes.len() <= 32768);
    let report: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(report["passed"], false);
    assert!(
        report["stage"]
            .as_str()
            .unwrap()
            .starts_with("initializer-")
    );
    assert_eq!(report["run_id"], std::env::var("GITHUB_RUN_ID").unwrap());
    assert_eq!(
        report["run_attempt"],
        std::env::var("GITHUB_RUN_ATTEMPT").unwrap()
    );
    let failures = report["initializer_failures"].as_array().unwrap();
    assert!(!failures.is_empty() && failures.len() <= 12);
    let mut observations = Vec::new();
    for failure in failures {
        let target = &failure["namespace"];
        if target.is_null() {
            continue;
        }
        assert_eq!(target["case"], failure["case"]);
        let session: u32 = target["session"].as_u64().unwrap().try_into().unwrap();
        let package = target["package_sid"].as_str().unwrap();
        let result = crate::account_state::execution::resources::namespace_native::fixture_recorded_namespace_absent(session, package);
        observations.push(
            serde_json::json!({"source":target,"absent":result.as_ref().ok(),
            "query_error":result.err().map(|e|e.to_string())}),
        );
    }
    assert!(!observations.is_empty());
    let proof = serde_json::json!({"run_id":report["run_id"],"run_attempt":report["run_attempt"],
        "observation":"query-only after primary test process returned; no ownership or cleanup authority",
        "observer_pid":std::process::id(),"observations":observations,"production_ready":false});
    let path = root.join("alysis-account-birth-namespace-after-exit.json");
    assert!(!path.exists());
    std::fs::write(path, serde_json::to_vec_pretty(&proof).unwrap()).unwrap();
    eprintln!(
        "failed initializer namespaces after primary process exit: {}",
        proof["observations"]
    );
}
