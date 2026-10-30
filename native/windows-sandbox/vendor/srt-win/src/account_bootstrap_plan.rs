//! Immutable write-ahead identity for the new atomic host-bootstrap layout.
//! Identity preparation commits no native birth, grants no resume/cleanup authority, and has no
//! cold-to-live reconstruction API. Legacy fourteen-role/ACL receipts cannot
//! retire this distinct marker scope. The original guarded coordinator can
//! record trusted-bootstrap startup; production begin remains gated.
#![allow(dead_code)]
use super::*;
use crate::{account_control::ControlAddress, account_state::image::PinnedRunnerImage};
use rusqlite::{Connection, OptionalExtension, params};

const FORMAT: &str = "alysis-atomic-bootstrap-identity-v1";
const MAX_BYTES: usize = 16384;
#[path = "account_bootstrap_desktop.rs"]
pub(crate) mod desktop;
#[path = "account_bootstrap_desktop_native.rs"]
pub(in crate::account_state::execution) mod desktop_native;
#[path = "account_bootstrap_host_profile.rs"]
pub(crate) mod host_profile;
#[path = "account_bootstrap_initializer_image.rs"]
pub(crate) mod initializer_image;
#[cfg(test)]
pub(crate) use initializer_image::OMIT_PROFILE_ENVIRONMENT as INITIALIZER_OMIT_PROFILE_ENVIRONMENT;
#[path = "account_bootstrap_lifecycle.rs"]
pub(crate) mod lifecycle;
#[path = "account_bootstrap_namespace.rs"]
pub(crate) mod namespace;
#[path = "account_bootstrap_station.rs"]
pub(crate) mod station;
const TABLE: &str = "candidate_atomic_bootstrap_plan";
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_atomic_bootstrap_plan(singleton INTEGER PRIMARY KEY CHECK(singleton=1), plan_json TEXT NOT NULL)",
    "CREATE TRIGGER atomic_plan_no_update BEFORE UPDATE ON candidate_atomic_bootstrap_plan BEGIN SELECT RAISE(ABORT,'atomic bootstrap identity is immutable'); END",
    "CREATE TRIGGER atomic_plan_no_delete BEFORE DELETE ON candidate_atomic_bootstrap_plan BEGIN SELECT RAISE(ABORT,'atomic bootstrap identity is immutable'); END",
    "CREATE TRIGGER atomic_plan_no_replace BEFORE INSERT ON candidate_atomic_bootstrap_plan WHEN EXISTS(SELECT 1 FROM candidate_atomic_bootstrap_plan) BEGIN SELECT RAISE(ABORT,'atomic bootstrap identity is immutable'); END",
];

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Plan {
    format: String,
    execution: ExecutionRecord,
    operation: String,
    credential_generation: String,
    image_id: [u8; 24],
    session: u32,
    control_nonce: [u8; 16],
}

impl Plan {
    fn validate(&self) -> Result<()> {
        self.execution
            .validate(&self.execution.identity.binding, self.execution.database_id)?;
        ensure!(
            self.format == FORMAT && self.execution.scope == BOOTSTRAP_PLAN_SCOPE,
            "unsupported atomic bootstrap identity/scope"
        );
        validate_random_id(&self.operation)?;
        ensure!(
            self.image_id != [0; 24] && self.control_nonce != [0; 16],
            "missing pinned image/control identity"
        );
        let binding = &self.execution.identity.binding;
        crate::account_credentials::generation_path(
            &binding.installation_id,
            binding.mode,
            &self.credential_generation,
        )?;
        Ok(())
    }
}

/// Retains live execution/lease and original image pins. Deliberately not
/// serializable, Clone, a process receipt, or an executable/cleanup capability.
pub(crate) struct CommittedPlan<'a, 'lease> {
    database: &'a PrivateDatabase<'lease>,
    execution: &'a ActiveExecution<'lease>,
    image: &'a PinnedRunnerImage,
    ready: &'a ReadyInstallation,
    address: &'a ControlAddress,
    plan: Plan,
    job_attempted: std::cell::Cell<bool>,
}

impl<'a, 'lease> CommittedPlan<'a, 'lease> {
    pub(crate) fn prepare(
        database: &'a PrivateDatabase<'lease>,
        execution: &'a ActiveExecution<'lease>,
        image: &'a PinnedRunnerImage,
        ready: &'a ReadyInstallation,
        address: &'a ControlAddress,
    ) -> Result<Self> {
        let record = execution.record()?;
        ensure!(
            record.scope == BOOTSTRAP_PLAN_SCOPE,
            "legacy execution cannot prepare atomic bootstrap identity"
        );
        record.validate(
            &ready.binding(record.identity.binding.mode)?,
            database.file_identity(),
        )?;
        image.require_installation(ready)?;
        let mut session = 0;
        unsafe {
            windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
                std::process::id(),
                &mut session,
            )?;
        }
        let generation = ready
            .record
            .account(record.identity.binding.mode)?
            .credential_generation
            .clone()
            .context("atomic bootstrap requires selected credential generation")?;
        let plan = Plan {
            format: FORMAT.into(),
            execution: record.clone(),
            operation: random_id()?,
            credential_generation: generation,
            image_id: image.file_identity()?,
            session,
            control_nonce: address.binding_nonce()?,
        };
        plan.validate()?;
        let connection = database.connection()?;
        durability(connection)?;
        persist(connection, &plan)?;
        let bound = Self {
            database,
            execution,
            image,
            ready,
            address,
            plan,
            job_attempted: std::cell::Cell::new(false),
        };
        bound.verify()?;
        Ok(bound)
    }

    /// Readback cannot recreate this object after process death; only the
    /// original borrowed guards and exact operation can verify a live plan.
    pub(crate) fn verify(&self) -> Result<()> {
        let ready = self.ready;
        let address = self.address;
        self.image.require_installation(ready)?;
        let record = self.execution.record()?;
        record.validate(
            &ready.binding(record.identity.binding.mode)?,
            self.database.file_identity(),
        )?;
        let mut session = 0;
        unsafe {
            windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
                std::process::id(),
                &mut session,
            )?;
        }
        ensure!(
            *record == self.plan.execution
                && session == self.plan.session
                && self.image.file_identity()? == self.plan.image_id
                && address.binding_nonce()? == self.plan.control_nonce
                && ready
                    .record
                    .account(record.identity.binding.mode)?
                    .credential_generation
                    .as_deref()
                    == Some(self.plan.credential_generation.as_str()),
            "live atomic bootstrap binding changed"
        );
        let connection = self.database.connection()?;
        durability(connection)?;
        require_plan(connection, &self.plan)
    }
}

fn durability(connection: &Connection) -> Result<()> {
    ensure!(
        connection.is_autocommit(),
        "atomic bootstrap plan cannot join another transaction"
    );
    let mode: String = connection.query_row("PRAGMA journal_mode", [], |r| r.get(0))?;
    let sync: i64 = connection.query_row("PRAGMA synchronous", [], |r| r.get(0))?;
    ensure!(
        mode == "delete" && sync == 2,
        "atomic bootstrap plan requires DELETE/FULL durability"
    );
    Ok(())
}

fn persist(connection: &Connection, plan: &Plan) -> Result<()> {
    plan.validate()?;
    durability(connection)?;
    crate::account_state::validate_header(connection, &plan.execution.identity.binding)?;
    let encoded = serde_json::to_string(plan)?;
    ensure!(
        encoded.len() <= MAX_BYTES,
        "atomic bootstrap identity exceeds bounds"
    );
    let transaction = connection.unchecked_transaction()?;
    // No IF NOT EXISTS: even an empty pre-existing table is ambiguous state,
    // not permission to retry a preparation whose commit outcome was lost.
    for statement in SCHEMA {
        transaction.execute_batch(statement)?;
    }
    lifecycle::create_schema(&transaction)?;
    namespace::create_schema(&transaction)?;
    station::create_schema(&transaction)?;
    desktop::create_schema(&transaction)?;
    host_profile::create_schema(&transaction)?;
    lifecycle::initializer::create_schema(&transaction)?;
    transaction.execute(
        "INSERT INTO candidate_atomic_bootstrap_plan VALUES(1,?1)",
        params![encoded],
    )?;
    transaction
        .commit()
        .context("commit atomic bootstrap identity; preserve ambiguous outcome")?;
    require_plan(connection, plan)
}

fn require_plan(connection: &Connection, expected: &Plan) -> Result<()> {
    expected.validate()?;
    durability(connection)?;
    crate::account_state::validate_header(connection, &expected.execution.identity.binding)?;
    require_schema(connection)?;
    lifecycle::require_schema(connection)?;
    namespace::require_schema(connection)?;
    station::require_schema(connection)?;
    desktop::require_schema(connection)?;
    host_profile::require_schema(connection)?;
    lifecycle::initializer::require_schema(connection)?;
    let count: u64 = connection.query_row(
        "SELECT count(*) FROM candidate_atomic_bootstrap_plan",
        [],
        |r| r.get(0),
    )?;
    ensure!(
        count == 1,
        "atomic bootstrap inventory is absent or ambiguous"
    );
    let json: Option<String> = connection.query_row(
        "SELECT plan_json FROM candidate_atomic_bootstrap_plan WHERE singleton=1 AND typeof(plan_json)='text' AND length(CAST(plan_json AS BLOB))<=?1",
        [MAX_BYTES as u64], |r| r.get(0)).optional()?;
    let observed: Plan =
        serde_json::from_str(&json.context("atomic bootstrap identity missing/oversized")?)
            .context("atomic bootstrap identity malformed; preserve record")?;
    observed.validate()?;
    ensure!(
        observed == *expected,
        "atomic bootstrap operation/execution binding differs"
    );
    Ok(())
}

fn require_schema(connection: &Connection) -> Result<()> {
    let mut statement = connection.prepare("SELECT sql FROM sqlite_schema WHERE tbl_name=?1")?;
    let observed: Vec<String> = statement
        .query_map([TABLE], |r| r.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    ensure!(
        observed.len() == SCHEMA.len()
            && SCHEMA
                .iter()
                .all(|sql| observed.iter().any(|row| row == sql)),
        "atomic bootstrap schema differs; preserve ambiguous state"
    );
    Ok(())
}

#[cfg(test)]
#[path = "account_bootstrap_plan_tests.rs"]
mod tests;
