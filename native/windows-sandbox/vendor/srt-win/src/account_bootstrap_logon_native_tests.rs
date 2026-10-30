//! Intent preparation uses a real original trusted bootstrap, but no credential
//! read, account logon, SDK profile or user command. Marker/records stay pending.
use super::*;
use crate::{account_control::ControlServer, account_state::image::PinnedRunnerImage};
use std::time::Duration;

#[test]
fn account_logon_preparation_preserves_intent_and_drains_original_on_errors() {
    for (scenario, expected) in [
        ("success", 1),
        ("write-failure", 0),
        ("post-commit-drift", 1),
    ] {
        let fixture = protected_fixture();
        let db = protected_database(&fixture);
        let ready = ReadyInstallation::fixture(fixture.setup.clone(), &fixture.root).unwrap();
        let active = atomic_fixture_active(&fixture, &db);
        let marker = std::fs::read(fixture.path()).unwrap();
        let image = PinnedRunnerImage::fixture_installed(&ready).unwrap();
        let account = ready.binding(AccountMode::Offline).unwrap().account_sid;
        let (server, address) = ControlServer::prepare(&account).unwrap();
        let plan =
            bootstrap_plan::CommittedPlan::prepare(&db, &active, &image, &ready, &address).unwrap();
        let job = plan.create_job().unwrap();
        job.record_birth().unwrap();
        let child = job.prepare_bootstrap().unwrap();
        let observed = child.fixture_readonly_observer().unwrap();
        child.record_birth().unwrap();
        let running = child.start(server, Duration::from_secs(3)).unwrap();
        let sql = db.connection().unwrap();
        if scenario == "write-failure" {
            sql.execute_batch("PRAGMA query_only=ON").unwrap();
        }
        if scenario == "post-commit-drift" {
            bootstrap_plan::lifecycle::resume::logon::AFTER_LOGON_INTENT.with(|slot| {
                *slot.borrow_mut() = Some(Box::new(|connection| {
                    connection
                        .execute_batch("DROP TRIGGER atomic_logon_no_update")
                        .unwrap();
                }));
            });
        }
        let prepared = running.prepare_account_logon();
        if scenario == "success" {
            let prepared = prepared.unwrap();
            assert_eq!(
                unsafe { WaitForSingleObject(observed.raw(), 0) },
                WAIT_TIMEOUT
            );
            drop(prepared);
        } else {
            assert!(prepared.is_err(), "unexpected success at {scenario}");
            drop(prepared);
        }
        assert_eq!(
            unsafe { WaitForSingleObject(observed.raw(), 5000) },
            WAIT_OBJECT_0
        );
        sql.execute_batch("PRAGMA query_only=OFF").unwrap();
        let count: usize = sql
            .query_row(
                "SELECT count(*) FROM candidate_atomic_account_logon",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, expected);
        if expected == 1 {
            let intent: String = sql
                .query_row(
                    "SELECT intent_json FROM candidate_atomic_account_logon",
                    [],
                    |r| r.get(0),
                )
                .unwrap();
            let intent: serde_json::Value = serde_json::from_str(&intent).unwrap();
            assert_eq!(
                intent["credential_generation"],
                ready
                    .record
                    .offline
                    .as_ref()
                    .unwrap()
                    .credential_generation
                    .as_deref()
                    .unwrap()
            );
            assert!(intent.get("password").is_none());
        }
        assert_eq!(std::fs::read(fixture.path()).unwrap(), marker);
        assert!(job.prepare_bootstrap().is_err());
        drop(job);
        assert_eq!(atomic_job_counts(active.record().unwrap()), None);
        bootstrap_plan::lifecycle::resume::logon::AFTER_LOGON_INTENT
            .with(|slot| assert!(slot.borrow_mut().take().is_none()));
        eprintln!("account logon preparation {scenario}: pending marker and intent preserved");
    }
}
