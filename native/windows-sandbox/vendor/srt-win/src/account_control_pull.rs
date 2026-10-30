//! Receiver-owned observation transfer from the exact original bootstrap. A
//! source handle number is untrusted input to DuplicateHandle, never a local
//! owned handle, PID lookup, termination/resume right or durable birth receipt.
//! Only the broker duplicates into its own table, so a failed exchange cannot
//! strand a pushed handle in another process. The sender keeps its child guard.
use super::entry::credential_wire::child::{BirthRequest, probe};
use crate::account_state::execution::bootstrap_plan::lifecycle::resume::logon::delivery::birth::account_resume::ResumeIntent;
use super::*;
use serde::{Deserialize, Serialize};

const PROTOCOL: &str = "alysis-bootstrap-process-observation-v1";

#[derive(Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Request {
    protocol: String,
    nonce: [u8; 16],
    birth: Option<BirthRequest>,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Offer {
    request: Request,
    source_handle: u64,
    created: u64,
}

/// Single-use exchange tied by native object comparison to the sealed birth
/// guard, not just a caller-selected ExpectedPeer or channel nonce. Construction
/// consumes the one offer attempt even when validation fails.
pub(crate) struct ProcessReceiver<'b, 'a> {
    bootstrap: &'b SuspendedBootstrap<'a>,
    channel: Option<ControlChannel<'a>>,
    account: String,
    session: u32,
    required_group: Option<String>,
    birth: Option<BirthRequest>,
}

impl<'b, 'a> ProcessReceiver<'b, 'a> {
    pub(crate) fn prepare(
        bootstrap: &'b mut SuspendedBootstrap<'a>,
        channel: ControlChannel<'a>,
        ready: &crate::account_install::ReadyInstallation,
        mode: crate::account_install::AccountMode,
    ) -> Result<Self> {
        // No raw SID override on the production path. Installation validation
        // precedes any use of an offered handle. Bootstrap birth itself remains
        // gated; this transport primitive does not satisfy that lifecycle gate.
        let result = (|| {
            bootstrap.image.require_installation(ready)?;
            let binding = ready.binding(mode)?;
            binding.validate()?;
            ensure!(
                binding.owner_sid == crate::sid::current_user_sid()?,
                "process offer owner differs"
            );
            Ok((binding.account_sid, binding.group_sid))
        })();
        match result {
            Ok((account, group)) => {
                let mut receiver = Self::create(bootstrap, channel, account)?;
                receiver.required_group = Some(group);
                Ok(receiver)
            }
            Err(error) => {
                bootstrap.offer_attempted = true;
                Err(error)
            }
        }
    }

    fn create(
        bootstrap: &'b mut SuspendedBootstrap<'a>,
        channel: ControlChannel<'a>,
        account: String,
    ) -> Result<Self> {
        ensure!(
            !bootstrap.offer_attempted,
            "bootstrap process offer already attempted"
        );
        bootstrap.offer_attempted = true;
        let channel = channel.into_role(true)?;
        bootstrap.peer()?;
        ensure!(
            unsafe { CompareObjectHandles(bootstrap.child.process(), channel.peer.process.raw()) }
                .as_bool(),
            "process offer channel is not the original bootstrap"
        );
        let mut session = 0;
        unsafe {
            windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
                std::process::id(),
                &mut session,
            )?;
        }
        Ok(Self {
            bootstrap,
            channel: Some(channel),
            account,
            session,
            required_group: None,
            birth: None,
        })
    }

    pub(crate) fn bind_birth(mut self, request: &BirthRequest) -> Result<Self> {
        request.validate()?;
        ensure!(
            self.birth.is_none()
                && request.selection.account.account_sid == self.account
                && self.required_group.as_deref()
                    == Some(request.selection.account.group_sid.as_str())
                && self.session == request.selection.session
                && self.bootstrap.durable_observation()?
                    == (
                        request.selection.bootstrap_pid,
                        request.selection.bootstrap_created,
                        request.selection.image_id
                    ),
            "account birth offer differs from original selected bootstrap"
        );
        self.birth = Some(request.clone());
        Ok(self)
    }

    /// Consumes the entire exchange. No error or successful observation can be
    /// retried against this birth guard. No acknowledgment authorizing resume is
    /// sent here; the future durable lifecycle must supply that separate step.
    pub(crate) fn receive(mut self, timeout: Duration) -> Result<ObservedRunner<'a>> {
        #[cfg(not(test))]
        ensure!(
            self.required_group.is_some(),
            "dedicated runner policy binding missing"
        );
        let mut channel = self
            .channel
            .take()
            .context("process offer permanently refused")?;
        let request = Request {
            protocol: PROTOCOL.into(),
            nonce: ControlAddress::fresh()?.nonce,
            birth: self.birth.clone(),
        };
        self.bootstrap.peer()?;
        channel.send(&serde_json::to_vec(&request)?, timeout)?;
        let offer: Offer = serde_json::from_slice(&channel.receive(timeout)?)
            .context("invalid process observation offer")?;
        ensure!(
            offer.request == request,
            "process offer protocol/operation mismatch"
        );
        ensure!(
            offer.source_handle > 0 && offer.source_handle < isize::MAX as u64,
            "invalid source process handle"
        );
        self.bootstrap.peer()?;
        let mut raw = HANDLE::default();
        unsafe {
            DuplicateHandle(
                self.bootstrap.child.process(),
                HANDLE(offer.source_handle as usize as *mut _),
                GetCurrentProcess(),
                &mut raw,
                (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
                false,
                DUPLICATE_HANDLE_OPTIONS(0),
            )
        }
        .context("pull original process observation")?;
        let pulled = OwnedHandle(raw); // Own before every fallible validation.
        noninheritable(pulled.raw())?;
        ensure!(
            unsafe { GetProcessId(pulled.raw()) } != 0,
            "offered object is not a process"
        );
        ensure!(
            !unsafe { CompareObjectHandles(pulled.raw(), self.bootstrap.child.process()) }
                .as_bool(),
            "bootstrap cannot offer itself as its account child"
        );
        let created = creation_time(pulled.raw())?;
        ensure!(
            created == offer.created && created >= creation_time(self.bootstrap.child.process())?,
            "offered process creation identity differs"
        );
        let peer = ExpectedPeer::retain(pulled.raw())?
            .with_job(self.bootstrap.job)?
            .with_image(self.bootstrap.image)?;
        peer.token
            .require_account_runner(&self.account, self.session)?;
        if let Some(group) = &self.required_group {
            peer.token
                .require_dedicated_runner(&self.account, self.session, group)?;
        }
        // Both original objects remain live and unchanged across the pull. The
        // source guard remains responsible for termination on a failed handoff.
        self.bootstrap.peer()?;
        channel.peer.verify(channel.handle.raw(), true)?;
        peer.alive()?;
        Ok(ObservedRunner {
            peer,
            created,
            channel,
            birth: request.birth,
        })
    }
}

/// Query/synchronize authority only. Retain the channel so the source continues
/// to hold its original suspended-child guard; dropping this observation closes
/// the exchange. There is no raw handle or generic resume API.
pub(crate) struct ObservedRunner<'a> {
    peer: ExpectedPeer<'a>,
    created: u64,
    channel: ControlChannel<'a>,
    birth: Option<BirthRequest>,
}

impl<'a> ObservedRunner<'a> {
    pub(crate) fn birth_identity(&self) -> Result<(u32, u64, [u8; 24])> {
        self.channel.peer.verify(self.channel.handle.raw(), true)?;
        self.peer.alive()?;
        ensure!(
            creation_time(self.peer.process.raw())? == self.created,
            "original account birth identity changed"
        );
        Ok((
            self.peer.pid,
            self.created,
            self.peer
                .image
                .context("account image binding missing")?
                .file_identity()?,
        ))
    }
    pub(crate) fn acknowledge_birth(
        &mut self,
        request: &BirthRequest,
        timeout: Duration,
    ) -> Result<()> {
        request.validate()?;
        ensure!(
            self.birth.as_ref() == Some(request),
            "account birth acknowledgment lacks original request"
        );
        let (pid, created, image) = self.birth_identity()?;
        ensure!(
            image == request.selection.image_id,
            "account acknowledgment image differs"
        );
        let frame = request.committed(pid, created)?;
        self.channel.send(&frame, timeout)?;
        ensure!(
            self.channel.receive(timeout)? == frame,
            "account birth acknowledgment differs"
        );
        self.birth_identity()?;
        Ok(())
    }
    pub(crate) fn start_self_check(
        mut self,
        intent: &mut ResumeIntent<'_, '_, '_, '_>,
        timeout: Duration,
    ) -> Result<RunningSelfCheck<'a>> {
        let request = intent.consume(&self)?;
        ensure!(
            self.birth.as_ref() == Some(&request.birth),
            "account resume birth differs"
        );
        self.channel.send(&request.frame("resume")?, timeout)?;
        ensure!(
            self.channel.receive(timeout)? == request.frame("resumed")?,
            "account resume acknowledgment differs"
        );
        Ok(RunningSelfCheck {
            observed: self,
            request,
        })
    }
    pub(crate) fn exit_observer(&self) -> Result<AccountExitObserver> {
        self.birth_identity()?;
        Ok(AccountExitObserver(
            ExpectedPeer::retain(self.peer.process.raw())?.process,
        ))
    }
    pub(crate) fn start_paired_self_check(
        mut self,
        intent: &mut crate::account_state::execution::paired_plan::native::account::resume::ResumeIntent<'_, '_, '_, '_>,
        timeout: Duration,
    ) -> Result<RunningSelfCheck<'a>> {
        let request = intent.consume(&self)?;
        ensure!(
            self.birth.as_ref() == Some(&request.birth)
                && request.birth.purpose.is_paired_consumer(),
            "paired self-check requires exact fixed-purpose birth"
        );
        self.channel.send(&request.frame("resume")?, timeout)?;
        ensure!(
            self.channel.receive(timeout)? == request.frame("resumed")?,
            "paired self-check resume acknowledgment differs"
        );
        Ok(RunningSelfCheck {
            observed: self,
            request,
        })
    }
    pub(crate) fn start_paired_profile(
        mut self,
        intent: &mut crate::account_state::execution::paired_plan::native::account::profile::OwnerResumeIntent<'_, '_, '_, '_>,
        timeout: Duration,
    ) -> Result<RunningSelfCheck<'a>> {
        let request = intent.consume(&self)?;
        ensure!(
            self.birth.as_ref() == Some(&request.birth)
                && matches!(
                    request.birth.purpose,
                    super::entry::credential_wire::child::Purpose::TrustedPairedProfileV1 { .. }
                ),
            "paired profile requires exact original owner birth"
        );
        self.channel.send(&request.frame("resume")?, timeout)?;
        ensure!(
            self.channel.receive(timeout)? == request.frame("resumed")?,
            "paired profile resume acknowledgment differs"
        );
        Ok(RunningSelfCheck {
            observed: self,
            request,
        })
    }
}

/// The one source resume acknowledgment has arrived. Only read-only exit
/// observation remains; this object cannot retry resume or authorize cleanup.
pub(crate) struct RunningSelfCheck<'a> {
    observed: ObservedRunner<'a>,
    request: probe::Request,
}
/// Retains the authenticated source channel after the one exit acknowledgment.
/// There is no second finish/resume operation on this completed state.
pub(crate) struct FinishedSelfCheck<'a> {
    _running: RunningSelfCheck<'a>,
}
impl RunningSelfCheck<'_> {
    #[cfg(test)]
    pub(crate) fn fixture_writer_offer(
        &mut self,
        timeout: Duration,
    ) -> Result<Option<crate::account_control::writer_fixture::Offer>> {
        if !matches!(
            self.request.birth.purpose,
            super::entry::credential_wire::child::Purpose::FixtureProfileWriterV1 { .. }
        ) {
            return Ok(None);
        }
        self.observed.birth_identity()?;
        let offer = crate::account_control::writer_fixture::Offer::read(
            &self.observed.channel.receive(timeout)?,
            &self.request,
        )?;
        self.observed.birth_identity()?;
        Ok(Some(offer))
    }
    #[cfg(test)]
    pub(crate) fn fixture_acknowledge_writer(&mut self, timeout: Duration) -> Result<()> {
        self.observed.birth_identity()?;
        self.observed.channel.send(
            &crate::account_control::writer_fixture::acknowledgment(&self.request)?,
            timeout,
        )
    }
    pub(crate) fn paired_profile_step(
        &mut self,
        intent: &mut crate::account_state::execution::paired_plan::native::account::profile::StepIntent<'_, '_, '_, '_>,
        timeout: Duration,
    ) -> Result<Vec<u8>> {
        self.observed.birth_identity()?;
        let body = intent.consume(&self.request)?;
        self.observed.channel.send(&body, timeout)?;
        let response = self.observed.channel.receive(timeout)?;
        self.observed.birth_identity()?;
        Ok(response)
    }
    pub(crate) fn renew_paired_profile(
        &mut self,
        intent: &mut crate::account_state::execution::paired_plan::native::account::profile::RenewalIntent<'_, '_, '_, '_>,
        timeout: Duration,
    ) -> Result<Vec<u8>> {
        self.observed.birth_identity()?;
        self.observed
            .channel
            .send(&intent.consume(&self.request)?, timeout)?;
        let response = self.observed.channel.receive(timeout)?;
        self.observed.birth_identity()?;
        Ok(response)
    }
    #[cfg(test)]
    pub(crate) fn fixture_writer_progress(
        &mut self,
        round: u32,
        acknowledge: bool,
        timeout: Duration,
    ) -> Result<()> {
        self.observed.birth_identity()?;
        let body =
            crate::account_control::writer_fixture::progress(&self.request, round, acknowledge)?;
        if acknowledge {
            self.observed.channel.send(&body, timeout)?;
        } else {
            ensure!(
                self.observed.channel.receive(timeout)? == body,
                "fixed writer progress differs"
            );
        }
        self.observed.birth_identity()?;
        Ok(())
    }
    pub(crate) fn complete_paired_profile(
        &mut self,
        intent: &mut crate::account_state::execution::paired_plan::native::account::profile::StepIntent<'_, '_, '_, '_>,
        timeout: Duration,
    ) -> Result<()> {
        self.observed.birth_identity()?;
        self.observed
            .channel
            .send(&intent.consume(&self.request)?, timeout)
    }
    pub(crate) fn complete_profile(
        &mut self,
        intent: &mut crate::account_state::execution::bootstrap_plan::lifecycle::resume::logon::delivery::birth::account_resume::account_profile::StepIntent<'_, '_, '_, '_>,
        timeout: Duration,
    ) -> Result<()> {
        self.observed.birth_identity()?;
        let body = intent.consume(&self.request)?;
        self.observed.channel.send(&body, timeout)
    }
    pub(crate) fn profile_step(
        &mut self,
        intent: &mut crate::account_state::execution::bootstrap_plan::lifecycle::resume::logon::delivery::birth::account_resume::account_profile::StepIntent<'_, '_, '_, '_>,
        timeout: Duration,
    ) -> Result<Vec<u8>> {
        self.observed.birth_identity()?;
        let body = intent.consume(&self.request)?;
        self.observed.channel.send(&body, timeout)?;
        let result = self.observed.channel.receive(timeout)?;
        self.observed.birth_identity()?;
        Ok(result)
    }
    pub(crate) fn finish(mut self, timeout: Duration) -> Result<()> {
        self.verify_finished(timeout)
    }
    #[cfg(test)]
    pub(crate) fn fixture_private_station(&mut self, timeout: Duration) -> Result<()> {
        self.observed.birth_identity()?;
        crate::account_control::private_station_fixture::receive(
            &mut self.observed.channel,
            &self.observed.peer,
            &self.request,
            timeout,
        )
    }
    #[cfg(test)]
    pub(crate) fn fixture_station_lowbox(
        &mut self,
        timeout: Duration,
        pull: impl FnMut((u64, u32, u64)) -> Result<OwnedHandle>,
    ) -> Result<Option<OwnedHandle>> {
        if !matches!(
            self.request.birth.purpose,
            super::entry::credential_wire::child::Purpose::FixtureStationLowboxV1 { .. }
        ) {
            return Ok(None);
        }
        self.observed.birth_identity()?;
        crate::account_control::station_lowbox_fixture::receive(
            &mut self.observed.channel,
            &self.observed.peer,
            &self.request,
            timeout,
            pull,
        )
    }
    fn verify_finished(&mut self, timeout: Duration) -> Result<()> {
        let response = self.observed.channel.receive(timeout);
        if response.is_err()
            && self.request.birth.purpose
                == super::entry::credential_wire::child::Purpose::TrustedHandoffSelfCheckV1
            && unsafe { WaitForSingleObject(self.observed.peer.process.raw(), 0) } == WAIT_OBJECT_0
        {
            let mut code = 0;
            unsafe {
                GetExitCodeProcess(self.observed.peer.process.raw(), &mut code)?;
            }
            ensure!(
                code != crate::account_control::handoff::TOKEN_QUERY_DENIED,
                "original account helper refused: cross-account bootstrap TOKEN_QUERY access denied"
            );
        }
        ensure!(
            response? == self.request.frame("exited")?,
            "account self-check exit acknowledgment differs"
        );
        self.observed
            .channel
            .peer
            .verify(self.observed.channel.handle.raw(), true)?;
        ensure!(
            unsafe { WaitForSingleObject(self.observed.peer.process.raw(), 0) } == WAIT_OBJECT_0,
            "original account self-check has not exited"
        );
        let mut code = 0;
        unsafe {
            GetExitCodeProcess(self.observed.peer.process.raw(), &mut code)?;
        }
        ensure!(
            code == probe::EXIT_CODE
                && creation_time(self.observed.peer.process.raw())? == self.request.created
                && self.observed.peer.pid == self.request.pid
                && self
                    .observed
                    .peer
                    .image
                    .context("account image missing")?
                    .file_identity()?
                    == self.request.birth.selection.image_id,
            "original account self-check exit identity differs"
        );
        Ok(())
    }
}

impl<'a> RunningSelfCheck<'a> {
    pub(crate) fn finish_paired_profile(
        mut self,
        timeout: Duration,
    ) -> Result<FinishedSelfCheck<'a>> {
        ensure!(
            matches!(
                self.request.birth.purpose,
                super::entry::credential_wire::child::Purpose::TrustedPairedProfileV1 { .. }
            ),
            "paired profile finish requires original owner purpose"
        );
        self.verify_finished(timeout)?;
        Ok(FinishedSelfCheck { _running: self })
    }
    pub(crate) fn finish_paired(mut self, timeout: Duration) -> Result<FinishedSelfCheck<'a>> {
        ensure!(
            self.request.birth.purpose.is_paired_consumer(),
            "paired finish requires fixed consumer self-check"
        );
        self.verify_finished(timeout)?;
        Ok(FinishedSelfCheck { _running: self })
    }
}

/// Original query/synchronize reference only, able to observe exit after the
/// owning job scope has closed. No raw handle, resume or cleanup authority.
pub(crate) struct AccountExitObserver(OwnedHandle);
impl AccountExitObserver {
    pub(crate) fn require_paired_profile_exit(&self, request: &probe::Request) -> Result<()> {
        request.validate()?;
        ensure!(
            matches!(
                request.birth.purpose,
                super::entry::credential_wire::child::Purpose::TrustedPairedProfileV1 { .. }
            ),
            "paired profile exit requires owner purpose"
        );
        self.require_fixed_exit_identity(request)
    }
    pub(crate) fn retain_exited_paired_profile(&self, request: &probe::Request) -> Result<Self> {
        self.require_paired_profile_exit(request)?;
        let copy = self.retain_query_reference()?;
        copy.require_paired_profile_exit(request)?;
        Ok(copy)
    }
    pub(crate) fn require_self_check_exit(&self, request: &probe::Request) -> Result<()> {
        request.validate()?;
        ensure!(
            request.birth.purpose.is_paired_consumer(),
            "paired exit observation requires fixed self-check"
        );
        self.require_fixed_exit_identity(request)
    }
    fn require_fixed_exit_identity(&self, request: &probe::Request) -> Result<()> {
        ensure!(
            unsafe { GetProcessId(self.0.raw()) } == request.pid
                && creation_time(self.0.raw())? == request.created
                && unsafe { WaitForSingleObject(self.0.raw(), 0) } == WAIT_OBJECT_0,
            "original paired self-check exit identity differs"
        );
        let mut code = 0;
        unsafe {
            GetExitCodeProcess(self.0.raw(), &mut code)?;
        }
        ensure!(
            code == probe::EXIT_CODE,
            "original paired self-check failed"
        );
        Ok(())
    }
    pub(crate) fn retain_exited_self_check(&self, request: &probe::Request) -> Result<Self> {
        self.require_self_check_exit(request)?;
        let copy = self.retain_query_reference()?;
        copy.require_self_check_exit(request)?;
        Ok(copy)
    }
    fn retain_query_reference(&self) -> Result<Self> {
        let mut raw = HANDLE::default();
        unsafe {
            DuplicateHandle(
                GetCurrentProcess(),
                self.0.raw(),
                GetCurrentProcess(),
                &mut raw,
                (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
                false,
                DUPLICATE_HANDLE_OPTIONS(0),
            )?;
        }
        let copy = Self(OwnedHandle(raw));
        Ok(copy)
    }
    /// Fixed-child diagnostic only. Waiting and querying this original native
    /// reference cannot authorize resource cleanup or a successful receipt.
    #[cfg(test)]
    pub(crate) fn fixture_wait_code(self) -> Result<u32> {
        ensure!(
            unsafe { WaitForSingleObject(self.0.raw(), 5000) } == WAIT_OBJECT_0,
            "original diagnostic child has not exited after job closure"
        );
        let mut code = 0;
        unsafe {
            GetExitCodeProcess(self.0.raw(), &mut code)?;
        }
        Ok(code)
    }
    #[cfg(test)]
    pub(crate) fn fixture_source_identity(&self) -> Result<(usize, u32, u64)> {
        ensure!(
            unsafe { WaitForSingleObject(self.0.raw(), 0) } == WAIT_TIMEOUT,
            "original account observer is not live"
        );
        Ok((
            self.0.raw().0 as usize,
            unsafe { GetProcessId(self.0.raw()) },
            creation_time(self.0.raw())?,
        ))
    }
    #[cfg(test)]
    pub(crate) fn fixture_exited_self_check_identity(&self) -> Result<(usize, u32, u64)> {
        ensure!(
            unsafe { WaitForSingleObject(self.0.raw(), 5000) } == WAIT_OBJECT_0,
            "original self-check is not exited"
        );
        let mut code = 0;
        unsafe {
            GetExitCodeProcess(self.0.raw(), &mut code)?;
        }
        ensure!(code == probe::EXIT_CODE, "original self-check exit differs");
        Ok((
            self.0.raw().0 as usize,
            unsafe { GetProcessId(self.0.raw()) },
            creation_time(self.0.raw())?,
        ))
    }
    pub(crate) fn wait(self, timeout: Duration) -> Result<()> {
        ensure!(
            timeout >= Duration::from_millis(1) && timeout <= Duration::from_secs(10),
            "account exit timeout outside bounds"
        );
        ensure!(
            unsafe { WaitForSingleObject(self.0.raw(), timeout.as_millis() as u32) }
                == WAIT_OBJECT_0,
            "account child did not exit after original job closure"
        );
        Ok(())
    }
}

/// Sender-side lifetime pin. No pushed duplicate, account password, job handle
/// or ownership receipt is sent. A future commit acknowledgment must not release
/// this guard until its exact operation has been durably recorded.
pub(crate) struct PendingProcessOffer<'b, 'a> {
    _child: &'b SpawnedChild,
    _channel: ControlChannel<'a>,
    birth: Option<BirthRequest>,
}

impl<'b, 'a> PendingProcessOffer<'b, 'a> {
    pub(crate) fn send(
        child: &'b SpawnedChild,
        channel: ControlChannel<'a>,
        timeout: Duration,
    ) -> Result<Self> {
        Self::send_inner(child, channel, None, timeout)
    }
    pub(crate) fn send_bound(
        child: &'b SpawnedChild,
        channel: ControlChannel<'a>,
        request: &BirthRequest,
        timeout: Duration,
    ) -> Result<Self> {
        request.validate()?;
        Self::send_inner(child, channel, Some(request), timeout)
    }
    fn send_inner(
        child: &'b SpawnedChild,
        channel: ControlChannel<'a>,
        birth: Option<&BirthRequest>,
        timeout: Duration,
    ) -> Result<Self> {
        let mut channel = channel.into_role(false)?;
        let request: Request = serde_json::from_slice(&channel.receive(timeout)?)?;
        ensure!(
            request.protocol == PROTOCOL
                && request.nonce != [0; 16]
                && request.birth.as_ref() == birth,
            "invalid process observation request"
        );
        let peer = ExpectedPeer::retain(child.process())?;
        let offer = Offer {
            request,
            source_handle: child.process().0 as usize as u64,
            created: creation_time(child.process())?,
        };
        peer.alive()?;
        channel.send(&serde_json::to_vec(&offer)?, timeout)?;
        Ok(Self {
            _child: child,
            _channel: channel,
            birth: offer.request.birth,
        })
    }
    pub(crate) fn wait_for_birth_commit(
        mut self,
        request: &BirthRequest,
        image: &crate::account_state::image::PinnedRunnerImage,
        handoff: Option<crate::account_control::handoff::Source>,
        timeout: Duration,
    ) -> Result<()> {
        ensure!(
            self.birth.as_ref() == Some(request),
            "account birth commit lacks original request"
        );
        let peer = ExpectedPeer::retain(self._child.process())?;
        let frame = request.committed(peer.pid, creation_time(self._child.process())?)?;
        ensure!(
            self._channel.receive(timeout)? == frame,
            "account birth commit differs"
        );
        peer.alive()?;
        self._channel.send(&frame, timeout)?;
        let frame = FrameStorage(self._channel.receive(timeout)?);
        ensure!(
            request.purpose.is_self_check(),
            "account child resume remains unavailable"
        );
        let resume = probe::Request::decode(&frame.0, request, self._child)?;
        let original = ExpectedPeer::retain(self._child.process())?.with_image(image)?;
        original.token.require_dedicated_runner(
            &request.selection.account.account_sid,
            request.selection.session,
            &request.selection.account.group_sid,
        )?;
        original.alive()?;
        // self is consumed. No second frame or uncertain native call can retry.
        let previous = unsafe { ResumeThread(self._child.thread()) };
        ensure!(
            previous == 1,
            "account initial suspension differs or resume failed"
        );
        self._channel.send(&resume.frame("resumed")?, timeout)?;
        if let Some(handoff) = handoff {
            handoff.complete(self._child, &resume, image, &mut self._channel, timeout)?;
        }
        ensure!(
            unsafe { WaitForSingleObject(self._child.process(), 5000) } == WAIT_OBJECT_0,
            "fixed account self-check did not exit"
        );
        let mut code = 0;
        unsafe {
            GetExitCodeProcess(self._child.process(), &mut code)?;
        }
        ensure!(
            code == probe::EXIT_CODE,
            "fixed account self-check failed: exit {code}"
        );
        self._channel.send(&resume.frame("exited")?, timeout)?;
        // Keep the original source/child pins until the broker retires the job.
        let _refused = FrameStorage(self._channel.receive(timeout)?);
        anyhow::bail!("account self-check already completed; no further operation")
    }
}

pub(in crate::account_control) fn creation_time(process: HANDLE) -> Result<u64> {
    let mut created = FILETIME::default();
    let mut exited = FILETIME::default();
    let mut kernel = FILETIME::default();
    let mut user = FILETIME::default();
    unsafe {
        GetProcessTimes(process, &mut created, &mut exited, &mut kernel, &mut user)?;
    }
    let value = (u64::from(created.dwHighDateTime) << 32) | u64::from(created.dwLowDateTime);
    ensure!(value != 0, "missing native process creation identity");
    Ok(value)
}

#[cfg(test)]
#[path = "account_control_pull_tests.rs"]
mod tests;
