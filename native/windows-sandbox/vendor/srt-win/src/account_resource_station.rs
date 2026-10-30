//! Retained preparation and standalone apply/restore for the disabled candidate.
//! No activation/launch caller. The lifecycle requires durable exact intents,
//! original handle and actor, readback and stopped restoration; native station
//! writes remain unqualified and must run only on disposable hosted fixtures.
use super::empty_job::verify_actor;
use super::*;
use crate::acl::object_grants::Snapshot;
use crate::util::{pcwstr, wstr};
use windows::Win32::Foundation::{CompareObjectHandles, HANDLE};
use windows::Win32::System::StationsAndDesktops::{
    CloseWindowStation, GetProcessWindowStation, GetUserObjectInformationW, HWINSTA,
    OpenWindowStationW, UOI_FLAGS, UOI_NAME, UOI_TYPE, USEROBJECTFLAGS,
};

const READ_CONTROL: u32 = 0x20000;
const WRITE_DAC: u32 = 0x40000;
const READ_ATTRIBUTES: u32 = 0x2;
// Same attach policy as the existing experimental namespace bootstrap. No
// WRITE_DAC/WRITE_OWNER/DELETE and no inheritance to any desktop is added.
const PACKAGE_ATTACH_MASK: u32 = 0x2037f;

/// No Clone, Deserialize, handle escape or reconstruction by saved name.
pub(crate) struct PreparedWindowStationGrant {
    operation: lifecycle::Operation,
    station: Station,
}

impl ResourceJournal<'_> {
    /// Preparation only. Even successful admission creates no resource receipt
    /// and grants no prerequisite or authority to bootstrap/launch a process.
    pub(crate) fn prepare_window_station_grant(&self) -> Result<PreparedWindowStationGrant> {
        require_live(self)?;
        verify_actor(&self.identity)?;
        ensure!(
            self.replay()?.states.is_empty(),
            "station preparation requires an untouched resource plan"
        );
        let station = Station::open_current("WinSta0", READ_CONTROL | WRITE_DAC | READ_ATTRIBUTES)?;
        let before = station.read()?;
        let after = proposed(&before, &self.identity.execution.identity.package_sid)?;
        let guard = PreparedWindowStationGrant {
            operation: lifecycle::Operation::new(self.identity.clone(), before, after),
            station,
        };
        guard.verify(self)?;
        Ok(guard)
    }
}

impl PreparedWindowStationGrant {
    /// Recheck the original broker, handle and complete raw owner/DACL shape.
    /// Comparisons observe drift; they do not serialize concurrent host edits.
    fn verify(&self, journal: &ResourceJournal<'_>) -> Result<()> {
        self.operation.verify_prepared(journal, &self.station)
    }
}

fn require_live(journal: &ResourceJournal<'_>) -> Result<()> {
    match &journal.authority {
        Authority::Live(_) => Ok(()),
        #[cfg(test)]
        Authority::FixtureLive => Ok(()),
        _ => anyhow::bail!("station preparation requires live execution authority"),
    }
}

pub(in crate::account_state::execution) fn proposed(
    before: &Snapshot,
    package: &str,
) -> Result<Snapshot> {
    before.acl()?;
    crate::sid::LocalPsid::from_string(&before.owner)?;
    ensure!(
        before.control & !0x150c == 0 && before.control & 4 != 0 && before.control & 0x108 == 0,
        "station DACL control cannot be preserved by this policy"
    );
    let sid = crate::sid::LocalPsid::from_string(package)?;
    ensure!(
        package.starts_with("S-1-15-2-"),
        "station requires an execution package SID"
    );
    let mut inherited = false;
    for ace in &before.aces {
        ensure!(
            !ace.windows(sid.as_bytes().len())
                .any(|part| part == sid.as_bytes()),
            "execution package already occurs in station DACL; refusing adoption"
        );
        if ace[1] & 0x10 != 0 {
            inherited = true;
            ensure!(
                ace[0] == 0,
                "inherited deny or opaque ACE prevents precedence-preserving station grant"
            );
        } else {
            ensure!(
                !inherited,
                "station explicit/inherited ACE order is unsupported"
            );
        }
    }
    // ACCESS_ALLOWED_ACE: type, zero inheritance flags, size, mask, validated SID.
    let size = u16::try_from(8 + sid.as_bytes().len())?;
    let mut addition = vec![0, 0];
    addition.extend_from_slice(&size.to_le_bytes());
    addition.extend_from_slice(&PACKAGE_ATTACH_MASK.to_le_bytes());
    addition.extend_from_slice(sid.as_bytes());
    let mut after = before.clone();
    let at = after
        .aces
        .iter()
        .position(|ace| ace[1] & 0x10 != 0)
        .unwrap_or(after.aces.len());
    after.aces.insert(at, addition);
    after.acl()?;
    // Bound a future durable intent before any effect can be implemented.
    ensure!(
        serde_json::to_vec(before)?.len() <= 65536 && serde_json::to_vec(&after)?.len() <= 65536,
        "station snapshot exceeds resource intent bounds"
    );
    Ok(after)
}

pub(in crate::account_state::execution) struct Station(HWINSTA);
impl Drop for Station {
    fn drop(&mut self) {
        // Only our independently opened reference, never GetProcessWindowStation.
        // No ACL rollback or journal retirement is hidden in Drop.
        let _ = unsafe { CloseWindowStation(self.0) };
    }
}
impl Station {
    #[cfg(test)]
    pub(in crate::account_state::execution) fn fixture_snapshot_current() -> Result<Snapshot> {
        crate::account_impersonation::require_process_identity()?;
        // Observation only; this independently opened reference cannot write
        // the DACL and is closed before returning the snapshot.
        Self::open_current("WinSta0", READ_CONTROL | READ_ATTRIBUTES)?.read()
    }
    pub(in crate::account_state::execution) fn prepare(
        package: &str,
    ) -> Result<(Self, Snapshot, Snapshot)> {
        crate::account_impersonation::require_process_identity()?;
        let station = Self::open_current("WinSta0", READ_CONTROL | WRITE_DAC | READ_ATTRIBUTES)?;
        let before = station.read()?;
        let after = proposed(&before, package)?;
        Ok((station, before, after))
    }
    pub(in crate::account_state::execution) fn verify_current(&self) -> Result<()> {
        crate::account_impersonation::require_process_identity()?;
        self.verify("WinSta0")
    }
    pub(in crate::account_state::execution) fn write_exact(
        &mut self,
        expected: &Snapshot,
        wanted: &Snapshot,
    ) -> Result<()> {
        self.verify_current()?;
        ensure!(
            self.read()? == *expected,
            "station descriptor changed before exact write"
        );
        crate::acl::object_grants::set_native_dacl(HANDLE(self.0.0), wanted)?;
        self.verify_current()?;
        ensure!(
            self.read()? == *wanted,
            "station exact write readback differs"
        );
        Ok(())
    }
    pub(in crate::account_state::execution) fn open_current(
        expected: &str,
        access: u32,
    ) -> Result<Self> {
        let current = unsafe { GetProcessWindowStation() }?;
        ensure!(
            object_text(HANDLE(current.0), UOI_NAME)? == expected,
            "unexpected broker window station"
        );
        let opened = unsafe { OpenWindowStationW(pcwstr(&wstr(expected)), false, access) }?;
        // Never wrap or close a borrowed process-station handle.
        ensure!(
            opened.0 != current.0,
            "window station open returned the borrowed process handle"
        );
        let station = Self(opened);
        station.verify(expected)?;
        Ok(station)
    }
    pub(in crate::account_state::execution) fn raw(&self) -> HANDLE {
        HANDLE(self.0.0)
    }
    fn verify(&self, expected: &str) -> Result<()> {
        let handle = HANDLE(self.0.0);
        let current = unsafe { GetProcessWindowStation() }?;
        ensure!(
            unsafe { CompareObjectHandles(handle, HANDLE(current.0)) }.as_bool(),
            "retained station is no longer the broker's current object"
        );
        ensure!(
            object_text(handle, UOI_NAME)? == expected
                && object_text(handle, UOI_TYPE)? == "WindowStation",
            "retained station name or type changed"
        );
        let mut flags = USEROBJECTFLAGS::default();
        let mut size = 0;
        unsafe {
            GetUserObjectInformationW(
                handle,
                UOI_FLAGS,
                Some((&mut flags as *mut USEROBJECTFLAGS).cast()),
                std::mem::size_of::<USEROBJECTFLAGS>() as u32,
                Some(&mut size),
            )
        }?;
        ensure!(
            size as usize == std::mem::size_of::<USEROBJECTFLAGS>() && !flags.fInherit.as_bool(),
            "retained station handle is inheritable or has invalid flags"
        );
        Ok(())
    }
    pub(in crate::account_state::execution) fn read(&self) -> Result<Snapshot> {
        // Use the existing bounded raw native query/parser, not Win32's ACL
        // reconstruction or an SDDL roundtrip. No mutation API is invoked.
        Snapshot::read(HANDLE(self.0.0))
    }
}
fn object_text(
    handle: HANDLE,
    index: windows::Win32::System::StationsAndDesktops::USER_OBJECT_INFORMATION_INDEX,
) -> Result<String> {
    let mut buffer = [0u16; 256];
    let mut bytes = 0;
    unsafe {
        GetUserObjectInformationW(
            handle,
            index,
            Some(buffer.as_mut_ptr().cast()),
            std::mem::size_of_val(&buffer) as u32,
            Some(&mut bytes),
        )
    }?;
    ensure!(
        bytes >= 2 && bytes as usize <= std::mem::size_of_val(&buffer) && bytes % 2 == 0,
        "invalid bounded station information size"
    );
    let length = bytes as usize / 2;
    ensure!(
        buffer[length - 1] == 0 && !buffer[..length - 1].contains(&0),
        "invalid station information string"
    );
    Ok(String::from_utf16(&buffer[..length - 1])?)
}

#[cfg(test)]
#[path = "account_resource_station_tests.rs"]
mod tests;

#[path = "account_resource_station_lifecycle.rs"]
mod lifecycle;
