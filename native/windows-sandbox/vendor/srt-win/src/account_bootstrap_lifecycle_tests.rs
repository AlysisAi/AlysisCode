//! Disk journal transition tests. Native adapters are qualified separately by
//! execution fixtures using real pinned markers, image and private database.
use super::super::tests::{Disk, plan};
use super::*;

fn events(plan: &Plan) -> Vec<Event> {
    let job = random_id().unwrap();
    let child = random_id().unwrap();
    vec![
        Event::JobIntent { nonce: job.clone() },
        Event::JobCreated {
            nonce: job,
            policy: 1,
        },
        Event::BootstrapIntent {
            nonce: child.clone(),
        },
        Event::BootstrapCreated {
            nonce: child,
            pid: plan.execution.pid + 1,
            created: 123,
            image_id: plan.image_id,
        },
    ]
}

#[test]
fn atomic_lifecycle_every_prefix_survives_reopen_without_completion_authority() {
    let plan = plan();
    let expected = events(&plan);
    for cut in 0..=4 {
        let mut disk = Disk::new(&plan);
        persist(disk.sql(), &plan).unwrap();
        for event in &expected[..cut] {
            append(disk.sql(), &plan, event.clone()).unwrap();
        }
        disk.reopen();
        assert_eq!(replay(disk.sql(), &plan).unwrap(), expected[..cut]);
        // Cold reads only produce event data; not live native ownership.
        for event in &expected[..cut] {
            assert!(append(disk.sql(), &plan, event.clone()).is_err());
        }
        assert_eq!(replay(disk.sql(), &plan).unwrap(), expected[..cut]);
    }
}

#[test]
fn atomic_lifecycle_out_of_order_and_mismatched_native_receipts_are_refused() {
    let plan = plan();
    let good = events(&plan);
    for cut in 0..4 {
        let disk = Disk::new(&plan);
        persist(disk.sql(), &plan).unwrap();
        for event in &good[..cut] {
            append(disk.sql(), &plan, event.clone()).unwrap();
        }
        for (index, event) in good.iter().enumerate() {
            if index != cut {
                assert!(append(disk.sql(), &plan, event.clone()).is_err());
            }
        }
        let mut wrong = good[cut].clone();
        match &mut wrong {
            Event::JobIntent { nonce } | Event::BootstrapIntent { nonce } => {
                *nonce = "../invalid".into()
            }
            Event::JobCreated { nonce, .. } | Event::BootstrapCreated { nonce, .. } => {
                *nonce = random_id().unwrap()
            }
        }
        assert!(append(disk.sql(), &plan, wrong).is_err());
        assert_eq!(replay(disk.sql(), &plan).unwrap(), good[..cut]);
    }
    let disk = Disk::new(&plan);
    persist(disk.sql(), &plan).unwrap();
    for event in &good[..3] {
        append(disk.sql(), &plan, event.clone()).unwrap();
    }
    for invalid in 0..4 {
        let mut event = good[3].clone();
        if let Event::BootstrapCreated {
            pid,
            created,
            image_id,
            ..
        } = &mut event
        {
            match invalid {
                0 => *pid = 0,
                1 => *pid = plan.execution.pid,
                2 => *created = 0,
                _ => image_id[0] ^= 1,
            }
        }
        assert!(append(disk.sql(), &plan, event).is_err());
    }
    assert_eq!(replay(disk.sql(), &plan).unwrap(), good[..3]);
}

#[test]
fn atomic_lifecycle_immutable_events_reject_sql_replacement_and_oversized_data() {
    let plan = plan();
    let disk = Disk::new(&plan);
    persist(disk.sql(), &plan).unwrap();
    let first = events(&plan)[0].clone();
    append(disk.sql(), &plan, first.clone()).unwrap();
    for sql in [
        "UPDATE candidate_atomic_bootstrap_events SET event_json='{}'",
        "DELETE FROM candidate_atomic_bootstrap_events",
        "INSERT OR REPLACE INTO candidate_atomic_bootstrap_events VALUES(0,'{}')",
    ] {
        assert!(disk.sql().execute_batch(sql).is_err());
    }
    assert_eq!(replay(disk.sql(), &plan).unwrap(), [first]);
    disk.sql()
        .execute(
            "INSERT INTO candidate_atomic_bootstrap_events VALUES(1,?1)",
            ["x".repeat(MAX_EVENT_BYTES + 1)],
        )
        .unwrap();
    assert!(replay(disk.sql(), &plan).is_err());
    assert!(append(disk.sql(), &plan, events(&plan)[1].clone()).is_err());
    let size: usize = disk
        .sql()
        .query_row(
            "SELECT length(event_json) FROM candidate_atomic_bootstrap_events WHERE sequence=1",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(size, MAX_EVENT_BYTES + 1);
}

#[test]
fn atomic_lifecycle_rejects_gaps_unknown_fields_and_missing_intents() {
    let plan = plan();
    for (sequence,json) in [(1,serde_json::to_string(&events(&plan)[0]).unwrap()),
        (0,"{\"kind\":\"job_intent\",\"nonce\":\"1123456789abcdef0123456789abcdef\",\"extra\":true}".into()),
        (0,serde_json::to_string(&events(&plan)[1]).unwrap())] {
        let disk=Disk::new(&plan); persist(disk.sql(),&plan).unwrap();
        disk.sql().execute("INSERT INTO candidate_atomic_bootstrap_events VALUES(?1,?2)",params![sequence,json]).unwrap();
        assert!(replay(disk.sql(),&plan).is_err());
        assert!(append(disk.sql(),&plan,events(&plan)[0].clone()).is_err());
        let retained:String=disk.sql().query_row("SELECT event_json FROM candidate_atomic_bootstrap_events",[],|r|r.get(0)).unwrap();
        assert_eq!(retained,json);
    }
}
