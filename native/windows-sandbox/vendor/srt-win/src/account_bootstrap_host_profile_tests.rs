use super::super::tests::{Disk, plan};
use super::*;
fn binding(plan: &Plan) -> Binding {
    Binding {
        operation: plan.operation.clone(),
        nonce: random_id().unwrap(),
        owner: plan.execution.owner.clone(),
        container: plan.execution.identity.container.clone(),
        package_sid: plan.execution.identity.package_sid.clone(),
        policy: 1,
    }
}
#[test]
fn atomic_host_profile_cold_prefix_cannot_authorize_deletion() {
    for count in 0..=2 {
        let p = plan();
        let mut disk = Disk::new(&p);
        persist(disk.sql(), &p).unwrap();
        let binding = binding(&p);
        let events = [
            Event::Intent {
                binding: binding.clone(),
            },
            Event::Created {
                binding: binding.clone(),
                folder_id: [7; 24],
            },
        ];
        assert!(append(disk.sql(), &p, events[1].clone()).is_err());
        for e in &events[..count] {
            append(disk.sql(), &p, e.clone()).unwrap();
        }
        disk.reopen();
        assert_eq!(replay(disk.sql(), &p).unwrap(), events[..count]);
        assert!(
            append(
                disk.sql(),
                &p,
                Event::RemoveIntent {
                    binding,
                    folder_id: [7; 24],
                    retirement: random_id().unwrap()
                }
            )
            .is_err()
        );
        assert_eq!(replay(disk.sql(), &p).unwrap(), events[..count]);
    }
}
#[test]
fn atomic_host_profile_identity_and_original_storage_mismatch_refused() {
    let p = plan();
    let disk = Disk::new(&p);
    persist(disk.sql(), &p).unwrap();
    let original = binding(&p);
    for case in 0..6 {
        let mut b = original.clone();
        match case {
            0 => b.operation = random_id().unwrap(),
            1 => b.nonce = b.operation.clone(),
            2 => b.owner.push_str("-1"),
            3 => b.container.push('x'),
            4 => b.package_sid.push_str("-1"),
            _ => b.policy += 1,
        }
        assert!(append(disk.sql(), &p, Event::Intent { binding: b }).is_err());
        assert!(replay(disk.sql(), &p).unwrap().is_empty());
    }
    append(
        disk.sql(),
        &p,
        Event::Intent {
            binding: original.clone(),
        },
    )
    .unwrap();
    assert!(
        append(
            disk.sql(),
            &p,
            Event::Created {
                binding: original,
                folder_id: [0; 24]
            }
        )
        .is_err()
    );
    assert_eq!(replay(disk.sql(), &p).unwrap().len(), 1);
}
#[test]
fn atomic_host_profile_failed_write_and_schema_drift_preserve_evidence() {
    let p = plan();
    let disk = Disk::new(&p);
    persist(disk.sql(), &p).unwrap();
    let event = Event::Intent {
        binding: binding(&p),
    };
    disk.sql().execute_batch("PRAGMA query_only=ON").unwrap();
    assert!(append(disk.sql(), &p, event.clone()).is_err());
    assert!(replay(disk.sql(), &p).unwrap().is_empty());
    disk.sql().execute_batch("PRAGMA query_only=OFF").unwrap();
    append(disk.sql(), &p, event).unwrap();
    let before = replay(disk.sql(), &p).unwrap();
    for sql in [
        "UPDATE candidate_atomic_host_profile SET event_json='{}'",
        "DELETE FROM candidate_atomic_host_profile",
        "INSERT OR REPLACE INTO candidate_atomic_host_profile VALUES(0,'{}')",
    ] {
        assert!(disk.sql().execute_batch(sql).is_err());
        assert_eq!(replay(disk.sql(), &p).unwrap(), before);
    }
    disk.sql().execute_batch("CREATE TRIGGER foreign_profile_trigger BEFORE INSERT ON candidate_atomic_host_profile BEGIN SELECT 1; END").unwrap();
    assert!(replay(disk.sql(), &p).is_err());
    assert_eq!(
        disk.sql()
            .query_row(
                "SELECT count(*) FROM candidate_atomic_host_profile",
                [],
                |r| r.get::<_, u32>(0)
            )
            .unwrap(),
        1
    );
}

#[test]
fn atomic_host_profile_use_prefix_cannot_reconstruct_capture_or_cleanup() {
    let p = plan();
    let mut disk = Disk::new(&p);
    persist(disk.sql(), &p).unwrap();
    let b = binding(&p);
    let nonce = random_id().unwrap();
    append(disk.sql(), &p, Event::Intent { binding: b.clone() }).unwrap();
    append(
        disk.sql(),
        &p,
        Event::Created {
            binding: b.clone(),
            folder_id: [7; 24],
        },
    )
    .unwrap();
    for (n, id) in [
        (b.nonce.clone(), [7; 24]),
        (b.operation.clone(), [7; 24]),
        (nonce.clone(), [8; 24]),
    ] {
        assert!(
            append(
                disk.sql(),
                &p,
                Event::UseIntent {
                    binding: b.clone(),
                    folder_id: id,
                    nonce: n
                }
            )
            .is_err()
        );
        assert_eq!(replay(disk.sql(), &p).unwrap().len(), 2);
    }
    let event = Event::UseIntent {
        binding: b.clone(),
        folder_id: [7; 24],
        nonce: nonce.clone(),
    };
    disk.sql().execute_batch("PRAGMA query_only=ON").unwrap();
    assert!(append(disk.sql(), &p, event.clone()).is_err());
    assert_eq!(replay(disk.sql(), &p).unwrap().len(), 2);
    disk.sql().execute_batch("PRAGMA query_only=OFF").unwrap();
    append(disk.sql(), &p, event).unwrap();
    disk.reopen();
    let before = replay(disk.sql(), &p).unwrap();
    assert_eq!(before.len(), 3);
    for event in [
        Event::Captured {
            binding: b.clone(),
            folder_id: [7; 24],
            nonce,
            retirement: random_id().unwrap(),
        },
        Event::RemoveIntent {
            binding: b,
            folder_id: [7; 24],
            retirement: random_id().unwrap(),
        },
    ] {
        assert!(append(disk.sql(), &p, event).is_err());
        assert_eq!(replay(disk.sql(), &p).unwrap(), before);
    }
}
