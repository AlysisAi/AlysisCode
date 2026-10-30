use super::super::super::super::tests::seeded;
use super::super::tests::selected;
use super::*;

#[test]
fn account_birth_journal_requires_delivered_selection_and_preserves_exact_cold_prefixes() {
    for cut in 0..=2 {
        let (mut disk, plan, ready) = seeded();
        for event in ready {
            append_resume(disk.sql(), &plan, event).unwrap();
        }
        let logon = intent(disk.sql(), &plan).unwrap();
        persist_intent(disk.sql(), &plan, &logon).unwrap();
        let selection = selected(&logon, &plan);
        let request = BirthRequest::new(selection.clone(), random_id().unwrap()).unwrap();
        let events = [
            BirthEvent::Intent {
                request: request.clone(),
            },
            BirthEvent::Created {
                nonce: request.nonce.clone(),
                pid: request.selection.bootstrap_pid + 1,
                created: request.selection.bootstrap_created + 1,
                image_id: plan.image_id,
            },
        ];
        assert!(append_birth(disk.sql(), &plan, events[0].clone()).is_err());
        assert!(replay_birth(disk.sql(), &plan).unwrap().is_empty());
        persist_receipt(disk.sql(), &plan, &selection).unwrap();
        for event in &events[..cut] {
            append_birth(disk.sql(), &plan, event.clone()).unwrap();
        }
        disk.reopen();
        assert!(replay_birth(disk.sql(), &plan).unwrap() == events[..cut]);
        if cut > 0 {
            assert!(append_birth(disk.sql(), &plan, events[0].clone()).is_err());
        }
        if cut == 2 {
            assert!(append_birth(disk.sql(), &plan, events[1].clone()).is_err());
        }
        assert!(replay_birth(disk.sql(), &plan).unwrap() == events[..cut]);
    }
}

#[test]
fn account_birth_journal_rejects_cross_selection_native_identity_and_mutation_before_write() {
    let (disk, plan, ready) = seeded();
    for event in ready {
        append_resume(disk.sql(), &plan, event).unwrap();
    }
    let logon = intent(disk.sql(), &plan).unwrap();
    persist_intent(disk.sql(), &plan, &logon).unwrap();
    let selection = selected(&logon, &plan);
    persist_receipt(disk.sql(), &plan, &selection).unwrap();
    let request = BirthRequest::new(selection, random_id().unwrap()).unwrap();
    let mut wrong = request.clone();
    wrong.selection.credential_generation = format!("CredOwner-{}", random_id().unwrap());
    assert!(append_birth(disk.sql(), &plan, BirthEvent::Intent { request: wrong }).is_err());
    assert!(replay_birth(disk.sql(), &plan).unwrap().is_empty());
    append_birth(
        disk.sql(),
        &plan,
        BirthEvent::Intent {
            request: request.clone(),
        },
    )
    .unwrap();
    for case in 0..6 {
        let mut event = BirthEvent::Created {
            nonce: request.nonce.clone(),
            pid: request.selection.bootstrap_pid + 1,
            created: request.selection.bootstrap_created + 1,
            image_id: plan.image_id,
        };
        if let BirthEvent::Created {
            nonce,
            pid,
            created,
            image_id,
        } = &mut event
        {
            match case {
                0 => *nonce = random_id().unwrap(),
                1 => *pid = 0,
                2 => *pid = plan.execution.pid,
                3 => *pid = request.selection.bootstrap_pid,
                4 => *created = 0,
                _ => image_id[0] ^= 1,
            }
        }
        assert!(
            append_birth(disk.sql(), &plan, event).is_err(),
            "case {case}"
        );
        assert_eq!(replay_birth(disk.sql(), &plan).unwrap().len(), 1);
    }
    for sql in [
        "UPDATE candidate_atomic_account_birth SET event_json='{}'",
        "DELETE FROM candidate_atomic_account_birth",
        "INSERT OR REPLACE INTO candidate_atomic_account_birth VALUES(0,'{}')",
    ] {
        assert!(disk.sql().execute_batch(sql).is_err());
    }
    disk.sql()
        .execute_batch("DROP TRIGGER atomic_account_birth_no_update")
        .unwrap();
    assert!(replay_birth(disk.sql(), &plan).is_err());
    let count: usize = disk
        .sql()
        .query_row(
            "SELECT count(*) FROM candidate_atomic_account_birth",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 1);
}
