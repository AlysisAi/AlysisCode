//! Read-only verification of the candidate's bounded OS ambient deny set.
//! Presence in metadata is insufficient; the exact account denials must precede
//! allows on the actual pinned directories. This is not a global filesystem
//! audit and does not replace contained behavioral qualification.

use crate::{
    account_install::{AccountMode, SetupRecord},
    acl::Mask,
    util::{OwnedHandle, OwnedSd, pcwstr, win32_ok, wstr},
};
use anyhow::{Context, Result, ensure};
use std::{
    collections::BTreeSet,
    path::{Path, PathBuf},
};
use windows::Win32::{
    Security::{
        ACE_HEADER, ACL,
        Authorization::{GetSecurityInfo, SE_FILE_OBJECT},
        DACL_SECURITY_INFORMATION, GetAce, GetSecurityDescriptorDacl, IsValidAcl,
        PSECURITY_DESCRIPTOR,
    },
    Storage::FileSystem::{
        BY_HANDLE_FILE_INFORMATION, CreateFileW, FILE_ATTRIBUTE_DIRECTORY,
        FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT,
        FILE_SHARE_READ, FILE_SHARE_WRITE, GetFileInformationByHandle, OPEN_EXISTING,
    },
    UI::Shell::{
        FOLDERID_ProgramData, FOLDERID_Public, FOLDERID_Windows, KF_FLAG_DEFAULT,
        SHGetKnownFolderPath,
    },
};

pub(super) fn verify(record: &SetupRecord) -> Result<()> {
    let targets = targets()?;
    let expected: BTreeSet<_> = targets
        .iter()
        .map(|path| path.to_string_lossy().to_ascii_lowercase())
        .collect();
    for mode in [AccountMode::Offline, AccountMode::Online] {
        let paths = &record.account(mode)?.ambient_paths;
        let observed: BTreeSet<_> = paths.iter().map(|path| path.to_ascii_lowercase()).collect();
        ensure!(
            observed == expected && observed.len() == paths.len(),
            "candidate ambient inventory differs from OS-resolved policy"
        );
    }
    let offline = record.account(AccountMode::Offline)?.sid.as_str();
    let online = record.account(AccountMode::Online)?.sid.as_str();
    let mut pins = Vec::new();
    for target in targets {
        let chain = pin(&target)?;
        let mut sd = PSECURITY_DESCRIPTOR::default();
        win32_ok(
            unsafe {
                GetSecurityInfo(
                    chain.last().context("ambient pin missing")?.raw(),
                    SE_FILE_OBJECT,
                    DACL_SECURITY_INFORMATION,
                    None,
                    None,
                    None,
                    None,
                    Some(&mut sd),
                )
            },
            "read candidate ambient security",
        )?;
        validate_denials(&OwnedSd::from_raw(sd), offline, online).with_context(|| {
            format!(
                "candidate ambient denial is incomplete at {}",
                target.display()
            )
        })?;
        pins.extend(chain);
    }
    Ok(())
}

fn known_folder(id: &windows::core::GUID) -> Result<PathBuf> {
    #[link(name = "ole32")]
    unsafe extern "system" {
        fn CoTaskMemFree(pointer: *const std::ffi::c_void);
    }
    let value = unsafe { SHGetKnownFolderPath(id, KF_FLAG_DEFAULT, None)? };
    let result = unsafe { value.to_string() };
    unsafe {
        CoTaskMemFree(value.0.cast());
    }
    Ok(PathBuf::from(result?))
}

fn targets() -> Result<Vec<PathBuf>> {
    let windows = known_folder(&FOLDERID_Windows)?;
    let mut targets = vec![
        known_folder(&FOLDERID_ProgramData)?,
        known_folder(&FOLDERID_Public)?,
        windows.join("Temp"),
    ];
    for path in &targets {
        ensure!(
            std::fs::metadata(path)?.is_dir(),
            "required ambient target is not a directory"
        );
    }
    for part in [
        "Tasks",
        "tracing",
        r"Registration\CRMLog",
        r"System32\FxsTmp",
        r"System32\com\dmp",
        r"System32\spool\PRINTERS",
        r"System32\spool\drivers\color",
        r"SysWOW64\FxsTmp",
        r"SysWOW64\com\dmp",
        r"SysWOW64\Tasks",
    ] {
        let path = windows.join(part);
        match std::fs::metadata(&path) {
            Ok(info) => {
                ensure!(
                    info.is_dir(),
                    "optional ambient target has wrong object type"
                );
                targets.push(path);
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => (),
            Err(error) => return Err(error).context("cannot observe optional ambient target"),
        }
    }
    Ok(targets)
}

fn pin(path: &Path) -> Result<Vec<OwnedHandle>> {
    let text = path.to_str().context("non-Unicode ambient path")?;
    ensure!(
        text.len() > 3
            && text.as_bytes()[0].is_ascii_alphabetic()
            && &text.as_bytes()[1..3] == b":\\",
        "ambient policy requires local absolute paths"
    );
    ensure!(
        text[3..].split('\\').all(|part| !part.is_empty()
            && !matches!(part, "." | "..")
            && !part.ends_with([' ', '.'])
            && !part.contains(['/', ':', '\0'])),
        "ambiguous ambient path"
    );
    let mut paths: Vec<_> = path.ancestors().collect();
    paths.reverse();
    let mut pins = Vec::new();
    for part in paths {
        let wide = wstr(part.to_str().context("non-Unicode ambient ancestor")?);
        let handle = OwnedHandle(unsafe {
            CreateFileW(
                pcwstr(&wide),
                0x20081,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
        }?);
        let mut info = BY_HANDLE_FILE_INFORMATION::default();
        unsafe {
            GetFileInformationByHandle(handle.raw(), &mut info)?;
        }
        ensure!(
            info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY.0 != 0
                && info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT.0 == 0,
            "ambient ancestor is not an ordinary directory"
        );
        pins.push(handle);
    }
    Ok(pins)
}

fn validate_denials(sd: &OwnedSd, offline: &str, online: &str) -> Result<()> {
    let offline = crate::sid::LocalPsid::from_string(offline)?;
    let online = crate::sid::LocalPsid::from_string(online)?;
    let wanted = Mask::FILE_GENERIC_WRITE
        .with(Mask::DELETE)
        .with(Mask::FILE_DELETE_CHILD)
        .with(Mask::WRITE_DAC)
        .with(Mask::WRITE_OWNER)
        .without(Mask::READ_CONTROL)
        .without(Mask::SYNCHRONIZE)
        .bits();
    let mut present = false.into();
    let mut defaulted = false.into();
    let mut acl: *mut ACL = std::ptr::null_mut();
    unsafe {
        GetSecurityDescriptorDacl(sd.ptr, &mut present, &mut acl, &mut defaulted)?;
    }
    ensure!(
        present.as_bool() && !acl.is_null() && unsafe { IsValidAcl(acl) }.as_bool(),
        "ambient DACL absent or invalid"
    );
    let mut found = [false, false];
    for index in 0..unsafe { (*acl).AceCount } {
        let mut raw = std::ptr::null_mut();
        unsafe {
            GetAce(acl, index as u32, &mut raw)?;
        }
        let header = unsafe { &*raw.cast::<ACE_HEADER>() };
        ensure!(header.AceSize >= 8, "short ambient ACE");
        if header.AceType == 0 {
            break;
        } // a later denial cannot negate already granted bits
        ensure!(
            header.AceType == 1,
            "unsupported ambient ACE before denials"
        );
        let bytes =
            unsafe { std::slice::from_raw_parts(raw.cast::<u8>(), header.AceSize as usize) };
        let mask = u32::from_le_bytes(bytes[4..8].try_into().unwrap());
        if header.AceFlags == 3 && mask & wanted == wanted {
            for (index, sid) in [offline.as_bytes(), online.as_bytes()]
                .into_iter()
                .enumerate()
            {
                found[index] |= bytes[8..] == *sid;
            }
        }
        if found == [true, true] {
            return Ok(());
        }
    }
    anyhow::bail!("both explicit inheritable write denials must precede allows")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn candidate_activation_ambient_denies_cannot_be_inherit_only_narrow_or_late() {
        let offline = "S-1-5-21-1-2-3-1003";
        let online = "S-1-5-21-1-2-3-1004";
        let deny = |sid: &str, flags: &str, mask: &str| format!("(D;{flags};{mask};;;{sid})");
        let good = format!(
            "D:P{}{}(A;;FA;;;BU)",
            deny(offline, "OICI", "FA"),
            deny(online, "OICI", "FA")
        );
        validate_denials(&OwnedSd::from_sddl(&good).unwrap(), offline, online).unwrap();
        for altered in [
            good.replace("OICI", "OICIIO"),
            good.replace("OICI", "OICIID"),
            good.replace("OICI", "OICINP"),
            good.replace(";FA;", ";SD;"),
            good.replace(online, "S-1-5-21-1-2-3-9999"),
            good.replacen("D:P", "D:P(A;;FA;;;BU)", 1),
        ] {
            assert!(
                validate_denials(&OwnedSd::from_sddl(&altered).unwrap(), offline, online).is_err()
            );
        }
    }
}
