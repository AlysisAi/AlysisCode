use super::super::super::tests::selected;
use super::*;
use crate::account_state::execution::bootstrap_plan::lifecycle::resume::tests::seeded;

fn prepared() -> (
    crate::account_state::execution::bootstrap_plan::tests::Disk,
    Plan,
    Vec<Event>,
) {
    let (disk, plan, ready) = seeded();
    for event in ready {
        append_resume(disk.sql(), &plan, event).unwrap();
    }
    let logon = intent(disk.sql(), &plan).unwrap();
    persist_intent(disk.sql(), &plan, &logon).unwrap();
    let selection = selected(&logon, &plan);
    persist_receipt(disk.sql(), &plan, &selection).unwrap();
    let mut birth = BirthRequest::new(selection, random_id().unwrap()).unwrap();
    birth.purpose = Purpose::TrustedSelfCheckV1;
    let pid = birth.selection.bootstrap_pid + 1;
    let created = birth.selection.bootstrap_created + 1;
    append_birth(
        disk.sql(),
        &plan,
        BirthEvent::Intent {
            request: birth.clone(),
        },
    )
    .unwrap();
    append_birth(
        disk.sql(),
        &plan,
        BirthEvent::Created {
            nonce: birth.nonce.clone(),
            pid,
            created,
            image_id: plan.image_id,
        },
    )
    .unwrap();
    let request = probe::Request {
        birth,
        nonce: random_id().unwrap(),
        pid,
        created,
    };
    let events = vec![
        Event::Intent {
            request: request.clone(),
        },
        Event::Resumed {
            nonce: request.nonce.clone(),
        },
        Event::Exited {
            nonce: request.nonce,
            code: probe::EXIT_CODE,
        },
    ];
    (disk, plan, events)
}
#[test]
fn account_self_check_resume_cold_prefixes_cannot_retry_or_reorder() {
    for cut in 0..=3 {
        let (mut disk, plan, events) = prepared();
        for event in &events[..cut] {
            append(disk.sql(), &plan, event.clone()).unwrap();
        }
        disk.reopen();
        assert!(replay(disk.sql(), &plan).unwrap() == events[..cut]);
        for (index, event) in events.iter().enumerate() {
            if index != cut {
                assert!(append(disk.sql(), &plan, event.clone()).is_err());
            }
        }
        assert!(replay(disk.sql(), &plan).unwrap() == events[..cut]);
    }
}
#[test]
fn account_self_check_resume_requires_exact_birth_and_observations() {
    let (disk, plan, events) = prepared();
    for case in 0..6 {
        let mut wrong = events[0].clone();
        if let Event::Intent { request } = &mut wrong {
            match case {
                0 => request.birth.purpose = Purpose::ObserveOnly,
                1 => request.pid += 1,
                2 => request.created += 1,
                3 => request.birth.selection.image_id[0] ^= 1,
                4 => request.birth.nonce = random_id().unwrap(),
                _ => request.nonce = request.birth.nonce.clone(),
            }
        }
        assert!(append(disk.sql(), &plan, wrong).is_err());
    }
    append(disk.sql(), &plan, events[0].clone()).unwrap();
    assert!(
        append(
            disk.sql(),
            &plan,
            Event::Resumed {
                nonce: random_id().unwrap()
            }
        )
        .is_err()
    );
    append(disk.sql(), &plan, events[1].clone()).unwrap();
    let Event::Exited { nonce, .. } = &events[2] else {
        unreachable!()
    };
    assert!(
        append(
            disk.sql(),
            &plan,
            Event::Exited {
                nonce: nonce.clone(),
                code: 0
            }
        )
        .is_err()
    );
    append(disk.sql(), &plan, events[2].clone()).unwrap();
}
#[test]
fn account_self_check_resume_rejects_mutation_gap_oversize_and_old_schema() {
    let (disk, plan, events) = prepared();
    append(disk.sql(), &plan, events[0].clone()).unwrap();
    for sql in [
        "UPDATE candidate_atomic_account_resume SET event_json='{}'",
        "DELETE FROM candidate_atomic_account_resume",
        "INSERT OR REPLACE INTO candidate_atomic_account_resume VALUES(0,'{}')",
    ] {
        assert!(disk.sql().execute_batch(sql).is_err());
    }
    disk.sql()
        .execute(
            "INSERT INTO candidate_atomic_account_resume VALUES(2,?1)",
            [serde_json::to_string(&events[2]).unwrap()],
        )
        .unwrap();
    assert!(replay(disk.sql(), &plan).is_err());
    let (disk, plan, _) = prepared();
    disk.sql()
        .execute(
            "INSERT INTO candidate_atomic_account_resume VALUES(0,?1)",
            ["x".repeat(8193)],
        )
        .unwrap();
    assert!(replay(disk.sql(), &plan).is_err());
    let (disk, plan, _) = prepared();
    disk.sql()
        .execute_batch("DROP TABLE candidate_atomic_account_resume")
        .unwrap();
    assert!(require_plan(disk.sql(), &plan).is_err());
}
