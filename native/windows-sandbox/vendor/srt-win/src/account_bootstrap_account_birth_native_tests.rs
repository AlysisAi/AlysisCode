use super::*;
use crate::{account_control::ControlServer, account_state::image::PinnedRunnerImage};
use bootstrap_plan::lifecycle::resume::logon::delivery::{
    DELIVERY_CHECKPOINT, birth::BIRTH_CHECKPOINT,
};
use std::time::Duration;

#[path = "account_bootstrap_account_birth_hosted_tests.rs"]
mod hosted;

#[test]
fn account_birth_failure_consumes_original_job_and_preserves_pending_evidence() {
    for (case, expected) in [
        ("missing-lease", 0),
        ("intent-write-failure", 0),
        ("intent-readback-drift", 1),
        ("native-account-mismatch", 1),
    ] {
        let fixture = protected_fixture();
        let db = protected_database(&fixture);
        let ready = ReadyInstallation::fixture(fixture.setup.clone(), &fixture.root).unwrap();
        let active = atomic_fixture_active(&fixture, &db);
        let marker = std::fs::read(fixture.path()).unwrap();
        let image = PinnedRunnerImage::fixture_installed(&ready).unwrap();
        let (server, address) =
            ControlServer::prepare(&ready.binding(AccountMode::Offline).unwrap().account_sid)
                .unwrap();
        let plan =
            bootstrap_plan::CommittedPlan::prepare(&db, &active, &image, &ready, &address).unwrap();
        let job = plan.create_job().unwrap();
        if case == "intent-write-failure" {
            DELIVERY_CHECKPOINT.with(|s| {
                *s.borrow_mut() = Some(Box::new(|phase, sql| {
                    if phase == "receipt-committed" {
                        sql.execute_batch("PRAGMA query_only=ON").unwrap();
                    }
                }))
            });
        }
        if case == "intent-readback-drift" {
            BIRTH_CHECKPOINT.with(|s| {
                *s.borrow_mut() = Some(Box::new(|phase, sequence, sql| {
                    if phase == "committed" && sequence == 0 {
                        sql.execute_batch("DROP TRIGGER atomic_account_birth_no_update")
                            .unwrap();
                    }
                }))
            });
        }
        let result = if case == "missing-lease" {
            job.observe_account_birth(server, Duration::from_secs(3))
        } else {
            job.fixture_observe_account_birth(
                server,
                "Synthetic-never-valid-account!",
                Duration::from_secs(3),
            )
        };
        assert!(result.is_err(), "case {case}");
        DELIVERY_CHECKPOINT.with(|s| {
            s.borrow_mut().take();
        });
        BIRTH_CHECKPOINT.with(|s| {
            s.borrow_mut().take();
        });
        let sql = db.connection().unwrap();
        sql.execute_batch("PRAGMA query_only=OFF").unwrap();
        let count: usize = sql
            .query_row(
                "SELECT count(*) FROM candidate_atomic_account_birth",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, expected, "case {case}");
        assert_eq!(
            atomic_job_counts(active.record().unwrap()),
            None,
            "original job remained open at {case}"
        );
        assert_eq!(std::fs::read(fixture.path()).unwrap(), marker);
        eprintln!("account birth {case}: whole original job closed, pending state retained");
    }
}
