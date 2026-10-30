//! Original namespace born empty and retained across both sibling jobs. Only
//! the sealed initializer may admit its fixed populated phase. Cold evidence
//! never authorizes adoption, handle reconstruction, or cleanup by name.
use super::*;
use crate::account_state::execution::resources::namespace_native as root_native;
use windows::Win32::Foundation::CloseHandle;

#[path = "account_paired_host_profile.rs"]
pub(crate) mod host_profile;
#[path = "account_paired_station.rs"]
pub(crate) mod station;

const TABLE: &str = "candidate_paired_namespace";
const POLICY: u32 = 2;
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_paired_namespace(sequence INTEGER PRIMARY KEY CHECK(sequence BETWEEN 0 AND 3), event_json TEXT NOT NULL)",
    "CREATE TRIGGER paired_namespace_no_update BEFORE UPDATE ON candidate_paired_namespace BEGIN SELECT RAISE(ABORT,'paired namespace event is immutable'); END",
    "CREATE TRIGGER paired_namespace_no_delete BEFORE DELETE ON candidate_paired_namespace BEGIN SELECT RAISE(ABORT,'paired namespace event is immutable'); END",
    "CREATE TRIGGER paired_namespace_no_replace BEFORE INSERT ON candidate_paired_namespace WHEN EXISTS(SELECT 1 FROM candidate_paired_namespace WHERE sequence=NEW.sequence) BEGIN SELECT RAISE(ABORT,'paired namespace event is immutable'); END",
];
pub(in crate::account_state::execution::paired_plan) fn create_schema(
    sql: &Connection,
) -> Result<()> {
    for statement in SCHEMA {
        sql.execute_batch(statement)?;
    }
    host_profile::create_schema(sql)?;
    station::create_schema(sql)?;
    station::initializer::create_schema(sql)?;
    Ok(())
}
pub(in crate::account_state::execution::paired_plan) fn require_schema(
    sql: &Connection,
) -> Result<()> {
    let mut s = sql.prepare("SELECT sql FROM sqlite_schema WHERE tbl_name=?1")?;
    let rows: Vec<String> = s
        .query_map([TABLE], |r| r.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    ensure!(
        rows.len() == SCHEMA.len() && SCHEMA.iter().all(|s| rows.contains(&s.to_string())),
        "paired namespace schema differs; preserve state"
    );
    host_profile::require_schema(sql)?;
    station::require_schema(sql)?;
    station::initializer::require_schema(sql)?;
    Ok(())
}
impl root_native::NamespacePolicySource for Plan {
    fn execution(&self) -> &ExecutionRecord {
        &self.execution
    }
    fn session(&self) -> u32 {
        self.session
    }
    fn rpc_policy(&self) -> root_native::rpc::PolicyKind {
        self.rpc_policy
    }
    fn namespace_birth(&self) -> root_native::RootBirthKind {
        self.namespace_birth
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
    #[serde(default, skip_serializing_if = "root_native::RootBirthKind::is_legacy")]
    birth_kind: root_native::RootBirthKind,
}
fn require_new_nonce(plan: &Plan, nonce: &str) -> Result<()> {
    validate_random_id(nonce)?;
    ensure!(
        nonce != plan.operation && nonce != plan.execution.transaction,
        "paired resource nonce aliases operation or execution"
    );
    for role in &plan.roles {
        let address: String = role
            .control_nonce
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        ensure!(
            [
                role.job_nonce.as_str(),
                role.birth_nonce.as_str(),
                role.resume_nonce.as_str(),
                role.retire_nonce.as_str(),
                address.as_str()
            ]
            .into_iter()
            .all(|n| n != nonce),
            "paired resource nonce aliases another purpose"
        );
    }
    Ok(())
}
// A diagnostic binding only; never creates a live namespace guard.
fn birth_nonce(sql: &Connection, plan: &Plan) -> Result<String> {
    let events = replay(sql, plan)?;
    let Some(Event::Created { binding }) = events.get(1) else {
        anyhow::bail!("paired original namespace birth missing");
    };
    Ok(binding.nonce.clone())
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
        retirements: [String; 2],
    },
    Removed {
        binding: Binding,
        retirements: [String; 2],
    },
}
fn retirement_nonces(sql: &Connection, plan: &Plan) -> Result<[String; 2]> {
    for role in [Role::Owner, Role::Consumers] {
        let rows = super::replay(sql, plan, role)?;
        ensure!(
            rows.len() == 9 && rows.last() == Some(&super::Event::Retired),
            "paired namespace requires both complete retirement histories"
        );
    }
    Ok([
        plan.roles[0].retire_nonce.clone(),
        plan.roles[1].retire_nonce.clone(),
    ])
}
fn validate(sql: &Connection, plan: &Plan, events: &[Event]) -> Result<()> {
    ensure!(events.len() <= 4, "paired namespace event bound exceeded");
    if events.is_empty() {
        return Ok(());
    }
    let Event::Intent { binding } = &events[0] else {
        anyhow::bail!("paired namespace intent missing");
    };
    validate_random_id(&binding.nonce)?;
    ensure!(
        binding.operation == plan.operation
            && binding.nonce != plan.operation
            && binding.nonce != plan.execution.transaction
            && binding.session == plan.session
            && binding.package_sid == plan.execution.identity.package_sid
            && binding.policy == POLICY
            && binding.birth_kind == plan.namespace_birth,
        "paired namespace binding differs from original plan"
    );
    for role in &plan.roles {
        let address: String = role
            .control_nonce
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        ensure!(
            [
                &role.job_nonce,
                &role.birth_nonce,
                &role.resume_nonce,
                &role.retire_nonce,
                &address
            ]
            .into_iter()
            .all(|n| n != &binding.nonce),
            "paired namespace nonce aliases another purpose"
        );
    }
    if let Some(event) = events.get(1) {
        ensure!(
            *event
                == Event::Created {
                    binding: binding.clone()
                },
            "paired namespace birth receipt differs"
        );
    }
    if let Some(event) = events.get(2) {
        let retirements = retirement_nonces(sql, plan)?;
        ensure!(
            *event
                == Event::RemoveIntent {
                    binding: binding.clone(),
                    retirements: retirements.clone()
                },
            "paired namespace removal lacks exact completed pair"
        );
        if let Some(event) = events.get(3) {
            ensure!(
                *event
                    == Event::Removed {
                        binding: binding.clone(),
                        retirements
                    },
                "paired namespace removed receipt differs"
            );
        }
    }
    Ok(())
}
fn replay(sql: &Connection, plan: &Plan) -> Result<Vec<Event>> {
    require_plan(sql, plan)?;
    let (count, max): (usize, Option<usize>) = sql.query_row(
        "SELECT count(*),max(length(CAST(event_json AS BLOB))) FROM candidate_paired_namespace",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    ensure!(
        count <= 4 && max.unwrap_or(0) <= 2048,
        "paired namespace inventory exceeds bounds"
    );
    let mut s = sql
        .prepare("SELECT sequence,event_json FROM candidate_paired_namespace ORDER BY sequence")?;
    let mut events = Vec::new();
    for row in s.query_map([], |r| Ok((r.get::<_, usize>(0)?, r.get::<_, String>(1)?)))? {
        let (sequence, bytes) = row?;
        ensure!(
            sequence == events.len(),
            "paired namespace sequence has a gap"
        );
        events.push(serde_json::from_str(&bytes).context("malformed paired namespace event")?);
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
    ensure!(bytes.len() <= 2048, "paired namespace event exceeds bound");
    let tx = sql.unchecked_transaction()?;
    tx.execute(
        "INSERT INTO candidate_paired_namespace VALUES(?1,?2)",
        params![sequence, bytes],
    )?;
    tx.commit()
        .context("paired namespace commit uncertain; preserve pending state")?;
    ensure!(
        replay(sql, plan)? == expected,
        "paired namespace readback differs"
    );
    Ok(())
}

/// Declaration order is security relevant: consumers, then owner drain inside
/// Jobs::drop before the namespace releases either original handle. Keep this
/// bundle intact across every fallible operation, including completion.
pub(crate) struct NamespacedPair<'p, 'a, 'lease> {
    jobs: Jobs<'p, 'a, 'lease>,
    initializer: Option<station::initializer::OwnedInitializer<'p, 'a, 'lease>>,
    profile: Option<host_profile::OwnedHostProfile<'p, 'a, 'lease>>,
    namespace: OwnedNamespace<'p, 'a, 'lease>,
}
struct OwnedNamespace<'p, 'a, 'lease> {
    root: Option<OwnedHandle>,
    #[cfg(test)]
    shadow: Option<root_native::FixtureShadow>,
    parent: OwnedHandle,
    plan: &'p CommittedPlan<'a, 'lease>,
    binding: Binding,
    retirement_attempted: bool,
}
impl<'p, 'a, 'lease> Jobs<'p, 'a, 'lease> {
    fn require_namespace_birth(&self) -> Result<()> {
        self.verify()?;
        for role in [Role::Owner, Role::Consumers] {
            ensure!(
                !self.births[role.index()].get()
                    && !self.retirements[role.index()].get()
                    && accounting(self.job(role))? == (0, 0)
                    && self.prefix(role)? == [super::Event::JobIntent, super::Event::JobCreated],
                "paired namespace requires both fresh original jobs"
            );
        }
        Ok(())
    }
    pub(crate) fn with_empty_namespace(self) -> Result<NamespacedPair<'p, 'a, 'lease>> {
        self.require_namespace_birth()?;
        let plan = self.plan;
        let sql = plan.database.connection()?;
        ensure!(
            replay(sql, &plan.plan)?.is_empty(),
            "paired namespace birth already attempted"
        );
        let parent = root_native::open_parent(&plan.plan)?;
        let binding = Binding {
            operation: plan.plan.operation.clone(),
            nonce: random_id()?,
            session: plan.plan.session,
            package_sid: plan.plan.execution.identity.package_sid.clone(),
            policy: POLICY,
            birth_kind: plan.plan.namespace_birth,
        };
        append(
            sql,
            &plan.plan,
            Event::Intent {
                binding: binding.clone(),
            },
        )?;
        self.require_namespace_birth()?;
        #[cfg(test)]
        let shadow = if plan.plan.namespace_birth
            == root_native::RootBirthKind::SessionShadowDirectoryV1
            || crate::account_control::station_lowbox_fixture::ENABLED.with(|s| s.get())
        {
            Some(root_native::FixtureShadow::open(&plan.plan)?)
        } else {
            None
        };
        #[cfg(test)]
        let root = match &shadow {
            Some(shadow) => root_native::fixture_create_shadow(&plan.plan, parent.raw(), shadow)?,
            None => root_native::create_native(&plan.plan, parent.raw())?,
        };
        #[cfg(not(test))]
        let root = root_native::create_native(&plan.plan, parent.raw())?;
        #[cfg(test)]
        if shadow.is_some()
            && crate::account_control::station_lowbox_fixture::ENABLED.with(|s| s.get())
        {
            crate::account_control::station_lowbox_fixture::SHADOW_BORN.with(|s| s.set(true));
        }
        let bundle = NamespacedPair {
            jobs: self,
            initializer: None,
            profile: None,
            namespace: OwnedNamespace {
                root: Some(root),
                #[cfg(test)]
                shadow,
                parent,
                plan,
                binding: binding.clone(),
                retirement_attempted: false,
            },
        };
        bundle.namespace.checkpoint("born")?;
        bundle.namespace.verify_open()?;
        append(sql, &plan.plan, Event::Created { binding })?;
        bundle.namespace.checkpoint("ready")?;
        bundle.require_open()?;
        Ok(bundle)
    }
}
impl<'p, 'a, 'lease> NamespacedPair<'p, 'a, 'lease> {
    pub(crate) fn with_unused_host_profile(mut self) -> Result<Self> {
        self.require_open()?;
        self.jobs.require_namespace_birth()?;
        ensure!(
            self.profile.is_none(),
            "paired host profile already present"
        );
        self.profile = Some(host_profile::OwnedHostProfile::create(self.jobs.plan)?);
        self.require_open()?;
        Ok(self)
    }
    pub(crate) fn with_host_profile_for_use(self) -> Result<Self> {
        let mut bundle = self.with_unused_host_profile()?;
        bundle
            .profile
            .as_mut()
            .context("paired original host profile missing")?
            .begin_use()?;
        bundle.require_open()?;
        Ok(bundle)
    }
    fn require_open(&self) -> Result<()> {
        ensure!(
            self.initializer.is_none(),
            "initialized pair requires its sealed coordinator"
        );
        ensure!(
            std::ptr::eq(self.jobs.plan, self.namespace.plan),
            "paired namespace belongs to another original plan"
        );
        self.jobs.verify()?;
        self.namespace.verify_open()?;
        if let Some(profile) = &self.profile {
            profile.verify_open()?;
        }
        ensure!(
            replay(
                self.namespace.plan.database.connection()?,
                &self.namespace.plan.plan
            )? == self.namespace.birth_events(),
            "paired namespace original birth receipts differ"
        );
        Ok(())
    }
    pub(crate) fn run_profile_self_check(
        mut self,
        owner: ControlServer,
        consumer: ControlServer,
        timeout: Duration,
    ) -> Result<CompletedNamespacedPair<'p, 'a, 'lease>> {
        self.require_open()?;
        let completed = self.jobs.run_profile_self_check(owner, consumer, timeout)?;
        if let Some(profile) = &mut self.profile {
            profile.retire(&self.jobs, &completed)?;
        }
        self.namespace.retire(&self.jobs, &completed)?;
        let done = CompletedNamespacedPair { bundle: self };
        done.verify()?;
        Ok(done)
    }
    #[cfg(test)]
    pub(crate) fn fixture_run_profile_lifetime(
        mut self,
        owner: ControlServer,
        consumer: ControlServer,
        timeout: Duration,
        password: &str,
    ) -> Result<CompletedNamespacedPair<'p, 'a, 'lease>> {
        self.require_open()?;
        let completed = self
            .jobs
            .fixture_run_profile_lifetime(owner, consumer, timeout, password, 0, true)?;
        if let Some(profile) = &mut self.profile {
            profile.retire(&self.jobs, &completed)?;
        }
        self.namespace.retire(&self.jobs, &completed)?;
        let done = CompletedNamespacedPair { bundle: self };
        done.verify()?;
        Ok(done)
    }
    #[cfg(test)]
    pub(crate) fn fixture_run_host_family(
        mut self,
        owner: ControlServer,
        consumer: ControlServer,
    ) -> Result<CompletedNamespacedPair<'p, 'a, 'lease>> {
        self.require_open()?;
        let owner = self
            .jobs
            .prepare_bootstrap(Role::Owner)?
            .start(owner, Duration::from_secs(5))?;
        let mut consumer = self
            .jobs
            .prepare_family_fixture()?
            .start(consumer, Duration::from_secs(5))?;
        let descendants = consumer.fixture_family_references()?;
        self.namespace.checkpoint("using")?;
        let consumers = self.jobs.retire_consumers(consumer)?;
        let completed = self.jobs.retire_owner(owner, consumers)?;
        for process in descendants {
            ensure!(
                unsafe { WaitForSingleObject(process.raw(), 0) } == WAIT_OBJECT_0,
                "namespace family survived original job retirement"
            );
        }
        if let Some(profile) = &mut self.profile {
            profile.retire(&self.jobs, &completed)?;
        }
        self.namespace.retire(&self.jobs, &completed)?;
        let done = CompletedNamespacedPair { bundle: self };
        done.verify()?;
        Ok(done)
    }
}
/// Constructed only after same-original-pair completion and checked native root
/// closure. Moving the intact bundle cannot turn saved rows into ownership.
pub(crate) struct CompletedNamespacedPair<'p, 'a, 'lease> {
    bundle: NamespacedPair<'p, 'a, 'lease>,
}
impl CompletedNamespacedPair<'_, '_, '_> {
    pub(crate) fn verify(&self) -> Result<()> {
        let ns = &self.bundle.namespace;
        ensure!(
            std::ptr::eq(self.bundle.jobs.plan, ns.plan)
                && ns.root.is_none()
                && ns.retirement_attempted,
            "paired namespace completion differs from original ownership"
        );
        for role in [Role::Owner, Role::Consumers] {
            self.bundle.jobs.verify_empty_retired_role(role)?;
        }
        if let Some(initializer) = &self.bundle.initializer {
            initializer.verify_released(&self.bundle.jobs)?;
        }
        if let Some(profile) = &self.bundle.profile {
            profile.verify_retired(&self.bundle.jobs)?;
        }
        let retirements = retirement_nonces(ns.plan.database.connection()?, &ns.plan.plan)?;
        let mut expected = ns.birth_events();
        expected.push(Event::RemoveIntent {
            binding: ns.binding.clone(),
            retirements: retirements.clone(),
        });
        expected.push(Event::Removed {
            binding: ns.binding.clone(),
            retirements,
        });
        ensure!(
            replay(ns.plan.database.connection()?, &ns.plan.plan)? == expected,
            "paired namespace completion receipt differs"
        );
        root_native::verify_parent(ns.parent.raw(), &ns.plan.plan)?;
        root_native::require_absent(ns.parent.raw(), &ns.plan.plan)
    }
}
impl OwnedNamespace<'_, '_, '_> {
    fn birth_events(&self) -> Vec<Event> {
        vec![
            Event::Intent {
                binding: self.binding.clone(),
            },
            Event::Created {
                binding: self.binding.clone(),
            },
        ]
    }
    fn verify_open(&self) -> Result<()> {
        self.plan.verify()?;
        #[cfg(test)]
        if let Some(shadow) = &self.shadow {
            shadow.verify(&self.plan.plan)?;
        }
        root_native::verify_native(
            self.root
                .as_ref()
                .context("paired namespace original root closed")?
                .raw(),
            self.parent.raw(),
            &self.plan.plan,
        )
    }
    fn retire(
        &mut self,
        jobs: &Jobs<'_, '_, '_>,
        completed: &CompletedPair<'_, '_, '_, '_>,
    ) -> Result<()> {
        ensure!(
            !std::mem::replace(&mut self.retirement_attempted, true),
            "paired namespace retirement already attempted"
        );
        ensure!(
            std::ptr::eq(completed.jobs, jobs) && std::ptr::eq(jobs.plan, self.plan),
            "paired namespace completion belongs to another original pair"
        );
        completed.verify()?;
        let sql = self.plan.database.connection()?;
        ensure!(
            replay(sql, &self.plan.plan)? == self.birth_events(),
            "paired namespace retirement requires original birth receipts"
        );
        let retirements = retirement_nonces(sql, &self.plan.plan)?;
        self.verify_open()?;
        self.checkpoint("before-retire")?;
        append(
            sql,
            &self.plan.plan,
            Event::RemoveIntent {
                binding: self.binding.clone(),
                retirements: retirements.clone(),
            },
        )?;
        self.checkpoint("retiring")?;
        self.verify_open()?;
        completed.verify()?;
        // Consume before attempting close. Neither Drop nor a saved integer may
        // retry an uncertain close, and no completion exists on any error.
        let root = self
            .root
            .take()
            .context("paired namespace close already attempted")?;
        unsafe { CloseHandle(root.into_raw()) }
            .context("paired namespace original close uncertain; preserve intent")?;
        root_native::verify_parent(self.parent.raw(), &self.plan.plan)?;
        root_native::wait_absent(self.parent.raw(), &self.plan.plan)?;
        self.checkpoint("closed")?;
        completed.verify()?;
        append(
            sql,
            &self.plan.plan,
            Event::Removed {
                binding: self.binding.clone(),
                retirements,
            },
        )?;
        self.checkpoint("completed")?;
        Ok(())
    }
    fn checkpoint(&self, phase: &str) -> Result<()> {
        #[cfg(test)]
        NAMESPACE_CHECKPOINT.with(|slot| -> Result<()> {
            if let Some(hook) = slot.borrow_mut().as_mut() {
                hook(
                    phase,
                    self.plan.database.connection()?,
                    self.root.as_ref().map(|h| h.raw()),
                )?;
            }
            Ok(())
        })?;
        #[cfg(not(test))]
        let _ = phase;
        Ok(())
    }
}
#[cfg(test)]
thread_local! {
    pub(crate) static NAMESPACE_CHECKPOINT: std::cell::RefCell<Option<Box<dyn FnMut(&str, &Connection, Option<HANDLE>) -> Result<()>>>> = const { std::cell::RefCell::new(None) };
}
#[cfg(test)]
pub(crate) fn fixture_require_absent(plan: &CommittedPlan<'_, '_>) -> Result<()> {
    fixture_require_absent_plan(&plan.plan)
}
/// Read-only post-crash diagnostic. Neither absence nor parsed rows authorize
/// ownership reconstruction, native mutation or a completed journal receipt.
#[cfg(test)]
pub(in crate::account_state::execution) fn fixture_require_absent_plan(plan: &Plan) -> Result<()> {
    let parent = root_native::open_parent(plan)?;
    root_native::require_absent(parent.raw(), plan)
}
#[cfg(test)]
#[path = "account_paired_namespace_tests.rs"]
mod tests;
