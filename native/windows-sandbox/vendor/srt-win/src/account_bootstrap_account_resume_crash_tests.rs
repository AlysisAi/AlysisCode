//! Fixed self-check cuts before resume, inside its acknowledgment commit, and
//! after original native exit. Query-only observers never keep the job alive.
use super::*;
use bootstrap_plan::lifecycle::resume::logon::delivery::birth::account_resume::ACCOUNT_RESUME_CHECKPOINT;

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
        *slot.borrow_mut() = Some(Box::new(move |phase, original, _sql| {
            if phase != "acknowledged" {
                return Ok(());
            }
            let point = point.clone();
            let nonce = nonce.clone();
            let execution = execution.clone();
            let root = root.clone();
            let mut original = Some(original);
            let mut before: Option<(Snapshot, Vec<u8>)> = None;
            ACCOUNT_RESUME_CHECKPOINT.with(|slot| {
                *slot.borrow_mut() = Some(Box::new(move |phase, seq, sql| {
                    let selected = match point.as_str() {
                        "self-check-intent" => phase == "committed" && seq == 0,
                        "self-check-resumed-hot-receipt" => phase == "inserted" && seq == 1,
                        "self-check-exited" => phase == "committed" && seq == 2,
                        _ => panic!("unknown self-check crash cut"),
                    };
                    let path =
                        crate::account_state::database_path(&root, &execution.identity.binding)
                            .unwrap();
                    if point == "self-check-resumed-hot-receipt" && phase == "committed" && seq == 0
                    {
                        let bytes = std::fs::read(&path).unwrap();
                        assert!(bytes.len() <= 1024 * 1024);
                        std::fs::write(root.join("account-before-write.db"), &bytes).unwrap();
                        before = Some((Snapshot::read(sql).unwrap(), bytes));
                    }
                    if !selected {
                        return;
                    }
                    let original = original.take().unwrap();
                    let (handle, child_pid, child_created) = if point == "self-check-intent" {
                        original.fixture_source_identity().unwrap()
                    } else {
                        original.fixture_exited_self_check_identity().unwrap()
                    };
                    let snapshot = if point.ends_with("hot-receipt") {
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
    let expected = if proof.point == "self-check-exited" {
        3
    } else {
        1
    };
    ensure!(
        proof.snapshot.account_resume.len() == expected,
        "self-check resume prefix differs"
    );
    let intent: serde_json::Value = serde_json::from_str(&proof.snapshot.account_resume[0])?;
    let birth: serde_json::Value = serde_json::from_str(&proof.snapshot.birth[0])?;
    let created: serde_json::Value = serde_json::from_str(&proof.snapshot.birth[1])?;
    let request = &intent["request"];
    ensure!(
        intent["kind"] == "intent"
            && request["birth"] == birth["request"]
            && request["birth"]["purpose"] == "trusted_self_check_v1"
            && request["pid"] == proof.child_pid
            && request["created"] == proof.child_created
            && created["pid"] == proof.child_pid
            && created["created"] == proof.child_created
            && request["birth"]["selection"]["image_id"] == created["image_id"],
        "self-check original resume/birth identities differ"
    );
    validate_random_id(
        request["nonce"]
            .as_str()
            .context("self-check nonce missing")?,
    )?;
    if expected == 3 {
        let resumed: serde_json::Value = serde_json::from_str(&proof.snapshot.account_resume[1])?;
        let exited: serde_json::Value = serde_json::from_str(&proof.snapshot.account_resume[2])?;
        ensure!(resumed["kind"] == "resumed" && exited["kind"] == "exited"
            && resumed["nonce"] == request["nonce"] && exited["nonce"] == request["nonce"]
            && exited["code"] == crate::account_control::bootstrap::entry::credential_wire::child::probe::EXIT_CODE,
            "self-check completion differs");
    }
    Ok(())
}
