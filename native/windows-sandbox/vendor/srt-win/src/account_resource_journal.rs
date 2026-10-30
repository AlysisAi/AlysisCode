//! Durable prerequisite for full candidate execution, NOT a launch authority.
//!
//! Every external effect needs a committed intent and a separately committed
//! observation. A random name, deterministic SID or successful process exit is
//! never substituted for an observed successful exclusive creation. Interrupted
//! create/delete intents remain ambiguous; this module issues no guessed repair.
//!
//! Native adapters own an empty bootstrap job, an empty namespace root, or a
//! standalone retained station grant or an unused host profile. They cannot
//! assign/launch processes or recover ownership after restart.
//! The account runner needs a separate authenticated control channel, and its
//! suspended creation/job-assignment gap must be solved before launch is enabled.
#![allow(dead_code)]

use super::{ActiveExecution, ExecutionRecord, PrivateDatabase, RESOURCE_SCOPE, StoppedExecution};
use anyhow::{Context, Result, ensure};
use rusqlite::{Connection, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

const MAX_EVENTS: usize = 128;
const MAX_EVENT_BYTES: usize = 128 * 1024;
const MAX_JOURNAL_BYTES: usize = 2 * 1024 * 1024;
const SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS candidate_resource_runs(
    container TEXT PRIMARY KEY, execution_json TEXT NOT NULL, session_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_resource_events(
    container TEXT NOT NULL, sequence INTEGER NOT NULL, event_json TEXT NOT NULL,
    PRIMARY KEY(container, sequence)
);
"#;

/// Complete inventory required by the existing adapters, including temporary
/// bootstrap objects and their shared kernel-ACL edits. A future adapter may
/// remove an unnecessary side effect from its design, but cannot silently skip
/// it while still performing it. These are roles, not caller-supplied paths.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Resource {
    HostProfile,
    WindowStationGrant,
    NamespaceRoot,
    BootstrapDesktop,
    BootstrapJob,
    BootstrapProcess,
    NamespaceKeeper,
    RunnerDesktop,
    SessionBaseNamedObjectsGrant,
    RunnerJob,
    RunnerProcess,
    AccountProfile,
    ChildJob,
    ChildProcess,
}

const ALL: [Resource; 14] = [
    Resource::HostProfile,
    Resource::WindowStationGrant,
    Resource::NamespaceRoot,
    Resource::BootstrapDesktop,
    Resource::BootstrapJob,
    Resource::BootstrapProcess,
    Resource::NamespaceKeeper,
    Resource::RunnerDesktop,
    Resource::SessionBaseNamedObjectsGrant,
    Resource::RunnerJob,
    Resource::RunnerProcess,
    Resource::AccountProfile,
    Resource::ChildJob,
    Resource::ChildProcess,
];

impl Resource {
    fn prerequisites(self) -> &'static [Resource] {
        use Resource::*;
        match self {
            HostProfile => &[],
            // Granting a derived package SID does not create/use a profile.
            // Bootstrap still requires the profile explicitly below.
            WindowStationGrant => &[],
            NamespaceRoot => &[],
            BootstrapDesktop => &[HostProfile, WindowStationGrant, NamespaceRoot],
            BootstrapJob => &[],
            BootstrapProcess => &[BootstrapDesktop, BootstrapJob],
            NamespaceKeeper => &[BootstrapProcess, NamespaceRoot],
            RunnerDesktop => &[NamespaceKeeper, WindowStationGrant],
            SessionBaseNamedObjectsGrant | RunnerJob => &[RunnerDesktop],
            RunnerProcess => &[RunnerJob, NamespaceKeeper, SessionBaseNamedObjectsGrant],
            AccountProfile => &[RunnerProcess],
            ChildJob => &[AccountProfile],
            ChildProcess => &[ChildJob, NamespaceKeeper],
        }
    }
    fn process(self) -> bool {
        matches!(
            self,
            Self::BootstrapProcess | Self::RunnerProcess | Self::ChildProcess
        )
    }
    fn shared_acl(self) -> bool {
        matches!(
            self,
            Self::WindowStationGrant | Self::SessionBaseNamedObjectsGrant
        )
    }
    fn profile(self) -> bool {
        matches!(self, Self::HostProfile | Self::AccountProfile)
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Identity {
    execution: ExecutionRecord,
    session_id: u32,
}

impl Identity {
    fn validate(&self) -> Result<()> {
        self.execution
            .validate(&self.execution.identity.binding, self.execution.database_id)?;
        ensure!(
            self.execution.scope == RESOURCE_SCOPE,
            "ACL-only execution cannot acquire resources"
        );
        Ok(())
    }
    fn subject(&self, resource: Resource) -> &str {
        match resource {
            Resource::RunnerProcess
            | Resource::AccountProfile
            | Resource::ChildJob
            | Resource::ChildProcess => &self.execution.identity.binding.account_sid,
            _ => &self.execution.owner,
        }
    }
    fn name(&self, resource: Resource) -> String {
        let suffix = self
            .execution
            .identity
            .container
            .strip_prefix("Alysis.Execution.")
            .unwrap();
        let package = &self.execution.identity.package_sid;
        match resource {
            Resource::HostProfile | Resource::AccountProfile => {
                self.execution.identity.container.clone()
            }
            Resource::NamespaceRoot | Resource::NamespaceKeeper => format!(
                "\\Sessions\\{}\\AppContainerNamedObjects\\{package}",
                self.session_id
            ),
            Resource::ChildJob | Resource::ChildProcess => self.execution.job.clone(),
            Resource::RunnerJob | Resource::RunnerProcess => {
                format!("Global\\AlysisSandboxRunner.{suffix}")
            }
            Resource::BootstrapJob | Resource::BootstrapProcess => {
                format!("Global\\AlysisSandboxBootstrap.{suffix}")
            }
            Resource::BootstrapDesktop => format!(
                "session:{}:WinSta0\\alysis-bootstrap-{suffix}",
                self.session_id
            ),
            Resource::RunnerDesktop => format!(
                "session:{}:WinSta0\\alysis-runner-{suffix}",
                self.session_id
            ),
            Resource::WindowStationGrant => format!("session:{}:WinSta0", self.session_id),
            Resource::SessionBaseNamedObjectsGrant => {
                format!("\\Sessions\\{}\\BaseNamedObjects", self.session_id)
            }
        }
    }
}

/// Folder-only profile receipts are legacy/incomplete and always refused.
/// Execution-capable profile ownership has no admitted native receipt yet.
/// The unused-profile receipt is restricted to its standalone composite adapter.
/// The synthetic profile variant exists only in in-memory transition tests. Process
/// observations bind PID+creation time, image file identity, and the exact
/// role-specific named job. Kernel-object birth must have returned exclusive
/// creation success; the future adapter must retain its actual handle.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", deny_unknown_fields, rename_all = "snake_case")]
pub(crate) enum Observation {
    // Decode old/incomplete history only to refuse it, never adopt or repair.
    Profile {
        folder_id: [u8; 24],
    },
    /// A fresh, unused host profile only. It cannot satisfy launch prerequisites
    /// or coexist with any other resource. The original composite guard is the
    /// cleanup authority; these bytes cannot reconstruct it after a restart.
    UnusedHostProfile {
        policy_version: u32,
        folder_id: [u8; 24],
    },
    #[cfg(test)]
    SyntheticProfile {
        folder_id: [u8; 24],
    },
    Process {
        pid: u32,
        created: u64,
        image_id: [u8; 24],
        job: String,
    },
    ExclusiveKernelObject,
    EmptyBootstrapJob {
        policy_version: u32,
    },
    EmptyNamespaceRoot {
        policy_version: u32,
    },
    SharedAclApplied,
    WindowStationAcl {
        policy_version: u32,
    },
}

/// Write-ahead snapshots for shared kernel ACL edits. Each native adapter must
/// validate its descriptor encoding, exact policy delta, original handle and
/// readback. The station adapter records bounded raw Snapshot JSON, not SDDL.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct AclIntent {
    pub before: Vec<u8>,
    pub after: Vec<u8>,
}

impl AclIntent {
    fn validate(&self) -> Result<()> {
        ensure!(
            (20..=65536).contains(&self.before.len())
                && (20..=65536).contains(&self.after.len())
                && self.before != self.after,
            "invalid/empty shared ACL intent"
        );
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "event", deny_unknown_fields, rename_all = "snake_case")]
enum Event {
    CreateIntent {
        resource: Resource,
        nonce: String,
        subject: String,
        name: String,
        acl: Option<AclIntent>,
    },
    Created {
        resource: Resource,
        nonce: String,
        observed: Observation,
    },
    RemoveIntent {
        resource: Resource,
        nonce: String,
    },
    Removed {
        resource: Resource,
        nonce: String,
    },
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum State {
    Creating {
        nonce: String,
    },
    Present {
        observed: Observation,
    },
    Removing {
        nonce: String,
        observed: Observation,
    },
    Removed,
}

#[derive(Default)]
struct Replay {
    states: BTreeMap<Resource, State>,
    events: usize,
    unused_profile_only: bool,
}

impl Replay {
    fn apply(&mut self, identity: &Identity, event: &Event) -> Result<()> {
        identity.validate()?;
        ensure!(self.events < MAX_EVENTS, "resource event limit exceeded");
        match event {
            Event::CreateIntent {
                resource,
                nonce,
                subject,
                name,
                acl,
            } => {
                crate::account_install::validate_random_id(nonce)?;
                ensure!(
                    !self.unused_profile_only,
                    "unused profile lifecycle cannot acquire execution resources"
                );
                ensure!(
                    !self.states.contains_key(resource),
                    "resource creation/reuse already recorded"
                );
                ensure!(
                    *subject == identity.subject(*resource) && *name == identity.name(*resource),
                    "resource intent identity differs from execution plan"
                );
                ensure!(
                    resource.prerequisites().iter().all(|required| matches!(
                        self.states.get(required),
                        Some(State::Present { .. })
                    )),
                    "resource creation prerequisites are not proven present"
                );
                ensure!(
                    resource.shared_acl() == acl.is_some(),
                    "shared ACL intent/snapshot mismatch"
                );
                if let Some(acl) = acl {
                    acl.validate()?;
                }
                ensure!(
                    !self.states.values().any(|state| matches!(
                        state,
                        State::Creating { .. } | State::Removing { .. }
                    )),
                    "ambiguous resource effect remains pending"
                );
                self.states.insert(
                    *resource,
                    State::Creating {
                        nonce: nonce.clone(),
                    },
                );
            }
            Event::Created {
                resource,
                nonce,
                observed,
            } => {
                ensure!(
                    matches!(self.states.get(resource), Some(State::Creating { nonce: pending }) if pending == nonce),
                    "resource receipt does not match a committed create intent"
                );
                match observed {
                    Observation::Profile { .. } => anyhow::bail!(
                        "folder-only profile receipt cannot prove composite ownership; preserve journal"
                    ),
                    Observation::UnusedHostProfile {
                        policy_version,
                        folder_id,
                    } => {
                        ensure!(
                            *resource == Resource::HostProfile
                                && *policy_version == 1
                                && *folder_id != [0; 24]
                                && self.states.len() == 1,
                            "unused profile receipt requires a standalone host profile"
                        );
                        self.unused_profile_only = true;
                    }
                    #[cfg(test)]
                    Observation::SyntheticProfile { folder_id } => ensure!(
                        resource.profile() && *folder_id != [0; 24],
                        "profile receipt identity missing/wrong type"
                    ),
                    Observation::Process {
                        pid,
                        created,
                        image_id,
                        job,
                    } => ensure!(
                        resource.process()
                            && *pid != 0
                            && *pid != identity.execution.pid
                            && *created != 0
                            && *image_id != [0; 24]
                            && *job == identity.name(*resource),
                        "process receipt lacks exact process/image/job identity"
                    ),
                    Observation::EmptyBootstrapJob { policy_version } => ensure!(
                        *resource == Resource::BootstrapJob && *policy_version == 1,
                        "unexpected empty bootstrap job receipt"
                    ),
                    Observation::EmptyNamespaceRoot { policy_version } => ensure!(
                        *resource == Resource::NamespaceRoot && *policy_version == 2,
                        "unexpected empty namespace root receipt"
                    ),
                    Observation::WindowStationAcl { policy_version } => ensure!(
                        *resource == Resource::WindowStationGrant && *policy_version == 1,
                        "unexpected retained window-station grant receipt"
                    ),
                    Observation::SharedAclApplied => {
                        ensure!(resource.shared_acl(), "unexpected shared ACL receipt")
                    }
                    Observation::ExclusiveKernelObject => ensure!(
                        !resource.profile() && !resource.process() && !resource.shared_acl(),
                        "resource requires more than a named-object creation receipt"
                    ),
                }
                #[cfg(test)]
                ensure!(!self.states.values().any(|state| matches!((state, observed),
                    (State::Present { observed: Observation::SyntheticProfile { folder_id: old } }, Observation::SyntheticProfile { folder_id }) if old == folder_id)),
                    "different model profiles cannot share one folder identity");
                ensure!(!self.states.values().any(|state| matches!((state, observed),
                    (State::Present { observed: Observation::Process { pid: old_pid, created: old_created, .. } }, Observation::Process { pid, created, .. })
                        if old_pid == pid && old_created == created)), "different resource roles cannot adopt the same process");
                self.states.insert(
                    *resource,
                    State::Present {
                        observed: observed.clone(),
                    },
                );
            }
            Event::RemoveIntent { resource, nonce } => {
                crate::account_install::validate_random_id(nonce)?;
                let Some(State::Present { observed }) = self.states.get(resource) else {
                    anyhow::bail!(
                        "resource removal requires recorded successful creation; preserve ambiguous intent"
                    );
                };
                ensure!(
                    !self.states.values().any(|state| matches!(
                        state,
                        State::Creating { .. } | State::Removing { .. }
                    )),
                    "ambiguous resource effect remains pending"
                );
                // Every process/job must be explicitly stopped before profile,
                // namespace, desktop or ACL teardown. Absence of a named child
                // job alone says nothing about an unassigned suspended runner.
                if !resource.process()
                    && !matches!(
                        resource,
                        Resource::BootstrapJob | Resource::RunnerJob | Resource::ChildJob
                    )
                {
                    ensure!(self.states.iter().all(|(role, state)| !role.process() || matches!(state, State::Removed)),
                        "process quiescence is required before resource teardown");
                }
                if matches!(
                    resource,
                    Resource::BootstrapJob | Resource::RunnerJob | Resource::ChildJob
                ) {
                    ensure!(
                        self.states.iter().all(|(role, state)| !role.process()
                            || identity.name(*role) != identity.name(*resource)
                            || matches!(state, State::Removed)),
                        "job cannot retire before its exact process"
                    );
                }
                // Profiles outlive namespace handles; host profile outlives the
                // account profile. Shared ACLs/desktops outlive their processes.
                let required = match resource {
                    Resource::HostProfile => &[
                        Resource::AccountProfile,
                        Resource::NamespaceKeeper,
                        Resource::NamespaceRoot,
                    ][..],
                    Resource::AccountProfile => &[Resource::ChildJob, Resource::RunnerJob][..],
                    Resource::NamespaceRoot => &[Resource::NamespaceKeeper][..],
                    _ => &[][..],
                };
                if *resource == Resource::HostProfile {
                    ensure!(
                        self.states
                            .iter()
                            .all(|(role, state)| *role == Resource::HostProfile
                                || matches!(state, State::Removed)),
                        "host profile must outlive every execution resource"
                    );
                }
                ensure!(
                    required.iter().all(|role| !self.states.contains_key(role)
                        || matches!(self.states.get(role), Some(State::Removed))),
                    "resource teardown ordering is incomplete"
                );
                let observed = observed.clone();
                self.states.insert(
                    *resource,
                    State::Removing {
                        nonce: nonce.clone(),
                        observed,
                    },
                );
            }
            Event::Removed { resource, nonce } => {
                ensure!(
                    matches!(self.states.get(resource), Some(State::Removing { nonce: pending, .. }) if pending == nonce),
                    "resource teardown receipt does not match its committed intent"
                );
                self.states.insert(*resource, State::Removed);
            }
        }
        self.events += 1;
        Ok(())
    }
    fn clean(&self) -> bool {
        self.states
            .values()
            .all(|state| matches!(state, State::Removed))
    }
}

enum Authority<'a> {
    Live(&'a ActiveExecution<'a>),
    Stopped(&'a StoppedExecution<'a>),
    #[cfg(test)]
    Fixture,
    #[cfg(test)]
    FixtureLive,
    #[cfg(test)]
    FixtureStopped,
}

pub(crate) struct ResourceJournal<'a> {
    connection: &'a Connection,
    identity: Identity,
    authority: Authority<'a>,
}

/// No public constructor: serialization alone cannot mint a retirement receipt.
pub(crate) struct RecoveredResources {
    identity: ExecutionRecord,
}
impl RecoveredResources {
    pub(super) fn matches(&self, record: &ExecutionRecord) -> bool {
        self.identity == *record
    }
}

impl<'a> ResourceJournal<'a> {
    pub(crate) fn open_live(
        database: &'a PrivateDatabase<'_>,
        execution: &'a ActiveExecution<'_>,
    ) -> Result<Self> {
        let record = execution.record()?;
        record.validate(&record.identity.binding, database.file_identity())?;
        let mut session = 0;
        unsafe {
            windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
                std::process::id(),
                &mut session,
            )?;
        }
        Self::open(
            database.connection()?,
            Identity {
                execution: record.clone(),
                session_id: session,
            },
            Authority::Live(execution),
            true,
        )
    }
    pub(crate) fn open_stopped(
        database: &'a PrivateDatabase<'_>,
        execution: &'a StoppedExecution<'_>,
    ) -> Result<Self> {
        let record = execution.record()?;
        record.validate(&record.identity.binding, database.file_identity())?;
        let connection = database.connection()?;
        // Never initialize recovery state: a missing plan is ambiguous because
        // this scope is reserved before resource setup and may have crashed.
        let session: u32 = connection
            .query_row(
                "SELECT session_id FROM candidate_resource_runs WHERE container=?1",
                [&record.identity.container],
                |row| row.get(0),
            )
            .context("resource plan missing; preserve full execution marker")?;
        Self::open(
            connection,
            Identity {
                execution: record.clone(),
                session_id: session,
            },
            Authority::Stopped(execution),
            false,
        )
    }
    fn open(
        connection: &'a Connection,
        identity: Identity,
        authority: Authority<'a>,
        create: bool,
    ) -> Result<Self> {
        let journal = Self {
            connection,
            identity,
            authority,
        };
        journal.validate()?;
        if create {
            journal.connection.execute_batch(SCHEMA)?;
            let transaction = journal.connection.unchecked_transaction()?;
            let runs: i64 =
                transaction.query_row("SELECT count(*) FROM candidate_resource_runs", [], |r| {
                    r.get(0)
                })?;
            let events: i64 = transaction.query_row(
                "SELECT count(*) FROM candidate_resource_events",
                [],
                |r| r.get(0),
            )?;
            ensure!(
                runs == 0 && events == 0,
                "existing resource plan must not be adopted or replaced"
            );
            transaction.execute(
                "INSERT INTO candidate_resource_runs VALUES(?1,?2,?3)",
                params![
                    journal.identity.execution.identity.container,
                    serde_json::to_string(&journal.identity.execution)?,
                    journal.identity.session_id
                ],
            )?;
            transaction
                .commit()
                .context("persist full-resource plan before effects")?;
        }
        journal.replay()?;
        Ok(journal)
    }
    fn validate(&self) -> Result<()> {
        self.identity.validate()?;
        crate::account_state::validate_header(
            self.connection,
            &self.identity.execution.identity.binding,
        )?;
        ensure!(
            self.connection.is_autocommit(),
            "resource journal cannot share an open transaction"
        );
        match &self.authority {
            Authority::Live(value) => ensure!(
                *value.record()? == self.identity.execution,
                "resource live authority mismatch"
            ),
            Authority::Stopped(value) => ensure!(
                *value.record()? == self.identity.execution,
                "resource stopped authority mismatch"
            ),
            #[cfg(test)]
            Authority::Fixture | Authority::FixtureLive | Authority::FixtureStopped => {
                return Ok(());
            }
        }
        let mode: String = self
            .connection
            .query_row("PRAGMA journal_mode", [], |r| r.get(0))?;
        let sync: i64 = self
            .connection
            .query_row("PRAGMA synchronous", [], |r| r.get(0))?;
        ensure!(
            mode == "delete" && sync == 2,
            "resource journal requires DELETE/FULL durability"
        );
        Ok(())
    }
    fn replay(&self) -> Result<Replay> {
        self.validate()?;
        let runs: i64 =
            self.connection
                .query_row("SELECT count(*) FROM candidate_resource_runs", [], |r| {
                    r.get(0)
                })?;
        ensure!(runs == 1, "ambiguous resource run inventory");
        let row: Option<(String, u32)> = self.connection.query_row(
            "SELECT execution_json,session_id FROM candidate_resource_runs WHERE container=?1 AND length(execution_json)<=16384",
            [&self.identity.execution.identity.container], |r| Ok((r.get(0)?,r.get(1)?))).optional()?;
        let (json, session) = row.context("missing/oversized full resource execution identity")?;
        ensure!(
            serde_json::from_str::<ExecutionRecord>(&json)? == self.identity.execution
                && session == self.identity.session_id,
            "resource journal execution/session identity mismatch"
        );
        let (count, bytes): (usize, usize) = self.connection.query_row(
            "SELECT count(*),coalesce(sum(length(event_json)),0) FROM candidate_resource_events",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        ensure!(
            count <= MAX_EVENTS && bytes <= MAX_JOURNAL_BYTES,
            "resource journal exceeds bounds"
        );
        let mut statement = self.connection.prepare("SELECT sequence,event_json FROM candidate_resource_events WHERE container=?1 ORDER BY sequence")?;
        let rows = statement.query_map([&self.identity.execution.identity.container], |r| {
            Ok((r.get::<_, usize>(0)?, r.get::<_, String>(1)?))
        })?;
        let mut replay = Replay::default();
        for row in rows {
            let (sequence, json) = row?;
            ensure!(
                sequence == replay.events && json.len() <= MAX_EVENT_BYTES,
                "resource event order/length mismatch"
            );
            let event = serde_json::from_str::<Event>(&json)?;
            self.validate_observation_source(&event)?;
            replay.apply(&self.identity, &event)?;
        }
        ensure!(
            replay.events == count,
            "resource journal contains foreign execution rows"
        );
        Ok(replay)
    }
    fn validate_observation_source(&self, _event: &Event) -> Result<()> {
        #[cfg(test)]
        if matches!(
            _event,
            Event::Created {
                observed: Observation::SyntheticProfile { .. },
                ..
            }
        ) {
            ensure!(
                matches!(self.authority, Authority::Fixture),
                "synthetic profile receipt is restricted to the transition model"
            );
        }
        Ok(())
    }
    fn append(&self, event: Event) -> Result<()> {
        self.validate_observation_source(&event)?;
        let mut replay = self.replay()?;
        let sequence = replay.events;
        replay.apply(&self.identity, &event)?;
        let json = serde_json::to_string(&event)?;
        ensure!(
            json.len() <= MAX_EVENT_BYTES,
            "resource event exceeds bounds"
        );
        let transaction = self.connection.unchecked_transaction()?;
        transaction.execute(
            "INSERT INTO candidate_resource_events VALUES(?1,?2,?3)",
            params![self.identity.execution.identity.container, sequence, json],
        )?;
        transaction.commit().context(
            "persist resource transition; ambiguous outcome must not be retried as an effect",
        )?;
        self.replay()?;
        Ok(())
    }
    /// Effect driver is deliberately private to this module's future native
    /// adapters. A generic closure returning success is not production proof.
    fn create_intent(&self, resource: Resource, acl: Option<AclIntent>) -> Result<String> {
        ensure!(
            !matches!(self.authority, Authority::Stopped(_)),
            "stopped execution cannot create resources"
        );
        let nonce = super::random_id()?;
        self.append(Event::CreateIntent {
            resource,
            nonce: nonce.clone(),
            subject: self.identity.subject(resource).into(),
            name: self.identity.name(resource),
            acl,
        })?;
        Ok(nonce)
    }
    fn created(&self, resource: Resource, nonce: String, observed: Observation) -> Result<()> {
        ensure!(
            !matches!(self.authority, Authority::Stopped(_)),
            "stopped execution cannot adopt an ambiguous creation"
        );
        self.append(Event::Created {
            resource,
            nonce,
            observed,
        })
    }
    fn remove_intent(&self, resource: Resource) -> Result<String> {
        ensure!(
            !matches!(self.authority, Authority::Live(_)),
            "live authority cannot begin teardown"
        );
        let nonce = super::random_id()?;
        self.append(Event::RemoveIntent {
            resource,
            nonce: nonce.clone(),
        })?;
        Ok(nonce)
    }
    fn removed(&self, resource: Resource, nonce: String) -> Result<()> {
        ensure!(
            !matches!(self.authority, Authority::Live(_)),
            "live authority cannot acknowledge teardown"
        );
        self.append(Event::Removed { resource, nonce })
    }
    pub(crate) fn recovered(&self) -> Result<RecoveredResources> {
        ensure!(
            !matches!(self.authority, Authority::Live(_)),
            "live authority cannot retire resource state"
        );
        ensure!(
            self.replay()?.clean(),
            "resource effects are pending or ambiguous; preserve journal"
        );
        // Retain completed resource audit rows. They are evidence if final
        // marker retirement is interrupted; a future archive protocol must
        // explicitly retire them before another resource execution is started.
        Ok(RecoveredResources {
            identity: self.identity.execution.clone(),
        })
    }
}

#[cfg(test)]
#[path = "account_resource_journal_tests.rs"]
mod tests;

#[path = "account_resource_job.rs"]
mod empty_job;
#[allow(unused_imports)] // Private prerequisite; activation has no caller yet.
pub(crate) use empty_job::EmptyBootstrapJob;

#[path = "account_resource_namespace.rs"]
mod empty_namespace;
#[allow(unused_imports)] // Private prerequisite; activation has no caller yet.
pub(crate) use empty_namespace::EmptyNamespaceRoot;
pub(super) use empty_namespace::native as namespace_native;
// Read-only entry only; native ownership and mutation APIs keep their existing scope.
#[cfg(test)]
pub(crate) use empty_namespace::native::RootBirthKind as FixtureRootBirthKind;
#[cfg(test)]
pub(crate) use empty_namespace::native::rpc::PolicyKind as FixtureRpcPolicy;
#[cfg(test)]
pub(crate) use empty_namespace::native::rpc::lowbox as fixture_rpc_lowbox;
pub(crate) use empty_namespace::native::rpc::read_only_account_probe as read_only_account_rpc_probe;

#[path = "account_resource_station.rs"]
pub(super) mod station;
#[allow(unused_imports)] // Standalone retained grant; disabled, no activation/launch caller.
pub(crate) use station::PreparedWindowStationGrant;

#[path = "account_resource_profile.rs"]
mod unused_profile;
pub(super) use unused_profile::NativeProfile as NativeHostProfile;
#[allow(unused_imports)] // Standalone lifecycle only; no activation/launch caller.
pub(crate) use unused_profile::UnusedHostProfile;
pub(super) use unused_profile::UsedNativeProfile as UsedNativeHostProfile;

#[path = "account_resource_control.rs"]
mod control;
#[allow(unused_imports)] // Disabled native adapter/transport integration; no runner authority.
pub(crate) use control::{EmptyJobConfirmation, ResourceControl};
