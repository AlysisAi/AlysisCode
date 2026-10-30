use super::super::tests::{Disk, plan};
use super::*;

fn binding(plan: &Plan) -> Binding {
    Binding {
        operation: plan.operation.clone(),
        nonce: random_id().unwrap(),
        session: plan.session,
        package_sid: plan.execution.identity.package_sid.clone(),
        policy: POLICY,
    }
}
#[test]
fn atomic_namespace_cold_prefix_is_data_and_cannot_authorize_removal() {
    for cut in 0..=2 {
        let plan = plan();
        let mut disk = Disk::new(&plan);
        persist(disk.sql(), &plan).unwrap();
        let b = binding(&plan);
        let events = [
            Event::Intent { binding: b.clone() },
            Event::Created { binding: b.clone() },
        ];
        assert!(append(disk.sql(), &plan, events[1].clone()).is_err());
        for event in &events[..cut] {
            append(disk.sql(), &plan, event.clone()).unwrap();
        }
        disk.reopen();
        assert_eq!(replay(disk.sql(), &plan).unwrap(), events[..cut]);
        assert!(
            append(
                disk.sql(),
                &plan,
                Event::RemoveIntent {
                    binding: b,
                    retirement: random_id().unwrap()
                }
            )
            .is_err()
        );
        assert_eq!(replay(disk.sql(), &plan).unwrap(), events[..cut]);
    }
}
#[test]
fn atomic_namespace_refuses_identity_drift_and_immutable_schema_changes() {
    let plan = plan();
    let disk = Disk::new(&plan);
    persist(disk.sql(), &plan).unwrap();
    let original = binding(&plan);
    for case in 0..6 {
        let mut b = original.clone();
        match case {
            0 => b.operation = random_id().unwrap(),
            1 => b.nonce = b.operation.clone(),
            2 => b.session += 1,
            3 => b.package_sid.push_str("-1"),
            4 => b.policy += 1,
            _ => b.nonce = "invalid".into(),
        }
        assert!(append(disk.sql(), &plan, Event::Intent { binding: b }).is_err());
        assert!(replay(disk.sql(), &plan).unwrap().is_empty());
    }
    append(
        disk.sql(),
        &plan,
        Event::Intent {
            binding: original.clone(),
        },
    )
    .unwrap();
    let mut other = original;
    other.nonce = random_id().unwrap();
    assert!(append(disk.sql(), &plan, Event::Created { binding: other }).is_err());
    for statement in [
        "UPDATE candidate_atomic_namespace SET event_json='{}'",
        "DELETE FROM candidate_atomic_namespace",
        "INSERT OR REPLACE INTO candidate_atomic_namespace VALUES(0,'{}')",
    ] {
        assert!(disk.sql().execute_batch(statement).is_err());
    }
    disk.sql()
        .execute_batch("DROP TRIGGER atomic_namespace_no_update")
        .unwrap();
    assert!(replay(disk.sql(), &plan).is_err());
    assert_eq!(
        disk.sql()
            .query_row("SELECT count(*) FROM candidate_atomic_namespace", [], |r| r
                .get::<_, usize>(0))
            .unwrap(),
        1
    );
}
#[test]
fn atomic_namespace_refuses_gaps_oversized_or_unknown_events() {
    for (sequence, bytes) in [
        (1, "{}".into()),
        (0, "x".repeat(2049)),
        (0, r#"{"kind":"adopt_by_name"}"#.into()),
    ] {
        let plan = plan();
        let disk = Disk::new(&plan);
        persist(disk.sql(), &plan).unwrap();
        disk.sql()
            .execute(
                "INSERT INTO candidate_atomic_namespace VALUES(?1,?2)",
                params![sequence, bytes],
            )
            .unwrap();
        assert!(replay(disk.sql(), &plan).is_err());
        assert_eq!(
            disk.sql()
                .query_row(
                    "SELECT event_json FROM candidate_atomic_namespace",
                    [],
                    |r| r.get::<_, String>(0)
                )
                .unwrap(),
            bytes
        );
    }
}
