use super::super::tests::{Disk, plan};
use super::*;
fn seed_station(sql: &Connection, plan: &Plan) -> String {
    let nonce = random_id().unwrap();
    let before = crate::acl::object_grants::Snapshot {
        revision: 2,
        control: 0x1004,
        owner: "S-1-5-18".into(),
        aces: vec![],
    };
    let after = super::super::super::resources::station::proposed(
        &before,
        &plan.execution.identity.package_sid,
    )
    .unwrap();
    let binding = serde_json::json!({"operation":plan.operation,"nonce":nonce,"session":plan.session,"package_sid":plan.execution.identity.package_sid,"policy":1});
    for (i, event) in [
        serde_json::json!({"kind":"intent","binding":binding,"before":before,"after":after}),
        serde_json::json!({"kind":"applied","binding":binding}),
    ]
    .iter()
    .enumerate()
    {
        sql.execute(
            "INSERT INTO candidate_atomic_station VALUES(?1,?2)",
            params![i, serde_json::to_string(event).unwrap()],
        )
        .unwrap();
    }
    assert_eq!(station::grant_nonce(sql, plan).unwrap(), nonce);
    nonce
}
fn intent(sql: &Connection, plan: &Plan) -> Event {
    Event::Intent {
        binding: Binding {
            operation: plan.operation.clone(),
            nonce: random_id().unwrap(),
            station_nonce: seed_station(sql, plan),
            session: plan.session,
            name: desktop_native::name(plan).unwrap(),
            package_sid: plan.execution.identity.package_sid.clone(),
            policy: 1,
        },
    }
}
#[test]
fn atomic_desktop_cold_birth_prefix_cannot_authorize_native_closure() {
    for count in 0..=2 {
        let p = plan();
        let mut disk = Disk::new(&p);
        persist(disk.sql(), &p).unwrap();
        let first = intent(disk.sql(), &p);
        let Event::Intent { binding } = &first else {
            unreachable!()
        };
        let binding = binding.clone();
        let events = [
            first,
            Event::Created {
                binding: binding.clone(),
            },
        ];
        assert!(append(disk.sql(), &p, events[1].clone()).is_err());
        for event in &events[..count] {
            append(disk.sql(), &p, event.clone()).unwrap();
        }
        disk.reopen();
        assert_eq!(replay(disk.sql(), &p).unwrap(), events[..count]);
        assert!(
            append(
                disk.sql(),
                &p,
                Event::CloseIntent {
                    binding,
                    retirement: random_id().unwrap()
                }
            )
            .is_err()
        );
        assert_eq!(replay(disk.sql(), &p).unwrap(), events[..count]);
    }
}
#[test]
fn atomic_desktop_binding_refuses_foreign_station_name_actor_and_policy() {
    let p = plan();
    let disk = Disk::new(&p);
    persist(disk.sql(), &p).unwrap();
    let first = intent(disk.sql(), &p);
    for case in 0..8 {
        let mut event = first.clone();
        let Event::Intent { binding } = &mut event else {
            unreachable!()
        };
        match case {
            0 => binding.operation = random_id().unwrap(),
            1 => binding.nonce = binding.operation.clone(),
            2 => binding.station_nonce = random_id().unwrap(),
            3 => binding.session += 1,
            4 => binding.name.push('x'),
            5 => binding.package_sid.push_str("-1"),
            6 => binding.policy += 1,
            _ => binding.nonce = binding.station_nonce.clone(),
        }
        assert!(append(disk.sql(), &p, event).is_err(), "case {case}");
        assert!(replay(disk.sql(), &p).unwrap().is_empty());
    }
}
#[test]
fn atomic_desktop_failed_write_and_schema_drift_preserve_evidence() {
    let p = plan();
    let disk = Disk::new(&p);
    persist(disk.sql(), &p).unwrap();
    let first = intent(disk.sql(), &p);
    disk.sql().execute_batch("PRAGMA query_only=ON").unwrap();
    assert!(append(disk.sql(), &p, first.clone()).is_err());
    assert!(replay(disk.sql(), &p).unwrap().is_empty());
    disk.sql().execute_batch("PRAGMA query_only=OFF").unwrap();
    append(disk.sql(), &p, first).unwrap();
    let before = replay(disk.sql(), &p).unwrap();
    for s in [
        "UPDATE candidate_atomic_desktop SET event_json='{}'",
        "DELETE FROM candidate_atomic_desktop",
        "INSERT OR REPLACE INTO candidate_atomic_desktop VALUES(0,'{}')",
    ] {
        assert!(disk.sql().execute_batch(s).is_err());
        assert_eq!(replay(disk.sql(), &p).unwrap(), before);
    }
    disk.sql().execute_batch("CREATE TRIGGER foreign_desktop_trigger BEFORE INSERT ON candidate_atomic_desktop BEGIN SELECT 1; END").unwrap();
    assert!(replay(disk.sql(), &p).is_err());
    assert_eq!(
        disk.sql()
            .query_row("SELECT count(*) FROM candidate_atomic_desktop", [], |r| r
                .get::<_, u32>(
                0
            ))
            .unwrap(),
        1
    );
}
