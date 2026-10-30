//! Narrow commit-before-ack integration with the retained empty-job adapter.
//! Wire data cannot append an observation or reconstruct native ownership.
//! No account runner, profile effect, cleanup or process launch is enabled.
use super::*;
use crate::account_control::ControlChannel;
use std::time::Duration;

const PROTOCOL: &str = "alysis-empty-bootstrap-job-birth-v1";

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    protocol: String,
    identity: Identity,
    operation_nonce: String,
}

impl Request {
    fn new(identity: Identity, operation_nonce: String) -> Result<Self> {
        identity.validate()?;
        crate::account_install::validate_random_id(&operation_nonce)?;
        Ok(Self {
            protocol: PROTOCOL.into(),
            identity,
            operation_nonce,
        })
    }
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Acknowledgment {
    request: Request,
    committed_sequence: usize,
    observation: Observation,
}

/// Owns the only broker endpoint. Any failed exchange drops that endpoint,
/// including native validation/commit failures between receiving and sending.
/// One birth confirmation per instance. Constructing another session is not
/// recovery: it still needs the original native guard and an exact journal.
pub(crate) struct ResourceControl<'a> {
    channel: Option<ControlChannel<'a>>,
    identity: Identity,
    completed: bool,
}

pub(super) struct PendingReply<'a> {
    channel: ControlChannel<'a>,
    request: Request,
}

impl<'a> ResourceControl<'a> {
    pub(crate) fn new(channel: ControlChannel<'a>, journal: &ResourceJournal<'_>) -> Result<Self> {
        journal.replay()?;
        Ok(Self {
            channel: Some(channel.into_role(true)?),
            identity: journal.identity.clone(),
            completed: false,
        })
    }

    pub(super) fn begin_empty_job_birth(
        &mut self,
        journal: &ResourceJournal<'_>,
        operation_nonce: &str,
        timeout: Duration,
    ) -> Result<PendingReply<'a>> {
        // Take before any fallible check. The caller's retained resource guard
        // and durable intent survive an error, but this connection cannot retry.
        let mut channel = self
            .channel
            .take()
            .context("resource control permanently refused")?;
        ensure!(!self.completed, "resource acknowledgment was already sent");
        journal.replay()?;
        ensure!(
            self.identity == journal.identity,
            "resource control execution/session changed"
        );
        let expected = Request::new(self.identity.clone(), operation_nonce.into())?;
        let bytes = channel.receive(timeout)?;
        let request: Request =
            serde_json::from_slice(&bytes).context("invalid resource confirmation request")?;
        ensure!(
            request == expected,
            "resource confirmation identity/operation mismatch"
        );
        Ok(PendingReply { channel, request })
    }

    /// Only the native empty-job adapter calls this after validating its retained
    /// guard and committing/readback of birth. No observation parameter or
    /// success callback is accepted from a peer or other caller.
    pub(super) fn finish_empty_job_birth(
        &mut self,
        mut reply: PendingReply<'a>,
        journal: &ResourceJournal<'_>,
        timeout: Duration,
    ) -> Result<()> {
        ensure!(
            self.channel.is_none() && !self.completed,
            "invalid resource control phase"
        );
        ensure!(
            reply.request.identity == self.identity && self.identity == journal.identity,
            "resource acknowledgment execution/session changed"
        );
        let replay = journal.replay()?;
        let expected = Observation::EmptyBootstrapJob { policy_version: 1 };
        ensure!(
            replay.events == 2
                && replay.states.len() == 1
                && matches!(replay.states.get(&Resource::BootstrapJob), Some(State::Present { observed }) if observed == &expected),
            "resource acknowledgment requires exact standalone committed birth"
        );
        let event: String = journal.connection.query_row(
            "SELECT event_json FROM candidate_resource_events WHERE container=?1 AND sequence=1",
            [&journal.identity.execution.identity.container],
            |row| row.get(0),
        )?;
        ensure!(
            serde_json::from_str::<Event>(&event)?
                == (Event::Created {
                    resource: Resource::BootstrapJob,
                    nonce: reply.request.operation_nonce.clone(),
                    observed: expected.clone(),
                }),
            "resource acknowledgment does not match exact committed operation"
        );
        let message = Acknowledgment {
            request: reply.request,
            committed_sequence: 1,
            observation: expected,
        };
        reply
            .channel
            .send(&serde_json::to_vec(&message)?, timeout)?;
        self.completed = true;
        // Retain the connection until its owner drops it. This lets the client
        // read the final response before observing a pipe disconnect.
        self.channel = Some(reply.channel);
        Ok(())
    }
}

/// Client-side message confirmation only. It cannot create native resources,
/// produce a lifecycle/retirement receipt or reopen a failed channel.
pub(crate) struct EmptyJobConfirmation<'a> {
    channel: ControlChannel<'a>,
    expected: Request,
}

impl<'a> EmptyJobConfirmation<'a> {
    pub(crate) fn start(
        channel: ControlChannel<'a>,
        execution: ExecutionRecord,
        session_id: u32,
        operation_nonce: String,
        timeout: Duration,
    ) -> Result<Self> {
        let mut channel = channel.into_role(false)?;
        let expected = Request::new(
            Identity {
                execution,
                session_id,
            },
            operation_nonce,
        )?;
        channel.send(&serde_json::to_vec(&expected)?, timeout)?;
        Ok(Self { channel, expected })
    }

    pub(crate) fn finish(mut self, timeout: Duration) -> Result<()> {
        let bytes = self.channel.receive(timeout)?;
        let message: Acknowledgment =
            serde_json::from_slice(&bytes).context("invalid resource acknowledgment")?;
        ensure!(
            message.request == self.expected
                && message.committed_sequence == 1
                && message.observation == (Observation::EmptyBootstrapJob { policy_version: 1 }),
            "resource acknowledgment identity/operation/observation mismatch"
        );
        Ok(())
    }
}

#[cfg(test)]
#[path = "account_resource_control_tests.rs"]
mod tests;
