//! Isolated filesystem/SQL fixtures, including protected database cold reopens.
//! Production KnownFolder/setup/lease admission is NOT bypassable outside cfg(test).
use super::*;
use crate::acl::object_grants::{PreparedReadTree, ReadTreeJournal};
use rusqlite::Connection;
use std::{os::windows::io::AsRawHandle, path::PathBuf};

#[path = "account_bootstrap_crash_tests.rs"]
mod atomic_crash;

#[path = "account_bootstrap_start_tests.rs"]
mod atomic_start;

#[path = "account_bootstrap_logon_native_tests.rs"]
mod atomic_logon;

#[path = "account_bootstrap_credential_native_tests.rs"]
mod atomic_credentials;

#[path = "account_bootstrap_account_birth_native_tests.rs"]
mod atomic_account_birth;

#[path = "account_paired_native_tests.rs"]
mod paired;

struct Fixture {
    root: PathBuf,
    setup: SetupRecord,
    preserve: bool,
}
impl Fixture {
    fn new() -> Self {
        let root = crate::util::test_temp_dir()
            .join(format!("alysis-execution-test-{}", random_id().unwrap()));
        std::fs::create_dir(&root).unwrap();
        let mut setup = crate::account_install::synthetic_ready();
        setup.owner_sid = crate::sid::current_user_sid().unwrap();
        setup.storage = Some(super::super::storage::synthetic_attestation(&setup));
        let acl = crate::acl::build_allow_dacl(&[
            crate::acl::Allow(
                &setup.owner_sid,
                crate::acl::Mask::FILE_ALL,
                crate::acl::OICI,
            ),
            crate::acl::Allow("S-1-5-18", crate::acl::Mask::FILE_ALL, crate::acl::OICI),
        ])
        .unwrap();
        crate::acl::write_file_dacl(
            root.to_str().unwrap(),
            acl.as_ptr(),
            crate::acl::Protection::Protected,
        )
        .unwrap();
        Self {
            root,
            setup,
            preserve: false,
        }
    }
    fn path(&self) -> PathBuf {
        self.root.join("execution-pending.json")
    }
    fn pending(&self, create: bool) -> Result<PendingFile> {
        let policy = policy(&self.setup)?;
        let pins = fs::pin_fixture_path(&self.root, &policy)?;
        PendingFile::open(&self.path(), policy, pins, create)
    }
    fn identity(&self) -> CandidateJournalIdentity {
        CandidateJournalIdentity::new(
            self.setup.binding(AccountMode::Offline).unwrap(),
            format!("Alysis.Execution.{}", random_id().unwrap()),
        )
        .unwrap()
    }
    fn active(&self) -> ActiveExecution<'static> {
        let pending = self.pending(true).unwrap();
        let mut record = fixture_record(self.identity());
        record.pending_id = pending.file.id.as_bytes();
        pending.write_new(&record).unwrap();
        ActiveExecution {
            pending,
            record,
            _lease: None,
        }
    }
    fn reopen_stopped(
        &self,
        binding: &AccountBinding,
        database_id: [u8; 24],
    ) -> Result<StoppedExecution<'static>> {
        let pending = self.pending(false)?;
        let record = pending.read()?;
        record.validate(binding, database_id)?;
        broker_stopped(record.pid, record.created)?;
        crate::job::wait_for_execution_exit(&record.identity.container)?;
        Ok(StoppedExecution {
            pending,
            record,
            _lease: None,
        })
    }
    fn connection(&self, identity: &CandidateJournalIdentity) -> Connection {
        let mut connection = Connection::open(self.root.join("test.db")).unwrap();
        connection
            .execute_batch("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL;")
            .unwrap();
        crate::account_state::initialize_header(&mut connection, &identity.binding).unwrap();
        connection
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        if !self.preserve {
            std::fs::remove_dir_all(&self.root).unwrap();
        }
    }
}

#[test]
fn candidate_execution_record_binds_scope_broker_transaction_and_file_identities() {
    let fixture = Fixture::new();
    let record = fixture_record(fixture.identity());
    record
        .validate(&record.identity.binding, record.database_id)
        .unwrap();
    let mutations: Vec<Box<dyn Fn(&mut ExecutionRecord)>> = vec![
        Box::new(|r| r.version = 2),
        Box::new(|r| r.scope = "launch".into()),
        Box::new(|r| r.owner = "S-1-5-18".into()),
        Box::new(|r| r.pid = 0),
        Box::new(|r| r.created = 0),
        Box::new(|r| r.job.push('x')),
        Box::new(|r| r.transaction = "../other".into()),
        Box::new(|r| r.database_id[0] ^= 1),
        Box::new(|r| r.pending_id = [0; 24]),
        Box::new(|r| r.identity.package_sid.push_str("-1")),
    ];
    for mutate in mutations {
        let mut changed = record.clone();
        mutate(&mut changed);
        assert!(
            changed
                .validate(&record.identity.binding, record.database_id)
                .is_err()
        );
    }
    let mut value = serde_json::to_value(&record).unwrap();
    value["unexpected"] = true.into();
    assert!(serde_json::from_value::<ExecutionRecord>(value).is_err());
}

#[test]
fn candidate_execution_private_marker_is_immutable_pinned_and_preserved_on_drop() {
    let fixture = Fixture::new();
    let active = fixture.active();
    active.record().unwrap(); // same-handle write readback starts at offset zero
    assert!(std::fs::write(fixture.path(), b"replacement").is_err());
    assert!(std::fs::rename(fixture.path(), fixture.root.join("moved")).is_err());
    assert!(std::fs::remove_file(fixture.path()).is_err());
    assert!(std::fs::rename(&fixture.root, fixture.root.with_extension("moved")).is_err());
    assert!(fixture.pending(true).is_err());
    let expected = active.record.clone();
    drop(active);
    assert!(fixture.path().exists());
    let pending = fixture.pending(false).unwrap();
    assert_eq!(pending.read().unwrap(), expected);
    assert!(
        fixture
            .reopen_stopped(&expected.identity.binding, expected.database_id)
            .is_err()
    );
}

#[test]
fn candidate_execution_partial_write_and_replaced_file_are_preserved() {
    let fixture = Fixture::new();
    let pending = fixture.pending(true).unwrap();
    pending.file.write_new(b"{\"version\":3").unwrap();
    assert!(
        pending
            .read()
            .unwrap_err()
            .to_string()
            .contains("malformed/partial")
    );
    drop(pending);
    let before = std::fs::read(fixture.path()).unwrap();
    assert!(fixture.pending(false).unwrap().read().is_err());
    assert_eq!(std::fs::read(fixture.path()).unwrap(), before);
    std::fs::remove_file(fixture.path()).unwrap();

    let active = fixture.active();
    let old = active.record.clone();
    drop(active);
    std::fs::remove_file(fixture.path()).unwrap();
    let replacement = fixture.pending(true).unwrap();
    assert_ne!(replacement.file.id.as_bytes(), old.pending_id);
    replacement
        .file
        .write_new(&serde_json::to_vec(&old).unwrap())
        .unwrap();
    assert!(
        replacement
            .read()
            .unwrap_err()
            .to_string()
            .contains("replaced")
    );
}

#[test]
fn candidate_execution_broker_death_and_database_identity_are_required() {
    let fixture = Fixture::new();
    let pending = fixture.pending(true).unwrap();
    let mut child = std::process::Command::new("cmd.exe")
        .args(["/d", "/c", "exit 0"])
        .spawn()
        .unwrap();
    let mut record = fixture_record(fixture.identity());
    record.pid = child.id();
    record.created = process_created(HANDLE(child.as_raw_handle())).unwrap();
    record.pending_id = pending.file.id.as_bytes();
    pending.write_new(&record).unwrap();
    child.wait().unwrap();
    broker_stopped(record.pid, record.created).unwrap();
    drop(child);
    broker_stopped(record.pid, record.created).unwrap();
    assert!(absent_process(std::process::id()).is_err());
    assert!(
        broker_stopped(
            std::process::id(),
            process_created(unsafe { GetCurrentProcess() }).unwrap()
        )
        .is_err()
    );
    // A reused PID with a different creation timestamp cannot be the old broker.
    broker_stopped(std::process::id(), 1).unwrap();
    drop(pending);
    assert!(
        fixture
            .reopen_stopped(&record.identity.binding, [9; 24])
            .is_err()
    );
    let stopped = fixture
        .reopen_stopped(&record.identity.binding, record.database_id)
        .unwrap();
    assert_eq!(stopped.record().unwrap(), &record);
    drop(stopped);
    assert!(fixture.path().exists());
}

#[test]
fn candidate_execution_process_enumeration_refuses_truncation_and_malformed_counts() {
    let ids = [4, 8, 12, 0];
    assert_eq!(complete_process_list(&ids, 12).unwrap(), Some(&ids[..3]));
    assert_eq!(complete_process_list(&ids, 16).unwrap(), None);
    for count in [0, 1, 6, 17, 20] {
        assert!(complete_process_list(&ids, count).is_err());
    }
}

#[test]
fn candidate_execution_grant_stop_recover_receipt_lifecycle() {
    let fixture = Fixture::new();
    let tree_path = fixture.root.join("tree");
    std::fs::create_dir(&tree_path).unwrap();
    std::fs::write(tree_path.join("child"), b"original").unwrap();
    let active = fixture.active();
    let connection = fixture.connection(active.identity());
    let tree = PreparedReadTree::prepare(tree_path.to_str().unwrap(), active.identity()).unwrap();
    let journal = ReadTreeJournal::fixture_active(&connection, &active).unwrap();
    assert!(
        journal
            .recover()
            .unwrap_err()
            .to_string()
            .contains("live execution")
    );
    tree.grant(&journal).unwrap();
    let stored: String = connection
        .query_row("SELECT identity_json FROM candidate_read_runs", [], |r| {
            r.get(0)
        })
        .unwrap();
    assert_eq!(
        serde_json::from_str::<ExecutionRecord>(&stored).unwrap(),
        active.record
    );
    drop(tree);
    drop(journal);
    let stopped = active.stop().unwrap();
    let journal = ReadTreeJournal::fixture_stopped(&connection, &stopped).unwrap();
    let tree = PreparedReadTree::prepare(tree_path.to_str().unwrap(), &stopped.record.identity)
        .err()
        .unwrap();
    // Existing execution ACEs cannot be silently adopted for a second grant.
    assert!(format!("{tree:#}").contains("package"));
    let receipt = journal.recover().unwrap();
    assert!(fixture.path().exists());
    drop(journal);
    stopped.finish(receipt).unwrap();
    assert!(!fixture.path().exists());
    assert_eq!(std::fs::read(tree_path.join("child")).unwrap(), b"original");
}

#[test]
fn candidate_execution_pregrant_and_postcleanup_interruptions_are_idempotent() {
    let fixture = Fixture::new();
    let active = fixture.active();
    let connection = fixture.connection(active.identity());
    let stopped = active.stop().unwrap();
    // Intent exists but the grant transaction never committed: no ACL was
    // authorized. A same-database empty journal is therefore recoverable.
    let journal = ReadTreeJournal::fixture_stopped(&connection, &stopped).unwrap();
    let receipt = journal.recover().unwrap();
    drop(receipt); // interruption after row retirement and before marker deletion
    let receipt = journal.recover().unwrap();
    drop(journal);
    stopped.finish(receipt).unwrap();
    assert!(!fixture.path().exists());
}

#[test]
fn candidate_execution_resource_scope_cannot_retire_with_only_acl_receipt() {
    let fixture = Fixture::new();
    let pending = fixture.pending(true).unwrap();
    let mut record = fixture_record(fixture.identity());
    record.scope = RESOURCE_SCOPE.into();
    record.pending_id = pending.file.id.as_bytes();
    pending.write_new(&record).unwrap();
    let active = ActiveExecution {
        pending,
        record,
        _lease: None,
    };
    let connection = fixture.connection(active.identity());
    let stopped = active.stop().unwrap();
    assert!(stopped.acl_recovery_record(None).is_err());
    // The fixture constructor deliberately bypasses production admission so
    // marker retirement is independently tested even with a forged test-only
    // ACL receipt. The production opener already refuses this scope above.
    let journal = ReadTreeJournal::fixture_stopped(&connection, &stopped).unwrap();
    let receipt = journal.recover().unwrap();
    drop(journal);
    let error = stopped.finish(receipt).unwrap_err();
    assert!(error.to_string().contains("resource recovery receipt"));
    assert!(fixture.path().exists());
}

#[test]
fn candidate_execution_sql_mismatch_and_durability_drift_preserve_pending() {
    let fixture = Fixture::new();
    let active = fixture.active();
    let connection = fixture.connection(active.identity());
    let stopped = active.stop().unwrap();
    let journal = ReadTreeJournal::fixture_stopped(&connection, &stopped).unwrap();
    let mut other = stopped.record.clone();
    other.transaction = random_id().unwrap();
    connection
        .execute(
            "INSERT INTO candidate_read_runs VALUES(?1,?2)",
            rusqlite::params![
                other.identity.container,
                serde_json::to_string(&other).unwrap()
            ],
        )
        .unwrap();
    assert!(
        journal
            .recover()
            .unwrap_err()
            .to_string()
            .contains("identity mismatch")
    );
    assert!(fixture.path().exists());
    connection.execute_batch("PRAGMA synchronous=OFF;").unwrap();
    assert!(
        journal
            .recover()
            .unwrap_err()
            .to_string()
            .contains("DELETE/FULL")
    );
    assert!(fixture.path().exists());
}

#[test]
fn candidate_execution_atomic_scope_refuses_legacy_grants_and_retirement() {
    let fixture = Fixture::new();
    let pending = fixture.pending(true).unwrap();
    let mut record = fixture_record(fixture.identity());
    record.scope = BOOTSTRAP_PLAN_SCOPE.into();
    record.pending_id = pending.file.id.as_bytes();
    pending.write_new(&record).unwrap();
    let before = std::fs::read(fixture.path()).unwrap();
    let active = ActiveExecution {
        pending,
        record,
        _lease: None,
    };
    let connection = fixture.connection(active.identity());
    assert!(ReadTreeJournal::fixture_active(&connection, &active).is_err());
    let stopped = active.stop().unwrap();
    assert!(stopped.acl_recovery_record(None).is_err());
    let receipt = RecoveredReadTree::new(stopped.record.clone());
    assert!(stopped.finish(receipt).is_err());
    assert_eq!(std::fs::read(fixture.path()).unwrap(), before);
    let pending = fixture.pending(false).unwrap();
    pending.read().unwrap();
}

// Exercise the protected opener and real typed journal across process exits,
// not a fixture Connection or production setup/account/runner activation.
#[derive(serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct DiskCrashControl {
    setup: SetupRecord,
    checkpoint: String,
    nonce: String,
}

const DISK_CRASH_ENV: &str = "ALYSIS_TEST_CANDIDATE_DISK_CRASH_ROOT";
const DISK_CRASH_NONCE_ENV: &str = "ALYSIS_TEST_CANDIDATE_DISK_CRASH_NONCE";
const DISK_CRASH_TEST: &str =
    "account_state::execution::tests::candidate_execution_protected_disk_crash_and_cold_recovery";

fn protected_database(fixture: &Fixture) -> PrivateDatabase<'static> {
    PrivateDatabase::open_under(&fixture.root, &fixture.setup, AccountMode::Offline, None).unwrap()
}

fn protected_fixture() -> Fixture {
    let mut fixture = Fixture::new();
    // Preserve any failed/ambiguous synthetic state for inspection. Only the
    // successful recovery cases opt into the usual fixture teardown.
    fixture.preserve = true;
    fixture.setup.storage = None;
    let plan =
        super::super::storage::PreparedStorage::prepare_fixture(&fixture.setup, &fixture.root)
            .unwrap();
    fixture.setup.storage = Some(plan.intent().clone());
    let pins = plan.create().unwrap();
    fixture.setup.storage = Some(pins.attestation().clone());
    fixture
}

fn atomic_fixture_active(
    fixture: &Fixture,
    database: &PrivateDatabase<'_>,
) -> ActiveExecution<'static> {
    let pending = fixture.pending(true).unwrap();
    let mut record = fixture_record(fixture.identity());
    record.scope = BOOTSTRAP_PLAN_SCOPE.into();
    record.database_id = database.file_identity();
    record.pending_id = pending.file.id.as_bytes();
    pending.write_new(&record).unwrap();
    ActiveExecution {
        pending,
        record,
        _lease: None,
    }
}

fn atomic_event_count(database: &PrivateDatabase<'_>) -> u64 {
    database
        .connection()
        .unwrap()
        .query_row(
            "SELECT count(*) FROM candidate_atomic_bootstrap_events",
            [],
            |r| r.get(0),
        )
        .unwrap()
}

fn atomic_job_counts(record: &ExecutionRecord) -> Option<(u32, u32)> {
    use windows::Win32::System::{JobObjects::*, SystemServices::JOB_OBJECT_QUERY};
    let handle =
        match unsafe { OpenJobObjectW(JOB_OBJECT_QUERY, false, pcwstr(&wstr(&record.job))) } {
            Ok(value) => OwnedHandle(value),
            Err(error) => {
                assert_eq!(
                    error.code(),
                    windows::Win32::Foundation::ERROR_FILE_NOT_FOUND.to_hresult()
                );
                return None;
            }
        };
    let mut info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
    unsafe {
        QueryInformationJobObject(
            Some(handle.raw()),
            JobObjectBasicAccountingInformation,
            (&mut info as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
            size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
            None,
        )
        .unwrap();
    }
    Some((info.ActiveProcesses, info.TotalProcesses))
}

#[test]
fn candidate_execution_atomic_native_birth_is_write_ahead_single_use_and_nonresumable() {
    use super::super::image::PinnedRunnerImage;
    use crate::account_control::ControlServer;
    // Every row set and marker is deliberately retained as small test evidence;
    // there is no lifecycle completion/retirement authority to clear them yet.
    for cut in [
        "normal",
        "job-intent",
        "job-collision",
        "job-receipt",
        "bootstrap-intent",
        "bootstrap-receipt",
    ] {
        let fixture = protected_fixture();
        let database = protected_database(&fixture);
        let ready = ReadyInstallation::fixture(fixture.setup.clone(), &fixture.root).unwrap();
        let active = atomic_fixture_active(&fixture, &database);
        let image = PinnedRunnerImage::fixture_installed(&ready).unwrap();
        let (_server, address) =
            ControlServer::prepare(&ready.binding(AccountMode::Offline).unwrap().account_sid)
                .unwrap();
        let plan =
            bootstrap_plan::CommittedPlan::prepare(&database, &active, &image, &ready, &address)
                .unwrap();
        assert_eq!(atomic_event_count(&database), 0);
        let pending_before = std::fs::read(fixture.path()).unwrap();
        let connection = database.connection().unwrap();
        if cut == "job-intent" {
            connection.execute_batch("PRAGMA query_only=ON").unwrap();
            assert!(plan.create_job().is_err());
            connection.execute_batch("PRAGMA query_only=OFF").unwrap();
            assert!(plan.create_job().is_err());
            assert_eq!(atomic_event_count(&database), 0);
            assert_eq!(atomic_job_counts(active.record().unwrap()), None);
        } else if cut == "job-collision" {
            let collision = crate::job::Job::for_execution(&active.identity().container).unwrap();
            assert!(plan.create_job().is_err());
            assert_eq!(atomic_event_count(&database), 1);
            assert_eq!(atomic_job_counts(active.record().unwrap()), Some((0, 0)));
            assert!(plan.create_job().is_err());
            drop(collision);
        } else {
            let job = plan.create_job().unwrap();
            assert_eq!(atomic_event_count(&database), 1);
            assert_eq!(atomic_job_counts(active.record().unwrap()), Some((0, 0)));
            if cut == "job-receipt" {
                connection.execute_batch("PRAGMA query_only=ON").unwrap();
                assert!(job.record_birth().is_err());
                connection.execute_batch("PRAGMA query_only=OFF").unwrap();
                assert_eq!(atomic_event_count(&database), 1);
                assert_eq!(atomic_job_counts(active.record().unwrap()), Some((0, 0)));
            }
            job.record_birth().unwrap();
            job.record_birth().unwrap(); // Exact same original object, no new birth.
            assert_eq!(atomic_event_count(&database), 2);
            assert!(plan.create_job().is_err());
            if cut == "bootstrap-intent" {
                connection.execute_batch("PRAGMA query_only=ON").unwrap();
                assert!(job.prepare_bootstrap().is_err());
                connection.execute_batch("PRAGMA query_only=OFF").unwrap();
                assert!(job.prepare_bootstrap().is_err());
                assert_eq!(atomic_event_count(&database), 2);
                assert_eq!(atomic_job_counts(active.record().unwrap()), Some((0, 0)));
            } else {
                let child = job.prepare_bootstrap().unwrap();
                assert_eq!(atomic_event_count(&database), 3);
                assert_eq!(atomic_job_counts(active.record().unwrap()), Some((1, 1)));
                if cut == "bootstrap-receipt" {
                    connection.execute_batch("PRAGMA query_only=ON").unwrap();
                    assert!(child.record_birth().is_err());
                    connection.execute_batch("PRAGMA query_only=OFF").unwrap();
                    assert_eq!(atomic_event_count(&database), 3);
                    assert_eq!(atomic_job_counts(active.record().unwrap()), Some((1, 1)));
                }
                child.record_birth().unwrap();
                child.record_birth().unwrap();
                assert_eq!(atomic_event_count(&database), 4);
                assert!(job.prepare_bootstrap().is_err());
                // The helper CLI was never resumed; original guarded-process
                // Drop proves exit before allowing the job/image pins to drop.
                drop(child);
                // Process signaling can precede the job's accounting update.
                // Require the same exact zero-active/one-total result within a
                // bound; never infer job emptiness from the child signal alone.
                let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
                loop {
                    let counts = atomic_job_counts(active.record().unwrap());
                    if counts == Some((0, 1)) {
                        break;
                    }
                    assert_eq!(counts, Some((1, 1)), "unexpected original job accounting");
                    assert!(
                        std::time::Instant::now() < deadline,
                        "original job accounting did not drain"
                    );
                    std::thread::sleep(std::time::Duration::from_millis(10));
                }
            }
            drop(job);
            assert_eq!(atomic_job_counts(active.record().unwrap()), None);
        }
        plan.verify().unwrap();
        assert_eq!(std::fs::read(fixture.path()).unwrap(), pending_before);
        assert!(
            bootstrap_plan::CommittedPlan::prepare(&database, &active, &image, &ready, &address)
                .is_err()
        );
        eprintln!(
            "atomic lifecycle {cut}: preserved {}",
            fixture.root.display()
        );
    }
}

fn disk_rows(database: &PrivateDatabase<'_>) -> (i64, i64) {
    database
        .connection()
        .unwrap()
        .query_row(
            "SELECT (SELECT count(*) FROM candidate_read_runs),
                    (SELECT count(*) FROM candidate_read_objects)",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap()
}

fn acl_observation(path: &std::path::Path) -> (u16, u32, Vec<Vec<u8>>) {
    let (sd, dacl) = crate::acl::read_file_dacl(path.to_str().unwrap()).unwrap();
    assert!(!dacl.is_null());
    let mut control = 0;
    let mut revision = 0;
    unsafe {
        windows::Win32::Security::GetSecurityDescriptorControl(sd.ptr, &mut control, &mut revision)
            .unwrap();
    }
    let mut aces = Vec::new();
    let kept = crate::acl::filter_aces(dacl, |_, bytes| {
        aces.push(bytes.to_vec());
        true
    })
    .unwrap();
    (control & 0x150c, kept.2.0, aces)
}

fn exit_at_disk_checkpoint(fixture: &Fixture, control: &DiskCrashControl) -> ! {
    std::fs::write(
        fixture.root.join("child-checkpoint.json"),
        serde_json::to_vec(&serde_json::json!({
            "checkpoint": control.checkpoint,
            "nonce": control.nonce,
            "pid": std::process::id(),
        }))
        .unwrap(),
    )
    .unwrap();
    // Intentionally bypass RAII; every selected checkpoint is an autocommit
    // boundary. The test makes no claim that SQLite hot journals are recovered.
    std::process::exit(87)
}

fn run_disk_crash_child(root: PathBuf) -> ! {
    assert_eq!(root.parent().unwrap(), crate::util::test_temp_dir());
    assert!(
        root.file_name()
            .unwrap()
            .to_str()
            .unwrap()
            .starts_with("alysis-execution-test-")
    );
    let args: Vec<_> = std::env::args().skip(1).collect();
    assert_eq!(
        args,
        [
            DISK_CRASH_TEST,
            "--exact",
            "--nocapture",
            "--test-threads=1"
        ]
    );
    let bytes = std::fs::read(root.join("crash-control.json")).unwrap();
    assert!(bytes.len() < 65536);
    let control: DiskCrashControl = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(std::env::var(DISK_CRASH_NONCE_ENV).unwrap(), control.nonce);
    validate_random_id(&control.nonce).unwrap();
    assert!(matches!(
        control.checkpoint.as_str(),
        "marker" | "grant" | "revoke-before-retire" | "rows-retired"
    ));
    let fixture = Fixture {
        root,
        setup: control.setup.clone(),
        preserve: true,
    };
    let database = protected_database(&fixture);
    let connection = database.connection().unwrap();
    assert_eq!(
        connection
            .query_row("PRAGMA journal_mode", [], |row| row.get::<_, String>(0))
            .unwrap(),
        "delete"
    );
    assert_eq!(
        connection
            .query_row("PRAGMA synchronous", [], |row| row.get::<_, i64>(0))
            .unwrap(),
        2
    );
    let pending = fixture.pending(true).unwrap();
    let mut record = fixture_record(fixture.identity());
    record.database_id = database.file_identity();
    record.pending_id = pending.file.id.as_bytes();
    pending.write_new(&record).unwrap();
    let active = ActiveExecution {
        pending,
        record,
        _lease: None,
    };
    if control.checkpoint == "marker" {
        exit_at_disk_checkpoint(&fixture, &control);
    }
    let tree = PreparedReadTree::prepare(
        fixture.root.join("tree").to_str().unwrap(),
        active.identity(),
    )
    .unwrap();
    assert_eq!(tree.object_count(), 2);
    let journal = ReadTreeJournal::open(&database, &active).unwrap();
    tree.grant(&journal).unwrap();
    assert_eq!(disk_rows(&database), (1, 2));
    drop(journal);
    drop(tree);
    if control.checkpoint == "grant" {
        exit_at_disk_checkpoint(&fixture, &control);
    }
    let stopped = active.stop().unwrap();
    if control.checkpoint == "revoke-before-retire" {
        // Fail the first durable row retirement after its object's DACL was
        // restored. No production fault hook or arbitrary callback is added.
        connection
            .execute_batch(
                "CREATE TRIGGER fixture_recovery_cut BEFORE DELETE ON candidate_read_objects
                 BEGIN SELECT RAISE(ABORT, 'fixture recovery cut'); END;",
            )
            .unwrap();
        let journal = ReadTreeJournal::open_stopped(&database, &stopped).unwrap();
        assert!(
            journal
                .recover()
                .unwrap_err()
                .to_string()
                .contains("fixture recovery cut")
        );
        assert_eq!(disk_rows(&database), (1, 2));
    } else {
        let journal = ReadTreeJournal::open_stopped(&database, &stopped).unwrap();
        drop(journal.recover().unwrap());
        assert_eq!(disk_rows(&database), (0, 0));
    }
    assert!(fixture.path().exists());
    exit_at_disk_checkpoint(&fixture, &control);
}

fn spawn_disk_checkpoint(fixture: &Fixture, checkpoint: &str) -> (u32, u64) {
    let nonce = random_id().unwrap();
    std::fs::write(
        fixture.root.join("crash-control.json"),
        serde_json::to_vec(&DiskCrashControl {
            setup: fixture.setup.clone(),
            checkpoint: checkpoint.into(),
            nonce: nonce.clone(),
        })
        .unwrap(),
    )
    .unwrap();
    let mut child = std::process::Command::new(std::env::current_exe().unwrap())
        .args([
            DISK_CRASH_TEST,
            "--exact",
            "--nocapture",
            "--test-threads=1",
        ])
        // Scope child selection to this exact command, never mutate the test
        // process environment or inherit a stale nonce/fixture selection.
        .env_remove(DISK_CRASH_ENV)
        .env_remove(DISK_CRASH_NONCE_ENV)
        .env(DISK_CRASH_ENV, &fixture.root)
        .env(DISK_CRASH_NONCE_ENV, &nonce)
        .env_remove("RUST_TEST_THREADS")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let pid = child.id();
    let created = process_created(HANDLE(child.as_raw_handle())).unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
    while child.try_wait().unwrap().is_none() {
        if std::time::Instant::now() >= deadline {
            child.kill().unwrap(); // Only this exact spawned fixture process.
            let _ = child.wait();
            panic!("isolated disk checkpoint child timed out; fixture retained");
        }
        std::thread::sleep(std::time::Duration::from_millis(25));
    }
    let output = child.wait_with_output().unwrap();
    assert_eq!(
        output.status.code(),
        Some(87),
        "selected child did not reach checkpoint: stdout={} stderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let proof: serde_json::Value =
        serde_json::from_slice(&std::fs::read(fixture.root.join("child-checkpoint.json")).unwrap())
            .unwrap();
    assert_eq!(
        proof,
        serde_json::json!({"checkpoint": checkpoint, "nonce": nonce, "pid": pid})
    );
    broker_stopped(pid, created).unwrap();
    (pid, created)
}

#[test]
fn candidate_execution_protected_disk_crash_and_cold_recovery() {
    if let Some(root) = std::env::var_os(DISK_CRASH_ENV) {
        run_disk_crash_child(PathBuf::from(root));
    }
    assert!(std::env::var_os(DISK_CRASH_NONCE_ENV).is_none());
    for checkpoint in [
        "marker",
        "grant",
        "revoke-before-retire",
        "rows-retired",
        "auxiliary",
    ] {
        let mut fixture = protected_fixture();
        let tree = fixture.root.join("tree");
        std::fs::create_dir(&tree).unwrap();
        let leaf = tree.join("child");
        std::fs::write(&leaf, b"unchanged synthetic content").unwrap();
        let before = [acl_observation(&tree), acl_observation(&leaf)];
        let child_checkpoint = if checkpoint == "auxiliary" {
            "grant"
        } else {
            checkpoint
        };
        let (pid, created) = spawn_disk_checkpoint(&fixture, child_checkpoint);
        let marker_bytes = std::fs::read(fixture.path()).unwrap();
        let pending = fixture.pending(false).unwrap();
        let record = pending.read().unwrap();
        assert_eq!((record.pid, record.created), (pid, created));
        drop(pending);
        let database_path = super::super::database_path(
            &fixture.root,
            &fixture.setup.binding(AccountMode::Offline).unwrap(),
        )
        .unwrap();
        assert!(!database_path.with_extension("db-journal").exists());

        if checkpoint == "auxiliary" {
            // A suspected hot/unknown sidecar must be refused BEFORE SQLite
            // admission. It and every bound artifact remain for explicit repair.
            let database_bytes = std::fs::read(&database_path).unwrap();
            let manifest_path = database_path.with_file_name("identity.json");
            let manifest_bytes = std::fs::read(&manifest_path).unwrap();
            let granted = [acl_observation(&tree), acl_observation(&leaf)];
            assert_ne!(granted[0], before[0]);
            assert_ne!(granted[1], before[1]);
            let sidecar = database_path.with_extension("db-journal");
            std::fs::write(&sidecar, b"ambiguous synthetic sidecar").unwrap();
            let error = PrivateDatabase::open_under(
                &fixture.root,
                &fixture.setup,
                AccountMode::Offline,
                None,
            )
            .err()
            .unwrap();
            assert!(error.to_string().contains("auxiliary or unknown"));
            assert_eq!(
                std::fs::read(&sidecar).unwrap(),
                b"ambiguous synthetic sidecar"
            );
            assert_eq!(std::fs::read(&database_path).unwrap(), database_bytes);
            assert_eq!(std::fs::read(&manifest_path).unwrap(), manifest_bytes);
            assert_eq!(std::fs::read(fixture.path()).unwrap(), marker_bytes);
            assert_eq!([acl_observation(&tree), acl_observation(&leaf)], granted);
            assert_eq!(
                std::fs::read(&leaf).unwrap(),
                b"unchanged synthetic content"
            );
            eprintln!(
                "preserved isolated ambiguous disk fixture: {}",
                fixture.root.display()
            );
            continue;
        }

        // Every child and its handles are gone: cold-open the real protected
        // database and independently re-admit the exact immutable marker.
        let database = protected_database(&fixture);
        assert_eq!(record.database_id, database.file_identity());
        let stopped = fixture
            .reopen_stopped(&record.identity.binding, database.file_identity())
            .unwrap();
        assert_eq!(stopped.record().unwrap(), &record);
        let journal = ReadTreeJournal::open_stopped(&database, &stopped).unwrap();
        let expected_rows = if matches!(checkpoint, "grant" | "revoke-before-retire") {
            (1, 2)
        } else {
            (0, 0)
        };
        assert_eq!(disk_rows(&database), expected_rows);
        if expected_rows.0 == 1 {
            let stored: String = database
                .connection()
                .unwrap()
                .query_row("SELECT identity_json FROM candidate_read_runs", [], |row| {
                    row.get(0)
                })
                .unwrap();
            assert_eq!(
                serde_json::from_str::<ExecutionRecord>(&stored).unwrap(),
                record
            );
        }
        if checkpoint == "grant" {
            assert_ne!(acl_observation(&tree), before[0]);
            assert_ne!(acl_observation(&leaf), before[1]);
        } else if checkpoint == "revoke-before-retire" {
            assert_ne!(acl_observation(&tree), before[0]);
            assert_eq!(acl_observation(&leaf), before[1]);
            assert!(journal.recover().is_err());
            assert_eq!(disk_rows(&database), (1, 2));
            assert_eq!(std::fs::read(fixture.path()).unwrap(), marker_bytes);
            database
                .connection()
                .unwrap()
                .execute_batch("DROP TRIGGER fixture_recovery_cut")
                .unwrap();
        } else {
            assert_eq!([acl_observation(&tree), acl_observation(&leaf)], before);
        }
        drop(journal.recover().unwrap()); // Interrupt before consuming receipt.
        assert_eq!(disk_rows(&database), (0, 0));
        assert_eq!([acl_observation(&tree), acl_observation(&leaf)], before);
        assert_eq!(std::fs::read(fixture.path()).unwrap(), marker_bytes);
        drop(journal);
        drop(stopped);
        drop(database);

        let database = protected_database(&fixture);
        assert_eq!(record.database_id, database.file_identity());
        let stopped = fixture
            .reopen_stopped(&record.identity.binding, database.file_identity())
            .unwrap();
        let journal = ReadTreeJournal::open_stopped(&database, &stopped).unwrap();
        assert_eq!(disk_rows(&database), (0, 0));
        let receipt = journal.recover().unwrap();
        drop(journal);
        stopped.finish(receipt).unwrap();
        assert!(!fixture.path().exists());
        assert_eq!(
            std::fs::read(&leaf).unwrap(),
            b"unchanged synthetic content"
        );
        assert_eq!([acl_observation(&tree), acl_observation(&leaf)], before);
        drop(database);
        fixture.preserve = false;
    }
}
