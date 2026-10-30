//! Immutable identity and bounded evidence for two original sibling jobs.
//! Journal data never reconstructs native ownership, resume or cleanup authority.
//! The owner cannot retire until the consumers' complete retirement is recorded.
#![allow(dead_code)]
use super::resources::namespace_native::RootBirthKind;
use super::resources::namespace_native::rpc::PolicyKind;
use super::*;
use crate::{account_control::ControlAddress, account_state::image::PinnedRunnerImage};
use rusqlite::{Connection, OptionalExtension, params};
use std::cell::Cell;

#[path = "account_paired_native.rs"]
pub(crate) mod native;

#[path = "account_paired_account_journal.rs"]
pub(super) mod account_journal;

#[cfg(test)]
pub(crate) use account_journal::profile_journal::PROFILE_JOURNAL_CHECKPOINT;
#[cfg(test)]
pub(crate) use account_journal::resume_journal::RESUME_JOURNAL_CHECKPOINT;

const FORMAT: &str = "alysis-paired-bootstrap-identity-v1";
const MAX_BYTES: usize = 16384;
const MAX_EVENT_BYTES: usize = 1024;
const TABLE: &str = "candidate_paired_bootstrap_plan";
const EVENTS_TABLE: &str = "candidate_paired_bootstrap_events";
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_paired_bootstrap_plan(singleton INTEGER PRIMARY KEY CHECK(singleton=1), plan_json TEXT NOT NULL)",
    "CREATE TRIGGER paired_plan_no_update BEFORE UPDATE ON candidate_paired_bootstrap_plan BEGIN SELECT RAISE(ABORT,'paired bootstrap identity is immutable'); END",
    "CREATE TRIGGER paired_plan_no_delete BEFORE DELETE ON candidate_paired_bootstrap_plan BEGIN SELECT RAISE(ABORT,'paired bootstrap identity is immutable'); END",
    "CREATE TRIGGER paired_plan_no_replace BEFORE INSERT ON candidate_paired_bootstrap_plan WHEN EXISTS(SELECT 1 FROM candidate_paired_bootstrap_plan WHERE singleton=NEW.singleton) BEGIN SELECT RAISE(ABORT,'paired bootstrap identity is immutable'); END",
];
const EVENT_SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_paired_bootstrap_events(role INTEGER NOT NULL CHECK(role IN (0,1)), sequence INTEGER NOT NULL CHECK(sequence BETWEEN 0 AND 8), event_json TEXT NOT NULL, PRIMARY KEY(role,sequence)) WITHOUT ROWID",
    "CREATE TRIGGER paired_events_no_update BEFORE UPDATE ON candidate_paired_bootstrap_events BEGIN SELECT RAISE(ABORT,'paired bootstrap event is immutable'); END",
    "CREATE TRIGGER paired_events_no_delete BEFORE DELETE ON candidate_paired_bootstrap_events BEGIN SELECT RAISE(ABORT,'paired bootstrap event is immutable'); END",
    "CREATE TRIGGER paired_events_no_replace BEFORE INSERT ON candidate_paired_bootstrap_events WHEN EXISTS(SELECT 1 FROM candidate_paired_bootstrap_events WHERE role=NEW.role AND sequence=NEW.sequence) BEGIN SELECT RAISE(ABORT,'paired bootstrap event is immutable'); END",
];

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Role {
    Owner,
    Consumers,
}
impl Role {
    pub(crate) const fn index(self) -> usize {
        match self {
            Self::Owner => 0,
            Self::Consumers => 1,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct RolePlan {
    role: Role,
    job_name: String,
    control_nonce: [u8; 16],
    job_nonce: String,
    birth_nonce: String,
    resume_nonce: String,
    retire_nonce: String,
}
impl RolePlan {
    fn new(container: &str, role: Role, address: &ControlAddress) -> Result<Self> {
        Ok(Self {
            role,
            job_name: crate::job::paired_execution_name(container, role == Role::Consumers)?,
            control_nonce: address.binding_nonce()?,
            job_nonce: random_id()?,
            birth_nonce: random_id()?,
            resume_nonce: random_id()?,
            retire_nonce: random_id()?,
        })
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Plan {
    format: String,
    execution: ExecutionRecord,
    operation: String,
    credential_generation: String,
    image_id: [u8; 24],
    session: u32,
    roles: [RolePlan; 2],
    // Omitted historical records retain BaselineV1. Every newer policy must
    // persist its own identity in all builds; decoding supplies no live rights.
    #[serde(default, skip_serializing_if = "PolicyKind::is_baseline")]
    rpc_policy: PolicyKind,
    #[serde(default, skip_serializing_if = "RootBirthKind::is_legacy")]
    namespace_birth: RootBirthKind,
}
impl Plan {
    #[cfg(test)]
    pub(in crate::account_state::execution) fn fixture_require_rpc_policy(
        &self,
        expected: PolicyKind,
    ) -> Result<()> {
        self.validate()?;
        ensure!(
            super::resources::namespace_native::NamespacePolicySource::rpc_policy(self) == expected,
            "reported RPC policy differs from independently selected fixture"
        );
        Ok(())
    }
    fn require_live_policy(&self) -> Result<()> {
        self.namespace_birth.require_live()?;
        if self.namespace_birth == RootBirthKind::SessionShadowDirectoryV1 {
            ensure!(
                self.rpc_policy == PolicyKind::SystemNsiReadInheritanceV3,
                "shadow lifecycle requires exact RPC V3"
            );
        }
        self.rpc_policy.require_live()?;
        Ok(())
    }
    #[cfg(test)]
    pub(in crate::account_state::execution) fn fixture_require_namespace_birth(
        &self,
        expected: RootBirthKind,
    ) -> Result<()> {
        self.validate()?;
        ensure!(
            self.namespace_birth == expected,
            "reported namespace constructor differs from independently selected fixture"
        );
        Ok(())
    }
    // Read-only native policy data; this is not a job or cleanup capability.
    pub(super) fn desktop_subject(&self) -> (&ExecutionRecord, &str, u32) {
        (&self.execution, &self.operation, self.session)
    }

    pub(super) fn validate(&self) -> Result<()> {
        self.execution
            .validate(&self.execution.identity.binding, self.execution.database_id)?;
        ensure!(
            self.format == FORMAT && self.execution.scope == PAIRED_PLAN_SCOPE,
            "unsupported paired bootstrap identity/scope"
        );
        validate_random_id(&self.operation)?;
        ensure!(
            self.image_id != [0; 24],
            "paired pinned image identity missing"
        );
        let binding = &self.execution.identity.binding;
        crate::account_credentials::generation_path(
            &binding.installation_id,
            binding.mode,
            &self.credential_generation,
        )?;
        // Normalize pipe nonces to the same representation as phase nonces so
        // cross-purpose collisions cannot hide behind their wire encoding.
        let mut nonces = vec![self.operation.clone(), self.execution.transaction.clone()];
        for (index, role) in self.roles.iter().enumerate() {
            ensure!(
                role.role.index() == index
                    && role.job_name
                        == crate::job::paired_execution_name(
                            &self.execution.identity.container,
                            role.role == Role::Consumers,
                        )?
                    && role.control_nonce != [0; 16],
                "paired role order, job identity or control address differs"
            );
            nonces.push(
                role.control_nonce
                    .iter()
                    .map(|b| format!("{b:02x}"))
                    .collect(),
            );
            for nonce in [
                &role.job_nonce,
                &role.birth_nonce,
                &role.resume_nonce,
                &role.retire_nonce,
            ] {
                validate_random_id(nonce)?;
                nonces.push(nonce.clone());
            }
        }
        ensure!(
            self.roles[0].job_name != self.roles[1].job_name,
            "paired original jobs must have distinct names"
        );
        for (index, nonce) in nonces.iter().enumerate() {
            ensure!(
                !nonces[..index].contains(nonce),
                "paired operation, address or phase nonce was reused"
            );
        }
        Ok(())
    }
}

/// Only the original live references create this guard. Parsed journal rows
/// provide diagnostic evidence; they cannot recreate this non-Clone object.
pub(crate) struct CommittedPlan<'a, 'lease> {
    database: &'a PrivateDatabase<'lease>,
    execution: &'a ActiveExecution<'lease>,
    image: &'a PinnedRunnerImage,
    ready: &'a ReadyInstallation,
    owner_address: &'a ControlAddress,
    consumer_address: &'a ControlAddress,
    plan: Plan,
    jobs_attempted: Cell<bool>,
}
impl<'a, 'lease> CommittedPlan<'a, 'lease> {
    pub(crate) fn prepare(
        database: &'a PrivateDatabase<'lease>,
        execution: &'a ActiveExecution<'lease>,
        image: &'a PinnedRunnerImage,
        ready: &'a ReadyInstallation,
        owner_address: &'a ControlAddress,
        consumer_address: &'a ControlAddress,
    ) -> Result<Self> {
        Self::prepare_inner(
            database,
            execution,
            image,
            ready,
            owner_address,
            consumer_address,
            PolicyKind::BaselineV1,
            RootBirthKind::LegacyUnspecified,
        )
    }
    #[cfg(test)]
    pub(in crate::account_state::execution) fn prepare_rpc_constructor_fixture(
        database: &'a PrivateDatabase<'lease>,
        execution: &'a ActiveExecution<'lease>,
        image: &'a PinnedRunnerImage,
        ready: &'a ReadyInstallation,
        owner_address: &'a ControlAddress,
        consumer_address: &'a ControlAddress,
    ) -> Result<Self> {
        Self::prepare_inner(
            database,
            execution,
            image,
            ready,
            owner_address,
            consumer_address,
            PolicyKind::KernelBaseAccountFullV1,
            RootBirthKind::LegacyUnspecified,
        )
    }
    #[cfg(test)]
    pub(in crate::account_state::execution) fn prepare_rpc_system_inheritance_fixture(
        database: &'a PrivateDatabase<'lease>,
        execution: &'a ActiveExecution<'lease>,
        image: &'a PinnedRunnerImage,
        ready: &'a ReadyInstallation,
        owner_address: &'a ControlAddress,
        consumer_address: &'a ControlAddress,
    ) -> Result<Self> {
        Self::prepare_inner(
            database,
            execution,
            image,
            ready,
            owner_address,
            consumer_address,
            PolicyKind::SystemInheritanceV1,
            RootBirthKind::LegacyUnspecified,
        )
    }
    #[cfg(test)]
    pub(in crate::account_state::execution) fn prepare_rpc_nsi_system_fixture(
        database: &'a PrivateDatabase<'lease>,
        execution: &'a ActiveExecution<'lease>,
        image: &'a PinnedRunnerImage,
        ready: &'a ReadyInstallation,
        owner_address: &'a ControlAddress,
        consumer_address: &'a ControlAddress,
    ) -> Result<Self> {
        Self::prepare_inner(
            database,
            execution,
            image,
            ready,
            owner_address,
            consumer_address,
            PolicyKind::NsiSystemInheritanceV1,
            RootBirthKind::LegacyUnspecified,
        )
    }
    #[cfg(test)]
    pub(in crate::account_state::execution) fn prepare_rpc_v3_lifecycle_fixture(
        database: &'a PrivateDatabase<'lease>,
        execution: &'a ActiveExecution<'lease>,
        image: &'a PinnedRunnerImage,
        ready: &'a ReadyInstallation,
        owner_address: &'a ControlAddress,
        consumer_address: &'a ControlAddress,
    ) -> Result<Self> {
        crate::account_rpc_v3_lifecycle_fixture::require_ordinary_live()?;
        Self::prepare_inner(
            database,
            execution,
            image,
            ready,
            owner_address,
            consumer_address,
            PolicyKind::SystemNsiReadInheritanceV3,
            RootBirthKind::LegacyUnspecified,
        )
    }
    #[cfg(test)]
    pub(in crate::account_state::execution) fn prepare_rpc_v3_shadow_lifecycle_fixture(
        database: &'a PrivateDatabase<'lease>,
        execution: &'a ActiveExecution<'lease>,
        image: &'a PinnedRunnerImage,
        ready: &'a ReadyInstallation,
        owner_address: &'a ControlAddress,
        consumer_address: &'a ControlAddress,
    ) -> Result<Self> {
        crate::account_rpc_v3_lifecycle_fixture::require_shadow_live()?;
        Self::prepare_inner(
            database,
            execution,
            image,
            ready,
            owner_address,
            consumer_address,
            PolicyKind::SystemNsiReadInheritanceV3,
            RootBirthKind::SessionShadowDirectoryV1,
        )
    }
    #[cfg(test)]
    pub(in crate::account_state::execution) fn prepare_rpc_v3_contained_offline_fixture(
        database: &'a PrivateDatabase<'lease>,
        execution: &'a ActiveExecution<'lease>,
        image: &'a PinnedRunnerImage,
        ready: &'a ReadyInstallation,
        owner_address: &'a ControlAddress,
        consumer_address: &'a ControlAddress,
    ) -> Result<Self> {
        crate::account_rpc_v3_lifecycle_fixture::require_contained_offline_live()?;
        ensure!(
            execution.record()?.identity.binding.mode
                == crate::account_install::AccountMode::Offline,
            "Offline contained fixture requires its original Offline execution"
        );
        Self::prepare_inner(
            database,
            execution,
            image,
            ready,
            owner_address,
            consumer_address,
            PolicyKind::SystemNsiReadInheritanceV3,
            RootBirthKind::SessionShadowDirectoryV1,
        )
    }
    #[cfg(test)]
    pub(in crate::account_state::execution) fn prepare_rpc_v3_contained_online_fixture(
        database: &'a PrivateDatabase<'lease>,
        execution: &'a ActiveExecution<'lease>,
        image: &'a PinnedRunnerImage,
        ready: &'a ReadyInstallation,
        owner_address: &'a ControlAddress,
        consumer_address: &'a ControlAddress,
    ) -> Result<Self> {
        crate::account_rpc_v3_lifecycle_fixture::require_contained_online_live()?;
        ensure!(
            execution.record()?.identity.binding.mode
                == crate::account_install::AccountMode::Online,
            "Online contained fixture requires its original Online execution"
        );
        Self::prepare_inner(
            database,
            execution,
            image,
            ready,
            owner_address,
            consumer_address,
            PolicyKind::SystemNsiReadInheritanceV3,
            RootBirthKind::SessionShadowDirectoryV1,
        )
    }
    fn prepare_inner(
        database: &'a PrivateDatabase<'lease>,
        execution: &'a ActiveExecution<'lease>,
        image: &'a PinnedRunnerImage,
        ready: &'a ReadyInstallation,
        owner_address: &'a ControlAddress,
        consumer_address: &'a ControlAddress,
        rpc_policy: PolicyKind,
        namespace_birth: RootBirthKind,
    ) -> Result<Self> {
        namespace_birth.require_live()?;
        rpc_policy.require_live()?;
        crate::account_impersonation::require_process_identity()?;
        let record = execution.record()?;
        ensure!(record.scope == PAIRED_PLAN_SCOPE, "paired scope required");
        record.validate(
            &ready.binding(record.identity.binding.mode)?,
            database.file_identity(),
        )?;
        ensure!(
            crate::sid::current_user_sid()? == record.owner,
            "paired plan requires its original host actor"
        );
        image.require_installation(ready)?;
        let plan = Plan {
            format: FORMAT.into(),
            execution: record.clone(),
            operation: random_id()?,
            credential_generation: ready
                .record
                .account(record.identity.binding.mode)?
                .credential_generation
                .clone()
                .context("paired bootstrap requires selected credential generation")?,
            image_id: image.file_identity()?,
            session: current_session()?,
            rpc_policy,
            namespace_birth,
            roles: [
                RolePlan::new(&record.identity.container, Role::Owner, owner_address)?,
                RolePlan::new(
                    &record.identity.container,
                    Role::Consumers,
                    consumer_address,
                )?,
            ],
        };
        persist(database.connection()?, &plan)?;
        let bound = Self {
            database,
            execution,
            image,
            ready,
            owner_address,
            consumer_address,
            plan,
            jobs_attempted: Cell::new(false),
        };
        bound.verify()?;
        Ok(bound)
    }
    pub(crate) fn address(&self, role: Role) -> &ControlAddress {
        match role {
            Role::Owner => self.owner_address,
            Role::Consumers => self.consumer_address,
        }
    }
    pub(crate) fn verify(&self) -> Result<()> {
        self.plan.require_live_policy()?;
        crate::account_impersonation::require_process_identity()?;
        self.image.require_installation(self.ready)?;
        let record = self.execution.record()?;
        record.validate(
            &self.ready.binding(record.identity.binding.mode)?,
            self.database.file_identity(),
        )?;
        ensure!(
            *record == self.plan.execution
                && crate::sid::current_user_sid()? == record.owner
                && current_session()? == self.plan.session
                && self.image.file_identity()? == self.plan.image_id
                && self.owner_address.binding_nonce()? == self.plan.roles[0].control_nonce
                && self.consumer_address.binding_nonce()? == self.plan.roles[1].control_nonce
                && self
                    .ready
                    .record
                    .account(record.identity.binding.mode)?
                    .credential_generation
                    .as_deref()
                    == Some(self.plan.credential_generation.as_str()),
            "live paired bootstrap binding changed"
        );
        require_plan(self.database.connection()?, &self.plan)
    }
}

fn current_session() -> Result<u32> {
    let mut session = 0;
    unsafe {
        windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
            std::process::id(),
            &mut session,
        )?;
    }
    Ok(session)
}
fn durability(sql: &Connection) -> Result<()> {
    ensure!(
        sql.is_autocommit(),
        "paired plan cannot join another transaction"
    );
    let mode: String = sql.query_row("PRAGMA journal_mode", [], |r| r.get(0))?;
    let sync: i64 = sql.query_row("PRAGMA synchronous", [], |r| r.get(0))?;
    ensure!(
        mode == "delete" && sync == 2,
        "paired plan requires DELETE/FULL durability"
    );
    Ok(())
}
fn persist(sql: &Connection, plan: &Plan) -> Result<()> {
    plan.validate()?;
    durability(sql)?;
    crate::account_state::validate_header(sql, &plan.execution.identity.binding)?;
    let encoded = serde_json::to_string(plan)?;
    ensure!(encoded.len() <= MAX_BYTES, "paired identity exceeds bounds");
    let tx = sql.unchecked_transaction()?;
    // Existing empty schemas are ambiguous outcomes, not retry permission.
    for statement in SCHEMA.into_iter().chain(EVENT_SCHEMA) {
        tx.execute_batch(statement)?;
    }
    account_journal::create_schema(&tx)?;
    native::namespace::create_schema(&tx)?;
    tx.execute(
        "INSERT INTO candidate_paired_bootstrap_plan VALUES(1,?1)",
        [encoded],
    )?;
    tx.commit()
        .context("paired plan commit uncertain; preserve state")?;
    require_plan(sql, plan)
}
pub(super) fn require_plan(sql: &Connection, expected: &Plan) -> Result<()> {
    expected.validate()?;
    durability(sql)?;
    crate::account_state::validate_header(sql, &expected.execution.identity.binding)?;
    account_journal::require_schema(sql)?;
    native::namespace::require_schema(sql)?;
    for (table, schema) in [(TABLE, SCHEMA), (EVENTS_TABLE, EVENT_SCHEMA)] {
        let mut statement = sql.prepare("SELECT sql FROM sqlite_schema WHERE tbl_name=?1")?;
        let rows: Vec<String> = statement
            .query_map([table], |r| r.get(0))?
            .collect::<std::result::Result<_, _>>()?;
        ensure!(
            rows.len() == schema.len() && schema.iter().all(|s| rows.iter().any(|r| r == s)),
            "paired schema differs; preserve state"
        );
    }
    let count: u64 = sql.query_row(
        "SELECT count(*) FROM candidate_paired_bootstrap_plan",
        [],
        |r| r.get(0),
    )?;
    ensure!(count == 1, "paired plan inventory is absent or ambiguous");
    let json: Option<String> = sql.query_row(
        "SELECT plan_json FROM candidate_paired_bootstrap_plan WHERE singleton=1 AND typeof(plan_json)='text' AND length(CAST(plan_json AS BLOB))<=?1",
        [MAX_BYTES as u64], |r| r.get(0),
    ).optional()?;
    let observed: Plan = serde_json::from_str(&json.context("paired identity missing/oversized")?)
        .context("malformed paired identity; preserve state")?;
    observed.validate()?;
    ensure!(
        observed == *expected,
        "paired operation/execution binding differs"
    );
    Ok(())
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(super) enum Event {
    JobIntent,
    JobCreated,
    BirthIntent,
    Born {
        pid: u32,
        created: u64,
        image_id: [u8; 24],
    },
    ResumeIntent,
    Resumed,
    Ready,
    RetireIntent,
    Retired,
}
fn validate_events(plan: &Plan, inventory: &[Vec<Event>; 2]) -> Result<()> {
    for events in inventory {
        ensure!(
            events.len() <= 9,
            "paired role event inventory exceeds bounds"
        );
        for (index, event) in events.iter().enumerate() {
            match (index, event) {
                (0, Event::JobIntent)
                | (1, Event::JobCreated)
                | (2, Event::BirthIntent)
                | (4, Event::ResumeIntent)
                | (5, Event::Resumed)
                | (6, Event::Ready)
                | (7, Event::RetireIntent)
                | (8, Event::Retired) => {}
                (
                    3,
                    Event::Born {
                        pid,
                        created,
                        image_id,
                    },
                ) => ensure!(
                    *pid != 0
                        && *pid != plan.execution.pid
                        && *created >= plan.execution.created
                        && *image_id == plan.image_id,
                    "paired birth differs from original process/image"
                ),
                _ => anyhow::bail!("paired role event is out of order"),
            }
        }
    }
    if let (
        Some(Event::Born {
            pid: a,
            created: ac,
            ..
        }),
        Some(Event::Born {
            pid: b,
            created: bc,
            ..
        }),
    ) = (inventory[0].get(3), inventory[1].get(3))
    {
        ensure!(
            (*a, *ac) != (*b, *bc),
            "paired roles reused one original process"
        );
    }
    ensure!(
        inventory[Role::Owner.index()].len() < 8
            || inventory[Role::Consumers.index()].last() == Some(&Event::Retired),
        "owner retirement requires fully retired consumers"
    );
    Ok(())
}
fn inventory(sql: &Connection, plan: &Plan) -> Result<[Vec<Event>; 2]> {
    require_plan(sql, plan)?;
    let (count, max, nontext): (u64, Option<u64>, u64) = sql.query_row(
        "SELECT count(*),max(length(CAST(event_json AS BLOB))),coalesce(sum(typeof(event_json)!='text'),0) FROM candidate_paired_bootstrap_events",
        [], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
    )?;
    ensure!(
        count <= 18 && max.unwrap_or(0) <= MAX_EVENT_BYTES as u64 && nontext == 0,
        "paired event inventory exceeds bounds or type"
    );
    let mut statement = sql.prepare(
        "SELECT role,sequence,event_json FROM candidate_paired_bootstrap_events ORDER BY role,sequence",
    )?;
    let mut inventory = [Vec::new(), Vec::new()];
    for row in statement.query_map([], |r| {
        Ok((
            r.get::<_, usize>(0)?,
            r.get::<_, usize>(1)?,
            r.get::<_, String>(2)?,
        ))
    })? {
        let (role, sequence, json) = row?;
        ensure!(
            role < 2 && sequence == inventory[role].len(),
            "paired role/sequence gap"
        );
        let event: Event =
            serde_json::from_str(&json).context("malformed paired event; preserve")?;
        // Also refuse extra fields on unit variants, independently of serde's
        // internally tagged unit-variant handling.
        ensure!(
            serde_json::from_str::<serde_json::Value>(&json)? == serde_json::to_value(&event)?,
            "paired event has unexpected fields"
        );
        inventory[role].push(event);
    }
    validate_events(plan, &inventory)?;
    Ok(inventory)
}
pub(super) fn replay(sql: &Connection, plan: &Plan, role: Role) -> Result<Vec<Event>> {
    let mut all = inventory(sql, plan)?;
    Ok(std::mem::take(&mut all[role.index()]))
}
pub(super) fn append(sql: &Connection, plan: &Plan, role: Role, event: Event) -> Result<()> {
    let mut expected = inventory(sql, plan)?;
    let sequence = expected[role.index()].len();
    expected[role.index()].push(event.clone());
    validate_events(plan, &expected)?;
    let encoded = serde_json::to_string(&event)?;
    ensure!(
        encoded.len() <= MAX_EVENT_BYTES,
        "paired event exceeds bounds"
    );
    let tx = sql.unchecked_transaction()?;
    tx.execute(
        "INSERT INTO candidate_paired_bootstrap_events VALUES(?1,?2,?3)",
        params![role.index(), sequence, encoded],
    )?;
    tx.commit()
        .context("paired event commit uncertain; preserve intent and original guards")?;
    ensure!(
        inventory(sql, plan)? == expected,
        "paired event readback differs"
    );
    Ok(())
}

#[cfg(test)]
pub(in crate::account_state::execution) fn fixture_plan() -> Plan {
    tests::plan()
}

#[cfg(test)]
#[path = "account_paired_plan_tests.rs"]
mod tests;
