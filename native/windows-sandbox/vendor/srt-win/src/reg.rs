//! Machine-wide install state in `HKLM\SOFTWARE\alysis-sandbox`.
//!
//! The registry is the natural home for small machine-wide
//! key/value state written by an elevated installer and read
//! unprivileged, and its DEFAULT security model is exactly the one
//! the install state needs — properties a `%ProgramData%` file
//! store had to hand-build (and defend in review, repeatedly):
//!
//! - Standard users cannot create keys under `HKLM\SOFTWARE`, so
//!   there is nothing to squat: no take-ownership, no no-follow
//!   open, no reparse rejection for the store itself.
//! - Admin-write / Users-read is the inherited default, so the
//!   identity, marker, and ambient-deny values are trustworthy on
//!   read — no SAM cross-validation, no corrupt-file degrade
//!   paths, no schema-version machinery.
//!
//! Layout (all opens `KEY_WOW64_64KEY` so a 32-bit build would not
//! land in WOW6432Node):
//!
//! - `HKLM\SOFTWARE\alysis-sandbox`: `MarkerVersion` (DWORD,
//!   written LAST — registry writes are not transactional across
//!   values, so the marker is the commit point), `SandboxUser`,
//!   `SandboxUserSid`, `SandboxGroupSid` (SZ), `CreatedAt`
//!   (QWORD), `AmbientDenies` (MULTI_SZ).
//! - `…\CredOwner-<32 lowercase hex>`: one `Blob` (BINARY,
//!   machine-scope DPAPI), selected by the admin-written `CredKey` value.
//!   A fresh generation is created on every repair: tightening an old DACL
//!   cannot revoke already-open registry handles. Created
//!   with an explicit SA — DACL atomic with creation — SYSTEM/
//!   Administrators full, recorded owner READ (machine DPAPI means
//!   readable ⇒ decryptable), sandbox account and group DENY
//!   (the child must never learn its own password: with
//!   it, it could CreateProcessWithLogonW itself a fresh logon
//!   session outside the job/desktop confinement).
//! - `…\Ca`: one `Der` (BINARY). Users KEY_READ|KEY_SET_VALUE so
//!   the unelevated `trust-ca` can record the CA; sandbox group
//!   DENY so the CHILD cannot touch the record (the accepted
//!   replace-the-CA trade-off is scoped to REAL local users).

use anyhow::{Context, Result, anyhow, ensure};
use windows::Win32::Foundation::{ERROR_FILE_NOT_FOUND, HANDLE};
use windows::Win32::Security::Authorization::{
    GetSecurityInfo, SE_REGISTRY_KEY, SetNamedSecurityInfoW, SetSecurityInfo,
};
use windows::Win32::Security::{
    ACE_HEADER, ACL, DACL_SECURITY_INFORMATION, GetAce, GetSecurityDescriptorControl,
    GetSecurityDescriptorDacl, GetSecurityDescriptorOwner, IsValidAcl, OWNER_SECURITY_INFORMATION,
    PROTECTED_DACL_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, PSID, SE_DACL_PROTECTED,
};
use windows::Win32::System::Registry::{
    HKEY, HKEY_LOCAL_MACHINE, KEY_ALL_ACCESS, KEY_READ, KEY_WOW64_64KEY, KEY_WRITE, REG_BINARY,
    REG_DWORD, REG_MULTI_SZ, REG_OPENED_EXISTING_KEY, REG_OPTION_NON_VOLATILE, REG_QWORD, REG_SZ,
    RegCloseKey, RegCreateKeyExW, RegDeleteTreeW, RegOpenKeyExW, RegQueryValueExW, RegSetValueExW,
};

use crate::util::{OwnedSd, pcwstr, wstr};

pub const BASE: &str = r"SOFTWARE\alysis-sandbox";
pub const CRED_SUBKEY: &str = r"SOFTWARE\alysis-sandbox\Cred"; // legacy cleanup only
pub const CA_SUBKEY: &str = r"SOFTWARE\alysis-sandbox\Ca";

/// A registry reference is a single generated component, never an arbitrary
/// relative/absolute path. Legacy fixed keys are deliberately not accepted.
pub fn credential_generation_path(reference: &str) -> Result<String> {
    let suffix = reference.strip_prefix("CredOwner-").unwrap_or_default();
    ensure!(
        suffix.len() == 32
            && suffix
                .bytes()
                .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c)),
        "invalid credential generation reference; rerun elevated sandbox setup to repair"
    );
    Ok(format!(r"{BASE}\{reference}"))
}

fn require_fresh_credential_generation(created: bool) -> Result<()> {
    ensure!(
        created,
        "credential generation already exists; refusing to reuse an object with unknown open handles"
    );
    Ok(())
}

/// Create a fresh credential object with its final DACL atomically. Never
/// overwrite an existing object, even if its current ACL is now correct.
pub fn create_credential_generation(
    owner: &str,
    sandbox: &str,
    sandbox_group: &str,
) -> Result<(String, Key)> {
    use windows::Win32::Security::Cryptography::{
        BCRYPT_USE_SYSTEM_PREFERRED_RNG, BCryptGenRandom,
    };
    let sddl = credential_key_sddl(owner, sandbox, sandbox_group)?;
    let mut random = [0u8; 16];
    let status = unsafe { BCryptGenRandom(None, &mut random, BCRYPT_USE_SYSTEM_PREFERRED_RNG) };
    ensure!(
        status.0 == 0,
        "credential generation entropy failed: {status:?}"
    );
    let reference = format!(
        "CredOwner-{}",
        random
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>()
    );
    let path = credential_generation_path(&reference)?;
    let (key, created) = Key::create(&path, Some(&sddl))?;
    require_fresh_credential_generation(created)?;
    key.validate_credential_security(owner, sandbox, sandbox_group)?;
    Ok((reference, key))
}

/// Machine-scope DPAPI relies on this ACL for confidentiality. The caller's
/// single-user binding is checked separately before any credential value read.
pub fn credential_key_sddl(owner: &str, sandbox: &str, sandbox_group: &str) -> Result<String> {
    for sid in [owner, sandbox, sandbox_group] {
        crate::sid::LocalPsid::from_string(sid).context("invalid credential policy SID")?;
    }
    ensure!(
        owner != sandbox && owner != sandbox_group && sandbox != sandbox_group,
        "credential owner and sandbox identities must be distinct"
    );
    Ok(format!(
        "O:BAG:BAD:P(D;;KA;;;{sandbox})(D;;KA;;;{sandbox_group})\
         (A;;KA;;;SY)(A;;KA;;;BA)(A;;KR;;;{owner})"
    ))
}

/// RAII HKEY.
pub struct Key(HKEY);
impl Drop for Key {
    fn drop(&mut self) {
        unsafe {
            let _ = RegCloseKey(self.0);
        }
    }
}

impl Key {
    /// Candidate setup uses a durable intent/result ordering before filesystem
    /// ownership changes. Baseline callers and behavior are unchanged.
    #[cfg(feature = "account-appcontainer-candidate")]
    pub(crate) fn flush(&self) -> Result<()> {
        crate::util::win32_ok(
            unsafe { windows::Win32::System::Registry::RegFlushKey(self.0) },
            "flush candidate setup state",
        )
    }

    /// Validate the descriptor on THIS open handle, then let the caller read
    /// Blob through the same handle. Never repairs security from a read path.
    pub fn validate_credential_security(
        &self,
        owner: &str,
        sandbox: &str,
        sandbox_group: &str,
    ) -> Result<()> {
        let mut descriptor = PSECURITY_DESCRIPTOR::default();
        crate::util::win32_ok(
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
            "cannot read credential key security; rerun elevated sandbox setup to repair",
        )?;
        validate_credential_descriptor(
            &OwnedSd::from_raw(descriptor),
            owner,
            sandbox,
            sandbox_group,
        )
        .context("credential store permissions are unsafe; rerun elevated sandbox setup to repair")
    }

    /// Open an existing key. `Ok(None)` when absent.
    pub fn open(path: &str, write: bool) -> Result<Option<Key>> {
        let mut h = HKEY::default();
        let w = wstr(path);
        let access =
            KEY_READ | KEY_WOW64_64KEY | if write { KEY_WRITE } else { Default::default() };
        let r = unsafe { RegOpenKeyExW(HKEY_LOCAL_MACHINE, pcwstr(&w), None, access, &mut h) };
        if r == ERROR_FILE_NOT_FOUND {
            return Ok(None);
        }
        if r.is_err() {
            return Err(anyhow!("RegOpenKeyExW(HKLM\\{path}): {r:?}"));
        }
        Ok(Some(Key(h)))
    }

    /// Open an existing key for VALUE WRITES ONLY: `KEY_READ |
    /// KEY_SET_VALUE`, never `KEY_WRITE` — `KEY_WRITE` (0x20006)
    /// includes `KEY_CREATE_SUB_KEY` (0x4), which the `Ca` subkey's
    /// `BUILTIN\Users` allow mask (0x2001b) deliberately does not
    /// grant, so an unelevated `trust-ca` open requesting KEY_WRITE
    /// would be ACCESS_DENIED despite having exactly the rights it
    /// needs. `Ok(None)` when absent.
    pub fn open_for_set_value(path: &str) -> Result<Option<Key>> {
        use windows::Win32::System::Registry::KEY_SET_VALUE;
        let mut h = HKEY::default();
        let w = wstr(path);
        let access = KEY_READ | KEY_SET_VALUE | KEY_WOW64_64KEY;
        let r = unsafe { RegOpenKeyExW(HKEY_LOCAL_MACHINE, pcwstr(&w), None, access, &mut h) };
        if r == ERROR_FILE_NOT_FOUND {
            return Ok(None);
        }
        if r.is_err() {
            return Err(anyhow!("RegOpenKeyExW(HKLM\\{path}, set-value): {r:?}"));
        }
        Ok(Some(Key(h)))
    }

    /// Create (or open) a key, optionally with an explicit security
    /// descriptor (SDDL). Returns `(key, created)`. When the key
    /// already existed the SA is IGNORED by the API — callers that
    /// need guaranteed security should use [`ensure_key_with_dacl`],
    /// which repairs the owner and DACL through the returned handle.
    pub fn create(path: &str, sddl: Option<&str>) -> Result<(Key, bool)> {
        Self::create_under(HKEY_LOCAL_MACHINE, path, sddl)
    }

    fn create_under(root: HKEY, path: &str, sddl: Option<&str>) -> Result<(Key, bool)> {
        let mut h = HKEY::default();
        let mut disp = Default::default();
        let w = wstr(path);
        let sd;
        let mut sa = None;
        if let Some(sddl) = sddl {
            sd = OwnedSd::from_sddl(sddl).with_context(|| format!("SD for HKLM\\{path}"))?;
            sa = Some(windows::Win32::Security::SECURITY_ATTRIBUTES {
                nLength: std::mem::size_of::<windows::Win32::Security::SECURITY_ATTRIBUTES>()
                    as u32,
                lpSecurityDescriptor: sd.ptr.0,
                bInheritHandle: false.into(),
            });
        }
        let r = unsafe {
            RegCreateKeyExW(
                root,
                pcwstr(&w),
                None,
                None,
                REG_OPTION_NON_VOLATILE,
                (if sddl.is_some() {
                    KEY_ALL_ACCESS
                } else {
                    KEY_READ | KEY_WRITE
                }) | KEY_WOW64_64KEY,
                sa.as_ref().map(|s| s as *const _),
                &mut h,
                Some(&mut disp),
            )
        };
        if r.is_err() {
            return Err(anyhow!("RegCreateKeyExW(HKLM\\{path}): {r:?}"));
        }
        Ok((Key(h), disp != REG_OPENED_EXISTING_KEY))
    }

    fn set_raw(
        &self,
        name: &str,
        ty: windows::Win32::System::Registry::REG_VALUE_TYPE,
        data: &[u8],
    ) -> Result<()> {
        let w = wstr(name);
        let r = unsafe { RegSetValueExW(self.0, pcwstr(&w), None, ty, Some(data)) };
        if r.is_err() {
            return Err(anyhow!("RegSetValueExW({name}): {r:?}"));
        }
        Ok(())
    }

    pub fn set_sz(&self, name: &str, val: &str) -> Result<()> {
        let mut v: Vec<u16> = val.encode_utf16().collect();
        v.push(0);
        self.set_raw(name, REG_SZ, unsafe {
            std::slice::from_raw_parts(v.as_ptr() as *const u8, v.len() * 2)
        })
    }

    pub fn set_dword(&self, name: &str, val: u32) -> Result<()> {
        self.set_raw(name, REG_DWORD, &val.to_le_bytes())
    }

    pub fn set_qword(&self, name: &str, val: u64) -> Result<()> {
        self.set_raw(name, REG_QWORD, &val.to_le_bytes())
    }

    pub fn set_binary(&self, name: &str, val: &[u8]) -> Result<()> {
        self.set_raw(name, REG_BINARY, val)
    }

    pub fn set_multi_sz(&self, name: &str, vals: &[String]) -> Result<()> {
        let mut v: Vec<u16> = Vec::new();
        for s in vals {
            v.extend(s.encode_utf16());
            v.push(0);
        }
        v.push(0); // double-NUL terminator (also for the empty list)
        self.set_raw(name, REG_MULTI_SZ, unsafe {
            std::slice::from_raw_parts(v.as_ptr() as *const u8, v.len() * 2)
        })
    }

    fn get_raw(&self, name: &str) -> Result<Option<Vec<u8>>> {
        let w = wstr(name);
        let mut len: u32 = 0;
        let r = unsafe { RegQueryValueExW(self.0, pcwstr(&w), None, None, None, Some(&mut len)) };
        if r == ERROR_FILE_NOT_FOUND {
            return Ok(None);
        }
        if r.is_err() {
            return Err(anyhow!("RegQueryValueExW({name}) sizing: {r:?}"));
        }
        let mut buf = vec![0u8; len as usize];
        let mut len2 = len;
        let r = unsafe {
            RegQueryValueExW(
                self.0,
                pcwstr(&w),
                None,
                None,
                Some(buf.as_mut_ptr()),
                Some(&mut len2),
            )
        };
        if r.is_err() {
            return Err(anyhow!("RegQueryValueExW({name}): {r:?}"));
        }
        buf.truncate(len2 as usize);
        Ok(Some(buf))
    }

    pub fn get_sz(&self, name: &str) -> Result<Option<String>> {
        let Some(b) = self.get_raw(name)? else {
            return Ok(None);
        };
        let u16s: Vec<u16> = b
            .as_chunks::<2>()
            .0
            .iter()
            .map(|&c| u16::from_le_bytes(c))
            .take_while(|&c| c != 0)
            .collect();
        Ok(Some(String::from_utf16_lossy(&u16s)))
    }

    pub fn get_dword(&self, name: &str) -> Result<Option<u32>> {
        Ok(self
            .get_raw(name)?
            .filter(|b| b.len() >= 4)
            .map(|b| u32::from_le_bytes([b[0], b[1], b[2], b[3]])))
    }

    pub fn get_qword(&self, name: &str) -> Result<Option<u64>> {
        Ok(self
            .get_raw(name)?
            .filter(|b| b.len() >= 8)
            .map(|b| u64::from_le_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]])))
    }

    pub fn get_binary(&self, name: &str) -> Result<Option<Vec<u8>>> {
        self.get_raw(name)
    }

    pub fn get_multi_sz(&self, name: &str) -> Result<Option<Vec<String>>> {
        let Some(b) = self.get_raw(name)? else {
            return Ok(None);
        };
        let u16s: Vec<u16> = b
            .as_chunks::<2>()
            .0
            .iter()
            .map(|&c| u16::from_le_bytes(c))
            .collect();
        let mut out = Vec::new();
        for part in u16s.split(|&c| c == 0) {
            if !part.is_empty() {
                out.push(String::from_utf16_lossy(part));
            }
        }
        Ok(Some(out))
    }
}

fn descriptor_dacl(sd: &OwnedSd) -> Result<*mut ACL> {
    let mut present = windows::core::BOOL::from(false);
    let mut dacl = std::ptr::null_mut();
    let mut defaulted = windows::core::BOOL::from(false);
    unsafe {
        GetSecurityDescriptorDacl(sd.ptr, &mut present, &mut dacl, &mut defaulted)?;
    }
    ensure!(
        present.as_bool() && !dacl.is_null(),
        "credential key has a null or absent DACL"
    );
    ensure!(
        unsafe { IsValidAcl(dacl) }.as_bool(),
        "credential key has an invalid DACL"
    );
    Ok(dacl)
}

fn descriptor_aces(sd: &OwnedSd) -> Result<Vec<Vec<u8>>> {
    let dacl = descriptor_dacl(sd)?;
    let count = unsafe { (*dacl).AceCount };
    let mut aces = Vec::with_capacity(count as usize);
    for index in 0..count {
        let mut raw = std::ptr::null_mut();
        unsafe {
            GetAce(dacl, index as u32, &mut raw)?;
        }
        let header = unsafe { &*raw.cast::<ACE_HEADER>() };
        ensure!(
            header.AceSize as usize >= std::mem::size_of::<ACE_HEADER>(),
            "invalid credential ACE"
        );
        aces.push(
            unsafe { std::slice::from_raw_parts(raw.cast::<u8>(), header.AceSize as usize) }
                .to_vec(),
        );
    }
    Ok(aces)
}

fn validate_credential_descriptor(
    descriptor: &OwnedSd,
    owner: &str,
    sandbox: &str,
    sandbox_group: &str,
) -> Result<()> {
    let expected = OwnedSd::from_sddl(&credential_key_sddl(owner, sandbox, sandbox_group)?)?;
    let mut control = 0;
    let mut revision = 0;
    unsafe {
        GetSecurityDescriptorControl(descriptor.ptr, &mut control, &mut revision)?;
    }
    ensure!(
        control & SE_DACL_PROTECTED.0 != 0,
        "credential DACL inheritance is not protected"
    );
    let mut actual_owner = PSID::default();
    let mut defaulted = windows::core::BOOL::from(false);
    unsafe {
        GetSecurityDescriptorOwner(descriptor.ptr, &mut actual_owner, &mut defaulted)?;
    }
    ensure!(!actual_owner.0.is_null(), "credential key has no owner");
    let actual_owner = crate::sid::psid_to_string(actual_owner)?;
    ensure!(
        [owner, "S-1-5-18", "S-1-5-32-544"].contains(&actual_owner.as_str()),
        "credential key has an untrusted owner"
    );
    // Require the exact deny-first policy. This excludes inherited, callback,
    // object-specific, extra-principal and broadened owner grants rather than
    // trying to infer whether an arbitrary registry DACL is equivalent.
    ensure!(
        descriptor_aces(descriptor)? == descriptor_aces(&expected)?,
        "credential DACL differs from the owner-only policy"
    );
    Ok(())
}

fn set_key_security_from_sddl(key: &Key, sddl: &str) -> Result<()> {
    let sd = OwnedSd::from_sddl(sddl)?;
    let dacl = descriptor_dacl(&sd)?;
    let mut owner = PSID::default();
    let mut defaulted = windows::core::BOOL::from(false);
    unsafe {
        GetSecurityDescriptorOwner(sd.ptr, &mut owner, &mut defaulted)?;
    }
    let mut information = DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION;
    if !owner.0.is_null() {
        information |= OWNER_SECURITY_INFORMATION;
    }
    crate::util::win32_ok(
        unsafe {
            SetSecurityInfo(
                HANDLE(key.0.0),
                SE_REGISTRY_KEY,
                information,
                (!owner.0.is_null()).then_some(owner),
                None,
                Some(dacl),
                None,
            )
        },
        "repair registry key security through open handle",
    )
}

/// Rewrite a key's DACL from SDDL (PROTECTED). Drift-healing for
/// open-existing paths — see [`Key::create`].
pub fn set_key_dacl_from_sddl(path: &str, sddl: &str) -> Result<()> {
    let sd = OwnedSd::from_sddl(sddl).with_context(|| format!("SD for HKLM\\{path}"))?;
    let mut present = windows::core::BOOL::from(false);
    let mut dacl = std::ptr::null_mut();
    let mut defaulted = windows::core::BOOL::from(false);
    unsafe {
        GetSecurityDescriptorDacl(sd.ptr, &mut present, &mut dacl, &mut defaulted)
            .with_context(|| format!("GetSecurityDescriptorDacl for HKLM\\{path}"))?;
    }
    if !present.as_bool() || dacl.is_null() {
        anyhow::bail!("SDDL '{sddl}' yielded no DACL");
    }
    let full = format!(r"MACHINE\{path}");
    let w = wstr(&full);
    let r = unsafe {
        SetNamedSecurityInfoW(
            pcwstr(&w),
            SE_REGISTRY_KEY,
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            Some(dacl),
            None,
        )
    };
    crate::util::win32_ok(r, &format!("SetNamedSecurityInfoW(HKLM\\{path})"))
}

/// Delete the whole `HKLM\SOFTWARE\alysis-sandbox` tree
/// (uninstall). No-op when absent.
pub fn delete_tree() -> Result<()> {
    let w = wstr(BASE);
    let r = unsafe { RegDeleteTreeW(HKEY_LOCAL_MACHINE, pcwstr(&w)) };
    if r == ERROR_FILE_NOT_FOUND || r.is_ok() {
        return Ok(());
    }
    Err(anyhow!("RegDeleteTreeW(HKLM\\{BASE}): {r:?}"))
}

/// Create the key with `sddl` atomically, or — when it already
/// exists (RegCreateKeyExW IGNORES the SA then; the rotation path)
/// — rewrite its owner and protected DACL to `sddl` through the same
/// handle. One primitive so no call site can
/// take the create half without the heal half. Only admins can have
/// created a key under HKLM\SOFTWARE, so the heal is drift repair,
/// not squat defense.
pub fn ensure_key_with_dacl(path: &str, sddl: &str) -> Result<Key> {
    let (key, created) = Key::create(path, Some(sddl))?;
    if !created {
        set_key_security_from_sddl(&key, sddl)?;
    }
    Ok(key)
}

/// Delete one subkey tree under HKLM. No-op when absent.
pub fn delete_subtree(path: &str) -> Result<()> {
    let w = wstr(path);
    let r = unsafe { RegDeleteTreeW(HKEY_LOCAL_MACHINE, pcwstr(&w)) };
    if r == ERROR_FILE_NOT_FOUND || r.is_ok() {
        return Ok(());
    }
    Err(anyhow!("RegDeleteTreeW(HKLM\\{path}): {r:?}"))
}

/// Delete one value from an open key. No-op when absent.
pub fn delete_value(key: &Key, name: &str) -> Result<()> {
    use windows::Win32::System::Registry::RegDeleteValueW;
    let w = wstr(name);
    let r = unsafe { RegDeleteValueW(key.0, pcwstr(&w)) };
    if r == ERROR_FILE_NOT_FOUND || r.is_ok() {
        return Ok(());
    }
    Err(anyhow!("RegDeleteValueW({name}): {r:?}"))
}

#[cfg(test)]
mod credential_tests {
    use super::*;
    const OWNER: &str = "S-1-5-21-1-2-3-1001";
    const ACCOUNT: &str = "S-1-5-21-1-2-3-1002";
    const GROUP: &str = "S-1-5-21-1-2-3-1003";

    #[test]
    fn credential_generation_reference_and_collision_policy_fail_closed() {
        let reference = "CredOwner-0123456789abcdef0123456789abcdef";
        assert_eq!(
            credential_generation_path(reference).unwrap(),
            format!(r"{BASE}\{reference}")
        );
        for invalid in [
            "",
            "Cred",
            "CredOwner-",
            "CredOwner-0123456789ABCDEF0123456789ABCDEF",
            "CredOwner-0123456789abcdef0123456789abcde",
            "CredOwner-0123456789abcdef0123456789abcdef0",
            "CredOwner-0123456789abcdef0123456789abcdeg",
            r"CredOwner-0123456789abcdef0123456789abcdef\child",
            r"..\CredOwner-0123456789abcdef0123456789abcdef",
            r"SOFTWARE\other\CredOwner-0123456789abcdef0123456789abcdef",
        ] {
            assert!(
                credential_generation_path(invalid).is_err(),
                "accepted {invalid:?}"
            );
        }
        require_fresh_credential_generation(true).unwrap();
        assert!(require_fresh_credential_generation(false).is_err());
    }

    #[test]
    fn credential_generation_keeps_old_handles_away_from_new_synthetic_value() {
        use windows::Win32::Security::Cryptography::{
            BCRYPT_USE_SYSTEM_PREFERRED_RNG, BCryptGenRandom,
        };
        use windows::Win32::System::Registry::HKEY_CURRENT_USER;
        let mut random = [0u8; 16];
        assert_eq!(
            unsafe { BCryptGenRandom(None, &mut random, BCRYPT_USE_SYSTEM_PREFERRED_RNG) }.0,
            0
        );
        let suffix = random
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>();
        let fixture = format!(r"Software\AlysisSandboxCredentialTest-{suffix}");
        struct Cleanup {
            path: String,
            children: [String; 2],
            restore_sddl: String,
        }
        impl Cleanup {
            fn remove(&self) -> Result<()> {
                for child in &self.children {
                    let path = wstr(child);
                    let mut handle = HKEY::default();
                    // Object owners retain WRITE_DAC even after the test has
                    // narrowed value access to KEY_READ. Restore only these
                    // synthetic children so recursive deletion can complete.
                    let access = KEY_READ
                        | KEY_WOW64_64KEY
                        | windows::Win32::System::Registry::REG_SAM_FLAGS(0x40000);
                    let result = unsafe {
                        RegOpenKeyExW(HKEY_CURRENT_USER, pcwstr(&path), None, access, &mut handle)
                    };
                    if result == ERROR_FILE_NOT_FOUND {
                        continue;
                    }
                    crate::util::win32_ok(result, "open synthetic fixture for cleanup")?;
                    set_key_security_from_sddl(&Key(handle), &self.restore_sddl)?;
                }
                let path = wstr(&self.path);
                let result = unsafe { RegDeleteTreeW(HKEY_CURRENT_USER, pcwstr(&path)) };
                if result == ERROR_FILE_NOT_FOUND {
                    return Ok(());
                }
                crate::util::win32_ok(result, "remove synthetic credential fixture")
            }
        }
        impl Drop for Cleanup {
            fn drop(&mut self) {
                if let Err(error) = self.remove() {
                    eprintln!("synthetic credential fixture cleanup failed: {error:#}");
                }
            }
        }
        let owner = crate::sid::current_user_sid().unwrap();
        let initial = format!("O:{owner}D:P(A;;KA;;;{owner})");
        let (base, created) =
            Key::create_under(HKEY_CURRENT_USER, &fixture, Some(&initial)).unwrap();
        assert!(created);
        let old_path = format!(r"{fixture}\Cred");
        let reference = format!("CredOwner-{suffix}");
        let new_path = format!(r"{fixture}\{reference}");
        let cleanup = Cleanup {
            path: fixture.clone(),
            children: [old_path.clone(), new_path.clone()],
            restore_sddl: format!("D:P(A;;KA;;;{owner})"),
        };
        let (old, created) =
            Key::create_under(HKEY_CURRENT_USER, &old_path, Some(&initial)).unwrap();
        assert!(created);
        old.set_binary("Blob", b"old synthetic credential").unwrap();
        let mut handle = HKEY::default();
        let old_w = wstr(&old_path);
        assert!(
            unsafe {
                RegOpenKeyExW(
                    HKEY_CURRENT_USER,
                    pcwstr(&old_w),
                    None,
                    KEY_READ | KEY_WOW64_64KEY,
                    &mut handle,
                )
            }
            .is_ok()
        );
        let old_reader = Key(handle);
        // Unelevated HKCU tests use the current user as owner (accepted by the
        // validator); all ACE bytes match production. No real secrets involved.
        let policy = credential_key_sddl(&owner, ACCOUNT, GROUP)
            .unwrap()
            .replacen("O:BA", &format!("O:{owner}"), 1);
        set_key_security_from_sddl(&old, &policy).unwrap();
        old.validate_credential_security(&owner, ACCOUNT, GROUP)
            .unwrap();
        // Existing write handles retain rights across a DACL tightening too.
        old.set_binary("Blob", b"old synthetic credential retained")
            .unwrap();
        credential_generation_path(&reference).unwrap();
        let (new, created) =
            Key::create_under(HKEY_CURRENT_USER, &new_path, Some(&initial)).unwrap();
        require_fresh_credential_generation(created).unwrap();
        new.set_binary("Blob", b"synthetic value before collision")
            .unwrap();
        let (_, collision_created) =
            Key::create_under(HKEY_CURRENT_USER, &new_path, Some(&initial)).unwrap();
        assert!(require_fresh_credential_generation(collision_created).is_err());
        assert_eq!(
            new.get_binary("Blob").unwrap().unwrap(),
            b"synthetic value before collision"
        );
        set_key_security_from_sddl(&new, &policy).unwrap();
        new.validate_credential_security(&owner, ACCOUNT, GROUP)
            .unwrap();
        new.set_binary("Blob", b"new synthetic credential").unwrap();
        base.set_sz("CredKey", &reference).unwrap();
        assert_eq!(
            base.get_sz("CredKey").unwrap().as_deref(),
            Some(reference.as_str())
        );
        assert_eq!(
            old_reader.get_binary("Blob").unwrap().unwrap(),
            b"old synthetic credential retained"
        );
        assert_eq!(
            new.get_binary("Blob").unwrap().unwrap(),
            b"new synthetic credential"
        );
        drop(old_reader);
        drop(old);
        drop(new);
        drop(base);
        cleanup.remove().unwrap();
    }

    #[test]
    fn credential_descriptor_accepts_only_protected_owner_policy() {
        let expected = credential_key_sddl(OWNER, ACCOUNT, GROUP).unwrap();
        for owner in ["BA", "SY", OWNER] {
            let actual = expected.replacen("O:BA", &format!("O:{owner}"), 1);
            validate_credential_descriptor(
                &OwnedSd::from_sddl(&actual).unwrap(),
                OWNER,
                ACCOUNT,
                GROUP,
            )
            .unwrap();
        }
        let invalid = [
            expected.replace("D:P", "D:"),
            format!("{expected}(A;;KR;;;BU)"),
            format!("{expected}(A;;KR;;;WD)"),
            expected.replace(&format!("(D;;KA;;;{ACCOUNT})"), ""),
            expected.replace(&format!("(D;;KA;;;{GROUP})"), ""),
            expected.replace(&format!("(A;;KR;;;{OWNER})"), &format!("(A;;KA;;;{OWNER})")),
            expected.replace(
                &format!("(A;;KR;;;{OWNER})"),
                &format!("(A;ID;KR;;;{OWNER})"),
            ),
            expected.replacen("O:BA", "O:S-1-5-21-1-2-3-9999", 1),
            format!("O:BAG:BAD:P(D;;KA;;;{GROUP})(A;;KA;;;SY)(A;;KA;;;BA)(A;;KR;;;BU)"),
            "O:BAG:BAD:NO_ACCESS_CONTROL".to_owned(),
        ];
        for actual in invalid {
            assert!(
                validate_credential_descriptor(
                    &OwnedSd::from_sddl(&actual).unwrap(),
                    OWNER,
                    ACCOUNT,
                    GROUP
                )
                .is_err(),
                "accepted unsafe descriptor: {actual}"
            );
        }
        assert!(credential_key_sddl(OWNER, OWNER, GROUP).is_err());
        assert!(credential_key_sddl("not a SID", ACCOUNT, GROUP).is_err());
    }

    // Use the current user's token with an additional read restriction keyed
    // only to its user SID. This makes the ordinary-user row deterministic even
    // when tests run elevated; no account provisioning or registry access.
    fn owner_only_test_token() -> crate::util::OwnedHandle {
        use windows::Win32::Security::{
            CreateRestrictedToken, DISABLE_MAX_PRIVILEGE, DuplicateTokenEx, SID_AND_ATTRIBUTES,
            SecurityImpersonation, TOKEN_DUPLICATE, TOKEN_QUERY, TokenImpersonation,
        };
        use windows::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};
        let mut primary = HANDLE::default();
        unsafe {
            OpenProcessToken(
                GetCurrentProcess(),
                TOKEN_QUERY | TOKEN_DUPLICATE,
                &mut primary,
            )
            .unwrap();
        }
        let primary = crate::util::OwnedHandle(primary);
        let mut impersonation = HANDLE::default();
        unsafe {
            DuplicateTokenEx(
                primary.raw(),
                TOKEN_QUERY | TOKEN_DUPLICATE,
                None,
                SecurityImpersonation,
                TokenImpersonation,
                &mut impersonation,
            )
            .unwrap();
        }
        let impersonation = crate::util::OwnedHandle(impersonation);
        let sid =
            crate::sid::LocalPsid::from_string(&crate::sid::current_user_sid().unwrap()).unwrap();
        let restrictions = [SID_AND_ATTRIBUTES {
            Sid: sid.as_psid(),
            Attributes: 0,
        }];
        let mut restricted = HANDLE::default();
        unsafe {
            CreateRestrictedToken(
                impersonation.raw(),
                DISABLE_MAX_PRIVILEGE,
                None,
                None,
                Some(&restrictions),
                &mut restricted,
            )
            .unwrap();
        }
        crate::util::OwnedHandle(restricted)
    }

    fn allows(token: HANDLE, sddl: &str, desired: u32) -> bool {
        use windows::Win32::Security::{AccessCheck, GENERIC_MAPPING, PRIVILEGE_SET};
        let sd = OwnedSd::from_sddl(sddl).unwrap();
        let mapping = GENERIC_MAPPING {
            GenericRead: KEY_READ.0,
            GenericWrite: KEY_WRITE.0,
            GenericExecute: KEY_READ.0,
            GenericAll: KEY_ALL_ACCESS.0,
        };
        let mut privileges = PRIVILEGE_SET::default();
        let mut privilege_size = std::mem::size_of::<PRIVILEGE_SET>() as u32;
        let mut granted = 0;
        let mut allowed = windows::core::BOOL::from(false);
        unsafe {
            AccessCheck(
                sd.ptr,
                token,
                desired,
                &mapping,
                Some(&mut privileges),
                &mut privilege_size,
                &mut granted,
                &mut allowed,
            )
            .unwrap();
        }
        allowed.as_bool()
    }

    #[test]
    fn credential_owner_reads_and_both_sandbox_identities_are_denied() {
        let token = owner_only_test_token();
        let me = crate::sid::current_user_sid().unwrap();
        let owner_policy = credential_key_sddl(&me, ACCOUNT, GROUP).unwrap();
        assert!(allows(token.raw(), &owner_policy, KEY_READ.0));
        if me != "S-1-5-18" {
            assert!(!allows(token.raw(), &owner_policy, KEY_WRITE.0));
        }
        let foreign_policy = credential_key_sddl(OWNER, ACCOUNT, GROUP).unwrap();
        assert_eq!(
            allows(token.raw(), &foreign_policy, KEY_READ.0),
            me == "S-1-5-18"
        );
        let account_policy = credential_key_sddl(OWNER, &me, GROUP).unwrap();
        assert!(!allows(token.raw(), &account_policy, KEY_READ.0));
        let group_policy = credential_key_sddl(OWNER, ACCOUNT, &me).unwrap();
        assert!(!allows(token.raw(), &group_policy, KEY_READ.0));
    }
}
