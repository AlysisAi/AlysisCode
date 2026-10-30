use super::super::super::tests::seeded;
use super::*;

pub(super) fn selected(intent: &Intent, plan: &Plan) -> Selection {
    Selection {
        protocol: Selection::protocol(),
        operation: intent.operation.clone(),
        logon: intent.nonce.clone(),
        ready: intent.ready.clone(),
        bootstrap_pid: intent.bootstrap_pid,
        bootstrap_created: intent.bootstrap_created,
        image_id: intent.image_id,
        session: plan.session,
        account: intent.account.clone(),
        credential_generation: intent.credential_generation.clone(),
    }
}

#[test]
fn credential_receipt_requires_original_intent_before_any_write_and_survives_cold_reopen() {
    let (mut disk, plan, ready) = seeded();
    for event in ready {
        append_resume(disk.sql(), &plan, event).unwrap();
    }
    let intent = intent(disk.sql(), &plan).unwrap();
    let selection = selected(&intent, &plan);
    assert!(persist_receipt(disk.sql(), &plan, &selection).is_err());
    assert!(read_receipt(disk.sql(), &plan).unwrap().is_none());
    persist_intent(disk.sql(), &plan, &intent).unwrap();
    for case in 0..12 {
        let mut wrong = selection.clone();
        match case {
            0 => wrong.operation = random_id().unwrap(),
            1 => wrong.logon = random_id().unwrap(),
            2 => wrong.ready = random_id().unwrap(),
            3 => wrong.bootstrap_pid += 1,
            4 => wrong.bootstrap_created += 1,
            5 => wrong.image_id[0] ^= 1,
            6 => wrong.session += 1,
            7 => wrong.account.owner_sid.push('1'),
            8 => wrong.account.account_sid.push('1'),
            9 => wrong.account.group_sid.push('1'),
            10 => wrong.account.installation_id = random_id().unwrap(),
            _ => wrong.credential_generation = format!("CredOwner-{}", random_id().unwrap()),
        }
        assert!(
            persist_receipt(disk.sql(), &plan, &wrong).is_err(),
            "case {case}"
        );
        assert!(read_receipt(disk.sql(), &plan).unwrap().is_none());
    }
    persist_receipt(disk.sql(), &plan, &selection).unwrap();
    disk.reopen();
    let receipt = read_receipt(disk.sql(), &plan).unwrap().unwrap();
    assert!(receipt.selection == selection);
    assert!(persist_receipt(disk.sql(), &plan, &selection).is_err());
    for sql in [
        "UPDATE candidate_atomic_credential_delivery SET receipt_json='{}'",
        "DELETE FROM candidate_atomic_credential_delivery",
        "INSERT OR REPLACE INTO candidate_atomic_credential_delivery VALUES(1,'{}')",
    ] {
        assert!(disk.sql().execute_batch(sql).is_err());
    }
    assert!(read_receipt(disk.sql(), &plan).unwrap().as_ref() == Some(&receipt));
}

#[test]
fn credential_receipt_rejects_schema_payload_drift_and_readonly_write_without_repair() {
    for scenario in ["schema", "unknown-field", "oversize", "readonly"] {
        let (disk, plan, ready) = seeded();
        for event in ready {
            append_resume(disk.sql(), &plan, event).unwrap();
        }
        let intent = intent(disk.sql(), &plan).unwrap();
        persist_intent(disk.sql(), &plan, &intent).unwrap();
        let selection = selected(&intent, &plan);
        if scenario == "schema" {
            disk.sql()
                .execute_batch("DROP TRIGGER atomic_delivery_no_update")
                .unwrap();
            assert!(persist_receipt(disk.sql(), &plan, &selection).is_err());
            assert!(read_receipt(disk.sql(), &plan).is_err());
        } else if scenario == "readonly" {
            disk.sql().execute_batch("PRAGMA query_only=ON").unwrap();
            assert!(persist_receipt(disk.sql(), &plan, &selection).is_err());
            assert!(read_receipt(disk.sql(), &plan).unwrap().is_none());
        } else {
            let payload = if scenario == "oversize" {
                "x".repeat(8193)
            } else {
                let mut value = serde_json::to_value(Receipt {
                    protocol: "alysis-credential-delivered-v1".into(),
                    selection,
                })
                .unwrap();
                value["synthetic-sensitive-input"] = serde_json::json!("not-a-credential");
                value.to_string()
            };
            disk.sql()
                .execute(
                    "INSERT INTO candidate_atomic_credential_delivery VALUES(1,?1)",
                    [&payload],
                )
                .unwrap();
            let error = read_receipt(disk.sql(), &plan).err().unwrap();
            assert!(!format!("{error:#}").contains("synthetic-sensitive-input"));
            let kept: String = disk
                .sql()
                .query_row(
                    "SELECT receipt_json FROM candidate_atomic_credential_delivery",
                    [],
                    |r| r.get(0),
                )
                .unwrap();
            assert_eq!(kept, payload);
        }
    }
}
