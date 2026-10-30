//! Standalone station grant only: original broker/handle, durable exact intent,
//! one apply, one stopped restore. No process/profile/desktop or restart adoption.
use super::super::empty_job::recorded;
use super::*;

const ROLE: Resource = Resource::WindowStationGrant;

// Private, sealed by the module boundary. Only Station implements this outside
// cfg(test); callers cannot supply a closure claiming successful native effects.
trait StationAccess {
    fn verify(&self) -> Result<()>;
    fn read(&self) -> Result<Snapshot>;
    fn write(&mut self, wanted: &Snapshot) -> Result<()>;
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Phase {
    Prepared,
    ApplyUncertain,
    Applied,
    RestoreUncertain,
    Restored,
}

pub(super) struct Operation {
    identity: Identity,
    pub(super) before: Snapshot,
    pub(super) after: Snapshot,
    create_nonce: Option<String>,
    remove_nonce: Option<String>,
    phase: Phase,
    #[cfg(test)]
    fault: Option<Fault>,
}

#[cfg(test)]
#[derive(Clone, Copy, PartialEq)]
enum Fault {
    IntentCommitted,
    CreatedCommitted,
    RemoveCommitted,
    RemovedCommitted,
}

impl Operation {
    pub(super) fn new(identity: Identity, before: Snapshot, after: Snapshot) -> Self {
        Self {
            identity,
            before,
            after,
            create_nonce: None,
            remove_nonce: None,
            phase: Phase::Prepared,
            #[cfg(test)]
            fault: None,
        }
    }
    pub(super) fn verify_prepared(
        &self,
        journal: &ResourceJournal<'_>,
        station: &Station,
    ) -> Result<()> {
        require_live(journal)?;
        ensure!(
            self.phase == Phase::Prepared
                && self.create_nonce.is_none()
                && journal.replay()?.states.is_empty(),
            "station preparation is no longer untouched"
        );
        self.verify(journal, station)?;
        ensure!(
            station.read()? == self.before,
            "station descriptor changed after preparation"
        );
        Ok(())
    }
    fn verify(&self, journal: &ResourceJournal<'_>, station: &impl StationAccess) -> Result<()> {
        ensure!(
            self.identity == journal.identity,
            "station execution identity changed"
        );
        verify_actor(&self.identity)?;
        ensure!(
            journal.replay()?.states.keys().all(|role| *role == ROLE),
            "standalone station adapter refuses other resource effects"
        );
        station.verify()?;
        ensure!(
            proposed(&self.before, &self.identity.execution.identity.package_sid)? == self.after,
            "station proposal differs from exact package policy"
        );
        Ok(())
    }
    fn intent(&self) -> Result<Event> {
        Ok(Event::CreateIntent {
            resource: ROLE,
            nonce: self
                .create_nonce
                .clone()
                .context("no retained station creation nonce")?,
            subject: self.identity.subject(ROLE).into(),
            name: self.identity.name(ROLE),
            acl: Some(AclIntent {
                before: serde_json::to_vec(&self.before)?,
                after: serde_json::to_vec(&self.after)?,
            }),
        })
    }
    fn created(&self) -> Result<Event> {
        Ok(Event::Created {
            resource: ROLE,
            nonce: self
                .create_nonce
                .clone()
                .context("no retained station creation nonce")?,
            observed: Observation::WindowStationAcl { policy_version: 1 },
        })
    }
    fn removal(&self, completed: bool) -> Result<Event> {
        let nonce = self
            .remove_nonce
            .clone()
            .context("no retained station removal nonce")?;
        Ok(if completed {
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
    fn original_intent(&self, journal: &ResourceJournal<'_>) -> Result<()> {
        ensure!(
            recorded(journal, &self.intent()?)?,
            "station original durable intent changed"
        );
        Ok(())
    }
    fn apply(
        &mut self,
        journal: &ResourceJournal<'_>,
        station: &mut impl StationAccess,
    ) -> Result<()> {
        require_live(journal)?;
        self.verify(journal, station)?;
        ensure!(
            self.phase == Phase::Prepared,
            "station apply already attempted; never retry uncertain effects"
        );
        ensure!(
            station.read()? == self.before,
            "station descriptor changed before grant"
        );
        if self.create_nonce.is_none() {
            self.create_nonce = Some(super::super::super::random_id()?);
        }
        let replay = journal.replay()?;
        match replay.states.get(&ROLE) {
            None => {
                journal.append(self.intent()?)?;
                #[cfg(test)]
                self.fail(Fault::IntentCommitted)?;
            }
            Some(State::Creating { nonce }) if Some(nonce) == self.create_nonce.as_ref() => {}
            _ => anyhow::bail!("station creation state differs from retained operation"),
        }
        self.original_intent(journal)?;
        // Check again after durable I/O. Host ACL edits are not synchronized;
        // observable drift is refused rather than overwritten as a repair.
        self.verify(journal, station)?;
        ensure!(
            station.read()? == self.before,
            "station descriptor drift after durable intent"
        );
        self.phase = Phase::ApplyUncertain;
        station
            .write(&self.after)
            .context("station grant outcome uncertain; preserve intent and handle")?;
        station.verify()?;
        ensure!(
            station.read()? == self.after,
            "station grant readback mismatch; preserve intent"
        );
        self.phase = Phase::Applied;
        Ok(())
    }
    fn record(
        &mut self,
        journal: &ResourceJournal<'_>,
        station: &impl StationAccess,
    ) -> Result<()> {
        require_live(journal)?;
        self.verify(journal, station)?;
        self.original_intent(journal)?;
        ensure!(
            self.phase == Phase::Applied,
            "station grant lacks retained successful write/readback proof"
        );
        ensure!(
            station.read()? == self.after,
            "station descriptor drift before grant receipt"
        );
        let event = self.created()?;
        match journal.replay()?.states.get(&ROLE) {
            Some(State::Creating { nonce }) if Some(nonce) == self.create_nonce.as_ref() => {
                journal.append(event.clone())?;
                #[cfg(test)]
                self.fail(Fault::CreatedCommitted)?;
            }
            Some(State::Present { .. }) => {}
            _ => anyhow::bail!("station receipt has changed or ambiguous state"),
        }
        ensure!(
            recorded(journal, &event)?,
            "station exact grant receipt missing"
        );
        Ok(())
    }
    fn restore(
        &mut self,
        journal: &ResourceJournal<'_>,
        station: &mut impl StationAccess,
    ) -> Result<()> {
        require_stopped(journal)?;
        self.verify(journal, station)?;
        self.original_intent(journal)?;
        ensure!(
            matches!(self.phase, Phase::Applied | Phase::Restored),
            "station restore lacks retained success proof or has an uncertain outcome"
        );
        ensure!(
            recorded(journal, &self.created()?)?,
            "station restore requires exact committed grant receipt"
        );
        if matches!(journal.replay()?.states.get(&ROLE), Some(State::Removed)) {
            ensure!(
                self.phase == Phase::Restored
                    && recorded(journal, &self.removal(false)?)?
                    && recorded(journal, &self.removal(true)?)?,
                "station removal lacks retained original restore proof"
            );
            ensure!(
                station.read()? == self.before,
                "station descriptor drift after restored receipt"
            );
            return Ok(());
        }
        match journal.replay()?.states.get(&ROLE) {
            Some(State::Present { .. }) => {
                ensure!(
                    self.phase == Phase::Applied,
                    "station restore phase differs from journal"
                );
                ensure!(
                    station.read()? == self.after,
                    "station descriptor drift before removal intent"
                );
                if self.remove_nonce.is_none() {
                    self.remove_nonce = Some(super::super::super::random_id()?);
                }
                journal.append(self.removal(false)?)?;
                #[cfg(test)]
                self.fail(Fault::RemoveCommitted)?;
            }
            Some(State::Removing { nonce, .. }) if Some(nonce) == self.remove_nonce.as_ref() => {}
            _ => anyhow::bail!("station removal state differs from retained operation"),
        }
        ensure!(
            recorded(journal, &self.removal(false)?)?,
            "station original removal intent changed"
        );
        self.verify(journal, station)?;
        if self.phase == Phase::Applied {
            ensure!(
                station.read()? == self.after,
                "station descriptor drift after removal intent"
            );
            self.phase = Phase::RestoreUncertain;
            station
                .write(&self.before)
                .context("station restoration outcome uncertain; preserve journal and handle")?;
            station.verify()?;
            ensure!(
                station.read()? == self.before,
                "station restoration readback mismatch; preserve removal intent"
            );
            self.phase = Phase::Restored;
        }
        ensure!(
            station.read()? == self.before,
            "station restored descriptor changed before receipt"
        );
        journal.append(self.removal(true)?)?;
        #[cfg(test)]
        self.fail(Fault::RemovedCommitted)?;
        ensure!(
            recorded(journal, &self.removal(true)?)?,
            "station exact restoration receipt missing"
        );
        Ok(())
    }
    #[cfg(test)]
    fn fail(&mut self, point: Fault) -> Result<()> {
        if self.fault == Some(point) {
            self.fault = None;
            anyhow::bail!("injected station post-commit failure");
        }
        Ok(())
    }
}

fn require_stopped(journal: &ResourceJournal<'_>) -> Result<()> {
    match &journal.authority {
        Authority::Stopped(_) => Ok(()),
        #[cfg(test)]
        Authority::FixtureStopped => Ok(()),
        _ => anyhow::bail!("station restoration requires stopped execution authority"),
    }
}

// The guard stays with the caller on every Result::Err. Dropping it only closes
// the independent reference; no hidden rollback or guessed journal retirement.
impl ResourceJournal<'_> {
    pub(crate) fn apply_window_station_grant(
        &self,
        guard: &mut PreparedWindowStationGrant,
    ) -> Result<()> {
        guard.operation.apply(self, &mut guard.station)
    }
    pub(crate) fn record_window_station_grant(
        &self,
        guard: &mut PreparedWindowStationGrant,
    ) -> Result<()> {
        guard.operation.record(self, &guard.station)
    }
    pub(crate) fn restore_window_station_grant(
        &self,
        guard: &mut PreparedWindowStationGrant,
    ) -> Result<()> {
        guard.operation.restore(self, &mut guard.station)
    }
}

impl StationAccess for Station {
    fn verify(&self) -> Result<()> {
        Station::verify(self, "WinSta0")
    }
    fn read(&self) -> Result<Snapshot> {
        Station::read(self)
    }
    fn write(&mut self, wanted: &Snapshot) -> Result<()> {
        // No named re-open, label/owner/group write or hierarchy propagation.
        // Exact intent and before comparison are the Operation's prerequisites.
        crate::acl::object_grants::set_native_dacl(HANDLE(self.0.0), wanted)
    }
}

#[cfg(test)]
#[path = "account_resource_station_lifecycle_tests.rs"]
mod tests;

#[cfg(test)]
#[path = "account_resource_station_host_tests.rs"]
mod disposable;
