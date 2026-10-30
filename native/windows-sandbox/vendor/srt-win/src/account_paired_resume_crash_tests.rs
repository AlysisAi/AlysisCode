//! The controller pins all four original processes while the consumer is still
//! suspended, then permits only the fixed probe. No PID reopen or job transfer.
use super::*;
use crate::account_control::bootstrap::entry::credential_wire::child::probe;
use paired_plan::RESUME_JOURNAL_CHECKPOINT;
use paired_plan::native::account::resume::RESUME_CHECKPOINT;

pub(super) fn is_resume(point: &str) -> bool {
    POINTS[5..].contains(&point)
}
fn exited(point: &str) -> bool {
    matches!(
        point,
        "paired-self-check-resumed-hot-receipt" | "paired-self-check-exited"
    )
}
pub(super) fn require_process_state(point: &str, kind: &str, process: &OwnedHandle) {
    if kind == "consumer-account" && exited(point) {
        assert_eq!(exited_code(process), probe::EXIT_CODE);
    } else {
        alive(process);
    }
}
pub(super) fn require_prefix(snapshot: &Snapshot, point: &str) {
    if !is_resume(point) {
        assert!(snapshot.resume.is_empty());
        return;
    }
    let count = if point == "paired-self-check-exited" {
        3
    } else {
        1
    };
    assert_eq!(snapshot.resume.len(), count);
    let rows: Vec<serde_json::Value> = snapshot
        .resume
        .iter()
        .enumerate()
        .map(|(i, row)| {
            assert_eq!((row.0, row.1), (1, i as u32));
            serde_json::from_str(&row.2).unwrap()
        })
        .collect();
    let acknowledged: serde_json::Value = serde_json::from_str(
        &snapshot
            .account
            .iter()
            .find(|r| r.0 == 1 && r.1 == 4)
            .unwrap()
            .2,
    )
    .unwrap();
    let request = &rows[0]["request"];
    assert_eq!(request["birth"], acknowledged["request"]);
    assert_eq!(request["birth"]["purpose"], "trusted_self_check_v1");
    assert_eq!(request["pid"], acknowledged["pid"]);
    assert_eq!(request["created"], acknowledged["created"]);
    assert_eq!(
        request["birth"]["selection"]["image_id"],
        acknowledged["image_id"]
    );
    validate_random_id(request["nonce"].as_str().unwrap()).unwrap();
    for (i, row) in rows.iter().enumerate() {
        assert_eq!(row["kind"], ["intent", "resumed", "exited"][i]);
        assert_eq!(&row["request"], request);
        if i == 2 {
            assert_eq!(row["code"], probe::EXIT_CODE);
        }
    }
}
pub(super) fn require_before(proof: &Proof) {
    assert!(is_resume(&proof.point));
    proof.snapshot.require_account_prefix(&proof.point);
    assert!(proof.snapshot.resume.is_empty());
}

pub(super) fn install(held: Held) {
    let mut held = Some(held);
    ACCOUNT_CRASH_CHECKPOINT.with(|slot| {
        *slot.borrow_mut() = Some(Box::new(move |phase, role, _sql, observer| {
            if phase != "acknowledged" || role != Role::Consumers {
                return;
            }
            let mut held = held.take().expect("consumer observation repeated");
            held.processes
                .push(("consumer-account".into(), retain_account(&observer)));
            let held = Rc::new(std::cell::RefCell::new(Some(held)));
            let before_resume = held.clone();
            RESUME_CHECKPOINT.with(|slot| {
                *slot.borrow_mut() = Some(Box::new(move |phase, sql| {
                    if phase != "before-intent" {
                        return;
                    }
                    let state = before_resume.borrow();
                    let original = state.as_ref().unwrap();
                    let snapshot = Snapshot::read(sql);
                    snapshot.require_account_prefix(&original.point);
                    assert!(snapshot.resume.is_empty());
                    for (_, process) in &original.processes {
                        alive(process);
                    }
                    original.publish(snapshot, "paired-account-observe.json");
                    // Native controller observation and exclusive membership
                    // checks must complete before this child may resume.
                    let ack = original.root.join("paired-account-observed.json");
                    let deadline = Instant::now() + Duration::from_secs(3);
                    while !ack.exists() {
                        assert!(
                            Instant::now() < deadline,
                            "original observer acknowledgment timed out"
                        );
                        std::thread::sleep(Duration::from_millis(10));
                    }
                    assert_eq!(std::fs::read(ack).unwrap(), original.nonce.as_bytes());
                    for (_, process) in &original.processes {
                        alive(process);
                    }
                }));
            });
            let mut before = None;
            RESUME_JOURNAL_CHECKPOINT.with(|slot| {
                *slot.borrow_mut() = Some(Box::new(move |phase, seq, sql| {
                    let state = held.borrow();
                    let original = state.as_ref().unwrap();
                    let hot = original.point == "paired-self-check-resumed-hot-receipt";
                    let path = crate::account_state::database_path(
                        &original.root,
                        &original.execution.identity.binding,
                    )
                    .unwrap();
                    if hot && phase == "committed" && seq == 0 {
                        let bytes = std::fs::read(&path).unwrap();
                        assert!(bytes.len() <= 1024 * 1024);
                        std::fs::write(original.root.join("paired-account-before-write.db"), bytes)
                            .unwrap();
                        before = Some(Snapshot::read(sql));
                    }
                    let selected = match original.point.as_str() {
                        "paired-self-check-intent" => phase == "committed" && seq == 0,
                        "paired-self-check-resumed-hot-receipt" => phase == "inserted" && seq == 1,
                        "paired-self-check-exited" => phase == "committed" && seq == 2,
                        _ => unreachable!(),
                    };
                    if !selected {
                        return;
                    }
                    let snapshot = if hot {
                        assert!(!sql.is_autocommit());
                        sql.cache_flush().unwrap();
                        let journal = std::fs::read(path.with_extension("db-journal")).unwrap();
                        assert!(journal.len() > 512 && journal.len() <= 1024 * 1024);
                        assert_eq!(
                            &journal[..8],
                            &[0xd9, 0xd5, 0x05, 0xf9, 0x20, 0xa1, 0x63, 0xd7]
                        );
                        assert_ne!(
                            std::fs::read(&path).unwrap(),
                            std::fs::read(original.root.join("paired-account-before-write.db"))
                                .unwrap()
                        );
                        before.take().unwrap()
                    } else {
                        Snapshot::read(sql)
                    };
                    drop(state);
                    held.borrow_mut().take().unwrap().hold_snapshot(snapshot);
                }));
            });
        }));
    });
}

/// Controller already owns original read-only references and has verified each
/// pinned image and exclusive membership while all four processes are alive.
pub(super) fn advance(fixture: &Fixture, before: &Proof, began: Instant) -> Result<Proof> {
    require_before(before);
    let part = fixture.root.join("paired-account-observed.part");
    std::fs::write(&part, before.nonce.as_bytes())?;
    std::fs::rename(part, fixture.root.join("paired-account-observed.json"))?;
    let path = fixture.root.join("paired-account-cut.json");
    while !path.exists() {
        ensure!(
            began.elapsed() < Duration::from_secs(8),
            "paired resume cut exceeded source timeout margin"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
    let bytes = std::fs::read(path)?;
    ensure!(bytes.len() < 65536, "paired resume cut exceeds byte bound");
    let proof: Proof = serde_json::from_slice(&bytes)?;
    ensure!(
        proof.nonce == before.nonce
            && proof.point == before.point
            && proof.execution == before.execution
            && proof.processes == before.processes
            && proof.snapshot.plan == before.snapshot.plan
            && proof.snapshot.bootstrap == before.snapshot.bootstrap
            && proof.snapshot.account == before.snapshot.account,
        "paired resume replaced original observer identities or birth evidence"
    );
    proof.snapshot.require_prefix(&proof.point);
    Ok(proof)
}

pub(super) fn verify_hot(fixture: &Fixture, proof: &Proof, marker: &[u8]) -> Result<()> {
    ensure!(
        proof.point == "paired-self-check-resumed-hot-receipt",
        "unexpected hot paired cut"
    );
    let path =
        crate::account_state::database_path(&fixture.root, &proof.execution.identity.binding)?;
    let journal = path.with_extension("db-journal");
    let original = std::fs::read(&path)?;
    let rollback = std::fs::read(&journal)?;
    let manifest = std::fs::read(path.with_file_name("identity.json"))?;
    ensure!(
        original.len() <= 1024 * 1024 && rollback.len() > 512 && rollback.len() <= 1024 * 1024,
        "paired hot database or journal exceeds bound"
    );
    ensure!(
        original != std::fs::read(fixture.root.join("paired-account-before-write.db"))?
            && rollback[..8] == [0xd9, 0xd5, 0x05, 0xf9, 0x20, 0xa1, 0x63, 0xd7],
        "paired receipt was not flushed into a hot transaction"
    );
    let unchanged = || -> Result<()> {
        ensure!(
            std::fs::read(&path)? == original
                && std::fs::read(&journal)? == rollback
                && std::fs::read(path.with_file_name("identity.json"))? == manifest
                && std::fs::read(fixture.path())? == marker,
            "paired hot evidence changed"
        );
        Ok(())
    };
    for _ in 0..2 {
        let error =
            PrivateDatabase::open_under(&fixture.root, &fixture.setup, AccountMode::Offline, None)
                .err()
                .context("paired hot original was admitted")?;
        ensure!(
            format!("{error:#}").contains("auxiliary or unknown object"),
            "paired hot admission refused for another reason"
        );
        unchanged()?;
    }
    let root = fixture.root.join("paired-account-rollback-oracle");
    std::fs::create_dir(&root)?;
    let copy = root.join("state.db");
    std::fs::write(&copy, &original)?;
    std::fs::write(copy.with_extension("db-journal"), &rollback)?;
    let sql = Connection::open_with_flags_and_vfs(
        fs::extended_path(&copy)?,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE,
        "win32-longpath",
    )?;
    ensure!(
        sql.query_row("PRAGMA integrity_check", [], |r| r.get::<_, String>(0))? == "ok",
        "paired rollback copy corrupt"
    );
    crate::account_state::validate_header(&sql, &proof.execution.identity.binding)?;
    assert_eq!(Snapshot::read(&sql), proof.snapshot);
    drop(sql);
    ensure!(
        !copy.with_extension("db-journal").exists(),
        "paired rollback oracle did not finish"
    );
    unchanged()
}
