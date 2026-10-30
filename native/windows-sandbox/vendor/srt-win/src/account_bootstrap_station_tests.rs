use super::super::tests::{Disk, plan};
use super::*;
fn intent(plan: &Plan) -> Event {
    let before = Snapshot {
        revision: 2,
        control: 0x1004,
        owner: "S-1-5-18".into(),
        aces: Vec::new(),
    };
    let after = proposed(&before, &plan.execution.identity.package_sid).unwrap();
    Event::Intent {
        binding: Binding {
            operation: plan.operation.clone(),
            nonce: random_id().unwrap(),
            session: plan.session,
            package_sid: plan.execution.identity.package_sid.clone(),
            policy: 1,
        },
        before,
        after,
    }
}
#[test]
fn atomic_station_cold_prefix_cannot_authorize_restoration() {
    for cut in 0..=2 {
        let plan = plan();
        let mut disk = Disk::new(&plan);
        persist(disk.sql(), &plan).unwrap();
        let start = intent(&plan);
        let Event::Intent { binding, .. } = &start else {
            unreachable!()
        };
        let binding = binding.clone();
        let events = [
            start,
            Event::Applied {
                binding: binding.clone(),
            },
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
                Event::RestoreIntent {
                    binding,
                    retirement: random_id().unwrap()
                }
            )
            .is_err()
        );
        assert_eq!(replay(disk.sql(), &plan).unwrap(), events[..cut]);
    }
}
#[test]
fn atomic_station_refuses_identity_and_exact_snapshot_drift() {
    let plan = plan();
    let disk = Disk::new(&plan);
    persist(disk.sql(), &plan).unwrap();
    for case in 0..8 {
        let mut event = intent(&plan);
        let Event::Intent {
            binding,
            before,
            after,
        } = &mut event
        else {
            unreachable!()
        };
        match case {
            0 => binding.operation = random_id().unwrap(),
            1 => binding.nonce = binding.operation.clone(),
            2 => binding.session += 1,
            3 => binding.package_sid.push_str("-1"),
            4 => binding.policy += 1,
            5 => after.aces.clear(),
            6 => before.owner = "bad".into(),
            _ => after.owner = "S-1-5-32-545".into(),
        }
        assert!(append(disk.sql(), &plan, event).is_err(), "case {case}");
        assert!(replay(disk.sql(), &plan).unwrap().is_empty());
    }
}
#[test]
fn atomic_station_failed_intent_and_schema_drift_preserve_prefix() {
    let plan = plan();
    let disk = Disk::new(&plan);
    persist(disk.sql(), &plan).unwrap();
    let start = intent(&plan);
    disk.sql().execute_batch("PRAGMA query_only=ON").unwrap();
    assert!(append(disk.sql(), &plan, start.clone()).is_err());
    assert!(replay(disk.sql(), &plan).unwrap().is_empty());
    disk.sql().execute_batch("PRAGMA query_only=OFF").unwrap();
    append(disk.sql(), &plan, start).unwrap();
    for sql in [
        "UPDATE candidate_atomic_station SET event_json='{}'",
        "DELETE FROM candidate_atomic_station",
        "INSERT OR REPLACE INTO candidate_atomic_station VALUES(0,'{}')",
    ] {
        assert!(disk.sql().execute_batch(sql).is_err());
    }
    disk.sql()
        .execute_batch("DROP TRIGGER atomic_station_no_delete")
        .unwrap();
    assert!(replay(disk.sql(), &plan).is_err());
    assert_eq!(
        disk.sql()
            .query_row("SELECT count(*) FROM candidate_atomic_station", [], |r| {
                r.get::<_, usize>(0)
            })
            .unwrap(),
        1
    );
}
