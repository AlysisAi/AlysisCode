//! Installation-owned access to the fixed discard device. Ordinary brokers
//! cannot WRITE_DAC on NUL, so only elevated setup/uninstall changes it. The
//! capability is tied to the recorded account SID and grants no other object.
//! Device ACLs can reset at reboot: readiness then requires explicit setup repair.

use anyhow::{Context, Result, ensure};
use std::mem::size_of;
use windows::Wdk::Foundation::OBJECT_ATTRIBUTES;
use windows::Win32::Foundation::{
    HANDLE, NTSTATUS, OBJ_CASE_INSENSITIVE, OBJ_DONT_REPARSE, UNICODE_STRING,
};
use windows::Win32::Security::Authorization::{GetSecurityInfo, SE_FILE_OBJECT, SetSecurityInfo};
use windows::Win32::Security::{ACE_FLAGS, ACL, DACL_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR};
use windows::Win32::System::IO::IO_STATUS_BLOCK;
use windows::core::PWSTR;

use crate::acl::{BuiltAcl, NewAce, ace_sid_is, filter_aces, rebuild_acl};
use crate::sid::LocalPsid;
use crate::util::{OwnedHandle, OwnedSd, win32_ok};

const READ_WRITE: u32 = 0xc0000000;
const READ_CONTROL: u32 = 0x20000;
const WRITE_DAC: u32 = 0x40000;

#[link(name = "ntdll")]
unsafe extern "system" {
    fn NtOpenFile(
        handle: *mut HANDLE,
        access: u32,
        attrs: *const OBJECT_ATTRIBUTES,
        io: *mut IO_STATUS_BLOCK,
        share: u32,
        options: u32,
    ) -> NTSTATUS;
}

pub(crate) fn capability(account_sid: &str) -> Result<LocalPsid> {
    // Parse before deriving: callers supply the recorded installation identity.
    let account = LocalPsid::from_string(account_sid)?;
    let canonical = crate::sid::psid_to_string(account.as_psid())?;
    LocalPsid::from_capability(&format!("Alysis.Native.NullDevice.{canonical}"))
}

fn open(access: u32) -> Result<OwnedHandle> {
    // Native absolute name, no caller DOS-device alias, filesystem path or reparse.
    let (mut wide, length) = crate::util::counted_utf16(r"\Device\Null");
    let name = UNICODE_STRING {
        Length: length,
        MaximumLength: length,
        Buffer: PWSTR(wide.as_mut_ptr()),
    };
    let attrs = OBJECT_ATTRIBUTES {
        Length: size_of::<OBJECT_ATTRIBUTES>() as u32,
        ObjectName: &name,
        Attributes: OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE,
        ..Default::default()
    };
    let mut handle = HANDLE::default();
    let mut io = IO_STATUS_BLOCK::default();
    // SYNCHRONIZE, FILE_SYNCHRONOUS_IO_NONALERT and FILE_NON_DIRECTORY_FILE.
    let status = unsafe { NtOpenFile(&mut handle, access | 0x100000, &attrs, &mut io, 3, 0x60) };
    status.ok().context("open fixed null device")?;
    let handle = OwnedHandle(handle);
    // NtOpenFile cannot create the object. Unlike filesystem creates, a device
    // open need not report FILE_OPENED in Information (Null reports zero).
    ensure!(
        status.0 == 0 && unsafe { io.Anonymous.Status.0 } == 0,
        "null device did not open synchronously"
    );
    Ok(handle)
}

fn descriptor(handle: HANDLE) -> Result<(OwnedSd, *mut ACL)> {
    let mut sd = PSECURITY_DESCRIPTOR::default();
    let mut acl = std::ptr::null_mut();
    let status = unsafe {
        GetSecurityInfo(
            handle,
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            None,
            None,
            Some(&mut acl),
            None,
            Some(&mut sd),
        )
    };
    let owned = OwnedSd::from_raw(sd);
    win32_ok(status, "read null-device DACL")?;
    ensure!(!acl.is_null(), "null device has no restrictive DACL");
    Ok((owned, acl))
}

/// Preserve all other ACE bytes and order. Refuse an unknown ACE layout rather
/// than treating a SID at a guessed offset as an installation-owned grant.
fn compose(acl: *const ACL, sid: &LocalPsid, grant: bool) -> Result<(BuiltAcl, bool)> {
    let mut matching = 0;
    let mut unsupported = false;
    let kept = filter_aces(acl, |header, body| {
        if header.AceType > 1 || body.len() < 8 {
            unsupported = true;
            return true;
        }
        if !ace_sid_is(body, sid.as_bytes()) {
            return true;
        }
        matching += 1;
        // SetSecurityInfo maps generic file rights to FILE_GENERIC_READ|WRITE.
        let mask = u32::from_le_bytes(body[4..8].try_into().unwrap());
        unsupported |= header.AceType != 0
            || header.AceFlags != 0
            || ![READ_WRITE, 0x12019f].contains(&mask)
            || body.len() != 8 + sid.as_bytes().len();
        false
    })?;
    ensure!(
        !unsupported && matching <= 1,
        "unexpected null-device ACL; refusing to rewrite it"
    );
    let additions = if grant {
        vec![NewAce::Allow(sid.as_psid(), READ_WRITE, ACE_FLAGS(0))]
    } else {
        Vec::new()
    };
    Ok((rebuild_acl(kept.2, &[], &kept, &additions)?, matching == 1))
}

pub fn ready(account_sid: &str) -> Result<bool> {
    let sid = capability(account_sid)?;
    let handle = open(READ_CONTROL)?;
    let (_sd, acl) = descriptor(handle.raw())?;
    Ok(compose(acl, &sid, false)?.1)
}

/// Called only while elevated install/uninstall holds its maintenance lease.
/// The account identity is recorded before adding a grant, so interrupted setup
/// remains recoverable; uninstall removes this SID before deleting that record.
pub fn configure(account_sid: &str, grant: bool) -> Result<()> {
    let sid = capability(account_sid)?;
    let handle = open(READ_CONTROL | WRITE_DAC)?;
    let (_sd, acl) = descriptor(handle.raw())?;
    let (replacement, present) = compose(acl, &sid, grant)?;
    if present == grant {
        return Ok(());
    }
    win32_ok(
        unsafe {
            SetSecurityInfo(
                handle.raw(),
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION,
                None,
                None,
                Some(replacement.as_ptr()),
                None,
            )
        },
        "update null-device capability",
    )?;
    let (_sd, updated) = descriptor(handle.raw())?;
    ensure!(
        compose(updated, &sid, false)?.1 == grant,
        "null-device grant verification failed"
    );
    Ok(())
}

/// Called while impersonating the actual suspended child, never the host token.
pub(crate) fn probe() -> Result<()> {
    drop(open(READ_WRITE).context("sandbox cannot open NUL; rerun elevated native setup")?);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use windows::Win32::Security::GetSecurityDescriptorDacl;

    fn dacl(sd: &OwnedSd) -> *const ACL {
        let mut present = false.into();
        let mut defaulted = false.into();
        let mut acl = std::ptr::null_mut();
        unsafe {
            GetSecurityDescriptorDacl(sd.ptr, &mut present, &mut acl, &mut defaulted).unwrap();
        }
        assert!(present.as_bool() && !acl.is_null());
        acl
    }
    fn bytes(acl: *const ACL) -> Vec<Vec<u8>> {
        let mut result = Vec::new();
        filter_aces(acl, |_, body| {
            result.push(body.to_vec());
            true
        })
        .unwrap();
        result
    }
    #[test]
    fn grant_revoke_preserves_other_aces_and_rejects_foreign_target_permissions() {
        let sid = capability("S-1-5-21-101-202-303-1001").unwrap();
        let other = capability("S-1-5-21-101-202-303-1002").unwrap();
        assert_ne!(sid.as_bytes(), other.as_bytes());
        let sd = OwnedSd::from_sddl("D:(D;;WD;;;AN)(A;;GA;;;SY)(A;;GRGW;;;WD)").unwrap();
        let original = bytes(dacl(&sd));
        let (added, present) = compose(dacl(&sd), &sid, true).unwrap();
        assert!(!present);
        assert_eq!(
            &bytes(added.as_ptr())[..original.len()],
            original.as_slice()
        );
        assert!(compose(added.as_ptr(), &sid, true).unwrap().1);
        let (removed, present) = compose(added.as_ptr(), &sid, false).unwrap();
        assert!(present);
        assert_eq!(bytes(removed.as_ptr()), original);
        for ace in ["A;;GA", "D;;GRGW", "A;OI;GRGW"] {
            let text = crate::sid::psid_to_string(sid.as_psid()).unwrap();
            let sd = OwnedSd::from_sddl(&format!("D:({ace};;;{text})")).unwrap();
            assert!(compose(dacl(&sd), &sid, true).is_err());
        }
    }
}
