//! Original runner image/path observations, never a launch or recovery receipt.
//! Production selects only the current helper image. Every ancestor is pinned
//! without delete sharing; the image additionally denies write sharing. No path
//! from a peer, journal or environment can select the executable. The trusted
//! launcher must prepare this guard BEFORE birth and keep it through termination.
//! This module cannot prove that ordering, publisher trust, loaded-DLL policy or
//! absence of process injection. The account lifecycle gate remains closed.
#![allow(dead_code)]

use super::fs::{self, Pinned, Policy};
use crate::{
    account_install::{AccountMode, ReadyInstallation},
    util::{OwnedHandle, OwnedSd},
};
use anyhow::{Context, Result, ensure};
use std::path::Path;
use windows::Win32::{
    Foundation::{
        DUPLICATE_SAME_ACCESS, DuplicateHandle, GetHandleInformation, HANDLE, HANDLE_FLAG_INHERIT,
    },
    Globalization::{CSTR_EQUAL, CompareStringOrdinal},
    Security::{GetSecurityDescriptorControl, GetSecurityDescriptorOwner, PSID},
    System::Threading::{GetCurrentProcess, PROCESS_NAME_WIN32, QueryFullProcessImageNameW},
};

#[derive(PartialEq, Eq)]
struct Identity {
    installation: String,
    owner: String,
    offline: String,
    online: String,
    group: String,
}

impl Identity {
    fn installed(ready: &ReadyInstallation) -> Result<Self> {
        let offline = ready.binding(AccountMode::Offline)?;
        let online = ready.binding(AccountMode::Online)?;
        offline.validate()?;
        online.validate()?;
        ensure!(
            offline.installation_id == online.installation_id
                && offline.owner_sid == online.owner_sid
                && offline.group_sid == online.group_sid
                && offline.account_sid != online.account_sid,
            "runner image installation identities differ"
        );
        Ok(Self {
            installation: offline.installation_id,
            owner: offline.owner_sid,
            offline: offline.account_sid,
            online: online.account_sid,
            group: offline.group_sid,
        })
    }
    fn selected(binding: &crate::account_install::AccountBinding) -> Result<Self> {
        binding.validate()?;
        let offline = crate::user::local_user_sid(crate::account_install::OFFLINE_USER)?
            .context("offline identity missing")?;
        let online = crate::user::local_user_sid(crate::account_install::ONLINE_USER)?
            .context("online identity missing")?;
        let group = crate::user::local_group_sid(crate::account_install::ACCOUNT_GROUP)?
            .context("account group missing")?;
        ensure!(
            offline != online
                && group == binding.group_sid
                && binding.account_sid
                    == match binding.mode {
                        AccountMode::Offline => &offline,
                        AccountMode::Online => &online,
                    }
                    .as_str(),
            "bootstrap image selected native identities differ"
        );
        Ok(Identity {
            installation: binding.installation_id.clone(),
            owner: binding.owner_sid.clone(),
            offline,
            online,
            group,
        })
    }
    fn policy(&self) -> Policy {
        Policy {
            owner: self.owner.clone(),
            offline: self.offline.clone(),
            online: self.online.clone(),
            group: self.group.clone(),
        }
    }
}

#[derive(PartialEq, Eq)]
struct SecurityShape {
    owner: String,
    control: u16,
    aces: Vec<Vec<u8>>,
}

/// Borrow this through peer authentication. It does not expose handles or accept
/// a caller-selected application name. Drop only releases its original pins.
pub(crate) struct PinnedRunnerImage {
    identity: Identity,
    parents: Vec<Pinned>,
    image: Pinned,
    security: Vec<SecurityShape>,
    #[cfg(test)]
    fixture_ancestors: bool,
}

impl PinnedRunnerImage {
    /// Receiver-side image pin for the trusted host bootstrap. The selected
    /// binding is data from its original authenticated broker, never readiness
    /// or credential authority. Resolve BOTH compiled account names and group
    /// natively before applying the same complete ancestor/leaf policy.
    pub(crate) fn prepare_bootstrap_selection(
        binding: &crate::account_install::AccountBinding,
    ) -> Result<Self> {
        crate::account_impersonation::require_process_identity()?;
        binding.validate()?;
        ensure!(
            binding.owner_sid == crate::sid::current_user_sid()?,
            "bootstrap image owner differs"
        );
        let identity = Identity::selected(binding)?;
        let path = std::env::current_exe()?;
        let parent = path.parent().context("bootstrap image has no parent")?;
        let parents = fs::pin_session_path(parent, &identity.policy())?;
        Self::finish(identity, parents, &path, false)
    }
    /// Identity data for a durable plan, never a substitute for retaining this
    /// guard and repeating native admission at the actual process birth.
    pub(crate) fn file_identity(&self) -> Result<[u8; 24]> {
        self.application()?;
        Ok(self.image.id.as_bytes())
    }
    /// Only the typed native birth adapter consumes this path while borrowing
    /// the guard. It still owns process creation and must check the result.
    pub(crate) fn application(&self) -> Result<&Path> {
        self.verify()?;
        ensure!(
            self.image.path == std::env::current_exe()?,
            "bootstrap image is not this helper"
        );
        Ok(&self.image.path)
    }
    pub(crate) fn prepare(ready: &ReadyInstallation) -> Result<Self> {
        crate::account_impersonation::require_process_identity()?;
        let identity = Identity::installed(ready)?;
        ensure!(
            identity.owner == crate::sid::current_user_sid()?,
            "runner image broker owner differs"
        );
        let path = std::env::current_exe().context("query current helper image")?;
        let parent = path.parent().context("runner image has no parent")?;
        let parents = fs::pin_session_path(parent, &identity.policy())?;
        Self::finish(identity, parents, &path, false)
    }

    fn finish(
        identity: Identity,
        parents: Vec<Pinned>,
        path: &Path,
        fixture: bool,
    ) -> Result<Self> {
        ensure!(
            parents.last().map(|p| p.path.as_path()) == path.parent(),
            "runner image parent differs"
        );
        // Full chain already pinned. OPEN_REPARSE_POINT and final-name checking
        // reject a leaf alias rather than following it; no write/delete sharing.
        let image = Pinned::open(path, false, true)?;
        let mut security = Vec::with_capacity(parents.len() + 1);
        for pin in parents.iter().chain(std::iter::once(&image)) {
            security.push(shape(&pin.security()?)?);
        }
        #[cfg(not(test))]
        ensure!(!fixture, "fixture admission unavailable");
        let guard = Self {
            identity,
            parents,
            image,
            security,
            #[cfg(test)]
            fixture_ancestors: fixture,
        };
        guard.verify()?;
        Ok(guard)
    }

    pub(crate) fn require_installation(&self, ready: &ReadyInstallation) -> Result<()> {
        ensure!(
            self.identity == Identity::installed(ready)?,
            "runner image installation changed"
        );
        self.verify()
    }

    pub(crate) fn verify_process(&self, process: HANDLE) -> Result<()> {
        self.verify()?;
        verify_process_path(process, &self.image.path)?;
        self.verify()
    }

    fn verify(&self) -> Result<()> {
        crate::account_impersonation::require_process_identity()?;
        ensure!(
            self.identity.owner == crate::sid::current_user_sid()?,
            "runner image broker identity changed"
        );
        #[cfg(test)]
        let admit_ancestors = !self.fixture_ancestors;
        #[cfg(not(test))]
        let admit_ancestors = true;
        verify_pins(
            &self.identity,
            &self.parents,
            &self.image,
            &self.security,
            admit_ancestors,
        )
    }

    #[cfg(test)]
    pub(crate) fn fixture_requires_full_ancestry(&self) -> bool {
        !self.fixture_ancestors
    }

    #[cfg(test)]
    pub(crate) fn fixture_installed(ready: &ReadyInstallation) -> Result<Self> {
        let identity = Identity::installed(ready)?;
        let path = std::env::current_exe()?;
        let parents = fs::pin_fixture_path(
            path.parent().context("missing fixture parent")?,
            &identity.policy(),
        )?;
        Self::finish(identity, parents, &path, true)
    }

    #[cfg(test)]
    pub(crate) fn fixture(path: &Path) -> Result<Self> {
        let identity = Identity {
            installation: "1".repeat(32),
            owner: crate::sid::current_user_sid()?,
            offline: "S-1-5-21-1-2-3-1001".into(),
            online: "S-1-5-21-1-2-3-1002".into(),
            group: "S-1-5-21-1-2-3-1003".into(),
        };
        let parents = fs::pin_fixture_path(
            path.parent().context("missing fixture parent")?,
            &identity.policy(),
        )?;
        Self::finish(identity, parents, path, true)
    }
}

// Shared read-only pin policy. Actor admission remains separate in each guard.
fn verify_pins(
    identity: &Identity,
    parents: &[Pinned],
    image: &Pinned,
    expected_security: &[SecurityShape],
    admit_ancestors: bool,
) -> Result<()> {
    ensure!(
        expected_security.len() == parents.len() + 1 && !parents.is_empty(),
        "runner image pins missing"
    );
    if admit_ancestors {
        fs::validate_pinned_ancestors(parents, &identity.policy())?;
    }
    for (index, pin) in parents.iter().chain(std::iter::once(image)).enumerate() {
        retained(pin, index < parents.len())?;
        let security = pin.security()?;
        ensure!(
            shape(&security)? == expected_security[index],
            "runner image path security changed"
        );
        if index == parents.len() {
            // Read/execute ACEs are permitted. An untrusted write/append,
            // delete, owner/DACL/attribute mutation grant is never admitted.
            fs::validate_ancestor(&security, &identity.owner, true)
                .context("runner image permits untrusted mutation")?;
        }
    }
    Ok(())
}

fn retained(pin: &Pinned, directory: bool) -> Result<()> {
    let mut flags = 0;
    unsafe {
        GetHandleInformation(pin.handle.raw(), &mut flags)?;
    }
    ensure!(
        flags & HANDLE_FLAG_INHERIT.0 == 0,
        "runner image pin is inheritable"
    );
    let mut raw = HANDLE::default();
    unsafe {
        DuplicateHandle(
            GetCurrentProcess(),
            pin.handle.raw(),
            GetCurrentProcess(),
            &mut raw,
            0,
            false,
            DUPLICATE_SAME_ACCESS,
        )?;
    }
    // Revalidate the original object, not a reopened name. The temporary file
    // reference is dropped after its ID/type/link count/final path are checked.
    let observed = Pinned::from_handle(OwnedHandle(raw), &pin.path, directory)?;
    ensure!(observed.id == pin.id, "runner image path identity changed");
    Ok(())
}

fn shape(sd: &OwnedSd) -> Result<SecurityShape> {
    let mut owner = PSID::default();
    let mut defaulted = false.into();
    let mut control = 0;
    let mut revision = 0;
    unsafe {
        GetSecurityDescriptorOwner(sd.ptr, &mut owner, &mut defaulted)?;
        GetSecurityDescriptorControl(sd.ptr, &mut control, &mut revision)?;
    }
    ensure!(!owner.0.is_null(), "runner image security owner missing");
    Ok(SecurityShape {
        owner: crate::sid::psid_to_string(owner)?,
        control: control & 0x150c,
        aces: fs::ace_bytes(sd)?,
    })
}

#[path = "account_image_observer.rs"]
pub(crate) mod account;

#[cfg(test)]
#[path = "account_image_tests.rs"]
mod tests;

fn verify_process_path(process: HANDLE, path: &Path) -> Result<()> {
    let mut name = vec![0u16; 32768];
    let mut length = name.len() as u32;
    unsafe {
        QueryFullProcessImageNameW(
            process,
            PROCESS_NAME_WIN32,
            windows::core::PWSTR(name.as_mut_ptr()),
            &mut length,
        )
    }
    .context("query original runner process image")?;
    ensure!(
        length > 0 && (length as usize) < name.len(),
        "invalid runner image query length"
    );
    name.truncate(length as usize);
    let actual = String::from_utf16(&name).context("runner image is not Unicode")?;
    fs::extended_path(Path::new(&actual))?;
    let expected: Vec<u16> = path
        .to_str()
        .context("pinned image is not Unicode")?
        .encode_utf16()
        .collect();
    ensure!(
        unsafe { CompareStringOrdinal(&name, &expected, true) } == CSTR_EQUAL,
        "original process does not run the pinned helper image"
    );
    Ok(())
}
