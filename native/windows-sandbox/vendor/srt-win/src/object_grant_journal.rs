//! Candidate database integration for explicit-object read grants. Rows do not
//! enter the baseline root-only working_aces table. Typed authority from the
//! shared pending-file gate precedes every grant/recovery. Rows bind the complete
//! immutable execution record, including the actual database and marker IDs.

use super::*;
use crate::account_state::{
    PrivateDatabase,
    execution::{ActiveExecution, ExecutionRecord, RecoveredReadTree, StoppedExecution},
};
use rusqlite::{Connection, OptionalExtension, params};

const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS candidate_read_runs (
    container TEXT PRIMARY KEY,
    identity_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_read_objects (
    container TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    file_id BLOB NOT NULL CHECK(length(file_id) = 24),
    record_json TEXT NOT NULL,
    PRIMARY KEY(container, ordinal),
    UNIQUE(container, file_id)
);
"#;

/// Borrowed protected candidate state. Production construction requires the
/// private opener and the shared lease; fixtures alone may use an in-memory
/// database. Trusted candidate SQL/ordinary RAII requirements are inherited
/// from PrivateDatabase. Opening does not adopt any baseline recovery rows.
pub struct ReadTreeJournal<'a> {
    connection: &'a Connection,
    pub(super) identity: CandidateJournalIdentity,
    authority: Authority<'a>,
}

enum Authority<'a> {
    Live(&'a ActiveExecution<'a>),
    Stopped(&'a StoppedExecution<'a>),
    #[cfg(test)]
    Fixture,
}

impl<'a> ReadTreeJournal<'a> {
    pub fn open(
        database: &'a PrivateDatabase<'_>,
        execution: &'a ActiveExecution<'_>,
    ) -> Result<Self> {
        Self::open_authorized(database, Authority::Live(execution))
    }

    pub fn open_stopped(
        database: &'a PrivateDatabase<'_>,
        execution: &'a StoppedExecution<'_>,
    ) -> Result<Self> {
        execution.acl_recovery_record(None)?;
        Self::open_authorized(database, Authority::Stopped(execution))
    }

    #[allow(dead_code)]
    pub(crate) fn open_after_resources(
        database: &'a PrivateDatabase<'_>,
        execution: &'a StoppedExecution<'_>,
        resources: &crate::account_state::execution::resources::RecoveredResources,
    ) -> Result<Self> {
        execution.acl_recovery_record(Some(resources))?;
        Self::open_authorized(database, Authority::Stopped(execution))
    }

    fn open_authorized(
        database: &'a PrivateDatabase<'_>,
        authority: Authority<'a>,
    ) -> Result<Self> {
        let record = match &authority {
            Authority::Live(value) => value.acl_record()?,
            Authority::Stopped(value) => value.record()?,
            #[cfg(test)]
            Authority::Fixture => unreachable!(),
        };
        record.validate(&record.identity.binding, database.file_identity())?;
        let journal = Self {
            connection: database.connection()?,
            identity: record.identity.clone(),
            authority,
        };
        journal.validate()?;
        journal.connection.execute_batch(SCHEMA)?;
        Ok(journal)
    }

    fn validate(&self) -> Result<()> {
        self.identity.validate(&self.identity.binding)?;
        crate::account_state::validate_header(self.connection, &self.identity.binding)?;
        ensure!(
            self.connection.is_autocommit(),
            "object journal cannot share an open SQL transaction"
        );
        self.execution_record()?;
        let mode: String = self
            .connection
            .query_row("PRAGMA journal_mode", [], |r| r.get(0))?;
        let sync: i64 = self
            .connection
            .query_row("PRAGMA synchronous", [], |r| r.get(0))?;
        #[cfg(test)]
        if matches!(self.authority, Authority::Fixture) {
            return Ok(());
        }
        ensure!(
            mode == "delete" && sync == 2,
            "object journal requires DELETE/FULL durability"
        );
        Ok(())
    }

    fn execution_record(&self) -> Result<Option<&ExecutionRecord>> {
        match &self.authority {
            Authority::Live(value) => Ok(Some(value.acl_record()?)),
            Authority::Stopped(value) => Ok(Some(value.record()?)),
            #[cfg(test)]
            Authority::Fixture => Ok(None),
        }
    }

    fn identity_json(&self) -> Result<String> {
        if let Some(record) = self.execution_record()? {
            return Ok(serde_json::to_string(record)?);
        }
        // This branch is reachable only by cfg(test) in-memory mechanics fixtures.
        Ok(serde_json::to_string(&self.identity)?)
    }

    pub(super) fn record(&self, objects: &[(Pinned, Record)]) -> Result<()> {
        self.validate()?;
        ensure!(
            !matches!(self.authority, Authority::Stopped(_)),
            "stopped execution cannot acquire new grants"
        );
        ensure!(
            !objects.is_empty() && objects.len() <= MAX_OBJECTS,
            "invalid read-tree size"
        );
        let transaction = self.connection.unchecked_transaction()?;
        let count: i64 =
            transaction.query_row("SELECT count(*) FROM candidate_read_runs", [], |r| r.get(0))?;
        let rows: i64 =
            transaction.query_row("SELECT count(*) FROM candidate_read_objects", [], |r| {
                r.get(0)
            })?;
        ensure!(
            count == 0 && rows == 0,
            "pending explicit-object grants must be recovered before a new grant"
        );
        transaction.execute(
            "INSERT INTO candidate_read_runs VALUES(?1, ?2)",
            params![self.identity.container, self.identity_json()?],
        )?;
        let mut total = 0;
        for (ordinal, (_, record)) in objects.iter().enumerate() {
            record.validate(&self.identity)?;
            let json = serde_json::to_string(record)?;
            total += json.len();
            ensure!(
                json.len() <= MAX_RECORD_BYTES && total <= MAX_TREE_BYTES,
                "object journal exceeds bounded size"
            );
            transaction.execute(
                "INSERT INTO candidate_read_objects VALUES(?1, ?2, ?3, ?4)",
                params![
                    self.identity.container,
                    ordinal as i64,
                    record.file_id.as_slice(),
                    json
                ],
            )?;
        }
        transaction
            .commit()
            .context("persist object grant intent before ACL mutation")
    }

    fn records(&self) -> Result<Vec<(i64, Record)>> {
        self.validate()?;
        let (count, bytes): (i64, Option<i64>) = self.connection.query_row(
            "SELECT count(*), sum(length(record_json)) FROM candidate_read_objects",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        ensure!(
            (0..=MAX_OBJECTS as i64).contains(&count)
                && bytes.unwrap_or(0) <= MAX_TREE_BYTES as i64,
            "oversized object grant journal"
        );
        let runs: i64 =
            self.connection
                .query_row("SELECT count(*) FROM candidate_read_runs", [], |r| r.get(0))?;
        if runs == 0 {
            ensure!(
                count == 0,
                "object grant records lack their execution identity"
            );
            return Ok(Vec::new());
        }
        ensure!(runs == 1, "ambiguous pending object grant executions");
        let json: Option<String> = self.connection.query_row(
            "SELECT identity_json FROM candidate_read_runs WHERE container = ?1 AND length(identity_json) <= 16384",
            [&self.identity.container], |r| r.get(0)).optional()?;
        let json = json.context("missing/oversized object execution identity")?;
        if let Some(expected) = self.execution_record()? {
            let stored: ExecutionRecord = serde_json::from_str(&json)?;
            stored.validate(&self.identity.binding, expected.database_id)?;
            ensure!(
                stored == *expected,
                "object journal execution identity mismatch"
            );
        } else {
            let stored: CandidateJournalIdentity = serde_json::from_str(&json)?;
            stored.validate(&self.identity.binding)?;
            ensure!(
                stored == self.identity,
                "object journal execution identity mismatch"
            );
        }
        let mut query = self.connection.prepare(
            "SELECT ordinal, file_id, record_json FROM candidate_read_objects WHERE container = ?1 ORDER BY ordinal DESC")?;
        let rows = query.query_map([&self.identity.container], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, Vec<u8>>(1)?,
                r.get::<_, String>(2)?,
            ))
        })?;
        let mut result = Vec::new();
        let mut identities = HashSet::new();
        for row in rows {
            let (ordinal, file_id, json) = row?;
            ensure!(
                (0..MAX_OBJECTS as i64).contains(&ordinal) && json.len() <= MAX_RECORD_BYTES,
                "invalid object journal row"
            );
            let record: Record =
                serde_json::from_str(&json).context("malformed object grant record")?;
            record.validate(&self.identity)?;
            ensure!(
                file_id == record.file_id && identities.insert(record.file_id),
                "duplicate or mismatched object journal identity"
            );
            result.push((ordinal, record));
        }
        ensure!(
            result.len() == count as usize,
            "object journal contains a foreign execution row"
        );
        Ok(result)
    }

    /// Remove grants only with typed stopped-execution authority. Live authority
    /// cannot recover, and stopped authority cannot record new grants. The
    /// returned opaque receipt permits exact-handle pending-marker retirement.
    ///
    /// Rows retire one exact identity at a time, after verified readback (or a
    /// definitive checked-ID absence). Missing roots never retire descendants.
    /// Error 87, ACL drift, unavailable volumes and malformed state are errors;
    /// no guessed cleanup or pending-journal removal is performed.
    pub fn recover(&self) -> Result<RecoveredReadTree> {
        self.validate()?;
        ensure!(
            !matches!(self.authority, Authority::Live(_)),
            "live execution cannot revoke grants"
        );
        crate::job::wait_for_execution_exit(&self.identity.container)?;
        self.recover_after_exit()?;
        let record = match self.execution_record()? {
            Some(record) => record.clone(),
            #[cfg(test)]
            None => crate::account_state::execution::fixture_record(self.identity.clone()),
            #[cfg(not(test))]
            None => unreachable!(),
        };
        Ok(RecoveredReadTree::new(record))
    }

    fn recover_after_exit(&self) -> Result<()> {
        // Admit all rows before the first ACL mutation. A malformed later row
        // cannot silently become an unrelated target after partial cleanup.
        for (ordinal, record) in self.records()? {
            let expected = FileId::from_bytes(&record.file_id)?;
            let target = match Pinned::open(&record.path, Some(expected), false) {
                Ok(target) => Some(target),
                Err(original) => match crate::path_id::locate_by_file_id_checked(&expected)
                    .context(
                        "cannot prove recorded object identity; keep per-object recovery row",
                    )? {
                    Some(path) => {
                        Some(Pinned::open(&path, Some(expected), false).with_context(|| {
                            format!(
                                "recorded object could not be pinned at either name: {original:#}"
                            )
                        })?)
                    }
                    None => None,
                },
            };
            if let Some(target) = target {
                ensure!(
                    target.directory == record.directory,
                    "recorded object kind mismatch"
                );
                let current = Snapshot::read(target.handle.raw())?;
                if current != record.before {
                    target.write(&record.after(), &record.before, false)?;
                }
            }
            self.validate()?;
            ensure!(self.connection.execute(
                "DELETE FROM candidate_read_objects WHERE container = ?1 AND ordinal = ?2 AND file_id = ?3",
                params![self.identity.container, ordinal, record.file_id.as_slice()])? == 1,
                "object journal changed during cleanup");
        }
        self.validate()?;
        ensure!(
            self.connection
                .query_row("SELECT count(*) FROM candidate_read_objects", [], |r| r
                    .get::<_, i64>(0))?
                == 0,
            "object recovery rows remain"
        );
        self.connection.execute(
            "DELETE FROM candidate_read_runs WHERE container = ?1",
            [&self.identity.container],
        )?;
        Ok(())
    }

    #[cfg(test)]
    pub(crate) fn fixture_active(
        connection: &'a Connection,
        execution: &'a ActiveExecution<'_>,
    ) -> Result<Self> {
        let journal = Self {
            connection,
            identity: execution.identity().clone(),
            authority: Authority::Live(execution),
        };
        journal.validate()?;
        connection.execute_batch(SCHEMA)?;
        Ok(journal)
    }

    #[cfg(test)]
    pub(crate) fn fixture_stopped(
        connection: &'a Connection,
        execution: &'a StoppedExecution<'_>,
    ) -> Result<Self> {
        let journal = Self {
            connection,
            identity: execution.record()?.identity.clone(),
            authority: Authority::Stopped(execution),
        };
        journal.validate()?;
        connection.execute_batch(SCHEMA)?;
        Ok(journal)
    }

    #[cfg(test)]
    pub(super) fn fixture(
        connection: &'a Connection,
        identity: &CandidateJournalIdentity,
    ) -> Result<Self> {
        let journal = Self {
            connection,
            identity: identity.clone(),
            authority: Authority::Fixture,
        };
        journal.validate()?;
        connection.execute_batch(SCHEMA)?;
        Ok(journal)
    }
}
