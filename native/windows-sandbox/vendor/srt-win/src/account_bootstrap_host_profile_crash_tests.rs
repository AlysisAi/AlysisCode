//! Original broker loss after the job exits, while used profile cleanup is live.
use super::*;
use bootstrap_plan::host_profile::{HOST_PROFILE_CHECKPOINT, HOST_PROFILE_WRITE_CHECKPOINT};
use std::{cell::RefCell, rc::Rc};

pub(super) fn install(
    point: &str,
    nonce: &str,
    execution: &ExecutionRecord,
    root: &std::path::Path,
) {
    let child = Rc::new(RefCell::new(None));
    let retained = child.clone();
    BIRTH_NATIVE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, original, _| {
            if phase == "observed" {
                ensure!(
                    retained.borrow().is_none(),
                    "profile crash child observed twice"
                );
                let identity = original.fixture_source_identity()?;
                *retained.borrow_mut() = Some((original, identity));
            }
            Ok(())
        }));
    });
    let point = point.to_owned();
    let nonce = nonce.to_owned();
    let execution = execution.clone();
    let root = root.to_owned();
    HOST_PROFILE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, sql, used| {
            if phase == "using" {
                used.context("used profile absent at crash setup")?
                    .fixture_add_registry_data(false)?;
                let path =
                    crate::account_profile_filesystem::RetainedProfileFilesystem::fixture_ac(
                        &execution.identity.container,
                        &execution.owner,
                    )?
                    .join("atomic-crash-data.bin");
                std::fs::write(path, b"bounded crash profile data")?;
            }
            let selected = match point.as_str() {
                "host-profile-used-captured" => "captured",
                "host-profile-used-closed" => "closed",
                "host-profile-used-hot-receipt" => "observed_after_use",
                _ => anyhow::bail!("unknown post-use profile crash cut"),
            };
            if phase != selected {
                return Ok(());
            }
            ensure!(
                atomic_job_counts(&execution).is_none(),
                "profile capture preceded whole-job exit"
            );
            let (original, (handle, child_pid, child_created)) = child
                .borrow_mut()
                .take()
                .context("profile crash original child absent")?;
            let proof = Proof {
                nonce: nonce.clone(),
                point: point.clone(),
                execution: execution.clone(),
                source_handle: handle as u64,
                child_pid,
                child_created,
                snapshot: Snapshot::read(sql)?,
            };
            if !point.ends_with("hot-receipt") {
                hold(&root, &proof, original);
            }
            let path = crate::account_state::database_path(&root, &execution.identity.binding)?;
            let before = std::fs::read(&path)?;
            ensure!(
                before.len() <= 1024 * 1024,
                "profile crash database exceeds bound"
            );
            std::fs::write(root.join("account-before-write.db"), &before)?;
            let root = root.clone();
            let mut held = Some((proof, original));
            HOST_PROFILE_WRITE_CHECKPOINT.with(|s| {
                *s.borrow_mut() = Some(Box::new(move |sequence, sql| {
                    if sequence != 3 {
                        return;
                    }
                    assert!(!sql.is_autocommit());
                    sql.cache_flush().unwrap();
                    let bytes = std::fs::read(path.with_extension("db-journal")).unwrap();
                    assert!(bytes.len() > 512 && bytes.len() <= 1024 * 1024);
                    assert_eq!(
                        &bytes[..8],
                        &[0xd9, 0xd5, 0x05, 0xf9, 0x20, 0xa1, 0x63, 0xd7]
                    );
                    assert_ne!(std::fs::read(&path).unwrap(), before);
                    let (proof, original) = held.take().unwrap();
                    hold(&root, &proof, original);
                }));
            });
            Ok(())
        }));
    });
}

pub(super) fn verify_prefix(proof: &Proof) -> Result<()> {
    let s = &proof.snapshot;
    let plan: serde_json::Value = serde_json::from_str(&s.plan)?;
    ensure!(
        s.birth.len() == 2 && s.retirement.len() == 2,
        "post-use crash preceded original job completion"
    );
    let birth: serde_json::Value = serde_json::from_str(&s.birth[1])?;
    let intent: serde_json::Value = serde_json::from_str(&s.retirement[0])?;
    let exited: serde_json::Value = serde_json::from_str(&s.retirement[1])?;
    let identity = &exited["identity"];
    ensure!(
        intent["kind"] == "intent"
            && exited["kind"] == "exited"
            && intent["identity"] == *identity
            && identity["operation"] == plan["operation"]
            && identity["pid"] == proof.child_pid
            && identity["created"] == proof.child_created
            && identity["birth_nonce"] == birth["nonce"]
            && identity["image_id"] == birth["image_id"],
        "post-use crash completion differs from original child"
    );
    validate_random_id(
        identity["nonce"]
            .as_str()
            .context("retirement nonce absent")?,
    )?;
    let born: serde_json::Value = serde_json::from_str(&s.host_profile[1])?;
    let used: serde_json::Value = serde_json::from_str(&s.host_profile[2])?;
    ensure!(
        used["kind"] == "use_intent"
            && used["binding"] == born["binding"]
            && used["folder_id"] == born["folder_id"]
            && used["nonce"] != used["binding"]["nonce"]
            && used["nonce"] != plan["operation"],
        "profile crash use prefix differs"
    );
    validate_random_id(used["nonce"].as_str().context("use nonce absent")?)?;
    if let Some(row) = s.host_profile.get(3) {
        let captured: serde_json::Value = serde_json::from_str(row)?;
        ensure!(
            captured["kind"] == "captured"
                && captured["binding"] == born["binding"]
                && captured["folder_id"] == born["folder_id"]
                && captured["nonce"] == used["nonce"]
                && captured["retirement"] == identity["nonce"],
            "profile crash capture prefix differs"
        );
    }
    if let Some(row) = s.host_profile.get(4) {
        let removal: serde_json::Value = serde_json::from_str(row)?;
        ensure!(
            removal["kind"] == "remove_intent"
                && removal["binding"] == born["binding"]
                && removal["folder_id"] == born["folder_id"]
                && removal["retirement"] == identity["nonce"],
            "profile crash removal prefix differs"
        );
    }
    Ok(())
}
