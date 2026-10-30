//! Candidate-only filesystem boundary around SQLite's path-based Windows VFS.
//! No baseline database, account or machine policy is modified here.

use anyhow::{Context, Result, ensure};
use rusqlite::{Connection, OpenFlags};
use serde::{Deserialize, Serialize};
use std::path::Path;

use crate::{
    account_install::{AccountBinding, AccountMode, ReadyInstallation, SetupRecord},
    execution_lease::ExecutionLease,
};

use super::fs;

const MANIFEST: &str = "identity.json";
const DATABASE: &str = "state.db";
const SQLITE_VFS: &str = "win32-longpath";

/// Select the bundled locking VFS explicitly. The default Windows VFS has a
/// smaller byte-path budget even when a CreateFileW name has an extended prefix.
fn sqlite_filename(path: &Path) -> Result<String> {
    let filename = fs::extended_path(path)?;
    // sqlite3_vfs_find initializes the bundled library; it opens no files.
    // This implementation never registers, unregisters or replaces VFS objects.
    let vfs = unsafe { rusqlite::ffi::sqlite3_vfs_find(c"win32-longpath".as_ptr()) };
    ensure!(
        !vfs.is_null(),
        "candidate database requires the locking win32-longpath VFS"
    );
    let maximum = unsafe { (*vfs).mxPathname };
    // xFullPathname's budget is bytes, while Win32's is UTF-16 units. Reserve
    // this single DELETE-mode database's rollback suffix and terminating NUL.
    // ATTACH is disabled per connection below, so multi-database transactions
    // cannot create super-journals through the supported SQL interface.
    ensure!(
        maximum > 0 && filename.len() + "-journal".len() + 1 <= maximum as usize,
        "candidate database path exceeds its VFS byte limit including auxiliary suffix"
    );
    fs::extended_path(Path::new(&format!("{}-journal", path.display())))?;
    Ok(filename)
}

#[derive(Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    version: u32,
    storage: super::StorageRecord,
    binding: AccountBinding,
    offline_sid: String,
    online_sid: String,
    directory_id: [u8; 24],
    database_id: [u8; 24],
}

/// A clean candidate database admitted before SQLite can inspect a hot journal.
///
/// The shared execution lease, every lexical ancestor, the private mode
/// directory and the main/manifest object identities remain pinned throughout
/// connection use. With ordinary RAII use (no leaked statements), the
/// connection closes before those handles are dropped. Auxiliary
/// files inherit the private parent's policy; SQLite's Windows VFS does NOT
/// provide this module's no-follow guarantee itself. Preexisting auxiliary or
/// unknown files are refused and preserved for future explicit recovery.
///
/// Host-owner and administrator processes are trusted; admission cannot revoke
/// old handles after a trusted administrator historically weakened a DACL.
/// Production uses the installer-attested OS ProgramFilesX64 KnownFolder root,
/// never a caller/environment path, and rejects untrusted owners/mutation
/// grants or changed identities throughout its ancestry.
/// The API accepts only
/// trusted candidate implementation SQL. ATTACH is disabled per connection;
/// callers must not change journal/temp modes, load extensions or execute
/// user/model-provided SQL.
/// The shared connection reference cannot be moved out of its owning guard.
pub struct PrivateDatabase<'lease> {
    // Field order is intentional: SQLite closes before its pins are released.
    connection: Connection,
    _main: fs::Pinned,
    _manifest: fs::Pinned,
    _parents: Vec<fs::Pinned>,
    binding: AccountBinding,
    _lease: Option<&'lease ExecutionLease>,
}

impl<'lease> PrivateDatabase<'lease> {
    /// Reload machine identity while holding the shared lease, refusing a stale
    /// ready snapshot before creating or opening candidate filesystem state.
    pub fn open(
        installation: &ReadyInstallation,
        lease: &'lease ExecutionLease,
        mode: AccountMode,
    ) -> Result<Self> {
        let current = ReadyInstallation::read(lease)?;
        ensure!(
            current.record == installation.record,
            "candidate installation changed; reload its identity"
        );
        let storage = super::storage::StoragePins::open(&current.record)?;
        Self::open_pinned(storage, &current.record, mode, Some(lease))
    }

    /// Borrow only for trusted candidate ACL statements. Validate the immutable
    /// header on every entry; no owned/mutable Connection escape is provided.
    pub fn connection(&self) -> Result<&Connection> {
        super::validate_header(&self.connection, &self.binding)?;
        Ok(&self.connection)
    }

    pub(crate) fn file_identity(&self) -> [u8; 24] {
        self._main.id.as_bytes()
    }

    #[cfg(test)]
    pub(super) fn open_under(
        session_root: &Path,
        record: &SetupRecord,
        mode: AccountMode,
        lease: Option<&'lease ExecutionLease>,
    ) -> Result<Self> {
        let storage = super::storage::StoragePins::open_fixture(record, session_root)?;
        Self::open_pinned(storage, record, mode, lease)
    }

    fn open_pinned(
        storage: super::storage::StoragePins,
        record: &SetupRecord,
        mode: AccountMode,
        lease: Option<&'lease ExecutionLease>,
    ) -> Result<Self> {
        record.validate_ready(
            crate::account_install::SCHEMA_VERSION,
            &crate::sid::current_user_sid()?,
        )?;
        let binding = record.binding(mode)?;
        let offline = record.binding(AccountMode::Offline)?;
        let online = record.binding(AccountMode::Online)?;
        let policy = fs::Policy {
            owner: binding.owner_sid.clone(),
            offline: offline.account_sid,
            online: online.account_sid,
            group: binding.group_sid.clone(),
        };
        let mut parents = storage.parents;
        let directory = parents
            .last()
            .context("attested storage root missing")?
            .path
            .join(mode.directory());
        let main_path = directory.join(DATABASE);
        let sqlite_path = sqlite_filename(&main_path)?;
        let (pinned, fresh) =
            fs::open_child_directory(parents.last().unwrap(), mode.directory(), Some(&policy))?;
        // Never tighten or adopt an existing directory. Even a fresh one
        // is checked through its pinned handle before creating children.
        policy.validate(&pinned, true)?;
        parents.push(pinned);
        admit_entries(&directory, fresh)?;
        let manifest_path = directory.join(MANIFEST);
        let main = if fresh {
            fs::Pinned::create_file(&main_path, &policy)?
        } else {
            fs::Pinned::open(&main_path, false, false)?
        };
        policy.validate(&main, false)?;
        let expected = Manifest {
            version: 2,
            storage: storage.attestation,
            binding: binding.clone(),
            offline_sid: policy.offline.clone(),
            online_sid: policy.online.clone(),
            directory_id: parents.last().unwrap().id.as_bytes(),
            database_id: main.id.as_bytes(),
        };
        let manifest = if fresh {
            let writer = fs::Pinned::create_file(&manifest_path, &policy)?;
            policy.validate(&writer, false)?;
            writer.write_new(&serde_json::to_vec(&expected)?)?;
            let identity = writer.id;
            drop(writer);
            let pinned = fs::Pinned::open(&manifest_path, false, true)?;
            ensure!(
                pinned.id == identity,
                "candidate manifest was replaced after creation"
            );
            pinned
        } else {
            fs::Pinned::open(&manifest_path, false, true)?
        };
        policy.validate(&manifest, false)?;
        let observed: Manifest = serde_json::from_slice(&manifest.read_small()?)
            .context("candidate identity manifest is malformed; preserve state for repair")?;
        ensure!(
            observed == expected,
            "candidate manifest identity mismatch; preserve state for repair"
        );
        ensure!(
            fresh || main.size()? >= 100,
            "candidate database initialization is incomplete; preserve state for repair"
        );
        // No CREATE flag: exclusivity/final DACL came from CreateFileW above.
        // Admission must precede this call: SQLite may recover journals before
        // the first SQL-level identity validation.
        let mut connection = Connection::open_with_flags_and_vfs(
            &sqlite_path,
            OpenFlags::SQLITE_OPEN_READ_WRITE,
            SQLITE_VFS,
        )?;
        // Restrict the candidate to its one admitted database before any SQL.
        // In particular, ATTACH must not bypass filesystem admission or create
        // super-journals whose names are outside the rollback-path budget.
        unsafe {
            rusqlite::ffi::sqlite3_limit(
                connection.handle(),
                rusqlite::ffi::SQLITE_LIMIT_ATTACHED,
                0,
            );
            ensure!(
                rusqlite::ffi::sqlite3_limit(
                    connection.handle(),
                    rusqlite::ffi::SQLITE_LIMIT_ATTACHED,
                    -1,
                ) == 0,
                "candidate database could not disable ATTACH"
            );
        }
        if !fresh {
            super::validate_header(&connection, &binding)?;
        }
        connection.execute_batch(
            "PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL; PRAGMA temp_store=MEMORY;",
        )?;
        ensure!(
            connection.query_row("PRAGMA journal_mode", [], |r| r.get::<_, String>(0))? == "delete",
            "candidate database requires DELETE journal mode"
        );
        if fresh {
            super::initialize_header(&mut connection, &binding)?;
        }
        Ok(Self {
            connection,
            _main: main,
            _manifest: manifest,
            _parents: parents,
            binding,
            _lease: lease,
        })
    }
}

fn admit_entries(directory: &Path, fresh: bool) -> Result<()> {
    let mut names = Vec::new();
    for entry in std::fs::read_dir(directory)? {
        let entry = entry?;
        let name = entry
            .file_name()
            .into_string()
            .map_err(|_| anyhow::anyhow!("non-Unicode candidate state entry"))?;
        ensure!(
            !fresh && matches!(name.as_str(), DATABASE | MANIFEST),
            "candidate state contains an auxiliary or unknown object ({name}); preserve it for explicit recovery"
        );
        names.push(name);
    }
    ensure!(
        fresh
            || (names.len() == 2
                && names.iter().any(|name| name == DATABASE)
                && names.iter().any(|name| name == MANIFEST)),
        "candidate state is partially initialized; refusing adoption"
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::account_install::synthetic_ready;

    struct Fixture {
        root: std::path::PathBuf,
        cleanup_root: std::path::PathBuf,
        record: SetupRecord,
    }
    impl Fixture {
        fn new() -> Self {
            Self::new_with_minimum_root_units(0)
        }
        fn new_with_minimum_root_units(minimum: usize) -> Self {
            let mut root = crate::util::test_temp_dir().join(format!(
                "alysis-candidate-db-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
            let cleanup_root = root.clone();
            std::fs::create_dir(&root).unwrap();
            while root.to_str().unwrap().encode_utf16().count() < minimum {
                root.push("private-long-path-component-0123456789");
                std::fs::create_dir(&root).unwrap();
            }
            let mut record = synthetic_ready();
            record.owner_sid = crate::sid::current_user_sid().unwrap();
            record.storage = None;
            let plan =
                super::super::storage::PreparedStorage::prepare_fixture(&record, &root).unwrap();
            record.storage = Some(plan.intent().clone());
            let pins = plan.create().unwrap();
            record.storage = Some(pins.attestation().clone());
            Self {
                root,
                cleanup_root,
                record,
            }
        }
        fn open(&self) -> Result<PrivateDatabase<'static>> {
            PrivateDatabase::open_under(&self.root, &self.record, AccountMode::Offline, None)
        }
        fn path(&self) -> std::path::PathBuf {
            super::super::database_path(
                &self.root,
                &self.record.binding(AccountMode::Offline).unwrap(),
            )
            .unwrap()
        }
        fn policy(&self) -> fs::Policy {
            fs::Policy {
                owner: self.record.owner_sid.clone(),
                offline: self.record.offline.as_ref().unwrap().sid.clone(),
                online: self.record.online.as_ref().unwrap().sid.clone(),
                group: self.record.group.as_ref().unwrap().sid.clone(),
            }
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            if self.cleanup_root.exists() {
                std::fs::remove_dir_all(&self.cleanup_root)
                    .expect("remove isolated candidate fixture");
            }
        }
    }

    #[test]
    fn candidate_disk_fixture_uses_long_path_but_admission_still_refuses_aliases() {
        use crate::util::{pcwstr, wstr};
        use windows::Win32::Storage::FileSystem::GetShortPathNameW;

        let fixture = Fixture::new();
        fs::Pinned::open(&fixture.root, true, false).unwrap();
        let dotted = fixture.root.join(".");
        let error = fs::Pinned::open(&dotted, true, false).err().unwrap();
        assert!(error.to_string().contains("ambiguous path component"));

        let wide = wstr(&fs::extended_path(&fixture.root).unwrap());
        let required = unsafe { GetShortPathNameW(pcwstr(&wide), None) };
        assert!(required > 0 && required < 32768);
        let mut buffer = vec![0; required as usize + 1];
        let length = unsafe { GetShortPathNameW(pcwstr(&wide), Some(&mut buffer)) };
        assert!(length > 0 && (length as usize) < buffer.len());
        let short = String::from_utf16(&buffer[..length as usize]).unwrap();
        let short = crate::path_id::strip_extended_prefix(&short);
        if !short.eq_ignore_ascii_case(fixture.root.to_str().unwrap()) {
            let error = fs::Pinned::open(Path::new(&short), true, false)
                .err()
                .unwrap();
            assert!(error.to_string().contains("alias or changed"));
        } else {
            // Volumes with 8.3 name creation disabled have no short alias to
            // exercise; the deterministic dotted alias above still rejects.
            eprintln!("fixture volume exposes no 8.3 alias");
        }
    }

    #[test]
    fn candidate_disk_create_commit_reopen_and_mode_isolation() {
        let fixture = Fixture::new();
        {
            let db = fixture.open().unwrap();
            let connection = db.connection().unwrap();
            connection.execute_batch("CREATE TABLE canary(value TEXT); BEGIN IMMEDIATE; INSERT INTO canary VALUES('private');").unwrap();
            assert!(fixture.path().with_extension("db-journal").exists());
            {
                let journal =
                    fs::Pinned::open(&fixture.path().with_extension("db-journal"), false, false)
                        .unwrap();
                fixture.policy().validate_inherited_file(&journal).unwrap();
            }
            connection.execute_batch("COMMIT;").unwrap();
            assert!(!fixture.path().with_extension("db-journal").exists());
        }
        let reopened = fixture.open().unwrap();
        assert_eq!(
            reopened
                .connection()
                .unwrap()
                .query_row("SELECT value FROM canary", [], |r| r.get::<_, String>(0))
                .unwrap(),
            "private"
        );
        let online =
            PrivateDatabase::open_under(&fixture.root, &fixture.record, AccountMode::Online, None)
                .unwrap();
        assert!(
            online
                .connection()
                .unwrap()
                .query_row("SELECT value FROM canary", [], |r| r.get::<_, String>(0))
                .is_err()
        );
    }

    #[test]
    fn candidate_disk_vfs_budget_reserves_rollback_suffix_in_both_encodings() {
        let boundary = format!("C:\\{}x", "x\\".repeat(16375));
        assert_eq!(boundary.encode_utf16().count(), 32754);
        assert!(sqlite_filename(Path::new(&boundary)).is_ok());
        assert!(
            sqlite_filename(Path::new(&format!("{boundary}x")))
                .unwrap_err()
                .to_string()
                .contains("UTF-16 limit")
        );

        // These BMP characters fit Win32's UTF-16 bound but exceed the VFS's
        // UTF-8 byte budget. Reject before xFullPathname could truncate them.
        let byte_overflow = format!("C:\\{}state.db", "\u{65e5}\u{672c}\\".repeat(10800));
        assert!(fs::extended_path(Path::new(&byte_overflow)).is_ok());
        assert!(
            sqlite_filename(Path::new(&byte_overflow))
                .unwrap_err()
                .to_string()
                .contains("VFS byte limit")
        );
    }

    #[test]
    fn candidate_disk_over_300_utf16_commit_cold_reopen_journal_and_locking() {
        let fixture = Fixture::new_with_minimum_root_units(300);
        let path = fixture.path();
        let units = path.to_str().unwrap().encode_utf16().count();
        assert!(
            units > 300,
            "long-path fixture was only {units} UTF-16 units"
        );
        let database_id;
        let manifest_bytes;
        {
            let first = fixture.open().unwrap();
            database_id = first.file_identity();
            manifest_bytes = std::fs::read(path.with_file_name(MANIFEST)).unwrap();
            let connection = first.connection().unwrap();
            let unattested = fixture.root.join("unattested.db");
            let attach_error = connection
                .execute(
                    "ATTACH DATABASE ?1 AS unattested",
                    [unattested.to_str().unwrap()],
                )
                .unwrap_err();
            assert!(
                attach_error
                    .to_string()
                    .contains("too many attached databases")
            );
            assert!(!unattested.exists());
            assert_eq!(
                connection
                    .query_row("SELECT count(*) FROM pragma_database_list", [], |r| r
                        .get::<_, i64>(0))
                    .unwrap(),
                1
            );
            assert_eq!(
                connection
                    .query_row("PRAGMA journal_mode", [], |r| r.get::<_, String>(0))
                    .unwrap(),
                "delete"
            );
            assert_eq!(
                connection
                    .query_row("PRAGMA synchronous", [], |r| r.get::<_, i64>(0))
                    .unwrap(),
                2
            );
            connection
                .execute_batch("CREATE TABLE long_canary(value TEXT);")
                .unwrap();
            // Both connections are admitted while clean, before a journal exists.
            let second = fixture.open().unwrap();
            assert_eq!(second.file_identity(), database_id);
            let contender = second.connection().unwrap();
            contender.busy_timeout(std::time::Duration::ZERO).unwrap();
            connection
                .execute_batch("BEGIN IMMEDIATE; INSERT INTO long_canary VALUES('first');")
                .unwrap();
            let journal_path = path.with_extension("db-journal");
            assert!(journal_path.exists());
            {
                let journal = fs::Pinned::open(&journal_path, false, false).unwrap();
                fixture.policy().validate_inherited_file(&journal).unwrap();
            }
            let error = contender.execute_batch("BEGIN IMMEDIATE;").unwrap_err();
            assert_eq!(
                error.sqlite_error_code(),
                Some(rusqlite::ErrorCode::DatabaseBusy)
            );
            connection.execute_batch("COMMIT;").unwrap();
            assert!(!journal_path.exists());
            contender
                .execute_batch("BEGIN IMMEDIATE; INSERT INTO long_canary VALUES('second'); COMMIT;")
                .unwrap();
            assert!(!journal_path.exists());
        }
        let reopened = fixture.open().unwrap();
        assert_eq!(reopened.file_identity(), database_id);
        assert_eq!(
            std::fs::read(path.with_file_name(MANIFEST)).unwrap(),
            manifest_bytes
        );
        assert_eq!(reopened.connection().unwrap().query_row(
            "SELECT group_concat(value, ',') FROM (SELECT value FROM long_canary ORDER BY rowid)",
            [], |r| r.get::<_, String>(0),
        ).unwrap(), "first,second");
        drop(reopened);
        // Cold admission at the same long path must retain unknown sidecars.
        let original = std::fs::read(&path).unwrap();
        let sidecar = path.with_extension("db-journal");
        std::fs::write(&sidecar, b"ambiguous long-path journal").unwrap();
        assert!(
            format!("{:#}", fixture.open().err().unwrap()).contains("auxiliary or unknown object")
        );
        assert_eq!(
            std::fs::read(&sidecar).unwrap(),
            b"ambiguous long-path journal"
        );
        assert_eq!(std::fs::read(&path).unwrap(), original);
        assert_eq!(
            std::fs::read(path.with_file_name(MANIFEST)).unwrap(),
            manifest_bytes
        );
        eprintln!(
            "CANDIDATE_LONG_PATH_UTF16={units}; COMMIT/COLD_REOPEN/JOURNAL/LOCKING/UNKNOWN_SIDECAR=PASS"
        );
    }

    #[test]
    fn candidate_disk_pins_main_manifest_and_all_ancestors() {
        let fixture = Fixture::new();
        let db = fixture.open().unwrap();
        for path in [
            fixture.path(),
            fixture.path().parent().unwrap().to_owned(),
            fixture.root.clone(),
        ] {
            assert!(
                std::fs::rename(&path, path.with_extension("moved")).is_err(),
                "rename unexpectedly succeeded: {}",
                path.display()
            );
        }
        assert!(std::fs::write(fixture.path().with_file_name(MANIFEST), b"changed").is_err());
        drop(db);
        let moved = fixture.root.with_extension("moved");
        std::fs::rename(&fixture.root, &moved).unwrap();
        std::fs::rename(&moved, &fixture.root).unwrap();
        fixture.open().unwrap();
    }

    #[test]
    fn candidate_disk_refuses_hot_journals_unknown_files_and_partial_state() {
        for name in [
            "state.db-journal",
            "state.db-wal",
            "state.db-shm",
            "unexpected",
        ] {
            let fixture = Fixture::new();
            drop(fixture.open().unwrap());
            let original = std::fs::read(fixture.path()).unwrap();
            let path = fixture.path().with_file_name(name);
            std::fs::write(&path, b"preserve this unverified object").unwrap();
            assert!(
                format!("{:#}", fixture.open().err().unwrap())
                    .contains("auxiliary or unknown object")
            );
            assert_eq!(
                std::fs::read(&path).unwrap(),
                b"preserve this unverified object"
            );
            assert_eq!(std::fs::read(fixture.path()).unwrap(), original);
        }
        let fixture = Fixture::new();
        drop(fixture.open().unwrap());
        std::fs::remove_file(fixture.path().with_file_name(MANIFEST)).unwrap();
        let original = std::fs::read(fixture.path()).unwrap();
        assert!(fixture.open().is_err());
        assert_eq!(std::fs::read(fixture.path()).unwrap(), original);
    }

    #[test]
    fn candidate_disk_refuses_hardlinks_replacements_and_malformed_manifest() {
        let fixture = Fixture::new();
        drop(fixture.open().unwrap());
        let original = std::fs::read(fixture.path()).unwrap();
        let alias = fixture.path().with_file_name("alias");
        std::fs::hard_link(fixture.path(), &alias).unwrap();
        // Put the second name outside the private directory so directory-entry
        // admission alone cannot account for this rejection.
        let outside = fixture.root.join("alias");
        std::fs::rename(&alias, &outside).unwrap();
        assert!(format!("{:#}", fixture.open().err().unwrap()).contains("multiple hardlinks"));
        std::fs::remove_file(outside).unwrap();
        fixture.open().unwrap();
        // Keep the old object alive while creating a new same-content file to
        // avoid NTFS file-record reuse confusing a replacement regression.
        let old = fixture.root.join("old-db");
        std::fs::rename(fixture.path(), &old).unwrap();
        let policy = fs::Policy {
            owner: fixture.record.owner_sid.clone(),
            offline: fixture.record.offline.as_ref().unwrap().sid.clone(),
            online: fixture.record.online.as_ref().unwrap().sid.clone(),
            group: fixture.record.group.as_ref().unwrap().sid.clone(),
        };
        drop(fs::Pinned::create_file(&fixture.path(), &policy).unwrap());
        std::fs::write(fixture.path(), &original).unwrap();
        assert!(
            format!("{:#}", fixture.open().err().unwrap()).contains("manifest identity mismatch")
        );
        assert_eq!(std::fs::read(fixture.path()).unwrap(), original);
        std::fs::remove_file(fixture.path()).unwrap();
        std::fs::rename(old, fixture.path()).unwrap();
        std::fs::write(fixture.path().with_file_name(MANIFEST), b"malformed").unwrap();
        assert!(format!("{:#}", fixture.open().err().unwrap()).contains("manifest is malformed"));
        assert_eq!(
            std::fs::read(fixture.path().with_file_name(MANIFEST)).unwrap(),
            b"malformed"
        );
    }

    #[test]
    fn candidate_disk_manifest_binds_every_identity_before_sqlite() {
        let fixture = Fixture::new();
        drop(fixture.open().unwrap());
        let path = fixture.path().with_file_name(MANIFEST);
        let original_manifest = std::fs::read(&path).unwrap();
        let original_database = std::fs::read(fixture.path()).unwrap();
        for field in [
            "version",
            "directory_id",
            "database_id",
            "offline_sid",
            "online_sid",
            "installation_id",
            "owner_sid",
            "account_sid",
            "group_sid",
            "mode",
            "storage_root_id",
            "storage_ancestor_id",
            "storage_owner",
        ] {
            let mut manifest: serde_json::Value =
                serde_json::from_slice(&original_manifest).unwrap();
            match field {
                "version" => manifest[field] = 1.into(),
                "storage_root_id" => {
                    manifest["storage"]["root_id"][12] =
                        (manifest["storage"]["root_id"][12].as_u64().unwrap() ^ 1).into()
                }
                "storage_ancestor_id" => {
                    manifest["storage"]["ancestors"][0]["file_id"][12] =
                        (manifest["storage"]["ancestors"][0]["file_id"][12]
                            .as_u64()
                            .unwrap()
                            ^ 1)
                        .into()
                }
                "storage_owner" => {
                    manifest["storage"]["identity"]["owner_sid"] = "S-1-5-21-1-2-3-9999".into()
                }
                "directory_id" | "database_id" => {
                    manifest[field][0] = (manifest[field][0].as_u64().unwrap() ^ 1).into()
                }
                "offline_sid" | "online_sid" => manifest[field] = "S-1-5-21-1-2-3-9999".into(),
                "installation_id" => {
                    manifest["binding"][field] = "1123456789abcdef0123456789abcdef".into()
                }
                "mode" => manifest["binding"][field] = "online".into(),
                _ => manifest["binding"][field] = "S-1-5-21-1-2-3-9999".into(),
            }
            let altered = serde_json::to_vec(&manifest).unwrap();
            std::fs::write(&path, &altered).unwrap();
            assert!(
                format!("{:#}", fixture.open().err().unwrap())
                    .contains("manifest identity mismatch"),
                "accepted altered {field}"
            );
            assert_eq!(std::fs::read(&path).unwrap(), altered);
            assert_eq!(std::fs::read(fixture.path()).unwrap(), original_database);
        }
    }

    #[test]
    fn candidate_disk_refuses_incomplete_and_corrupt_database_without_repair() {
        let fixture = Fixture::new();
        drop(fixture.open().unwrap());
        for data in [Vec::new(), vec![b'x'; 1000]] {
            std::fs::write(fixture.path(), &data).unwrap();
            let error = format!("{:#}", fixture.open().err().unwrap());
            assert!(error.contains(if data.is_empty() {
                "initialization is incomplete"
            } else {
                "identity header is missing"
            }));
            assert_eq!(std::fs::read(fixture.path()).unwrap(), data);
        }
    }

    #[test]
    fn candidate_disk_atomic_directory_birth_pins_exact_owner_object() {
        let fixture = Fixture::new();
        let policy = fixture.policy();
        let chain = fs::pin_fixture_path(&fixture.root, &policy).unwrap();
        let parent = chain.last().unwrap();
        let (created, fresh) =
            fs::open_child_directory(parent, "new-private", Some(&policy)).unwrap();
        assert!(fresh);
        policy.validate(&created, true).unwrap();
        assert!(std::fs::rename(&created.path, fixture.root.join("replacement")).is_err());
        let (reopened, fresh) =
            fs::open_child_directory(parent, "new-private", Some(&policy)).unwrap();
        assert!(!fresh);
        assert_eq!(created.id, reopened.id);
        policy.validate(&reopened, true).unwrap();
        // A preexisting ordinary directory is not silently tightened/adopted.
        std::fs::create_dir(fixture.root.join("preexisting")).unwrap();
        let (untrusted, fresh) =
            fs::open_child_directory(parent, "preexisting", Some(&policy)).unwrap();
        assert!(!fresh);
        assert!(policy.validate(&untrusted, true).is_err());
    }

    #[test]
    fn candidate_disk_reparse_ancestor_is_refused_before_creating_state() {
        use crate::util::{OwnedHandle, pcwstr, wstr};
        use windows::Win32::{Storage::FileSystem::*, System::IO::DeviceIoControl};
        let fixture = Fixture::new();
        let target = fixture.root.join("target");
        let link = fixture.root.join("junction");
        std::fs::create_dir(&target).unwrap();
        std::fs::create_dir(&link).unwrap();
        let wide = wstr(&fs::extended_path(&link).unwrap());
        let handle = OwnedHandle(
            unsafe {
                CreateFileW(
                    pcwstr(&wide),
                    0x40000000,
                    FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                    None,
                    OPEN_EXISTING,
                    FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                    None,
                )
            }
            .unwrap(),
        );
        let substitute = wstr(&format!(r"\??\{}", target.display()));
        let print = wstr(target.to_str().unwrap());
        let mut buffer = Vec::new();
        buffer.extend_from_slice(&0xa0000003u32.to_le_bytes());
        buffer
            .extend_from_slice(&((8 + 2 * (substitute.len() + print.len())) as u16).to_le_bytes());
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
                handle.raw(),
                0x000900a4,
                Some(buffer.as_ptr().cast()),
                buffer.len() as u32,
                None,
                0,
                Some(&mut returned),
                None,
            )
        }
        .unwrap();
        drop(handle);
        let result =
            PrivateDatabase::open_under(&link, &fixture.record, AccountMode::Offline, None);
        assert!(format!("{:#}", result.err().unwrap()).contains("reparse point"));
        assert_eq!(std::fs::read_dir(&target).unwrap().count(), 0);
        unsafe { RemoveDirectoryW(pcwstr(&wide)) }.unwrap();
    }
}
