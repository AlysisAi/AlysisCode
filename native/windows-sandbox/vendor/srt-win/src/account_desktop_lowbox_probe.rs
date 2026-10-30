//! Fixed-child desktop diagnostic. The caller must already authenticate
//! the complete original lowbox request and retain its original desktop/process.
//! Data here grants no launch, desktop ownership, restoration or cleanup right.
#![cfg(test)]

use super::super::private_station_fixture::{security, text};
use super::*;
use windows::{
    Wdk::Storage::FileSystem::NtSetSecurityObject,
    Win32::{Security::*, System::StationsAndDesktops::*},
};

const ALLOWED: u32 = 0x20081; // RC + READOBJECTS + WRITEOBJECTS
const MUTATION_OPENS: [u32; 3] = [0x40081, 0x80081, 0xc0081];
const NATIVE_DENIED: i32 = 0xc0000022u32 as i32;
const MAX_PROOF_BYTES: usize = 12288;
const INFO: OBJECT_SECURITY_INFORMATION = OBJECT_SECURITY_INFORMATION(
    OWNER_SECURITY_INFORMATION.0 | DACL_SECURITY_INFORMATION.0 | LABEL_SECURITY_INFORMATION.0,
);

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum DesktopStage {
    Actor,
    Attached,
    AllowedOpen,
    DeniedOpen,
    SetUserDacl,
    SetUserOwner,
    NtSetDacl,
    NtSetOwner,
    AttachedSetUserDacl,
    AttachedSetUserOwner,
    AttachedNtSetDacl,
    AttachedNtSetOwner,
    Close,
    Complete,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct DesktopProof {
    pub(super) name: String,
    pub(super) descriptor: String,
    pub(super) owner_sid: String,
    pub(super) passed: bool,
    pub(super) stage: DesktopStage,
    pub(super) hresult: Option<i32>,
    pub(super) actor_verified: bool,
    pub(super) attached_name_verified: bool,
    pub(super) positive_open: bool,
    pub(super) attached_same_object: bool,
    pub(super) descriptor_matches: bool,
    pub(super) open_denials: Vec<(u32, i32)>,
    pub(super) set_user_dacl: Option<i32>,
    pub(super) set_user_owner: Option<i32>,
    pub(super) nt_set_dacl: Option<i32>,
    pub(super) nt_set_owner: Option<i32>,
    pub(super) attached_set_user_dacl: Option<i32>,
    pub(super) attached_set_user_owner: Option<i32>,
    pub(super) attached_nt_set_dacl: Option<i32>,
    pub(super) attached_nt_set_owner: Option<i32>,
    pub(super) fresh_closed: bool,
    pub(super) close_hresult: Option<i32>,
}

// Pure shape checks. Native identity and the complete original request are
// independent requirements; an accepted serialized proof cannot create either.
fn subject(name: &str, descriptor: &str, owner: &str) -> Result<()> {
    let nonce = name
        .strip_prefix("alysis-station-lowbox-")
        .context("desktop fixture name differs")?;
    ensure!(
        nonce.len() == 32
            && nonce
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        "desktop fixture name is not one fixed component"
    );
    ensure!(
        !descriptor.is_empty() && descriptor.len() <= 8192 && !descriptor.contains('\0'),
        "desktop descriptor exceeds bounds"
    );
    let suffix = owner
        .strip_prefix("S-1-5-21-")
        .context("desktop owner must be a dedicated account SID")?;
    let parts: Vec<_> = suffix.split('-').collect();
    ensure!(
        owner.len() <= 64
            && parts.len() == 4
            && parts
                .iter()
                .all(|p| p.parse::<u32>().ok().is_some_and(|n| n.to_string() == *p)),
        "desktop owner SID is not canonical"
    );
    Ok(())
}

impl DesktopProof {
    fn new(name: &str, descriptor: &str, owner: &str) -> Result<Self> {
        subject(name, descriptor, owner)?;
        Ok(Self {
            name: name.into(),
            descriptor: descriptor.into(),
            owner_sid: owner.into(),
            passed: false,
            stage: DesktopStage::Actor,
            hresult: None,
            actor_verified: false,
            attached_name_verified: false,
            positive_open: false,
            attached_same_object: false,
            descriptor_matches: false,
            open_denials: Vec::new(),
            set_user_dacl: None,
            set_user_owner: None,
            nt_set_dacl: None,
            nt_set_owner: None,
            attached_set_user_dacl: None,
            attached_set_user_owner: None,
            attached_nt_set_dacl: None,
            attached_nt_set_owner: None,
            fresh_closed: false,
            close_hresult: None,
        })
    }

    pub(super) fn require(&self) -> Result<()> {
        subject(&self.name, &self.descriptor, &self.owner_sid)?;
        ensure!(
            self.open_denials.len() <= 3,
            "desktop denial inventory exceeds bound"
        );
        for (index, (rights, _)) in self.open_denials.iter().enumerate() {
            ensure!(
                *rights == MUTATION_OPENS[index],
                "desktop denial rights/order differs"
            );
        }
        ensure!(
            (!self.fresh_closed || (self.positive_open && self.close_hresult.is_none()))
                && (self.close_hresult.is_none() || (self.positive_open && !self.fresh_closed)),
            "desktop fresh close evidence contradicts ownership"
        );
        if self.passed {
            let denied = ERROR_ACCESS_DENIED.to_hresult().0;
            ensure!(
                self.stage == DesktopStage::Complete
                    && self.hresult.is_none()
                    && self.actor_verified
                    && self.attached_name_verified
                    && self.positive_open
                    && self.attached_same_object
                    && self.descriptor_matches
                    && self.fresh_closed
                    && self.close_hresult.is_none()
                    && self.open_denials == MUTATION_OPENS.map(|rights| (rights, denied))
                    && self.set_user_dacl == Some(denied)
                    && self.set_user_owner == Some(denied)
                    && self.nt_set_dacl == Some(NATIVE_DENIED)
                    && self.nt_set_owner == Some(NATIVE_DENIED)
                    && self.attached_set_user_dacl == Some(denied)
                    && self.attached_set_user_owner == Some(denied)
                    && self.attached_nt_set_dacl == Some(NATIVE_DENIED)
                    && self.attached_nt_set_owner == Some(NATIVE_DENIED),
                "desktop mutation denial proof incomplete"
            );
        } else {
            ensure!(
                self.stage != DesktopStage::Complete,
                "failed desktop proof claims completion"
            );
        }
        ensure!(
            serde_json::to_vec(self)?.len() <= MAX_PROOF_BYTES,
            "desktop proof exceeds byte bound"
        );
        Ok(())
    }

    /// Call this against the descriptor and name from the exact original
    /// parent-owned desktop, not values reconstructed from this proof.
    pub(super) fn require_subject(&self, name: &str, descriptor: &str, owner: &str) -> Result<()> {
        self.require()?;
        subject(name, descriptor, owner)?;
        ensure!(
            self.name == name && self.descriptor == descriptor && self.owner_sid == owner,
            "desktop proof differs from original subject"
        );
        Ok(())
    }
}

struct FreshDesktop(Option<HDESK>);
impl FreshDesktop {
    fn close(&mut self) -> Result<()> {
        let handle = self.0.take().context("fresh desktop close repeated")?;
        unsafe {
            CloseDesktop(handle)?;
        }
        Ok(())
    }
}
impl Drop for FreshDesktop {
    fn drop(&mut self) {
        if let Some(handle) = self.0.take() {
            let _ = unsafe { CloseDesktop(handle) };
        }
    }
}

fn actor(owner: &str) -> Result<()> {
    crate::account_impersonation::require_process_identity()?;
    ensure!(
        crate::sid::current_user_sid()? == owner,
        "desktop mutation actor is not expected current owner"
    );
    let mut raw = HANDLE::default();
    unsafe {
        OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut raw)?;
    }
    let token = OwnedHandle(raw);
    noninheritable(token.raw())?;
    for (class, expected) in [(TokenType, TokenPrimary.0 as u32), (TokenIsAppContainer, 1)] {
        let mut value = 0u32;
        let mut bytes = 0;
        unsafe {
            GetTokenInformation(
                token.raw(),
                class,
                Some((&mut value as *mut u32).cast()),
                size_of::<u32>() as u32,
                &mut bytes,
            )?;
        }
        ensure!(
            bytes as usize == size_of::<u32>() && value == expected,
            "desktop mutation diagnostic requires actual AppContainer primary actor"
        );
    }
    Ok(())
}

/// Only the authenticated fixed child calls this, after complete original
/// process/token/package/session/request checks. Expected text supplies no
/// native authority. No desktop is created, attached, restored or adopted.
pub(super) fn probe(
    expected_name: &str,
    expected_descriptor: &str,
    owner_sid: &str,
) -> Result<DesktopProof> {
    let mut proof = DesktopProof::new(expected_name, expected_descriptor, owner_sid)?;
    let mut fresh = None::<FreshDesktop>;
    let result = (|| -> Result<()> {
        actor(owner_sid)?;
        proof.actor_verified = true;
        proof.stage = DesktopStage::Attached;
        // Borrow only: never construct an owned guard or call CloseDesktop on
        // the thread's system attachment, on success or on any failure path.
        let attached = unsafe { GetThreadDesktop(GetCurrentThreadId())? };
        noninheritable(HANDLE(attached.0))?;
        ensure!(
            text(HANDLE(attached.0), UOI_TYPE)? == "Desktop"
                && text(HANDLE(attached.0), UOI_NAME)? == expected_name,
            "original attached desktop name/type differs"
        );
        proof.attached_name_verified = true;
        proof.stage = DesktopStage::AllowedOpen;
        let opened = unsafe {
            OpenDesktopW(
                pcwstr(&wstr(expected_name)),
                DESKTOP_CONTROL_FLAGS(0),
                false,
                ALLOWED,
            )?
        };
        // If Windows ever returns the borrowed handle value, refuse rather
        // than guess that it is independently closeable ownership.
        ensure!(
            opened != attached,
            "fresh desktop aliases borrowed handle value"
        );
        fresh = Some(FreshDesktop(Some(opened)));
        proof.positive_open = true;
        noninheritable(HANDLE(opened.0))?;
        proof.attached_same_object =
            unsafe { CompareObjectHandles(HANDLE(attached.0), HANDLE(opened.0)).as_bool() };
        ensure!(
            proof.attached_same_object,
            "fresh desktop is not original attachment"
        );
        proof.descriptor_matches = security(HANDLE(opened.0), INFO)?.1 == expected_descriptor;
        ensure!(
            proof.descriptor_matches,
            "original desktop descriptor differs"
        );
        proof.stage = DesktopStage::DeniedOpen;
        for rights in MUTATION_OPENS {
            match unsafe {
                OpenDesktopW(
                    pcwstr(&wstr(expected_name)),
                    DESKTOP_CONTROL_FLAGS(0),
                    false,
                    rights,
                )
            } {
                Ok(unexpected) => {
                    proof.open_denials.push((rights, 0));
                    ensure!(
                        unexpected != attached && unexpected != opened,
                        "unexpected desktop mutation open aliases retained handle"
                    );
                    FreshDesktop(Some(unexpected)).close()?;
                    anyhow::bail!("fresh desktop mutation open unexpectedly succeeded");
                }
                Err(error) => {
                    proof.open_denials.push((rights, error.code().0));
                    ensure!(
                        error.code() == ERROR_ACCESS_DENIED.to_hresult(),
                        "fresh desktop mutation open was not access-denied"
                    );
                }
            }
        }
        // A valid CURRENT owner avoids ERROR_INVALID_OWNER qualifying as a
        // denial. A DACL attempt deliberately differs from the original ACL;
        // any unexpected success is failure and must be preserved by parent.
        let mutation = OwnedSd::from_sddl(&format!("O:{owner_sid}D:P(A;;0xf01ff;;;SY)"))?;
        let denied = ERROR_ACCESS_DENIED.to_hresult().0;
        for (attached_path, handle) in [(false, opened), (true, attached)] {
            proof.stage = if attached_path {
                DesktopStage::AttachedSetUserDacl
            } else {
                DesktopStage::SetUserDacl
            };
            let code = unsafe {
                SetUserObjectSecurity(HANDLE(handle.0), &DACL_SECURITY_INFORMATION, mutation.ptr)
            }
            .err()
            .map_or(0, |e| e.code().0);
            if attached_path {
                proof.attached_set_user_dacl = Some(code);
            } else {
                proof.set_user_dacl = Some(code);
            }
            ensure!(
                code == denied,
                "desktop USER DACL mutation was not access-denied"
            );

            proof.stage = if attached_path {
                DesktopStage::AttachedSetUserOwner
            } else {
                DesktopStage::SetUserOwner
            };
            let code = unsafe {
                SetUserObjectSecurity(HANDLE(handle.0), &OWNER_SECURITY_INFORMATION, mutation.ptr)
            }
            .err()
            .map_or(0, |e| e.code().0);
            if attached_path {
                proof.attached_set_user_owner = Some(code);
            } else {
                proof.set_user_owner = Some(code);
            }
            ensure!(
                code == denied,
                "desktop USER owner mutation was not access-denied"
            );

            proof.stage = if attached_path {
                DesktopStage::AttachedNtSetDacl
            } else {
                DesktopStage::NtSetDacl
            };
            let code = unsafe {
                NtSetSecurityObject(HANDLE(handle.0), DACL_SECURITY_INFORMATION.0, mutation.ptr)
            }
            .0;
            if attached_path {
                proof.attached_nt_set_dacl = Some(code);
            } else {
                proof.nt_set_dacl = Some(code);
            }
            ensure!(
                code == NATIVE_DENIED,
                "desktop native DACL mutation was not access-denied"
            );

            proof.stage = if attached_path {
                DesktopStage::AttachedNtSetOwner
            } else {
                DesktopStage::NtSetOwner
            };
            let code = unsafe {
                NtSetSecurityObject(HANDLE(handle.0), OWNER_SECURITY_INFORMATION.0, mutation.ptr)
            }
            .0;
            if attached_path {
                proof.attached_nt_set_owner = Some(code);
            } else {
                proof.nt_set_owner = Some(code);
            }
            ensure!(
                code == NATIVE_DENIED,
                "desktop native owner mutation was not access-denied"
            );
        }
        Ok(())
    })();
    let mut failure = result.err();
    if failure.is_none() {
        proof.stage = DesktopStage::Close;
    }
    if let Some(mut owned) = fresh {
        match owned.close() {
            Ok(()) => proof.fresh_closed = true,
            Err(error) => {
                proof.close_hresult = native_code(&error);
                if failure.is_none() {
                    failure = Some(error);
                }
            }
        }
    }
    if let Some(error) = failure {
        proof.hresult = native_code(&error);
    } else {
        proof.passed = true;
        proof.stage = DesktopStage::Complete;
    }
    proof.require_subject(expected_name, expected_descriptor, owner_sid)?;
    Ok(proof)
}

#[cfg(test)]
mod tests {
    use super::*;
    const NAME: &str = "alysis-station-lowbox-0123456789abcdef0123456789abcdef";
    const OWNER: &str = "S-1-5-21-1-2-3-1004";
    const DESCRIPTOR: &str = "O:S-1-5-21-1-2-3-1004D:P(A;;RC;;;OW)S:(ML;;NW;;;LW)";
    fn success() -> DesktopProof {
        let deny = ERROR_ACCESS_DENIED.to_hresult().0;
        let mut p = DesktopProof::new(NAME, DESCRIPTOR, OWNER).unwrap();
        p.passed = true;
        p.stage = DesktopStage::Complete;
        p.actor_verified = true;
        p.attached_name_verified = true;
        p.positive_open = true;
        p.attached_same_object = true;
        p.descriptor_matches = true;
        p.fresh_closed = true;
        p.open_denials = MUTATION_OPENS.map(|r| (r, deny)).to_vec();
        p.set_user_dacl = Some(deny);
        p.set_user_owner = Some(deny);
        p.nt_set_dacl = Some(NATIVE_DENIED);
        p.nt_set_owner = Some(NATIVE_DENIED);
        p.attached_set_user_dacl = Some(deny);
        p.attached_set_user_owner = Some(deny);
        p.attached_nt_set_dacl = Some(NATIVE_DENIED);
        p.attached_nt_set_owner = Some(NATIVE_DENIED);
        p
    }
    #[test]
    fn desktop_lowbox_complete_proof_round_trips_and_binds_exact_subject() {
        let bytes = serde_json::to_vec(&success()).unwrap();
        assert!(bytes.len() < MAX_PROOF_BYTES);
        let p: DesktopProof = serde_json::from_slice(&bytes).unwrap();
        p.require_subject(NAME, DESCRIPTOR, OWNER).unwrap();
        assert!(
            p.require_subject(&NAME.replace('0', "1"), DESCRIPTOR, OWNER)
                .is_err()
        );
        assert!(p.require_subject(NAME, "D:P", OWNER).is_err());
        assert!(
            p.require_subject(NAME, DESCRIPTOR, "S-1-5-21-1-2-3-1005")
                .is_err()
        );
    }
    #[test]
    fn desktop_lowbox_success_requires_every_observation_and_checked_close() {
        for case in 0..17 {
            let mut p = success();
            match case {
                0 => p.actor_verified = false,
                1 => p.attached_name_verified = false,
                2 => p.positive_open = false,
                3 => p.attached_same_object = false,
                4 => p.descriptor_matches = false,
                5 => p.fresh_closed = false,
                6 => {
                    p.open_denials.pop();
                }
                7 => p.set_user_dacl = None,
                8 => p.set_user_owner = None,
                9 => p.nt_set_dacl = None,
                10 => p.nt_set_owner = None,
                11 => p.attached_set_user_dacl = None,
                12 => p.attached_set_user_owner = None,
                13 => p.attached_nt_set_dacl = None,
                14 => p.attached_nt_set_owner = None,
                15 => p.close_hresult = Some(-1),
                _ => p.hresult = Some(-1),
            }
            assert!(p.require().is_err(), "missing observation {case}");
        }
    }
    #[test]
    fn desktop_lowbox_success_does_not_accept_unrelated_errors_or_wrong_open_rights() {
        for wrong in [
            0,
            ERROR_INVALID_OWNER.to_hresult().0,
            ERROR_INVALID_HANDLE.to_hresult().0,
            ERROR_INVALID_PARAMETER.to_hresult().0,
            ERROR_FILE_NOT_FOUND.to_hresult().0,
        ] {
            for case in 0..9 {
                let mut p = success();
                match case {
                    0 => p.open_denials[0].1 = wrong,
                    1 => p.set_user_dacl = Some(wrong),
                    2 => p.set_user_owner = Some(wrong),
                    3 => p.nt_set_dacl = Some(wrong),
                    4 => p.nt_set_owner = Some(wrong),
                    5 => p.attached_set_user_dacl = Some(wrong),
                    6 => p.attached_set_user_owner = Some(wrong),
                    7 => p.attached_nt_set_dacl = Some(wrong),
                    _ => p.attached_nt_set_owner = Some(wrong),
                }
                assert!(
                    p.require().is_err(),
                    "unrelated status {wrong} field {case}"
                );
            }
        }
        for wrong in [0x40000, 0x80000, 0xc0000] {
            let mut p = success();
            p.open_denials[0].0 = wrong;
            assert!(p.require().is_err());
        }
        let mut p = success();
        p.open_denials.swap(0, 1);
        assert!(p.require().is_err());
    }
    #[test]
    fn desktop_lowbox_partial_refusals_preserve_failure_without_becoming_success() {
        let mut p = DesktopProof::new(NAME, DESCRIPTOR, OWNER).unwrap();
        p.require().unwrap();
        p.actor_verified = true;
        p.attached_name_verified = true;
        p.positive_open = true;
        p.attached_same_object = true;
        p.descriptor_matches = true;
        p.stage = DesktopStage::DeniedOpen;
        p.open_denials
            .push((MUTATION_OPENS[0], ERROR_INVALID_PARAMETER.to_hresult().0));
        p.fresh_closed = true;
        p.require().unwrap();
        p.passed = true;
        assert!(p.require().is_err());
        p.passed = false;
        p.stage = DesktopStage::Complete;
        assert!(p.require().is_err());
        p.stage = DesktopStage::Close;
        p.fresh_closed = false;
        p.close_hresult = Some(ERROR_INVALID_HANDLE.to_hresult().0);
        p.require().unwrap();
        p.fresh_closed = true;
        assert!(p.require().is_err());
    }
    #[test]
    fn desktop_lowbox_codec_and_subject_refuse_aliases_unknown_and_oversized_data() {
        for name in [
            "Default",
            "WinSta0\\Default",
            "../alysis-station-lowbox-0123456789abcdef0123456789abcdef",
            "alysis-station-lowbox-0123456789ABCDEF0123456789ABCDEF",
            "alysis-station-lowbox-0",
        ] {
            assert!(DesktopProof::new(name, DESCRIPTOR, OWNER).is_err());
        }
        for owner in [
            "S-1-5-18",
            "S-1-5-21-1-2-3-01004",
            "S-1-5-21-1-2-3-4294967296",
            "S-1-5-21-1-2-3-4-5",
        ] {
            assert!(DesktopProof::new(NAME, DESCRIPTOR, owner).is_err());
        }
        assert!(DesktopProof::new(NAME, &"x".repeat(8193), OWNER).is_err());
        assert!(DesktopProof::new(NAME, "D:P\0", OWNER).is_err());
        let mut value = serde_json::to_value(success()).unwrap();
        value["skip_attached_check"] = serde_json::json!(true);
        assert!(serde_json::from_value::<DesktopProof>(value).is_err());
        let mut value = serde_json::to_value(success()).unwrap();
        value["stage"] = serde_json::json!("invented");
        assert!(serde_json::from_value::<DesktopProof>(value).is_err());
        let mut value = serde_json::to_value(success()).unwrap();
        value.as_object_mut().unwrap().remove("fresh_closed");
        assert!(serde_json::from_value::<DesktopProof>(value).is_err());
        let mut proof = success();
        proof.descriptor = "\u{1}".repeat(8192); // JSON escaping also counts toward wire size.
        assert!(proof.require().is_err());
        let mut proof = success();
        proof
            .open_denials
            .push((MUTATION_OPENS[2], ERROR_ACCESS_DENIED.to_hresult().0));
        assert!(proof.require().is_err());
    }
}
