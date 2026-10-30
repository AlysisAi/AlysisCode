//! One durable consumer self-check resume, followed by original exit and whole
//! consumer-job retirement. There is no arbitrary command or profile operation.
use super::super::super::account_journal::resume_journal::{self, Event};
use super::*;
use crate::account_control::bootstrap::{
    entry::credential_wire::child::probe, pull::FinishedSelfCheck,
};

pub(crate) struct ResumeIntent<'j, 'p, 'a, 'l> {
    jobs: &'j Jobs<'p, 'a, 'l>,
    request: probe::Request,
    attempted: bool,
}
impl ResumeIntent<'_, '_, '_, '_> {
    pub(crate) fn consume(&mut self, observed: &ObservedRunner<'_>) -> Result<probe::Request> {
        ensure!(
            !std::mem::replace(&mut self.attempted, true),
            "paired account resume already attempted"
        );
        self.jobs.verify()?;
        ensure!(
            self.jobs.prefix(Role::Consumers)?.len() == 7
                && !self.jobs.retirements[Role::Consumers.index()].get(),
            "paired resume requires live consumer role"
        );
        ensure!(
            resume_journal::replay(
                self.jobs.plan.database.connection()?,
                &self.jobs.plan.plan,
                Role::Consumers
            )? == [Event::Intent {
                request: self.request.clone()
            }],
            "paired resume intent changed"
        );
        ensure!(
            observed.birth_identity()?
                == (
                    self.request.pid,
                    self.request.created,
                    self.request.birth.selection.image_id
                ),
            "paired resume original account identity differs"
        );
        Ok(self.request.clone())
    }
}

/// Keeps the original authenticated source channel alive after the exact child
/// exit, through retirement intent. No cold constructor or public resume API.
pub(crate) struct CompletedSelfCheck<'j, 'p, 'a, 'l> {
    _running: FinishedSelfCheck<'j>,
    pub(in crate::account_state::execution::paired_plan::native) birth: Bootstrap<'j, 'p, 'a, 'l>,
    request: probe::Request,
    exit: AccountExitObserver,
}
impl CompletedSelfCheck<'_, '_, '_, '_> {
    pub(in crate::account_state::execution::paired_plan::native) fn exit_observer(
        &self,
    ) -> Result<AccountExitObserver> {
        let jobs = self.birth.jobs;
        jobs.verify()?;
        self.birth.child.durable_observation()?;
        ensure!(
            self.birth.role == Role::Consumers,
            "paired self-check role changed"
        );
        let events = resume_journal::replay(
            jobs.plan.database.connection()?,
            &jobs.plan.plan,
            Role::Consumers,
        )?;
        ensure!(
            events.len() == 3
                && events.last()
                    == Some(&Event::Exited {
                        request: self.request.clone(),
                        code: probe::EXIT_CODE
                    }),
            "paired self-check completion differs"
        );
        self.exit.retain_exited_self_check(&self.request)
    }
}

pub(super) fn run<'j, 'p, 'a, 'l>(
    account: ObservedAccount<'j, 'p, 'a, 'l>,
    timeout: Duration,
) -> Result<CompletedConsumers<'j, 'p, 'a, 'l>> {
    run_with_progress(account, timeout, |_, _| {
        anyhow::bail!("consumer progress requires original profile owner")
    })
}

pub(super) fn run_with_progress<'j, 'p, 'a, 'l>(
    account: ObservedAccount<'j, 'p, 'a, 'l>,
    timeout: Duration,
    #[allow(unused_mut, unused_variables)] mut progress: impl FnMut(u32, &BirthRequest) -> Result<()>,
) -> Result<CompletedConsumers<'j, 'p, 'a, 'l>> {
    let jobs = account.birth.jobs;
    let role = account.birth.role;
    let result = (|| {
        ensure!(
            role == Role::Consumers && account.request.purpose.is_paired_consumer(),
            "paired resume admits fixed consumer self-check only"
        );
        ensure!(
            (Duration::from_millis(1)..=Duration::from_secs(10)).contains(&timeout),
            "paired resume timeout exceeds bound"
        );
        account.verify()?;
        let sql = jobs.plan.database.connection()?;
        ensure!(
            resume_journal::replay(sql, &jobs.plan.plan, role)?.is_empty(),
            "paired account resume already recorded"
        );
        let (pid, created, _) = account.observed.birth_identity()?;
        let request = probe::Request {
            birth: account.request.clone(),
            nonce: random_id()?,
            pid,
            created,
        };
        let exit = account.observed.exit_observer()?;
        // Ownership of account is consumed on every failure, even if no intent
        // can commit. No caller retains a live object with which to retry.
        checkpoint("before-intent", sql);
        resume_journal::append(
            sql,
            &jobs.plan.plan,
            role,
            Event::Intent {
                request: request.clone(),
            },
        )?;
        checkpoint("intent", sql);
        let mut ticket = ResumeIntent {
            jobs,
            request: request.clone(),
            attempted: false,
        };
        #[allow(unused_mut)]
        let mut running = account
            .observed
            .start_paired_self_check(&mut ticket, timeout)?;
        jobs.verify()?;
        checkpoint("resumed", sql);
        resume_journal::append(
            sql,
            &jobs.plan.plan,
            role,
            Event::Resumed {
                request: request.clone(),
            },
        )?;
        // Retain this original query-only reference through exact consumer-job
        // retirement. The lowbox already exits before its private USER pair
        // closes; it must never enter the writer family's live-until-drain test.
        #[cfg(test)]
        let _station_child = running.fixture_station_lowbox(timeout, |reference| {
            let original = account
                .birth
                .child
                .fixture_pull_family_reference(reference)?;
            jobs.require_exclusive_member(Role::Consumers, original.raw())?;
            Ok(original)
        })?;
        #[cfg(test)]
        let mut family = Vec::new();
        #[cfg(test)]
        let writer = running.fixture_writer_offer(timeout)?;
        #[cfg(test)]
        let mut lifetime = None;
        #[cfg(test)]
        if let Some(offer) = &writer {
            for reference in offer.references {
                let original = account
                    .birth
                    .child
                    .fixture_pull_family_reference(reference)?;
                jobs.require_exclusive_member(Role::Consumers, original.raw())?;
                let peer = crate::account_control::ExpectedPeer::retain(original.raw())?;
                let selected = &request.birth.selection;
                peer.require_profile_runner(
                    &selected.account.account_sid,
                    selected.session,
                    &selected.account.group_sid,
                )?;
                crate::account_control::writer_fixture::FAMILY_OBSERVED.with(|slot| {
                    if let Some(callback) = slot.borrow_mut().as_mut() {
                        callback(original.raw());
                    }
                });
                family.push(original);
            }
            let began = std::time::Instant::now();
            // Before the ACK that can let the consumer start its first progress
            // receive. Sampling only before our later receive would omit any
            // broker descheduling after this send and could outlive its timeout.
            let mut progress_tick =
                unsafe { windows::Win32::System::SystemInformation::GetTickCount64() };
            running.fixture_acknowledge_writer(timeout)?;
            let Purpose::FixtureProfileWriterV1 { binding } = &request.birth.purpose else {
                unreachable!()
            };
            for round in 1..=binding.hold_rounds {
                running.fixture_writer_progress(round, false, timeout)?;
                progress(round, &request.birth)?;
                crate::account_control::writer_fixture::FAMILY_PROGRESS.with(|slot| {
                    if let Some(callback) = slot.borrow_mut().as_mut() {
                        callback(round, sql, &request, offer, progress_tick);
                    }
                });
                progress_tick =
                    unsafe { windows::Win32::System::SystemInformation::GetTickCount64() };
                running.fixture_writer_progress(round, true, timeout)?;
                crate::account_control::writer_fixture::PROGRESS.with(|slot| slot.set(round));
                for original in &family {
                    ensure!(
                        unsafe {
                            windows::Win32::System::Threading::WaitForSingleObject(
                                original.raw(),
                                0,
                            )
                        } == windows::Win32::Foundation::WAIT_TIMEOUT,
                        "writer family exited during live use"
                    );
                    jobs.require_exclusive_member(Role::Consumers, original.raw())?;
                }
            }
            let elapsed = began.elapsed().as_millis();
            ensure!(
                binding.hold_rounds == 0 || elapsed >= u128::from(binding.hold_rounds) * 1000,
                "fixed writer did not outlive ordinary receive window"
            );
            lifetime = Some(
                serde_json::json!({"rounds":binding.hold_rounds,"elapsed_ms":elapsed,"owner_renewed_each_round":true}),
            );
        }
        let running = running.finish_paired(timeout)?;
        exit.require_self_check_exit(&request)?;
        jobs.verify()?;
        checkpoint("exited", sql);
        resume_journal::append(
            sql,
            &jobs.plan.plan,
            role,
            Event::Exited {
                request: request.clone(),
                code: probe::EXIT_CODE,
            },
        )?;
        checkpoint("retiring", sql);
        #[cfg(test)]
        for original in &family {
            ensure!(
                unsafe {
                    windows::Win32::System::Threading::WaitForSingleObject(original.raw(), 0)
                } == windows::Win32::Foundation::WAIT_TIMEOUT,
                "writer descendant exited before whole-job retirement"
            );
            jobs.require_exclusive_member(Role::Consumers, original.raw())?;
        }
        let checked = CompletedSelfCheck {
            _running: running,
            birth: account.birth,
            request,
            exit,
        };
        jobs.retire(role, RetirementSubject::CheckedAccount(checked))?;
        let completed = CompletedConsumers { jobs };
        completed.verify(jobs)?;
        #[cfg(test)]
        if let Some(offer) = writer {
            let mut descendants = Vec::new();
            for (original, (_, pid, created)) in family.iter().zip(offer.references) {
                ensure!(
                    unsafe {
                        windows::Win32::System::Threading::WaitForSingleObject(original.raw(), 0)
                    } == windows::Win32::Foundation::WAIT_OBJECT_0,
                    "writer descendant survived retirement"
                );
                let mut code = 0;
                unsafe {
                    windows::Win32::System::Threading::GetExitCodeProcess(
                        original.raw(),
                        &mut code,
                    )?;
                }
                ensure!(
                    code == 1,
                    "writer descendant ended without explicit whole-job retirement"
                );
                descendants.push(serde_json::json!({"pid":pid,"created":created,"exit_code":code}));
            }
            crate::account_control::writer_fixture::REPORT.with(|slot| {
                *slot.borrow_mut() = Some(serde_json::json!({
                    "file":offer.file, "registry_written":true, "original_descendants":descendants,
                    "live_after_consumer_exit":true, "consumer_job_retired_before_capture":true,
                    "lifetime":lifetime,
                    "descendants_write_profile":!offer.descendant_files.is_empty(),
                    "descendant_files":offer.descendant_files, "descendant_registry_writes":false,
                    "untrusted_commands_qualified":false
                }))
            });
        }
        Ok(completed)
    })();
    if result.is_err() {
        jobs.job(role)
            .terminate_and_wait()
            .context("paired self-check failure; original consumer job drain failed")?;
    }
    result
}

#[cfg(test)]
thread_local! {pub(crate) static RESUME_CHECKPOINT:std::cell::RefCell<Option<Box<dyn FnMut(&str,&Connection)>>> = const {std::cell::RefCell::new(None)};}
fn checkpoint(phase: &str, sql: &Connection) {
    #[cfg(test)]
    RESUME_CHECKPOINT.with(|s| {
        if let Some(callback) = s.borrow_mut().as_mut() {
            callback(phase, sql);
        }
    });
    #[cfg(not(test))]
    let _ = (phase, sql);
}
