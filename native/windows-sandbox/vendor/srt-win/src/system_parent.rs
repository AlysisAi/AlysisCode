//! Read-only proof for parents whose existing system ACL already prevents
//! sandbox mutation. This is not an access-denied fallback or a path allowlist.
use super::*;
use crate::path_id::FileId;
use windows::Win32::Security::INHERIT_ONLY_ACE;

const TRUSTED_INSTALLER: &str = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464";

fn system_principal(sid: &str) -> bool {
    matches!(sid, SID_SYSTEM | SID_BUILTIN_ADMINS | TRUSTED_INSTALLER)
}

fn protected_descriptor(owner: &str, dacl: *const ACL) -> Result<bool> {
    if !system_principal(owner) || dacl.is_null() {
        return Ok(false);
    }
    let mut protected = true;
    filter_aces(dacl, |header, body| {
        // Only ordinary allow/deny ACEs have semantics understood here.
        // Even an unfamiliar deny is not evidence of a protected parent.
        if !matches!(header.AceType, 0 | 1) {
            protected = false;
            return false;
        }
        if header.AceType == 1 || header.AceFlags & INHERIT_ONLY_ACE.0 as u8 != 0 {
            return false;
        }
        if body.len() < 16 || body.len() != 16 + 4 * body[9] as usize {
            protected = false;
            return false;
        }
        let mask = u32::from_le_bytes(body[4..8].try_into().unwrap());
        let sid =
            crate::sid::psid_to_string(PSID(unsafe { body.as_ptr().add(8) }.cast_mut().cast()));
        // Adding a new subdirectory alone cannot replace an existing child.
        // Reject every other write right, including generic rights, DELETE,
        // parent DELETE_CHILD, ownership/DACL edits and reparse-point writes.
        let mutation = 0x1000_0000 | 0x4000_0000 | 0x000d_0000 | 0x40 | 0x100 | 0x10 | 0x2;
        if !sid.is_ok_and(|sid| system_principal(&sid)) && mask & mutation != 0 {
            protected = false;
        }
        false
    })?;
    Ok(protected)
}

/// Verify the exact directory and every ancestor through pinned, non-reparse
/// handles. A protected leaf alone is insufficient: an attacker could rename
/// it through a writable ancestor. Neither host-user ownership nor a deny ACE
/// cancelling a broad allow is accepted as system protection. The sandbox
/// account token disables Administrators; AppContainer write restrictions
/// require the sandbox SID or Everyone, neither of which receives mutation
/// grants here. Both tokens are also kernel-probed before user code starts.
/// Trusted administrators changing these ACLs remain outside the threat model.
pub fn system_parent_protected(path: &str, expected: FileId) -> Result<bool> {
    use windows::Win32::Storage::FileSystem::{
        BY_HANDLE_FILE_INFORMATION, FILE_ATTRIBUTE_DIRECTORY, GetFileInformationByHandle,
    };
    let path = std::path::Path::new(path);
    ensure!(path.is_absolute(), "system parent must be absolute");
    let mut chain: Vec<_> = path.ancestors().collect();
    chain.reverse();
    let mut pins = Vec::with_capacity(chain.len());
    for component in chain {
        pins.push(LockedAclTarget::open_component(
            component,
            Mask::READ_CONTROL.bits(),
        )?);
    }
    ensure!(
        crate::path_id::file_id_from_handle(pins.last().context("empty parent chain")?.raw())?
            == expected,
        "system parent identity changed"
    );
    // Check the leaf first: ordinary user-owned parents take the explicit deny
    // path without requiring their ancestors to satisfy the system policy.
    for pin in pins.iter().rev() {
        let mut info = BY_HANDLE_FILE_INFORMATION::default();
        unsafe { GetFileInformationByHandle(pin.raw(), &mut info)? };
        ensure!(
            info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY.0 != 0,
            "parent is not a directory"
        );
        let mut descriptor = PSECURITY_DESCRIPTOR::default();
        let mut owner = PSID::default();
        win32_ok(
            unsafe {
                GetSecurityInfo(
                    pin.raw(),
                    SE_FILE_OBJECT,
                    OWNER_SECURITY_INFORMATION,
                    Some(&mut owner),
                    None,
                    None,
                    None,
                    Some(&mut descriptor),
                )
            },
            "read system parent owner",
        )?;
        let _descriptor = OwnedSd::from_raw(descriptor);
        if owner.0.is_null() || !system_principal(&crate::sid::psid_to_string(owner)?) {
            return Ok(false);
        }
        let raw = RawFileDacl::read(pin.raw())?;
        raw.require_supported_control()?;
        if !protected_descriptor(&crate::sid::psid_to_string(owner)?, raw.acl())? {
            return Ok(false);
        }
    }
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn admits(sddl: &str) -> bool {
        use windows::Win32::Security::{GetSecurityDescriptorDacl, GetSecurityDescriptorOwner};
        let sd = OwnedSd::from_sddl(sddl).unwrap();
        let mut owner = PSID::default();
        let mut defaulted = false.into();
        let mut present = false.into();
        let mut dacl = std::ptr::null_mut();
        unsafe {
            GetSecurityDescriptorOwner(sd.ptr, &mut owner, &mut defaulted).unwrap();
            GetSecurityDescriptorDacl(sd.ptr, &mut present, &mut dacl, &mut defaulted).unwrap();
        }
        protected_descriptor(&crate::sid::psid_to_string(owner).unwrap(), dacl).unwrap()
    }

    #[test]
    fn system_parent_proof_requires_system_owner_and_no_untrusted_mutation() {
        for owner in [SID_SYSTEM, SID_BUILTIN_ADMINS, TRUSTED_INSTALLER] {
            assert!(admits(&format!(
                "O:{owner}D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FRFX;;;BU)(A;OICIIO;GA;;;CO)"
            )));
            // The drive root commonly permits new subdirectories, but not
            // deletion/replacement of existing system directories.
            assert!(admits(&format!("O:{owner}D:P(A;;0x4;;;AU)(A;;FA;;;SY)")));
            for mask in [
                0x10000000u32,
                0x40000000,
                0x10000,
                0x40000,
                0x80000,
                0x40,
                0x100,
                0x10,
                0x2,
            ] {
                for trustee in ["BU", "AU", "WD", "OW", "S-1-5-21-1-2-3-1000"] {
                    assert!(
                        !admits(&format!(
                            "O:{owner}D:P(A;;0x{mask:x};;;{trustee})(A;;FA;;;SY)"
                        )),
                        "{owner} {trustee} {mask:x}"
                    );
                }
            }
        }
        assert!(!admits("O:S-1-5-21-1-2-3-1000D:P(A;;FR;;;WD)"));
        assert!(!admits("O:SYD:NO_ACCESS_CONTROL"));
        assert!(!admits("O:SYD:P(D;;FA;;;BU)(A;;FA;;;BU)"));
        assert!(!admits(
            "O:SYD:P(OA;;FR;00000000-0000-0000-0000-000000000001;;BU)"
        ));
        assert!(admits("O:SYD:P")); // An empty DACL denies everyone.
    }

    #[test]
    fn program_files_proof_is_read_only_and_checks_identity() {
        // This only reads the real system directory; it never stamps a test ACE.
        let path = std::env::var("ProgramFiles").unwrap();
        let (path, _) = crate::path_id::canonicalize_path(&path).unwrap();
        let id = crate::path_id::capture_file_id(&path).unwrap();
        let pin =
            LockedAclTarget::open_component(std::path::Path::new(&path), Mask::READ_CONTROL.bits())
                .unwrap();
        let before = RawFileDacl::read(pin.raw()).unwrap();
        assert!(system_parent_protected(&path, id).unwrap());
        assert_eq!(before, RawFileDacl::read(pin.raw()).unwrap());
        let wrong = FileId::from_bytes(&[0; 24]).unwrap();
        assert!(system_parent_protected(&path, wrong).is_err());
    }
}
