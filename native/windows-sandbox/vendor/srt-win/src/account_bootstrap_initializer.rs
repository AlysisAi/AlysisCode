//! Single-job ordinary AppContainer initialization. The fixed system child is
//! born suspended and never resumed. Its original query token and a query-only
//! private RPC directory pin survive;
//! explicit token release requires the same live CompletedJob. A durable intent
//! precedes one private RPC account DACL publication. No alias-target grant,
//! command execution or cold ownership reconstruction is admitted.
use super::super::{
    desktop, host_profile, initializer_image::SystemInitializerImage, namespace, station,
};
use super::*;
use crate::account_control::bootstrap::entry::initializer_wire;
use crate::account_state::execution::resources::namespace_native::inventory::{
    DirectoryEntry, InitializerLink, observe_children, observe_initializer_link,
};
use crate::account_state::execution::resources::namespace_native::rpc::{
    AccessPolicy, RetainedRpc,
};
use resume::logon::delivery::birth::retirement::CompletedJob;
use std::time::Duration;
use windows::Win32::{Foundation::*, Security::*, System::Threading::*};

const TABLE: &str = "candidate_atomic_initializer";
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_atomic_initializer(sequence INTEGER PRIMARY KEY CHECK(sequence BETWEEN 0 AND 6), event_json TEXT NOT NULL)",
    "CREATE TRIGGER atomic_initializer_no_update BEFORE UPDATE ON candidate_atomic_initializer BEGIN SELECT RAISE(ABORT,'initializer event is immutable'); END",
    "CREATE TRIGGER atomic_initializer_no_delete BEFORE DELETE ON candidate_atomic_initializer BEGIN SELECT RAISE(ABORT,'initializer event is immutable'); END",
    "CREATE TRIGGER atomic_initializer_no_replace BEFORE INSERT ON candidate_atomic_initializer WHEN EXISTS(SELECT 1 FROM candidate_atomic_initializer WHERE sequence=NEW.sequence) BEGIN SELECT RAISE(ABORT,'initializer event is immutable'); END",
];
pub(in crate::account_state::execution::bootstrap_plan) fn create_schema(
    sql: &Connection,
) -> Result<()> {
    for statement in SCHEMA {
        sql.execute_batch(statement)?;
    }
    Ok(())
}
pub(in crate::account_state::execution::bootstrap_plan) fn require_schema(
    sql: &Connection,
) -> Result<()> {
    let mut s = sql.prepare("SELECT sql FROM sqlite_schema WHERE tbl_name=?1")?;
    let rows: Vec<String> = s
        .query_map([TABLE], |r| r.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    ensure!(
        rows.len() == SCHEMA.len() && SCHEMA.iter().all(|s| rows.iter().any(|r| r == s)),
        "initializer schema differs; preserve state"
    );
    Ok(())
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Binding {
    operation: String,
    nonce: String,
    bootstrap_pid: u32,
    bootstrap_created: u64,
    namespace: String,
    profile_use: String,
    desktop: String,
    station: String,
    image_id: [u8; 24],
    policy: u32,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Birth {
    pid: u32,
    created: u64,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum InitEvent {
    Intent {
        binding: Binding,
    },
    Born {
        binding: Binding,
        birth: Birth,
    },
    Stopped {
        binding: Binding,
        birth: Birth,
    },
    RpcIntent {
        binding: Binding,
        policy: AccessPolicy,
    },
    RpcGranted {
        binding: Binding,
        policy: AccessPolicy,
    },
    ReleaseIntent {
        binding: Binding,
        retirement: String,
    },
    Released {
        binding: Binding,
        retirement: String,
    },
}
fn validate(sql: &Connection, plan: &Plan, events: &[InitEvent]) -> Result<()> {
    ensure!(events.len() <= 7, "initializer event bound exceeded");
    if events.is_empty() {
        return Ok(());
    }
    let InitEvent::Intent { binding } = &events[0] else {
        anyhow::bail!("initializer intent missing");
    };
    validate_random_id(&binding.nonce)?;
    let bootstrap = super::replay(sql, plan)?;
    let Some(Event::BootstrapCreated { pid, created, .. }) = bootstrap.get(3) else {
        anyhow::bail!("initializer requires recorded original suspended bootstrap");
    };
    let resources = [
        namespace::birth_nonce(sql, plan)?,
        host_profile::use_nonce(sql, plan)?,
        desktop::birth_nonce(sql, plan)?,
        station::grant_nonce(sql, plan)?,
    ];
    ensure!(
        binding.operation == plan.operation
            && binding.nonce != plan.operation
            && binding.policy == 4
            && binding.bootstrap_pid == *pid
            && binding.bootstrap_created == *created
            && binding.image_id != [0; 24]
            && [
                binding.namespace.clone(),
                binding.profile_use.clone(),
                binding.desktop.clone(),
                binding.station.clone()
            ] == resources
            && resources.iter().all(|n| n != &binding.nonce),
        "initializer binding differs from original plan/resources"
    );
    if let Some(event) = events.get(1) {
        let InitEvent::Born { binding: b, birth } = event else {
            anyhow::bail!("initializer birth missing");
        };
        ensure!(
            b == binding
                && birth.pid != 0
                && birth.pid != plan.execution.pid
                && birth.pid != *pid
                && birth.created != 0,
            "initializer birth differs from original intent"
        );
        if let Some(event) = events.get(2) {
            ensure!(
                *event
                    == InitEvent::Stopped {
                        binding: binding.clone(),
                        birth: birth.clone()
                    },
                "initializer stopped receipt differs"
            );
        }
    }
    if let Some(event) = events.get(3) {
        let InitEvent::RpcIntent { binding: b, policy } = event else {
            anyhow::bail!("initializer RPC grant intent missing");
        };
        ensure!(b == binding, "initializer RPC binding differs");
        policy.validate(plan)?;
        if let Some(event) = events.get(4) {
            ensure!(
                *event
                    == InitEvent::RpcGranted {
                        binding: binding.clone(),
                        policy: policy.clone()
                    },
                "initializer RPC grant receipt differs"
            );
        }
    }
    if let Some(event) = events.get(5) {
        let retirement = resume::logon::delivery::birth::retirement::completed_nonce(sql, plan)?;
        ensure!(
            *event
                == InitEvent::ReleaseIntent {
                    binding: binding.clone(),
                    retirement: retirement.clone()
                },
            "initializer release lacks exact completed job"
        );
        if let Some(event) = events.get(6) {
            ensure!(
                *event
                    == InitEvent::Released {
                        binding: binding.clone(),
                        retirement
                    },
                "initializer released receipt differs"
            );
        }
    }
    Ok(())
}
fn replay(sql: &Connection, plan: &Plan) -> Result<Vec<InitEvent>> {
    require_plan(sql, plan)?;
    let (count, max): (usize, Option<usize>) = sql.query_row(
        "SELECT count(*),max(length(CAST(event_json AS BLOB))) FROM candidate_atomic_initializer",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    ensure!(
        count <= 7 && max.unwrap_or(0) <= 16384,
        "initializer inventory exceeds bounds"
    );
    let mut s = sql.prepare(
        "SELECT sequence,event_json FROM candidate_atomic_initializer ORDER BY sequence",
    )?;
    let mut events = Vec::new();
    for row in s.query_map([], |r| Ok((r.get::<_, usize>(0)?, r.get::<_, String>(1)?)))? {
        let (index, bytes) = row?;
        ensure!(index == events.len(), "initializer sequence has a gap");
        events.push(serde_json::from_str(&bytes).context("malformed initializer event")?);
    }
    validate(sql, plan, &events)?;
    Ok(events)
}
fn append(sql: &Connection, plan: &Plan, event: InitEvent) -> Result<()> {
    let mut events = replay(sql, plan)?;
    let index = events.len();
    events.push(event.clone());
    validate(sql, plan, &events)?;
    let bytes = serde_json::to_string(&event)?;
    ensure!(bytes.len() <= 16384, "initializer event exceeds bound");
    let tx = sql.unchecked_transaction()?;
    tx.execute(
        "INSERT INTO candidate_atomic_initializer VALUES(?1,?2)",
        params![index, bytes],
    )?;
    tx.commit()
        .context("initializer commit uncertain; preserve intent and original guards")?;
    ensure!(replay(sql, plan)? == events, "initializer readback differs");
    Ok(())
}
struct Live {
    rpc: Option<RetainedRpc>,
    access: Option<AccessPolicy>,
    token: Option<OwnedHandle>,
    image: SystemInitializerImage,
    binding: Binding,
    birth: Birth,
}
/// A borrowed view of four retained original resources. The containing caller
/// owns/consumes the job before this view is dropped, including on failures.
/// Drop only closes remaining handles; it never claims journaled retirement.
pub(in crate::account_state::execution::bootstrap_plan) struct Initialization<'r, 'p, 'a, 'lease> {
    live: Option<Live>,
    attempted: bool,
    plan: &'p CommittedPlan<'a, 'lease>,
    namespace: &'r namespace::OwnedNamespace<'p, 'a, 'lease>,
    profile: &'r host_profile::OwnedHostProfile<'p, 'a, 'lease>,
    desktop: &'r desktop::OwnedDesktop<'p, 'a, 'lease>,
    station: &'r station::OwnedStation<'p, 'a, 'lease>,
}
impl<'r, 'p, 'a, 'lease> Initialization<'r, 'p, 'a, 'lease> {
    pub(in crate::account_state::execution::bootstrap_plan) fn prepare(
        plan: &'p CommittedPlan<'a, 'lease>,
        namespace: &'r namespace::OwnedNamespace<'p, 'a, 'lease>,
        profile: &'r host_profile::OwnedHostProfile<'p, 'a, 'lease>,
        desktop: &'r desktop::OwnedDesktop<'p, 'a, 'lease>,
        station: &'r station::OwnedStation<'p, 'a, 'lease>,
    ) -> Result<Self> {
        let value = Self {
            live: None,
            attempted: false,
            plan,
            namespace,
            profile,
            desktop,
            station,
        };
        value.verify_resources()?;
        ensure!(
            observe_children(namespace.initializer_root(plan)?)?.is_empty(),
            "initializer requires original empty namespace"
        );
        ensure!(
            replay(plan.database.connection()?, &plan.plan)?.is_empty(),
            "initializer was already attempted"
        );
        Ok(value)
    }
    fn verify_resources(&self) -> Result<()> {
        self.plan.verify()?;
        self.namespace.initializer_root(self.plan)?;
        self.profile.require_initializer(self.plan)?;
        self.desktop.initializer_name(self.plan)?;
        self.station.require_initializer(self.plan)
    }
    pub(in crate::account_state::execution::bootstrap_plan) fn begin(
        &mut self,
        running: &mut resume::RunningBootstrap<'_, 'p, 'a, 'lease>,
    ) -> Result<()> {
        let bootstrap = &running.birth;
        ensure!(
            !std::mem::replace(&mut self.attempted, true),
            "initializer birth already attempted"
        );
        ensure!(
            std::ptr::eq(bootstrap.owner.plan, self.plan),
            "initializer job belongs to another plan"
        );
        bootstrap.record_birth()?;
        self.verify_resources()?;
        let (bootstrap_pid, bootstrap_created, _) = bootstrap.child.durable_observation()?;
        let image = SystemInitializerImage::prepare(&self.plan.plan.execution.owner)?;
        let sql = self.plan.database.connection()?;
        let plan = &self.plan.plan;
        let binding = Binding {
            operation: plan.operation.clone(),
            nonce: random_id()?,
            bootstrap_pid,
            bootstrap_created,
            namespace: namespace::birth_nonce(sql, plan)?,
            profile_use: host_profile::use_nonce(sql, plan)?,
            desktop: desktop::birth_nonce(sql, plan)?,
            station: station::grant_nonce(sql, plan)?,
            image_id: image.identity()?,
            policy: 4,
        };
        ensure!(
            replay(sql, plan)?.is_empty(),
            "initializer intent already exists"
        );
        append(
            sql,
            plan,
            InitEvent::Intent {
                binding: binding.clone(),
            },
        )?;
        #[cfg(test)]
        self.checkpoint("intent")?;
        self.verify_resources()?;
        bootstrap.child.durable_observation()?;
        bootstrap.owner.verify()?;
        ensure!(
            observe_children(self.namespace.initializer_root(self.plan)?)?.is_empty(),
            "initializer namespace changed before birth"
        );
        let request = initializer_wire::Request {
            operation: plan.operation.clone(),
            nonce: binding.nonce.clone(),
            bootstrap_pid,
            bootstrap_created,
            container: plan.execution.identity.container.clone(),
            package_sid: plan.execution.identity.package_sid.clone(),
            owner: plan.execution.owner.clone(),
            session: plan.session,
            image_id: image.identity()?,
            #[cfg(test)]
            omit_profile_environment: super::super::initializer_image::OMIT_PROFILE_ENVIRONMENT
                .with(|s| s.get()),
        };
        running
            ._channel
            .send(&request.encode()?, Duration::from_secs(10))?;
        let process =
            initializer_wire::receive_process(&bootstrap.child, &mut running._channel, &request)?;
        let mut assigned = false.into();
        unsafe {
            windows::Win32::System::JobObjects::IsProcessInJob(
                process.raw(),
                Some(bootstrap.owner.job.raw()),
                &mut assigned,
            )?;
        }
        ensure!(
            assigned.as_bool(),
            "initializer was not born in original job"
        );
        image.verify_process(process.raw())?;
        let birth = Birth {
            pid: unsafe { GetProcessId(process.raw()) },
            created: process_created(process.raw())?,
        };
        let mut session = 0;
        unsafe {
            windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(birth.pid, &mut session)?;
        }
        ensure!(
            session == plan.session
                && unsafe { WaitForSingleObject(process.raw(), 0) } == WAIT_TIMEOUT,
            "initializer session/liveness differs"
        );
        let mut token = HANDLE::default();
        unsafe {
            OpenProcessToken(process.raw(), TOKEN_QUERY, &mut token)?;
        }
        let token = OwnedHandle(token);
        verify_token(token.raw(), plan)?;
        // Retain the keeper before any fallible receipt. Every outer failure
        // drains the owning job before this view can release its resources.
        self.live = Some(Live {
            rpc: None,
            access: None,
            token: Some(token),
            image,
            binding: binding.clone(),
            birth: birth.clone(),
        });
        #[cfg(test)]
        self.checkpoint("born")?;
        append(
            sql,
            plan,
            InitEvent::Born {
                binding: binding.clone(),
                birth: birth.clone(),
            },
        )?;
        running._channel.send(
            &request.frame("stop", birth.pid, birth.created)?,
            Duration::from_secs(10),
        )?;
        ensure!(
            running._channel.receive(Duration::from_secs(10))?
                == request.frame("stopped", birth.pid, birth.created)?,
            "initializer source stop acknowledgment differs"
        );
        ensure!(
            unsafe { WaitForSingleObject(process.raw(), 5000) } == WAIT_OBJECT_0,
            "initializer original process termination unverified"
        );
        unsafe { CloseHandle(process.into_raw()) }
            .context("initializer query process close uncertain")?;
        // The OS-created directory is opened only relative to our retained
        // private root, after original process exit. Keep this original query
        // pin across account birth and whole-job retirement; never open Global.
        self.live.as_mut().unwrap().rpc = Some(RetainedRpc::open(
            self.namespace.initializer_root(self.plan)?,
        )?);
        #[cfg(test)]
        self.checkpoint("stopped")?;
        append(
            sql,
            plan,
            InitEvent::Stopped {
                binding: binding.clone(),
                birth,
            },
        )?;
        let root = self.namespace.initializer_root(self.plan)?;
        let policy = self
            .live
            .as_ref()
            .unwrap()
            .rpc
            .as_ref()
            .unwrap()
            .prepare_account_access(root, plan)?;
        #[cfg(test)]
        self.checkpoint("rpc-intent")?;
        append(
            sql,
            plan,
            InitEvent::RpcIntent {
                binding: binding.clone(),
                policy: policy.clone(),
            },
        )?;
        // Revalidate the original resources after the durable intent/readback.
        // Any error consumes the whole job before this original RPC pin drops.
        self.verify_resources()?;
        self.live
            .as_mut()
            .unwrap()
            .rpc
            .as_mut()
            .unwrap()
            .grant_account(root, plan, &policy)?;
        self.live.as_mut().unwrap().access = Some(policy.clone());
        #[cfg(test)]
        self.checkpoint("rpc-granted")?;
        append(sql, plan, InitEvent::RpcGranted { binding, policy })?;
        self.verify_ready()?;
        #[cfg(test)]
        self.checkpoint("ready")?;
        self.verify_ready()
    }
    pub(in crate::account_state::execution::bootstrap_plan) fn verify_ready(&self) -> Result<()> {
        self.verify_resources()?;
        let live = self
            .live
            .as_ref()
            .context("initializer original keeper missing")?;
        live.image.application()?;
        verify_token(
            live.token
                .as_ref()
                .context("initializer original token closed")?
                .raw(),
            &self.plan.plan,
        )?;
        ensure!(
            replay(self.plan.database.connection()?, &self.plan.plan)?
                == vec![
                    InitEvent::Intent {
                        binding: live.binding.clone()
                    },
                    InitEvent::Born {
                        binding: live.binding.clone(),
                        birth: live.birth.clone()
                    },
                    InitEvent::Stopped {
                        binding: live.binding.clone(),
                        birth: live.birth.clone()
                    },
                    InitEvent::RpcIntent {
                        binding: live.binding.clone(),
                        policy: live.access.as_ref().context("RPC policy missing")?.clone()
                    },
                    InitEvent::RpcGranted {
                        binding: live.binding.clone(),
                        policy: live.access.as_ref().context("RPC grant missing")?.clone()
                    }
                ],
            "initializer receipts differ before use"
        );
        let root = self.namespace.initializer_root(self.plan)?;
        live.rpc
            .as_ref()
            .context("original initializer RPC pin missing")?
            .verify_account_access(
                root,
                &self.plan.plan,
                live.access
                    .as_ref()
                    .context("original RPC policy missing")?,
            )?;
        let entries = observe_children(root)?;
        let links = [
            observe_initializer_link(root, InitializerLink::Global)?,
            observe_initializer_link(root, InitializerLink::Local)?,
            observe_initializer_link(root, InitializerLink::Session)?,
        ];
        verify_namespace_observation(&entries, &links, &self.plan.plan)?;
        Ok(())
    }
    #[cfg(test)]
    pub(in crate::account_state::execution::bootstrap_plan) fn rpc_probe_purpose(
        &self,
    ) -> Result<crate::account_control::bootstrap::entry::credential_wire::child::Purpose> {
        self.verify_ready()?;
        Ok(crate::account_control::bootstrap::entry::credential_wire::child::Purpose::TrustedRpcSelfCheckV1 {
            package_sid: self.plan.plan.execution.identity.package_sid.clone(),
            initializer: self.live.as_ref().unwrap().binding.nonce.clone(),
        })
    }
    pub(in crate::account_state::execution::bootstrap_plan) fn verify_rpc_probe(
        &self,
        purpose: &crate::account_control::bootstrap::entry::credential_wire::child::Purpose,
    ) -> Result<()> {
        self.verify_ready()?;
        let crate::account_control::bootstrap::entry::credential_wire::child::Purpose::TrustedRpcSelfCheckV1 { package_sid, initializer } = purpose else {
            anyhow::bail!("RPC probe purpose differs");
        };
        ensure!(
            package_sid == &self.plan.plan.execution.identity.package_sid
                && initializer == &self.live.as_ref().unwrap().binding.nonce,
            "RPC probe differs from retained initializer"
        );
        Ok(())
    }
    pub(in crate::account_state::execution::bootstrap_plan) fn release(
        &mut self,
        completed: &CompletedJob<'_, '_, '_>,
    ) -> Result<()> {
        let retirement = completed.verify_for(self.plan)?.to_owned();
        self.verify_ready()?;
        let binding = self.live.as_ref().unwrap().binding.clone();
        append(
            self.plan.database.connection()?,
            &self.plan.plan,
            InitEvent::ReleaseIntent {
                binding: binding.clone(),
                retirement: retirement.clone(),
            },
        )?;
        #[cfg(test)]
        self.checkpoint("releasing")?;
        completed.verify_for(self.plan)?;
        self.verify_resources()?;
        self.live
            .as_mut()
            .unwrap()
            .rpc
            .take()
            .context("initializer RPC close already attempted")?
            .close()?;
        let token = self
            .live
            .as_mut()
            .unwrap()
            .token
            .take()
            .context("initializer close already attempted")?;
        unsafe { CloseHandle(token.into_raw()) }
            .context("initializer token close uncertain; preserve intent")?;
        #[cfg(test)]
        self.checkpoint("closed")?;
        completed.verify_for(self.plan)?;
        append(
            self.plan.database.connection()?,
            &self.plan.plan,
            InitEvent::Released {
                binding,
                retirement,
            },
        )?;
        #[cfg(test)]
        self.checkpoint("completed")?;
        Ok(())
    }
    #[cfg(test)]
    fn checkpoint(&self, phase: &str) -> Result<()> {
        INITIALIZER_CHECKPOINT.with(|s| {
            if let Some(hook) = s.borrow_mut().as_mut() {
                hook(phase, self.plan.database.connection()?)?;
            }
            Ok(())
        })
    }
}
// Exact diagnostic policy only. In particular, Global names a SHARED target;
// none of these strings grants permission to open a target or modify its ACL.
fn verify_namespace_observation(
    entries: &[DirectoryEntry],
    links: &[String; 3],
    plan: &Plan,
) -> Result<()> {
    let expected: Vec<_> = [
        ("Global", "SymbolicLink"),
        ("Local", "SymbolicLink"),
        ("RPC Control", "Directory"),
        ("Session", "SymbolicLink"),
    ]
    .into_iter()
    .map(|(name, kind)| DirectoryEntry {
        name: name.into(),
        kind: kind.into(),
    })
    .collect();
    ensure!(
        entries == expected,
        "initializer namespace children differ: {entries:?}"
    );
    let private = format!(
        "\\Sessions\\{}\\AppContainerNamedObjects\\{}",
        plan.session, plan.execution.identity.package_sid
    );
    ensure!(
        links == &["\\BaseNamedObjects".to_owned(), private.clone(), private],
        "initializer namespace aliases differ: {links:?}"
    );
    Ok(())
}

fn verify_token(token: HANDLE, plan: &Plan) -> Result<()> {
    crate::account_impersonation::require_process_identity()?;
    crate::account_namespace::verify_noninheritable(token)?;
    crate::account_profile_registry::verify_profile_token(
        token,
        &crate::sid::sid_bytes(&plan.execution.identity.package_sid)?,
        &crate::sid::sid_bytes(&plan.execution.owner)?,
    )
}
#[cfg(test)]
thread_local! {pub(crate) static INITIALIZER_CHECKPOINT:std::cell::RefCell<Option<Box<dyn FnMut(&str,&Connection)->Result<()>>>>=const{std::cell::RefCell::new(None)};}

#[cfg(test)]
#[path = "account_bootstrap_initializer_tests.rs"]
mod tests;

#[cfg(test)]
thread_local! {pub(crate) static RPC_SELF_CHECK:std::cell::Cell<bool>=const{std::cell::Cell::new(false)};}
