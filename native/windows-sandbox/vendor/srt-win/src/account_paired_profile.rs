//! One original loaded account owns profile create/use/capture/remove. The
//! consumer remains a fixed self-check; no untrusted command is admitted here.
//! Native completion proofs, not cold rows, gate every destructive step.
use super::super::super::account_journal::profile_journal::{self, Event};
use super::*;
use crate::account_control::bootstrap::{
    entry::credential_wire::child::probe,
    pull::{FinishedSelfCheck, RunningSelfCheck},
};
#[cfg(test)]
use crate::account_state::execution::paired_plan::native::namespace::station::initializer::ProfileInitialization;
use crate::account_state::execution::resources::NativeHostProfile;

#[path = "account_paired_profile_lease.rs"]
mod lease;
#[path = "account_paired_profile_wire.rs"]
mod wire;
use wire::{Message, Phase};
pub(crate) use wire::{client, relay};

#[cfg(test)]
pub(crate) fn fixture_write_registry(
    binding: &crate::account_control::bootstrap::entry::credential_wire::child::FixtureWriterBinding,
    actor: &str,
) -> Result<()> {
    binding.validate()?;
    NativeHostProfile::fixture_write_registry(
        &binding.container,
        actor,
        &binding.use_nonce,
        binding.unsupported_registry,
    )
}

fn subject(request: &probe::Request) -> Result<&str> {
    request.validate()?;
    let Purpose::TrustedPairedProfileV1 { container, .. } = &request.birth.purpose else {
        anyhow::bail!("paired profile requires fixed original-owner purpose");
    };
    Ok(container)
}

pub(crate) struct OwnerResumeIntent<'j, 'p, 'a, 'l> {
    jobs: &'j Jobs<'p, 'a, 'l>,
    request: probe::Request,
    attempted: bool,
}
impl OwnerResumeIntent<'_, '_, '_, '_> {
    pub(crate) fn consume(&mut self, observed: &ObservedRunner<'_>) -> Result<probe::Request> {
        ensure!(
            !std::mem::replace(&mut self.attempted, true),
            "paired profile resume already attempted"
        );
        subject(&self.request)?;
        self.jobs.verify()?;
        ensure!(
            self.jobs.prefix(Role::Owner)?.len() == 7
                && !self.jobs.retirements[Role::Owner.index()].get(),
            "paired profile owner no longer ready"
        );
        ensure!(
            profile_journal::replay(
                self.jobs.plan.database.connection()?,
                &self.jobs.plan.plan,
                Role::Owner
            )? == [Event::ResumeIntent {
                request: self.request.clone()
            }],
            "paired profile original resume intent differs"
        );
        ensure!(
            observed.birth_identity()?
                == (
                    self.request.pid,
                    self.request.created,
                    self.request.birth.selection.image_id
                ),
            "paired profile original owner differs"
        );
        Ok(self.request.clone())
    }
}

/// No constructor outside this module. Capture/removal tickets own the exact
/// live consumer-completion proof; parsing the same bytes cannot create one.
pub(crate) struct StepIntent<'j, 'p, 'a, 'l> {
    jobs: &'j Jobs<'p, 'a, 'l>,
    message: Message,
    consumers: Option<CompletedConsumers<'j, 'p, 'a, 'l>>,
    attempted: bool,
}
impl StepIntent<'_, '_, '_, '_> {
    pub(crate) fn consume(&mut self, request: &probe::Request) -> Result<Vec<u8>> {
        ensure!(
            !std::mem::replace(&mut self.attempted, true),
            "paired profile step already attempted"
        );
        self.jobs.verify()?;
        ensure!(
            &self.message.request == request
                && self.jobs.prefix(Role::Owner)?.len() == 7
                && !self.jobs.retirements[Role::Owner.index()].get(),
            "paired profile live owner/request changed"
        );
        let length = match self.message.phase {
            Phase::Create => 3,
            Phase::Use => 5,
            Phase::Capture => 7,
            Phase::Remove => 9,
            Phase::Removed => 10,
            _ => anyhow::bail!("paired profile step is not an intent"),
        };
        if matches!(
            self.message.phase,
            Phase::Capture | Phase::Remove | Phase::Removed
        ) {
            self.consumers
                .as_ref()
                .context("paired profile requires original consumer completion")?
                .verify(self.jobs)?;
        } else {
            ensure!(
                self.consumers.is_none(),
                "unexpected paired completion proof"
            );
        }
        let events = profile_journal::replay(
            self.jobs.plan.database.connection()?,
            &self.jobs.plan.plan,
            Role::Owner,
        )?;
        ensure!(
            events.len() == length && events.last() == Some(&self.message.event()?),
            "paired profile original durable step changed"
        );
        self.message.wire()
    }
}

struct ActiveOwner<'j, 'p, 'a, 'l> {
    running: RunningSelfCheck<'j>,
    birth: Bootstrap<'j, 'p, 'a, 'l>,
    request: probe::Request,
    exit: AccountExitObserver,
    last: Message,
    lease: Option<lease::UseLease>,
}
impl<'j, 'p, 'a, 'l> ActiveOwner<'j, 'p, 'a, 'l> {
    fn begin(account: ObservedAccount<'j, 'p, 'a, 'l>, timeout: Duration) -> Result<Self> {
        ensure!(
            account.birth.role == Role::Owner,
            "profile requires original owner role"
        );
        account.verify()?;
        let jobs = account.birth.jobs;
        let sql = jobs.plan.database.connection()?;
        ensure!(
            profile_journal::replay(sql, &jobs.plan.plan, Role::Owner)?.is_empty(),
            "paired profile already attempted"
        );
        let (pid, created, _) = account.observed.birth_identity()?;
        let request = probe::Request {
            birth: account.request.clone(),
            nonce: random_id()?,
            pid,
            created,
        };
        subject(&request)?;
        let exit = account.observed.exit_observer()?;
        checkpoint("before-resume-intent", sql);
        profile_journal::append(
            sql,
            &jobs.plan.plan,
            Role::Owner,
            Event::ResumeIntent {
                request: request.clone(),
            },
        )?;
        let mut intent = OwnerResumeIntent {
            jobs,
            request: request.clone(),
            attempted: false,
        };
        let running = account
            .observed
            .start_paired_profile(&mut intent, timeout)?;
        checkpoint("resumed", sql);
        profile_journal::append(
            sql,
            &jobs.plan.plan,
            Role::Owner,
            Event::Resumed {
                request: request.clone(),
            },
        )?;
        let mut active = Self {
            running,
            birth: account.birth,
            last: Message::create(&request),
            request,
            exit,
            lease: None,
        };
        let create = active.last.clone();
        let (created, _) = active.exchange(create, None, timeout)?;
        active.last = created;
        Ok(active)
    }
    fn exchange(
        &mut self,
        message: Message,
        consumers: Option<CompletedConsumers<'j, 'p, 'a, 'l>>,
        timeout: Duration,
    ) -> Result<(Message, Option<CompletedConsumers<'j, 'p, 'a, 'l>>)> {
        let jobs = self.birth.jobs;
        self.birth.child.durable_observation()?;
        let sql = jobs.plan.database.connection()?;
        let reply = match message.phase {
            Phase::Create => Phase::Created,
            Phase::Use => Phase::Using,
            Phase::Capture => Phase::Captured,
            Phase::Remove => Phase::Removed,
            _ => anyhow::bail!("invalid paired exchange"),
        };
        checkpoint(
            match message.phase {
                Phase::Create => "before-create",
                Phase::Use => "before-use",
                Phase::Capture => "before-capture",
                Phase::Remove => "before-remove",
                _ => unreachable!(),
            },
            sql,
        );
        profile_journal::append(sql, &jobs.plan.plan, Role::Owner, message.event()?)?;
        let mut intent = StepIntent {
            jobs,
            message: message.clone(),
            consumers,
            attempted: false,
        };
        let response = self.running.paired_profile_step(&mut intent, timeout)?;
        let receipt = Message::read(&response, &self.request, reply)?;
        receipt.follows(&message)?;
        jobs.verify()?;
        self.birth.child.durable_observation()?;
        checkpoint(
            match reply {
                Phase::Created => "created",
                Phase::Using => "using",
                Phase::Captured => "captured",
                Phase::Removed => "removed",
                _ => unreachable!(),
            },
            sql,
        );
        profile_journal::append(sql, &jobs.plan.plan, Role::Owner, receipt.event()?)?;
        Ok((receipt, intent.consumers.take()))
    }
    fn begin_use(
        &mut self,
        consumer: &ObservedAccount<'j, 'p, 'a, 'l>,
        timeout: Duration,
    ) -> Result<()> {
        let jobs = self.birth.jobs;
        ensure!(
            std::ptr::eq(jobs, consumer.birth.jobs) && consumer.birth.role == Role::Consumers,
            "profile consumer belongs to another live pair"
        );
        consumer.verify()?;
        let mut message = self.last.at(Phase::Use);
        message.use_nonce = Some(random_id()?);
        #[cfg(test)]
        if let Purpose::FixtureProfileWriterV1 { binding } = &consumer.request.purpose {
            binding.require_owner(
                &self.request,
                self.last.folder.context("writer original folder missing")?,
                &binding.use_nonce,
            )?;
            message.use_nonce = Some(binding.use_nonce.clone());
        }
        #[cfg(test)]
        if let Purpose::FixtureStationLowboxV1 { binding } = &consumer.request.purpose {
            binding.require_owner(
                &self.request,
                self.last
                    .folder
                    .context("station probe original folder missing")?,
                &binding.profile.use_nonce,
            )?;
            message.use_nonce = Some(binding.profile.use_nonce.clone());
        }
        message.consumer = Some(consumer.request.clone());
        message.follows(&self.last)?;
        let (using, _) = self.exchange(message, None, timeout)?;
        self.last = using;
        self.lease = Some(lease::UseLease::new(&self.last)?);
        consumer.verify()
    }
    fn renew(&mut self, consumer: &BirthRequest, timeout: Duration) -> Result<()> {
        ensure!(
            self.last.phase == Phase::Using && self.last.consumer.as_ref() == Some(consumer),
            "paired renewal consumer differs"
        );
        self.birth.child.durable_observation()?;
        let lease = self
            .lease
            .as_mut()
            .context("original profile use lease absent")?;
        let mut intent = RenewalIntent {
            jobs: self.birth.jobs,
            using: self.last.clone(),
            body: lease.renewal()?,
            attempted: false,
        };
        let response = self.running.renew_paired_profile(&mut intent, timeout)?;
        lease.confirm(&response)?;
        self.birth.child.durable_observation().map(|_| ())
    }
    fn finish(
        mut self,
        consumers: CompletedConsumers<'j, 'p, 'a, 'l>,
        timeout: Duration,
    ) -> Result<CompletedPair<'j, 'p, 'a, 'l>> {
        let jobs = self.birth.jobs;
        consumers.verify(jobs)?;
        self.lease
            .as_ref()
            .context("original profile use lease absent")?
            .require_current()?;
        let mut capture = self.last.at(Phase::Capture);
        capture.retirement = Some(
            jobs.plan.plan.roles[Role::Consumers.index()]
                .retire_nonce
                .clone(),
        );
        capture.follows(&self.last)?;
        let (captured, consumers) = self.exchange(capture, Some(consumers), timeout)?;
        let remove = captured.at(Phase::Remove);
        remove.follows(&captured)?;
        let (removed, consumers) = self.exchange(remove, consumers, timeout)?;
        let mut confirmed = StepIntent {
            jobs,
            message: removed,
            consumers,
            attempted: false,
        };
        self.running
            .complete_paired_profile(&mut confirmed, timeout)?;
        let running = self.running.finish_paired_profile(timeout)?;
        self.exit.require_paired_profile_exit(&self.request)?;
        let sql = jobs.plan.database.connection()?;
        checkpoint("exited", sql);
        profile_journal::append(
            sql,
            &jobs.plan.plan,
            Role::Owner,
            Event::Exited {
                request: self.request.clone(),
                code: probe::EXIT_CODE,
            },
        )?;
        confirmed
            .consumers
            .as_ref()
            .context("paired original completion missing")?
            .verify(jobs)?;
        let completed = CompletedOwner {
            _running: running,
            birth: self.birth,
            request: self.request,
            exit: self.exit,
        };
        checkpoint("retiring", sql);
        jobs.retire(Role::Owner, RetirementSubject::ProfileOwner(completed))?;
        let pair = CompletedPair { jobs };
        pair.verify()?;
        Ok(pair)
    }
}

/// Constructed only by the original active owner with its in-memory use lease.
/// A renewal grants no native effect, resume, retirement or cleanup permission.
pub(crate) struct RenewalIntent<'j, 'p, 'a, 'l> {
    jobs: &'j Jobs<'p, 'a, 'l>,
    using: Message,
    body: Vec<u8>,
    attempted: bool,
}
impl RenewalIntent<'_, '_, '_, '_> {
    pub(crate) fn consume(&mut self, request: &probe::Request) -> Result<Vec<u8>> {
        ensure!(
            !std::mem::replace(&mut self.attempted, true),
            "paired renewal already attempted"
        );
        self.jobs.verify()?;
        ensure!(
            &self.using.request == request
                && self.using.phase == Phase::Using
                && self.jobs.prefix(Role::Owner)?.len() == 7
                && self.jobs.prefix(Role::Consumers)?.len() == 7,
            "paired renewal original live roles differ"
        );
        let sql = self.jobs.plan.database.connection()?;
        let profiles = profile_journal::replay(sql, &self.jobs.plan.plan, Role::Owner)?;
        ensure!(
            profiles.len() == 6 && profiles.last() == Some(&self.using.event()?),
            "paired renewal requires original durable use"
        );
        let resumed = super::super::super::account_journal::resume_journal::replay(
            sql,
            &self.jobs.plan.plan,
            Role::Consumers,
        )?;
        ensure!(
            resumed.len() == 2,
            "paired renewal requires original live consumer resume"
        );
        let super::super::super::account_journal::resume_journal::Event::Resumed {
            request: consumer,
        } = &resumed[1]
        else {
            anyhow::bail!("paired consumer resume receipt absent")
        };
        ensure!(
            self.using.consumer.as_ref() == Some(&consumer.birth),
            "paired renewal original consumer resume differs"
        );
        Ok(std::mem::take(&mut self.body))
    }
}

pub(crate) struct CompletedOwner<'j, 'p, 'a, 'l> {
    _running: FinishedSelfCheck<'j>,
    pub(in crate::account_state::execution::paired_plan::native) birth: Bootstrap<'j, 'p, 'a, 'l>,
    request: probe::Request,
    exit: AccountExitObserver,
}
impl CompletedOwner<'_, '_, '_, '_> {
    pub(in crate::account_state::execution::paired_plan::native) fn exit_observer(
        &self,
    ) -> Result<AccountExitObserver> {
        let jobs = self.birth.jobs;
        jobs.verify()?;
        self.birth.child.durable_observation()?;
        ensure!(
            self.birth.role == Role::Owner,
            "paired profile completion role differs"
        );
        let events = profile_journal::replay(
            jobs.plan.database.connection()?,
            &jobs.plan.plan,
            Role::Owner,
        )?;
        ensure!(
            events.len() == 11
                && events.last()
                    == Some(&Event::Exited {
                        request: self.request.clone(),
                        code: probe::EXIT_CODE
                    }),
            "paired profile original completion differs"
        );
        self.exit.retain_exited_paired_profile(&self.request)
    }
}

impl<'p, 'a, 'l> Jobs<'p, 'a, 'l> {
    pub(crate) fn run_profile_self_check(
        &self,
        owner: ControlServer,
        consumer: ControlServer,
        timeout: Duration,
    ) -> Result<CompletedPair<'_, 'p, 'a, 'l>> {
        self.run_profile_inner(
            owner,
            consumer,
            timeout,
            None,
            None,
            #[cfg(test)]
            None,
        )
    }
    #[cfg(test)]
    pub(crate) fn fixture_run_profile_self_check(
        &self,
        owner: ControlServer,
        consumer: ControlServer,
        timeout: Duration,
        password: &str,
    ) -> Result<CompletedPair<'_, 'p, 'a, 'l>> {
        self.run_profile_inner(owner, consumer, timeout, Some(password), None, None)
    }
    #[cfg(test)]
    pub(crate) fn fixture_run_profile_writer(
        &self,
        owner: ControlServer,
        consumer: ControlServer,
        timeout: Duration,
        password: &str,
        unsupported: bool,
    ) -> Result<CompletedPair<'_, 'p, 'a, 'l>> {
        self.run_profile_inner(
            owner,
            consumer,
            timeout,
            Some(password),
            Some((unsupported, 0, None, false)),
            None,
        )
    }
    #[cfg(test)]
    pub(crate) fn fixture_run_profile_lifetime(
        &self,
        owner: ControlServer,
        consumer: ControlServer,
        timeout: Duration,
        password: &str,
        mode: u32,
        descendant_files: bool,
    ) -> Result<CompletedPair<'_, 'p, 'a, 'l>> {
        ensure!(mode <= 2, "fixed lifetime scenario outside bound");
        self.run_profile_inner(
            owner,
            consumer,
            timeout,
            Some(password),
            Some((
                false,
                12,
                if mode == 0 {
                    None
                } else {
                    Some((6, mode == 2))
                },
                descendant_files,
            )),
            None,
        )
    }
    #[cfg(test)]
    pub(crate) fn fixture_run_initialized_profile_lifetime(
        &self,
        owner: ControlServer,
        consumer: ControlServer,
        timeout: Duration,
        password: &str,
        initializer: &mut ProfileInitialization<'_, 'p, 'a, 'l>,
    ) -> Result<CompletedPair<'_, 'p, 'a, 'l>> {
        self.run_profile_inner(
            owner,
            consumer,
            timeout,
            Some(password),
            Some((false, 12, None, true)),
            Some(initializer),
        )
    }
    fn run_profile_inner(
        &self,
        owner_server: ControlServer,
        consumer_server: ControlServer,
        timeout: Duration,
        fixture_password: Option<&str>,
        fixture_writer: Option<(bool, u32, Option<(u32, bool)>, bool)>,
        #[cfg(test)] initializer: Option<&mut ProfileInitialization<'_, 'p, 'a, 'l>>,
    ) -> Result<CompletedPair<'_, 'p, 'a, 'l>> {
        // Keep the original profile owner outside the fallible consumer scope.
        // A consumer failure drains its entire job before owner guards drop.
        let mut owner = None;
        let result = (|| {
            self.verify()?;
            let purpose = Purpose::TrustedPairedProfileV1 {
                container: self.plan.plan.execution.identity.container.clone(),
                package_sid: self.plan.plan.execution.identity.package_sid.clone(),
                policy: 1,
            };
            let original = self
                .prepare_bootstrap(Role::Owner)?
                .start(owner_server, timeout)?
                .observe_account_inner(timeout, fixture_password, purpose)?;
            owner = Some(ActiveOwner::begin(original, timeout)?);
            // Profile loading/creation can be slow. Only now start the consumer
            // source, whose fixed control deadlines remain short and bounded.
            let consumer_purpose = Purpose::TrustedSelfCheckV1;
            #[cfg(not(test))]
            ensure!(fixture_writer.is_none(), "writer fixture unavailable");
            #[cfg(test)]
            let mut consumer_purpose = if let Some((
                unsupported,
                hold_rounds,
                _,
                descendant_files,
            )) = fixture_writer
            {
                ensure!(
                    fixture_password.is_some(),
                    "writer requires disposable fixture credential"
                );
                let active = owner.as_ref().unwrap();
                Purpose::FixtureProfileWriterV1 { binding: crate::account_control::bootstrap::entry::credential_wire::child::FixtureWriterBinding {
                    container: self.plan.plan.execution.identity.container.clone(),
                    package_sid: self.plan.plan.execution.identity.package_sid.clone(),
                    folder: active.last.folder.context("writer profile not created")?,
                    owner_birth: active.request.birth.nonce.clone(),
                    owner_resume: active.request.nonce.clone(),
                    owner_pid: active.request.pid, owner_created: active.request.created,
                    use_nonce: random_id()?, unsupported_registry: unsupported, hold_rounds, descendant_files,
                }}
            } else {
                consumer_purpose
            };
            let consumer = self
                .prepare_bootstrap(Role::Consumers)?
                .start(consumer_server, timeout)?;
            #[cfg(test)]
            let consumer = {
                let mut consumer = consumer;
                // The original owner already holds its active profile. Run
                // only this sealed test initializer on the ready consumer
                // source, before credential selection or account-child birth.
                if let Some(initializer) = initializer {
                    initializer.start(self, &mut consumer)?;
                    let contained = crate::account_rpc_v3_lifecycle_fixture::contained_mode()?;
                    if contained.is_some()
                        || crate::account_control::station_lowbox_fixture::ENABLED.with(|s| s.get())
                    {
                        ensure!(
                            fixture_password.is_some(),
                            "station probe requires owned disposable credential"
                        );
                        let active = owner.as_ref().context("station profile owner absent")?;
                        let original =
                            initializer.fixture_station_probe_subject(self, &consumer)?;
                        let binding = crate::account_control::bootstrap::entry::credential_wire::child::FixtureStationLowboxBinding {
                            profile: crate::account_control::bootstrap::entry::credential_wire::child::FixtureWriterBinding {
                                container: self.plan.plan.execution.identity.container.clone(),
                                package_sid: self.plan.plan.execution.identity.package_sid.clone(),
                                folder: active.last.folder.context("station profile folder absent")?,
                                owner_birth: active.request.birth.nonce.clone(), owner_resume: active.request.nonce.clone(),
                                owner_pid: active.request.pid, owner_created: active.request.created,
                                use_nonce: random_id()?, unsupported_registry: false, hold_rounds: 0, descendant_files: false,
                            }, initializer: original.clone(), policy: self.plan.plan.rpc_policy.fixture_binding_policy(),
                            completion: match contained {
                                Some(crate::account_install::AccountMode::Offline) => crate::account_control::bootstrap::entry::credential_wire::child::LowboxCompletion::V3ShadowBoundedV1,
                                Some(crate::account_install::AccountMode::Online) => crate::account_control::bootstrap::entry::credential_wire::child::LowboxCompletion::V3ShadowOnlineBoundedV1,
                                None => Default::default(),
                            },
                        };
                        binding.require_owner(
                            &active.request,
                            binding.profile.folder,
                            &binding.profile.use_nonce,
                        )?;
                        binding.require_initializer(&original)?;
                        binding.require_rpc_policy(self.plan.plan.rpc_policy)?;
                        binding.require_namespace_birth(self.plan.plan.namespace_birth)?;
                        consumer_purpose = Purpose::FixtureStationLowboxV1 { binding };
                    }
                }
                ensure!(
                    !(crate::account_control::station_lowbox_fixture::ENABLED.with(|s| s.get())
                        || crate::account_rpc_v3_lifecycle_fixture::contained_mode()?.is_some())
                        || matches!(consumer_purpose, Purpose::FixtureStationLowboxV1 { .. }),
                    "station probe original live initializer missing"
                );
                consumer
            };
            let consumer =
                consumer.observe_account_inner(timeout, fixture_password, consumer_purpose)?;
            owner.as_mut().unwrap().begin_use(&consumer, timeout)?;
            let completed = resume::run_with_progress(consumer, timeout, |round, consumer| {
                #[cfg(test)]
                if let Some((_, _, Some((stop, kill_source)), _)) = fixture_writer {
                    if stop == round {
                        if kill_source {
                            owner
                                .as_ref()
                                .context("original profile owner absent")?
                                .birth
                                .child
                                .fixture_terminate_original_source()?;
                        } else {
                            anyhow::bail!("fixed original-owner renewal deliberately interrupted");
                        }
                    }
                }
                #[cfg(not(test))]
                let _ = round;
                owner
                    .as_mut()
                    .context("original profile owner absent")?
                    .renew(consumer, timeout)
            })?;
            owner.take().unwrap().finish(completed, timeout)
        })();
        if let Err(error) = result {
            if let Err(drain) = self.job(Role::Consumers).terminate_and_wait() {
                // Match strict Job::Drop: do not unwind through the live owner
                // when consumer quiescence is unknown. Kernel teardown still
                // closes both original jobs; durable pending evidence remains.
                crate::util::exit_for_unverified_cleanup("paired profile consumers", &drain);
            }
            let owners = self.job(Role::Owner).terminate_and_wait();
            drop(owner);
            owners.context("paired profile failure; original owner drain failed")?;
            return Err(error);
        }
        result
    }
}

#[cfg(test)]
thread_local! {pub(crate) static PROFILE_CHECKPOINT:std::cell::RefCell<Option<Box<dyn FnMut(&str,&Connection)>>> = const {std::cell::RefCell::new(None)};}
fn checkpoint(phase: &str, sql: &Connection) {
    #[cfg(test)]
    PROFILE_CHECKPOINT.with(|s| {
        if let Some(callback) = s.borrow_mut().as_mut() {
            callback(phase, sql);
        }
    });
    #[cfg(not(test))]
    let _ = (phase, sql);
}
