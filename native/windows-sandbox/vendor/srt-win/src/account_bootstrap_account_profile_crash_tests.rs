//! Actual account-helper death under the original job while its broker is killed
//! before a profile effect or inside either native-effect receipt transaction.
//! Cold evidence never reopens SDK resources or reconstructs cleanup authority.
use super::*;
use bootstrap_plan::lifecycle::resume::logon::delivery::birth::account_resume::account_profile::ACCOUNT_PROFILE_CHECKPOINT;

pub(super) fn install(
    point: &str,
    nonce: &str,
    execution: &ExecutionRecord,
    root: &std::path::Path,
) {
    let point = point.to_owned();
    let nonce = nonce.to_owned();
    let execution = execution.clone();
    let root = root.to_owned();
    BIRTH_NATIVE_CHECKPOINT.with(|slot| {
        *slot.borrow_mut() = Some(Box::new(move |phase, original, _| {
            if phase != "acknowledged" {
                return Ok(());
            }
            let point = point.clone();
            let nonce = nonce.clone();
            let execution = execution.clone();
            let root = root.clone();
            let mut original = Some(original);
            let mut before: Option<(Snapshot, Vec<u8>)> = None;
            ACCOUNT_PROFILE_CHECKPOINT.with(|slot| {
                *slot.borrow_mut() = Some(Box::new(move |phase, seq, sql| {
                    let (cut, hot) = match point.as_str() {
                        "account-profile-intent" => (0, false),
                        "account-profile-created-hot-receipt" => (1, true),
                        "account-profile-removed-hot-receipt" => (3, true),
                        _ => panic!("unknown account profile cut"),
                    };
                    let path =
                        crate::account_state::database_path(&root, &execution.identity.binding)
                            .unwrap();
                    if hot && phase == "committed" && seq + 1 == cut {
                        let bytes = std::fs::read(&path).unwrap();
                        assert!(bytes.len() <= 1024 * 1024);
                        std::fs::write(root.join("account-before-write.db"), &bytes).unwrap();
                        before = Some((Snapshot::read(sql).unwrap(), bytes));
                    }
                    if seq != cut || phase != if hot { "inserted" } else { "committed" } {
                        return;
                    }
                    let original = original.take().unwrap();
                    let (handle, child_pid, child_created) =
                        original.fixture_source_identity().unwrap();
                    let snapshot = if hot {
                        assert!(!sql.is_autocommit());
                        sql.cache_flush().unwrap();
                        let journal = std::fs::read(path.with_extension("db-journal")).unwrap();
                        assert!(journal.len() > 512 && journal.len() <= 1024 * 1024);
                        assert_eq!(
                            &journal[..8],
                            &[0xd9, 0xd5, 0x05, 0xf9, 0x20, 0xa1, 0x63, 0xd7]
                        );
                        let (snapshot, bytes) = before.take().unwrap();
                        assert_ne!(std::fs::read(&path).unwrap(), bytes);
                        snapshot
                    } else {
                        Snapshot::read(sql).unwrap()
                    };
                    let proof = Proof {
                        nonce: nonce.clone(),
                        point: point.clone(),
                        execution: execution.clone(),
                        source_handle: handle as u64,
                        child_pid,
                        child_created,
                        snapshot,
                    };
                    hold(&root, &proof, original);
                }));
            });
            Ok(())
        }));
    });
}
pub(super) fn verify_prefix(proof: &Proof) -> Result<()> {
    let expected = match proof.point.as_str() {
        "account-profile-intent" | "account-profile-created-hot-receipt" => 1,
        "account-profile-removed-hot-receipt" => 3,
        _ => anyhow::bail!("unknown account profile proof"),
    };
    ensure!(
        proof.snapshot.account_profile.len() == expected
            && proof.snapshot.account_resume.len() == 2,
        "account profile crash prefix differs"
    );
    let resume: serde_json::Value = serde_json::from_str(&proof.snapshot.account_resume[0])?;
    let resumed: serde_json::Value = serde_json::from_str(&proof.snapshot.account_resume[1])?;
    let birth: serde_json::Value = serde_json::from_str(&proof.snapshot.birth[0])?;
    let created: serde_json::Value = serde_json::from_str(&proof.snapshot.birth[1])?;
    let request = &resume["request"];
    ensure!(
        resume["kind"] == "intent"
            && resumed["kind"] == "resumed"
            && resumed["nonce"] == request["nonce"]
            && request["birth"] == birth["request"]
            && request["pid"] == proof.child_pid
            && request["created"] == proof.child_created
            && created["pid"] == proof.child_pid
            && created["created"] == proof.child_created
            && request["birth"]["purpose"]["trusted_unused_profile_v1"]["policy"] == 1,
        "account profile proof differs from original helper"
    );
    let mut folder = serde_json::Value::Null;
    for (i, row) in proof.snapshot.account_profile.iter().enumerate() {
        let event: serde_json::Value = serde_json::from_str(row)?;
        ensure!(
            event["request"] == *request && event["phase"] == ["create", "created", "remove"][i],
            "account profile crash event binding differs"
        );
        if i == 0 {
            ensure!(event["folder"].is_null(), "premature profile receipt");
        } else if i == 1 {
            let value: [u8; 24] = serde_json::from_value(event["folder"].clone())?;
            ensure!(value != [0; 24], "missing native account profile identity");
            folder = event["folder"].clone();
        } else {
            ensure!(
                event["folder"] == folder,
                "original account storage changed"
            );
        }
    }
    Ok(())
}
