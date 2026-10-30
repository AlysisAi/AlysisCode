//! Private transport prerequisite, not a resource receipt or launch authority.
//! Both ends retain a process reference supplied by the trusted launcher; no
//! PID/name from a message can construct peer authority. The launcher still
//! owes verified image preparation before birth and secure original-handle handoff.
//! Primary-token continuity is observed through retained native references;
//! it does not establish a trusted image, complete token policy or launch authority.
#![allow(dead_code)]

use crate::util::{OwnedHandle, OwnedSd, pcwstr, wstr};
use anyhow::{Context, Result, ensure};
use std::{
    mem::size_of,
    time::{Duration, Instant},
};
use windows::Win32::{
    Foundation::*,
    Security::{Cryptography::*, SECURITY_ATTRIBUTES},
    Storage::FileSystem::*,
    System::{Pipes::*, Threading::*},
};

#[path = "account_control_handoff.rs"]
pub(crate) mod handoff;

#[cfg(test)]
#[path = "account_paired_writer_fixture.rs"]
pub(crate) mod writer_fixture;

#[cfg(test)]
#[path = "account_private_station_fixture.rs"]
pub(crate) mod private_station_fixture;

#[cfg(test)]
#[path = "account_station_lowbox_fixture.rs"]
pub(crate) mod station_lowbox_fixture;

#[path = "account_control_io.rs"]
mod io;

#[path = "account_control_token.rs"]
mod primary_token;

#[path = "account_control_job.rs"]
pub(crate) mod containment;

#[path = "account_control_bootstrap.rs"]
pub(crate) mod bootstrap;

#[cfg(test)]
#[path = "account_control_token_tests.rs"]
mod token_tests;

// CreateFileW also needs read-attributes access for the client open. Grant only
// that metadata read plus synchronize/read/write data, never create-instance.
const CLIENT_RIGHTS: u32 = 0x0010_0083;
const HEADER: usize = 33;
const MAX_FRAME: usize = 64 * 1024;
const MAX_FRAMES: u64 = 128;
const MAX_BYTES: usize = 1024 * 1024;

/// Transport-owned copies are scrubbed after completed/cancelled I/O, including
/// error/unwind paths. Successful receive transfers ownership to the caller,
/// which must scrub secret payloads itself. Pending I/O must still be drained
/// before this storage can be wiped or freed; io::transfer enforces that order.
struct FrameStorage(Vec<u8>);
impl Drop for FrameStorage {
    fn drop(&mut self) {
        #[cfg(test)]
        let populated = self.0.iter().any(|byte| *byte != 0);
        for byte in &mut self.0 {
            unsafe {
                std::ptr::write_volatile(byte, 0);
            }
        }
        std::sync::atomic::compiler_fence(std::sync::atomic::Ordering::SeqCst);
        #[cfg(test)]
        FRAME_SCRUB_AUDIT.set(Some((
            self.0.len(),
            populated,
            self.0.iter().all(|byte| *byte == 0),
        )));
    }
}
#[cfg(test)]
thread_local! {
    static FRAME_SCRUB_AUDIT: std::cell::Cell<Option<(usize, bool, bool)>> = const { std::cell::Cell::new(None) };
}

/// Data for the future trusted bootstrap handoff, never cleanup authority.
/// No Debug representation or caller-supplied pipe path.
#[derive(serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ControlAddress {
    nonce: [u8; 16],
}

impl ControlAddress {
    pub(crate) fn binding_nonce(&self) -> Result<[u8; 16]> {
        self.path()?;
        Ok(self.nonce)
    }
    fn fresh() -> Result<Self> {
        let mut nonce = [0; 16];
        unsafe { BCryptGenRandom(None, &mut nonce, BCRYPT_USE_SYSTEM_PREFERRED_RNG).ok()? };
        ensure!(nonce != [0; 16], "invalid control identity");
        Ok(Self { nonce })
    }
    fn path(&self) -> Result<String> {
        ensure!(self.nonce != [0; 16], "invalid control identity");
        let suffix: String = self.nonce.iter().map(|b| format!("{b:02x}")).collect();
        Ok(format!(r"\\.\pipe\AlysisSandboxControl.{suffix}"))
    }
}

/// Duplicate of an original process handle, not a re-open by PID. This proves
/// endpoint identity, liveness and primary-token continuity. Candidate runner
/// admission also borrows a strict job for repeated containment observations.
/// Neither path establishes executable trust, atomic birth or launch authority.
pub(crate) struct ExpectedPeer<'a> {
    process: OwnedHandle,
    pid: u32,
    token: primary_token::PrimaryToken,
    job: Option<containment::JobBinding<'a>>,
    image: Option<&'a crate::account_state::image::PinnedRunnerImage>,
    account_image: Option<&'a crate::account_state::image::account::AccountImageObserver>,
}

impl<'a> ExpectedPeer<'a> {
    pub(crate) fn retain(original: HANDLE) -> Result<Self> {
        crate::account_impersonation::require_process_identity()?;
        let mut raw = HANDLE::default();
        unsafe {
            DuplicateHandle(
                GetCurrentProcess(),
                original,
                GetCurrentProcess(),
                &mut raw,
                (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
                false,
                DUPLICATE_HANDLE_OPTIONS(0),
            )?;
        }
        let process = OwnedHandle(raw);
        let pid = unsafe { GetProcessId(process.raw()) };
        ensure!(pid != 0, "control peer is not a process");
        let token = primary_token::PrimaryToken::retain(process.raw())?;
        let peer = Self {
            process,
            pid,
            token,
            job: None,
            image: None,
            account_image: None,
        };
        peer.alive()?;
        Ok(peer)
    }
    pub(crate) fn require_profile_runner(
        &self,
        account: &str,
        session: u32,
        group: &str,
    ) -> Result<()> {
        self.alive()?;
        self.token.require_dedicated_runner(account, session, group)
    }
    /// Candidate-only admission from installed account selection and the original
    /// child guard, borrowed strict job and installed image/path guard. Still no
    /// proof of pre-birth pinning, atomic birth or permission to launch/resume.
    /// Every legacy account entry point remains unconditionally closed.
    pub(crate) fn retain_account_runner(
        original: &crate::launch::SpawnedChild,
        ready: &crate::account_install::ReadyInstallation,
        mode: crate::account_install::AccountMode,
        job: &'a crate::job::Job,
        image: &'a crate::account_state::image::PinnedRunnerImage,
    ) -> Result<Self> {
        crate::account_impersonation::require_process_identity()?;
        let binding = ready.binding(mode)?;
        binding.validate()?;
        ensure!(
            binding.owner_sid == crate::sid::current_user_sid()?,
            "runner broker owner differs"
        );
        let mut session = 0;
        unsafe {
            windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
                std::process::id(),
                &mut session,
            )?
        };
        let peer = Self::retain(original.process())?;
        peer.token
            .require_dedicated_runner(&binding.account_sid, session, &binding.group_sid)?;
        image.require_installation(ready)?;
        peer.with_job(job)?.with_image(image)
    }
    fn with_job(mut self, job: &'a crate::job::Job) -> Result<Self> {
        self.job = Some(containment::JobBinding::observe(job, self.process.raw())?);
        self.alive()?;
        Ok(self)
    }
    /// Image/path observations only, not an account role or a birth receipt.
    pub(crate) fn with_image(
        mut self,
        image: &'a crate::account_state::image::PinnedRunnerImage,
    ) -> Result<Self> {
        self.image = Some(image);
        self.alive()?;
        Ok(self)
    }
    fn alive(&self) -> Result<()> {
        crate::account_impersonation::require_process_identity()?;
        noninheritable(self.process.raw())?;
        ensure!(
            unsafe { WaitForSingleObject(self.process.raw(), 0) } == WAIT_TIMEOUT,
            "original control peer is no longer verifiably alive"
        );
        self.token.verify(self.process.raw())?;
        if let Some(job) = &self.job {
            job.verify(self.process.raw())?;
        }
        if let Some(image) = self.image {
            image.verify_process(self.process.raw())?;
        }
        if let Some(image) = self.account_image {
            image.verify_process(self.process.raw())?;
        }
        ensure!(
            unsafe { WaitForSingleObject(self.process.raw(), 0) } == WAIT_TIMEOUT,
            "original control peer exited during security observation"
        );
        Ok(())
    }
    fn verify(&self, pipe: HANDLE, server: bool) -> Result<()> {
        self.alive()?;
        let mut actual = 0;
        unsafe {
            if server {
                GetNamedPipeClientProcessId(pipe, &mut actual)?;
            } else {
                GetNamedPipeServerProcessId(pipe, &mut actual)?;
            }
        }
        ensure!(
            actual == self.pid,
            "control endpoint belongs to a different process"
        );
        self.alive()
    }
}

pub(crate) struct ControlServer {
    handle: OwnedHandle,
    nonce: [u8; 16],
}

impl ControlServer {
    pub(crate) fn require_address(&self, address: &ControlAddress) -> Result<()> {
        ensure!(
            self.nonce == address.binding_nonce()?,
            "bootstrap control server differs from durable address"
        );
        noninheritable(self.handle.raw())
    }
    pub(crate) fn prepare(account_sid: &str) -> Result<(Self, ControlAddress)> {
        let address = ControlAddress::fresh()?;
        Ok((Self::create(&address, account_sid)?, address))
    }
    fn create(address: &ControlAddress, account_sid: &str) -> Result<Self> {
        crate::account_impersonation::require_process_identity()?;
        let account = crate::sid::LocalPsid::from_string(account_sid)?;
        let owner = crate::sid::current_user_sid()?;
        ensure!(
            account_sid != owner
                && account_sid.starts_with("S-1-5-21-")
                && account_sid.split('-').count() == 8
                && crate::sid::psid_to_string(account.as_psid())? == account_sid,
            "control client must be a canonical account SID distinct from the owner"
        );
        // No package/capability/Everyone ACE. The account may transfer data but
        // cannot create a server instance, change permissions or change owner.
        // A high-integrity bootstrap must not implicitly label this pipe High:
        // the selected raw account is Medium. Low-integrity children still fail
        // independent read/write-up checks, even with a matching account ACE.
        let descriptor = OwnedSd::from_sddl(&format!(
            "O:{owner}D:P(A;;GA;;;SY)(A;;GA;;;{owner})(A;;0x{CLIENT_RIGHTS:x};;;{account_sid})(A;;RC;;;OW)S:(ML;;NWNR;;;ME)"
        ))?;
        let security = SECURITY_ATTRIBUTES {
            nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: descriptor.ptr.0,
            bInheritHandle: false.into(),
        };
        let raw = unsafe {
            CreateNamedPipeW(
                pcwstr(&wstr(&address.path()?)),
                PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE | FILE_FLAG_OVERLAPPED,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                1,
                MAX_FRAME as u32 + HEADER as u32,
                MAX_FRAME as u32 + HEADER as u32,
                0,
                Some(&security),
            )
        };
        ensure!(
            !raw.is_invalid(),
            "exclusive control pipe creation failed: {}",
            std::io::Error::last_os_error()
        );
        let handle = OwnedHandle(raw);
        noninheritable(handle.raw())?;
        Ok(Self {
            handle,
            nonce: address.nonce,
        })
    }
    /// One accept attempt, including on error. No reconnection/name adoption.
    pub(crate) fn accept<'a>(
        self,
        peer: ExpectedPeer<'a>,
        timeout: Duration,
    ) -> Result<ControlChannel<'a>> {
        peer.alive()?;
        noninheritable(self.handle.raw())?;
        io::connect(self.handle.raw(), deadline(timeout)?)?;
        peer.verify(self.handle.raw(), true)?;
        Ok(ControlChannel::new(self.handle, self.nonce, true, peer))
    }
}

pub(crate) struct ControlChannel<'a> {
    handle: OwnedHandle,
    nonce: [u8; 16],
    server: bool,
    peer: ExpectedPeer<'a>,
    send_sequence: u64,
    receive_sequence: u64,
    sent: usize,
    received: usize,
    usable: bool,
}

impl<'a> ControlChannel<'a> {
    /// Tighten an already authenticated original peer to a retained image pin.
    /// This consumes the endpoint on every failure and grants no process rights.
    pub(crate) fn with_image(
        mut self,
        image: &'a crate::account_state::image::PinnedRunnerImage,
    ) -> Result<Self> {
        ensure!(self.usable, "control channel is permanently refused");
        self.peer = self.peer.with_image(image)?;
        self.peer.verify(self.handle.raw(), self.server)?;
        noninheritable(self.handle.raw())?;
        Ok(self)
    }
    /// Consume the endpoint when assigning a protocol role. This is a transport
    /// role check only; no launch or resource authority is created here.
    pub(crate) fn into_role(self, server: bool) -> Result<Self> {
        ensure!(
            self.server == server && self.usable,
            "incorrect/refused control endpoint role"
        );
        self.peer.verify(self.handle.raw(), self.server)?;
        noninheritable(self.handle.raw())?;
        Ok(self)
    }
    pub(crate) fn connect(address: &ControlAddress, peer: ExpectedPeer<'a>) -> Result<Self> {
        peer.alive()?;
        // First instance must already exist. No WaitNamedPipe/retry can adopt a
        // later server. Identification-only SQOS prevents client impersonation.
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
        peer.verify(handle.raw(), false)?;
        Ok(Self::new(handle, address.nonce, false, peer))
    }
    fn new(handle: OwnedHandle, nonce: [u8; 16], server: bool, peer: ExpectedPeer<'a>) -> Self {
        Self {
            handle,
            nonce,
            server,
            peer,
            send_sequence: 0,
            receive_sequence: 0,
            sent: 0,
            received: 0,
            usable: true,
        }
    }
    fn begin(&mut self) -> Result<()> {
        ensure!(self.usable, "control channel is permanently refused");
        // Even native verification failures poison the channel. Restoring a
        // handle flag or thread identity cannot make a failed attempt retryable.
        self.usable = false;
        noninheritable(self.handle.raw())?;
        self.peer.verify(self.handle.raw(), self.server)
    }
    /// Messages are untrusted data, not lifecycle receipts. Consumers must
    /// validate operation identities and durable state before acknowledging.
    pub(crate) fn send(&mut self, body: &[u8], timeout: Duration) -> Result<()> {
        self.write_in_flight(body, timeout)?;
        self.peer.verify(self.handle.raw(), self.server)?;
        self.sent += body.len();
        self.send_sequence += 1;
        self.usable = true;
        Ok(())
    }
    // Leaves this endpoint permanently unusable until the ordinary send path
    // verifies its live peer. The sole other caller consumes an acknowledged
    // fixed self-check and proves original-child exit while retaining this pipe.
    fn write_in_flight(&mut self, body: &[u8], timeout: Duration) -> Result<()> {
        self.begin()?;
        let deadline = deadline(timeout)?;
        ensure!(
            !body.is_empty()
                && body.len() <= MAX_FRAME
                && self.send_sequence < MAX_FRAMES
                && self.sent + body.len() <= MAX_BYTES,
            "control message bound exceeded"
        );
        let mut frame = FrameStorage(Vec::with_capacity(HEADER + body.len()));
        frame.0.extend_from_slice(b"AC01");
        frame.0.push(if self.server { 1 } else { 2 });
        frame.0.extend_from_slice(&self.nonce);
        frame.0.extend_from_slice(&self.send_sequence.to_le_bytes());
        frame
            .0
            .extend_from_slice(&(body.len() as u32).to_le_bytes());
        frame.0.extend_from_slice(body);
        io::transfer(self.handle.raw(), &mut frame.0, true, deadline)?;
        Ok(())
    }
    pub(crate) fn receive(&mut self, timeout: Duration) -> Result<Vec<u8>> {
        self.begin()?;
        self.receive_until(deadline(timeout)?)
    }
    /// Only loaded-profile birth can wait beyond an ordinary IPC deadline.
    /// The original contained and image-pinned bootstrap must match the request.
    /// This extends no credential, bootstrap startup or command deadline.
    pub(crate) fn receive_loaded_profile_birth_status(
        &mut self,
        request: &bootstrap::entry::credential_wire::child::BirthRequest,
    ) -> Result<()> {
        self.begin()?;
        let timeout = request.profile_load_wait()?;
        let selected = &request.selection;
        ensure!(
            self.server
                && self.peer.job.is_some()
                && self.peer.pid == selected.bootstrap_pid
                && bootstrap::pull::creation_time(self.peer.process.raw())?
                    == selected.bootstrap_created
                && self
                    .peer
                    .image
                    .context("profile birth source image missing")?
                    .file_identity()?
                    == selected.image_id,
            "profile birth wait differs from original contained source"
        );
        self.peer
            .token
            .require_account_runner(&selected.account.owner_sid, selected.session)?;
        let frame = self.receive_until(Instant::now() + timeout)?;
        self.usable = false;
        request.require_status(&frame)?;
        self.usable = true;
        Ok(())
    }
    fn receive_until(&mut self, deadline: Instant) -> Result<Vec<u8>> {
        ensure!(
            self.receive_sequence < MAX_FRAMES && self.received < MAX_BYTES,
            "control message count/byte bound exceeded"
        );
        let mut header = [0; HEADER];
        io::transfer(self.handle.raw(), &mut header, false, deadline)?;
        let len = u32::from_le_bytes(header[29..33].try_into().unwrap()) as usize;
        ensure!(
            &header[..4] == b"AC01"
                && header[4] == if self.server { 2 } else { 1 }
                && header[5..21] == self.nonce
                && u64::from_le_bytes(header[21..29].try_into().unwrap()) == self.receive_sequence,
            "control frame identity/direction/sequence mismatch"
        );
        ensure!(
            len > 0 && len <= MAX_FRAME && self.received + len <= MAX_BYTES,
            "control frame length exceeds bound"
        );
        let mut body = FrameStorage(vec![0; len]);
        io::transfer(self.handle.raw(), &mut body.0, false, deadline)?;
        self.peer.verify(self.handle.raw(), self.server)?;
        self.received += len;
        self.receive_sequence += 1;
        self.usable = true;
        Ok(std::mem::take(&mut body.0))
    }
}

fn noninheritable(handle: HANDLE) -> Result<()> {
    let mut flags = 0;
    unsafe { GetHandleInformation(handle, &mut flags)? };
    ensure!(
        flags & HANDLE_FLAG_INHERIT.0 == 0,
        "control handle became inheritable"
    );
    Ok(())
}
fn deadline(timeout: Duration) -> Result<Instant> {
    ensure!(
        timeout >= Duration::from_millis(1) && timeout <= Duration::from_secs(10),
        "control timeout outside bounds"
    );
    Ok(Instant::now() + timeout)
}

#[cfg(test)]
#[path = "account_control_tests.rs"]
mod tests;
