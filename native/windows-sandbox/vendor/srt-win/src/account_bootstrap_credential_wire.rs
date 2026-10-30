//! Bounded credential handoff. Native birth requires a separate bound request;
//! possession alone grants no account logon, resume or cleanup authority.
//! Selection is public protocol data; the password frame has no Debug/serde or
//! generic accessor, is bound to this selection, and is scrubbed on every exit.
use super::*;
use crate::{
    account_install::{AccountBinding, validate_random_id},
    account_state::image::PinnedRunnerImage,
};
use serde::{Deserialize, Serialize};

const PROTOCOL: &str = "alysis-bootstrap-credential-v1";
const MAX_SELECTION: usize = 4096;
const PASSWORD_PREFIX: &[u8] = b"ACPW1";

#[path = "account_bootstrap_account_child.rs"]
pub(crate) mod child;

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Selection {
    pub protocol: String,
    pub operation: String,
    pub logon: String,
    pub ready: String,
    pub bootstrap_pid: u32,
    pub bootstrap_created: u64,
    pub image_id: [u8; 24],
    pub session: u32,
    pub account: AccountBinding,
    pub credential_generation: String,
}
impl Selection {
    pub(crate) fn protocol() -> String {
        PROTOCOL.into()
    }
    pub(crate) fn validate(&self) -> Result<()> {
        ensure!(self.protocol == PROTOCOL, "credential protocol differs");
        for nonce in [&self.operation, &self.logon, &self.ready] {
            validate_random_id(nonce)?;
        }
        ensure!(
            self.operation != self.logon
                && self.logon != self.ready
                && self.operation != self.ready,
            "credential operation identities overlap"
        );
        self.account.validate()?;
        crate::account_credentials::generation_path(
            &self.account.installation_id,
            self.account.mode,
            &self.credential_generation,
        )?;
        ensure!(
            self.bootstrap_pid != 0 && self.bootstrap_created != 0 && self.image_id != [0; 24],
            "credential recipient identity missing"
        );
        Ok(())
    }
    pub(crate) fn encode(&self) -> Result<Vec<u8>> {
        self.validate()?;
        let bytes = serde_json::to_vec(self)?;
        ensure!(
            bytes.len() <= MAX_SELECTION,
            "credential selection exceeds bounds"
        );
        Ok(bytes)
    }
    fn decode(bytes: &[u8]) -> Result<Self> {
        ensure!(
            bytes.len() <= MAX_SELECTION,
            "credential selection exceeds bounds"
        );
        let selected: Self = serde_json::from_slice(bytes)
            .map_err(|_| anyhow::anyhow!("invalid credential selection"))?;
        selected.validate()?;
        Ok(selected)
    }
    pub(in crate::account_control) fn require_recipient(
        &self,
        ready: &str,
        image: &PinnedRunnerImage,
    ) -> Result<()> {
        self.validate()?;
        let mut session = 0;
        unsafe {
            windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
                std::process::id(),
                &mut session,
            )?;
        }
        ensure!(
            self.ready == ready
                && self.bootstrap_pid == std::process::id()
                && self.bootstrap_created
                    == super::super::pull::creation_time(unsafe { GetCurrentProcess() })?
                && self.session == session
                && self.account.owner_sid == crate::sid::current_user_sid()?
                && self.image_id == image.file_identity()?,
            "credential recipient differs from original bootstrap selection"
        );
        image.verify_process(unsafe { GetCurrentProcess() })?;
        Ok(())
    }
    pub(crate) fn acknowledgment(&self, received: bool) -> Result<Vec<u8>> {
        self.validate()?;
        // Full selection is echoed, not just a nonce. It contains no password.
        let mut bytes = if received {
            b"credential-received:".to_vec()
        } else {
            b"credential-prepared:".to_vec()
        };
        bytes.extend_from_slice(&self.encode()?);
        Ok(bytes)
    }
    pub(crate) fn require_ack(&self, frame: &[u8], received: bool) -> Result<()> {
        ensure!(
            frame == self.acknowledgment(received)?,
            "credential acknowledgment differs"
        );
        Ok(())
    }
}

pub(crate) struct PasswordFrame(FrameStorage);
impl PasswordFrame {
    pub(crate) fn encode(password: &str, selection: &Selection) -> Result<Self> {
        selection.validate()?;
        let length = password.encode_utf16().count();
        ensure!(
            (1..=127).contains(&length) && !password.contains('\0'),
            "invalid credential shape"
        );
        // Reserve once before writing the first secret unit: no realloc copies.
        let mut frame = FrameStorage(Vec::with_capacity(
            PASSWORD_PREFIX.len() + 32 + 2 + length * 2,
        ));
        frame.0.extend_from_slice(PASSWORD_PREFIX);
        frame.0.extend_from_slice(selection.logon.as_bytes());
        frame.0.extend_from_slice(&(length as u16).to_le_bytes());
        for unit in password.encode_utf16() {
            frame.0.extend_from_slice(&unit.to_le_bytes());
        }
        Ok(Self(frame))
    }
    fn receive(bytes: Vec<u8>, selection: &Selection) -> Result<Self> {
        let frame = Self(FrameStorage(bytes)); // Wipe even malformed input.
        frame.validate(selection)?;
        Ok(frame)
    }
    fn validate(&self, selection: &Selection) -> Result<()> {
        selection.validate()?;
        let bytes = &self.0.0;
        const HEADER: usize = 5 + 32 + 2;
        ensure!(
            (HEADER + 2..=HEADER + 254).contains(&bytes.len()),
            "credential frame size differs"
        );
        ensure!(
            bytes.starts_with(PASSWORD_PREFIX) && &bytes[5..37] == selection.logon.as_bytes(),
            "credential frame operation differs"
        );
        let length = u16::from_le_bytes([bytes[37], bytes[38]]) as usize;
        ensure!(
            (1..=127).contains(&length) && bytes.len() == HEADER + length * 2,
            "credential frame length differs"
        );
        let units = bytes[HEADER..]
            .chunks_exact(2)
            .map(|p| u16::from_le_bytes([p[0], p[1]]));
        ensure!(
            units.clone().all(|u| u != 0) && char::decode_utf16(units).all(|c| c.is_ok()),
            "invalid credential encoding"
        );
        Ok(())
    }
    pub(crate) fn send(
        &self,
        channel: &mut ControlChannel<'_>,
        selection: &Selection,
        timeout: Duration,
    ) -> Result<()> {
        self.validate(selection)?;
        channel.send(&self.0.0, timeout)
    }
}

/// Only a held in-memory secret, never a logged-on process or cleanup receipt.
/// There is no password accessor or launch method until the child-birth protocol
/// is implemented. Drop scrubs the packet before closing the private channel.
pub(super) struct ReceivedCredential<'a> {
    password: PasswordFrame,
    selection: Selection,
    channel: ControlChannel<'a>,
    image: &'a PinnedRunnerImage,
}

pub(super) fn selection(channel: &mut ControlChannel<'_>) -> Result<Selection> {
    let frame = FrameStorage(channel.receive(TIMEOUT)?);
    selection_frame(&frame.0)
}
pub(super) fn selection_frame(frame: &[u8]) -> Result<Selection> {
    Selection::decode(frame)
}

pub(super) fn receive<'a>(
    channel: ControlChannel<'a>,
    selection: Selection,
    ready: &str,
    image: &'a PinnedRunnerImage,
) -> Result<ReceivedCredential<'a>> {
    selection.require_recipient(ready, image)?;
    let mut channel = channel.with_image(image)?.into_role(false)?;
    channel.send(&selection.acknowledgment(false)?, TIMEOUT)?;
    let password = PasswordFrame::receive(channel.receive(TIMEOUT)?, &selection)?;
    // Recheck before acknowledging possession; every failure drops the secret.
    selection.require_recipient(ready, image)?;
    channel.send(&selection.acknowledgment(true)?, TIMEOUT)?;
    Ok(ReceivedCredential {
        password,
        selection,
        channel,
        image,
    })
}

impl ReceivedCredential<'_> {
    pub(super) fn wait_for_retirement(mut self) -> Result<()> {
        let frame = FrameStorage(self.channel.receive(TIMEOUT)?);
        let mut expected = b"discard-credential:".to_vec();
        expected.extend_from_slice(self.selection.logon.as_bytes());
        if frame.0 != expected {
            return child::hold_suspended(self, &frame.0);
        }
        drop(self.password);
        self.channel.send(b"credential-discarded", TIMEOUT)?;
        // Keep the original peer alive while its broker reads the acknowledgment.
        // The consumed broker guard terminates this trusted bootstrap afterward;
        // no receipt authorizes account creation or resource retirement here.
        let _refused = FrameStorage(self.channel.receive(TIMEOUT)?);
        anyhow::bail!("credential retired; further bootstrap operations unavailable")
    }
}

#[cfg(test)]
#[path = "account_bootstrap_credential_wire_tests.rs"]
mod tests;
