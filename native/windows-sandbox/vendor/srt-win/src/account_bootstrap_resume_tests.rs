use super::super::super::tests::{Disk, plan};
use super::*;

pub(super) fn seeded() -> (Disk, Plan, Vec<ResumeEvent>) {
    let p = plan();
    let disk = Disk::new(&p);
    persist(disk.sql(), &p).unwrap();
    let job = random_id().unwrap();
    let birth = random_id().unwrap();
    let nonce = random_id().unwrap();
    for event in [
        Event::JobIntent { nonce: job.clone() },
        Event::JobCreated {
            nonce: job,
            policy: 1,
        },
        Event::BootstrapIntent {
            nonce: birth.clone(),
        },
        Event::BootstrapCreated {
            nonce: birth.clone(),
            pid: p.execution.pid + 1,
            created: 7,
            image_id: p.image_id,
        },
    ] {
        append(disk.sql(), &p, event).unwrap();
    }
    let events = vec![
        ResumeEvent::Intent {
            nonce: nonce.clone(),
            birth,
            pid: p.execution.pid + 1,
            created: 7,
            image_id: p.image_id,
        },
        ResumeEvent::Resumed {
            nonce: nonce.clone(),
        },
        ResumeEvent::Ready { nonce },
    ];
    (disk, p, events)
}
#[test]
fn atomic_resume_durable_prefixes_are_data_without_recreated_authority() {
    for cut in 0..=3 {
        let (mut disk, p, events) = seeded();
        for event in &events[..cut] {
            append_resume(disk.sql(), &p, event.clone()).unwrap();
        }
        disk.reopen();
        assert_eq!(replay_resume(disk.sql(), &p).unwrap(), events[..cut]);
        for event in &events[..cut] {
            assert!(append_resume(disk.sql(), &p, event.clone()).is_err());
        }
        assert_eq!(replay_resume(disk.sql(), &p).unwrap(), events[..cut]);
    }
}
#[test]
fn atomic_resume_rejects_mismatched_birth_and_out_of_order_receipts() {
    let (disk, p, events) = seeded();
    for i in 0..5 {
        let mut wrong = events[0].clone();
        if let ResumeEvent::Intent {
            nonce,
            birth,
            pid,
            created,
            image_id,
        } = &mut wrong
        {
            match i {
                0 => *nonce = "../wrong".into(),
                1 => *birth = random_id().unwrap(),
                2 => *pid += 1,
                3 => *created += 1,
                _ => image_id[0] ^= 1,
            }
        }
        assert!(append_resume(disk.sql(), &p, wrong).is_err());
    }
    for cut in 0..3 {
        for (i, event) in events.iter().enumerate() {
            if i != cut {
                assert!(append_resume(disk.sql(), &p, event.clone()).is_err());
            }
        }
        append_resume(disk.sql(), &p, events[cut].clone()).unwrap();
    }
    assert_eq!(replay_resume(disk.sql(), &p).unwrap(), events);
}
#[test]
fn atomic_resume_rejects_mutation_missing_schema_gaps_and_oversized_events() {
    let (disk, p, events) = seeded();
    append_resume(disk.sql(), &p, events[0].clone()).unwrap();
    for sql in [
        "UPDATE candidate_atomic_bootstrap_resume SET event_json='{}'",
        "DELETE FROM candidate_atomic_bootstrap_resume",
        "INSERT OR REPLACE INTO candidate_atomic_bootstrap_resume VALUES(0,'{}')",
    ] {
        assert!(disk.sql().execute_batch(sql).is_err());
    }
    disk.sql()
        .execute(
            "INSERT INTO candidate_atomic_bootstrap_resume VALUES(2,?1)",
            [serde_json::to_string(&events[2]).unwrap()],
        )
        .unwrap();
    assert!(replay_resume(disk.sql(), &p).is_err());
    let (disk, p, _) = seeded();
    disk.sql()
        .execute(
            "INSERT INTO candidate_atomic_bootstrap_resume VALUES(0,?1)",
            ["x".repeat(1025)],
        )
        .unwrap();
    assert!(replay_resume(disk.sql(), &p).is_err());
    let (disk, p, _) = seeded();
    disk.sql()
        .execute_batch("DROP TABLE candidate_atomic_bootstrap_resume")
        .unwrap();
    assert!(require_plan(disk.sql(), &p).is_err()); // No upgrade/adoption of older plans.
}
