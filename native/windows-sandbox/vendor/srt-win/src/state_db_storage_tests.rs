//! File-backed synthetic ledgers only. No ACL, account or installed state writes.
use super::*;
use std::collections::BTreeMap;
use std::os::windows::process::CommandExt;
use std::process::{Command, Stdio};

struct Fixture {
    root: PathBuf,
    base: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let base = crate::util::test_temp_dir().canonicalize().unwrap();
        let root = base.join(format!(
            "alysis-ledger-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir(&root).unwrap();
        Self { root, base }
    }
    fn path(&self) -> PathBuf {
        self.root.join("state.db")
    }
    fn cleanup(self) {
        let checked = self.root.canonicalize().unwrap();
        assert!(checked.starts_with(&self.base) && checked != self.base);
        std::fs::remove_dir_all(checked).unwrap();
    }
    fn snapshot(&self) -> BTreeMap<String, Vec<u8>> {
        std::fs::read_dir(&self.root)
            .unwrap()
            .map(|entry| {
                let entry = entry.unwrap();
                (
                    entry.file_name().into_string().unwrap(),
                    std::fs::read(entry.path()).unwrap(),
                )
            })
            .collect()
    }
}
fn rw(path: &Path) -> Result<Connection> {
    open_read_write(path, SESSION_SCHEMA_SQL, SESSION_SCHEMA_VERSION)
}
fn ro(path: &Path) -> Result<Option<Connection>> {
    open_read_only(path, SESSION_SCHEMA_SQL, SESSION_SCHEMA_VERSION)
}
fn assert_durable(connection: &Connection) {
    let mode: String = connection
        .pragma_query_value(None, "journal_mode", |r| r.get(0))
        .unwrap();
    let sync: i64 = connection
        .pragma_query_value(None, "synchronous", |r| r.get(0))
        .unwrap();
    let fk: i64 = connection
        .pragma_query_value(None, "foreign_keys", |r| r.get(0))
        .unwrap();
    assert_eq!((mode.as_str(), sync, fk), ("wal", 2, 1));
}
fn insert_pending(connection: &Connection) {
    connection.execute("INSERT INTO working_aces(canonical_path,kind,file_id,mask) VALUES(?1,'grant',?2,'modify')",
        params![r"C:\synthetic-untouched-ledger-target", vec![7u8; 24]]).unwrap();
}
fn assert_pending(connection: &Connection) {
    let row: (String, Vec<u8>, String) = connection
        .query_row(
            "SELECT canonical_path,file_id,mask FROM working_aces WHERE kind='grant'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    assert_eq!(
        row,
        (
            r"C:\synthetic-untouched-ledger-target".into(),
            vec![7u8; 24],
            "modify".into()
        )
    );
}
fn raw_create(path: &Path, schema: &str, version: i64) {
    let db = Connection::open_with_flags_and_vfs(
        path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE | rusqlite::OpenFlags::SQLITE_OPEN_CREATE,
        "win32-longpath",
    )
    .unwrap();
    db.execute_batch(schema).unwrap();
    db.pragma_update(None, "user_version", version).unwrap();
}

#[test]
fn baseline_storage_fresh_wal_full_and_cold_reopen_preserve_pending_rows() {
    let fixture = Fixture::new();
    let path = fixture.path();
    assert!(ro(&path).unwrap().is_none());
    {
        let connection = rw(&path).unwrap();
        assert_durable(&connection);
        insert_pending(&connection);
    }
    assert!(!sidecar(&path, "-wal").try_exists().unwrap());
    assert!(!sidecar(&path, "-shm").try_exists().unwrap());
    {
        let readonly = ro(&path).unwrap().unwrap();
        assert_pending(&readonly);
    }
    {
        let reopened = rw(&path).unwrap();
        assert_durable(&reopened);
        assert_pending(&reopened);
    }
    fixture.cleanup();
}

#[test]
fn baseline_storage_refuses_unknown_history_without_backup_repair_or_scrub() {
    for defect in [
        "corrupt",
        "empty",
        "version_old",
        "version_zero",
        "missing_table",
        "conflict_ignore",
        "trigger",
        "oversized_sql",
        "excess_objects",
    ] {
        let fixture = Fixture::new();
        let path = fixture.path();
        match defect {
            "corrupt" => std::fs::write(&path, b"not a SQLite ledger; preserve all bytes").unwrap(),
            "empty" => std::fs::write(&path, b"").unwrap(),
            "version_old" | "version_zero" => {
                let version = if defect == "version_old" {
                    SESSION_SCHEMA_VERSION - 1
                } else {
                    0
                };
                raw_create(&path, SESSION_SCHEMA_SQL, version);
            }
            "missing_table" => {
                raw_create(&path, SESSION_SCHEMA_SQL, SESSION_SCHEMA_VERSION);
                let db = Connection::open_with_flags_and_vfs(
                    &path,
                    rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE,
                    "win32-longpath",
                )
                .unwrap();
                db.execute("DROP TABLE placeholders", []).unwrap();
            }
            "conflict_ignore" => {
                let altered = SESSION_SCHEMA_SQL.replace(
                    "pid                 INTEGER PRIMARY KEY,",
                    "pid                 INTEGER PRIMARY KEY ON CONFLICT IGNORE,",
                );
                assert_ne!(altered, SESSION_SCHEMA_SQL);
                raw_create(&path, &altered, SESSION_SCHEMA_VERSION);
            }
            "oversized_sql" => raw_create(
                &path,
                &format!(
                    "CREATE TABLE oversized(value TEXT DEFAULT '{}');",
                    "x".repeat(70000)
                ),
                SESSION_SCHEMA_VERSION,
            ),
            "excess_objects" => raw_create(
                &path,
                &(0..33)
                    .map(|i| format!("CREATE TABLE extra_{i}(value TEXT);"))
                    .collect::<String>(),
                SESSION_SCHEMA_VERSION,
            ),
            "trigger" => raw_create(
                &path,
                &format!(
                    "{SESSION_SCHEMA_SQL}\nCREATE TRIGGER suppress_pending BEFORE INSERT ON working_aces BEGIN SELECT RAISE(IGNORE); END;"
                ),
                SESSION_SCHEMA_VERSION,
            ),
            _ => unreachable!(),
        }
        let before = fixture.snapshot();
        assert!(ro(&path).is_err(), "read-only accepted {defect}");
        assert_eq!(fixture.snapshot(), before, "RO modified {defect}");
        assert!(rw(&path).is_err(), "writable accepted {defect}");
        assert_eq!(fixture.snapshot(), before, "RW modified {defect}");
        fixture.cleanup();
    }
}

#[test]
fn baseline_storage_sidecars_block_absent_or_zero_main_without_creating_state() {
    for suffix in SIDECARS {
        for zero_main in [false, true] {
            let fixture = Fixture::new();
            let path = fixture.path();
            if zero_main {
                std::fs::write(&path, b"").unwrap();
            }
            std::fs::write(sidecar(&path, suffix), b"preserved recovery evidence").unwrap();
            let before = fixture.snapshot();
            assert!(ro(&path).is_err());
            assert!(rw(&path).is_err());
            assert_eq!(fixture.snapshot(), before);
            fixture.cleanup();
        }
    }
    let fixture = Fixture::new();
    assert!(
        ro(&fixture.root.join("missing-parent").join("state.db"))
            .unwrap()
            .is_none()
    );
    assert!(fixture.snapshot().is_empty());
    fixture.cleanup();
}

#[test]
fn baseline_storage_admission_pins_main_and_ancestors_and_exclusive_birth() {
    let fixture = Fixture::new();
    let path = fixture.path();
    let pin = PinnedMain::open(&path, true).unwrap().unwrap();
    assert!(pin.fresh);
    assert!(
        open_component(&path, true, false).is_err(),
        "CREATE_NEW adopted an existing main"
    );
    assert!(std::fs::rename(&path, fixture.root.join("moved.db")).is_err());
    assert!(
        std::fs::rename(
            &fixture.root,
            fixture.base.join("must-not-move-ledger-fixture")
        )
        .is_err()
    );
    pin.verify().unwrap();
    assert_eq!(std::fs::metadata(&path).unwrap().len(), 0);
    drop(pin);
    assert!(
        rw(&path).is_err(),
        "uncertain empty birth was adopted on retry"
    );
    fixture.cleanup();
}

#[test]
fn baseline_storage_refuses_hardlinked_main_without_touching_either_name() {
    let fixture = Fixture::new();
    let path = fixture.path();
    {
        drop(rw(&path).unwrap());
    }
    let other = fixture.root.join("other.db");
    std::fs::hard_link(&path, &other).unwrap();
    let before = fixture.snapshot();
    assert!(ro(&path).is_err());
    assert!(rw(&path).is_err());
    assert_eq!(fixture.snapshot(), before);
    fixture.cleanup();
}

const CHILD_PATH: &str = "ALYSIS_TEST_BASELINE_WAL_CRASH_PATH";
const CHILD_MODE: &str = "ALYSIS_TEST_BASELINE_WAL_CRASH_MODE";
#[test]
#[ignore = "launched only by bounded parent WAL crash fixture"]
fn baseline_storage_wal_child_commits_then_exits_without_drop() {
    let path = std::env::var_os(CHILD_PATH).expect("parent-owned synthetic fixture required");
    let path = Path::new(&path);
    let base = crate::util::test_temp_dir().canonicalize().unwrap();
    let parent = path.parent().unwrap().canonicalize().unwrap();
    assert!(parent.starts_with(&base) && parent != base);
    assert!(
        parent
            .file_name()
            .unwrap()
            .to_str()
            .unwrap()
            .starts_with("alysis-ledger-")
    );
    assert_eq!(path.file_name().unwrap(), "state.db");
    assert!(!path.try_exists().unwrap());
    require_no_sidecars(path).unwrap();
    let connection = rw(path).unwrap();
    assert_durable(&connection);
    connection
        .pragma_update(None, "wal_autocheckpoint", 0)
        .unwrap();
    insert_pending(&connection);
    assert_pending(&connection);
    match std::env::var(CHILD_MODE).as_deref() {
        Ok("valid") => {},
        Ok("version") => connection.pragma_update(None, "user_version", SESSION_SCHEMA_VERSION - 1).unwrap(),
        Ok("schema") => connection.execute_batch("CREATE TRIGGER suppress_pending BEFORE INSERT ON working_aces BEGIN SELECT RAISE(IGNORE); END;").unwrap(),
        _ => panic!("explicit parent fixture mode required"),
    }
    // Process exit bypasses Connection::drop/checkpoint. This qualifies a
    // process crash with committed WAL, not machine power loss.
    std::process::exit(37);
}

fn committed_wal_fixture(fixture: &Fixture, mode: &str) {
    let path = fixture.path();
    let mut child = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "state_db::storage::tests::baseline_storage_wal_child_commits_then_exits_without_drop",
            "--ignored",
            "--nocapture",
        ])
        .env(CHILD_PATH, &path)
        .env(CHILD_MODE, mode)
        .creation_flags(0x08000000)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(20);
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        if std::time::Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            panic!("bounded synthetic WAL child timed out; fixture retained");
        }
        std::thread::sleep(std::time::Duration::from_millis(20));
    };
    assert_eq!(status.code(), Some(37));
    assert!(std::fs::metadata(sidecar(&path, "-wal")).unwrap().len() > 32);
}

#[test]
fn baseline_storage_reopens_committed_wal_after_bounded_process_exit() {
    let fixture = Fixture::new();
    committed_wal_fixture(&fixture, "valid");
    let path = fixture.path();
    {
        let readonly = ro(&path).unwrap().unwrap();
        assert_pending(&readonly);
    }
    {
        let reopened = rw(&path).unwrap();
        assert_durable(&reopened);
        assert_pending(&reopened);
    }
    fixture.cleanup();
}

#[test]
fn baseline_storage_refused_cold_wal_preserves_main_wal_and_pending_rows() {
    for mode in ["version", "schema"] {
        let fixture = Fixture::new();
        committed_wal_fixture(&fixture, mode);
        let path = fixture.path();
        let authoritative = || {
            (
                std::fs::read(&path).unwrap(),
                std::fs::read(sidecar(&path, "-wal")).unwrap(),
            )
        };
        let before = authoritative();
        assert!(ro(&path).is_err(), "accepted unsupported cold WAL {mode}");
        assert_eq!(
            authoritative(),
            before,
            "RO changed authoritative {mode} WAL history"
        );
        assert!(
            rw(&path).is_err(),
            "writable accepted unsupported cold WAL {mode}"
        );
        assert_eq!(
            authoritative(),
            before,
            "RW changed authoritative {mode} WAL history"
        );
        // Test-only direct read proves pending rows remain accessible for an
        // explicit supported recovery/migration tool. Ordinary SHM rebuild
        // and read marks are permitted; no history is inferred from SHM.
        {
            let raw = connect(&path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
            assert_pending(&raw);
        }
        assert_eq!(authoritative(), before);
        assert!(std::fs::read_dir(&fixture.root).unwrap().all(|entry| {
            ["state.db", "state.db-wal", "state.db-shm"]
                .contains(&entry.unwrap().file_name().to_str().unwrap())
        }));
        fixture.cleanup();
    }
}

#[test]
fn baseline_storage_final_admission_observes_late_link_change_or_blocks_it() {
    let fixture = Fixture::new();
    let path = fixture.path();
    drop(rw(&path).unwrap());
    let pin = PinnedMain::open(&path, false).unwrap().unwrap();
    match std::fs::hard_link(&path, fixture.root.join("late-link.db")) {
        Ok(()) => assert!(
            pin.verify().is_err(),
            "late hardlink bypassed final metadata admission"
        ),
        Err(error) => {
            assert_eq!(
                error.raw_os_error(),
                Some(32),
                "unexpected hardlink failure"
            );
            pin.verify().unwrap();
        }
    }
    drop(pin);
    fixture.cleanup();
}

#[test]
fn baseline_storage_canonical_spelling_reopens_same_main_and_pins_both_chains() {
    let fixture = Fixture::new();
    let path = fixture.path();
    drop(rw(&path).unwrap());
    let ordinary = PathBuf::from(path_id::strip_extended_prefix(path.to_str().unwrap()));
    let ordinary_pin = PinnedMain::open(&ordinary, false).unwrap().unwrap();
    let canonical = ordinary_pin.path.clone();
    let canonical_pin = PinnedMain::open(&canonical, false).unwrap().unwrap();
    assert_eq!(ordinary_pin.id, canonical_pin.id);
    assert_eq!(
        ordinary_pin.requested_parents.last().unwrap().id,
        ordinary_pin.canonical_parents.last().unwrap().id
    );
    assert!(!ordinary_pin.canonical_parents.is_empty());
    ordinary_pin.verify().unwrap();
    canonical_pin.verify().unwrap();
    for parent in &ordinary_pin.canonical_parents {
        assert!(same_spelling(
            &parent.resolved,
            parent.path.to_str().unwrap()
        ));
    }
    assert!(
        std::fs::rename(
            &fixture.root,
            fixture.base.join("must-not-move-canonical-ledger")
        )
        .is_err()
    );
    drop(canonical_pin);
    drop(ordinary_pin);
    {
        let connection = rw(&ordinary).unwrap();
        assert_durable(&connection);
    }
    {
        let connection = ro(&canonical).unwrap().unwrap();
        assert_eq!(
            schema_rows(&connection).unwrap(),
            expected_schema(SESSION_SCHEMA_SQL).unwrap()
        );
    }
    fixture.cleanup();
}

#[test]
fn baseline_storage_namespace_binding_refuses_foreign_parent_and_leaf_change() {
    let fixture = Fixture::new();
    let actual = fixture.path();
    drop(rw(&actual).unwrap());
    let other_parent = fixture.root.join("other-parent");
    std::fs::create_dir(&other_parent).unwrap();
    let original_id = path_id::capture_file_id(actual.to_str().unwrap()).unwrap();
    for (requested, message) in [
        (other_parent.join("state.db"), "parents differ"),
        (fixture.root.join("different.db"), "leaf spelling"),
    ] {
        let original = open_component(&actual, false, false).unwrap();
        assert_eq!(
            path_id::file_id_from_handle(original.raw()).unwrap(),
            original_id
        );
        let parents = ancestors(&requested)
            .into_iter()
            .map(|parent| PinnedDirectory::open(parent, false).unwrap())
            .collect();
        // Supply conflicting observations of the same retained main object;
        // production must reject them before any SQLite admission or effects.
        let error = PinnedMain::bind(&requested, original, parents, false)
            .err()
            .unwrap();
        assert!(error.to_string().contains(message), "{error:#}");
        assert_eq!(
            path_id::capture_file_id(actual.to_str().unwrap()).unwrap(),
            original_id
        );
        assert!(!requested.try_exists().unwrap());
    }
    fixture.cleanup();
}

#[test]
fn baseline_storage_mismatched_sidecar_namespaces_are_not_ignored() {
    let fixture = Fixture::new();
    let other = fixture.root.join("other");
    std::fs::create_dir(&other).unwrap();
    let requested = fixture.path();
    let canonical = other.join("state.db");
    for suffix in SIDECARS {
        let request_sidecar = sidecar(&requested, suffix);
        let actual_sidecar = sidecar(&canonical, suffix);
        std::fs::write(&request_sidecar, b"requested recovery evidence").unwrap();
        assert!(require_sidecar_agreement(&requested, &canonical).is_err());
        std::fs::write(&actual_sidecar, b"canonical recovery evidence").unwrap();
        assert!(require_sidecar_agreement(&requested, &canonical).is_err());
        assert_eq!(
            std::fs::read(&request_sidecar).unwrap(),
            b"requested recovery evidence"
        );
        assert_eq!(
            std::fs::read(&actual_sidecar).unwrap(),
            b"canonical recovery evidence"
        );
        std::fs::remove_file(request_sidecar).unwrap();
        std::fs::remove_file(actual_sidecar).unwrap();
    }
    fixture.cleanup();
}

#[test]
fn baseline_storage_requested_and_canonical_ancestry_refuse_real_junction() {
    use windows::Win32::{Storage::FileSystem::FILE_SHARE_DELETE, System::IO::DeviceIoControl};
    let fixture = Fixture::new();
    let physical = fixture.root.join("physical");
    let link = fixture.root.join("linked");
    std::fs::create_dir(&physical).unwrap();
    std::fs::create_dir(&link).unwrap();
    let target = physical.join("state.db");
    drop(rw(&target).unwrap());
    let before = std::fs::read(&target).unwrap();
    let link_handle = OwnedHandle(unsafe {
        CreateFileW(
            pcwstr(&wstr(link.to_str().unwrap())),
            0x40000000,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        .unwrap()
    });
    let target_text = path_id::strip_extended_prefix(physical.to_str().unwrap());
    let substitute = wstr(&format!(r"\??\{target_text}"));
    let print = wstr(target_text);
    let payload = 8 + 2 * (substitute.len() + print.len());
    assert!(payload < u16::MAX as usize);
    let mut buffer = Vec::new();
    buffer.extend_from_slice(&0xa0000003u32.to_le_bytes());
    buffer.extend_from_slice(&(payload as u16).to_le_bytes());
    buffer.extend_from_slice(&0u16.to_le_bytes());
    for value in [
        0,
        ((substitute.len() - 1) * 2) as u16,
        (substitute.len() * 2) as u16,
        ((print.len() - 1) * 2) as u16,
    ] {
        buffer.extend_from_slice(&value.to_le_bytes());
    }
    for value in substitute.into_iter().chain(print) {
        buffer.extend_from_slice(&value.to_le_bytes());
    }
    let mut returned = 0;
    unsafe {
        DeviceIoControl(
            link_handle.raw(),
            0x000900a4,
            Some(buffer.as_ptr().cast()),
            buffer.len() as u32,
            None,
            0,
            Some(&mut returned),
            None,
        )
        .unwrap();
    }
    drop(link_handle);
    let error = PinnedMain::open(&link.join("state.db"), false)
        .err()
        .unwrap();
    assert!(error.to_string().contains("reparse point"), "{error:#}");
    assert!(PinnedDirectory::open(&link, true).is_err());
    assert_eq!(std::fs::read(&target).unwrap(), before);
    std::fs::remove_dir(&link).unwrap(); // Remove only the test-owned junction.
    fixture.cleanup();
}
