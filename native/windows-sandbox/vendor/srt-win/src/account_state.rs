//! Identity gates for the opt-in account backend's future recovery state.
//!
//! The baseline ACL database infers one installed account SID during recovery;
//! candidate rows must never enter that database. Each selected account gets a
//! separate database with an immutable identity header. A protected, owner-only
//! directory and handle-bound filesystem validation precede every disk open.
//! The disk opener admits clean state only; ambiguous crash artifacts remain
//! preserved for a future explicit candidate recovery implementation.

use anyhow::{Context, Result, ensure};
use rusqlite::{Connection, params};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

use crate::account_install::{AccountBinding, validate_random_id};

#[path = "account_state_disk.rs"]
mod disk;
pub use disk::PrivateDatabase;
#[path = "account_state_fs.rs"]
mod fs;
#[path = "account_image.rs"]
pub(crate) mod image;
#[path = "account_execution.rs"]
pub mod execution;
#[path = "account_storage.rs"]
pub(crate) mod storage;
pub use storage::StorageRecord;

/// The baseline recovery reader accepts version 2 only. Candidate execution
/// must put this version at the top level of the SAME shared pending journal,
/// so baseline execution/maintenance cannot clear or bypass candidate recovery.
pub const JOURNAL_VERSION: u32 = 3;
pub const DATABASE_HEADER_VERSION: u32 = 1;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CandidateJournalIdentity {
    pub version: u32,
    pub binding: AccountBinding,
    pub container: String,
    pub package_sid: String,
}

impl CandidateJournalIdentity {
    /// Derivation does not create a profile, namespace, process or journal.
    pub fn new(binding: AccountBinding, container: String) -> Result<Self> {
        binding.validate()?;
        validate_container(&container)?;
        let package_sid = crate::appcontainer::derive(&container)?.string()?;
        Ok(Self {
            version: JOURNAL_VERSION,
            binding,
            container,
            package_sid,
        })
    }

    pub fn validate(&self, expected: &AccountBinding) -> Result<()> {
        expected.validate()?;
        self.binding.validate()?;
        ensure!(
            self.version == JOURNAL_VERSION,
            "unsupported candidate journal version"
        );
        ensure!(
            self.binding == *expected,
            "candidate journal/setup identity mismatch"
        );
        validate_container(&self.container)?;
        ensure!(
            crate::appcontainer::derive(&self.container)?.string()? == self.package_sid,
            "candidate journal package SID does not match its execution name"
        );
        Ok(())
    }
}

fn validate_container(container: &str) -> Result<()> {
    let suffix = container
        .strip_prefix("Alysis.Execution.")
        .context("invalid candidate execution name")?;
    validate_random_id(suffix)?;
    Ok(())
}

/// Shared with baseline execution/maintenance. Never use a candidate-specific
/// pending filename or an independent execution mutex.
pub fn pending_journal_path() -> Result<PathBuf> {
    Ok(crate::state_db::session_store_dir()?.join("execution-pending.json"))
}

/// Returns a path only; does not create or open directories/files. Production
/// PrivateDatabase resolves the OS ProgramFilesX64 KnownFolder itself; this helper
/// is not authority to supply arbitrary storage to the protected opener.
pub fn database_path(program_files: &Path, binding: &AccountBinding) -> Result<PathBuf> {
    binding.validate()?;
    ensure!(
        program_files.is_absolute(),
        "candidate ProgramFiles directory must be absolute"
    );
    Ok(program_files
        .join(format!("AlysisSandboxState-{}", binding.installation_id))
        .join(binding.mode.directory())
        .join("state.db"))
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct DatabaseHeader {
    pub version: u32,
    pub binding: AccountBinding,
}

impl DatabaseHeader {
    pub fn new(binding: AccountBinding) -> Result<Self> {
        binding.validate()?;
        Ok(Self {
            version: DATABASE_HEADER_VERSION,
            binding,
        })
    }

    pub fn validate(&self, expected: &AccountBinding) -> Result<()> {
        expected.validate()?;
        self.binding.validate()?;
        ensure!(
            self.version == DATABASE_HEADER_VERSION,
            "unsupported candidate database header version"
        );
        ensure!(
            self.binding == *expected,
            "candidate database/setup identity mismatch"
        );
        Ok(())
    }
}

const HEADER_SCHEMA: &str = r#"
CREATE TABLE account_identity (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    identity_json TEXT NOT NULL
);
CREATE TRIGGER account_identity_no_update BEFORE UPDATE ON account_identity
    BEGIN SELECT RAISE(ABORT, 'candidate identity header is immutable'); END;
CREATE TRIGGER account_identity_no_delete BEFORE DELETE ON account_identity
    BEGIN SELECT RAISE(ABORT, 'candidate identity header is immutable'); END;
CREATE TRIGGER account_identity_no_replace BEFORE INSERT ON account_identity
    WHEN EXISTS (SELECT 1 FROM account_identity)
    BEGIN SELECT RAISE(ABORT, 'candidate identity header is immutable'); END;
"#;

/// Initialize only a pristine connection. This never upgrades/adopts a legacy
/// database. Triggers guard accidental header rewrites; the surrounding owner-
/// only filesystem DACL, not SQLite, is the boundary against untrusted writers.
pub fn initialize_header(connection: &mut Connection, binding: &AccountBinding) -> Result<()> {
    let header = DatabaseHeader::new(binding.clone())?;
    let transaction =
        connection.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    let existing: i64 =
        transaction.query_row("SELECT count(*) FROM sqlite_master", [], |row| row.get(0))?;
    let version: i64 = transaction.query_row("PRAGMA user_version", [], |row| row.get(0))?;
    ensure!(
        existing == 0 && version == 0,
        "refusing to adopt an existing database as candidate state"
    );
    transaction.execute_batch(HEADER_SCHEMA)?;
    transaction.execute(
        "INSERT INTO account_identity(singleton, identity_json) VALUES(1, ?1)",
        params![serde_json::to_string(&header)?],
    )?;
    transaction.commit()?;
    validate_header(connection, binding)
}

/// Must precede all candidate ACL reads, writes and recovery. Missing, legacy,
/// malformed or differently owned state stays pending; it is never rewritten.
pub fn validate_header(connection: &Connection, expected: &AccountBinding) -> Result<()> {
    expected.validate()?;
    let (count, maximum): (i64, Option<i64>) = connection
        .query_row(
            "SELECT count(*), max(length(identity_json)) FROM account_identity",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .context("candidate database identity header is missing")?;
    ensure!(
        count == 1 && maximum.is_some_and(|size| size <= 16 * 1024),
        "candidate database identity header is incomplete or oversized"
    );
    let json: String = connection.query_row(
        "SELECT identity_json FROM account_identity WHERE singleton = 1",
        [],
        |row| row.get(0),
    )?;
    let header: DatabaseHeader =
        serde_json::from_str(&json).context("invalid candidate database identity header")?;
    header.validate(expected)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::account_install::{AccountMode, synthetic_ready};

    #[test]
    fn candidate_journal_binds_all_identities_and_package_derivation() {
        let record = synthetic_ready();
        let binding = record.binding(AccountMode::Offline).unwrap();
        let journal = CandidateJournalIdentity::new(
            binding.clone(),
            "Alysis.Execution.0123456789abcdef0123456789abcdef".into(),
        )
        .unwrap();
        journal.validate(&binding).unwrap();
        assert!(
            journal
                .validate(&record.binding(AccountMode::Online).unwrap())
                .is_err()
        );
        let mut altered = journal.clone();
        altered.version = 2;
        assert!(altered.validate(&binding).is_err());
        let mut altered = journal.clone();
        altered.package_sid.push_str("-1");
        assert!(altered.validate(&binding).is_err());
        let mut altered = journal.clone();
        altered.container = "Global\\other".into();
        assert!(altered.validate(&binding).is_err());
        let mut altered = journal.clone();
        altered.binding.owner_sid = "S-1-5-21-1-2-3-1999".into();
        assert!(altered.validate(&binding).is_err());
        assert!(CandidateJournalIdentity::new(binding, "Alysis.Execution.1234".into()).is_err());
        let serialized = serde_json::to_value(&journal).unwrap();
        assert_eq!(serialized["version"], 3);
    }

    #[test]
    fn candidate_paths_share_pending_journal_but_separate_databases() {
        let record = synthetic_ready();
        let offline = record.binding(AccountMode::Offline).unwrap();
        let online = record.binding(AccountMode::Online).unwrap();
        let root = Path::new(r"C:\synthetic\sandbox");
        let path = database_path(root, &offline).unwrap();
        assert_ne!(path, root.join("state.db"));
        assert_ne!(path, database_path(root, &online).unwrap());
        assert!(
            path.starts_with(root.join(format!("AlysisSandboxState-{}", offline.installation_id)))
        );
        assert!(database_path(Path::new("relative"), &offline).is_err());
        let mut invalid = offline;
        invalid.installation_id = "..\\escape".into();
        assert!(database_path(root, &invalid).is_err());
        assert_eq!(
            pending_journal_path().unwrap(),
            crate::state_db::session_store_dir()
                .unwrap()
                .join("execution-pending.json")
        );
    }

    #[test]
    fn candidate_database_header_cannot_be_rebound_or_adopt_legacy_rows() {
        let record = synthetic_ready();
        let binding = record.binding(AccountMode::Offline).unwrap();
        let mut connection = Connection::open_in_memory().unwrap();
        initialize_header(&mut connection, &binding).unwrap();
        validate_header(&connection, &binding).unwrap();
        assert!(
            validate_header(&connection, &record.binding(AccountMode::Online).unwrap()).is_err()
        );
        for field in ["installation", "owner", "account", "group"] {
            let mut foreign = binding.clone();
            match field {
                "installation" => {
                    foreign.installation_id = "1123456789abcdef0123456789abcdef".into()
                }
                "owner" => foreign.owner_sid = "S-1-5-21-1-2-3-1999".into(),
                "account" => foreign.account_sid = "S-1-5-21-1-2-3-1999".into(),
                _ => foreign.group_sid = "S-1-5-21-1-2-3-1999".into(),
            }
            assert!(validate_header(&connection, &foreign).is_err());
        }
        for sql in [
            "DELETE FROM account_identity",
            "UPDATE account_identity SET identity_json = '{}'",
            "INSERT OR REPLACE INTO account_identity VALUES(1, '{}')",
        ] {
            assert!(connection.execute_batch(sql).is_err());
        }
        validate_header(&connection, &binding).unwrap();
        assert!(initialize_header(&mut connection, &binding).is_err());
        let mut legacy = Connection::open_in_memory().unwrap();
        legacy
            .execute_batch("CREATE TABLE working_aces(path TEXT)")
            .unwrap();
        assert!(initialize_header(&mut legacy, &binding).is_err());
        assert!(validate_header(&legacy, &binding).is_err());
        assert_eq!(
            legacy
                .query_row(
                    "SELECT count(*) FROM sqlite_master WHERE type='table'",
                    [],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            1
        );
    }
}
