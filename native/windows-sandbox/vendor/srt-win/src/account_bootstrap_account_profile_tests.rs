use super::*;
use crate::account_state::execution::bootstrap_plan::lifecycle::resume::logon::delivery::tests::selected;
use crate::account_state::execution::bootstrap_plan::lifecycle::resume::tests::seeded;

fn prepared() -> (
    crate::account_state::execution::bootstrap_plan::tests::Disk,
    Plan,
    Vec<Event>,
) {
    let (disk, plan, ready) = seeded();
    for e in ready {
        append_resume(disk.sql(), &plan, e).unwrap();
    }
    let logon = intent(disk.sql(), &plan).unwrap();
    persist_intent(disk.sql(), &plan, &logon).unwrap();
    let selection = selected(&logon, &plan);
    persist_receipt(disk.sql(), &plan, &selection).unwrap();
    let mut birth = BirthRequest::new(selection, random_id().unwrap()).unwrap();
    birth.purpose = Purpose::TrustedUnusedProfileV1 {
        container: plan.execution.identity.container.clone(),
        package_sid: plan.execution.identity.package_sid.clone(),
        policy: 1,
    };
    let request = probe::Request {
        pid: birth.selection.bootstrap_pid + 1,
        created: birth.selection.bootstrap_created + 1,
        nonce: random_id().unwrap(),
        birth: birth.clone(),
    };
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
            nonce: birth.nonce,
            pid: request.pid,
            created: request.created,
            image_id: plan.image_id,
        },
    )
    .unwrap();
    super::super::append(
        disk.sql(),
        &plan,
        super::super::Event::Intent {
            request: request.clone(),
        },
    )
    .unwrap();
    super::super::append(
        disk.sql(),
        &plan,
        super::super::Event::Resumed {
            nonce: request.nonce.clone(),
        },
    )
    .unwrap();
    let events = [Phase::Create, Phase::Created, Phase::Remove, Phase::Removed]
        .iter()
        .map(|p| Event {
            request: request.clone(),
            phase: *p,
            folder: if *p == Phase::Create {
                None
            } else {
                Some([7; 24])
            },
        })
        .collect();
    (disk, plan, events)
}
#[test]
fn account_profile_cold_prefixes_preserve_pending_and_refuse_reordered_or_repeated_effects() {
    for cut in 0..=4 {
        let (mut disk, plan, events) = prepared();
        for e in &events[..cut] {
            append(disk.sql(), &plan, e.clone()).unwrap();
        }
        disk.reopen();
        assert!(replay(disk.sql(), &plan).unwrap() == events[..cut]);
        for (i, e) in events.iter().enumerate() {
            if i != cut {
                assert!(append(disk.sql(), &plan, e.clone()).is_err());
            }
        }
        assert!(replay(disk.sql(), &plan).unwrap() == events[..cut]);
    }
}
#[test]
fn account_profile_requires_exact_original_birth_purpose_resume_and_folder() {
    let (disk, plan, events) = prepared();
    for case in 0..8 {
        let mut wrong = events[0].clone();
        match case {
            0 => wrong.request.pid += 1,
            1 => wrong.request.created += 1,
            2 => wrong.request.nonce = random_id().unwrap(),
            3 => wrong.request.birth.purpose = Purpose::TrustedHandoffSelfCheckV1,
            4 => wrong.request.birth.nonce = random_id().unwrap(),
            5 => wrong.request.birth.selection.account.account_sid = "S-1-1-0".into(),
            6 => wrong.folder = Some([7; 24]),
            _ => {
                if let Purpose::TrustedUnusedProfileV1 { policy, .. } =
                    &mut wrong.request.birth.purpose
                {
                    *policy = 2;
                }
            }
        }
        assert!(append(disk.sql(), &plan, wrong).is_err());
    }
    append(disk.sql(), &plan, events[0].clone()).unwrap();
    let mut wrong = events[1].clone();
    wrong.folder = Some([0; 24]);
    assert!(append(disk.sql(), &plan, wrong).is_err());
    append(disk.sql(), &plan, events[1].clone()).unwrap();
    let mut wrong = events[2].clone();
    wrong.folder = Some([8; 24]);
    assert!(append(disk.sql(), &plan, wrong).is_err());
}
#[test]
fn account_profile_rejects_mutable_schema_unknown_fields_and_unbounded_frames() {
    let (disk, plan, events) = prepared();
    append(disk.sql(), &plan, events[0].clone()).unwrap();
    for sql in [
        "UPDATE candidate_atomic_account_profile SET event_json='{}'",
        "DELETE FROM candidate_atomic_account_profile",
        "INSERT OR REPLACE INTO candidate_atomic_account_profile VALUES(0,'{}')",
    ] {
        assert!(disk.sql().execute_batch(sql).is_err());
    }
    let e = &events[0];
    let bytes = e.wire().unwrap();
    assert!(Event::receive(&bytes, &e.request, Phase::Create, None).unwrap() == *e);
    assert!(Event::receive(&bytes, &e.request, Phase::Remove, None).is_err());
    assert!(Event::receive(&vec![0; 8193], &e.request, Phase::Create, None).is_err());
    let mut json = serde_json::to_value(e).unwrap();
    json["command"] = serde_json::json!("refused");
    assert!(serde_json::from_value::<Event>(json).is_err());
    disk.sql()
        .execute_batch("DROP TRIGGER atomic_account_profile_no_update")
        .unwrap();
    assert!(replay(disk.sql(), &plan).is_err());
}
#[test]
fn account_profile_write_failure_and_committed_readback_drift_cannot_advance() {
    struct Reset;
    impl Drop for Reset {
        fn drop(&mut self) {
            ACCOUNT_PROFILE_CHECKPOINT.with(|s| {
                s.borrow_mut().take();
            });
        }
    }
    for cut in 0..4 {
        let (mut disk, plan, events) = prepared();
        for e in &events[..cut] {
            append(disk.sql(), &plan, e.clone()).unwrap();
        }
        disk.sql().execute_batch("PRAGMA query_only=ON").unwrap();
        assert!(append(disk.sql(), &plan, events[cut].clone()).is_err());
        disk.reopen();
        assert!(replay(disk.sql(), &plan).unwrap() == events[..cut]);
    }
    let (disk, plan, events) = prepared();
    let _reset = Reset;
    ACCOUNT_PROFILE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(|phase, _, sql| {
            if phase == "committed" {
                sql.execute_batch("DROP TRIGGER atomic_account_profile_no_update")
                    .unwrap();
            }
        }))
    });
    assert!(append(disk.sql(), &plan, events[0].clone()).is_err());
    assert_eq!(
        disk.sql()
            .query_row(
                "SELECT count(*) FROM candidate_atomic_account_profile",
                [],
                |r| r.get::<_, usize>(0)
            )
            .unwrap(),
        1
    );
}
