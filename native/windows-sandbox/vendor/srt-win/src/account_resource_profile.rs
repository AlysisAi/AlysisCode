//! Exclusive, unused host profile only. No command, account profile, bootstrap,
//! arbitrary path recovery, implicit deletion or caller-supplied native effect.
use super::empty_job::{recorded, verify_actor};
use super::*;

#[path = "account_resource_profile_native.rs"]
mod native;
pub(in crate::account_state::execution) use native::{NativeProfile, UsedNativeProfile};

const ROLE: Resource = Resource::HostProfile;

// Sealed in this private module. NativeProfile is the only production adapter.
trait ProfileAccess {
    fn create(&mut self, identity: &Identity) -> Result<[u8; 24]>;
    fn unchanged(&self) -> Result<()>;
    fn close_trees(&mut self) -> Result<()>;
    fn delete(&mut self, identity: &Identity) -> Result<()>;
    fn absent(&self) -> Result<()>;
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Phase {
    Prepared,
    CreateUncertain,
    Created,
    CloseUncertain,
    DeleteUncertain,
    Deleted,
}

struct Operation {
    identity: Identity,
    create_nonce: Option<String>,
    remove_nonce: Option<String>,
    folder_id: Option<[u8; 24]>,
    phase: Phase,
    #[cfg(test)]
    fault: Option<Fault>,
}

/// Ownership is process-local and non-serializable. Drop only closes original
/// handles; it never invokes the SDK delete or records a successful cleanup.
pub(crate) struct UnusedHostProfile {
    operation: Operation,
    native: NativeProfile,
}

#[cfg(test)]
#[derive(Clone, Copy, PartialEq)]
enum Fault {
    IntentCommitted,
    CreatedCommitted,
    RemoveCommitted,
    RemovedCommitted,
}

fn require_live(j: &ResourceJournal<'_>) -> Result<()> {
    match j.authority {
        Authority::Live(_) => Ok(()),
        #[cfg(test)]
        Authority::FixtureLive => Ok(()),
        _ => anyhow::bail!("profile creation requires live authority"),
    }
}
fn require_stopped(j: &ResourceJournal<'_>) -> Result<()> {
    match j.authority {
        Authority::Stopped(_) => Ok(()),
        #[cfg(test)]
        Authority::FixtureStopped => Ok(()),
        _ => anyhow::bail!("profile retirement requires stopped authority"),
    }
}

impl Operation {
    fn new(identity: Identity) -> Self {
        Self {
            identity,
            create_nonce: None,
            remove_nonce: None,
            folder_id: None,
            phase: Phase::Prepared,
            #[cfg(test)]
            fault: None,
        }
    }
    fn verify(&self, j: &ResourceJournal<'_>) -> Result<()> {
        ensure!(
            self.identity == j.identity,
            "profile belongs to another execution"
        );
        verify_actor(&self.identity)?;
        ensure!(
            j.replay()?.states.keys().all(|r| *r == ROLE),
            "unused profile refuses other resource effects"
        );
        Ok(())
    }
    fn intent(&self) -> Result<Event> {
        Ok(Event::CreateIntent {
            resource: ROLE,
            nonce: self
                .create_nonce
                .clone()
                .context("profile lacks original creation nonce")?,
            subject: self.identity.subject(ROLE).into(),
            name: self.identity.name(ROLE),
            acl: None,
        })
    }
    fn created(&self) -> Result<Event> {
        Ok(Event::Created {
            resource: ROLE,
            nonce: self
                .create_nonce
                .clone()
                .context("profile lacks original creation nonce")?,
            observed: Observation::UnusedHostProfile {
                policy_version: 1,
                folder_id: self
                    .folder_id
                    .context("profile lacks captured filesystem identity")?,
            },
        })
    }
    fn removal(&self, complete: bool) -> Result<Event> {
        let nonce = self
            .remove_nonce
            .clone()
            .context("profile lacks original removal nonce")?;
        Ok(if complete {
            Event::Removed {
                resource: ROLE,
                nonce,
            }
        } else {
            Event::RemoveIntent {
                resource: ROLE,
                nonce,
            }
        })
    }
    fn birth(&mut self, j: &ResourceJournal<'_>, profile: &mut impl ProfileAccess) -> Result<()> {
        require_live(j)?;
        self.verify(j)?;
        ensure!(
            self.phase == Phase::Prepared,
            "profile creation was already attempted"
        );
        if self.create_nonce.is_none() {
            self.create_nonce = Some(super::super::random_id()?);
        }
        match j.replay()?.states.get(&ROLE) {
            None => {
                j.append(self.intent()?)?;
                #[cfg(test)]
                self.fail(Fault::IntentCommitted)?;
            }
            Some(State::Creating { nonce }) if Some(nonce) == self.create_nonce.as_ref() => {}
            _ => anyhow::bail!("profile creation intent differs from original operation"),
        }
        ensure!(
            recorded(j, &self.intent()?)?,
            "profile intent is not durable"
        );
        self.verify(j)?;
        self.phase = Phase::CreateUncertain;
        let id = profile.create(&self.identity)?;
        ensure!(
            id != [0; 24],
            "profile creation lacks original folder identity"
        );
        profile.unchanged()?;
        self.folder_id = Some(id);
        self.phase = Phase::Created;
        Ok(())
    }
    fn record(&mut self, j: &ResourceJournal<'_>, profile: &impl ProfileAccess) -> Result<()> {
        require_live(j)?;
        self.verify(j)?;
        ensure!(
            self.phase == Phase::Created,
            "profile birth is not retained and verified"
        );
        ensure!(
            recorded(j, &self.intent()?)?,
            "profile original intent changed"
        );
        profile.unchanged()?;
        let event = self.created()?;
        match j.replay()?.states.get(&ROLE) {
            Some(State::Creating { nonce }) if Some(nonce) == self.create_nonce.as_ref() => {
                j.append(event.clone())?;
                #[cfg(test)]
                self.fail(Fault::CreatedCommitted)?;
            }
            Some(State::Present { .. }) => {}
            _ => anyhow::bail!("profile birth receipt state differs from retained operation"),
        }
        ensure!(recorded(j, &event)?, "profile exact birth receipt missing");
        Ok(())
    }
    fn retire(&mut self, j: &ResourceJournal<'_>, profile: &mut impl ProfileAccess) -> Result<()> {
        require_stopped(j)?;
        self.verify(j)?;
        ensure!(
            matches!(self.phase, Phase::Created | Phase::Deleted),
            "profile close/delete outcome uncertain; preserve original operation and journal"
        );
        ensure!(
            recorded(j, &self.intent()?)? && recorded(j, &self.created()?)?,
            "profile retirement lacks its exact successful birth"
        );
        if matches!(j.replay()?.states.get(&ROLE), Some(State::Removed)) {
            ensure!(
                self.phase == Phase::Deleted
                    && recorded(j, &self.removal(false)?)?
                    && recorded(j, &self.removal(true)?)?,
                "removed profile lacks original delete proof"
            );
            return profile.absent();
        }
        match j.replay()?.states.get(&ROLE) {
            Some(State::Present { .. }) => {
                ensure!(
                    self.phase == Phase::Created,
                    "profile phase differs from journal"
                );
                profile.unchanged()?;
                if self.remove_nonce.is_none() {
                    self.remove_nonce = Some(super::super::random_id()?);
                }
                j.append(self.removal(false)?)?;
                #[cfg(test)]
                self.fail(Fault::RemoveCommitted)?;
            }
            Some(State::Removing { nonce, .. }) if Some(nonce) == self.remove_nonce.as_ref() => {}
            _ => anyhow::bail!("profile removal differs from retained operation"),
        }
        ensure!(
            recorded(j, &self.removal(false)?)?,
            "profile removal intent is not durable"
        );
        self.verify(j)?;
        if self.phase == Phase::Created {
            profile.unchanged()?;
            self.phase = Phase::CloseUncertain;
            profile.close_trees()?;
            // Closing the last storage handle permits SDK deletion. Never retry
            // a failed close or an uncertain SDK effect, even after DB reopen.
            self.phase = Phase::DeleteUncertain;
            verify_actor(&self.identity)?;
            profile.delete(&self.identity)?;
            self.phase = Phase::Deleted;
        }
        profile.absent()?;
        j.append(self.removal(true)?)?;
        #[cfg(test)]
        self.fail(Fault::RemovedCommitted)?;
        ensure!(
            recorded(j, &self.removal(true)?)?,
            "profile removal acknowledgment missing"
        );
        Ok(())
    }
    #[cfg(test)]
    fn fail(&mut self, fault: Fault) -> Result<()> {
        if self.fault == Some(fault) {
            self.fault = None;
            anyhow::bail!("injected after profile commit");
        }
        Ok(())
    }
}

impl ResourceJournal<'_> {
    pub(crate) fn prepare_unused_host_profile(&self) -> Result<UnusedHostProfile> {
        require_live(self)?;
        verify_actor(&self.identity)?;
        ensure!(
            self.replay()?.states.is_empty(),
            "profile preparation requires untouched plan"
        );
        Ok(UnusedHostProfile {
            operation: Operation::new(self.identity.clone()),
            native: NativeProfile::default(),
        })
    }
    pub(crate) fn create_unused_host_profile(&self, guard: &mut UnusedHostProfile) -> Result<()> {
        guard.operation.birth(self, &mut guard.native)
    }
    pub(crate) fn record_unused_host_profile(&self, guard: &mut UnusedHostProfile) -> Result<()> {
        guard.operation.record(self, &guard.native)
    }
    pub(crate) fn retire_unused_host_profile(&self, guard: &mut UnusedHostProfile) -> Result<()> {
        guard.operation.retire(self, &mut guard.native)
    }
}

#[cfg(test)]
#[path = "account_resource_profile_tests.rs"]
mod tests;
