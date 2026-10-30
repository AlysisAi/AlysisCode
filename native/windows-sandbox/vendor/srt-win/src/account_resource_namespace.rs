//! Private retained empty namespace prerequisite. No bootstrap, child-directory
//! grant, keeper token, raw-handle API or account activation is exposed.
//! Ownership survives a database reopen only while the original guard lives.
use super::empty_job::{recorded, verify_actor};
use super::*;
use crate::util::OwnedHandle;
use windows::Win32::Foundation::CloseHandle;

const ROLE: Resource = Resource::NamespaceRoot;
const POLICY_VERSION: u32 = 2;

#[path = "account_resource_namespace_native.rs"]
pub(in crate::account_state::execution) mod native;
use native::*;

impl native::NamespacePolicySource for Identity {
    fn execution(&self) -> &ExecutionRecord {
        &self.execution
    }
    fn session(&self) -> u32 {
        self.session_id
    }
}

/// Drop releases the original root and parent handles only. It never adopts a
/// named object, recursively removes children, or acknowledges journal cleanup.
pub(crate) struct EmptyNamespaceRoot {
    identity: Identity,
    create_nonce: String,
    phase: Phase,
    parent: OwnedHandle,
    remove_nonce: Option<String>,
    #[cfg(test)]
    fault: Option<Fault>,
    #[cfg(test)]
    close_calls: usize,
}

enum Phase {
    Open(OwnedHandle),
    Closed,
    // CloseHandle was called once but its result was not trustworthy. Do not
    // retry a possibly reused handle value, including from Drop. The empty
    // resource and its durable intent remain unresolved until explicit repair.
    CloseUncertain,
}

#[cfg(test)]
#[derive(Clone, Copy, PartialEq)]
enum Fault {
    CreatedAfterCommit,
    RemoveIntentAfterCommit,
    RemovedAfterCommit,
}

impl ResourceJournal<'_> {
    /// Commit intent, exclusively create and verify, then return ownership.
    /// A separate acknowledgment keeps the guard with the caller if persisting
    /// successful birth fails. Failed/unverified birth leaves Creating intact.
    pub(crate) fn create_empty_namespace_root(&self) -> Result<EmptyNamespaceRoot> {
        require_live(self)?;
        self.replay()?;
        verify_actor(&self.identity)?;
        ensure!(
            self.replay()?.states.is_empty(),
            "empty-namespace adapter requires a resource plan with no other effects"
        );
        let parent = open_parent(&self.identity)?;
        let nonce = self.create_intent(ROLE, None)?;
        let handle = create_native(&self.identity, parent.raw()).context(
            "empty namespace root birth/configuration unverified; preserve creation intent",
        )?;
        Ok(EmptyNamespaceRoot {
            identity: self.identity.clone(),
            create_nonce: nonce,
            phase: Phase::Open(handle),
            parent,
            remove_nonce: None,
            #[cfg(test)]
            fault: None,
            #[cfg(test)]
            close_calls: 0,
        })
    }

    pub(crate) fn record_empty_namespace_root(&self, guard: &mut EmptyNamespaceRoot) -> Result<()> {
        require_live(self)?;
        guard.verify_identity(self)?;
        guard.verify_open()?;
        let event = guard.created_event();
        let replay = self.replay()?;
        match replay.states.get(&ROLE) {
            Some(State::Creating { nonce }) if nonce == &guard.create_nonce => {
                self.append(event.clone())?;
                #[cfg(test)]
                guard.fail(Fault::CreatedAfterCommit)?;
            }
            Some(State::Present { observed }) if observed == &observation() => {
                // append can commit then fail its readback. Match the exact
                // original nonce/event rather than accepting Present by name.
                ensure!(
                    recorded(self, &event)?,
                    "empty namespace root birth receipt mismatch"
                );
            }
            _ => anyhow::bail!("empty namespace root birth has changed or ambiguous journal state"),
        }
        ensure!(
            recorded(self, &event)?,
            "empty namespace root birth acknowledgment missing"
        );
        Ok(())
    }

    /// Original retained ownership and stopped authority are both required.
    /// This is deliberately not a cold-recovery-by-name API.
    pub(crate) fn retire_empty_namespace_root(&self, guard: &mut EmptyNamespaceRoot) -> Result<()> {
        require_stopped(self)?;
        guard.verify_identity(self)?;
        ensure!(
            recorded(self, &guard.created_event())?,
            "empty namespace root cannot retire without its exact committed birth"
        );
        let replay = self.replay()?;
        if matches!(replay.states.get(&ROLE), Some(State::Removed)) {
            let nonce = guard
                .remove_nonce
                .as_ref()
                .context("missing retained removal nonce")?;
            ensure!(
                matches!(guard.phase, Phase::Closed)
                    && recorded(
                        self,
                        &Event::Removed {
                            resource: ROLE,
                            nonce: nonce.clone()
                        }
                    )?,
                "removed state lacks the original retained close proof"
            );
            return Ok(());
        }
        ensure!(
            !matches!(guard.phase, Phase::CloseUncertain),
            "empty namespace root close is uncertain; retain recovery state without retry"
        );
        match replay.states.get(&ROLE) {
            Some(State::Present { observed }) if observed == &observation() => {
                guard.verify_open()?;
                let nonce = match &guard.remove_nonce {
                    Some(nonce) => nonce.clone(),
                    None => {
                        let nonce = super::super::random_id()?;
                        guard.remove_nonce = Some(nonce.clone());
                        nonce
                    }
                };
                self.append(Event::RemoveIntent {
                    resource: ROLE,
                    nonce,
                })?;
                #[cfg(test)]
                guard.fail(Fault::RemoveIntentAfterCommit)?;
            }
            Some(State::Removing { nonce, observed })
                if observed == &observation() && guard.remove_nonce.as_ref() == Some(nonce) => {}
            _ => anyhow::bail!("empty namespace root removal differs from retained operation"),
        }
        let nonce = guard
            .remove_nonce
            .clone()
            .context("missing retained removal nonce")?;
        ensure!(
            recorded(
                self,
                &Event::RemoveIntent {
                    resource: ROLE,
                    nonce: nonce.clone()
                }
            )?,
            "empty namespace root removal intent is not durable"
        );
        if matches!(guard.phase, Phase::Open(_)) {
            // Recheck after journaling. An unexpected child, altered ACL or
            // label never reaches this explicit retirement CloseHandle. Dropping
            // the caller-owned guard still closes its original reference.
            guard.verify_open()?;
            let Phase::Open(handle) = std::mem::replace(&mut guard.phase, Phase::CloseUncertain)
            else {
                unreachable!()
            };
            #[cfg(test)]
            {
                guard.close_calls += 1;
            }
            unsafe { CloseHandle(handle.into_raw()) }
                .context("empty namespace root close unverified; do not retry the handle")?;
            guard.phase = Phase::Closed;
        }
        ensure!(
            matches!(guard.phase, Phase::Closed),
            "missing exact retained close proof"
        );
        verify_parent(guard.parent.raw(), &guard.identity)?;
        require_absent(guard.parent.raw(), &guard.identity)?;
        self.append(Event::Removed {
            resource: ROLE,
            nonce: nonce.clone(),
        })?;
        #[cfg(test)]
        guard.fail(Fault::RemovedAfterCommit)?;
        ensure!(
            recorded(
                self,
                &Event::Removed {
                    resource: ROLE,
                    nonce
                }
            )?,
            "empty namespace root retirement acknowledgment missing"
        );
        Ok(())
    }
}

impl EmptyNamespaceRoot {
    fn created_event(&self) -> Event {
        Event::Created {
            resource: ROLE,
            nonce: self.create_nonce.clone(),
            observed: observation(),
        }
    }
    fn verify_identity(&self, journal: &ResourceJournal<'_>) -> Result<()> {
        ensure!(
            self.identity == journal.identity,
            "empty namespace root belongs to another execution/session"
        );
        verify_actor(&self.identity)?;
        let replay = journal.replay()?;
        ensure!(
            replay.states.keys().all(|role| *role == ROLE),
            "empty-namespace adapter cannot clean an execution with other resource effects"
        );
        ensure!(
            recorded(
                journal,
                &Event::CreateIntent {
                    resource: ROLE,
                    nonce: self.create_nonce.clone(),
                    subject: self.identity.subject(ROLE).into(),
                    name: self.identity.name(ROLE),
                    acl: None,
                }
            )?,
            "empty namespace root original creation intent changed"
        );
        Ok(())
    }
    fn verify_open(&self) -> Result<()> {
        let Phase::Open(handle) = &self.phase else {
            anyhow::bail!("empty namespace root has no retained open ownership handle");
        };
        verify_native(handle.raw(), self.parent.raw(), &self.identity)
    }
    #[cfg(test)]
    fn fail(&mut self, point: Fault) -> Result<()> {
        if self.fault == Some(point) {
            self.fault = None;
            anyhow::bail!("injected failure after durable empty-namespace transition");
        }
        Ok(())
    }
}

fn require_live(journal: &ResourceJournal<'_>) -> Result<()> {
    match &journal.authority {
        Authority::Live(_) => Ok(()),
        #[cfg(test)]
        Authority::FixtureLive => Ok(()),
        _ => anyhow::bail!("empty namespace root birth requires live execution authority"),
    }
}
fn require_stopped(journal: &ResourceJournal<'_>) -> Result<()> {
    match &journal.authority {
        Authority::Stopped(_) => Ok(()),
        #[cfg(test)]
        Authority::FixtureStopped => Ok(()),
        _ => anyhow::bail!("empty namespace root retirement requires stopped execution authority"),
    }
}

fn observation() -> Observation {
    Observation::EmptyNamespaceRoot {
        policy_version: POLICY_VERSION,
    }
}

#[cfg(test)]
#[path = "account_resource_namespace_tests.rs"]
mod tests;
