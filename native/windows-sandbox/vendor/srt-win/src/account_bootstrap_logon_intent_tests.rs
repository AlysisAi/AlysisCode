use super::super::tests::seeded;
use super::*;

#[test]
fn account_logon_intent_requires_complete_readiness_and_survives_cold_reopen_as_data() {
    for cut in 0..=3 {
        let (mut disk, plan, ready) = seeded();
        for event in &ready[..cut] {
            append_resume(disk.sql(), &plan, event.clone()).unwrap();
        }
        if cut != 3 {
            assert!(intent(disk.sql(), &plan).is_err());
            assert!(read(disk.sql(), &plan).unwrap().is_none());
            continue;
        }
        let expected = intent(disk.sql(), &plan).unwrap();
        persist_intent(disk.sql(), &plan, &expected).unwrap();
        disk.reopen();
        assert_eq!(read(disk.sql(), &plan).unwrap(), Some(expected.clone()));
        assert!(persist_intent(disk.sql(), &plan, &expected).is_err());
        assert!(persist_intent(disk.sql(), &plan, &intent(disk.sql(), &plan).unwrap()).is_err());
        assert_eq!(read(disk.sql(), &plan).unwrap(), Some(expected));
    }
}

#[test]
fn account_logon_intent_refuses_account_generation_native_identity_and_operation_substitution() {
    let (disk, plan, ready) = seeded();
    for event in ready {
        append_resume(disk.sql(), &plan, event).unwrap();
    }
    let expected = intent(disk.sql(), &plan).unwrap();
    for case in 0..11 {
        let mut wrong = expected.clone();
        match case {
            0 => wrong.protocol.push('x'),
            1 => wrong.operation = random_id().unwrap(),
            2 => wrong.nonce = wrong.ready.clone(),
            3 => wrong.ready = random_id().unwrap(),
            4 => wrong.bootstrap_pid += 1,
            5 => wrong.bootstrap_created += 1,
            6 => wrong.image_id[0] ^= 1,
            7 => wrong.account.owner_sid.push('1'),
            8 => wrong.account.account_sid.push('1'),
            9 => wrong.credential_generation.push('1'),
            _ => wrong.nonce = plan.operation.clone(),
        }
        assert!(
            persist_intent(disk.sql(), &plan, &wrong).is_err(),
            "case {case}"
        );
        assert!(read(disk.sql(), &plan).unwrap().is_none());
    }
}

#[test]
fn account_logon_intent_preserves_committed_record_and_refuses_schema_or_payload_drift() {
    for scenario in ["immutable", "missing-schema", "oversized", "foreign-fields"] {
        let (disk, plan, ready) = seeded();
        for event in ready {
            append_resume(disk.sql(), &plan, event).unwrap();
        }
        let expected = intent(disk.sql(), &plan).unwrap();
        if scenario == "immutable" {
            persist_intent(disk.sql(), &plan, &expected).unwrap();
            for sql in [
                "UPDATE candidate_atomic_account_logon SET intent_json='{}'",
                "DELETE FROM candidate_atomic_account_logon",
                "INSERT OR REPLACE INTO candidate_atomic_account_logon VALUES(1,'{}')",
            ] {
                assert!(disk.sql().execute_batch(sql).is_err());
            }
            assert_eq!(read(disk.sql(), &plan).unwrap(), Some(expected));
        } else if scenario == "missing-schema" {
            disk.sql()
                .execute_batch("DROP TABLE candidate_atomic_account_logon")
                .unwrap();
            assert!(require_plan(disk.sql(), &plan).is_err());
            assert!(read(disk.sql(), &plan).is_err());
        } else {
            let payload = if scenario == "oversized" {
                "x".repeat(MAX_BYTES + 1)
            } else {
                let mut value = serde_json::to_value(expected).unwrap();
                value["password"] = serde_json::json!("synthetic-rejected-field");
                value.to_string()
            };
            disk.sql()
                .execute(
                    "INSERT INTO candidate_atomic_account_logon VALUES(1,?1)",
                    [&payload],
                )
                .unwrap();
            assert!(read(disk.sql(), &plan).is_err());
            let kept: String = disk
                .sql()
                .query_row(
                    "SELECT intent_json FROM candidate_atomic_account_logon",
                    [],
                    |r| r.get(0),
                )
                .unwrap();
            assert_eq!(kept, payload);
        }
    }
}

#[test]
fn account_logon_intent_write_failure_creates_no_retry_authority() {
    let (disk, plan, ready) = seeded();
    for event in ready {
        append_resume(disk.sql(), &plan, event).unwrap();
    }
    let expected = intent(disk.sql(), &plan).unwrap();
    disk.sql().execute_batch("PRAGMA query_only=ON").unwrap();
    assert!(persist_intent(disk.sql(), &plan, &expected).is_err());
    assert!(read(disk.sql(), &plan).unwrap().is_none());
    // A cold empty table or this failed pure data write cannot construct the
    // PreparedAccountLogon type; only the consumed original running guard can.
}
