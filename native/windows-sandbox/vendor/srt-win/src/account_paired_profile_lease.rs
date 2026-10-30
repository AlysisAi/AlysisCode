//! Bounded liveness between the original Using and Capture frames. This state
//! has no native handles, resource operation, persistence or cleanup authority.
use super::{Message, Phase};
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use std::{
    cell::Cell,
    time::{Duration, Instant},
};

const PREFIX: &[u8] = b"alysis-paired-owner-use-lease-v1:";
const MAX_FRAME: usize = 1024;
const MAX_PULSES: u32 = 96;
const LIFETIME: Duration = Duration::from_secs(300);

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Subject {
    owner_birth: String,
    owner_resume: String,
    consumer_birth: String,
    use_nonce: String,
    folder: [u8; 24],
}

#[derive(Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Direction {
    Renew,
    Ack,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Frame {
    subject: Subject,
    sequence: u32,
    phase: Direction,
}
impl Frame {
    fn wire(&self) -> Result<Vec<u8>> {
        let mut bytes = PREFIX.to_vec();
        bytes.extend_from_slice(&serde_json::to_vec(self)?);
        ensure!(
            bytes.len() <= MAX_FRAME,
            "paired use lease frame exceeds bound"
        );
        Ok(bytes)
    }
    fn read(bytes: &[u8]) -> Result<Self> {
        ensure!(
            bytes.len() <= MAX_FRAME && is_renewal(bytes),
            "paired use lease frame differs or exceeds bound"
        );
        serde_json::from_slice(&bytes[PREFIX.len()..])
            .map_err(|_| anyhow::anyhow!("malformed paired use lease frame"))
    }
}

/// Prefix dispatch only. ACKs and malformed lease frames also reach the strict
/// parser, which refuses wrong direction or malformed data without a retry.
pub(super) fn is_renewal(bytes: &[u8]) -> bool {
    bytes.starts_with(PREFIX)
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Side {
    Sender,
    Receiver,
}

/// Constructed from a fully validated Using message supplied by the live flow.
/// This state is neither cloned nor serialized and proves no native ownership.
pub(super) struct UseLease {
    subject: Subject,
    started: Instant,
    next: u32,
    side: Option<Side>,
    pending: Option<Vec<u8>>,
    refused: Cell<bool>,
    #[cfg(test)]
    now: Option<Instant>,
}
impl UseLease {
    pub(super) fn new(using: &Message) -> Result<Self> {
        let started = Instant::now();
        using.validate()?;
        ensure!(
            using.phase == Phase::Using,
            "paired use lease requires original Using"
        );
        Ok(Self {
            subject: Subject {
                owner_birth: using.request.birth.nonce.clone(),
                owner_resume: using.request.nonce.clone(),
                consumer_birth: using
                    .consumer
                    .as_ref()
                    .context("lease consumer missing")?
                    .nonce
                    .clone(),
                use_nonce: using.use_nonce.clone().context("lease use nonce missing")?,
                folder: using.folder.context("lease original folder missing")?,
            },
            started,
            next: 0,
            side: None,
            pending: None,
            refused: Cell::new(false),
            #[cfg(test)]
            now: None,
        })
    }

    fn require_time(&self) -> Result<()> {
        let now = Instant::now();
        #[cfg(test)]
        let now = self.now.unwrap_or(now);
        ensure!(
            now.checked_duration_since(self.started)
                .is_some_and(|age| age < LIFETIME),
            "paired use lease expired or clock moved backwards"
        );
        Ok(())
    }

    // A failed operation leaves this bit set, including parse/serialization
    // failures. Only the end of an entirely successful operation clears it.
    fn attempt(&self) -> Result<()> {
        ensure!(
            !self.refused.replace(true),
            "paired use lease already refused"
        );
        self.require_time()
    }

    pub(super) fn renewal(&mut self) -> Result<Vec<u8>> {
        self.attempt()?;
        ensure!(
            self.pending.is_none() && self.side != Some(Side::Receiver) && self.next < MAX_PULSES,
            "paired use lease pending, wrong direction or exhausted"
        );
        let request = Frame {
            subject: self.subject.clone(),
            sequence: self.next,
            phase: Direction::Renew,
        }
        .wire()?;
        let acknowledgment = Frame {
            subject: self.subject.clone(),
            sequence: self.next,
            phase: Direction::Ack,
        }
        .wire()?;
        self.require_time()?;
        self.side = Some(Side::Sender);
        self.pending = Some(acknowledgment);
        self.refused.set(false);
        Ok(request)
    }

    /// The authenticated receiver must send this exact ACK or terminate its
    /// flow. Accepting one pulse advances the receiver; replay cannot retry it.
    pub(super) fn accept(&mut self, bytes: &[u8]) -> Result<Vec<u8>> {
        self.attempt()?;
        ensure!(
            self.pending.is_none() && self.side != Some(Side::Sender) && self.next < MAX_PULSES,
            "paired use lease pending, wrong direction or exhausted"
        );
        let frame = Frame::read(bytes)?;
        ensure!(
            frame.subject == self.subject
                && frame.sequence == self.next
                && frame.phase == Direction::Renew,
            "paired use lease subject, sequence or direction differs"
        );
        let acknowledgment = Frame {
            phase: Direction::Ack,
            ..frame
        }
        .wire()?;
        self.require_time()?;
        self.side = Some(Side::Receiver);
        self.next += 1;
        self.refused.set(false);
        Ok(acknowledgment)
    }

    pub(super) fn confirm(&mut self, bytes: &[u8]) -> Result<()> {
        self.attempt()?;
        ensure!(
            self.side == Some(Side::Sender),
            "paired use lease confirmation direction differs"
        );
        let pending = self
            .pending
            .as_ref()
            .context("paired use lease has no pending pulse")?;
        ensure!(
            bytes == pending.as_slice(),
            "paired use lease acknowledgment differs"
        );
        self.require_time()?;
        self.pending.take();
        self.next += 1;
        self.refused.set(false);
        Ok(())
    }

    /// Capture is permitted with no pulses or after pulse 96, but never while
    /// an ACK is pending, after refusal, or at/after the original hard deadline.
    pub(super) fn require_current(&self) -> Result<()> {
        let result = (|| {
            ensure!(!self.refused.get(), "paired use lease already refused");
            self.require_time()?;
            ensure!(
                self.pending.is_none(),
                "paired use lease acknowledgment pending"
            );
            Ok(())
        })();
        if result.is_err() {
            self.refused.set(true);
        }
        result
    }
}

#[cfg(test)]
#[path = "account_paired_profile_lease_tests.rs"]
mod tests;
