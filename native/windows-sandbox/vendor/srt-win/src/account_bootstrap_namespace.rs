//! Retained empty namespace composed with the original atomic job. The broker
//! keeps both native namespace handles across account birth and whole-job exit.
//! Only the live completion from that same plan permits explicit retirement.
//! Cold rows are diagnostic data, never ownership or permission to adopt names.
use super::super::resources::namespace_native as native;
use super::*;
use crate::account_control::ControlServer;
use lifecycle::{
    JournaledJob,
    resume::logon::delivery::birth::{AccountBirthObservation, retirement},
};
use std::time::Duration;
use windows::Win32::Foundation::CloseHandle;

const TABLE: &str = "candidate_atomic_namespace";
const POLICY: u32 = 2;
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_atomic_namespace(sequence INTEGER PRIMARY KEY CHECK(sequence BETWEEN 0 AND 3), event_json TEXT NOT NULL)",
    "CREATE TRIGGER atomic_namespace_no_update BEFORE UPDATE ON candidate_atomic_namespace BEGIN SELECT RAISE(ABORT,'atomic namespace event is immutable'); END",
    "CREATE TRIGGER atomic_namespace_no_delete BEFORE DELETE ON candidate_atomic_namespace BEGIN SELECT RAISE(ABORT,'atomic namespace event is immutable'); END",
    "CREATE TRIGGER atomic_namespace_no_replace BEFORE INSERT ON candidate_atomic_namespace WHEN EXISTS(SELECT 1 FROM candidate_atomic_namespace WHERE sequence=NEW.sequence) BEGIN SELECT RAISE(ABORT,'atomic namespace event is immutable'); END",
];
pub(super) fn create_schema(sql: &Connection) -> Result<()> {
    for statement in SCHEMA {
        sql.execute_batch(statement)?;
    }
    Ok(())
}
pub(super) fn require_schema(sql: &Connection) -> Result<()> {
    let mut s = sql.prepare("SELECT sql FROM sqlite_schema WHERE tbl_name=?1")?;
    let rows: Vec<String> = s
        .query_map([TABLE], |r| r.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    ensure!(
        rows.len() == SCHEMA.len() && SCHEMA.iter().all(|s| rows.iter().any(|r| r == s)),
        "atomic namespace schema differs; preserve state"
    );
    Ok(())
}
impl native::NamespacePolicySource for Plan {
    fn execution(&self) -> &ExecutionRecord {
        &self.execution
    }
    fn session(&self) -> u32 {
        self.session
    }
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Binding {
    operation: String,
    nonce: String,
    session: u32,
    package_sid: String,
    policy: u32,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum Event {
    Intent {
        binding: Binding,
    },
    Created {
        binding: Binding,
    },
    RemoveIntent {
        binding: Binding,
        retirement: String,
    },
    Removed {
        binding: Binding,
        retirement: String,
    },
}
fn validate(sql: &Connection, plan: &Plan, events: &[Event]) -> Result<()> {
    ensure!(events.len() <= 4, "atomic namespace event bound exceeded");
    if events.is_empty() {
        return Ok(());
    }
    let Event::Intent { binding } = &events[0] else {
        anyhow::bail!("atomic namespace intent missing");
    };
    validate_random_id(&binding.nonce)?;
    ensure!(
        binding.operation == plan.operation
            && binding.nonce != plan.operation
            && binding.session == plan.session
            && binding.package_sid == plan.execution.identity.package_sid
            && binding.policy == POLICY,
        "atomic namespace binding differs from original plan"
    );
    if let Some(event) = events.get(1) {
        ensure!(
            *event
                == Event::Created {
                    binding: binding.clone()
                },
            "atomic namespace birth receipt differs"
        );
    }
    if let Some(event) = events.get(2) {
        let nonce = retirement::completed_nonce(sql, plan)?;
        ensure!(
            nonce != binding.nonce
                && *event
                    == Event::RemoveIntent {
                        binding: binding.clone(),
                        retirement: nonce.clone(),
                    },
            "atomic namespace removal lacks exact completed job"
        );
        if let Some(event) = events.get(3) {
            ensure!(
                *event
                    == Event::Removed {
                        binding: binding.clone(),
                        retirement: nonce
                    },
                "atomic namespace removed receipt differs"
            );
        }
    }
    Ok(())
}
fn replay(sql: &Connection, plan: &Plan) -> Result<Vec<Event>> {
    require_plan(sql, plan)?;
    let (count, max): (usize, Option<usize>) = sql.query_row(
        "SELECT count(*),max(length(CAST(event_json AS BLOB))) FROM candidate_atomic_namespace",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    ensure!(
        count <= 4 && max.unwrap_or(0) <= 2048,
        "atomic namespace inventory exceeds bounds"
    );
    let mut s = sql
        .prepare("SELECT sequence,event_json FROM candidate_atomic_namespace ORDER BY sequence")?;
    let mut events = Vec::new();
    for row in s.query_map([], |r| Ok((r.get::<_, usize>(0)?, r.get::<_, String>(1)?)))? {
        let (sequence, bytes) = row?;
        ensure!(
            sequence == events.len(),
            "atomic namespace sequence has a gap"
        );
        events.push(serde_json::from_str(&bytes).context("malformed atomic namespace event")?);
    }
    validate(sql, plan, &events)?;
    Ok(events)
}
fn append(sql: &Connection, plan: &Plan, event: Event) -> Result<()> {
    let mut expected = replay(sql, plan)?;
    let sequence = expected.len();
    expected.push(event.clone());
    validate(sql, plan, &expected)?;
    let bytes = serde_json::to_string(&event)?;
    ensure!(bytes.len() <= 2048, "atomic namespace event exceeds bound");
    let tx = sql.unchecked_transaction()?;
    tx.execute(
        "INSERT INTO candidate_atomic_namespace VALUES(?1,?2)",
        params![sequence, bytes],
    )?;
    tx.commit()
        .context("atomic namespace commit uncertain; preserve pending state")?;
    ensure!(
        replay(sql, plan)? == expected,
        "atomic namespace readback differs"
    );
    Ok(())
}
pub(super) fn retired_nonce(sql: &Connection, plan: &Plan) -> Result<String> {
    let events = replay(sql, plan)?;
    let Some(Event::Removed { retirement, .. }) = events.get(3) else {
        anyhow::bail!("atomic namespace retirement is incomplete");
    };
    Ok(retirement.clone())
}

/// Field order is deliberate: on all errors/unwind, drain the original job
/// before releasing broker-held namespace ownership. No raw handles escape.
pub(crate) struct NamespacedJob<'p, 'a, 'lease> {
    job: Option<JournaledJob<'p, 'a, 'lease>>,
    profile: Option<host_profile::OwnedHostProfile<'p, 'a, 'lease>>,
    namespace: OwnedNamespace<'p, 'a, 'lease>,
}
pub(super) struct OwnedNamespace<'p, 'a, 'lease> {
    root: Option<OwnedHandle>,
    parent: OwnedHandle,
    plan: &'p CommittedPlan<'a, 'lease>,
    binding: Binding,
}
impl<'p, 'a, 'lease> JournaledJob<'p, 'a, 'lease> {
    pub(crate) fn with_empty_namespace(self) -> Result<NamespacedJob<'p, 'a, 'lease>> {
        self.record_birth()?;
        self.require_resource_birth()?;
        let plan = self.plan;
        let sql = plan.database.connection()?;
        ensure!(
            replay(sql, &plan.plan)?.is_empty(),
            "atomic namespace birth already attempted"
        );
        let parent = native::open_parent(&plan.plan)?;
        let binding = Binding {
            operation: plan.plan.operation.clone(),
            nonce: random_id()?,
            session: plan.plan.session,
            package_sid: plan.plan.execution.identity.package_sid.clone(),
            policy: POLICY,
        };
        append(
            sql,
            &plan.plan,
            Event::Intent {
                binding: binding.clone(),
            },
        )?;
        self.require_resource_birth()?;
        let root = native::create_native(&plan.plan, parent.raw())?;
        let bundle = NamespacedJob {
            job: Some(self),
            profile: None,
            namespace: OwnedNamespace {
                root: Some(root),
                parent,
                plan,
                binding: binding.clone(),
            },
        };
        #[cfg(test)]
        bundle.namespace.checkpoint("born")?;
        bundle.namespace.verify_open()?;
        append(sql, &plan.plan, Event::Created { binding })?;
        #[cfg(test)]
        bundle.namespace.checkpoint("ready")?;
        bundle.namespace.verify_open()?;
        Ok(bundle)
    }
}
impl<'p, 'a, 'lease> NamespacedJob<'p, 'a, 'lease> {
    pub(crate) fn with_host_profile_for_use(self) -> Result<Self> {
        let mut value = self.with_unused_host_profile()?;
        value
            .profile
            .as_mut()
            .context("original host profile missing")?
            .begin_use()?;
        value.require_before_bootstrap()?;
        Ok(value)
    }
    pub(crate) fn with_unused_host_profile(mut self) -> Result<Self> {
        self.require_before_bootstrap()?;
        ensure!(
            self.profile.is_none(),
            "original host profile already present"
        );
        self.profile = Some(host_profile::OwnedHostProfile::create(self.namespace.plan)?);
        self.require_before_bootstrap()?;
        Ok(self)
    }
    pub(super) fn plan(&self) -> &'p CommittedPlan<'a, 'lease> {
        self.namespace.plan
    }
    pub(super) fn require_before_bootstrap(&self) -> Result<()> {
        self.job
            .as_ref()
            .context("namespace job already consumed")?
            .require_resource_birth()?;
        self.namespace.verify_open()?;
        if let Some(profile) = &self.profile {
            profile.verify_open()?;
        }
        ensure!(
            replay(
                self.namespace.plan.database.connection()?,
                &self.namespace.plan.plan
            )? == vec![
                Event::Intent {
                    binding: self.namespace.binding.clone()
                },
                Event::Created {
                    binding: self.namespace.binding.clone()
                },
            ],
            "namespace birth receipt differs before next resource"
        );
        Ok(())
    }
    pub(crate) fn observe_account_birth(
        self,
        server: ControlServer,
        timeout: Duration,
    ) -> Result<AccountBirthObservation> {
        self.observe_completed(
            None,
            server,
            timeout,
            #[cfg(test)]
            None,
        )
        .map(|(observation, _)| observation)
    }
    #[cfg(test)]
    pub(crate) fn fixture_observe_account_birth(
        self,
        server: ControlServer,
        password: &str,
        timeout: Duration,
    ) -> Result<AccountBirthObservation> {
        self.observe_completed(None, server, timeout, Some(password))
            .map(|(observation, _)| observation)
    }
    pub(super) fn observe_completed(
        mut self,
        initializer_resources: Option<(
            &desktop::OwnedDesktop<'p, 'a, 'lease>,
            &station::OwnedStation<'p, 'a, 'lease>,
        )>,
        server: ControlServer,
        timeout: Duration,
        #[cfg(test)] password: Option<&str>,
    ) -> Result<(AccountBirthObservation, CompletedNamespace<'p, 'a, 'lease>)> {
        self.namespace.verify_open()?;
        if let Some(profile) = &self.profile {
            profile.verify_open()?;
        }
        let mut initialization = if let Some((desktop, station)) = initializer_resources {
            Some(lifecycle::initializer::Initialization::prepare(
                self.namespace.plan,
                &self.namespace,
                self.profile
                    .as_ref()
                    .context("initializer host profile missing")?,
                desktop,
                station,
            )?)
        } else {
            None
        };
        let job = self.job.take().context("missing original namespace job")?;
        let (observation, completed) = job.observe_for_namespace(
            initialization.as_mut(),
            server,
            timeout,
            #[cfg(test)]
            password,
        )?;
        if let Some(initialization) = &mut initialization {
            initialization.release(&completed)?;
        }
        drop(initialization);
        // A profile's observation token can retain this namespace. Its storage
        // and token must retire under live job completion before root closure.
        if let Some(profile) = &mut self.profile {
            profile.retire(&completed)?;
        }
        self.namespace.retire(&completed)?;
        Ok((
            observation,
            CompletedNamespace {
                namespace: self.namespace,
                job: completed,
            },
        ))
    }
}
/// Original native closure and same-job completion retained in the host. This
/// value has no cold constructor, cloning, public fields or raw handle API.
pub(super) struct CompletedNamespace<'p, 'a, 'lease> {
    namespace: OwnedNamespace<'p, 'a, 'lease>,
    job: retirement::CompletedJob<'p, 'a, 'lease>,
}
impl CompletedNamespace<'_, '_, '_> {
    pub(super) fn verify_for(&self, plan: &CommittedPlan<'_, '_>) -> Result<&str> {
        ensure!(
            std::ptr::eq(self.namespace.plan, plan) && self.namespace.root.is_none(),
            "namespace completion differs from original live ownership"
        );
        let nonce = self.job.verify_for(plan)?;
        let binding = &self.namespace.binding;
        ensure!(
            replay(plan.database.connection()?, &plan.plan)?
                == vec![
                    Event::Intent {
                        binding: binding.clone()
                    },
                    Event::Created {
                        binding: binding.clone()
                    },
                    Event::RemoveIntent {
                        binding: binding.clone(),
                        retirement: nonce.to_owned()
                    },
                    Event::Removed {
                        binding: binding.clone(),
                        retirement: nonce.to_owned()
                    },
                ],
            "namespace completion receipt differs from original close"
        );
        native::verify_parent(self.namespace.parent.raw(), &plan.plan)?;
        native::require_absent(self.namespace.parent.raw(), &plan.plan)?;
        Ok(nonce)
    }
}
pub(super) fn birth_nonce(sql: &Connection, plan: &Plan) -> Result<String> {
    let events = replay(sql, plan)?;
    let Some(Event::Created { binding }) = events.get(1) else {
        anyhow::bail!("initializer namespace birth is missing");
    };
    Ok(binding.nonce.clone())
}
impl OwnedNamespace<'_, '_, '_> {
    pub(super) fn initializer_root(
        &self,
        plan: &CommittedPlan<'_, '_>,
    ) -> Result<windows::Win32::Foundation::HANDLE> {
        ensure!(
            std::ptr::eq(self.plan, plan),
            "initializer namespace belongs to another plan"
        );
        plan.verify()?;
        let root = self
            .root
            .as_ref()
            .context("initializer namespace closed")?
            .raw();
        native::verify_identity(root, self.parent.raw(), &plan.plan)?;
        ensure!(
            replay(plan.database.connection()?, &plan.plan)?.len() == 2,
            "initializer namespace is not in original open phase"
        );
        Ok(root)
    }

    fn verify_open(&self) -> Result<()> {
        self.plan.verify()?;
        native::verify_native(
            self.root
                .as_ref()
                .context("namespace original handle closed")?
                .raw(),
            self.parent.raw(),
            &self.plan.plan,
        )
    }
    fn retire(&mut self, completed: &retirement::CompletedJob<'_, '_, '_>) -> Result<()> {
        let nonce = completed.verify_for(self.plan)?.to_owned();
        let sql = self.plan.database.connection()?;
        ensure!(
            replay(sql, &self.plan.plan)?
                == vec![
                    Event::Intent {
                        binding: self.binding.clone()
                    },
                    Event::Created {
                        binding: self.binding.clone()
                    },
                ],
            "namespace retirement requires original exact birth receipts"
        );
        self.verify_open()?;
        append(
            sql,
            &self.plan.plan,
            Event::RemoveIntent {
                binding: self.binding.clone(),
                retirement: nonce.clone(),
            },
        )?;
        #[cfg(test)]
        self.checkpoint("retiring")?;
        self.verify_open()?;
        completed.verify_for(self.plan)?;
        // Consume the original reference before CloseHandle; even an uncertain
        // close is never retried by Drop or by replaying a saved numeric handle.
        let root = self
            .root
            .take()
            .context("namespace close already attempted")?;
        unsafe { CloseHandle(root.into_raw()) }
            .context("namespace original close uncertain; preserve intent")?;
        native::verify_parent(self.parent.raw(), &self.plan.plan)?;
        native::wait_absent(self.parent.raw(), &self.plan.plan)?;
        #[cfg(test)]
        self.checkpoint("closed")?;
        completed.verify_for(self.plan)?;
        append(
            sql,
            &self.plan.plan,
            Event::Removed {
                binding: self.binding.clone(),
                retirement: nonce,
            },
        )?;
        #[cfg(test)]
        self.checkpoint("completed")?;
        Ok(())
    }
    #[cfg(test)]
    fn checkpoint(&self, phase: &str) -> Result<()> {
        NAMESPACE_CHECKPOINT.with(|slot| {
            if let Some(hook) = slot.borrow_mut().as_mut() {
                hook(
                    phase,
                    self.plan.database.connection()?,
                    self.root.as_ref().map(|h| h.raw()),
                )?;
            }
            Ok(())
        })
    }
}
#[cfg(test)]
thread_local! {
    pub(crate) static NAMESPACE_CHECKPOINT: std::cell::RefCell<Option<Box<dyn FnMut(&str, &Connection, Option<windows::Win32::Foundation::HANDLE>)->Result<()>>>> = const { std::cell::RefCell::new(None) };
}
#[cfg(test)]
pub(crate) fn fixture_require_absent(plan: &CommittedPlan<'_, '_>) -> Result<()> {
    let parent = native::open_parent(&plan.plan)?;
    native::require_absent(parent.raw(), &plan.plan)
}
#[cfg(test)]
#[path = "account_bootstrap_namespace_tests.rs"]
mod tests;

#[cfg(test)]
pub(crate) fn fixture_observe_failed_initializer_settlement(
    plan: &CommittedPlan<'_, '_>,
) -> Result<u32> {
    native::fixture_observe_settlement(&plan.plan)
}

#[cfg(test)]
pub(crate) fn fixture_wait_absent(plan: &CommittedPlan<'_, '_>) -> Result<()> {
    let parent = native::open_parent(&plan.plan)?;
    native::wait_absent(parent.raw(), &plan.plan)
}
