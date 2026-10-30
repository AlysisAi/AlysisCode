//! A never-resumed initializer in the original consumer job. Its original
//! token and private RPC pin remain in the intact resource bundle, after Jobs
//! and before host profile/root in declaration order. Saved rows grant no
//! ownership, dispatch, retry or cold cleanup authority.
use super::*;
use crate::account_control::bootstrap::entry::initializer_wire::{self, paired};
use crate::account_state::execution::bootstrap_plan::initializer_image::SystemInitializerImage;
use crate::account_state::execution::resources::namespace_native::{
    inventory::{DirectoryEntry, InitializerLink, observe_children, observe_initializer_link},
    rpc::{AccessPolicy, RetainedRpc},
};
use windows::Win32::{Security::*, System::Threading::*};

const TABLE: &str = "candidate_paired_initializer";
const MAX_EVENT: usize = 16384;
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_paired_initializer(sequence INTEGER PRIMARY KEY CHECK(sequence BETWEEN 0 AND 6), event_json TEXT NOT NULL)",
    "CREATE TRIGGER paired_initializer_no_update BEFORE UPDATE ON candidate_paired_initializer BEGIN SELECT RAISE(ABORT,'paired initializer event is immutable'); END",
    "CREATE TRIGGER paired_initializer_no_delete BEFORE DELETE ON candidate_paired_initializer BEGIN SELECT RAISE(ABORT,'paired initializer event is immutable'); END",
    "CREATE TRIGGER paired_initializer_no_replace BEFORE INSERT ON candidate_paired_initializer WHEN EXISTS(SELECT 1 FROM candidate_paired_initializer WHERE sequence=NEW.sequence) BEGIN SELECT RAISE(ABORT,'paired initializer event is immutable'); END",
];
pub(in crate::account_state::execution::paired_plan) fn create_schema(
    sql: &Connection,
) -> Result<()> {
    for statement in SCHEMA {
        sql.execute_batch(statement)?;
    }
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
        rows.len() == SCHEMA.len() && SCHEMA.iter().all(|s| rows.iter().any(|r| r == s)),
        "paired initializer schema differs; preserve state"
    );
    Ok(())
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Birth {
    pid: u32,
    created: u64,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum Event {
    Intent {
        request: paired::Request,
    },
    Born {
        request: paired::Request,
        birth: Birth,
    },
    Stopped {
        request: paired::Request,
        birth: Birth,
    },
    RpcIntent {
        request: paired::Request,
        policy: AccessPolicy,
    },
    RpcGranted {
        request: paired::Request,
        policy: AccessPolicy,
    },
    ReleaseIntent {
        request: paired::Request,
        retirements: [String; 2],
    },
    Released {
        request: paired::Request,
        retirements: [String; 2],
    },
}

fn resources(sql: &Connection, p: &Plan) -> Result<paired::Resources> {
    let profile =
        host_profile::resource_binding(sql, p)?.context("initializer host profile absent")?;
    Ok(paired::Resources {
        owner_job: p.roles[Role::Owner.index()].job_nonce.clone(),
        consumer_job: p.roles[Role::Consumers.index()].job_nonce.clone(),
        namespace: super::super::birth_nonce(sql, p)?,
        host_profile: profile.birth,
        host_profile_use: profile
            .use_nonce
            .context("initializer requires host profile use intent")?,
        station: super::grant_nonce(sql, p)?,
        desktop: desktop::birth_nonce(sql, p)?,
    })
}
fn validate(sql: &Connection, p: &Plan, events: &[Event]) -> Result<()> {
    ensure!(events.len() <= 7, "paired initializer event bound exceeded");
    if events.is_empty() {
        return Ok(());
    }
    let Event::Intent { request } = &events[0] else {
        anyhow::bail!("paired initializer intent absent")
    };
    request.validate()?;
    super::super::require_new_nonce(p, &request.subject.nonce)?;
    let owner = crate::account_state::execution::paired_plan::replay(sql, p, Role::Owner)?;
    ensure!(
        owner.get(6) == Some(&crate::account_state::execution::paired_plan::Event::Ready),
        "paired initializer owner has no durable readiness"
    );
    let source = crate::account_state::execution::paired_plan::replay(sql, p, Role::Consumers)?;
    let Some(crate::account_state::execution::paired_plan::Event::Born { pid, created, .. }) =
        source.get(3)
    else {
        anyhow::bail!("paired initializer original consumer birth absent")
    };
    ensure!(
        source.get(6) == Some(&crate::account_state::execution::paired_plan::Event::Ready),
        "paired initializer consumer has no durable readiness"
    );
    let b = &request.subject;
    ensure!(
        b.operation == p.operation
            && b.bootstrap_pid == *pid
            && b.bootstrap_created == *created
            && b.owner == p.execution.owner
            && b.session == p.session
            && b.container == p.execution.identity.container
            && b.package_sid == p.execution.identity.package_sid
            && request.resources == resources(sql, p)?,
        "paired initializer original bindings differ"
    );
    if let Some(event) = events.get(1) {
        let Event::Born {
            request: actual,
            birth,
        } = event
        else {
            anyhow::bail!("paired initializer birth absent")
        };
        ensure!(
            actual == request
                && birth.pid != 0
                && birth.pid != b.bootstrap_pid
                && birth.pid != p.execution.pid
                && birth.created >= b.bootstrap_created,
            "paired initializer child differs"
        );
        if let Some(event) = events.get(2) {
            ensure!(
                *event
                    == Event::Stopped {
                        request: request.clone(),
                        birth: birth.clone()
                    },
                "paired initializer stopped receipt differs"
            );
        }
    }
    if let Some(event) = events.get(3) {
        let Event::RpcIntent {
            request: actual,
            policy,
        } = event
        else {
            anyhow::bail!("paired initializer RPC intent absent")
        };
        ensure!(actual == request, "paired initializer RPC binding differs");
        policy.validate(p)?;
        if let Some(event) = events.get(4) {
            ensure!(
                *event
                    == Event::RpcGranted {
                        request: request.clone(),
                        policy: policy.clone()
                    },
                "paired initializer RPC receipt differs"
            );
        }
    }
    if let Some(event) = events.get(5) {
        let retirements = super::super::retirement_nonces(sql, p)?;
        ensure!(
            *event
                == Event::ReleaseIntent {
                    request: request.clone(),
                    retirements: retirements.clone()
                },
            "paired initializer release lacks both original retirement histories"
        );
        if let Some(event) = events.get(6) {
            ensure!(
                *event
                    == Event::Released {
                        request: request.clone(),
                        retirements
                    },
                "paired initializer release receipt differs"
            );
        }
    }
    Ok(())
}
fn replay(sql: &Connection, p: &Plan) -> Result<Vec<Event>> {
    require_plan(sql, p)?;
    let (count, max): (usize, Option<usize>) = sql.query_row(
        "SELECT count(*),max(length(CAST(event_json AS BLOB))) FROM candidate_paired_initializer",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    ensure!(
        count <= 7 && max.unwrap_or(0) <= MAX_EVENT,
        "paired initializer inventory exceeds bound"
    );
    let mut s = sql.prepare(
        "SELECT sequence,event_json FROM candidate_paired_initializer ORDER BY sequence",
    )?;
    let mut events = Vec::new();
    for row in s.query_map([], |r| Ok((r.get::<_, usize>(0)?, r.get::<_, String>(1)?)))? {
        let (index, bytes) = row?;
        ensure!(
            index == events.len(),
            "paired initializer sequence has a gap"
        );
        events.push(serde_json::from_str(&bytes).context("malformed paired initializer event")?);
    }
    validate(sql, p, &events)?;
    Ok(events)
}
fn append(sql: &Connection, p: &Plan, event: Event) -> Result<()> {
    let mut events = replay(sql, p)?;
    let sequence = events.len();
    events.push(event.clone());
    validate(sql, p, &events)?;
    let bytes = serde_json::to_string(&event)?;
    ensure!(
        bytes.len() <= MAX_EVENT,
        "paired initializer event exceeds bound"
    );
    let tx = sql.unchecked_transaction()?;
    tx.execute(
        "INSERT INTO candidate_paired_initializer VALUES(?1,?2)",
        params![sequence, bytes],
    )?;
    tx.commit()
        .context("paired initializer commit uncertain; preserve original guards and intent")?;
    ensure!(
        replay(sql, p)? == events,
        "paired initializer readback differs"
    );
    Ok(())
}

struct Live {
    process: Option<OwnedHandle>,
    rpc: Option<RetainedRpc>,
    token: Option<OwnedHandle>,
    image: SystemInitializerImage,
    birth: Option<Birth>,
    policy: Option<AccessPolicy>,
}
impl Drop for Live {
    fn drop(&mut self) {
        // Both original Jobs precede this guard in the intact pair. Accounting
        // zero alone is not process signaling, including after failed birth
        // receipts. Keep token/RPC/image until the exact initializer exits too.
        if let Some(process) = &self.process {
            if unsafe { WaitForSingleObject(process.raw(), 30_000) } != WAIT_OBJECT_0 {
                crate::util::exit_for_unverified_cleanup(
                    "paired original initializer",
                    &anyhow::anyhow!(
                        "original initializer exit unverified after owning jobs drain"
                    ),
                );
            }
            #[cfg(test)]
            INITIALIZER_EXIT.with(|s| {
                if let Some(hook) = s.borrow_mut().as_mut() {
                    hook(process.raw());
                }
            });
        }
    }
}
pub(in crate::account_state::execution::paired_plan::native::namespace) struct OwnedInitializer<
    'p,
    'a,
    'l,
> {
    plan: &'p CommittedPlan<'a, 'l>,
    request: paired::Request,
    live: Live,
    release_attempted: bool,
    released: bool,
}

fn verify_token(token: HANDLE, p: &Plan) -> Result<()> {
    crate::account_impersonation::require_process_identity()?;
    crate::account_namespace::verify_noninheritable(token)?;
    crate::account_profile_registry::verify_profile_token(
        token,
        &crate::sid::sid_bytes(&p.execution.identity.package_sid)?,
        &crate::sid::sid_bytes(&p.execution.owner)?,
    )
}
fn verify_population(root: HANDLE, p: &Plan) -> Result<()> {
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
        observe_children(root)? == expected,
        "paired initializer namespace children differ"
    );
    let private = format!(
        "\\Sessions\\{}\\AppContainerNamedObjects\\{}",
        p.session, p.execution.identity.package_sid
    );
    let links = [
        observe_initializer_link(root, InitializerLink::Global)?,
        observe_initializer_link(root, InitializerLink::Local)?,
        observe_initializer_link(root, InitializerLink::Session)?,
    ];
    ensure!(
        links == ["\\BaseNamedObjects".to_owned(), private.clone(), private],
        "paired initializer namespace aliases differ"
    );
    Ok(())
}
impl<'p, 'a, 'l> OwnedInitializer<'p, 'a, 'l> {
    fn prepare(
        jobs: &Jobs<'p, 'a, 'l>,
        running: &RunningRole<'_, 'p, 'a, 'l>,
        namespace: &OwnedNamespace<'p, 'a, 'l>,
        profile: &host_profile::OwnedHostProfile<'p, 'a, 'l>,
        station: &OwnedStation<'p, 'a, 'l>,
        desktop: &desktop::OwnedDesktop<'p, 'a, 'l>,
    ) -> Result<Self> {
        ensure!(
            std::ptr::eq(jobs, running.birth.jobs) && running.birth.role == Role::Consumers,
            "initializer requires the original consumer role"
        );
        jobs.verify()?;
        namespace.verify_open()?;
        profile.require_initializer(jobs.plan)?;
        station.verify_applied()?;
        desktop.verify_ready(station)?;
        ensure!(
            std::ptr::eq(jobs.plan, namespace.plan) && std::ptr::eq(jobs.plan, station.plan),
            "initializer resources belong to another pair"
        );
        let (pid, created, _) = running.birth.child.durable_observation()?;
        jobs.require_exclusive_member(
            Role::Consumers,
            running.birth.child.original_exit_reference()?.raw(),
        )?;
        let p = &jobs.plan.plan;
        let sql = jobs.plan.database.connection()?;
        ensure!(
            replay(sql, p)?.is_empty(),
            "paired initializer already attempted"
        );
        let image = SystemInitializerImage::prepare(&p.execution.owner)?;
        let request = paired::Request {
            subject: initializer_wire::Request {
                operation: p.operation.clone(),
                nonce: random_id()?,
                bootstrap_pid: pid,
                bootstrap_created: created,
                container: p.execution.identity.container.clone(),
                package_sid: p.execution.identity.package_sid.clone(),
                owner: p.execution.owner.clone(),
                session: p.session,
                image_id: image.identity()?,
                #[cfg(test)]
                omit_profile_environment: false,
            },
            resources: resources(sql, p)?,
        };
        request.validate()?;
        Ok(Self {
            plan: jobs.plan,
            request,
            live: Live {
                process: None,
                rpc: None,
                token: None,
                image,
                birth: None,
                policy: None,
            },
            release_attempted: false,
            released: false,
        })
    }
    fn verify_resources(
        &self,
        jobs: &Jobs<'p, 'a, 'l>,
        ns: &OwnedNamespace<'p, 'a, 'l>,
        profile: &host_profile::OwnedHostProfile<'p, 'a, 'l>,
        station: &OwnedStation<'p, 'a, 'l>,
        desktop: &desktop::OwnedDesktop<'p, 'a, 'l>,
    ) -> Result<HANDLE> {
        ensure!(
            std::ptr::eq(self.plan, jobs.plan)
                && std::ptr::eq(self.plan, ns.plan)
                && std::ptr::eq(self.plan, station.plan),
            "initializer live plan differs"
        );
        jobs.verify()?;
        profile.require_initializer(self.plan)?;
        station.verify_applied()?;
        desktop.verify_ready(station)?;
        ensure!(
            !ns.retirement_attempted
                && super::super::replay(self.plan.database.connection()?, &self.plan.plan)?
                    == ns.birth_events(),
            "initializer namespace lifetime changed"
        );
        let root = ns
            .root
            .as_ref()
            .context("initializer original root closed")?
            .raw();
        root_native::verify_identity(root, ns.parent.raw(), &self.plan.plan)?;
        ensure!(
            self.request.resources == resources(self.plan.database.connection()?, &self.plan.plan)?,
            "initializer resource history changed"
        );
        self.live.image.application()?;
        Ok(root)
    }
    fn start(
        &mut self,
        jobs: &Jobs<'p, 'a, 'l>,
        running: &mut RunningRole<'_, 'p, 'a, 'l>,
        ns: &OwnedNamespace<'p, 'a, 'l>,
        profile: &host_profile::OwnedHostProfile<'p, 'a, 'l>,
        station: &OwnedStation<'p, 'a, 'l>,
        desktop: &desktop::OwnedDesktop<'p, 'a, 'l>,
    ) -> Result<()> {
        ensure!(
            std::ptr::eq(jobs, running.birth.jobs) && running.birth.role == Role::Consumers,
            "initializer source is not original consumer"
        );
        let p = &self.plan.plan;
        let sql = self.plan.database.connection()?;
        append(
            sql,
            p,
            Event::Intent {
                request: self.request.clone(),
            },
        )?;
        checkpoint("intent", sql)?;
        let root = self.verify_resources(jobs, ns, profile, station, desktop)?;
        ensure!(
            observe_children(root)?.is_empty(),
            "initializer root populated before birth"
        );
        running.birth.child.durable_observation()?;
        running
            ._channel
            .send(&self.request.encode()?, Duration::from_secs(10))?;
        let process =
            paired::receive_process(&running.birth.child, &mut running._channel, &self.request)?;
        self.live.process = Some(process);
        let process = self.live.process.as_ref().unwrap();
        #[cfg(test)]
        INITIALIZER_PROCESS.with(|s| -> Result<()> {
            if let Some(hook) = s.borrow_mut().as_mut() {
                hook(process.raw())?;
            }
            Ok(())
        })?;
        jobs.require_exclusive_member(Role::Consumers, process.raw())?;
        self.live.image.verify_process(process.raw())?;
        let birth = Birth {
            pid: unsafe { GetProcessId(process.raw()) },
            created: process_created(process.raw())?,
        };
        let mut session = 0;
        unsafe {
            windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(birth.pid, &mut session)?;
        }
        ensure!(
            session == p.session
                && unsafe { WaitForSingleObject(process.raw(), 0) } == WAIT_TIMEOUT,
            "paired initializer session or suspension lifetime differs"
        );
        let mut token = HANDLE::default();
        unsafe {
            OpenProcessToken(process.raw(), TOKEN_QUERY, &mut token)?;
        }
        self.live.token = Some(OwnedHandle(token));
        verify_token(self.live.token.as_ref().unwrap().raw(), p)?;
        self.live.birth = Some(birth.clone());
        checkpoint("born", sql)?;
        append(
            sql,
            p,
            Event::Born {
                request: self.request.clone(),
                birth: birth.clone(),
            },
        )?;
        running._channel.send(
            &self.request.frame("stop", birth.pid, birth.created)?,
            Duration::from_secs(10),
        )?;
        ensure!(
            running._channel.receive(Duration::from_secs(10))?
                == self.request.frame("stopped", birth.pid, birth.created)?,
            "paired initializer stop acknowledgment differs"
        );
        ensure!(
            unsafe { WaitForSingleObject(process.raw(), 5000) } == WAIT_OBJECT_0,
            "paired original initializer exit unverified"
        );
        unsafe { CloseHandle(self.live.process.take().unwrap().into_raw()) }
            .context("paired initializer original process close uncertain")?;
        let root = self.verify_resources(jobs, ns, profile, station, desktop)?;
        self.live.rpc = Some(RetainedRpc::open(root)?);
        verify_population(root, p)?;
        checkpoint("stopped", sql)?;
        append(
            sql,
            p,
            Event::Stopped {
                request: self.request.clone(),
                birth,
            },
        )?;
        let policy = self
            .live
            .rpc
            .as_ref()
            .unwrap()
            .prepare_account_access(root, p)?;
        checkpoint("rpc-intent", sql)?;
        append(
            sql,
            p,
            Event::RpcIntent {
                request: self.request.clone(),
                policy: policy.clone(),
            },
        )?;
        let root = self.verify_resources(jobs, ns, profile, station, desktop)?;
        verify_population(root, p)?;
        verify_token(self.live.token.as_ref().unwrap().raw(), p)?;
        self.live
            .rpc
            .as_mut()
            .unwrap()
            .grant_account(root, p, &policy)?;
        self.live.policy = Some(policy.clone());
        checkpoint("rpc-granted", sql)?;
        append(
            sql,
            p,
            Event::RpcGranted {
                request: self.request.clone(),
                policy,
            },
        )?;
        self.verify_ready(jobs, ns, profile, station, desktop)?;
        checkpoint("ready", sql)?;
        self.verify_ready(jobs, ns, profile, station, desktop)
    }
    fn ready_events(&self) -> Result<Vec<Event>> {
        let b = self
            .live
            .birth
            .as_ref()
            .context("initializer birth absent")?;
        let policy = self
            .live
            .policy
            .as_ref()
            .context("initializer RPC policy absent")?;
        Ok(vec![
            Event::Intent {
                request: self.request.clone(),
            },
            Event::Born {
                request: self.request.clone(),
                birth: b.clone(),
            },
            Event::Stopped {
                request: self.request.clone(),
                birth: b.clone(),
            },
            Event::RpcIntent {
                request: self.request.clone(),
                policy: policy.clone(),
            },
            Event::RpcGranted {
                request: self.request.clone(),
                policy: policy.clone(),
            },
        ])
    }
    fn verify_ready(
        &self,
        jobs: &Jobs<'p, 'a, 'l>,
        ns: &OwnedNamespace<'p, 'a, 'l>,
        profile: &host_profile::OwnedHostProfile<'p, 'a, 'l>,
        station: &OwnedStation<'p, 'a, 'l>,
        desktop: &desktop::OwnedDesktop<'p, 'a, 'l>,
    ) -> Result<()> {
        ensure!(
            !self.release_attempted && !self.released,
            "initializer release already attempted"
        );
        let root = self.verify_resources(jobs, ns, profile, station, desktop)?;
        verify_population(root, &self.plan.plan)?;
        verify_token(
            self.live
                .token
                .as_ref()
                .context("initializer token absent")?
                .raw(),
            &self.plan.plan,
        )?;
        self.live
            .rpc
            .as_ref()
            .context("initializer RPC absent")?
            .verify_account_access(
                root,
                &self.plan.plan,
                self.live
                    .policy
                    .as_ref()
                    .context("initializer RPC policy absent")?,
            )?;
        ensure!(
            replay(self.plan.database.connection()?, &self.plan.plan)? == self.ready_events()?,
            "initializer ready receipts differ"
        );
        Ok(())
    }
    fn release(
        &mut self,
        jobs: &Jobs<'p, 'a, 'l>,
        completed: &CompletedPair<'_, 'p, 'a, 'l>,
        ns: &OwnedNamespace<'p, 'a, 'l>,
        profile: &host_profile::OwnedHostProfile<'p, 'a, 'l>,
        station: &OwnedStation<'p, 'a, 'l>,
        desktop: &desktop::OwnedDesktop<'p, 'a, 'l>,
    ) -> Result<()> {
        self.verify_ready(jobs, ns, profile, station, desktop)?;
        ensure!(
            !std::mem::replace(&mut self.release_attempted, true),
            "initializer release repeated"
        );
        ensure!(
            std::ptr::eq(completed.jobs, jobs),
            "initializer completion belongs to another original pair"
        );
        completed.verify()?;
        let sql = self.plan.database.connection()?;
        let p = &self.plan.plan;
        let retirements = super::super::retirement_nonces(sql, p)?;
        append(
            sql,
            p,
            Event::ReleaseIntent {
                request: self.request.clone(),
                retirements: retirements.clone(),
            },
        )?;
        checkpoint("releasing", sql)?;
        completed.verify()?;
        let root = self.verify_resources(jobs, ns, profile, station, desktop)?;
        verify_population(root, p)?;
        self.live
            .rpc
            .as_ref()
            .context("initializer RPC absent")?
            .verify_account_access(
                root,
                p,
                self.live
                    .policy
                    .as_ref()
                    .context("initializer RPC policy absent")?,
            )?;
        verify_token(
            self.live
                .token
                .as_ref()
                .context("initializer token absent")?
                .raw(),
            p,
        )?;
        self.live
            .rpc
            .take()
            .context("initializer RPC close already attempted")?
            .close()?;
        let token = self
            .live
            .token
            .take()
            .context("initializer token close already attempted")?;
        unsafe { CloseHandle(token.into_raw()) }
            .context("paired initializer token close uncertain; preserve intent")?;
        checkpoint("closed", sql)?;
        completed.verify()?;
        append(
            sql,
            p,
            Event::Released {
                request: self.request.clone(),
                retirements,
            },
        )?;
        checkpoint("completed", sql)?;
        self.released = true;
        self.verify_released(jobs)
    }
    pub(in crate::account_state::execution::paired_plan::native::namespace) fn verify_released(
        &self,
        jobs: &Jobs<'p, 'a, 'l>,
    ) -> Result<()> {
        ensure!(
            std::ptr::eq(self.plan, jobs.plan)
                && self.release_attempted
                && self.released
                && self.live.process.is_none()
                && self.live.rpc.is_none()
                && self.live.token.is_none(),
            "initializer original release incomplete"
        );
        for role in [Role::Owner, Role::Consumers] {
            jobs.verify_empty_retired_role(role)?;
        }
        let mut expected = self.ready_events()?;
        let retirements =
            super::super::retirement_nonces(self.plan.database.connection()?, &self.plan.plan)?;
        expected.push(Event::ReleaseIntent {
            request: self.request.clone(),
            retirements: retirements.clone(),
        });
        expected.push(Event::Released {
            request: self.request.clone(),
            retirements,
        });
        ensure!(
            replay(self.plan.database.connection()?, &self.plan.plan)? == expected,
            "initializer released receipts differ"
        );
        Ok(())
    }
}
fn checkpoint(phase: &str, sql: &Connection) -> Result<()> {
    #[cfg(test)]
    INITIALIZER_CHECKPOINT.with(|s| -> Result<()> {
        if let Some(hook) = s.borrow_mut().as_mut() {
            hook(phase, sql)?;
        }
        Ok(())
    })?;
    #[cfg(not(test))]
    let _ = (phase, sql);
    Ok(())
}
#[cfg(test)]
thread_local! {pub(crate) static INITIALIZER_CHECKPOINT:std::cell::RefCell<Option<Box<dyn FnMut(&str,&Connection)->Result<()>>>>=const{std::cell::RefCell::new(None)};}
#[cfg(test)]
thread_local! {pub(crate) static INITIALIZER_PROCESS:std::cell::RefCell<Option<Box<dyn FnMut(HANDLE)->Result<()>>>>=const{std::cell::RefCell::new(None)};}
#[cfg(test)]
thread_local! {
    pub(crate) static INITIALIZER_EXIT: std::cell::RefCell<Option<Box<dyn FnMut(HANDLE)>>> = const { std::cell::RefCell::new(None) };
}

#[cfg(test)]
impl<'p, 'a, 'l> StationPair<'p, 'a, 'l> {
    pub(crate) fn fixture_run_initialized_profile_lifetime(
        mut self,
        owner: ControlServer,
        consumer: ControlServer,
        timeout: Duration,
        password: &str,
    ) -> Result<CompletedStationPair<'p, 'a, 'l>> {
        self.require_open()?;
        let mut pair = self
            .pair
            .take()
            .context("initialized profile pair absent")?;
        let desktop = self
            .desktop
            .as_ref()
            .context("initialized profile desktop absent")?;
        let profile = pair
            .profile
            .as_ref()
            .context("initialized host profile absent")?;
        let completed = {
            let mut initialization = ProfileInitialization {
                jobs: &pair.jobs,
                slot: &mut pair.initializer,
                namespace: &pair.namespace,
                profile,
                station: &self.station,
                desktop,
            };
            pair.jobs.fixture_run_initialized_profile_lifetime(
                owner,
                consumer,
                timeout,
                password,
                &mut initialization,
            )?
        };
        pair.initializer
            .as_mut()
            .context("profile initializer never started")?
            .release(
                &pair.jobs,
                &completed,
                &pair.namespace,
                profile,
                &self.station,
                desktop,
            )?;
        pair.profile
            .as_mut()
            .unwrap()
            .retire(&pair.jobs, &completed)?;
        pair.namespace.retire(&pair.jobs, &completed)?;
        let completed = CompletedNamespacedPair { bundle: pair };
        completed.verify()?;
        self.finish(completed)
    }

    pub(crate) fn fixture_run_initializer(
        mut self,
        owner: ControlServer,
        consumer: ControlServer,
    ) -> Result<CompletedStationPair<'p, 'a, 'l>> {
        self.require_open()?;
        let mut pair = self.pair.take().context("initializer pair absent")?;
        let desktop = self
            .desktop
            .as_ref()
            .context("initializer requires original desktop")?;
        let profile = pair
            .profile
            .as_ref()
            .context("initializer requires original host profile")?;
        let owner = pair
            .jobs
            .prepare_bootstrap(Role::Owner)?
            .start(owner, Duration::from_secs(10))?;
        let mut consumer = pair
            .jobs
            .prepare_bootstrap(Role::Consumers)?
            .start(consumer, Duration::from_secs(10))?;
        pair.initializer = Some(OwnedInitializer::prepare(
            &pair.jobs,
            &consumer,
            &pair.namespace,
            profile,
            &self.station,
            desktop,
        )?);
        pair.initializer.as_mut().unwrap().start(
            &pair.jobs,
            &mut consumer,
            &pair.namespace,
            profile,
            &self.station,
            desktop,
        )?;
        let consumers = pair.jobs.retire_consumers(consumer)?;
        let completed = pair.jobs.retire_owner(owner, consumers)?;
        pair.initializer.as_mut().unwrap().release(
            &pair.jobs,
            &completed,
            &pair.namespace,
            profile,
            &self.station,
            desktop,
        )?;
        pair.profile
            .as_mut()
            .unwrap()
            .retire(&pair.jobs, &completed)?;
        pair.namespace.retire(&pair.jobs, &completed)?;
        let completed = CompletedNamespacedPair { bundle: pair };
        completed.verify()?;
        self.finish(completed)
    }
}

/// Borrowed only by the fixed profile fixture. Construction remains inside the
/// intact resource owner; a callback or saved request cannot supply authority.
#[cfg(test)]
pub(crate) struct ProfileInitialization<'b, 'p, 'a, 'l> {
    jobs: &'b Jobs<'p, 'a, 'l>,
    slot: &'b mut Option<OwnedInitializer<'p, 'a, 'l>>,
    namespace: &'b OwnedNamespace<'p, 'a, 'l>,
    profile: &'b host_profile::OwnedHostProfile<'p, 'a, 'l>,
    station: &'b OwnedStation<'p, 'a, 'l>,
    desktop: &'b desktop::OwnedDesktop<'p, 'a, 'l>,
}
#[cfg(test)]
impl<'p, 'a, 'l> ProfileInitialization<'_, 'p, 'a, 'l> {
    /// Data for the future fixed probe, supplied only after rechecking every
    /// original live resource. This does not authorize launch or reconstruct
    /// cleanup ownership from the returned serialized request.
    pub(crate) fn fixture_station_probe_subject(
        &self,
        jobs: &Jobs<'p, 'a, 'l>,
        consumer: &RunningRole<'_, 'p, 'a, 'l>,
    ) -> Result<paired::Request> {
        ensure!(
            std::ptr::eq(self.jobs, jobs)
                && std::ptr::eq(consumer.birth.jobs, jobs)
                && consumer.birth.role == Role::Consumers,
            "station probe requires the original consumer and resource pair"
        );
        let initializer = self
            .slot
            .as_ref()
            .context("station probe initializer absent")?;
        initializer.verify_ready(
            jobs,
            self.namespace,
            self.profile,
            self.station,
            self.desktop,
        )?;
        let (pid, created, _) = consumer.birth.child.durable_observation()?;
        ensure!(
            initializer.request.subject.bootstrap_pid == pid
                && initializer.request.subject.bootstrap_created == created,
            "station probe initializer belongs to another original consumer"
        );
        jobs.require_exclusive_member(
            Role::Consumers,
            consumer.birth.child.original_exit_reference()?.raw(),
        )?;
        Ok(initializer.request.clone())
    }

    pub(crate) fn start(
        &mut self,
        jobs: &Jobs<'p, 'a, 'l>,
        consumer: &mut RunningRole<'_, 'p, 'a, 'l>,
    ) -> Result<()> {
        ensure!(
            std::ptr::eq(self.jobs, jobs) && self.slot.is_none(),
            "profile initializer requires original pair and one attempt"
        );
        *self.slot = Some(OwnedInitializer::prepare(
            jobs,
            consumer,
            self.namespace,
            self.profile,
            self.station,
            self.desktop,
        )?);
        self.slot.as_mut().unwrap().start(
            jobs,
            consumer,
            self.namespace,
            self.profile,
            self.station,
            self.desktop,
        )
    }
}

#[cfg(test)]
#[path = "account_paired_initializer_tests.rs"]
mod tests;
