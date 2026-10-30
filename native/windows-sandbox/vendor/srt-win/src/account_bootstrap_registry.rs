//! Private same-handle bootstrap metadata adapter. Fresh creation requires a
//! no-follow absence preflight and new-object disposition. Partial opens use
//! OPEN_LINK and reject links. No existing-root repair or guessed cleanup.

use anyhow::{Context, Result, ensure};
use windows::Win32::{
    Foundation::{ERROR_FILE_NOT_FOUND, HANDLE},
    Security::{
        ACE_HEADER, ACL,
        Authorization::{GetSecurityInfo, SE_REGISTRY_KEY, SetSecurityInfo},
        Cryptography::{BCRYPT_USE_SYSTEM_PREFERRED_RNG, BCryptGenRandom},
        DACL_SECURITY_INFORMATION, GetAce, GetSecurityDescriptorControl, GetSecurityDescriptorDacl,
        GetSecurityDescriptorOwner, IsValidAcl, OWNER_SECURITY_INFORMATION,
        PROTECTED_DACL_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, PSID, SE_DACL_PROTECTED,
        SECURITY_ATTRIBUTES,
    },
    System::Registry::{
        HKEY, HKEY_LOCAL_MACHINE, KEY_ALL_ACCESS, KEY_READ, KEY_WOW64_64KEY, REG_CREATED_NEW_KEY,
        REG_DWORD, REG_OPTION_NON_VOLATILE, REG_OPTION_OPEN_LINK, REG_SZ, REG_VALUE_TYPE,
        RegCloseKey, RegCreateKeyExW, RegFlushKey, RegOpenKeyExW, RegQueryValueExW, RegSetValueExW,
    },
};

use super::{Observed, Ownership, Phase, Principal, SetupRecord, Store, sam};
use crate::account_install::{AccountMode, REGISTRY_ROOT};
use crate::util::{OwnedSd, pcwstr, win32_ok, wstr};

pub(super) const MAX_VALUE: usize = 1024 * 1024;

pub(super) fn random_id() -> Result<String> {
    let mut random = [0u8; 16];
    let status = unsafe { BCryptGenRandom(None, &mut random, BCRYPT_USE_SYSTEM_PREFERRED_RNG) };
    ensure!(status.0 == 0, "bootstrap entropy failed: {status:?}");
    Ok(random.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn bootstrap_sddl(owner: &str) -> Result<String> {
    crate::account_install::validate_sid(owner)?;
    Ok(format!(
        "O:BAG:BAD:P(A;;KA;;;SY)(A;;KA;;;BA)(A;;KR;;;{owner})"
    ))
}

fn final_sddl(value: &Ownership) -> Result<String> {
    let binding = value.record.binding(AccountMode::Offline)?;
    crate::reg::credential_key_sddl(&binding.owner_sid, &binding.account_sid, &binding.group_sid)
}

struct Key(HKEY);

impl Drop for Key {
    fn drop(&mut self) {
        unsafe {
            let _ = RegCloseKey(self.0);
        }
    }
}

impl Key {
    fn read(&self, name: &str, expected: Option<REG_VALUE_TYPE>) -> Result<Option<Vec<u8>>> {
        let name = wstr(name);
        let mut size = 0;
        let mut ty = REG_VALUE_TYPE::default();
        let status = unsafe {
            RegQueryValueExW(
                self.0,
                pcwstr(&name),
                None,
                Some(&mut ty),
                None,
                Some(&mut size),
            )
        };
        if status == ERROR_FILE_NOT_FOUND {
            return Ok(None);
        }
        win32_ok(status, "size candidate bootstrap value")?;
        ensure!(
            size as usize <= MAX_VALUE,
            "candidate bootstrap value exceeds size limit"
        );
        ensure!(
            expected.is_none_or(|value| value == ty),
            "candidate bootstrap value type mismatch"
        );
        let mut bytes = vec![0; size as usize];
        let expected_size = size;
        win32_ok(
            unsafe {
                RegQueryValueExW(
                    self.0,
                    pcwstr(&name),
                    None,
                    Some(&mut ty),
                    Some(bytes.as_mut_ptr()),
                    Some(&mut size),
                )
            },
            "read candidate bootstrap value",
        )?;
        ensure!(
            size == expected_size && expected.is_none_or(|value| value == ty),
            "candidate bootstrap value changed while reading"
        );
        Ok(Some(bytes))
    }

    fn text(&self, name: &str) -> Result<Option<String>> {
        self.read(name, Some(REG_SZ))?
            .map(|bytes| decode_text(&bytes))
            .transpose()
    }

    fn marker(&self) -> Result<u32> {
        let bytes = self.read("MarkerVersion", Some(REG_DWORD))?.context(
            "candidate bootstrap marker missing; explicit administrator repair required",
        )?;
        Ok(u32::from_le_bytes(bytes.try_into().map_err(|_| {
            anyhow::anyhow!("invalid bootstrap marker size")
        })?))
    }

    fn write(&self, name: &str, ty: REG_VALUE_TYPE, bytes: &[u8]) -> Result<()> {
        ensure!(
            bytes.len() <= MAX_VALUE,
            "candidate bootstrap value exceeds size limit"
        );
        let wide = wstr(name);
        win32_ok(
            unsafe { RegSetValueExW(self.0, pcwstr(&wide), None, ty, Some(bytes)) },
            "write candidate bootstrap value",
        )?;
        // Ownership transitions are few and justify an explicit hive flush.
        // A failure/readback mismatch after this point is an uncertain write.
        win32_ok(
            unsafe { RegFlushKey(self.0) },
            "flush candidate bootstrap ownership",
        )?;
        ensure!(
            self.read(name, Some(ty))?.as_deref() == Some(bytes),
            "candidate bootstrap write verification failed"
        );
        Ok(())
    }

    fn write_json(&self, name: &str, value: &impl serde::Serialize) -> Result<()> {
        let text = serde_json::to_string(value)?;
        let bytes: Vec<u8> = text
            .encode_utf16()
            .chain([0])
            .flat_map(u16::to_le_bytes)
            .collect();
        self.write(name, REG_SZ, &bytes)
    }

    fn validate_security(&self, owner: &str, expected: &str) -> Result<()> {
        let mut descriptor = PSECURITY_DESCRIPTOR::default();
        win32_ok(
            unsafe {
                GetSecurityInfo(
                    HANDLE(self.0.0),
                    SE_REGISTRY_KEY,
                    OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                    None,
                    None,
                    None,
                    None,
                    Some(&mut descriptor),
                )
            },
            "read bootstrap root security",
        )?;
        validate_descriptor(&OwnedSd::from_raw(descriptor), owner, expected)
    }

    fn set_security(&self, sddl: &str) -> Result<()> {
        let descriptor = OwnedSd::from_sddl(sddl)?;
        win32_ok(
            unsafe {
                SetSecurityInfo(
                    HANDLE(self.0.0),
                    SE_REGISTRY_KEY,
                    DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                    None,
                    None,
                    Some(dacl(&descriptor)?),
                    None,
                )
            },
            "protect owned bootstrap root",
        )?;
        win32_ok(
            unsafe { RegFlushKey(self.0) },
            "flush bootstrap root security",
        )
    }
}

fn decode_text(bytes: &[u8]) -> Result<String> {
    ensure!(
        bytes.len() >= 2 && bytes.len() % 2 == 0,
        "invalid bootstrap string size"
    );
    let mut wide: Vec<u16> = bytes
        .chunks_exact(2)
        .map(|v| u16::from_le_bytes([v[0], v[1]]))
        .collect();
    ensure!(
        wide.pop() == Some(0) && !wide.contains(&0),
        "invalid bootstrap string terminator"
    );
    String::from_utf16(&wide).context("invalid bootstrap UTF-16")
}

fn dacl(descriptor: &OwnedSd) -> Result<*mut ACL> {
    let mut present = false.into();
    let mut defaulted = false.into();
    let mut acl = std::ptr::null_mut();
    unsafe {
        GetSecurityDescriptorDacl(descriptor.ptr, &mut present, &mut acl, &mut defaulted)?;
    }
    ensure!(
        present.as_bool() && !acl.is_null() && unsafe { IsValidAcl(acl) }.as_bool(),
        "bootstrap DACL absent, null or invalid"
    );
    Ok(acl)
}

fn aces(descriptor: &OwnedSd) -> Result<Vec<Vec<u8>>> {
    let acl = dacl(descriptor)?;
    let mut result = Vec::new();
    for index in 0..unsafe { (*acl).AceCount } {
        let mut pointer = std::ptr::null_mut();
        unsafe {
            GetAce(acl, u32::from(index), &mut pointer)?;
        }
        let length = unsafe { (*pointer.cast::<ACE_HEADER>()).AceSize } as usize;
        ensure!(
            length >= std::mem::size_of::<ACE_HEADER>(),
            "invalid bootstrap ACE"
        );
        result.push(unsafe { std::slice::from_raw_parts(pointer.cast::<u8>(), length) }.to_vec());
    }
    Ok(result)
}

fn validate_descriptor(descriptor: &OwnedSd, owner: &str, expected: &str) -> Result<()> {
    let mut control = 0;
    let mut revision = 0;
    unsafe {
        GetSecurityDescriptorControl(descriptor.ptr, &mut control, &mut revision)?;
    }
    ensure!(
        control & SE_DACL_PROTECTED.0 != 0,
        "bootstrap DACL is not protected"
    );
    let mut actual_owner = PSID::default();
    let mut defaulted = false.into();
    unsafe {
        GetSecurityDescriptorOwner(descriptor.ptr, &mut actual_owner, &mut defaulted)?;
    }
    ensure!(!actual_owner.0.is_null(), "bootstrap root owner is absent");
    let actual_owner = crate::sid::psid_to_string(actual_owner)?;
    ensure!(
        [owner, "S-1-5-18", "S-1-5-32-544"].contains(&actual_owner.as_str()),
        "bootstrap root has an untrusted owner"
    );
    ensure!(
        aces(descriptor)? == aces(&OwnedSd::from_sddl(expected)?)?,
        "bootstrap root DACL differs from exact policy"
    );
    Ok(())
}

pub(super) struct MachineStore {
    key: Key,
}

impl MachineStore {
    pub(super) fn create_fresh(owner: &str) -> Result<Self> {
        let path = wstr(REGISTRY_ROOT);
        let mut existing = HKEY::default();
        let status = unsafe {
            RegOpenKeyExW(
                HKEY_LOCAL_MACHINE,
                pcwstr(&path),
                Some(REG_OPTION_OPEN_LINK.0),
                KEY_READ | KEY_WOW64_64KEY,
                &mut existing,
            )
        };
        if status.is_ok() {
            drop(Key(existing));
            anyhow::bail!(
                "candidate root already exists; first install refuses ordinary keys and registry links"
            );
        }
        ensure!(
            status == ERROR_FILE_NOT_FOUND,
            "cannot establish candidate root absence: {status:?}"
        );
        // HKLM\\SOFTWARE is an administrator-controlled parent. The shared
        // lease excludes cooperating installers; a hostile administrator is
        // outside this boundary. Still require the atomic create disposition.
        let descriptor = OwnedSd::from_sddl(&bootstrap_sddl(owner)?)?;
        let attributes = SECURITY_ATTRIBUTES {
            nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: descriptor.ptr.0,
            bInheritHandle: false.into(),
        };
        let mut raw = HKEY::default();
        let mut disposition = Default::default();
        win32_ok(
            unsafe {
                RegCreateKeyExW(
                    HKEY_LOCAL_MACHINE,
                    pcwstr(&path),
                    None,
                    None,
                    REG_OPTION_NON_VOLATILE,
                    KEY_ALL_ACCESS | KEY_WOW64_64KEY,
                    Some(&attributes),
                    &mut raw,
                    Some(&mut disposition),
                )
            },
            "create candidate bootstrap root",
        )?;
        let key = Key(raw);
        ensure!(
            disposition == REG_CREATED_NEW_KEY,
            "candidate root already exists; first install refuses adoption"
        );
        key.validate_security(owner, &bootstrap_sddl(owner)?)?;
        Ok(Self { key })
    }

    pub(super) fn open_partial(owner: &str) -> Result<(Self, Ownership, u32)> {
        let mut raw = HKEY::default();
        let path = wstr(REGISTRY_ROOT);
        win32_ok(
            unsafe {
                RegOpenKeyExW(
                    HKEY_LOCAL_MACHINE,
                    pcwstr(&path),
                    Some(REG_OPTION_OPEN_LINK.0),
                    KEY_ALL_ACCESS | KEY_WOW64_64KEY,
                    &mut raw,
                )
            },
            "open candidate bootstrap root",
        )?;
        let key = Key(raw);
        ensure!(
            key.read("SymbolicLinkValue", None)?.is_none(),
            "candidate bootstrap root must not be a registry link"
        );
        let text = key
            .text("Bootstrap")?
            .context("bootstrap ownership is missing; explicit administrator repair required")?;
        let value: Ownership =
            serde_json::from_str(&text).context("invalid bootstrap ownership")?;
        value.validate()?;
        ensure!(value.record.owner_sid == owner, "bootstrap owner mismatch");
        let bootstrap = bootstrap_sddl(owner)?;
        if value.phase == Phase::Creating {
            key.validate_security(owner, &bootstrap)?;
        } else if key.validate_security(owner, &bootstrap).is_err() {
            key.validate_security(owner, &final_sddl(&value)?)?;
        }
        if let Some(text) = key.text("State")? {
            let state: SetupRecord = serde_json::from_str(&text)?;
            ensure!(
                value.phase != Phase::Creating && state == value.record,
                "candidate State was modified before bootstrap handoff"
            );
        }
        ensure!(
            key.text("CredentialLedger")?.is_none(),
            "bootstrap must not resume an owned credential transaction"
        );
        let marker = key.marker()?;
        Ok((Self { key }, value, marker))
    }
}

impl Store for MachineStore {
    fn observe(&self) -> Result<Observed> {
        sam::observe()
    }
    fn write_marker_zero(&mut self) -> Result<()> {
        self.key
            .write("MarkerVersion", REG_DWORD, &0u32.to_le_bytes())
    }
    fn write_ownership(&mut self, value: &Ownership) -> Result<()> {
        value.validate()?;
        self.key.write_json("Bootstrap", value)
    }
    fn create_principal(&mut self, principal: Principal) -> Result<String> {
        sam::create_only(principal)
    }
    fn protect_final_root(&mut self, value: &Ownership) -> Result<()> {
        value.validate()?;
        ensure!(
            value.phase == Phase::Finalizing,
            "bootstrap root policy requires finalizing ownership"
        );
        let policy = final_sddl(value)?;
        self.key.set_security(&policy)?;
        self.key.validate_security(&value.record.owner_sid, &policy)
    }
    fn write_state(&mut self, value: &SetupRecord) -> Result<()> {
        if let Some(text) = self.key.text("State")? {
            ensure!(
                serde_json::from_str::<SetupRecord>(&text)? == *value,
                "refusing to overwrite unrelated candidate State"
            );
        }
        self.key.write_json("State", value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const OWNER: &str = "S-1-5-21-1-2-3-1001";

    #[test]
    fn bootstrap_descriptor_rejects_broad_inherited_or_foreign_access() {
        let expected = bootstrap_sddl(OWNER).unwrap();
        validate_descriptor(&OwnedSd::from_sddl(&expected).unwrap(), OWNER, &expected).unwrap();
        for invalid in [
            expected.replace("D:P", "D:"),
            format!("{expected}(A;;KR;;;BU)"),
            expected.replace("(A;;KR;", "(A;;KA;"),
            expected.replace("O:BA", "O:S-1-5-21-1-2-3-9999"),
        ] {
            assert!(
                validate_descriptor(&OwnedSd::from_sddl(&invalid).unwrap(), OWNER, &expected)
                    .is_err()
            );
        }
    }

    #[test]
    fn bootstrap_strings_require_exact_bounded_utf16_structure() {
        assert_eq!(decode_text(&[b'a', 0, 0, 0]).unwrap(), "a");
        for invalid in [&[][..], &[0], &[b'a', 0], &[0, 0, 0, 0], &[0, 0xd8, 0, 0]] {
            assert!(decode_text(invalid).is_err());
        }
    }
}
