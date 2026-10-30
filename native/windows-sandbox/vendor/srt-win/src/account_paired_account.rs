//! One role-bound account child. Observation children never resume; a separate
//! consumer purpose admits only a journaled fixed self-check. Original source
//! and child guards remain bound through role retirement; cold rows grant no rights.
use super::super::account_journal::{self, Event as AccountEvent};
use super::*;
use crate::account_control::bootstrap::{
    entry::credential_wire::{
        PasswordFrame, Selection,
        child::{BirthRequest, Purpose},
    },
    pull::{AccountExitObserver, ObservedRunner, ProcessReceiver},
};

#[path = "account_paired_resume.rs"]
pub(crate) mod resume;

#[path = "account_paired_profile.rs"]
pub(crate) mod profile;

/// No generic credential getter or alternate account selector is exposed.
pub(crate) struct Delivery<'j, 'p, 'a, 'l> {
    running: RunningRole<'j, 'p, 'a, 'l>,
    selection: Selection,
    timeout: Duration,
    read_attempted: bool,
    send_attempted: bool,
}

impl<'j, 'p, 'a, 'l> Delivery<'j, 'p, 'a, 'l> {
    fn start(mut running: RunningRole<'j, 'p, 'a, 'l>, timeout: Duration) -> Result<Self> {
        ensure!(
            (Duration::from_millis(1)..=Duration::from_secs(10)).contains(&timeout),
            "paired account timeout outside bounds"
        );
        let jobs = running.birth.jobs;
        let role = running.birth.role;
        jobs.verify()?;
        ensure!(
            jobs.prefix(role)?.len() == 7 && !jobs.retirements[role.index()].get(),
            "paired account requires original ready role"
        );
        let plan = jobs.plan;
        ensure!(
            account_journal::replay(plan.database.connection()?, &plan.plan, role)?.is_empty(),
            "paired account already attempted"
        );
        let (pid, created, image_id) = running.birth.child.durable_observation()?;
        let selection = Selection {
            protocol: Selection::protocol(),
            operation: plan.plan.operation.clone(),
            logon: random_id()?,
            ready: plan.plan.roles[role.index()].resume_nonce.clone(),
            bootstrap_pid: pid,
            bootstrap_created: created,
            image_id,
            session: plan.plan.session,
            account: plan.plan.execution.identity.binding.clone(),
            credential_generation: plan.plan.credential_generation.clone(),
        };
        account_journal::append(
            plan.database.connection()?,
            &plan.plan,
            role,
            AccountEvent::Intent {
                selection: selection.clone(),
            },
        )?;
        running._channel.send(&selection.encode()?, timeout)?;
        selection.require_ack(&running._channel.receive(timeout)?, false)?;
        let delivery = Self {
            running,
            selection,
            timeout,
            read_attempted: false,
            send_attempted: false,
        };
        delivery.verify_intent()?;
        Ok(delivery)
    }

    fn verify_intent(&self) -> Result<()> {
        let jobs = self.running.birth.jobs;
        jobs.verify()?;
        ensure!(
            !jobs.retirements[self.running.birth.role.index()].get()
                && account_journal::replay(
                    jobs.plan.database.connection()?,
                    &jobs.plan.plan,
                    self.running.birth.role
                )? == [AccountEvent::Intent {
                    selection: self.selection.clone()
                }],
            "paired account original intent changed"
        );
        ensure!(
            self.running.birth.child.durable_observation()?
                == (
                    self.selection.bootstrap_pid,
                    self.selection.bootstrap_created,
                    self.selection.image_id
                ),
            "paired account original bootstrap changed"
        );
        Ok(())
    }

    pub(crate) fn begin_read(
        &mut self,
    ) -> Result<(
        &ReadyInstallation,
        &crate::execution_lease::ExecutionLease,
        AccountMode,
    )> {
        ensure!(
            !std::mem::replace(&mut self.read_attempted, true),
            "paired credential read already attempted"
        );
        self.verify_intent()?;
        let plan = self.running.birth.jobs.plan;
        let lease = plan
            .execution
            ._lease
            .context("paired account requires original execution lease")?;
        Ok((plan.ready, lease, self.selection.account.mode))
    }

    pub(crate) fn send_password(&mut self, user: &str, password: &str) -> Result<()> {
        ensure!(
            self.read_attempted && !std::mem::replace(&mut self.send_attempted, true),
            "paired credential transfer already attempted or unprepared"
        );
        self.verify_intent()?;
        ensure!(
            user == self.selection.account.account_name,
            "paired credential account differs"
        );
        let frame = PasswordFrame::encode(password, &self.selection)?;
        frame.send(&mut self.running._channel, &self.selection, self.timeout)?;
        drop(frame);
        self.selection
            .require_ack(&self.running._channel.receive(self.timeout)?, true)?;
        self.verify_intent()?;
        let jobs = self.running.birth.jobs;
        account_journal::append(
            jobs.plan.database.connection()?,
            &jobs.plan.plan,
            self.running.birth.role,
            AccountEvent::Delivered {
                selection: self.selection.clone(),
            },
        )
    }

    fn observe(self, purpose: Purpose) -> Result<ObservedAccount<'j, 'p, 'a, 'l>> {
        ensure!(
            self.read_attempted && self.send_attempted,
            "paired credential was not delivered"
        );
        let jobs = self.running.birth.jobs;
        let role = self.running.birth.role;
        jobs.verify()?;
        let plan = jobs.plan;
        let mut request = BirthRequest::new(self.selection.clone(), random_id()?)?;
        request.purpose = purpose;
        account_journal::append(
            plan.database.connection()?,
            &plan.plan,
            role,
            AccountEvent::BirthIntent {
                request: request.clone(),
            },
        )?;
        let RunningRole {
            mut birth,
            mut _channel,
        } = self.running;
        _channel.send(&request.encode()?, self.timeout)?;
        if matches!(request.purpose, Purpose::TrustedPairedProfileV1 { .. }) {
            _channel.receive_loaded_profile_birth_status(&request)?;
        } else {
            request.require_status(&_channel.receive(self.timeout)?)?;
        }
        let receiver = ProcessReceiver::prepare(
            &mut birth.child,
            _channel,
            plan.ready,
            request.selection.account.mode,
        )?
        .bind_birth(&request)?;
        let mut observed = receiver.receive(self.timeout)?;
        jobs.verify()?;
        birth.child.durable_observation()?;
        let (pid, created, image_id) = observed.birth_identity()?;
        crash_checkpoint("born", role, plan.database.connection()?, &observed)?;
        checkpoint("born", role, plan.database.connection()?);
        account_journal::append(
            plan.database.connection()?,
            &plan.plan,
            role,
            AccountEvent::Born {
                request: request.clone(),
                pid,
                created,
                image_id,
            },
        )?;
        ensure!(
            observed.birth_identity()? == (pid, created, image_id),
            "paired account changed before acknowledgment"
        );
        observed.acknowledge_birth(&request, self.timeout)?;
        crash_checkpoint("acknowledged", role, plan.database.connection()?, &observed)?;
        checkpoint("acknowledged", role, plan.database.connection()?);
        account_journal::append(
            plan.database.connection()?,
            &plan.plan,
            role,
            AccountEvent::Acknowledged {
                request: request.clone(),
                pid,
                created,
                image_id,
            },
        )?;
        let owned = ObservedAccount {
            observed,
            birth,
            request,
        };
        owned.verify()?;
        Ok(owned)
    }
}

/// Owns the authenticated channel and original bootstrap guard. There is no
/// arbitrary frame, handle export or profile API on this type. The resume module
/// checks the fixed purpose committed before birth and consumes the whole guard.
pub(crate) struct ObservedAccount<'j, 'p, 'a, 'l> {
    observed: ObservedRunner<'j>,
    pub(super) birth: Bootstrap<'j, 'p, 'a, 'l>,
    request: BirthRequest,
}
impl<'j, 'p, 'a, 'l> ObservedAccount<'j, 'p, 'a, 'l> {
    fn verify(&self) -> Result<()> {
        let jobs = self.birth.jobs;
        jobs.verify()?;
        let (pid, created, image_id) = self.observed.birth_identity()?;
        self.birth.child.durable_observation()?;
        let events = account_journal::replay(
            jobs.plan.database.connection()?,
            &jobs.plan.plan,
            self.birth.role,
        )?;
        ensure!(
            events.len() == 5
                && events.last()
                    == Some(&AccountEvent::Acknowledged {
                        request: self.request.clone(),
                        pid,
                        created,
                        image_id
                    }),
            "paired original account birth differs"
        );
        Ok(())
    }
    pub(super) fn exit_observer(&self) -> Result<AccountExitObserver> {
        self.verify()?;
        self.observed.exit_observer()
    }
    pub(crate) fn retire_consumers(self) -> Result<CompletedConsumers<'j, 'p, 'a, 'l>> {
        let jobs = self.birth.jobs;
        ensure!(
            self.birth.role == Role::Consumers,
            "paired account consumer role required"
        );
        jobs.retire(Role::Consumers, RetirementSubject::Account(self))?;
        let completed = CompletedConsumers { jobs };
        completed.verify(jobs)?;
        Ok(completed)
    }
    pub(crate) fn retire_owner(
        self,
        consumers: CompletedConsumers<'j, 'p, 'a, 'l>,
    ) -> Result<CompletedPair<'j, 'p, 'a, 'l>> {
        let jobs = self.birth.jobs;
        ensure!(
            self.birth.role == Role::Owner,
            "paired account owner role required"
        );
        consumers.verify(jobs)?;
        jobs.retire(Role::Owner, RetirementSubject::Account(self))?;
        let completed = CompletedPair { jobs };
        completed.verify()?;
        Ok(completed)
    }
    #[cfg(test)]
    pub(crate) fn fixture_identity(&self) -> Result<(u32, u64, [u8; 24])> {
        self.verify()?;
        self.observed.birth_identity()
    }
    #[cfg(test)]
    pub(crate) fn fixture_exit_observer(&self) -> Result<AccountExitObserver> {
        self.exit_observer()
    }
}

impl<'j, 'p, 'a, 'l> RunningRole<'j, 'p, 'a, 'l> {
    pub(crate) fn observe_account(
        self,
        timeout: Duration,
    ) -> Result<ObservedAccount<'j, 'p, 'a, 'l>> {
        self.observe_account_inner(timeout, None, Purpose::ObserveOnly)
    }
    #[cfg(test)]
    pub(crate) fn fixture_observe_account(
        self,
        timeout: Duration,
        password: &str,
    ) -> Result<ObservedAccount<'j, 'p, 'a, 'l>> {
        self.observe_account_inner(timeout, Some(password), Purpose::ObserveOnly)
    }
    pub(crate) fn run_consumer_self_check(
        self,
        timeout: Duration,
    ) -> Result<CompletedConsumers<'j, 'p, 'a, 'l>> {
        let account = self.observe_account_inner(timeout, None, Purpose::TrustedSelfCheckV1)?;
        resume::run(account, timeout)
    }
    #[cfg(test)]
    pub(crate) fn fixture_run_consumer_self_check(
        self,
        timeout: Duration,
        password: &str,
    ) -> Result<CompletedConsumers<'j, 'p, 'a, 'l>> {
        let account =
            self.observe_account_inner(timeout, Some(password), Purpose::TrustedSelfCheckV1)?;
        resume::run(account, timeout)
    }
    fn observe_account_inner(
        self,
        timeout: Duration,
        fixture_password: Option<&str>,
        purpose: Purpose,
    ) -> Result<ObservedAccount<'j, 'p, 'a, 'l>> {
        #[cfg(not(test))]
        ensure!(
            fixture_password.is_none(),
            "paired fixture credential unavailable"
        );
        let jobs = self.birth.jobs;
        let role = self.birth.role;
        let result = (|| {
            ensure!(
                purpose == Purpose::ObserveOnly
                    || (role == Role::Consumers && purpose.is_paired_consumer())
                    || (role == Role::Owner
                        && purpose
                            == Purpose::TrustedPairedProfileV1 {
                                container: jobs.plan.plan.execution.identity.container.clone(),
                                package_sid: jobs.plan.plan.execution.identity.package_sid.clone(),
                                policy: 1,
                            }),
                "paired account purpose not admitted for this role"
            );
            let mut delivery = Delivery::start(self, timeout)?;
            #[cfg(test)]
            if let Some(password) = fixture_password {
                // Synthetic fixture only; production always requires the live
                // execution lease and existing protected credential reader.
                delivery.read_attempted = true;
                let user = delivery.selection.account.account_name.clone();
                delivery.send_password(&user, password)?;
            } else {
                crate::account_credentials::deliver_to_paired_bootstrap(&mut delivery)?;
            }
            #[cfg(not(test))]
            crate::account_credentials::deliver_to_paired_bootstrap(&mut delivery)?;
            delivery.observe(purpose)
        })();
        if result.is_err() {
            // An uncertain birth never leaves descendants awaiting an eventual
            // outer Drop. Drain this exact role; no completed receipt is minted.
            jobs.job(role)
                .terminate_and_wait()
                .context("paired account failure; original role drain failed")?;
        }
        result
    }
}

#[cfg(test)]
thread_local! { pub(crate) static ACCOUNT_CHECKPOINT: std::cell::RefCell<Option<Box<dyn FnMut(&str,Role,&Connection)>>> = const {std::cell::RefCell::new(None)}; }
#[cfg(test)]
thread_local! { pub(crate) static ACCOUNT_CRASH_CHECKPOINT: std::cell::RefCell<Option<Box<dyn FnMut(&str,Role,&Connection,AccountExitObserver)>>> = const {std::cell::RefCell::new(None)}; }
fn crash_checkpoint(
    phase: &str,
    role: Role,
    sql: &Connection,
    observed: &ObservedRunner<'_>,
) -> Result<()> {
    #[cfg(test)]
    ACCOUNT_CRASH_CHECKPOINT.with(|s| -> Result<()> {
        if let Some(callback) = s.borrow_mut().as_mut() {
            callback(phase, role, sql, observed.exit_observer()?);
        }
        Ok(())
    })?;
    #[cfg(not(test))]
    let _ = (phase, role, sql, observed);
    Ok(())
}
fn checkpoint(phase: &str, role: Role, sql: &Connection) {
    #[cfg(test)]
    ACCOUNT_CHECKPOINT.with(|s| {
        if let Some(callback) = s.borrow_mut().as_mut() {
            callback(phase, role, sql);
        }
    });
    #[cfg(not(test))]
    let _ = (phase, role, sql);
}
