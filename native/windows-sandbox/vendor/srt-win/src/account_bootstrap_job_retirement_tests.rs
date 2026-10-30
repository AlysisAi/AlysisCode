use super::super::super::super::super::tests::seeded;
use super::super::super::tests::selected;
use super::*;

fn seed_birth(sql: &Connection, plan: &Plan, ready: Vec<ResumeEvent>) -> Identity {
    for event in ready {
        append_resume(sql, plan, event).unwrap();
    }
    let logon = intent(sql, plan).unwrap();
    persist_intent(sql, plan, &logon).unwrap();
    let selection = selected(&logon, plan);
    persist_receipt(sql, plan, &selection).unwrap();
    let request = BirthRequest::new(selection, random_id().unwrap()).unwrap();
    let identity = Identity {
        operation: plan.operation.clone(),
        nonce: random_id().unwrap(),
        birth_nonce: request.nonce.clone(),
        pid: request.selection.bootstrap_pid + 1,
        created: request.selection.bootstrap_created + 1,
        image_id: plan.image_id,
    };
    assert!(
        append(
            sql,
            plan,
            Event::Intent {
                identity: identity.clone()
            }
        )
        .is_err()
    );
    append_birth(sql, plan, BirthEvent::Intent { request }).unwrap();
    assert!(
        append(
            sql,
            plan,
            Event::Intent {
                identity: identity.clone()
            }
        )
        .is_err()
    );
    append_birth(
        sql,
        plan,
        BirthEvent::Created {
            nonce: identity.birth_nonce.clone(),
            pid: identity.pid,
            created: identity.created,
            image_id: identity.image_id,
        },
    )
    .unwrap();
    identity
}

#[test]
fn account_job_retirement_preserves_cold_prefixes_without_reconstructing_authority() {
    for cut in 0..=2 {
        let (mut disk, plan, ready) = seeded();
        let identity = seed_birth(disk.sql(), &plan, ready);
        let events = [
            Event::Intent {
                identity: identity.clone(),
            },
            Event::Exited { identity },
        ];
        assert!(append(disk.sql(), &plan, events[1].clone()).is_err());
        for event in &events[..cut] {
            append(disk.sql(), &plan, event.clone()).unwrap();
        }
        disk.reopen();
        assert!(replay(disk.sql(), &plan).unwrap() == events[..cut]);
        if cut > 0 {
            assert!(append(disk.sql(), &plan, events[0].clone()).is_err());
        }
        if cut == 2 {
            assert!(append(disk.sql(), &plan, events[1].clone()).is_err());
        }
        assert!(replay(disk.sql(), &plan).unwrap() == events[..cut]);
    }
}

#[test]
fn account_job_retirement_refuses_changed_binding_and_schema_without_writing() {
    let (disk, plan, ready) = seeded();
    let identity = seed_birth(disk.sql(), &plan, ready);
    for case in 0..8 {
        let mut wrong = identity.clone();
        match case {
            0 => wrong.operation = random_id().unwrap(),
            1 => wrong.nonce = wrong.operation.clone(),
            2 => wrong.nonce = wrong.birth_nonce.clone(),
            3 => wrong.birth_nonce = random_id().unwrap(),
            4 => wrong.pid += 1,
            5 => wrong.created += 1,
            6 => wrong.image_id[0] ^= 1,
            _ => wrong.nonce = "../bad".into(),
        }
        assert!(
            append(disk.sql(), &plan, Event::Intent { identity: wrong }).is_err(),
            "case {case}"
        );
        assert!(replay(disk.sql(), &plan).unwrap().is_empty());
    }
    append(
        disk.sql(),
        &plan,
        Event::Intent {
            identity: identity.clone(),
        },
    )
    .unwrap();
    let mut other = identity.clone();
    other.nonce = random_id().unwrap();
    assert!(append(disk.sql(), &plan, Event::Exited { identity: other }).is_err());
    for sql in [
        "UPDATE candidate_atomic_job_retirement SET event_json='{}'",
        "DELETE FROM candidate_atomic_job_retirement",
        "INSERT OR REPLACE INTO candidate_atomic_job_retirement VALUES(0,'{}')",
    ] {
        assert!(disk.sql().execute_batch(sql).is_err());
    }
    disk.sql()
        .execute_batch("DROP TRIGGER atomic_job_retirement_no_update")
        .unwrap();
    assert!(replay(disk.sql(), &plan).is_err());
    let count: usize = disk
        .sql()
        .query_row(
            "SELECT count(*) FROM candidate_atomic_job_retirement",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 1);
}
