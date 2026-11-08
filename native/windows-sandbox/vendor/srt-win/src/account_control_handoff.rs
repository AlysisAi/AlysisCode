//! Single-use, read-only bootstrap reference handoff for the fixed account probe.
//! This is transport identity only, never command, resource or cleanup authority.
//! The source retains the original armed child; the broker owns the only job.
use super::*;
use crate::{
    account_state::image::{PinnedRunnerImage, account::AccountImageObserver},
    launch::SpawnedChild,
};
use bootstrap::entry::credential_wire::child::{BirthRequest, Purpose, probe::Request};
use serde::{Deserialize, Serialize};
use crate::account_state::execution::bootstrap_plan::lifecycle::resume::logon::delivery::birth::account_resume::account_profile;

const PROTOCOL: &str = "alysis-account-parent-observation-v1";
const FIRST_FRAME_LIMIT: usize = 8192;
pub(crate) const TOKEN_QUERY_DENIED: u32 = 0x4151;

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Offer {
    protocol: String,
    request: Request,
    destination_handle: u64,
}

fn address(birth: &BirthRequest) -> Result<ControlAddress> {
    birth.validate()?;
    ensure!(
        birth.purpose.uses_handoff(),
        "account handoff requires its fixed purpose"
    );
    // The original broker commits this random birth nonce before source effects.
    // It is distinct from the other operation nonces and not a caller pipe path.
    let mut nonce = [0; 16];
    for (i, part) in birth.nonce.as_bytes().chunks_exact(2).enumerate() {
        nonce[i] = u8::from_str_radix(std::str::from_utf8(part)?, 16)?;
    }
    let result = ControlAddress { nonce };
    result.binding_nonce()?;
    Ok(result)
}

pub(crate) struct Source {
    server: ControlServer,
    birth: BirthRequest,
}
impl Source {
    pub(crate) fn prepare(birth: &BirthRequest, image: &PinnedRunnerImage) -> Result<Option<Self>> {
        birth.validate()?;
        if !birth.purpose.uses_handoff() {
            return Ok(None);
        }
        birth
            .selection
            .require_recipient(&birth.selection.ready, image)?;
        let server = ControlServer::create(&address(birth)?, &birth.selection.account.account_sid)?;
        Ok(Some(Self {
            server,
            birth: birth.clone(),
        }))
    }
    /// Consumed once, only after the separate original-child durable resume.
    pub(crate) fn complete(
        self,
        child: &SpawnedChild,
        request: &Request,
        image: &PinnedRunnerImage,
        broker: &mut ControlChannel<'_>,
        timeout: Duration,
    ) -> Result<()> {
        request.validate()?;
        ensure!(
            request.birth == self.birth
                && request.pid == unsafe { GetProcessId(child.process()) }
                && request.created == bootstrap::pull::creation_time(child.process())?,
            "account handoff is not the original resumed child"
        );
        request
            .birth
            .selection
            .require_recipient(&request.birth.selection.ready, image)?;
        let peer = ExpectedPeer::retain(child.process())?.with_image(image)?;
        peer.token.require_dedicated_runner(
            &self.birth.selection.account.account_sid,
            self.birth.selection.session,
            &self.birth.selection.account.group_sid,
        )?;
        let mut channel = self.server.accept(peer, timeout)?;
        channel.peer.verify(channel.handle.raw(), true)?;
        let mut remote = HANDLE::default();
        unsafe {
            DuplicateHandle(
                GetCurrentProcess(),
                GetCurrentProcess(),
                child.process(),
                &mut remote,
                (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
                false,
                DUPLICATE_HANDLE_OPTIONS(0),
            )?;
        }
        // This number belongs to the child's table. Never CloseHandle it here,
        // or remotely close a stale value after an uncertain exchange. Exactly
        // one query-only reference is disposed by termination of the exact child.
        let offer = Offer {
            protocol: PROTOCOL.into(),
            request: request.clone(),
            destination_handle: remote.0 as usize as u64,
        };
        let body = serde_json::to_vec(&offer)?;
        ensure!(
            body.len() <= FIRST_FRAME_LIMIT,
            "account handoff exceeds bound"
        );
        channel.send(&body, timeout)?;
        if matches!(
            request.birth.purpose,
            Purpose::TrustedUnusedProfileV1 { .. }
        ) {
            account_profile::relay(broker, &mut channel, request, timeout)?;
        }
        if matches!(
            request.birth.purpose,
            Purpose::TrustedPairedProfileV1 { .. }
        ) {
            let result =
                crate::account_state::execution::paired_plan::native::account::profile::relay(
                    broker,
                    &mut channel,
                    request,
                    timeout,
                );
            #[cfg(test)]
            if result.is_err() {
                // The fixed account client closes its pipe while unwinding to
                // its numeric refusal exit. Keep the original child/source
                // guards alive until that exit is observed; an early source
                // unwind would kill the child and erase the refusal diagnostic.
                // This bounded observation grants no retry or cleanup receipt.
                ensure!(
                    unsafe {
                        WaitForSingleObject(child.process(), timeout.as_millis().min(5000) as u32)
                    } == WAIT_OBJECT_0,
                    "original fixed profile refusal did not exit within bound"
                );
            }
            result?;
        }
        #[cfg(test)]
        let _family = if matches!(
            request.birth.purpose,
            Purpose::FixtureProfileWriterV1 { .. }
        ) {
            Some(super::writer_fixture::relay(
                broker,
                &mut channel,
                child,
                request,
                image,
                timeout,
            )?)
        } else {
            None
        };
        #[cfg(test)]
        if matches!(request.birth.purpose, Purpose::FixturePrivateStationV1) {
            super::private_station_fixture::relay(broker, &mut channel, request, timeout)?;
        }
        #[cfg(test)]
        let _station_child = if matches!(
            request.birth.purpose,
            Purpose::FixtureStationLowboxV1 { .. }
        ) {
            super::station_lowbox_fixture::relay(
                broker,
                &mut channel,
                child,
                request,
                image,
                timeout,
            )?
        } else {
            None
        };
        ReadyAcknowledged::receive(channel, child, request, timeout)?.finish(timeout)
    }
}

/// Only the exact, authenticated ready acknowledgment constructs this terminal
/// state. Finishing grants no command/resource authority and consumes the pipe.
struct ReadyAcknowledged<'a, 'b> {
    channel: ControlChannel<'a>,
    child: &'b SpawnedChild,
    request: &'b Request,
}
impl<'a, 'b> ReadyAcknowledged<'a, 'b> {
    fn receive(
        mut channel: ControlChannel<'a>,
        child: &'b SpawnedChild,
        request: &'b Request,
        timeout: Duration,
    ) -> Result<Self> {
        request.validate()?;
        ensure!(
            request.birth.purpose.uses_handoff() && channel.server,
            "terminal handoff requires fixed source role"
        );
        ensure!(
            channel.receive(timeout)? == acknowledgment(request, b"ready")?,
            "account handoff acknowledgment differs"
        );
        Ok(Self {
            channel,
            child,
            request,
        })
    }
    fn finish(mut self, timeout: Duration) -> Result<()> {
        let request = self.request;
        let process = self.child.process();
        ensure!(
            unsafe { CompareObjectHandles(process, self.channel.peer.process.raw()) }.as_bool()
                && unsafe { GetProcessId(process) } == request.pid
                && bootstrap::pull::creation_time(process)? == request.created,
            "terminal handoff original child differs"
        );
        let end = deadline(timeout)?;
        // The peer is deliberately going to exit after receiving this frame.
        // Preserve all prewrite checks, then require that exact terminal event
        // instead of racing an ordinary postwrite live-peer observation. Keep
        // this pipe open so the child's normal receive can verify its source.
        self.channel
            .write_in_flight(&acknowledgment(request, b"complete")?, timeout)?;
        let remaining = end.saturating_duration_since(Instant::now()).as_millis() as u32;
        ensure!(
            unsafe { WaitForSingleObject(process, remaining) } == WAIT_OBJECT_0,
            "terminal handoff child did not exit within bound"
        );
        let mut code = 0;
        unsafe {
            GetExitCodeProcess(process, &mut code)?;
        }
        ensure!(
            code == bootstrap::entry::credential_wire::child::probe::EXIT_CODE
                && unsafe { GetProcessId(process) } == request.pid
                && bootstrap::pull::creation_time(process)? == request.created
                && self
                    .channel
                    .peer
                    .image
                    .context("terminal handoff image missing")?
                    .file_identity()?
                    == request.birth.selection.image_id,
            "terminal handoff original exit differs"
        );
        Ok(())
    }
}

fn acknowledgment(request: &Request, phase: &[u8]) -> Result<Vec<u8>> {
    ensure!(
        phase == b"ready" || phase == b"complete",
        "invalid handoff phase"
    );
    let mut value = b"alysis-account-parent-observation-v1:".to_vec();
    value.extend_from_slice(phase);
    value.extend_from_slice(&request.frame("resume")?);
    Ok(value)
}

/// Private, receive-only first connection. It cannot send, retry or reconnect.
/// The sole production caller must validate the offer and native peer before
/// promoting the same pipe instance, with its consumed frame counters retained.
struct PendingClient {
    handle: OwnedHandle,
    nonce: [u8; 16],
    received: usize,
}
impl PendingClient {
    fn connect(address: &ControlAddress) -> Result<Self> {
        crate::account_impersonation::require_process_identity()?;
        let raw = unsafe {
            CreateFileW(
                pcwstr(&wstr(&address.path()?)),
                CLIENT_RIGHTS,
                FILE_SHARE_MODE(0),
                None,
                OPEN_EXISTING,
                FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION,
                None,
            )
        }?;
        let handle = OwnedHandle(raw);
        noninheritable(handle.raw())?;
        Ok(Self {
            handle,
            nonce: address.nonce,
            received: 0,
        })
    }
    fn first(mut self, timeout: Duration) -> Result<(Self, FrameStorage)> {
        let deadline = deadline(timeout)?;
        let mut header = [0; HEADER];
        io::transfer(self.handle.raw(), &mut header, false, deadline)?;
        let len = u32::from_le_bytes(header[29..33].try_into().unwrap()) as usize;
        ensure!(
            &header[..4] == b"AC01"
                && header[4] == 1
                && header[5..21] == self.nonce
                && u64::from_le_bytes(header[21..29].try_into().unwrap()) == 0,
            "account handoff first frame identity differs"
        );
        ensure!(
            (1..=FIRST_FRAME_LIMIT).contains(&len),
            "account handoff first frame exceeds bound"
        );
        let mut body = FrameStorage(vec![0; len]);
        io::transfer(self.handle.raw(), &mut body.0, false, deadline)?;
        noninheritable(self.handle.raw())?;
        self.received = len;
        Ok((self, body))
    }
    fn promote<'a>(self, peer: ExpectedPeer<'a>) -> Result<ControlChannel<'a>> {
        ensure!(self.received > 0, "account handoff first frame missing");
        peer.verify(self.handle.raw(), false)?;
        let mut channel = ControlChannel::new(self.handle, self.nonce, false, peer);
        channel.receive_sequence = 1;
        channel.received = self.received;
        Ok(channel)
    }
}

fn validate_offer(offer: &Offer, birth: &BirthRequest, pid: u32, created: u64) -> Result<()> {
    offer.request.validate()?;
    ensure!(
        offer.protocol == PROTOCOL
            && &offer.request.birth == birth
            && offer.request.pid == pid
            && offer.request.created == created,
        "account handoff offer operation differs"
    );
    ensure!(
        offer.destination_handle > 0 && offer.destination_handle < isize::MAX as u64,
        "account handoff original process reference invalid"
    );
    Ok(())
}

/// Fixed entry. No effects or secret send precedes peer authentication.
pub(crate) fn client(birth: &BirthRequest) -> Result<()> {
    let mut phase = 1u32;
    #[cfg(test)]
    let mut family = None;
    let result = (|| -> Result<()> {
        let address = address(birth)?;
        let selected = &birth.selection;
        phase = 2;
        let image =
            AccountImageObserver::prepare(&selected.account, selected.session, selected.image_id)?;
        phase = 3;
        let pending = PendingClient::connect(&address)?;
        phase = 4;
        let (pending, body) = pending.first(Duration::from_secs(5))?;
        phase = 5;
        let offer: Offer = serde_json::from_slice(&body.0)
            .map_err(|_| anyhow::anyhow!("invalid account handoff offer"))?;
        validate_offer(
            &offer,
            birth,
            std::process::id(),
            bootstrap::pull::creation_time(unsafe { GetCurrentProcess() })?,
        )?;
        drop(body);
        phase = 6;
        let raw = HANDLE(offer.destination_handle as usize as *mut _);
        noninheritable(raw)?;
        // Duplicate and validate before owning anything derived from the wire. The
        // offered original itself remains borrowed until this fixed child exits.
        let mut local = HANDLE::default();
        unsafe {
            DuplicateHandle(
                GetCurrentProcess(),
                raw,
                GetCurrentProcess(),
                &mut local,
                (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
                false,
                DUPLICATE_HANDLE_OPTIONS(0),
            )?;
        }
        let original = OwnedHandle(local);
        phase = 7;
        noninheritable(original.raw())?;
        ensure!(
            unsafe { GetProcessId(original.raw()) } == selected.bootstrap_pid
                && bootstrap::pull::creation_time(original.raw())? == selected.bootstrap_created,
            "account handoff parent native identity differs"
        );
        phase = 8;
        image.verify_process(original.raw())?;
        phase = 9;
        let mut token = HANDLE::default();
        let result = unsafe {
            OpenProcessToken(
                original.raw(),
                windows::Win32::Security::TOKEN_QUERY,
                &mut token,
            )
        };
        if let Err(error) = &result {
            if error.code() == windows::core::HRESULT::from_win32(ERROR_ACCESS_DENIED.0) {
                // A fixed refusal diagnostic, never success or a fallback. The
                // broker independently observes this original child's exit code.
                std::process::exit(TOKEN_QUERY_DENIED as i32);
            }
        }
        result.context("account helper bootstrap token-query feasibility")?;
        drop(OwnedHandle(token));
        phase = 10;
        let mut peer = ExpectedPeer::retain(original.raw())?;
        peer.token
            .require_account_runner(&selected.account.owner_sid, selected.session)?;
        peer.account_image = Some(&image);
        peer.alive()?;
        phase = 11;
        let mut channel = pending.promote(peer)?;
        if matches!(birth.purpose, Purpose::TrustedUnusedProfileV1 { .. }) {
            phase = 15;
            account_profile::client(&mut channel, &offer.request, &mut phase)?;
        }
        if matches!(birth.purpose, Purpose::TrustedPairedProfileV1 { .. }) {
            phase = 24;
            crate::account_state::execution::paired_plan::native::account::profile::client(
                &mut channel,
                &offer.request,
                &mut phase,
            )?;
        }
        #[cfg(test)]
        if matches!(birth.purpose, Purpose::FixtureProfileWriterV1 { .. }) {
            family = Some(super::writer_fixture::client(
                &mut channel,
                &offer.request,
                &image,
                &mut phase,
            )?);
        }
        #[cfg(test)]
        if matches!(birth.purpose, Purpose::FixturePrivateStationV1) {
            phase = 34;
            super::private_station_fixture::client(&mut channel, &offer.request)?;
        }
        #[cfg(test)]
        if matches!(birth.purpose, Purpose::FixtureStationLowboxV1 { .. }) {
            phase = 46;
            super::station_lowbox_fixture::client(&mut channel, &offer.request, &image)?;
        }
        phase = 12;
        channel.send(
            &acknowledgment(&offer.request, b"ready")?,
            Duration::from_secs(5),
        )?;
        phase = 13;
        ensure!(
            channel.receive(Duration::from_secs(5))?
                == acknowledgment(&offer.request, b"complete")?,
            "account handoff completion differs"
        );
        phase = 14;
        #[cfg(test)]
        if let Some(family) = &family {
            family.describe()?;
        }
        image.verify_current()
    })();
    #[cfg(test)]
    if result.is_ok() && family.is_some() {
        // ChildGuard destructors must not stop descendants before the broker
        // proves their original live references and retires the whole job.
        std::process::exit(bootstrap::entry::credential_wire::child::probe::EXIT_CODE as i32);
    }
    // Disposable test-child diagnostics are numeric only. They never alter
    // authentication, grant authority or satisfy the fixed success exit code.
    // Its broker observes this original child after whole-job closure, even
    // when the source pipe has already closed before reporting the error.
    #[cfg(test)]
    if let Err(error) = &result {
        let native = error
            .chain()
            .find_map(|cause| cause.downcast_ref::<windows::core::Error>())
            .map_or(0, |error| error.code().0 as u32 & 0xffff);
        std::process::exit((0x42000000 | (phase << 16) | native) as i32);
    }
    result.with_context(|| format!("account handoff read-only stage {phase}"))
}

#[cfg(test)]
pub(super) mod tests {
    use super::*;
    const TIMEOUT: Duration = Duration::from_secs(1);
    fn me() -> ExpectedPeer<'static> {
        ExpectedPeer::retain(unsafe { GetCurrentProcess() }).unwrap()
    }
    fn request() -> Request {
        use bootstrap::entry::credential_wire::Selection;
        Request {
            birth: BirthRequest {
                protocol: "alysis-suspended-account-birth-v2".into(),
                nonce: "6123456789abcdef0123456789abcdef".into(),
                purpose: Purpose::TrustedHandoffSelfCheckV1,
                selection: Selection {
                    protocol: Selection::protocol(),
                    operation: "0123456789abcdef0123456789abcdef".into(),
                    logon: "1123456789abcdef0123456789abcdef".into(),
                    ready: "2123456789abcdef0123456789abcdef".into(),
                    bootstrap_pid: 7,
                    bootstrap_created: 9,
                    image_id: [3; 24],
                    session: 1,
                    account: crate::account_install::synthetic_ready()
                        .binding(crate::account_install::AccountMode::Offline)
                        .unwrap(),
                    credential_generation: "CredOwner-3123456789abcdef0123456789abcdef".into(),
                },
            },
            nonce: "7123456789abcdef0123456789abcdef".into(),
            pid: 8,
            created: 10,
        }
    }
    fn header(address: &ControlAddress, length: usize) -> Vec<u8> {
        let mut header = b"AC01".to_vec();
        header.push(1);
        header.extend_from_slice(&address.nonce);
        header.extend_from_slice(&0u64.to_le_bytes());
        header.extend_from_slice(&(length as u32).to_le_bytes());
        header
    }
    // Reuses the existing pinned, atomically job-contained same-user pipe
    // fixture. This qualifies only terminal transport, not account admission.
    pub(in crate::account_control) fn terminal_client(mut channel: ControlChannel<'_>, mode: &str) {
        let request: Request = serde_json::from_slice(&channel.receive(TIMEOUT).unwrap()).unwrap();
        request.validate().unwrap();
        assert_eq!(request.pid, std::process::id());
        assert_eq!(
            request.created,
            bootstrap::pull::creation_time(unsafe { GetCurrentProcess() }).unwrap()
        );
        let mut ready = acknowledgment(&request, b"ready").unwrap();
        if mode == "wrong-ready" {
            ready[0] ^= 1;
        }
        channel.send(&ready, TIMEOUT).unwrap();
        assert_eq!(
            channel.receive(Duration::from_secs(3)).unwrap(),
            acknowledgment(&request, b"complete").unwrap()
        );
        if mode == "wait" {
            unsafe {
                Sleep(5000);
            }
            panic!("original child was not terminated after terminal timeout");
        }
        std::process::exit(if mode == "wrong-exit" {
            0x4152
        } else {
            bootstrap::entry::credential_wire::child::probe::EXIT_CODE as i32
        });
    }
    pub(in crate::account_control) fn terminal_source(
        mut channel: ControlChannel<'_>,
        child: &SpawnedChild,
        image: &PinnedRunnerImage,
        mode: &str,
    ) {
        let mut request = request();
        request.birth.selection.bootstrap_pid = std::process::id();
        request.birth.selection.bootstrap_created =
            bootstrap::pull::creation_time(unsafe { GetCurrentProcess() }).unwrap();
        request.birth.selection.image_id = image.file_identity().unwrap();
        request.pid = unsafe { GetProcessId(child.process()) };
        request.created = bootstrap::pull::creation_time(child.process()).unwrap();
        request.validate().unwrap();
        channel
            .send(&serde_json::to_vec(&request).unwrap(), TIMEOUT)
            .unwrap();
        let ready = ReadyAcknowledged::receive(channel, child, &request, TIMEOUT);
        if mode == "wrong-ready" {
            assert!(
                ready
                    .err()
                    .unwrap()
                    .to_string()
                    .contains("acknowledgment differs")
            );
            return;
        }
        let ready = ready.unwrap();
        let _token = if mode == "token" {
            Some(super::super::token_tests::mutate_disposable_child(child))
        } else {
            None
        };
        let result = ready.finish(if mode == "wait" {
            Duration::from_millis(100)
        } else {
            TIMEOUT
        });
        match mode {
            "complete" => result.unwrap(),
            "wrong-exit" => assert!(
                result
                    .err()
                    .unwrap()
                    .to_string()
                    .contains("original exit differs")
            ),
            "wait" => assert!(
                result
                    .err()
                    .unwrap()
                    .to_string()
                    .contains("did not exit within bound")
            ),
            "token" => assert!(result.err().unwrap().to_string().contains("token changed")),
            _ => panic!("unknown terminal fixture mode"),
        }
    }
    #[test]
    fn account_handoff_offer_refuses_changed_birth_and_native_destination_identity() {
        let request = request();
        request.validate().unwrap();
        address(&request.birth).unwrap();
        let offer = || Offer {
            protocol: PROTOCOL.into(),
            request: request.clone(),
            destination_handle: 4,
        };
        validate_offer(&offer(), &request.birth, request.pid, request.created).unwrap();
        for case in 0..13 {
            let mut wrong = offer();
            match case {
                0 => wrong.protocol.push('x'),
                1 => wrong.request.birth.nonce.replace_range(..1, "a"),
                2 => wrong
                    .request
                    .birth
                    .selection
                    .operation
                    .replace_range(..1, "a"),
                3 => wrong.request.birth.selection.bootstrap_pid += 1,
                4 => wrong.request.birth.selection.bootstrap_created += 1,
                5 => wrong.request.birth.selection.image_id[0] ^= 1,
                6 => wrong.request.birth.selection.session += 1,
                7 => wrong.request.birth.purpose = Purpose::TrustedSelfCheckV1,
                8 => wrong.request.pid += 1,
                9 => wrong.request.created += 1,
                10 => wrong.destination_handle = 0,
                11 => wrong.destination_handle = isize::MAX as u64,
                _ => wrong.destination_handle = u64::MAX,
            }
            assert!(
                validate_offer(&wrong, &request.birth, request.pid, request.created).is_err(),
                "case {case}"
            );
        }
        let mut wrong = request.clone();
        wrong.nonce.replace_range(..1, "a");
        assert_ne!(
            acknowledgment(&wrong, b"ready").unwrap(),
            acknowledgment(&request, b"ready").unwrap()
        );
        assert_ne!(
            acknowledgment(&request, b"complete").unwrap(),
            acknowledgment(&request, b"ready").unwrap()
        );
        assert!(acknowledgment(&request, b"execute").is_err());
        let mut extended = serde_json::to_value(offer()).unwrap();
        extended["command"] = serde_json::json!("forbidden");
        assert!(serde_json::from_value::<Offer>(extended).is_err());
        let mut wrong = request.birth;
        wrong.purpose = Purpose::TrustedSelfCheckV1;
        assert!(address(&wrong).is_err());
    }
    #[test]
    fn account_handoff_first_frame_refuses_wrong_header_and_closes_connection() {
        for offset in [0, 4, 5, 21] {
            let (server, address) = ControlServer::prepare("S-1-5-21-1-2-3-9999").unwrap();
            let client = PendingClient::connect(&address).unwrap();
            let server = server.accept(me(), TIMEOUT).unwrap();
            let raw = client.handle.raw();
            let mut header = header(&address, 1);
            header[offset] ^= 1;
            io::transfer(
                server.handle.raw(),
                &mut header,
                true,
                deadline(TIMEOUT).unwrap(),
            )
            .unwrap();
            let error = client.first(TIMEOUT).err().unwrap();
            assert!(error.to_string().contains("identity differs"));
            let mut flags = 0;
            assert!(unsafe { GetHandleInformation(raw, &mut flags) }.is_err());
        }
    }
    #[test]
    fn account_handoff_partial_first_frame_cancels_io_and_closes_without_promotion() {
        let (server, address) = ControlServer::prepare("S-1-5-21-1-2-3-9999").unwrap();
        let client = PendingClient::connect(&address).unwrap();
        let server = server.accept(me(), TIMEOUT).unwrap();
        let raw = client.handle.raw();
        let mut partial = header(&address, 32);
        partial.extend_from_slice(b"synthetic");
        io::transfer(
            server.handle.raw(),
            &mut partial,
            true,
            deadline(TIMEOUT).unwrap(),
        )
        .unwrap();
        assert!(client.first(Duration::from_millis(10)).is_err());
        let mut flags = 0;
        assert!(unsafe { GetHandleInformation(raw, &mut flags) }.is_err());
        assert_eq!(FRAME_SCRUB_AUDIT.get(), Some((32, true, true)));
    }
    #[test]
    fn account_handoff_promotion_refuses_a_different_original_process() {
        let original = super::super::tests::suspended();
        let (server, address) = ControlServer::prepare("S-1-5-21-1-2-3-9999").unwrap();
        let client = PendingClient::connect(&address).unwrap();
        let mut server = server.accept(me(), TIMEOUT).unwrap();
        server.send(b"first", TIMEOUT).unwrap();
        let (client, _frame) = client.first(TIMEOUT).unwrap();
        let raw = client.handle.raw();
        let error = client
            .promote(ExpectedPeer::retain(original.process()).unwrap())
            .err()
            .unwrap();
        assert!(error.to_string().contains("different process"));
        let mut flags = 0;
        assert!(unsafe { GetHandleInformation(raw, &mut flags) }.is_err());
    }
    #[test]
    fn account_handoff_promotion_preserves_original_pipe_sequence_and_byte_counters() {
        let (server, address) = ControlServer::prepare("S-1-5-21-1-2-3-9999").unwrap();
        let client = PendingClient::connect(&address).unwrap();
        let mut server = server.accept(me(), TIMEOUT).unwrap();
        server.send(b"first", TIMEOUT).unwrap();
        let (client, frame) = client.first(TIMEOUT).unwrap();
        assert_eq!(frame.0, b"first");
        let mut client = client.promote(me()).unwrap();
        assert_eq!((client.receive_sequence, client.received), (1, 5));
        client.send(b"reply", TIMEOUT).unwrap();
        assert_eq!(server.receive(TIMEOUT).unwrap(), b"reply");
        server.send(b"second", TIMEOUT).unwrap();
        assert_eq!(client.receive(TIMEOUT).unwrap(), b"second");
        assert_eq!((client.receive_sequence, client.received), (2, 11));
    }
    #[test]
    fn account_handoff_first_frame_refuses_oversized_payload_before_reading_body() {
        let (server, address) = ControlServer::prepare("S-1-5-21-1-2-3-9999").unwrap();
        let client = PendingClient::connect(&address).unwrap();
        let server = server.accept(me(), TIMEOUT).unwrap();
        let mut header = b"AC01".to_vec();
        header.push(1);
        header.extend_from_slice(&address.nonce);
        header.extend_from_slice(&0u64.to_le_bytes());
        header.extend_from_slice(&((FIRST_FRAME_LIMIT + 1) as u32).to_le_bytes());
        io::transfer(
            server.handle.raw(),
            &mut header,
            true,
            deadline(TIMEOUT).unwrap(),
        )
        .unwrap();
        let error = client.first(TIMEOUT).err().unwrap();
        assert!(error.to_string().contains("exceeds bound"));
    }
    #[test]
    fn account_handoff_first_frame_cannot_be_promoted_without_consuming_it() {
        let (_server, address) = ControlServer::prepare("S-1-5-21-1-2-3-9999").unwrap();
        let client = PendingClient::connect(&address).unwrap();
        assert!(
            client
                .promote(me())
                .err()
                .unwrap()
                .to_string()
                .contains("first frame missing")
        );
    }
}
