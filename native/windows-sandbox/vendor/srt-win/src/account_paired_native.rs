//! Two strict sibling jobs and their original trusted host bootstraps. No job
//! handle leaves the broker. The account adapter admits suspended observation
//! and a fixed consumer self-check. A separate original-owner adapter uses live
//! completion for bounded profile cleanup. No user command is admitted here.
#![allow(dead_code)]
use super::*;
use crate::{
    account_control::{ControlChannel, ControlServer, bootstrap::SuspendedBootstrap, containment},
    job::Job,
};
use std::{
    cell::Cell,
    mem::{size_of, size_of_val},
    time::Duration,
};
use windows::Win32::{
    Foundation::{HANDLE, WAIT_OBJECT_0},
    System::{
        JobObjects::{
            IsProcessInJob, JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
            JobObjectBasicAccountingInformation, QueryInformationJobObject,
        },
        Threading::WaitForSingleObject,
    },
};

#[path = "account_paired_account.rs"]
pub(crate) mod account;

#[path = "account_paired_namespace.rs"]
pub(crate) mod namespace;

enum RetirementSubject<'j, 'p, 'a, 'l> {
    Host(RunningRole<'j, 'p, 'a, 'l>),
    Account(account::ObservedAccount<'j, 'p, 'a, 'l>),
    CheckedAccount(account::resume::CompletedSelfCheck<'j, 'p, 'a, 'l>),
    ProfileOwner(account::profile::CompletedOwner<'j, 'p, 'a, 'l>),
}
impl<'j, 'p, 'a, 'l> RetirementSubject<'j, 'p, 'a, 'l> {
    fn birth(&self) -> &Bootstrap<'j, 'p, 'a, 'l> {
        match self {
            Self::Host(role) => &role.birth,
            Self::Account(role) => &role.birth,
            Self::CheckedAccount(role) => &role.birth,
            Self::ProfileOwner(role) => &role.birth,
        }
    }
}

/// Consumers drop first on ordinary unwind. Strict Job::Drop drains before
/// closing; abrupt broker death closes both sole original kernel job handles.
/// Keeping a retired job open until the pair drops does not grant another birth:
/// both the in-memory one-shot latch and immutable journal forbid it.
pub(crate) struct Jobs<'p, 'a, 'lease> {
    consumers: Job,
    owner: Job,
    plan: &'p CommittedPlan<'a, 'lease>,
    births: [Cell<bool>; 2],
    retirements: [Cell<bool>; 2],
}

fn accounting(job: &Job) -> Result<(u32, u32)> {
    let mut data = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
    let mut returned = 0;
    unsafe {
        QueryInformationJobObject(
            Some(job.raw()),
            JobObjectBasicAccountingInformation,
            (&mut data as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
            size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
            Some(&mut returned),
        )?;
    }
    ensure!(
        returned as usize == size_of_val(&data),
        "incomplete paired job accounting"
    );
    Ok((data.ActiveProcesses, data.TotalProcesses))
}

impl<'a, 'lease> CommittedPlan<'a, 'lease> {
    pub(crate) fn create_jobs(&self) -> Result<Jobs<'_, 'a, 'lease>> {
        self.verify()?;
        ensure!(
            !self.jobs_attempted.replace(true),
            "paired jobs already attempted"
        );
        let sql = self.database.connection()?;
        ensure!(
            replay(sql, &self.plan, Role::Owner)?.is_empty()
                && replay(sql, &self.plan, Role::Consumers)?.is_empty(),
            "paired job intent already exists"
        );
        let owner = self.create_role_job(Role::Owner)?;
        let consumers = self.create_role_job(Role::Consumers)?;
        let jobs = Jobs {
            consumers,
            owner,
            plan: self,
            births: [Cell::new(false), Cell::new(false)],
            retirements: [Cell::new(false), Cell::new(false)],
        };
        jobs.verify()?;
        Ok(jobs)
    }

    fn create_role_job(&self, role: Role) -> Result<Job> {
        self.verify()?;
        let sql = self.database.connection()?;
        append(sql, &self.plan, role, Event::JobIntent)?;
        self.verify()?;
        let job = Job::for_paired_role(
            &self.plan.execution.identity.container,
            role == Role::Consumers,
        )?;
        containment::prepare(&job)?;
        ensure!(
            accounting(&job)? == (0, 0),
            "paired job is not freshly empty"
        );
        checkpoint("job-created", role, sql, None);
        append(sql, &self.plan, role, Event::JobCreated)?;
        containment::prepare(&job)?;
        Ok(job)
    }
}

impl<'p, 'a, 'lease> Jobs<'p, 'a, 'lease> {
    fn job(&self, role: Role) -> &Job {
        match role {
            Role::Owner => &self.owner,
            Role::Consumers => &self.consumers,
        }
    }

    fn verify(&self) -> Result<()> {
        self.plan.verify()?;
        containment::prepare(&self.owner)?;
        containment::prepare(&self.consumers)?;
        ensure!(
            self.owner.raw() != self.consumers.raw(),
            "paired original jobs alias"
        );
        Ok(())
    }

    fn prefix(&self, role: Role) -> Result<Vec<Event>> {
        self.verify()?;
        replay(self.plan.database.connection()?, &self.plan.plan, role)
    }

    pub(crate) fn prepare_bootstrap(&self, role: Role) -> Result<Bootstrap<'_, 'p, 'a, 'lease>> {
        self.prepare_bootstrap_inner(role, false)
    }

    #[cfg(test)]
    pub(crate) fn prepare_family_fixture(&self) -> Result<Bootstrap<'_, 'p, 'a, 'lease>> {
        self.prepare_bootstrap_inner(Role::Consumers, true)
    }

    fn prepare_bootstrap_inner(
        &self,
        role: Role,
        family_fixture: bool,
    ) -> Result<Bootstrap<'_, 'p, 'a, 'lease>> {
        #[cfg(not(test))]
        ensure!(!family_fixture, "family fixture unavailable");
        self.verify()?;
        ensure!(
            !self.births[role.index()].replace(true),
            "paired role birth already attempted"
        );
        ensure!(
            self.prefix(role)? == [Event::JobIntent, Event::JobCreated]
                && accounting(self.job(role))? == (0, 0),
            "paired bootstrap requires original fresh job receipt"
        );
        append(
            self.plan.database.connection()?,
            &self.plan.plan,
            role,
            Event::BirthIntent,
        )?;
        let mut intent = BirthIntent {
            jobs: self,
            role,
            attempted: false,
        };
        #[cfg(test)]
        let child = if family_fixture {
            ensure!(
                role == Role::Consumers,
                "family fixture requires consumer role"
            );
            SuspendedBootstrap::prepare_paired_family_fixture(&mut intent)?
        } else {
            SuspendedBootstrap::prepare_paired_recorded(&mut intent)?
        };
        #[cfg(not(test))]
        let child = SuspendedBootstrap::prepare_paired_recorded(&mut intent)?;
        let (pid, created, image_id) = child.durable_observation()?;
        self.require_exclusive_member(role, child.original_exit_reference()?.raw())?;
        let sql = self.plan.database.connection()?;
        let observed = child.original_exit_reference()?;
        checkpoint("bootstrap-born", role, sql, Some(observed.raw()));
        append(
            sql,
            &self.plan.plan,
            role,
            Event::Born {
                pid,
                created,
                image_id,
            },
        )?;
        drop(observed);
        Ok(Bootstrap {
            child,
            jobs: self,
            role,
        })
    }

    fn require_exclusive_member(&self, role: Role, process: HANDLE) -> Result<()> {
        self.verify()?;
        for candidate in [Role::Owner, Role::Consumers] {
            let mut member = windows::core::BOOL::default();
            unsafe {
                IsProcessInJob(process, Some(self.job(candidate).raw()), &mut member)?;
            }
            ensure!(
                member.as_bool() == (candidate == role),
                "bootstrap belongs to wrong paired job"
            );
        }
        Ok(())
    }

    pub(crate) fn retire_consumers<'j>(
        &'j self,
        running: RunningRole<'j, 'p, 'a, 'lease>,
    ) -> Result<CompletedConsumers<'j, 'p, 'a, 'lease>> {
        self.retire(Role::Consumers, RetirementSubject::Host(running))?;
        let completed = CompletedConsumers { jobs: self };
        completed.verify(self)?;
        Ok(completed)
    }

    pub(crate) fn retire_owner<'j>(
        &'j self,
        running: RunningRole<'j, 'p, 'a, 'lease>,
        consumers: CompletedConsumers<'j, 'p, 'a, 'lease>,
    ) -> Result<CompletedPair<'j, 'p, 'a, 'lease>> {
        consumers.verify(self)?;
        self.retire(Role::Owner, RetirementSubject::Host(running))?;
        let completed = CompletedPair { jobs: self };
        completed.verify()?;
        Ok(completed)
    }

    fn retire(&self, role: Role, subject: RetirementSubject<'_, 'p, 'a, 'lease>) -> Result<()> {
        ensure!(
            std::ptr::eq(self, subject.birth().jobs) && subject.birth().role == role,
            "paired retirement requires same original job and role"
        );
        let result = self.retire_inner(role, subject);
        if let Err(error) = result {
            // Dropping a source guard terminates only that process. Its children
            // still belong to this live job, including when the initial intent
            // cannot be written. Drain the entire exact role before returning;
            // a failed journal operation never becomes a completion receipt.
            if let Err(drain) = self.job(role).terminate_and_wait() {
                return Err(error.context(format!(
                    "paired retirement failed; original role drain also failed: {drain:#}"
                )));
            }
            return Err(error);
        }
        Ok(())
    }

    fn retire_inner(
        &self,
        role: Role,
        subject: RetirementSubject<'_, 'p, 'a, 'lease>,
    ) -> Result<()> {
        self.verify()?;
        ensure!(
            !self.retirements[role.index()].replace(true),
            "paired role retirement already attempted"
        );
        let account_exit = match &subject {
            RetirementSubject::Host(_) => None,
            RetirementSubject::Account(account) => Some(account.exit_observer()?),
            RetirementSubject::CheckedAccount(account) => Some(account.exit_observer()?),
            RetirementSubject::ProfileOwner(account) => Some(account.exit_observer()?),
        };
        let observed = subject.birth().child.original_exit_reference()?;
        self.require_exclusive_member(role, observed.raw())?;
        let sql = self.plan.database.connection()?;
        append(sql, &self.plan.plan, role, Event::RetireIntent)?;
        // Release source process/thread and authenticated channel before the
        // original job drains every descendant. Only read-only exit proof stays.
        drop(subject);
        self.job(role).terminate_and_wait()?;
        if let Some(account) = account_exit {
            account.wait(Duration::from_secs(5))?;
        }
        ensure!(
            unsafe { WaitForSingleObject(observed.raw(), 0) } == WAIT_OBJECT_0,
            "paired original bootstrap exit unobserved"
        );
        drop(observed);
        self.verify_empty_retired_role(role)?;
        checkpoint("job-drained", role, sql, None);
        append(sql, &self.plan.plan, role, Event::Retired)?;
        self.verify_empty_retired_role(role)
    }

    fn verify_empty_retired_role(&self, role: Role) -> Result<()> {
        self.verify()?;
        ensure!(
            self.births[role.index()].get() && self.retirements[role.index()].get(),
            "paired original retirement latches missing"
        );
        let (active, total) = accounting(self.job(role))?;
        ensure!(
            active == 0 && total > 0,
            "paired original job is not retired"
        );
        Ok(())
    }
}

/// Single-use live tickets only. Role/schema data read from disk cannot
/// reconstruct either type or access the original job handle.
pub(crate) struct BirthIntent<'j, 'p, 'a, 'lease> {
    jobs: &'j Jobs<'p, 'a, 'lease>,
    role: Role,
    attempted: bool,
}
impl<'j> BirthIntent<'j, '_, '_, '_> {
    pub(crate) fn consume(
        &mut self,
    ) -> Result<(&'j PinnedRunnerImage, &'j Job, &'j ControlAddress)> {
        ensure!(
            !std::mem::replace(&mut self.attempted, true),
            "paired birth ticket already consumed"
        );
        ensure!(
            self.jobs.prefix(self.role)?
                == [Event::JobIntent, Event::JobCreated, Event::BirthIntent]
                && self.jobs.births[self.role.index()].get()
                && !self.jobs.retirements[self.role.index()].get()
                && accounting(self.jobs.job(self.role))? == (0, 0),
            "paired birth ticket differs from original empty job"
        );
        Ok((
            self.jobs.plan.image,
            self.jobs.job(self.role),
            self.jobs.plan.address(self.role),
        ))
    }
}

pub(crate) struct Bootstrap<'j, 'p, 'a, 'lease> {
    child: SuspendedBootstrap<'j>,
    jobs: &'j Jobs<'p, 'a, 'lease>,
    role: Role,
}
impl<'j, 'p, 'a, 'lease> Bootstrap<'j, 'p, 'a, 'lease> {
    #[cfg(test)]
    pub(crate) fn fixture_readonly_observer(&self) -> Result<crate::util::OwnedHandle> {
        self.child.original_exit_reference()
    }

    pub(crate) fn start(
        mut self,
        server: ControlServer,
        timeout: Duration,
    ) -> Result<RunningRole<'j, 'p, 'a, 'lease>> {
        self.jobs.verify()?;
        let plan = self.jobs.plan;
        server.require_address(plan.address(self.role))?;
        ensure!(
            (Duration::from_millis(1)..=Duration::from_secs(10)).contains(&timeout),
            "paired bootstrap startup timeout outside bounds"
        );
        let (pid, created, image_id) = self.child.durable_observation()?;
        let prefix = self.jobs.prefix(self.role)?;
        ensure!(
            prefix.len() == 4
                && prefix[3]
                    == (Event::Born {
                        pid,
                        created,
                        image_id
                    }),
            "paired original birth differs"
        );
        append(
            plan.database.connection()?,
            &plan.plan,
            self.role,
            Event::ResumeIntent,
        )?;
        let mut intent = ResumeIntent {
            jobs: self.jobs,
            role: self.role,
            attempted: false,
        };
        self.child.resume_paired_recorded(&mut intent)?;
        let observed = self.child.original_exit_reference()?;
        checkpoint(
            "bootstrap-resumed",
            self.role,
            plan.database.connection()?,
            Some(observed.raw()),
        );
        append(
            plan.database.connection()?,
            &plan.plan,
            self.role,
            Event::Resumed,
        )?;
        let mut channel = server.accept(self.child.peer()?, timeout)?;
        let challenge = crate::account_control::bootstrap::entry::challenge(
            &plan.plan.roles[self.role.index()].resume_nonce,
        )?;
        channel.send(&challenge, timeout)?;
        ensure!(
            channel.receive(timeout)? == challenge,
            "paired readiness challenge differs"
        );
        self.child.capture_startup_exit()?;
        self.jobs
            .require_exclusive_member(self.role, observed.raw())?;
        self.child.durable_observation()?;
        checkpoint(
            "bootstrap-ready",
            self.role,
            plan.database.connection()?,
            Some(observed.raw()),
        );
        append(
            plan.database.connection()?,
            &plan.plan,
            self.role,
            Event::Ready,
        )?;
        self.child.allow_post_startup_work()?;
        channel.send(crate::account_control::bootstrap::STARTUP_CAPTURED, timeout)?;
        Ok(RunningRole {
            birth: self,
            _channel: channel,
        })
    }
}

pub(crate) struct ResumeIntent<'j, 'p, 'a, 'lease> {
    jobs: &'j Jobs<'p, 'a, 'lease>,
    role: Role,
    attempted: bool,
}
impl ResumeIntent<'_, '_, '_, '_> {
    pub(crate) fn consume(&mut self, child: &SuspendedBootstrap<'_>) -> Result<()> {
        ensure!(
            !std::mem::replace(&mut self.attempted, true),
            "paired resume ticket already consumed"
        );
        let prefix = self.jobs.prefix(self.role)?;
        let (pid, created, image_id) = child.durable_observation()?;
        ensure!(
            prefix.len() == 5
                && prefix[3]
                    == (Event::Born {
                        pid,
                        created,
                        image_id
                    })
                && prefix[4] == Event::ResumeIntent
                && self.jobs.births[self.role.index()].get()
                && !self.jobs.retirements[self.role.index()].get(),
            "paired resume ticket differs from original role birth"
        );
        self.jobs
            .require_exclusive_member(self.role, child.original_exit_reference()?.raw())
    }
}

pub(crate) struct RunningRole<'j, 'p, 'a, 'lease> {
    birth: Bootstrap<'j, 'p, 'a, 'lease>,
    _channel: ControlChannel<'j>,
}

#[cfg(test)]
impl RunningRole<'_, '_, '_, '_> {
    pub(crate) fn fixture_family_references(&mut self) -> Result<Vec<crate::util::OwnedHandle>> {
        ensure!(
            self.birth.role == Role::Consumers,
            "family observation requires consumer role"
        );
        let bytes = self._channel.receive(Duration::from_secs(5))?;
        ensure!(bytes.len() <= 1024, "family observation exceeds bound");
        let references: Vec<(u64, u32, u64)> = serde_json::from_slice(&bytes)?;
        ensure!(
            references.len() == 2 && references[0].1 != references[1].1,
            "family observation requires distinct child and grandchild"
        );
        let mut originals = Vec::new();
        for reference in references {
            let observed = self.birth.child.fixture_pull_family_reference(reference)?;
            self.birth
                .jobs
                .require_exclusive_member(Role::Consumers, observed.raw())?;
            originals.push(observed);
        }
        Ok(originals)
    }
}

/// The exact live consumer job has been drained and cannot admit another birth.
/// No Deserialize/Clone, cold constructor, raw handle or profile API exists.
pub(crate) struct CompletedConsumers<'j, 'p, 'a, 'lease> {
    jobs: &'j Jobs<'p, 'a, 'lease>,
}
impl CompletedConsumers<'_, '_, '_, '_> {
    fn verify(&self, jobs: &Jobs<'_, '_, '_>) -> Result<()> {
        ensure!(
            std::ptr::eq(self.jobs, jobs),
            "consumer completion belongs to another live pair"
        );
        self.jobs.verify_empty_retired_role(Role::Consumers)?;
        let prefix = self.jobs.prefix(Role::Consumers)?;
        ensure!(
            prefix.len() == 9 && prefix.last() == Some(&Event::Retired),
            "consumer retirement receipt missing"
        );
        Ok(())
    }
}

pub(crate) struct CompletedPair<'j, 'p, 'a, 'lease> {
    jobs: &'j Jobs<'p, 'a, 'lease>,
}
impl CompletedPair<'_, '_, '_, '_> {
    pub(crate) fn verify(&self) -> Result<()> {
        for role in [Role::Owner, Role::Consumers] {
            self.jobs.verify_empty_retired_role(role)?;
            let prefix = self.jobs.prefix(role)?;
            ensure!(
                prefix.len() == 9 && prefix.last() == Some(&Event::Retired),
                "paired retirement incomplete"
            );
        }
        Ok(())
    }
}

#[cfg(test)]
thread_local! {
    pub(crate) static NATIVE_CHECKPOINT: std::cell::RefCell<Option<Box<dyn FnMut(&str,Role,&Connection,Option<HANDLE>)>>> = const {std::cell::RefCell::new(None)};
}
fn checkpoint(phase: &str, role: Role, sql: &Connection, process: Option<HANDLE>) {
    #[cfg(test)]
    NATIVE_CHECKPOINT.with(|s| {
        if let Some(callback) = s.borrow_mut().as_mut() {
            callback(phase, role, sql, process);
        }
    });
    #[cfg(not(test))]
    let _ = (phase, role, sql, process);
}
