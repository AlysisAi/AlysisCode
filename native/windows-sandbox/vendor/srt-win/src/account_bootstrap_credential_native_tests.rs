//! Synthetic secret and real original bootstrap/channel/exit observations.
//! No registry credential read, installed account, account logon or SDK profile.
use super::*;
use crate::{account_control::ControlServer, account_state::image::PinnedRunnerImage};
use bootstrap_plan::lifecycle::resume::logon::delivery::DELIVERY_CHECKPOINT;
use std::time::Duration;

#[test]
fn credential_delivery_original_peer_success_and_errors_preserve_pending_state() {
    for (scenario, receipt_count) in [
        ("discard", 1),
        ("drop", 1),
        ("missing-lease", 0),
        ("invalid-timeout", 0),
        ("write-failure", 0),
        ("before-send-drift", 0),
        ("after-ack-drift", 0),
        ("after-commit-drift", 1),
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
        job.record_birth().unwrap();
        let child = job.prepare_bootstrap().unwrap();
        let observed = child.fixture_readonly_observer().unwrap();
        child.record_birth().unwrap();
        let prepared = child
            .start(server, Duration::from_secs(3))
            .unwrap()
            .prepare_account_logon()
            .unwrap();
        let sql = db.connection().unwrap();
        let original_intent: String = sql
            .query_row(
                "SELECT intent_json FROM candidate_atomic_account_logon",
                [],
                |r| r.get(0),
            )
            .unwrap();
        if scenario == "write-failure" {
            sql.execute_batch("PRAGMA query_only=ON").unwrap();
        }
        let drift_phase = match scenario {
            "before-send-drift" => Some("before-send"),
            "after-ack-drift" => Some("acknowledged"),
            "after-commit-drift" => Some("receipt-committed"),
            _ => None,
        };
        let phases = std::rc::Rc::new(std::cell::RefCell::new(Vec::new()));
        let trace = phases.clone();
        DELIVERY_CHECKPOINT.with(|slot| {
            assert!(slot.borrow().is_none());
            *slot.borrow_mut() = Some(Box::new(move |phase, connection| {
                trace.borrow_mut().push(phase.to_owned());
                if Some(phase) == drift_phase {
                    connection
                        .execute_batch("DROP TRIGGER atomic_delivery_no_update")
                        .unwrap();
                }
            }));
        });
        let result = if scenario == "missing-lease" {
            prepared.deliver(Duration::from_secs(3))
        } else {
            prepared.fixture_deliver(
                "Synthetic-handoff-only!🦀",
                if scenario == "invalid-timeout" {
                    Duration::ZERO
                } else {
                    Duration::from_secs(3)
                },
            )
        };
        if matches!(scenario, "discard" | "drop") {
            let delivered = result.unwrap();
            assert_eq!(
                unsafe { WaitForSingleObject(observed.raw(), 0) },
                WAIT_TIMEOUT
            );
            if scenario == "discard" {
                delivered.discard().unwrap();
            } else {
                drop(delivered);
            }
        } else {
            let error = result.err().expect("injected failure must refuse delivery");
            if scenario == "missing-lease" {
                assert!(format!("{error:#}").contains("requires original execution lease"));
            }
        }
        DELIVERY_CHECKPOINT.with(|slot| {
            assert!(slot.borrow_mut().take().is_some());
        });
        let expected_phases: &[&str] = match scenario {
            "invalid-timeout" | "missing-lease" => &[],
            "before-send-drift" => &["before-send"],
            "after-ack-drift" | "write-failure" => &["before-send", "acknowledged"],
            _ => &[
                "before-send",
                "acknowledged",
                "receipt-inserted",
                "receipt-committed",
            ],
        };
        assert_eq!(*phases.borrow(), expected_phases, "{scenario}");
        assert_eq!(
            unsafe { WaitForSingleObject(observed.raw(), 5000) },
            WAIT_OBJECT_0
        );
        sql.execute_batch("PRAGMA query_only=OFF").unwrap();
        let count: usize = sql
            .query_row(
                "SELECT count(*) FROM candidate_atomic_credential_delivery",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, receipt_count, "{scenario}");
        let kept: String = sql
            .query_row(
                "SELECT intent_json FROM candidate_atomic_account_logon",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(kept, original_intent);
        if count == 1 {
            let receipt: String = sql
                .query_row(
                    "SELECT receipt_json FROM candidate_atomic_credential_delivery",
                    [],
                    |r| r.get(0),
                )
                .unwrap();
            assert!(!receipt.contains("Synthetic-handoff") && !receipt.contains("password"));
        }
        assert_eq!(std::fs::read(fixture.path()).unwrap(), marker);
        assert!(job.prepare_bootstrap().is_err());
        drop(job);
        assert_eq!(atomic_job_counts(active.record().unwrap()), None);
        eprintln!(
            "credential delivery {scenario}: original bootstrap exited, pending evidence preserved"
        );
    }
}
