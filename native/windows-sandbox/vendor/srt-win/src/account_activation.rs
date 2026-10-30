//! Read-only native gates before the disabled candidate can claim readiness.
//! No provisioning, password changes, execution, ACL writes, or WFP mutation.
//! The installer still owes contained behavioral qualification; these gates
//! cannot be replaced by a caller-supplied successful callback.

use anyhow::{Context, Result, ensure};
use std::collections::BTreeSet;
use windows::Win32::NetworkManagement::NetManagement::*;
use windows::Win32::Security::Authentication::Identity::*;
use windows::core::PCWSTR;

use super::{ACCOUNT_GROUP, AccountMode, SCHEMA_VERSION, SetupRecord};
use crate::util::{pcwstr, wstr};

#[path = "account_activation_ambient.rs"]
mod ambient;
#[path = "account_activation_wfp.rs"]
pub(super) mod wfp;

pub(super) const USERS: &str = "S-1-5-32-545";
pub(super) const DENIALS: [&str; 4] = [
    "SeDenyNetworkLogonRight",
    "SeDenyBatchLogonRight",
    "SeDenyServiceLogonRight",
    "SeDenyRemoteInteractiveLogonRight",
];

pub(super) fn verify(record: &SetupRecord) -> Result<()> {
    record.validate_ready(SCHEMA_VERSION, &crate::sid::current_user_sid()?)?;
    ensure!(
        super::setup::is_elevated()?,
        "candidate activation verification requires its elevated owner"
    );
    crate::local_account_host::require_local_sam()?;
    record.validate_observed_principals(
        crate::user::local_group_sid(ACCOUNT_GROUP)?.as_deref(),
        crate::user::local_user_sid(super::OFFLINE_USER)?.as_deref(),
        crate::user::local_user_sid(super::ONLINE_USER)?.as_deref(),
    )?;
    let offline = record.binding(AccountMode::Offline)?;
    validate_group_rights(&rights(&offline.group_sid)?)?;
    ensure!(
        group_members(ACCOUNT_GROUP)?
            == BTreeSet::from([
                offline.account_sid.clone(),
                record.binding(AccountMode::Online)?.account_sid,
            ]),
        "candidate group has missing or unexpected direct members"
    );
    wfp::verify(record)?;
    ambient::verify(record)?;
    for mode in [AccountMode::Offline, AccountMode::Online] {
        let binding = record.binding(mode)?;
        observe_account(&binding)?;
        ensure!(
            rights(&binding.account_sid)?.is_empty(),
            "candidate account has unexpected directly assigned LSA rights"
        );
        crate::account_credentials::verify_activation_logons(
            &binding,
            record
                .account(mode)?
                .credential_generation
                .as_deref()
                .context("candidate credential missing")?,
        )?;
    }
    // All checks were observational. Detect an administrative identity change
    // before returning, rather than accepting a name queried earlier.
    record.validate_observed_principals(
        crate::user::local_group_sid(ACCOUNT_GROUP)?.as_deref(),
        crate::user::local_user_sid(super::OFFLINE_USER)?.as_deref(),
        crate::user::local_user_sid(super::ONLINE_USER)?.as_deref(),
    )
}

fn validate_account(
    flags: u32,
    privilege: USER_PRIV,
    groups: &BTreeSet<String>,
    group: &str,
) -> Result<()> {
    let required = UF_NORMAL_ACCOUNT | UF_SCRIPT.0 | UF_DONT_EXPIRE_PASSWD.0;
    // No disabled/locked/passwordless/trust/delegation/smartcard-only modes.
    // PASSWD_CANT_CHANGE is an optional additional restriction, not a bypass.
    let allowed = required | UF_PASSWD_CANT_CHANGE.0;
    ensure!(
        flags & required == required && flags & !allowed == 0 && privilege == USER_PRIV_USER,
        "candidate account is not an enabled ordinary password account with the expected flags"
    );
    ensure!(
        *groups == BTreeSet::from([USERS.to_owned(), group.to_owned()]),
        "candidate account has missing or unexpected local group memberships"
    );
    Ok(())
}

fn validate_group_rights(observed: &BTreeSet<String>) -> Result<()> {
    ensure!(
        *observed == DENIALS.into_iter().map(str::to_owned).collect(),
        "candidate group must have exactly the four required logon denials"
    );
    Ok(())
}

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

fn observe_account(binding: &super::AccountBinding) -> Result<()> {
    let name = wstr(&binding.account_name);
    let mut buffer = std::ptr::null_mut();
    let result = unsafe { NetUserGetInfo(PCWSTR::null(), pcwstr(&name), 1, &mut buffer) };
    let buffer = NetBuffer(buffer);
    ensure!(
        result == 0 && !buffer.0.is_null(),
        "observe candidate account flags: {result}"
    );
    let info = unsafe { &*buffer.0.cast::<USER_INFO_1>() };
    let flags = info.usri1_flags.0;
    let privilege = info.usri1_priv;
    validate_account(
        flags,
        privilege,
        &memberships(&binding.account_name)?,
        &binding.group_sid,
    )
}

pub(super) fn memberships(account: &str) -> Result<BTreeSet<String>> {
    let name = wstr(account);
    let mut groups = std::ptr::null_mut();
    let mut read = 0;
    let mut total = 0;
    let result = unsafe {
        NetUserGetLocalGroups(
            PCWSTR::null(),
            pcwstr(&name),
            0,
            LG_INCLUDE_INDIRECT,
            &mut groups,
            64 * 1024,
            &mut read,
            &mut total,
        )
    };
    let groups = NetBuffer(groups);
    ensure!(
        result == 0 && read == total && read <= 128 && (read == 0 || !groups.0.is_null()),
        "candidate local memberships could not be completely enumerated: {result}"
    );
    let mut observed = BTreeSet::new();
    if read == 0 {
        return Ok(observed);
    }
    for group in unsafe {
        std::slice::from_raw_parts(groups.0.cast::<LOCALGROUP_USERS_INFO_0>(), read as usize)
    } {
        ensure!(
            !group.lgrui0_name.is_null(),
            "candidate membership has no name"
        );
        let group = unsafe { group.lgrui0_name.to_string()? };
        let sid = membership_sid(&group)?;
        ensure!(
            observed.insert(sid),
            "duplicate candidate local group identity"
        );
    }
    Ok(observed)
}

pub(super) fn group_members(name: &str) -> Result<BTreeSet<String>> {
    let name = wstr(name);
    let mut raw = std::ptr::null_mut();
    let mut read = 0;
    let mut total = 0;
    let result = unsafe {
        NetLocalGroupGetMembers(
            PCWSTR::null(),
            pcwstr(&name),
            0,
            &mut raw,
            64 * 1024,
            &mut read,
            &mut total,
            None,
        )
    };
    let raw = NetBuffer(raw);
    ensure!(
        result == 0 && read == total && read <= 128 && (read == 0 || !raw.0.is_null()),
        "candidate direct group members could not be completely enumerated: {result}"
    );
    let mut members = BTreeSet::new();
    if read == 0 {
        return Ok(members);
    }
    for value in unsafe {
        std::slice::from_raw_parts(raw.0.cast::<LOCALGROUP_MEMBERS_INFO_0>(), read as usize)
    } {
        let sid = crate::sid::psid_to_string(value.lgrmi0_sid)?;
        ensure!(
            members.insert(sid),
            "duplicate candidate direct group member"
        );
    }
    Ok(members)
}

fn membership_sid(name: &str) -> Result<String> {
    // BUILTIN aliases do not live under COMPUTER\name. The well-known SID
    // supplies its exact localized alias name; this special case never falls
    // back to an unqualified account/domain lookup. Every non-Users group still
    // requires local SAM existence and a machine-qualified identity.
    if name == crate::sid::lookup_account_name(USERS)? {
        return Ok(USERS.to_owned());
    }
    crate::user::local_group_sid(name)?.context("candidate local group disappeared")
}

struct Policy(LSA_HANDLE);
impl Drop for Policy {
    fn drop(&mut self) {
        unsafe {
            let _ = LsaClose(self.0);
        }
    }
}
struct LsaBuffer(*mut LSA_UNICODE_STRING);
impl Drop for LsaBuffer {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                let _ = LsaFreeMemory(Some(self.0.cast()));
            }
        }
    }
}

pub(super) fn rights(sid: &str) -> Result<BTreeSet<String>> {
    let sid = crate::sid::LocalPsid::from_string(sid)?;
    let mut handle = LSA_HANDLE::default();
    let result = unsafe {
        LsaOpenPolicy(
            None,
            &LSA_OBJECT_ATTRIBUTES::default(),
            POLICY_LOOKUP_NAMES as u32,
            &mut handle,
        )
    };
    ensure!(
        result.is_ok(),
        "open candidate LSA observation policy: {result:?}"
    );
    let handle = Policy(handle);
    let mut values = std::ptr::null_mut();
    let mut count = 0;
    let result =
        unsafe { LsaEnumerateAccountRights(handle.0, sid.as_psid(), &mut values, &mut count) };
    let values = LsaBuffer(values);
    if result.is_err() {
        let code = unsafe { LsaNtStatusToWinError(result) };
        ensure!(code == 2, "observe candidate LSA rights: {code}");
        return Ok(BTreeSet::new());
    }
    ensure!(
        count <= 128 && (count == 0 || !values.0.is_null()),
        "invalid candidate LSA rights buffer"
    );
    let mut result = BTreeSet::new();
    if count == 0 {
        return Ok(result);
    }
    for value in unsafe { std::slice::from_raw_parts(values.0, count as usize) } {
        ensure!(
            value.Length > 0
                && value.Length % 2 == 0
                && value.Length <= 512
                && value.MaximumLength >= value.Length
                && !value.Buffer.is_null(),
            "invalid candidate LSA right string"
        );
        let text = String::from_utf16(unsafe {
            std::slice::from_raw_parts(value.Buffer.0, value.Length as usize / 2)
        })?;
        ensure!(result.insert(text), "duplicate candidate LSA right");
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn candidate_activation_builtin_membership_resolves_locally() {
        let name = crate::sid::lookup_account_name(USERS).unwrap();
        assert_eq!(membership_sid(&name).unwrap(), USERS);
    }
    #[test]
    fn candidate_activation_account_policy_requires_exact_flags_and_memberships() {
        let group = "S-1-5-21-1-2-3-1002";
        let good = UF_NORMAL_ACCOUNT | UF_SCRIPT.0 | UF_DONT_EXPIRE_PASSWD.0;
        let groups = BTreeSet::from([USERS.to_owned(), group.into()]);
        validate_account(good, USER_PRIV_USER, &groups, group).unwrap();
        validate_account(
            good | UF_PASSWD_CANT_CHANGE.0,
            USER_PRIV_USER,
            &groups,
            group,
        )
        .unwrap();
        for flag in [
            UF_ACCOUNTDISABLE,
            UF_LOCKOUT,
            UF_PASSWD_NOTREQD,
            UF_TRUSTED_FOR_DELEGATION,
            UF_PASSWORD_EXPIRED,
        ] {
            assert!(validate_account(good | flag.0, USER_PRIV_USER, &groups, group).is_err());
        }
        assert!(validate_account(good, USER_PRIV_ADMIN, &groups, group).is_err());
        let mut missing = groups.clone();
        missing.remove(group);
        assert!(validate_account(good, USER_PRIV_USER, &missing, group).is_err());
        let mut admin = groups;
        admin.insert("S-1-5-32-544".into());
        assert!(validate_account(good, USER_PRIV_USER, &admin, group).is_err());
    }
    #[test]
    fn candidate_activation_lsa_policy_requires_all_denials_without_extra_privilege() {
        let good: BTreeSet<String> = DENIALS.into_iter().map(str::to_owned).collect();
        validate_group_rights(&good).unwrap();
        for right in DENIALS {
            let mut missing = good.clone();
            missing.remove(right);
            assert!(validate_group_rights(&missing).is_err());
        }
        let mut privileged = good;
        privileged.insert("SeTcbPrivilege".into());
        assert!(validate_group_rights(&privileged).is_err());
    }
}
