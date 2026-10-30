//! Kill the original fixture broker after durable checkpoints, bypassing Rust
//! Drop. Retain only the original child process observation, never a job handle.
//! Clean boundaries and actual interrupted event transactions are exercised.
//! Hot originals remain refused and untouched; rollback is checked on a copy.
//! This is not production hot-journal recovery or in-flight logon qualification.
use super::*;
use crate::{account_control::ControlServer, account_state::image::PinnedRunnerImage};
use rusqlite::OptionalExtension;
use std::time::{Duration, Instant};
use windows::Win32::{
    Foundation::{
        DUPLICATE_HANDLE_OPTIONS, DuplicateHandle, GetHandleInformation, HANDLE_FLAG_INHERIT,
    },
    System::Threading::{GetProcessId, TerminateProcess},
};

const TEST: &str =
    "account_state::execution::tests::atomic_crash::atomic_lifecycle_held_broker_fixture";
const ROOT_ENV: &str = "ALYSIS_TEST_ATOMIC_CRASH_ROOT";
const NONCE_ENV: &str = "ALYSIS_TEST_ATOMIC_CRASH_NONCE";

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Checkpoint {
    nonce: String,
    checkpoint: String,
    execution: ExecutionRecord,
    plan_json: String,
    events: Vec<String>,
    resume_events: Vec<String>,
    logon_intent: Option<String>,
    credential_receipt: Option<String>,
    source_handle: Option<u64>,
    child_created: Option<u64>,
    hot_journal: bool,
}

fn checkpoint(
    control: &DiskCrashControl,
    database: &PrivateDatabase<'_>,
    active: &ActiveExecution<'_>,
    observation: Option<&OwnedHandle>,
) -> Checkpoint {
    let connection = database.connection().unwrap();
    let events = connection
        .prepare("SELECT event_json FROM candidate_atomic_bootstrap_events ORDER BY sequence")
        .unwrap()
        .query_map([], |r| r.get(0))
        .unwrap()
        .collect::<std::result::Result<Vec<String>, _>>()
        .unwrap();
    Checkpoint {
        nonce: control.nonce.clone(),
        checkpoint: control.checkpoint.clone(),
        execution: active.record().unwrap().clone(),
        plan_json: connection
            .query_row(
                "SELECT plan_json FROM candidate_atomic_bootstrap_plan",
                [],
                |r| r.get(0),
            )
            .unwrap(),
        events,
        resume_events: connection
            .prepare("SELECT event_json FROM candidate_atomic_bootstrap_resume ORDER BY sequence")
            .unwrap()
            .query_map([], |r| r.get(0))
            .unwrap()
            .collect::<std::result::Result<Vec<String>, _>>()
            .unwrap(),
        source_handle: observation.as_ref().map(|p| p.raw().0 as usize as u64),
        logon_intent: connection
            .query_row(
                "SELECT intent_json FROM candidate_atomic_account_logon WHERE singleton=1",
                [],
                |r| r.get(0),
            )
            .optional()
            .unwrap(),
        credential_receipt: connection
            .query_row(
                "SELECT receipt_json FROM candidate_atomic_credential_delivery WHERE singleton=1",
                [],
                |r| r.get(0),
            )
            .optional()
            .unwrap(),
        hot_journal: false,
        child_created: observation
            .as_ref()
            .map(|p| process_created(p.raw()).unwrap()),
    }
}

fn publish_and_hold(root: &std::path::Path, proof: &Checkpoint) -> ! {
    let pending = root.join("atomic-checkpoint.part");
    std::fs::write(&pending, serde_json::to_vec(proof).unwrap()).unwrap();
    std::fs::rename(&pending, root.join("atomic-checkpoint.json")).unwrap();
    std::thread::sleep(Duration::from_secs(20));
    panic!("controller did not terminate held atomic broker");
}

fn hold(
    fixture: &Fixture,
    control: &DiskCrashControl,
    database: &PrivateDatabase<'_>,
    active: &ActiveExecution<'_>,
    observation: Option<OwnedHandle>,
) -> ! {
    let proof = checkpoint(control, database, active, observation.as_ref());
    publish_and_hold(&fixture.root, &proof)
}

fn arm_hot_write(
    fixture: &Fixture,
    control: &DiskCrashControl,
    database: &PrivateDatabase<'_>,
    active: &ActiveExecution<'_>,
    observation: Option<OwnedHandle>,
) {
    let mut proof = checkpoint(control, database, active, observation.as_ref());
    proof.hot_journal = true;
    let root = fixture.root.clone();
    let path =
        crate::account_state::database_path(&root, &proof.execution.identity.binding).unwrap();
    let committed = std::fs::read(&path).unwrap();
    assert!(committed.len() <= 1024 * 1024);
    std::fs::write(root.join("atomic-before-write.db"), &committed).unwrap();
    let policy = policy(&fixture.setup).unwrap();
    let credential = control.checkpoint == "hot-credential-receipt";
    let hook: Box<dyn FnOnce(&Connection)> = Box::new(move |connection| {
        // The real lifecycle append is inside its transaction, after the
        // INSERT and before COMMIT. Force genuine dirty pages to the main
        // file, rather than manufacturing an empty '-journal' pathname.
        assert!(!connection.is_autocommit());
        let count: usize = connection
            .query_row(
                if credential {
                    "SELECT count(*) FROM candidate_atomic_credential_delivery"
                } else {
                    "SELECT count(*) FROM candidate_atomic_bootstrap_events"
                },
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(
            count,
            if credential {
                1
            } else {
                proof.events.len() + 1
            }
        );
        connection.cache_flush().unwrap();
        let journal_path = path.with_extension("db-journal");
        let journal = fs::Pinned::open(&journal_path, false, false).unwrap();
        policy.validate_inherited_file(&journal).unwrap();
        drop(journal); // Do not prevent Windows VFS cleanup in control cases.
        let bytes = std::fs::read(&journal_path).unwrap();
        assert!(bytes.len() > 512 && bytes.len() <= 1024 * 1024);
        assert_eq!(
            &bytes[..8],
            &[0xd9, 0xd5, 0x05, 0xf9, 0x20, 0xa1, 0x63, 0xd7]
        );
        assert_ne!(std::fs::read(&path).unwrap(), committed);
        // Keep the original read-only reference alive until forced death.
        assert_eq!(
            observation.as_ref().map(|p| p.raw().0 as usize as u64),
            proof.source_handle
        );
        publish_and_hold(&root, &proof)
    });
    if credential {
        let mut hook = Some(hook);
        bootstrap_plan::lifecycle::resume::logon::delivery::DELIVERY_CHECKPOINT.with(|slot| {
            assert!(slot.borrow().is_none());
            *slot.borrow_mut() = Some(Box::new(move |phase, connection| {
                if phase == "receipt-inserted" {
                    hook.take().unwrap()(connection);
                }
            }));
        });
    } else {
        bootstrap_plan::lifecycle::AFTER_EVENT_INSERT.with(|slot| {
            assert!(slot.borrow().is_none());
            *slot.borrow_mut() = Some(hook);
        });
    }
}

#[test]
#[ignore = "bounded subprocess of the original-handle crash qualification"]
fn atomic_lifecycle_held_broker_fixture() {
    let root = PathBuf::from(std::env::var_os(ROOT_ENV).unwrap());
    assert_eq!(root.parent().unwrap(), crate::util::test_temp_dir());
    assert!(
        root.file_name()
            .unwrap()
            .to_str()
            .unwrap()
            .starts_with("alysis-execution-test-")
    );
    assert_eq!(
        std::env::args().skip(1).collect::<Vec<_>>(),
        [
            TEST,
            "--exact",
            "--ignored",
            "--nocapture",
            "--test-threads=1"
        ]
    );
    let bytes = std::fs::read(root.join("atomic-control.json")).unwrap();
    assert!(bytes.len() < 65536);
    let control: DiskCrashControl = serde_json::from_slice(&bytes).unwrap();
    validate_random_id(&control.nonce).unwrap();
    assert_eq!(std::env::var(NONCE_ENV).unwrap(), control.nonce);
    assert!(matches!(
        control.checkpoint.as_str(),
        "plan"
            | "job"
            | "job-recorded"
            | "bootstrap"
            | "bootstrap-recorded"
            | "hot-bootstrap-intent"
            | "hot-bootstrap-receipt"
            | "running"
            | "resume-intent"
            | "resume-ack"
            | "ready-receipt"
            | "logon-before-intent"
            | "logon-committed-intent"
            | "credential-before-send"
            | "credential-acknowledged"
            | "credential-receipt-committed"
            | "hot-credential-receipt"
    ));
    let fixture = Fixture {
        root,
        setup: control.setup.clone(),
        preserve: true,
    };
    let database = protected_database(&fixture);
    let ready = ReadyInstallation::fixture(fixture.setup.clone(), &fixture.root).unwrap();
    let active = atomic_fixture_active(&fixture, &database);
    let image = PinnedRunnerImage::fixture_installed(&ready).unwrap();
    let (server, address) =
        ControlServer::prepare(&ready.binding(AccountMode::Offline).unwrap().account_sid).unwrap();
    let plan = bootstrap_plan::CommittedPlan::prepare(&database, &active, &image, &ready, &address)
        .unwrap();
    if control.checkpoint == "plan" {
        hold(&fixture, &control, &database, &active, None);
    }
    let job = plan.create_job().unwrap();
    if control.checkpoint == "job" {
        hold(&fixture, &control, &database, &active, None);
    }
    job.record_birth().unwrap();
    if control.checkpoint == "job-recorded" {
        hold(&fixture, &control, &database, &active, None);
    }
    if control.checkpoint == "hot-bootstrap-intent" {
        arm_hot_write(&fixture, &control, &database, &active, None);
    }
    let child = job.prepare_bootstrap().unwrap();
    if control.checkpoint == "bootstrap" {
        hold(
            &fixture,
            &control,
            &database,
            &active,
            Some(child.fixture_readonly_observer().unwrap()),
        );
    }
    if control.checkpoint == "hot-bootstrap-receipt" {
        arm_hot_write(
            &fixture,
            &control,
            &database,
            &active,
            Some(child.fixture_readonly_observer().unwrap()),
        );
    }
    child.record_birth().unwrap();
    let resume_cut = match control.checkpoint.as_str() {
        "resume-intent" => Some(1),
        "resume-ack" => Some(2),
        "ready-receipt" => Some(3),
        _ => None,
    };
    if let Some(cut) = resume_cut {
        let observer = child.fixture_readonly_observer().unwrap();
        let mut proof = checkpoint(&control, &database, &active, Some(&observer));
        let root = fixture.root.clone();
        bootstrap_plan::lifecycle::resume::AFTER_RESUME_EVENT.with(|slot|{
            assert!(slot.borrow().is_none());
            *slot.borrow_mut()=Some((cut,Box::new(move|connection|{
                proof.resume_events=connection.prepare("SELECT event_json FROM candidate_atomic_bootstrap_resume ORDER BY sequence").unwrap()
                    .query_map([],|r|r.get(0)).unwrap().collect::<std::result::Result<Vec<String>,_>>().unwrap();
                assert_eq!(proof.resume_events.len(),cut);
                assert_eq!(observer.raw().0 as usize as u64,proof.source_handle.unwrap());
                publish_and_hold(&root,&proof);
            })));
        });
        let _running = child.start(server, Duration::from_secs(3)).unwrap();
        panic!("resume checkpoint was not reached");
    }
    if matches!(
        control.checkpoint.as_str(),
        "logon-before-intent" | "logon-committed-intent"
    ) {
        let running = child.start(server, Duration::from_secs(3)).unwrap();
        let observer = running.fixture_readonly_observer().unwrap();
        if control.checkpoint == "logon-before-intent" {
            hold(&fixture, &control, &database, &active, Some(observer));
        }
        let mut proof = checkpoint(&control, &database, &active, Some(&observer));
        let root = fixture.root.clone();
        bootstrap_plan::lifecycle::resume::logon::AFTER_LOGON_INTENT.with(|slot| {
            assert!(slot.borrow().is_none());
            *slot.borrow_mut() = Some(Box::new(move |connection| {
                assert!(connection.is_autocommit());
                proof.logon_intent = Some(connection.query_row("SELECT intent_json FROM candidate_atomic_account_logon WHERE singleton=1", [], |r|r.get(0)).unwrap());
                assert_eq!(observer.raw().0 as usize as u64,proof.source_handle.unwrap());
                publish_and_hold(&root,&proof);
            }));
        });
        let _prepared = running.prepare_account_logon().unwrap();
        panic!("account logon intent checkpoint not reached");
    }
    if control.checkpoint.starts_with("credential-")
        || control.checkpoint == "hot-credential-receipt"
    {
        let running = child.start(server, Duration::from_secs(3)).unwrap();
        let observer = running.fixture_readonly_observer().unwrap();
        let prepared = running.prepare_account_logon().unwrap();
        if control.checkpoint == "hot-credential-receipt" {
            arm_hot_write(&fixture, &control, &database, &active, Some(observer));
        } else {
            let mut proof = checkpoint(&control, &database, &active, Some(&observer));
            let phase = control
                .checkpoint
                .strip_prefix("credential-")
                .unwrap()
                .to_owned();
            let root = fixture.root.clone();
            bootstrap_plan::lifecycle::resume::logon::delivery::DELIVERY_CHECKPOINT.with(|slot| {
            assert!(slot.borrow().is_none());
            *slot.borrow_mut() = Some(Box::new(move |seen, connection| {
                if seen == phase {
                    assert!(connection.is_autocommit());
                    proof.credential_receipt = connection.query_row("SELECT receipt_json FROM candidate_atomic_credential_delivery WHERE singleton=1", [], |r|r.get(0)).optional().unwrap();
                    assert_eq!(observer.raw().0 as usize as u64, proof.source_handle.unwrap());
                    publish_and_hold(&root, &proof);
                }
            }));
        });
        }
        let _delivered = prepared
            .fixture_deliver("Synthetic-crash-handoff-only!", Duration::from_secs(3))
            .unwrap();
        panic!("credential checkpoint not reached");
    }
    if control.checkpoint == "running" {
        let running = child.start(server, Duration::from_secs(3)).unwrap();
        hold(
            &fixture,
            &control,
            &database,
            &active,
            Some(running.fixture_readonly_observer().unwrap()),
        );
    }
    hold(
        &fixture,
        &control,
        &database,
        &active,
        Some(child.fixture_readonly_observer().unwrap()),
    );
}

struct Broker(std::process::Child);
impl Drop for Broker {
    fn drop(&mut self) {
        if self.0.try_wait().ok().flatten().is_none() {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
}

#[test]
fn atomic_lifecycle_abrupt_broker_death_preserves_every_durable_prefix() {
    for (count, checkpoint) in [
        "plan",
        "job",
        "job-recorded",
        "bootstrap",
        "bootstrap-recorded",
    ]
    .into_iter()
    .enumerate()
    {
        run_checkpoint(count, checkpoint, false);
    }
}

#[test]
fn atomic_lifecycle_interrupted_write_refuses_hot_original_and_copy_recovers_prefix() {
    run_checkpoint(2, "hot-bootstrap-intent", true);
    run_checkpoint(3, "hot-bootstrap-receipt", true);
}

#[test]
fn atomic_lifecycle_running_bootstrap_dies_on_broker_loss_and_preserves_ready_receipts() {
    for checkpoint in ["resume-intent", "resume-ack", "ready-receipt", "running"] {
        run_checkpoint(4, checkpoint, false);
    }
}

#[test]
fn atomic_lifecycle_account_logon_intent_crash_preserves_pending_state_without_replay() {
    for checkpoint in ["logon-before-intent", "logon-committed-intent"] {
        run_checkpoint(4, checkpoint, false);
    }
}

#[test]
fn atomic_lifecycle_credential_delivery_broker_crash_preserves_receipt_without_replay() {
    for checkpoint in [
        "credential-before-send",
        "credential-acknowledged",
        "credential-receipt-committed",
    ] {
        run_checkpoint(4, checkpoint, false);
    }
}

#[test]
fn atomic_lifecycle_credential_interrupted_receipt_refuses_hot_original_and_copy_has_no_receipt() {
    run_checkpoint(4, "hot-credential-receipt", true);
}

fn run_checkpoint(count: usize, checkpoint: &str, hot: bool) {
    let fixture = protected_fixture();
    let nonce = random_id().unwrap();
    std::fs::write(
        fixture.root.join("atomic-control.json"),
        serde_json::to_vec(&DiskCrashControl {
            setup: fixture.setup.clone(),
            checkpoint: checkpoint.into(),
            nonce: nonce.clone(),
        })
        .unwrap(),
    )
    .unwrap();
    let stdout = std::fs::File::create_new(fixture.root.join("atomic-broker.stdout.log")).unwrap();
    let stderr = std::fs::File::create_new(fixture.root.join("atomic-broker.stderr.log")).unwrap();
    let mut broker = Broker(
        std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                TEST,
                "--exact",
                "--ignored",
                "--nocapture",
                "--test-threads=1",
            ])
            .env(ROOT_ENV, &fixture.root)
            .env(NONCE_ENV, &nonce)
            .env_remove("RUST_TEST_THREADS")
            .stdin(std::process::Stdio::null())
            .stdout(stdout)
            .stderr(stderr)
            .spawn()
            .unwrap(),
    );
    let original = HANDLE(broker.0.as_raw_handle());
    let created = process_created(original).unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    let checkpoint_path = fixture.root.join("atomic-checkpoint.json");
    while !checkpoint_path.exists() {
        assert!(
            broker.0.try_wait().unwrap().is_none(),
            "held broker exited before checkpoint; logs preserved"
        );
        assert!(
            Instant::now() < deadline,
            "held broker checkpoint timeout; logs preserved"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
    let bytes = std::fs::read(&checkpoint_path).unwrap();
    assert!(bytes.len() <= 32768);
    let proof: Checkpoint = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(proof.nonce, nonce);
    assert_eq!(proof.checkpoint, checkpoint);
    assert_eq!(
        (proof.execution.pid, proof.execution.created),
        (broker.0.id(), created)
    );
    assert_eq!(proof.events.len(), count);
    assert_eq!(proof.hot_journal, hot);
    assert_eq!(
        proof.resume_events.len(),
        match checkpoint {
            "resume-intent" => 1,
            "resume-ack" => 2,
            "ready-receipt" | "running" | "logon-before-intent" | "logon-committed-intent" => 3,
            c if c.starts_with("credential-") || c == "hot-credential-receipt" => 3,
            _ => 0,
        }
    );
    assert_eq!(
        proof.logon_intent.is_some(),
        checkpoint == "logon-committed-intent"
            || checkpoint.starts_with("credential-")
            || checkpoint == "hot-credential-receipt"
    );
    assert_eq!(
        proof.credential_receipt.is_some(),
        checkpoint == "credential-receipt-committed"
    );
    assert!(
        !std::str::from_utf8(&bytes)
            .unwrap()
            .contains("Synthetic-crash-handoff-only")
    );
    let marker = std::fs::read(fixture.path()).unwrap();
    assert_eq!(
        serde_json::from_slice::<ExecutionRecord>(&marker).unwrap(),
        proof.execution
    );
    let child = proof.source_handle.map(|source| {
        assert!(source > 0 && source < isize::MAX as u64);
        let mut raw = HANDLE::default();
        // Wire integer is interpreted only in the original broker table.
        // No executable, terminate, duplicate, or job authority is acquired.
        unsafe {
            DuplicateHandle(
                original,
                HANDLE(source as usize as *mut _),
                GetCurrentProcess(),
                &mut raw,
                (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
                false,
                DUPLICATE_HANDLE_OPTIONS(0),
            )
            .unwrap();
        }
        let observation = OwnedHandle(raw);
        assert_eq!(Some(process_created(raw).unwrap()), proof.child_created);
        assert!(proof.child_created.unwrap() >= created);
        assert_ne!(unsafe { GetProcessId(raw) }, broker.0.id());
        assert_eq!(unsafe { WaitForSingleObject(raw, 0) }, WAIT_TIMEOUT);
        let mut flags = 0;
        unsafe {
            GetHandleInformation(raw, &mut flags).unwrap();
        }
        assert_eq!(flags & HANDLE_FLAG_INHERIT.0, 0);
        assert!(unsafe { TerminateProcess(raw, 1) }.is_err());
        let image = PinnedRunnerImage::fixture(&std::env::current_exe().unwrap()).unwrap();
        image.verify_process(raw).unwrap();
        observation
    });
    assert_eq!(child.is_some(), count >= 3);
    unsafe {
        TerminateProcess(original, 74).unwrap();
    }
    assert_eq!(
        unsafe { WaitForSingleObject(original, 5000) },
        WAIT_OBJECT_0
    );
    assert_eq!(broker.0.wait().unwrap().code(), Some(74));
    if let Some(child) = child {
        assert_eq!(
            unsafe { WaitForSingleObject(child.raw(), 5000) },
            WAIT_OBJECT_0,
            "original contained child survived broker death"
        );
        drop(child);
    }
    assert_eq!(atomic_job_counts(&proof.execution), None);
    if hot {
        verify_hot_original_and_copy(&fixture, &proof, &marker);
        eprintln!(
            "atomic hot {checkpoint}: preserved {}",
            fixture.root.display()
        );
        return;
    }
    let database = protected_database(&fixture);
    assert_eq!(database.file_identity(), proof.execution.database_id);
    let connection = database.connection().unwrap();
    assert_eq!(
        connection
            .query_row(
                "SELECT plan_json FROM candidate_atomic_bootstrap_plan",
                [],
                |r| r.get::<_, String>(0)
            )
            .unwrap(),
        proof.plan_json
    );
    let events = connection
        .prepare("SELECT event_json FROM candidate_atomic_bootstrap_events ORDER BY sequence")
        .unwrap()
        .query_map([], |r| r.get(0))
        .unwrap()
        .collect::<std::result::Result<Vec<String>, _>>()
        .unwrap();
    assert_eq!(events, proof.events);
    let resume_events = connection
        .prepare("SELECT event_json FROM candidate_atomic_bootstrap_resume ORDER BY sequence")
        .unwrap()
        .query_map([], |r| r.get(0))
        .unwrap()
        .collect::<std::result::Result<Vec<String>, _>>()
        .unwrap();
    assert_eq!(resume_events, proof.resume_events);
    let logon_intent: Option<String> = connection
        .query_row(
            "SELECT intent_json FROM candidate_atomic_account_logon WHERE singleton=1",
            [],
            |r| r.get(0),
        )
        .optional()
        .unwrap();
    assert_eq!(logon_intent, proof.logon_intent);
    let receipt: Option<String> = connection
        .query_row(
            "SELECT receipt_json FROM candidate_atomic_credential_delivery WHERE singleton=1",
            [],
            |r| r.get(0),
        )
        .optional()
        .unwrap();
    assert_eq!(receipt, proof.credential_receipt);
    let stopped = fixture
        .reopen_stopped(&proof.execution.identity.binding, database.file_identity())
        .unwrap();
    assert!(stopped.acl_recovery_record(None).is_err());
    assert!(ReadTreeJournal::open_stopped(&database, &stopped).is_err());
    assert_eq!(std::fs::read(fixture.path()).unwrap(), marker);
    eprintln!(
        "atomic abrupt {checkpoint}: preserved {}",
        fixture.root.display()
    );
}

fn verify_hot_original_and_copy(fixture: &Fixture, proof: &Checkpoint, marker: &[u8]) {
    let path =
        crate::account_state::database_path(&fixture.root, &proof.execution.identity.binding)
            .unwrap();
    let journal = path.with_extension("db-journal");
    let original = std::fs::read(&path).unwrap();
    let rollback = std::fs::read(&journal).unwrap();
    let manifest = std::fs::read(path.with_file_name("identity.json")).unwrap();
    assert!(original.len() <= 1024 * 1024 && rollback.len() <= 1024 * 1024);
    assert_ne!(
        original,
        std::fs::read(fixture.root.join("atomic-before-write.db")).unwrap()
    );
    assert_eq!(
        &rollback[..8],
        &[0xd9, 0xd5, 0x05, 0xf9, 0x20, 0xa1, 0x63, 0xd7]
    );
    for _ in 0..2 {
        // A production private open must refuse BEFORE SQLite can roll back
        // and discard this evidence. Repeated refusal cannot become adoption.
        let error =
            PrivateDatabase::open_under(&fixture.root, &fixture.setup, AccountMode::Offline, None)
                .err()
                .unwrap();
        assert!(format!("{error:#}").contains("auxiliary or unknown object"));
        assert_eq!(std::fs::read(&path).unwrap(), original);
        assert_eq!(std::fs::read(&journal).unwrap(), rollback);
        assert_eq!(
            std::fs::read(path.with_file_name("identity.json")).unwrap(),
            manifest
        );
        assert_eq!(std::fs::read(fixture.path()).unwrap(), marker);
    }
    // This isolated COPY is a test oracle, never an operational recovery path,
    // replacement DB, or source of live ownership. Keep the originals untouched.
    let copy_root = fixture.root.join("rollback-oracle");
    std::fs::create_dir(&copy_root).unwrap();
    let copy = copy_root.join("state.db");
    std::fs::write(&copy, &original).unwrap();
    std::fs::write(copy.with_extension("db-journal"), &rollback).unwrap();
    let sql = Connection::open_with_flags_and_vfs(
        fs::extended_path(&copy).unwrap(),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE,
        "win32-longpath",
    )
    .unwrap();
    assert_eq!(
        sql.query_row("PRAGMA integrity_check", [], |r| r.get::<_, String>(0))
            .unwrap(),
        "ok"
    );
    crate::account_state::validate_header(&sql, &proof.execution.identity.binding).unwrap();
    assert_eq!(
        sql.query_row(
            "SELECT plan_json FROM candidate_atomic_bootstrap_plan",
            [],
            |r| r.get::<_, String>(0)
        )
        .unwrap(),
        proof.plan_json
    );
    let events = sql
        .prepare("SELECT event_json FROM candidate_atomic_bootstrap_events ORDER BY sequence")
        .unwrap()
        .query_map([], |r| r.get::<_, String>(0))
        .unwrap()
        .collect::<std::result::Result<Vec<_>, _>>()
        .unwrap();
    assert_eq!(events, proof.events); // Interrupted intent/receipt never committed.
    let resume_events = sql
        .prepare("SELECT event_json FROM candidate_atomic_bootstrap_resume ORDER BY sequence")
        .unwrap()
        .query_map([], |r| r.get::<_, String>(0))
        .unwrap()
        .collect::<std::result::Result<Vec<_>, _>>()
        .unwrap();
    assert_eq!(resume_events, proof.resume_events);
    let intent: Option<String> = sql
        .query_row(
            "SELECT intent_json FROM candidate_atomic_account_logon WHERE singleton=1",
            [],
            |r| r.get(0),
        )
        .optional()
        .unwrap();
    assert_eq!(intent, proof.logon_intent);
    let receipt: Option<String> = sql
        .query_row(
            "SELECT receipt_json FROM candidate_atomic_credential_delivery WHERE singleton=1",
            [],
            |r| r.get(0),
        )
        .optional()
        .unwrap();
    assert_eq!(receipt, proof.credential_receipt);
    drop(sql);
    assert!(!copy.with_extension("db-journal").exists());
    assert_eq!(std::fs::read(&path).unwrap(), original);
    assert_eq!(std::fs::read(&journal).unwrap(), rollback);
    assert_eq!(std::fs::read(fixture.path()).unwrap(), marker);
}
