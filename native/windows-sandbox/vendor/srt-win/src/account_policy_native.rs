//! Native effects for the fixed candidate policy plan. Kept private beneath the
//! transaction coordinator; no arbitrary group, SID, right, or callback input.
use super::*;
use crate::util::{pcwstr, wstr};
use windows::Win32::Security::Authentication::Identity::*;
use windows::core::{PCWSTR, PWSTR};

struct NetBuffer(*mut u8);
impl Drop for NetBuffer {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                let _ = NetApiBufferFree(Some(self.0.cast()));
            }
        }
    }
}
fn account(binding: &AccountBinding) -> Result<Account> {
    let name = wstr(&binding.account_name);
    let mut raw = std::ptr::null_mut();
    let code = unsafe { NetUserGetInfo(PCWSTR::null(), pcwstr(&name), 4, &mut raw) };
    let raw = NetBuffer(raw);
    ensure!(
        code == 0 && !raw.0.is_null(),
        "observe candidate policy account: {code}"
    );
    let info = unsafe { &*raw.0.cast::<USER_INFO_4>() };
    // Do not read password, profile, or unrelated fields returned by SAM.
    Ok(Account {
        sid: crate::sid::psid_to_string(info.usri4_user_sid)?,
        flags: info.usri4_flags.0,
        privilege: info.usri4_priv.0,
        operator_flags: info.usri4_auth_flags.0,
        password_expired: info.usri4_password_expired,
        groups: activation::memberships(&binding.account_name)?,
        rights: activation::rights(&binding.account_sid)?,
    })
}
pub(super) struct NativePolicy {
    identity: Identity,
}
impl NativePolicy {
    pub(super) fn new(identity: Identity) -> Self {
        Self { identity }
    }
}
impl Policy for NativePolicy {
    fn observe(&mut self) -> Result<Snapshot> {
        crate::local_account_host::require_local_sam()?;
        let group_sid = crate::user::local_group_sid(super::super::ACCOUNT_GROUP)?
            .context("candidate policy group is missing")?;
        ensure!(
            group_sid == self.identity.offline.group_sid,
            "candidate policy group identity changed"
        );
        let value = Snapshot {
            group_sid,
            group_members: activation::group_members(super::super::ACCOUNT_GROUP)?,
            group_rights: activation::rights(&self.identity.offline.group_sid)?,
            offline: account(&self.identity.offline)?,
            online: account(&self.identity.online)?,
        };
        // Snapshot readers span several APIs. Recheck every principal before
        // accepting the observation; the lease excludes our own setup/execution.
        // A hostile administrator mutating SAM concurrently is outside scope.
        ensure!(
            crate::user::local_group_sid(super::super::ACCOUNT_GROUP)?.as_deref()
                == Some(self.identity.offline.group_sid.as_str())
                && crate::user::local_user_sid(&self.identity.offline.account_name)?.as_deref()
                    == Some(self.identity.offline.account_sid.as_str())
                && crate::user::local_user_sid(&self.identity.online.account_name)?.as_deref()
                    == Some(self.identity.online.account_sid.as_str()),
            "candidate policy principal identity changed during observation"
        );
        value.offline.validate_disabled(&self.identity.offline)?;
        value.online.validate_disabled(&self.identity.online)?;
        Ok(value)
    }
    fn apply(&mut self, effect: Effect) -> Result<()> {
        match effect {
            Effect::Denials => add_denials(&self.identity.offline.group_sid),
            Effect::Users(mode) => {
                let name = crate::sid::lookup_account_name(activation::USERS)?;
                add_member(&name, &self.identity.binding(mode).account_sid)
            }
            Effect::Owned(mode) => add_member(
                super::super::ACCOUNT_GROUP,
                &self.identity.binding(mode).account_sid,
            ),
        }
    }
}
fn add_member(group: &str, member: &str) -> Result<()> {
    let group = wstr(group);
    let member = crate::sid::LocalPsid::from_string(member)?;
    let value = LOCALGROUP_MEMBERS_INFO_0 {
        lgrmi0_sid: member.as_psid(),
    };
    let code = unsafe {
        NetLocalGroupAddMembers(
            PCWSTR::null(),
            pcwstr(&group),
            0,
            (&value as *const LOCALGROUP_MEMBERS_INFO_0).cast(),
            1,
        )
    };
    // Even ALREADY_MEMBER is unexpected after the exact missing-member
    // observation. Preserve the intent instead of guessing who changed it.
    ensure!(code == 0, "add fixed candidate group member: {code}");
    Ok(())
}
struct LsaPolicy(LSA_HANDLE);
impl Drop for LsaPolicy {
    fn drop(&mut self) {
        unsafe {
            let _ = LsaClose(self.0);
        }
    }
}
fn add_denials(group: &str) -> Result<()> {
    let group = crate::sid::LocalPsid::from_string(group)?;
    let mut handle = LSA_HANDLE::default();
    let status = unsafe {
        LsaOpenPolicy(
            None,
            &LSA_OBJECT_ATTRIBUTES::default(),
            (POLICY_CREATE_ACCOUNT | POLICY_LOOKUP_NAMES) as u32,
            &mut handle,
        )
    };
    ensure!(
        status.is_ok(),
        "open fixed candidate LSA policy: {status:?}"
    );
    let handle = LsaPolicy(handle);
    let buffers: Vec<_> = activation::DENIALS
        .iter()
        .map(|right| crate::util::counted_utf16(right))
        .collect();
    let rights: Vec<_> = buffers
        .iter()
        .map(|(buffer, length)| LSA_UNICODE_STRING {
            Length: *length,
            MaximumLength: *length,
            Buffer: PWSTR(buffer.as_ptr() as *mut u16),
        })
        .collect();
    let status = unsafe { LsaAddAccountRights(handle.0, group.as_psid(), &rights) };
    ensure!(
        status.is_ok(),
        "add fixed candidate group logon denials: {status:?}"
    );
    Ok(())
}
