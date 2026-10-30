//! ACL helpers for the sandbox-user FS model — `srt-win acl
//! stamp|grant|restore|revoke|recover`.
//!
//! The model is **additive explicit ACEs for the dedicated
//! `alysis-sandbox` user SID**, never a `PROTECTED` rewrite or SD
//! snapshot:
//!
//! - `grant` ⇒ `(A;OICI;READ_EXEC|MODIFY_NO_FDC;;;<sb-SID>)` so the
//!   sandbox user (which has no inherent rights on real-user-owned
//!   files) can reach the working tree;
//! - `stamp` ⇒ `(D;OICI;mask;;;<sb-SID>)` on the target plus
//!   `(D;;FILE_DELETE_CHILD;;;<sb-SID>)` on the parent;
//! - install-time ambient write-denies (`ambient.rs`) reuse the
//!   `stamp` deny shape, recorded in `ambient_denies` with no holder
//!   so they persist across sessions until `uninstall`.
//!
//! Restore = walk the path's explicit ACEs, drop any whose trustee
//! is `<sb-SID>`, write back preserving the DACL's protection state
//! (`UNPROTECTED` re-derives inherited ACEs; a `SE_DACL_PROTECTED`
//! path keeps its severed inheritance). The single chokepoint is
//! [`apply_sandbox_aces`] ([`SbAceSet`]): converge the path to
//! exactly the wanted ALLOW + DENY for `<sb-SID>`, idempotently.
//!
//! The PROTECTED allow-lists in [`set_path_dacl_from_sddl`]'s
//! callers are the ONE remaining `PROTECTED` consumer — they
//! protect the state stores from the sandbox child, not user
//! files. (Cross-broker serialization uses a file lock inside the
//! protected session dir, not a named mutex — named objects are
//! create-first squattable by a same-session sandbox child.)
//!
//! Globs are **rejected**. Directory targets get `(OI)(CI)` ACEs
//! so the additive grant/deny inherits to the whole subtree.

use anyhow::{Context, Result, anyhow, bail, ensure};
use std::ffi::c_void;
use std::mem::size_of;
use windows::Win32::Security::Authorization::{
    GetNamedSecurityInfoW, GetSecurityInfo, SE_FILE_OBJECT, SetNamedSecurityInfoW, SetSecurityInfo,
};
use windows::Win32::Security::{
    ACE_FLAGS, ACE_HEADER, ACE_REVISION, ACL, ACL_REVISION, ACL_SIZE_INFORMATION,
    AclSizeInformation, AddAccessAllowedAceEx, AddAccessDeniedAceEx, AddAce, CONTAINER_INHERIT_ACE,
    DACL_SECURITY_INFORMATION, GetAce, GetAclInformation, GetLengthSid,
    GetSecurityDescriptorControl, InitializeAcl, InitializeSecurityDescriptor, OBJECT_INHERIT_ACE,
    OWNER_SECURITY_INFORMATION, PROTECTED_DACL_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, PSID,
    SE_DACL_PROTECTED, SECURITY_ATTRIBUTES, SECURITY_DESCRIPTOR, SetSecurityDescriptorControl,
    SetSecurityDescriptorDacl, UNPROTECTED_DACL_SECURITY_INFORMATION,
};
use windows::Win32::Storage::FileSystem::{
    FILE_ALL_ACCESS, FILE_GENERIC_EXECUTE, FILE_GENERIC_READ,
};
use windows::Win32::System::SystemServices::{
    ACCESS_DENIED_ACE_TYPE, CRITICAL_ACE_FLAG, SECURITY_DESCRIPTOR_REVISION,
};

use crate::sid::LocalPsid;
use crate::util::{OwnedSd, pcwstr, win32_ok, wstr};

// Deliberately isolated from the baseline inheritable-ACL implementation.
#[cfg(feature = "account-appcontainer-candidate")]
#[path = "object_grants.rs"]
pub mod object_grants;

/// Owner-Rights well-known SID. ANY ACE for this SID replaces
/// the kernel's implicit `READ_CONTROL|WRITE_DAC` grant to the
/// owner with exactly the ACE's mask.
pub const SID_OWNER_RIGHTS: &str = "S-1-3-4";
pub const SID_SYSTEM: &str = "S-1-5-18";
pub const SID_BUILTIN_ADMINS: &str = "S-1-5-32-544";
pub const SID_BUILTIN_USERS: &str = "S-1-5-32-545";

// ─── DACL builder primitives ────────────────────────────────────────
// The policy functions below declare ACE lists as `&[Allow]`; this
// section turns them into a self-owning ACL buffer. `Mask`'s field
// is private and `Allow::OWNER_RIGHTS` takes no mask, so neither a
// hex-typo (`0x0130_01bf` for `0x0013_01bf`) nor a mask-`0`
// `OWNER_RIGHTS` ACE (which `SetNamedSecurityInfoW` drops on write
// — a silent sandbox escape) is spellable from policy code.

/// Access mask. Construct via the named consts and
/// [`Mask::with`]/[`Mask::without`]; the inner `u32` is private so
/// hex literals at call sites are not possible.
#[derive(Copy, Clone, PartialEq, Eq, Debug)]
pub struct Mask(u32);

impl Mask {
    // Standard rights.
    pub const DELETE: Self = Self(0x0001_0000);
    pub const READ_CONTROL: Self = Self(0x0002_0000);
    pub const WRITE_DAC: Self = Self(0x0004_0000);
    pub const WRITE_OWNER: Self = Self(0x0008_0000);
    pub const SYNCHRONIZE: Self = Self(0x0010_0000);

    // Generic — resolved via the object's GENERIC_MAPPING.
    pub const GENERIC_ALL: Self = Self(0x1000_0000);

    // File/dir-specific.
    pub const FILE_DELETE_CHILD: Self = Self(0x0000_0040);
    pub const FILE_ALL: Self = Self(FILE_ALL_ACCESS.0);
    pub const FILE_WRITE_ATTRIBUTES: Self = Self(0x0000_0100);
    pub const FILE_GENERIC_READ: Self = Self(FILE_GENERIC_READ.0);
    pub const FILE_GENERIC_WRITE: Self =
        Self(windows::Win32::Storage::FileSystem::FILE_GENERIC_WRITE.0);
    pub const FILE_GENERIC_EXECUTE: Self = Self(FILE_GENERIC_EXECUTE.0);

    /// `FILE_GENERIC_READ | FILE_GENERIC_EXECUTE` — the
    /// [`GrantMask::ReadOnly`] mask.
    pub const FILE_READ_EXEC: Self = Self::FILE_GENERIC_READ.with(Self::FILE_GENERIC_EXECUTE);

    /// `FileSystemRights.Modify` MINUS `FILE_DELETE_CHILD` — the
    /// working-tree [`GrantMask::Modify`] mask granted to the
    /// sandbox-user SID via `acl grant`. The sandbox user can
    /// create/write/read/delete non-protected siblings (`DELETE`
    /// is in there) but `FILE_DELETE_CHILD` is withheld so a
    /// denied file inside the tree stays protected — the
    /// parent-FDC DENY (`SbAce::DenyFdc`) is evaluated first, but
    /// withholding it from the grant too is defense-in-depth.
    pub const MODIFY_NO_FDC: Self = Self::FILE_GENERIC_READ
        .with(Self::FILE_GENERIC_WRITE)
        .with(Self::FILE_GENERIC_EXECUTE)
        .with(Self::DELETE)
        .without(Self::FILE_DELETE_CHILD);

    pub const fn bits(self) -> u32 {
        self.0
    }
    pub const fn with(self, m: Self) -> Self {
        Self(self.0 | m.0)
    }
    pub const fn without(self, m: Self) -> Self {
        Self(self.0 & !m.0)
    }
}

impl std::ops::BitOr for Mask {
    type Output = Self;
    fn bitor(self, r: Self) -> Self {
        self.with(r)
    }
}

/// `(OI)(CI)` inheritance for directory-target ACEs.
pub const OICI: ACE_FLAGS = ACE_FLAGS(CONTAINER_INHERIT_ACE.0 | OBJECT_INHERIT_ACE.0);
/// No inheritance — the ACE applies to the object itself only.
pub const NO_INHERIT: ACE_FLAGS = ACE_FLAGS(0);

/// One row in an `&[Allow]` ACE list — `(SID, mask, inherit-flags)`.
/// The SID is a string so the policy function reads as an
/// SDDL-style table; [`build_allow_dacl`] does the
/// `ConvertStringSidToSidW` parsing once.
#[derive(Copy, Clone)]
pub struct Allow<'a>(pub &'a str, pub Mask, pub ACE_FLAGS);

impl Allow<'static> {
    /// The only `OWNER_RIGHTS` ACE this crate emits. Mask is fixed
    /// at `READ_CONTROL` — suppresses owner-implicit `WRITE_DAC`
    /// (so an owner-child cannot rewrite the DACL) while still
    /// letting the owner read it. **The mask must be non-zero**:
    /// `SetNamedSecurityInfoW` silently drops a mask-0 ALLOW ACE on
    /// write, so the conceptually-purer `OWNER_RIGHTS:0` never
    /// reaches disk. With this const + `Mask`'s private field, the
    /// mask-0 mistake is unspellable from policy code.
    pub const OWNER_RIGHTS: Self = Allow(SID_OWNER_RIGHTS, Mask::READ_CONTROL, NO_INHERIT);
}

/// Self-owning ACL: `buf` holds the `ACL` header + ACEs.
/// `AddAccessAllowedAceEx` copies SID bytes inline into each ACE
/// (`SidStart` embeds the SID — see the size calc in
/// [`build_allow_dacl`]), so once built `buf` is self-contained.
pub struct BuiltAcl {
    buf: Vec<u8>,
}

impl BuiltAcl {
    pub fn as_ptr(&self) -> *const ACL {
        self.buf.as_ptr() as *const ACL
    }

    /// Wrap this ACL in an absolute-format SD with `SE_DACL_PROTECTED`
    /// set, plus a `SECURITY_ATTRIBUTES` borrowing it — for
    /// `CreateMutexExW` and similar object-creation APIs that take
    /// a `*const SECURITY_ATTRIBUTES`. The returned [`OwnedSa`] owns
    /// the ACL, the SD, and the SA; pass [`OwnedSa::as_ptr`] and
    /// keep the [`OwnedSa`] alive until the call returns.
    ///
    /// No `O:`/`G:` (owner/group): an explicit owner SID at object
    /// creation goes through `SeAssignSecurity`, which rejects any
    /// owner that isn't the caller's user / an `SE_GROUP_OWNER`
    /// group / a `SeRestorePrivilege`-enabled token
    /// (`ERROR_INVALID_OWNER`); leaving them unset defaults
    /// owner/group to the caller, which is what we want.
    pub fn into_security_attributes(self) -> Result<OwnedSa> {
        let mut sd: Box<SECURITY_DESCRIPTOR> = Box::default();
        let psd = PSECURITY_DESCRIPTOR(&mut *sd as *mut _ as *mut c_void);
        unsafe {
            InitializeSecurityDescriptor(psd, SECURITY_DESCRIPTOR_REVISION)
                .context("InitializeSecurityDescriptor")?;
            SetSecurityDescriptorDacl(psd, true, Some(self.as_ptr()), false)
                .context("SetSecurityDescriptorDacl")?;
            SetSecurityDescriptorControl(psd, SE_DACL_PROTECTED, SE_DACL_PROTECTED)
                .context("SetSecurityDescriptorControl(PROTECTED)")?;
        }
        let sa = SECURITY_ATTRIBUTES {
            nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: psd.0,
            bInheritHandle: false.into(),
        };
        Ok(OwnedSa {
            _acl: self,
            _sd: sd,
            sa,
        })
    }
}

/// A `SECURITY_ATTRIBUTES` with its backing SD and ACL. Heap-pins
/// the SD via `Box` so `sa.lpSecurityDescriptor` stays valid across
/// moves of the [`OwnedSa`] itself. Do not `Clone`.
pub struct OwnedSa {
    _acl: BuiltAcl,
    _sd: Box<SECURITY_DESCRIPTOR>,
    sa: SECURITY_ATTRIBUTES,
}

impl OwnedSa {
    pub fn as_ptr(&self) -> *const SECURITY_ATTRIBUTES {
        &self.sa
    }
}

/// Build an ALLOW-only DACL from an ACE table. Dedups by SID
/// (case-insensitive; first occurrence wins).
pub fn build_allow_dacl(aces: &[Allow<'_>]) -> Result<BuiltAcl> {
    // Parse SIDs first so the ACL sizing loop has the lengths.
    // Dedup by SID string (case-insensitive) — first wins.
    let mut seen = std::collections::HashSet::new();
    let parsed: Vec<(LocalPsid, u32, ACE_FLAGS)> = aces
        .iter()
        .filter(|Allow(s, _, _)| seen.insert(s.to_ascii_uppercase()))
        .map(|Allow(sid_str, m, fl)| {
            let sid = LocalPsid::from_string(sid_str)
                .with_context(|| format!("parse SID '{sid_str}'"))?;
            if unsafe { GetLengthSid(sid.as_psid()) } == 0 {
                bail!("GetLengthSid('{sid_str}') == 0");
            }
            Ok((sid, m.bits(), *fl))
        })
        .collect::<Result<_>>()?;
    let head: Vec<NewAce> = parsed
        .iter()
        .map(|(s, m, fl)| NewAce::Allow(s.as_psid(), *m, *fl))
        .collect();
    rebuild_acl(ACL_REVISION, &head, &(Vec::new(), 0, ACL_REVISION), &[])
}

/// Fixed prefix of `ACCESS_ALLOWED_ACE` / `ACCESS_DENIED_ACE`
/// (Header 4 + Mask 4); `SidStart` is the first DWORD of the SID.
const ACE_FIXED: usize = 8;

/// `n` rounded up to a DWORD boundary — `InitializeAcl` requires the
/// ACL buffer length to be DWORD-aligned.
const fn dword_align(n: usize) -> usize {
    (n + 3) & !3
}

/// One ACE to add at the head/tail of a [`rebuild_acl`] output.
/// `PSID` borrows the caller's SID buffer; keep it alive across the
/// call.
pub(crate) enum NewAce {
    Allow(PSID, u32, ACE_FLAGS),
    Deny(PSID, u32, ACE_FLAGS),
}

impl NewAce {
    fn sid(&self) -> PSID {
        let (Self::Allow(s, ..) | Self::Deny(s, ..)) = self;
        *s
    }
}

/// Size + `InitializeAcl(rev)` + `head` ACEs + `kept` raw ACEs +
/// `tail` ACEs → [`BuiltAcl`]. The single ACL-construction
/// chokepoint: [`build_allow_dacl`] (head only, `rev =
/// ACL_REVISION`), [`apply_sandbox_aces`] (head + kept, `rev` from
/// the source ACL via [`filter_aces`]), and `winsta::recompose_dacl`
/// (kept + tail) all thread through here.
///
/// `rev` MUST match the kept ACEs' source revision when `kept` is
/// non-empty (`AddAce` requires `ACL_REVISION_DS` when copying
/// object-type ACEs); pass [`KeptAces`].2.
pub(crate) fn rebuild_acl(
    rev: ACE_REVISION,
    head: &[NewAce],
    kept: &KeptAces,
    tail: &[NewAce],
) -> Result<BuiltAcl> {
    let mut total = size_of::<ACL>() + kept.1;
    for a in head.iter().chain(tail) {
        total += ACE_FIXED + unsafe { GetLengthSid(a.sid()) } as usize;
    }
    total = dword_align(total);
    let mut buf = vec![0u8; total];
    let acl = buf.as_mut_ptr() as *mut ACL;
    unsafe { InitializeAcl(acl, total as u32, rev) }.context("InitializeAcl")?;
    let add = |a: &NewAce| match *a {
        NewAce::Allow(s, m, f) => unsafe { AddAccessAllowedAceEx(acl, rev, f, m, s) }
            .with_context(|| format!("AddAccessAllowedAceEx({m:#x})")),
        NewAce::Deny(s, m, f) => unsafe { AddAccessDeniedAceEx(acl, rev, f, m, s) }
            .with_context(|| format!("AddAccessDeniedAceEx({m:#x})")),
    };
    head.iter().try_for_each(&add)?;
    for (ace, sz) in &kept.0 {
        unsafe { AddAce(acl, rev, u32::MAX, *ace, *sz as u32) }.context("AddAce(keep)")?;
    }
    tail.iter().try_for_each(&add)?;
    Ok(BuiltAcl { buf })
}

// ─── DACL read + ACE-walk primitives ────────────────────────────────
// Shared low-level wrappers so the recompose callers don't each
// open-code GetNamedSecurityInfoW + GetAce loops.
// `winsta.rs::recompose_dacl` uses `filter_aces` with its own
// predicate (it KEEPS inherited ACEs; the file caller here drops
// them).

/// Read a file's DACL via
/// `GetNamedSecurityInfoW(SE_FILE_OBJECT, DACL_SECURITY_INFORMATION)`.
/// The returned `*mut ACL` points INTO the returned `OwnedSd`'s
/// buffer — keep the `OwnedSd` alive while using the pointer.
pub(crate) fn read_file_dacl(canonical_path: &str) -> Result<(OwnedSd, *mut ACL)> {
    let w = wstr(canonical_path);
    let mut dacl: *mut ACL = std::ptr::null_mut();
    let mut psd = PSECURITY_DESCRIPTOR::default();
    let r = unsafe {
        GetNamedSecurityInfoW(
            pcwstr(&w),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            None,
            None,
            Some(&mut dacl),
            None,
            &mut psd,
        )
    };
    win32_ok(r, &format!("GetNamedSecurityInfoW('{canonical_path}')"))?;
    Ok((OwnedSd::from_raw(psd), dacl))
}

/// Whether `sd`'s DACL carries `SE_DACL_PROTECTED` — inheritance from
/// the parent was deliberately severed (`icacls /inheritance:d`,
/// hardened trees). [`apply_sandbox_aces`] preserves this bit on
/// write-back: recomposing such a path with `UNPROTECTED_` would
/// silently re-enable inheritance, durably re-admitting the parent's
/// (often permissive — e.g. a data-drive root's `Authenticated
/// Users:(M)`) inheritable ACEs.
pub(crate) fn sd_dacl_protected(sd: &OwnedSd) -> Result<bool> {
    // The binding's `pcontrol` is a bare `*mut u16` (the wire shape
    // of `SECURITY_DESCRIPTOR_CONTROL`).
    let mut control: u16 = 0;
    let mut revision = 0u32;
    unsafe { GetSecurityDescriptorControl(sd.ptr, &mut control, &mut revision) }
        .context("GetSecurityDescriptorControl")?;
    Ok(control & SE_DACL_PROTECTED.0 != 0)
}

/// Whether [`write_file_dacl`] sets `PROTECTED_` (block inheritance
/// from the parent — the state-DB dir's allow-list, and
/// [`apply_sandbox_aces`] on a path whose DACL was already
/// protected) or `UNPROTECTED_DACL_SECURITY_INFORMATION` (re-derive
/// inherited ACEs from the parent — [`apply_sandbox_aces`] on the
/// common unprotected path).
pub(crate) enum Protection {
    Protected,
    Unprotected,
}

/// Write `acl` as `path`'s DACL via
/// `SetNamedSecurityInfoW(SE_FILE_OBJECT, DACL | <p>)`. Mirror of
/// [`read_file_dacl`].
pub(crate) fn write_file_dacl(path: &str, acl: *const ACL, p: Protection) -> Result<()> {
    let prot = match p {
        Protection::Protected => PROTECTED_DACL_SECURITY_INFORMATION,
        Protection::Unprotected => UNPROTECTED_DACL_SECURITY_INFORMATION,
    };
    let w = wstr(path);
    let r = unsafe {
        SetNamedSecurityInfoW(
            pcwstr(&w),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION | prot,
            None,
            None,
            Some(acl),
            None,
        )
    };
    win32_ok(r, &format!("SetNamedSecurityInfoW('{path}')"))
}

/// `(kept ACE ptrs+sizes, Σ kept sizes, source AclRevision)`.
pub(crate) type KeptAces = (Vec<(*const c_void, u16)>, usize, ACE_REVISION);

/// Walk every ACE of `acl` and collect `(ptr, AceSize)` for those
/// `keep` accepts. Returns `(kept, Σ kept sizes, source AclRevision)`.
/// `acl == null` → `(∅, 0, ACL_REVISION)`. The kept pointers point
/// into `acl`'s buffer; caller must keep that buffer alive.
///
/// `keep` receives the raw `ACE_HEADER` and `body` = the full ACE
/// bytes (`hdr.AceSize` long, starting at the ACE pointer — so
/// `body[ACE_FIXED..]` is the embedded SID).
///
/// The source's `AclRevision` is returned so a caller building a
/// fresh ACL from kept ACEs can preserve it: `AddAce` requires
/// `ACL_REVISION_DS` (4) when copying object-type ACEs, and
/// `RtlValidAcl` rejects an ACL whose revision doesn't match its
/// ACEs.
pub(crate) fn filter_aces(
    acl: *const ACL,
    mut keep: impl FnMut(&ACE_HEADER, &[u8]) -> bool,
) -> Result<KeptAces> {
    if acl.is_null() {
        return Ok((Vec::new(), 0, ACL_REVISION));
    }
    let src_rev = ACE_REVISION(unsafe { (*acl).AclRevision } as u32);
    let mut info = ACL_SIZE_INFORMATION::default();
    unsafe {
        GetAclInformation(
            acl,
            &mut info as *mut _ as *mut c_void,
            size_of::<ACL_SIZE_INFORMATION>() as u32,
            AclSizeInformation,
        )
        .context("GetAclInformation")?;
    }
    let mut kept: Vec<(*const c_void, u16)> = Vec::new();
    let mut kept_sz = 0usize;
    for i in 0..info.AceCount {
        let mut ace: *mut c_void = std::ptr::null_mut();
        unsafe { GetAce(acl, i, &mut ace) }.map_err(|e| anyhow!("GetAce({i}): {e}"))?;
        if ace.is_null() {
            bail!("GetAce({i}) returned null");
        }
        let hdr = unsafe { &*(ace as *const ACE_HEADER) };
        let body = unsafe { std::slice::from_raw_parts(ace as *const u8, hdr.AceSize as usize) };
        if keep(hdr, body) {
            kept.push((ace as *const c_void, hdr.AceSize));
            kept_sz += hdr.AceSize as usize;
        }
    }
    Ok((kept, kept_sz, src_rev))
}

/// True iff `body[ACE_FIXED..]` starts with exactly `sid_bytes` —
/// i.e. the ACE's trustee SID is `sid_bytes`.
pub(crate) fn ace_sid_is(body: &[u8], sid_bytes: &[u8]) -> bool {
    body.get(ACE_FIXED..ACE_FIXED + sid_bytes.len()) == Some(sid_bytes)
}

/// Apply the broker-only DACL to a directory with `(OI)(CI)`
/// inheritance, optionally **prefixed** by a `(D;OICI;FA;;;
/// <deny_sid>)` ACE. Used by `state_db.rs` and `install.rs` to
/// protect `%LOCALAPPDATA%\alysis-sandbox\`.
///
/// `deny_sid` is the [`crate::user::SANDBOX_GROUP`] SID when the
/// sandbox user has been provisioned: the credential file in this
/// directory is encrypted with **machine-scope** DPAPI, which any
/// local account can decrypt — so the sandbox account MUST NOT be
/// able to read it. The real-user `PROTECTED` allow set already
/// excludes the sandbox user, but the explicit DENY makes that
/// intent visible in `Get-Acl` and survives any future widening of
/// the allow set.
///
/// Built via SDDL because [`build_allow_dacl`] only emits ALLOW
/// ACEs, and adding a generic DENY row to the [`Allow`] table
/// would invite misuse.
pub fn stamp_dir_inheriting(canonical_path: &str, deny_sid: Option<&str>) -> Result<()> {
    let deny = deny_sid
        .map(|s| format!("(D;OICI;FA;;;{s})"))
        .unwrap_or_default();
    // Trustee = the **real user** (the broker matches; the
    // `alysis-sandbox` child does not). SY/BA = FILE_ALL `(OI)(CI)`;
    // OWNER_RIGHTS = READ_CONTROL `(OI)(CI)`. SDDL's `FA` =
    // `FILE_ALL_ACCESS`; `RC` = `READ_CONTROL`; `S-1-3-4` =
    // OWNER_RIGHTS. Canonical ACE order = DENY before ALLOW.
    let user_sid = crate::sid::current_user_sid()?;
    let sddl = format!(
        "D:P{deny}\
         (A;OICI;FA;;;{user_sid})\
         (A;OICI;FA;;;SY)\
         (A;OICI;FA;;;BA)\
         (A;OICI;RC;;;S-1-3-4)"
    );
    set_path_dacl_from_sddl(canonical_path, &sddl, "state-db dir")
}

/// SDDL → SD → DACL pointer → `SetNamedSecurityInfoW(PROTECTED)`.
/// One-shot helper for the few call sites that need a DENY ACE
/// (which the [`BuiltAcl`] machinery deliberately doesn't expose).
/// The `D:P` prefix in `sddl` is informational; `PROTECTED` is set
/// here regardless via `PROTECTED_DACL_SECURITY_INFORMATION`.
pub fn set_path_dacl_from_sddl(path: &str, sddl: &str, label: &str) -> Result<()> {
    use windows::Win32::Security::GetSecurityDescriptorDacl;
    let sd = crate::util::OwnedSd::from_sddl(sddl)
        .with_context(|| format!("{label}: build SD from SDDL"))?;
    let mut present = windows::core::BOOL::from(false);
    let mut dacl: *mut ACL = std::ptr::null_mut();
    let mut defaulted = windows::core::BOOL::from(false);
    unsafe {
        GetSecurityDescriptorDacl(sd.ptr, &mut present, &mut dacl, &mut defaulted)
            .with_context(|| format!("{label}: GetSecurityDescriptorDacl"))?;
    }
    if !present.as_bool() || dacl.is_null() {
        bail!("{label}: SDDL '{sddl}' yielded no DACL");
    }
    write_file_dacl(path, dacl, Protection::Protected).context(label.to_owned())
}

/// Open `path` for security-descriptor writes with NO-FOLLOW
/// semantics and REJECT reparse points. `%ProgramData%`'s default
/// DACL lets standard users pre-create directories — including as
/// NTFS mount points / junctions targeting an arbitrary directory —
/// so an elevated install that take-owns and re-ACLs by NAME can be
/// redirected into re-ACLing a victim tree. Opening with
/// `FILE_FLAG_OPEN_REPARSE_POINT` pins the object itself (no
/// traversal), the attribute check rejects a planted reparse point,
/// and the returned HANDLE is what the security writes below
/// operate on — validate-then-use on the same object, not a name.
pub fn open_for_security_no_follow(path: &str) -> Result<crate::util::OwnedHandle> {
    use windows::Win32::Foundation::INVALID_HANDLE_VALUE;
    use windows::Win32::Storage::FileSystem::BY_HANDLE_FILE_INFORMATION;
    use windows::Win32::Storage::FileSystem::{
        CreateFileW, FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS,
        FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
        GetFileInformationByHandle, OPEN_EXISTING,
    };
    let w = wstr(path);
    let h = unsafe {
        CreateFileW(
            pcwstr(&w),
            Mask::READ_CONTROL.bits() | Mask::WRITE_DAC.bits() | 0x0008_0000, // + WRITE_OWNER
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
    }
    .with_context(|| format!("CreateFileW('{path}', no-follow)"))?;
    if h == INVALID_HANDLE_VALUE {
        bail!("CreateFileW('{path}'): INVALID_HANDLE_VALUE");
    }
    let owned = crate::util::OwnedHandle(h);
    let mut info = BY_HANDLE_FILE_INFORMATION::default();
    unsafe { GetFileInformationByHandle(owned.0, &mut info) }
        .with_context(|| format!("GetFileInformationByHandle('{path}')"))?;
    if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT.0 != 0 {
        bail!(
            "'{path}' is a reparse point (junction/symlink) — refusing \
             to operate on a redirected store path; remove it and re-run"
        );
    }
    Ok(owned)
}

/// Set OWNER = `BUILTIN\Administrators` on an open handle (see
/// [`open_for_security_no_follow`] for why by-handle). Requires
/// `SeTakeOwnershipPrivilege`/`SeRestorePrivilege` enabled unless
/// the caller already owns the object.
pub fn set_handle_owner_admins(h: &crate::util::OwnedHandle, label: &str) -> Result<()> {
    use windows::Win32::Security::Authorization::{SE_FILE_OBJECT, SetSecurityInfo};
    let admins = crate::sid::LocalPsid::from_string(SID_BUILTIN_ADMINS)?;
    let r = unsafe {
        SetSecurityInfo(
            h.0,
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION,
            Some(admins.as_psid()),
            None,
            None,
            None,
        )
    };
    win32_ok(r, &format!("SetSecurityInfo(owner, {label})"))
}

/// Write a PROTECTED DACL parsed from `sddl` onto an open handle
/// (see [`open_for_security_no_follow`] for why by-handle).
pub fn set_handle_dacl_from_sddl(
    h: &crate::util::OwnedHandle,
    sddl: &str,
    label: &str,
) -> Result<()> {
    use windows::Win32::Security::Authorization::{SE_FILE_OBJECT, SetSecurityInfo};
    use windows::Win32::Security::GetSecurityDescriptorDacl;
    let sd = crate::util::OwnedSd::from_sddl(sddl)
        .with_context(|| format!("{label}: build SD from SDDL"))?;
    let mut present = windows::core::BOOL::from(false);
    let mut dacl: *mut ACL = std::ptr::null_mut();
    let mut defaulted = windows::core::BOOL::from(false);
    unsafe {
        GetSecurityDescriptorDacl(sd.ptr, &mut present, &mut dacl, &mut defaulted)
            .with_context(|| format!("{label}: GetSecurityDescriptorDacl"))?;
    }
    if !present.as_bool() || dacl.is_null() {
        bail!("{label}: SDDL '{sddl}' yielded no DACL");
    }
    let r = unsafe {
        SetSecurityInfo(
            h.0,
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            Some(dacl),
            None,
        )
    };
    win32_ok(r, &format!("SetSecurityInfo(dacl, {label})"))
}

// ─── Additive grants (working-tree access for the sandbox user) ─────
//
// Under the separate-user model the sandbox user has NO inherent
// rights on real-user-owned files. `acl grant` adds an inheritable
// ALLOW ACE for the sandbox user's SID on a path (typically the
// working-tree root) so the child can read/write there; `acl stamp
// --sandbox-user-sid` adds an explicit DENY ACE on a path (and a
// `(OI)(CI)` `FILE_DELETE_CHILD` DENY on its parent) so the child
// can NOT read/write/delete it even when an inherited
// `BUILTIN\Users` ACE would otherwise allow. Both are ADDITIVE
// (the path keeps its own explicit ACEs and inheritance);
// revoke/restore drops the SID's ACEs by walk-and-filter, not a
// full-SD restore.

/// Per-grant access level. `Modify` is [`Mask::MODIFY_NO_FDC`] (the
/// working-tree grant — read/write/create/delete-own but NOT
/// `FILE_DELETE_CHILD`, so a denied file inside the granted tree
/// cannot be deleted via parent-FDC even where the only sb-user
/// access comes from this grant). `ReadOnly` is
/// [`Mask::FILE_READ_EXEC`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GrantMask {
    /// Object-only path traversal and metadata lookup; no directory listing.
    Traverse,
    ReadOnly,
    Modify,
}

/// Per-deny mask. `ReadDeny` denies everything; `WriteDeny` leaves
/// read+execute. The bits are the *denied* rights.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DenyMask {
    WriteDeny,
    ReadDeny,
}

/// One explicit ACE the sandbox user holds on a path. The
/// separate-user FS model is entirely additive: `acl grant` adds
/// ALLOW ACEs, `acl stamp --sandbox-user-sid` adds DENY ACEs (plus
/// an object-only `FILE_DELETE_CHILD` DENY on the parent). Restore
/// drops the SID's ACEs via walk-and-filter — no PROTECTED rewrite,
/// no SD snapshot, no calibration.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SbAce {
    Grant(GrantMask),
    Deny(DenyMask),
    /// `(D;;FILE_DELETE_CHILD;;;<sb>)` — applied only to the parent
    /// of every denied target so the sandbox user cannot `del`/`ren`
    /// it via parent-FDC even when the parent carries an inherited
    /// `BUILTIN\Users:(F)` (which the sandbox user, a Users member,
    /// would otherwise pick up).
    DenyFdc,
    /// Object-only (`NO_INHERIT`) identity/ACL protection on an
    /// INTERMEDIATE directory. Deny DELETE, WRITE_DAC and WRITE_OWNER
    /// so a child cannot remove the guard or rename/rmdir the ancestor
    /// (which would bypass the leaf's stamp), without affecting children:
    /// a full-mask `(OI)(CI)` deny here would deny reads over the
    /// whole subtree if the placeholder later becomes a real user
    /// directory.
    DenyDelete,
}

impl GrantMask {
    fn bits(self) -> u32 {
        match self {
            GrantMask::Traverse => {
                windows::Win32::Storage::FileSystem::FILE_TRAVERSE.0
                    | windows::Win32::Storage::FileSystem::FILE_READ_ATTRIBUTES.0
                    | Mask::SYNCHRONIZE.bits()
            }
            GrantMask::ReadOnly => Mask::FILE_READ_EXEC.bits(),
            GrantMask::Modify => Mask::MODIFY_NO_FDC.bits(),
        }
    }
}

impl DenyMask {
    fn bits(self) -> u32 {
        match self {
            DenyMask::ReadDeny => Mask::FILE_ALL.bits(),
            // FILE_GENERIC_WRITE includes SYNCHRONIZE + READ_CONTROL
            // — denying SYNCHRONIZE blocks ANY synchronous open
            // (read included). Strip both so denyWrite leaves
            // read open. FILE_WRITE_ATTRIBUTES is already in
            // FILE_GENERIC_WRITE; the explicit `.with()` is
            // belt-and-braces (it survives a constant change).
            DenyMask::WriteDeny => Mask::FILE_GENERIC_WRITE
                .with(Mask::DELETE)
                .with(Mask::FILE_DELETE_CHILD)
                .with(Mask::WRITE_DAC)
                .with(Mask::WRITE_OWNER)
                .with(Mask::FILE_WRITE_ATTRIBUTES)
                .without(Mask::SYNCHRONIZE)
                .without(Mask::READ_CONTROL)
                .bits(),
        }
    }
}

impl SbAce {
    /// `'grant' | 'deny' | 'deny_fdc'` — the row's `kind` column.
    pub fn kind(self) -> &'static str {
        match self {
            SbAce::Grant(_) => "grant",
            SbAce::Deny(_) => "deny",
            SbAce::DenyFdc => "deny_fdc",
            SbAce::DenyDelete => "deny_delete",
        }
    }
    /// `'read' | 'modify' | 'denyRead' | 'denyWrite' | 'fdc'` — the
    /// row's `mask` / holder's `want_mask` column. Round-trips via
    /// [`SbAce::parse`].
    pub fn as_str(self) -> &'static str {
        match self {
            SbAce::Grant(GrantMask::Traverse) => "traverse",
            SbAce::Grant(GrantMask::ReadOnly) => "read",
            SbAce::Grant(GrantMask::Modify) => "modify",
            SbAce::Deny(DenyMask::ReadDeny) => "denyRead",
            SbAce::Deny(DenyMask::WriteDeny) => "denyWrite",
            SbAce::DenyFdc => "fdc",
            SbAce::DenyDelete => "delete",
        }
    }
    pub fn parse(kind: &str, mask: &str) -> Result<Self> {
        Ok(match (kind, mask) {
            ("grant", "traverse") => SbAce::Grant(GrantMask::Traverse),
            ("grant", "read") => SbAce::Grant(GrantMask::ReadOnly),
            ("grant", "modify") => SbAce::Grant(GrantMask::Modify),
            ("deny", "denyRead") => SbAce::Deny(DenyMask::ReadDeny),
            ("deny", "denyWrite") => SbAce::Deny(DenyMask::WriteDeny),
            ("deny_fdc", _) => SbAce::DenyFdc,
            ("deny_delete", _) => SbAce::DenyDelete,
            (k, m) => bail!("unknown SbAce kind={k:?} mask={m:?}"),
        })
    }
    /// Widening within one kind: `Modify ⊃ ReadOnly`, `ReadDeny ⊃
    /// WriteDeny`, `DenyFdc` is unit. Cross-kind is meaningless (a
    /// path can hold one `Grant` row AND one `Deny` row; never
    /// merged).
    pub fn max(self, other: Self) -> Self {
        use DenyMask::ReadDeny as Dr;
        use GrantMask::Modify as Gm;
        use GrantMask::ReadOnly as Gr;
        match (self, other) {
            (SbAce::Grant(Gm), _) | (_, SbAce::Grant(Gm)) => SbAce::Grant(Gm),
            (SbAce::Grant(Gr), _) | (_, SbAce::Grant(Gr)) => SbAce::Grant(Gr),
            (SbAce::Grant(_), _) | (_, SbAce::Grant(_)) => self,
            (SbAce::Deny(Dr), _) | (_, SbAce::Deny(Dr)) => SbAce::Deny(Dr),
            _ => self,
        }
    }
}

/// What [`apply_sandbox_aces`] should converge a path to: at most one
/// ALLOW ACE, one DENY ACE, and one parent-FDC DENY for the sandbox
/// user. State-DB recomputes this from the live holder rows on every
/// change so cross-holder mask escalation/downgrade and the
/// grant/deny-on-same-path interaction are handled in one place.
#[derive(Debug, Clone, Copy, Default)]
pub struct SbAceSet {
    pub grant: Option<GrantMask>,
    pub deny: Option<DenyMask>,
    pub deny_fdc: bool,
    pub deny_delete: bool,
}

impl SbAceSet {
    /// The set's entries as [`NewAce`]s for `sid`, in canonical
    /// deny → deny-fdc → allow order. `Deny`/`Grant` carry
    /// [`OICI`]; `DenyFdc`/`DenyDelete` are object-only ([`NO_INHERIT`]) — see
    /// [`SbAce::DenyDelete`].
    fn head_aces(&self, sid: PSID) -> Vec<NewAce> {
        let mut v = Vec::with_capacity(4);
        if let Some(m) = self.deny {
            v.push(NewAce::Deny(sid, m.bits(), OICI));
        }
        if self.deny_delete {
            v.push(NewAce::Deny(
                sid,
                Mask::DELETE
                    .with(Mask::WRITE_DAC)
                    .with(Mask::WRITE_OWNER)
                    .bits(),
                NO_INHERIT,
            ));
        }
        if self.deny_fdc {
            v.push(NewAce::Deny(
                sid,
                Mask::FILE_DELETE_CHILD.bits(),
                NO_INHERIT,
            ));
        }
        if let Some(m) = self.grant {
            let inherit = if m == GrantMask::Traverse {
                NO_INHERIT
            } else {
                OICI
            };
            v.push(NewAce::Allow(sid, m.bits(), inherit));
        }
        v
    }
}

// GetSecurityInfo(SE_FILE_OBJECT) reconstructs inheritance flags on legacy,
// nonautomatic DACLs. That view is useful for inheritance policy, but writing it
// back changes unrelated stored ACEs. Object-only updates therefore use this
// bounded native snapshot and retain the locked file handle throughout.
const RAW_DACL_CONTROL: u16 = 0x150c;
#[derive(Clone, Debug, PartialEq, Eq)]
struct RawFileDacl {
    control: u16,
    bytes: Vec<u8>,
}

impl RawFileDacl {
    fn read(handle: windows::Win32::Foundation::HANDLE) -> Result<Self> {
        use windows::Wdk::Storage::FileSystem::NtQuerySecurityObject;
        use windows::Win32::Security::{GetSecurityDescriptorDacl, IsValidAcl};
        // An ACL is bounded by its u16 length; allow room for its descriptor.
        // usize storage provides native alignment and is owned only by Rust.
        const CAPACITY: usize = 128 * 1024;
        let mut words = vec![0usize; CAPACITY / size_of::<usize>()];
        let descriptor = PSECURITY_DESCRIPTOR(words.as_mut_ptr().cast());
        let mut returned = 0;
        unsafe {
            NtQuerySecurityObject(
                handle,
                DACL_SECURITY_INFORMATION.0,
                Some(descriptor),
                CAPACITY as u32,
                &mut returned,
            )
        }
        .ok()
        .context("read pinned raw file DACL")?;
        ensure!(
            (20..=CAPACITY).contains(&(returned as usize)),
            "invalid raw file descriptor length"
        );
        let mut present = false.into();
        let mut defaulted = false.into();
        let mut acl = std::ptr::null_mut();
        let mut control = 0;
        let mut revision = 0;
        unsafe {
            GetSecurityDescriptorDacl(descriptor, &mut present, &mut acl, &mut defaulted)?;
            GetSecurityDescriptorControl(descriptor, &mut control, &mut revision)?;
        }
        ensure!(
            present.as_bool() && !acl.is_null(),
            "raw file DACL must be present"
        );
        let start = words.as_ptr() as usize;
        let end = start + returned as usize;
        let acl_start = acl as usize;
        ensure!(
            acl_start >= start
                && acl_start
                    .checked_add(size_of::<ACL>())
                    .is_some_and(|p| p <= end),
            "raw file DACL header is outside the descriptor"
        );
        let size = unsafe { (*acl).AclSize } as usize;
        ensure!(
            size >= size_of::<ACL>() && acl_start.checked_add(size).is_some_and(|p| p <= end),
            "raw file DACL exceeds its descriptor"
        );
        ensure!(
            unsafe { IsValidAcl(acl) }.as_bool(),
            "invalid raw file DACL"
        );
        Ok(Self {
            control: control & RAW_DACL_CONTROL,
            bytes: unsafe { std::slice::from_raw_parts(acl.cast::<u8>(), size) }.to_vec(),
        })
    }

    fn acl(&self) -> *const ACL {
        self.bytes.as_ptr().cast()
    }

    fn aces(&self) -> Result<Vec<Vec<u8>>> {
        let mut result = Vec::new();
        filter_aces(self.acl(), |_, bytes| {
            result.push(bytes.to_vec());
            true
        })?;
        Ok(result)
    }

    fn equivalent(&self, other: &Self) -> Result<bool> {
        // ACL allocation padding is not an ACE; compare revision, every ACE
        // byte in order, and persisted DACL control.
        Ok(self.control == other.control
            && self.bytes[0] == other.bytes[0]
            && self.aces()? == other.aces()?)
    }

    fn require_supported_control(&self) -> Result<()> {
        ensure!(
            self.control & 4 != 0 && self.control & 0x108 == 0,
            "sandbox ACL cannot preserve defaulted or pending inheritance control"
        );
        Ok(())
    }

    fn write_object_only(
        &self,
        handle: windows::Win32::Foundation::HANDLE,
        acl: BuiltAcl,
    ) -> Result<()> {
        use windows::Wdk::Storage::FileSystem::NtSetSecurityObject;
        use windows::Win32::Security::SECURITY_DESCRIPTOR_CONTROL;

        self.require_supported_control()?;
        let wanted = Self {
            control: self.control,
            bytes: acl.buf,
        };
        ensure!(
            Self::read(handle)?.equivalent(self)?,
            "raw file ACL changed before object-only update"
        );
        if wanted.equivalent(self)? {
            return Ok(());
        }
        let mut descriptor = SECURITY_DESCRIPTOR::default();
        let raw = PSECURITY_DESCRIPTOR((&mut descriptor as *mut SECURITY_DESCRIPTOR).cast());
        // Measured raw control: copying AUTO_INHERITED (0x400) alone changes
        // 0x404 to 0x004. NtSetSecurityObject consumes AUTO_INHERIT_REQ (0x100)
        // as input to retain 0x404. The request is not persisted, and is never
        // supplied for a previously nonautomatic DACL. PROTECTED is retained.
        let control = (self.control & 0x1400) | if self.control & 0x400 != 0 { 0x100 } else { 0 };
        unsafe {
            InitializeSecurityDescriptor(raw, SECURITY_DESCRIPTOR_REVISION)?;
            SetSecurityDescriptorDacl(raw, true, Some(wanted.acl()), false)?;
            SetSecurityDescriptorControl(
                raw,
                SECURITY_DESCRIPTOR_CONTROL(0x1500),
                SECURITY_DESCRIPTOR_CONTROL(control),
            )?;
            NtSetSecurityObject(handle, DACL_SECURITY_INFORMATION.0, raw)
                .ok()
                .context("set pinned object-only sandbox ACL")?;
        }
        ensure!(
            Self::read(handle)?.equivalent(&wanted)?,
            "object-only raw ACL readback mismatch; retain recovery intent"
        );
        Ok(())
    }
}

// Diagnostic metadata is deliberately numeric and bounded. Never include SID
// values, masks, callback conditions, or arbitrary ACE body bytes in errors.
fn ordinary_ace_refusal(hdr: &ACE_HEADER, bytes: &[u8]) -> Option<String> {
    use windows::Win32::Security::IsValidSid;
    use windows::Win32::System::SystemServices::ACCESS_ALLOWED_ACE_TYPE;

    let ordinary =
        [ACCESS_ALLOWED_ACE_TYPE as u8, ACCESS_DENIED_ACE_TYPE as u8].contains(&hdr.AceType);
    let shape = if ordinary && bytes.len() >= ACE_FIXED + 8 {
        format!(
            "sid_revision={} sid_subauthority_count={} sid_bytes={}",
            bytes[ACE_FIXED],
            bytes[ACE_FIXED + 1],
            bytes.len() - ACE_FIXED
        )
    } else {
        "sid_shape=unavailable".to_owned()
    };
    let reason = if !ordinary {
        "unsupported_type"
    } else if hdr.AceFlags & CRITICAL_ACE_FLAG as u8 != 0
        && hdr.AceType == ACCESS_DENIED_ACE_TYPE as u8
    {
        "critical_deny"
    } else if hdr.AceFlags & !0x3f != 0 {
        "unsupported_flags"
    } else if bytes.len() < ACE_FIXED + 8 {
        "truncated_sid_header"
    } else if bytes[ACE_FIXED] != 1 {
        "sid_revision"
    } else if bytes[ACE_FIXED + 1] > 15 {
        "sid_subauthority_count"
    } else if bytes.len() != ACE_FIXED + 8 + usize::from(bytes[ACE_FIXED + 1]) * 4 {
        "sid_length"
    } else if !unsafe { IsValidSid(PSID(bytes[ACE_FIXED..].as_ptr() as *mut _)) }.as_bool() {
        "invalid_sid"
    } else {
        return None;
    };
    Some(format!(
        "reason={reason} ace_type={} ace_flags=0x{:02x} ace_size={} observed_bytes={} {shape}",
        hdr.AceType,
        hdr.AceFlags,
        hdr.AceSize,
        bytes.len()
    ))
}

fn selected_sid_aces(acl: *const ACL, sid: &[u8]) -> Result<Vec<Vec<u8>>> {
    let mut result = Vec::new();
    let mut refusal = None;
    let mut index = 0usize;
    filter_aces(acl, |hdr, bytes| {
        let current_index = index;
        index += 1;
        // Object/callback ACEs have different trustee offsets or conditions.
        // Do not infer ownership by finding a SID-shaped prefix in their body.
        if let Some(details) = ordinary_ace_refusal(hdr, bytes) {
            if refusal.is_none() {
                refusal = Some(format!("ace_index={current_index} {details}"));
            }
            return true;
        }
        let matches_target = &bytes[ACE_FIXED..] == sid;
        if hdr.AceFlags & CRITICAL_ACE_FLAG as u8 != 0 {
            // SDK winnt.h: critical allowed ACEs cannot be removed. The
            // propagation branch drops inherited ACEs, so reject that form
            // symmetrically in preflight and apply, before any new intent.
            let reason = if matches_target {
                Some("critical_target")
            } else if hdr.AceFlags & INHERITED_ACE != 0 {
                Some("critical_inherited")
            } else {
                None
            };
            if let Some(reason) = reason {
                if refusal.is_none() {
                    refusal = Some(format!(
                        "ace_index={current_index} reason={reason} ace_type={} ace_flags=0x{:02x} ace_size={}",
                        hdr.AceType, hdr.AceFlags, hdr.AceSize
                    ));
                }
                return true;
            }
        }
        if matches_target {
            result.push(bytes.to_vec());
        }
        true
    })?;
    if let Some(details) = refusal {
        bail!("sandbox ACL requires ordinary bounded allow/deny ACEs: {details}");
    }
    // A reconstructed view can reorder unrelated entries; target ownership
    // must agree exactly, including duplicates, flags, masks and ACE type.
    result.sort();
    Ok(result)
}

fn critical_aces(acl: *const ACL) -> Result<Vec<Vec<u8>>> {
    let mut result = Vec::new();
    filter_aces(acl, |header, bytes| {
        if header.AceFlags & CRITICAL_ACE_FLAG as u8 != 0 {
            result.push(bytes.to_vec());
        }
        true
    })?;
    Ok(result)
}

/// Converge `canonical_path`'s explicit ACEs for `sandbox_sid` to
/// exactly `set`. Existing explicit entries for this SID are replaced,
/// with new deny entries preceding allow entries.
///
/// Object-only changes preserve all unrelated raw ACE bytes/order/flags and
/// persisted DACL control through the exact pinned handle, without descendant
/// propagation. The reconstructed Win32 inheritance view is never written back
/// for these changes.
///
/// Deliberate inheritable grant/deny changes retain the propagating policy:
/// surviving explicit entries are kept, and an unprotected DACL re-derives its
/// inherited entries from its parent. A protected DACL stays protected so an
/// update cannot re-admit the parent's inheritable permissions. Stray inherited
/// entries on a protected DACL are discarded only by that propagation path,
/// not by object-only preservation.
///
/// `SetEntriesInAclW(REVOKE_ACCESS)` is NOT used: per MSDN it
/// removes `ACCESS_ALLOWED_ACE`/`SYSTEM_AUDIT_ACE` for the trustee,
/// not `ACCESS_DENIED_ACE` — so a prior `Deny`/`DenyFdc` ACE would
/// survive. `SET_ACCESS` discards all but adds a new ALLOW ACE,
/// which is wrong when `set` is empty. So we walk + filter
/// manually.
///
/// Admission is symmetric for new grants and their later revocation: only
/// ordinary allow/deny ACEs with supported persisted DACL control are accepted,
/// and raw versus reconstructed target-SID ownership must agree. Unsupported
/// object/callback ACEs or ambiguous legacy inheritance fail before mutation.
/// Unrelated explicit critical allow ACEs are retained byte-exact. Critical
/// target, deny, or inherited ACEs are refused before journaling/mutation; the
/// propagation branch must never remove a critical ACE or guess its authority.
/// Previously created grants on such ACLs may require external recovery.
/// Preflight can reject unsupported requests before journaling, but apply must
/// still recheck: trusted external ACL edits after preflight can cause a later
/// failure, and that failure must never retire durable recovery intent.
///
/// Both grant and deny carry `(OI)(CI)` so directory targets cover
/// the subtree; on a file the inheritance flags are inert.
pub fn apply_sandbox_aces(canonical_path: &str, sandbox_sid: &str, set: SbAceSet) -> Result<()> {
    apply_sandbox_aces_for_id(canonical_path, sandbox_sid, set, None)
}

/// Read-only admission before a caller records new mutation intent. This pins
/// the same expected object and shares apply's policy checks. A trusted external
/// ACL edit after this guard drops can still cause post-journal refusal; apply
/// must revalidate and callers must preserve intent on that later error.
pub(crate) fn preflight_sandbox_aces_for_id(
    canonical_path: &str,
    sandbox_sid: &str,
    expected: crate::path_id::FileId,
) -> Result<()> {
    let target = LockedAclTarget::open(canonical_path, Some(expected))?;
    let sid = LocalPsid::from_string(sandbox_sid)
        .with_context(|| format!("parse sandbox SID '{sandbox_sid}'"))?;
    admitted_sandbox_dacl(target.handle.raw(), sid.as_bytes())
        .with_context(|| format!("sandbox ACL preflight canonical_path={canonical_path:?}"))?;
    Ok(())
}

fn admitted_sandbox_dacl(
    handle: windows::Win32::Foundation::HANDLE,
    sid: &[u8],
) -> Result<(OwnedSd, *mut ACL, RawFileDacl)> {
    let mut old = std::ptr::null_mut();
    let mut descriptor = PSECURITY_DESCRIPTOR::default();
    win32_ok(
        unsafe {
            GetSecurityInfo(
                handle,
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION,
                None,
                None,
                Some(&mut old),
                None,
                Some(&mut descriptor),
            )
        },
        "read pinned sandbox ACL admission",
    )?;
    let sd = OwnedSd::from_raw(descriptor);
    // Admission is symmetric: never create an inheritable grant on an ACL
    // that its later empty-set revocation cannot safely interpret.
    let raw = RawFileDacl::read(handle)?;
    raw.require_supported_control()?;
    ensure!(
        selected_sid_aces(raw.acl(), sid).context("ACL admission view=raw")?
            == selected_sid_aces(old, sid).context("ACL admission view=win32")?,
        "raw and reconstructed sandbox ACE ownership disagree; refusing ACL update"
    );
    ensure!(
        critical_aces(raw.acl())? == critical_aces(old)?,
        "raw and reconstructed critical ACEs disagree; refusing ACL update"
    );
    // old points into sd, which remains owned for the complete recomposition.
    Ok((sd, old, raw))
}

/// Apply to the exact recorded object, with no name-based identity gap. The
/// parent chain stays pinned too. Object-only updates use a raw native DACL
/// on that exact handle; Win32 reconstruction/propagation is kept separate.
pub fn apply_sandbox_aces_for_id(
    canonical_path: &str,
    sandbox_sid: &str,
    set: SbAceSet,
    expected: Option<crate::path_id::FileId>,
) -> Result<()> {
    let target = LockedAclTarget::open(canonical_path, expected)?;
    let sid = LocalPsid::from_string(sandbox_sid)
        .with_context(|| format!("parse sandbox SID '{sandbox_sid}'"))?;
    let sid_bytes = sid.as_bytes();
    let (sd, old, raw_before) = admitted_sandbox_dacl(target.handle.raw(), sid_bytes)
        .with_context(|| format!("sandbox ACL apply canonical_path={canonical_path:?}"))?;
    let protected =
        sd_dacl_protected(&sd).with_context(|| format!("recompose '{canonical_path}'"))?;
    // Object-only parent protection must not traverse the entire user profile.
    // Use a raw, non-propagating handle update only when neither
    // the old nor the new sandbox ACEs are inheritable. SetSecurityInfo with
    // MAXIMUM_ALLOWED also avoids propagation, but requests DELETE access,
    // which fails sharing checks on directories held as a process's cwd.
    // Inheritable grant/deny changes still use full propagation below.
    let old_inheritable = filter_aces(old, |hdr, body| {
        hdr.AceFlags & INHERITED_ACE == 0
            && hdr.AceFlags & OICI.0 as u8 != 0
            && ace_sid_is(body, sid_bytes)
    })?;
    if matches!(set.grant, None | Some(GrantMask::Traverse))
        && set.deny.is_none()
        && old_inheritable.0.is_empty()
    {
        // Preserve raw order/flags, including this SID's inherited entries.
        // Never feed Win32's reconstructed inheritance view into this setter.
        let kept = filter_aces(raw_before.acl(), |hdr, body| {
            hdr.AceFlags & INHERITED_ACE != 0 || !ace_sid_is(body, sid_bytes)
        })?;
        let new = rebuild_acl(kept.2, &set.head_aces(sid.as_psid()), &kept, &[])?;
        return raw_before.write_object_only(target.handle.raw(), new);
    }
    let critical_before = critical_aces(raw_before.acl())?;
    // 2. Collect surviving explicit ACEs (drop inherited and any
    //    explicit ACE whose SID == sandbox_sid — allow AND deny).
    let kept = filter_aces(old, |hdr, body| {
        hdr.AceFlags & INHERITED_ACE == 0 && !ace_sid_is(body, sid_bytes)
    })?;
    // 3. Build fresh ACL: set's entries (deny-first canonical order)
    //    then surviving explicit ACEs.
    let new = rebuild_acl(kept.2, &set.head_aces(sid.as_psid()), &kept, &[])?;
    // 4. Write back, preserving the DACL's protection state:
    //    UNPROTECTED so the kernel re-derives inherited ACEs from the
    //    parent, PROTECTED when inheritance was severed before we
    //    touched the path (see doc comment).
    let prot = if protected {
        Protection::Protected
    } else {
        Protection::Unprotected
    };
    let protection = match prot {
        Protection::Protected => PROTECTED_DACL_SECURITY_INFORMATION,
        Protection::Unprotected => UNPROTECTED_DACL_SECURITY_INFORMATION,
    };
    win32_ok(
        unsafe {
            SetSecurityInfo(
                target.handle.raw(),
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION | protection,
                None,
                None,
                Some(new.as_ptr()),
                None,
            )
        },
        &format!("SetSecurityInfo('{canonical_path}')"),
    )?;
    if !critical_before.is_empty() {
        ensure!(
            critical_aces(RawFileDacl::read(target.handle.raw())?.acl())? == critical_before,
            "critical ACE readback mismatch; retain recovery intent"
        );
    }
    Ok(())
}

/// Pin the entire lexical path before a named object-only ACL update. Opening
/// ancestors from the drive root down, without DELETE sharing and with
/// OPEN_REPARSE_POINT, prevents replacing a component after it is inspected.
/// The leaf handle is also used for both identity and DACL operations.
struct LockedAclTarget {
    handle: crate::util::OwnedHandle,
    _parents: Vec<crate::util::OwnedHandle>,
}

impl LockedAclTarget {
    fn open(path: &str, expected: Option<crate::path_id::FileId>) -> Result<Self> {
        let path = std::path::Path::new(path);
        ensure!(path.is_absolute(), "ACL target must be absolute");
        let mut ancestors: Vec<_> = path.ancestors().skip(1).collect();
        ancestors.reverse();
        let mut parents = Vec::with_capacity(ancestors.len());
        for parent in ancestors {
            parents.push(Self::open_component(parent, 0)?);
        }
        let handle =
            Self::open_component(path, Mask::READ_CONTROL.bits() | Mask::WRITE_DAC.bits())?;
        if let Some(expected) = expected {
            ensure!(
                crate::path_id::file_id_from_handle(handle.raw())? == expected,
                "sandbox ACL target changed: {}",
                path.display()
            );
        }
        Ok(Self {
            handle,
            _parents: parents,
        })
    }

    fn open_component(path: &std::path::Path, access: u32) -> Result<crate::util::OwnedHandle> {
        use windows::Win32::Storage::FileSystem::{
            BY_HANDLE_FILE_INFORMATION, CreateFileW, FILE_ATTRIBUTE_REPARSE_POINT,
            FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ,
            FILE_SHARE_WRITE, GetFileInformationByHandle, OPEN_EXISTING,
        };
        let wide = wstr(path.to_str().context("ACL path is not Unicode")?);
        let handle = crate::util::OwnedHandle(
            unsafe {
                CreateFileW(
                    pcwstr(&wide),
                    // Metadata-only handles do not participate in NTFS sharing
                    // checks. FILE_READ_DATA (FILE_LIST_DIRECTORY for ancestors)
                    // is necessary for denying concurrent DELETE/rename opens.
                    access | 1,
                    FILE_SHARE_READ | FILE_SHARE_WRITE,
                    None,
                    OPEN_EXISTING,
                    FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                    None,
                )
            }
            .with_context(|| format!("pin sandbox ACL path {}", path.display()))?,
        );
        let mut info = BY_HANDLE_FILE_INFORMATION::default();
        unsafe {
            GetFileInformationByHandle(handle.raw(), &mut info)?;
        }
        ensure!(
            info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT.0 == 0,
            "sandbox ACL path contains a reparse point: {}",
            path.display()
        );
        Ok(handle)
    }
}

/// Retain the workspace owner's access to output owned by the sandbox account.
/// Python's private Windows temp directories grant OWNER_RIGHTS rather than a
/// concrete user SID. That ACE changes meaning on files created by a different
/// account. Add an inheritable grant for the actual owner before execution;
/// retain it after sandbox grants are revoked so returned output stays usable.
/// This never grants a different host identity access to someone else's root.
pub fn ensure_workspace_owner_access(path: &str, owner_sid: &str) -> Result<()> {
    ensure_owner_access(path, owner_sid, Mask::FILE_ALL, true)
}

/// OWNER_RIGHTS-only private folders do not provide the host-user leg of an
/// AppContainer read check. Preserve explicit read/execute for their verified
/// owner, without adding any write permission or changing foreign-owned roots.
pub fn ensure_owned_read_access(path: &str, owner_sid: &str) -> Result<()> {
    ensure_owner_access(path, owner_sid, Mask::FILE_READ_EXEC, false)
}

fn ensure_owner_access(path: &str, owner_sid: &str, mask: Mask, require_owner: bool) -> Result<()> {
    use windows::Win32::Security::GetSecurityDescriptorOwner;
    let target = LockedAclTarget::open(path, None)?;
    let owner = LocalPsid::from_string(owner_sid)?;
    let mut descriptor = PSECURITY_DESCRIPTOR::default();
    let mut dacl = std::ptr::null_mut();
    win32_ok(
        unsafe {
            GetSecurityInfo(
                target.handle.raw(),
                SE_FILE_OBJECT,
                OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                None,
                None,
                Some(&mut dacl),
                None,
                Some(&mut descriptor),
            )
        },
        "read workspace ownership",
    )?;
    let sd = OwnedSd::from_raw(descriptor);
    let mut actual = PSID::default();
    let mut defaulted = windows::core::BOOL(0);
    unsafe {
        GetSecurityDescriptorOwner(sd.ptr, &mut actual, &mut defaulted)?;
    }
    anyhow::ensure!(!actual.0.is_null(), "workspace has no owner");
    let actual_bytes =
        unsafe { std::slice::from_raw_parts(actual.0.cast::<u8>(), GetLengthSid(actual) as usize) };
    if actual_bytes != owner.as_bytes() {
        anyhow::ensure!(
            !require_owner,
            "workspace must belong to the current Windows user"
        );
        return Ok(());
    }
    let existing = filter_aces(dacl, |hdr, body| {
        hdr.AceType == windows::Win32::System::SystemServices::ACCESS_ALLOWED_ACE_TYPE as u8
            && hdr.AceFlags & OICI.0 as u8 == OICI.0 as u8
            && hdr.AceFlags & windows::Win32::Security::INHERIT_ONLY_ACE.0 as u8 == 0
            && ace_sid_is(body, owner.as_bytes())
            && u32::from_le_bytes(body[4..8].try_into().unwrap()) & mask.bits() == mask.bits()
    })?;
    if !existing.0.is_empty() {
        return Ok(());
    }
    // Keep deny-before-allow ordering and every unrelated explicit ACE.
    let kept = filter_aces(dacl, |hdr, _| hdr.AceFlags & INHERITED_ACE == 0)?;
    let new = rebuild_acl(
        kept.2,
        &[],
        &kept,
        &[NewAce::Allow(owner.as_psid(), mask.bits(), OICI)],
    )?;
    let protection = if sd_dacl_protected(&sd)? {
        PROTECTED_DACL_SECURITY_INFORMATION
    } else {
        UNPROTECTED_DACL_SECURITY_INFORMATION
    };
    win32_ok(
        unsafe {
            SetSecurityInfo(
                target.handle.raw(),
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION | protection,
                None,
                None,
                Some(new.as_ptr()),
                None,
            )
        },
        "preserve workspace owner access",
    )
}

/// Whether `canonical_path` carries an EXPLICIT `(OI)(CI)` deny ACE
/// for `sandbox_sid` covering at least the [`DenyMask::WriteDeny`]
/// bits. Drift check for `srt-win status`'s ambient write-deny
/// report and the install early-out's completeness check — a
/// recorded install-time stamp whose on-disk ACE is gone (e.g. an
/// `icacls /reset` by an admin) reads as absent, and an unrelated
/// narrow deny on the same path (a `DenyDelete` placeholder, a
/// parent-FDC ACE) must NOT count as the write-deny being intact.
pub fn sandbox_deny_present(canonical_path: &str, sandbox_sid: &str) -> Result<bool> {
    let sid = LocalPsid::from_string(sandbox_sid)
        .with_context(|| format!("parse sandbox SID '{sandbox_sid}'"))?;
    let sid_bytes = sid.as_bytes();
    let want_mask = DenyMask::WriteDeny.bits();
    let want_flags = OICI.0 as u8;
    let (_sd, dacl) = read_file_dacl(canonical_path)?;
    let denies = filter_aces(dacl, |hdr, body| {
        // ACCESS_DENIED_ACE layout: header (4) then Mask (u32 LE).
        let mask = u32::from_le_bytes([body[4], body[5], body[6], body[7]]);
        u32::from(hdr.AceType) == ACCESS_DENIED_ACE_TYPE
            && hdr.AceFlags & INHERITED_ACE == 0
            && hdr.AceFlags & want_flags == want_flags
            && mask & want_mask == want_mask
            && ace_sid_is(body, sid_bytes)
    })?;
    Ok(!denies.0.is_empty())
}

const INHERITED_ACE: u8 = 0x10;
#[cfg(test)]
mod tests {
    use super::*;

    fn ace_count(d: &BuiltAcl) -> u16 {
        // ACL.AceCount is bytes 4–5 (LE u16).
        u16::from_le_bytes([d.buf[4], d.buf[5]])
    }

    /// IsolatedDesk's `[broker, sb, SY]:GA` DACL — 3 ACEs,
    /// revision 2, GENERIC_ALL on each. Regression for the
    /// `KeptAces`/`rebuild_acl` chokepoint: a sizing or revision
    /// bug here breaks the sandbox-user desktop attach (R1 hang).
    #[test]
    fn isolated_desk_dacl_shape() {
        let d = build_allow_dacl(&[
            Allow("S-1-5-21-1-2-3-1000", Mask::GENERIC_ALL, NO_INHERIT),
            Allow("S-1-5-21-1-2-3-1004", Mask::GENERIC_ALL, NO_INHERIT),
            Allow(SID_SYSTEM, Mask::GENERIC_ALL, NO_INHERIT),
        ])
        .unwrap();
        assert_eq!(d.buf[0], 2, "AclRevision");
        assert_eq!(ace_count(&d), 3);
    }

    #[test]
    fn build_allow_dacl_dedup() {
        // Same SID twice (case-insensitive) → 1 ACE; first wins.
        let d = build_allow_dacl(&[
            Allow(SID_BUILTIN_ADMINS, Mask::FILE_ALL, NO_INHERIT),
            Allow("s-1-5-32-544", Mask::FILE_READ_EXEC, NO_INHERIT),
        ])
        .unwrap();
        assert_eq!(ace_count(&d), 1);
    }

    /// Grant → revoke on a `SE_DACL_PROTECTED` directory must leave
    /// the protection bit (and the explicit ACE set) intact.
    /// Regression: [`apply_sandbox_aces`] used to write back
    /// `UNPROTECTED_` unconditionally, which re-enabled inheritance
    /// on a deliberately severed path — durably re-admitting the
    /// parent's inheritable ACEs (e.g. a data-drive root's
    /// `Authenticated Users:(M)`), and revoke could not undo it.
    #[test]
    fn recompose_preserves_dacl_protection() {
        // Guests — a resolvable well-known SID that is never the
        // test runner's own identity, standing in for the sandbox
        // user (no account provisioning needed to write ACEs).
        const FAKE_SB_SID: &str = "S-1-5-32-546";
        let dir = std::env::temp_dir().join(format!("srtwin-prot-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.to_str().unwrap();

        // Sever inheritance: protected DACL with an explicit
        // owner-side allow (mirrors `icacls /inheritance:d` plus a
        // lockdown to a fixed ACE set).
        let user_sid = crate::sid::current_user_sid().unwrap();
        let lockdown = build_allow_dacl(&[
            Allow(&user_sid, Mask::FILE_ALL, OICI),
            Allow(SID_SYSTEM, Mask::FILE_ALL, OICI),
        ])
        .unwrap();
        write_file_dacl(path, lockdown.as_ptr(), Protection::Protected).unwrap();
        let (sd, _) = read_file_dacl(path).unwrap();
        assert!(sd_dacl_protected(&sd).unwrap(), "setup: DACL protected");

        // Grant + revoke through the production chokepoint.
        let grant = SbAceSet {
            grant: Some(GrantMask::Modify),
            ..Default::default()
        };
        apply_sandbox_aces(path, FAKE_SB_SID, grant).unwrap();
        let (sd, dacl) = read_file_dacl(path).unwrap();
        assert!(sd_dacl_protected(&sd).unwrap(), "protection survives grant");
        let (aces, ..) = filter_aces(dacl, |hdr, _| hdr.AceFlags & INHERITED_ACE != 0).unwrap();
        assert!(aces.is_empty(), "no inherited ACEs re-derived after grant");

        apply_sandbox_aces(path, FAKE_SB_SID, SbAceSet::default()).unwrap();
        let (sd, dacl) = read_file_dacl(path).unwrap();
        assert!(
            sd_dacl_protected(&sd).unwrap(),
            "protection survives revoke"
        );
        let (aces, ..) = filter_aces(dacl, |hdr, _| hdr.AceFlags & INHERITED_ACE != 0).unwrap();
        assert!(aces.is_empty(), "no inherited ACEs re-derived after revoke");

        // The common case keeps its contract: an unprotected dir
        // stays unprotected across the same round-trip, and its
        // inherited ACEs (the parent's OICI lockdown entries
        // propagate to it) really are re-derived by the
        // `UNPROTECTED_` write-back — not merely "protection bit
        // still clear".
        let plain = dir.join("plain");
        std::fs::create_dir_all(&plain).unwrap();
        let plain_path = plain.to_str().unwrap();
        apply_sandbox_aces(plain_path, FAKE_SB_SID, grant).unwrap();
        apply_sandbox_aces(plain_path, FAKE_SB_SID, SbAceSet::default()).unwrap();
        let (sd, dacl) = read_file_dacl(plain_path).unwrap();
        assert!(
            !sd_dacl_protected(&sd).unwrap(),
            "unprotected dir stays unprotected"
        );
        let (aces, ..) = filter_aces(dacl, |hdr, _| hdr.AceFlags & INHERITED_ACE != 0).unwrap();
        assert!(
            !aces.is_empty(),
            "inherited ACEs re-derived on the unprotected dir"
        );

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn parent_delete_protection_does_not_change_children() {
        const FAKE_SB_SID: &str = "S-1-5-32-546";
        let dir = std::env::temp_dir().join(format!("alysis-fdc-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let child = dir.join("child");
        std::fs::create_dir_all(&child).unwrap();
        let snapshot = |path: &std::path::Path| {
            let (_sd, acl) = read_file_dacl(path.to_str().unwrap()).unwrap();
            unsafe { std::slice::from_raw_parts(acl.cast::<u8>(), (*acl).AclSize as usize) }
                .to_vec()
        };
        let before = snapshot(&child);
        let path = dir.to_str().unwrap();
        // Simulate a cwd handle: DELETE sharing is unavailable, but an ACL
        // update must still work without requesting unrelated DELETE access.
        use windows::Win32::Storage::FileSystem::{
            CreateFileW, FILE_FLAG_BACKUP_SEMANTICS, FILE_SHARE_READ, FILE_SHARE_WRITE,
            OPEN_EXISTING,
        };
        let held = crate::util::OwnedHandle(unsafe {
            CreateFileW(
                pcwstr(&wstr(path)),
                Mask::READ_CONTROL.bits(),
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS,
                None,
            )
            .unwrap()
        });
        let (before_sd, _) = read_file_dacl(path).unwrap();
        let protected = sd_dacl_protected(&before_sd).unwrap();
        apply_sandbox_aces(
            path,
            FAKE_SB_SID,
            SbAceSet {
                deny_fdc: true,
                deny_delete: true,
                ..Default::default()
            },
        )
        .unwrap();
        assert_eq!(snapshot(&child), before);
        let (_sd, dacl) = read_file_dacl(path).unwrap();
        let sid = LocalPsid::from_string(FAKE_SB_SID).unwrap();
        let identity_mask = Mask::DELETE
            .with(Mask::WRITE_DAC)
            .with(Mask::WRITE_OWNER)
            .bits();
        let denies = filter_aces(dacl, |hdr, body| {
            let mask = u32::from_le_bytes([body[4], body[5], body[6], body[7]]);
            u32::from(hdr.AceType) == ACCESS_DENIED_ACE_TYPE
                && hdr.AceFlags == NO_INHERIT.0 as u8
                && mask == identity_mask
                && ace_sid_is(body, sid.as_bytes())
        })
        .unwrap();
        assert_eq!(
            denies.0.len(),
            1,
            "ancestor identity guard must be object-only"
        );
        let later = dir.join("later");
        std::fs::create_dir_all(&later).unwrap();
        assert_eq!(
            snapshot(&later),
            before,
            "new child must not inherit parent FDC deny"
        );
        apply_sandbox_aces(path, FAKE_SB_SID, SbAceSet::default()).unwrap();
        assert_eq!(snapshot(&child), before);
        let (after_sd, _) = read_file_dacl(path).unwrap();
        assert_eq!(sd_dacl_protected(&after_sd).unwrap(), protected);
        drop(held);
        std::fs::remove_dir_all(dir).unwrap();
    }

    // Independent low-level Win32 read: unlike GetNamedSecurityInfo, this
    // exposes the stored descriptor without reconstructing inheritance flags.
    fn raw_test_descriptor(path: &std::path::Path) -> Vec<usize> {
        use windows::Win32::Security::{GROUP_SECURITY_INFORMATION, GetFileSecurityW};
        let mut words = vec![0usize; 128 * 1024 / size_of::<usize>()];
        let mut needed = 0;
        unsafe {
            GetFileSecurityW(
                pcwstr(&wstr(path.to_str().unwrap())),
                (DACL_SECURITY_INFORMATION
                    | OWNER_SECURITY_INFORMATION
                    | GROUP_SECURITY_INFORMATION)
                    .0,
                Some(PSECURITY_DESCRIPTOR(words.as_mut_ptr().cast())),
                (words.len() * size_of::<usize>()) as u32,
                &mut needed,
            )
            .ok()
            .unwrap();
        }
        assert!((20..=words.len() * size_of::<usize>()).contains(&(needed as usize)));
        words
    }

    fn raw_test_snapshot(path: &std::path::Path) -> RawFileDacl {
        use windows::Win32::Security::GetSecurityDescriptorDacl;
        let words = raw_test_descriptor(path);
        let descriptor = PSECURITY_DESCRIPTOR(words.as_ptr() as *mut _);
        let mut dacl = std::ptr::null_mut();
        let mut present = false.into();
        let mut defaulted = false.into();
        let mut control = 0;
        let mut revision = 0;
        unsafe {
            GetSecurityDescriptorDacl(descriptor, &mut present, &mut dacl, &mut defaulted).unwrap();
            GetSecurityDescriptorControl(descriptor, &mut control, &mut revision).unwrap();
        }
        assert!(present.as_bool() && !dacl.is_null());
        RawFileDacl {
            control: control & RAW_DACL_CONTROL,
            bytes: unsafe {
                std::slice::from_raw_parts(dacl.cast::<u8>(), (*dacl).AclSize as usize)
            }
            .to_vec(),
        }
    }

    fn raw_fixture(mode: &str) -> (std::path::PathBuf, std::path::PathBuf) {
        use std::sync::atomic::{AtomicU64, Ordering};
        use windows::Win32::Security::SetFileSecurityW;
        use windows::Win32::Storage::FileSystem::CreateDirectoryW;
        // Windows clock ticks can repeat across parallel tests in one process.
        // Keep each explicit-security-descriptor fixture independent of the clock.
        static SEQUENCE: AtomicU64 = AtomicU64::new(0);
        let sequence = SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let owner = crate::sid::current_user_sid().unwrap();
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = crate::util::test_temp_dir()
            .join(format!("alysis-raw-acl-{}-{nonce}-{sequence}", std::process::id()));
        // Two different ACEs for the same unrelated trustee exercise the
        // reconstructed legacy Win32 ordering, without a host-specific SID.
        let allows = format!(
            "(A;OICI;FA;;;{owner})(A;OICI;FA;;;SY)(A;OICI;0x1301bf;;;BU)(A;OICI;0x1200a9;;;BU)"
        );
        let outer = OwnedSd::from_sddl(&format!("O:{owner}D:P{allows}")).unwrap();
        let attributes = SECURITY_ATTRIBUTES {
            nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: outer.ptr.0,
            bInheritHandle: false.into(),
        };
        unsafe { CreateDirectoryW(pcwstr(&wstr(root.to_str().unwrap())), Some(&attributes)) }
            .unwrap();
        let parent = root.join("parent");
        std::fs::create_dir(&parent).unwrap();
        let input = match mode {
            "legacy" => "",
            "automatic" => "ARAI",
            "protected" => "PARAI",
            _ => panic!("unknown fixture control"),
        };
        let descriptor = OwnedSd::from_sddl(&format!("D:{input}{allows}")).unwrap();
        unsafe {
            SetFileSecurityW(
                pcwstr(&wstr(parent.to_str().unwrap())),
                DACL_SECURITY_INFORMATION,
                descriptor.ptr,
            )
            .unwrap();
        }
        let expected = match mode {
            "legacy" => 4,
            "automatic" => 0x404,
            _ => 0x1404,
        };
        assert_eq!(raw_test_snapshot(&parent).control, expected);
        (root, parent)
    }

    fn guest_restricted_access(path: &std::path::Path, desired: u32) -> bool {
        use crate::util::OwnedHandle;
        use windows::Win32::Foundation::HANDLE;
        use windows::Win32::Security::{
            AccessCheck, CreateRestrictedToken, DISABLE_MAX_PRIVILEGE, DuplicateToken,
            GENERIC_MAPPING, PRIVILEGE_SET, SID_AND_ATTRIBUTES, SecurityImpersonation,
            TOKEN_DUPLICATE, TOKEN_QUERY,
        };
        use windows::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};
        let owner = LocalPsid::from_string(&crate::sid::current_user_sid().unwrap()).unwrap();
        let guest = LocalPsid::from_string("S-1-5-32-546").unwrap();
        let mut base = HANDLE::default();
        unsafe {
            OpenProcessToken(
                GetCurrentProcess(),
                TOKEN_DUPLICATE | TOKEN_QUERY,
                &mut base,
            )
        }
        .unwrap();
        let base = OwnedHandle(base);
        // The owner restriction grants the ordinary fixture access. A Guests
        // deny must still win in this independent restricted-SID access check.
        let restrictions = [
            SID_AND_ATTRIBUTES {
                Sid: guest.as_psid(),
                Attributes: 0,
            },
            SID_AND_ATTRIBUTES {
                Sid: owner.as_psid(),
                Attributes: 0,
            },
        ];
        let mut restricted = HANDLE::default();
        unsafe {
            CreateRestrictedToken(
                base.raw(),
                DISABLE_MAX_PRIVILEGE,
                None,
                None,
                Some(&restrictions),
                &mut restricted,
            )
        }
        .unwrap();
        let restricted = OwnedHandle(restricted);
        let mut token = HANDLE::default();
        unsafe { DuplicateToken(restricted.raw(), SecurityImpersonation, &mut token) }.unwrap();
        let token = OwnedHandle(token);
        let words = raw_test_descriptor(path);
        let map = GENERIC_MAPPING {
            GenericRead: FILE_GENERIC_READ.0,
            GenericWrite: windows::Win32::Storage::FileSystem::FILE_GENERIC_WRITE.0,
            GenericExecute: FILE_GENERIC_EXECUTE.0,
            GenericAll: FILE_ALL_ACCESS.0,
        };
        let mut privileges = [0usize; 128];
        let mut length = std::mem::size_of_val(&privileges) as u32;
        let mut granted = 0;
        let mut allowed = false.into();
        unsafe {
            AccessCheck(
                PSECURITY_DESCRIPTOR(words.as_ptr() as *mut _),
                token.raw(),
                desired,
                &map,
                Some(privileges.as_mut_ptr().cast::<PRIVILEGE_SET>()),
                &mut length,
                &mut granted,
                &mut allowed,
            )
        }
        .unwrap();
        allowed.as_bool() && granted & desired == desired
    }

    #[test]
    fn object_only_acl_preserves_raw_parent_children_control_and_effective_denial() {
        const SID: &str = "S-1-5-32-546";
        let sid = LocalPsid::from_string(SID).unwrap();
        let guard = SbAceSet {
            deny_fdc: true,
            deny_delete: true,
            ..Default::default()
        };
        let rights = Mask::DELETE.with(Mask::FILE_DELETE_CHILD).bits();
        for mode in ["legacy", "automatic", "protected"] {
            let (root, parent) = raw_fixture(mode);
            let child = parent.join("existing");
            let grandchild = child.join("nested.txt");
            std::fs::create_dir(&child).unwrap();
            std::fs::write(&grandchild, b"synthetic child").unwrap();
            let before = raw_test_snapshot(&parent);
            let child_before = raw_test_snapshot(&child);
            let grandchild_before = raw_test_snapshot(&grandchild);
            assert!(guest_restricted_access(&parent, rights));
            assert!(guest_restricted_access(&child, rights));

            // Keep the same delete-sharing constraint as a live pinned cwd.
            let held = LockedAclTarget::open(parent.to_str().unwrap(), None).unwrap();
            assert!(std::fs::rename(&parent, root.join("moved")).is_err());
            apply_sandbox_aces(parent.to_str().unwrap(), SID, guard).unwrap();
            let added = raw_test_snapshot(&parent);
            assert_eq!(added.control, before.control);
            let unrelated: Vec<_> = added
                .aces()
                .unwrap()
                .into_iter()
                .filter(|ace| !ace_sid_is(ace, sid.as_bytes()))
                .collect();
            assert_eq!(
                unrelated,
                before.aces().unwrap(),
                "{mode}: unrelated raw ACEs"
            );
            assert_eq!(raw_test_snapshot(&child), child_before, "{mode}: old child");
            assert_eq!(
                raw_test_snapshot(&grandchild),
                grandchild_before,
                "{mode}: grandchild"
            );
            for denied in [Mask::DELETE, Mask::FILE_DELETE_CHILD] {
                assert!(
                    !guest_restricted_access(&parent, denied.bits()),
                    "{mode}: independent object-only deny {:#x}",
                    denied.bits()
                );
            }
            assert!(
                guest_restricted_access(&parent, Mask::FILE_READ_EXEC.bits()),
                "{mode}: ordinary read and traversal remain usable"
            );
            assert!(
                guest_restricted_access(&child, rights),
                "{mode}: child remains usable"
            );
            apply_sandbox_aces(parent.to_str().unwrap(), SID, guard).unwrap();
            assert_eq!(
                raw_test_snapshot(&parent),
                added,
                "{mode}: add is idempotent"
            );

            let later = parent.join("later");
            std::fs::create_dir(&later).unwrap();
            assert_eq!(
                raw_test_snapshot(&later),
                child_before,
                "{mode}: future child"
            );
            assert!(guest_restricted_access(&later, rights));

            apply_sandbox_aces(parent.to_str().unwrap(), SID, SbAceSet::default()).unwrap();
            assert_eq!(
                raw_test_snapshot(&parent),
                before,
                "{mode}: exact raw restoration"
            );
            assert!(guest_restricted_access(&parent, rights));
            apply_sandbox_aces(parent.to_str().unwrap(), SID, SbAceSet::default()).unwrap();
            assert_eq!(
                raw_test_snapshot(&parent),
                before,
                "{mode}: remove is idempotent"
            );
            assert_eq!(raw_test_snapshot(&child), child_before);
            assert_eq!(raw_test_snapshot(&later), child_before);
            drop(held);
            std::fs::remove_dir_all(root).unwrap();
        }
    }

    #[test]
    fn object_only_acl_refuses_reconstructed_target_ownership_before_mutation() {
        let (root, parent) = raw_fixture("legacy");
        let child = parent.join("child");
        std::fs::create_dir(&child).unwrap();
        let before = raw_test_snapshot(&parent);
        let child_before = raw_test_snapshot(&child);
        let sid = LocalPsid::from_string(SID_BUILTIN_USERS).unwrap();
        let (_view, acl) = read_file_dacl(parent.to_str().unwrap()).unwrap();
        assert_ne!(
            selected_sid_aces(before.acl(), sid.as_bytes()).unwrap(),
            selected_sid_aces(acl, sid.as_bytes()).unwrap(),
            "fixture must expose ambiguous inheritance"
        );
        let error = apply_sandbox_aces(
            parent.to_str().unwrap(),
            SID_BUILTIN_USERS,
            SbAceSet {
                deny_fdc: true,
                ..Default::default()
            },
        )
        .unwrap_err();
        assert!(
            format!("{error:#}").contains("ownership disagree"),
            "{error:#}"
        );
        assert_eq!(raw_test_snapshot(&parent), before);
        assert_eq!(raw_test_snapshot(&child), child_before);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn acl_refusal_diagnostics_identify_predicate_without_ace_payload() {
        let sid = LocalPsid::from_string("S-1-5-32-546").unwrap();
        let mut ordinary = vec![0u8; ACE_FIXED];
        ordinary.extend_from_slice(sid.as_bytes());
        let valid_header = ACE_HEADER {
            AceType: 0,
            AceFlags: 3,
            AceSize: ordinary.len() as u16,
        };
        assert!(ordinary_ace_refusal(&valid_header, &ordinary).is_none());
        for reason in [
            "unsupported_type",
            "unsupported_flags",
            "truncated_sid_header",
            "sid_revision",
            "sid_subauthority_count",
            "sid_length",
        ] {
            let mut header = valid_header;
            let mut bytes = ordinary.clone();
            match reason {
                "unsupported_type" => {
                    header.AceType = 9;
                    bytes.extend_from_slice(b"private-condition-must-not-appear");
                }
                "unsupported_flags" => header.AceFlags = 0x80,
                "truncated_sid_header" => bytes.truncate(ACE_FIXED + 4),
                "sid_revision" => bytes[ACE_FIXED] = 2,
                "sid_subauthority_count" => bytes[ACE_FIXED + 1] = 16,
                "sid_length" => bytes.push(0),
                _ => unreachable!(),
            }
            header.AceSize = bytes.len() as u16;
            let diagnostic = ordinary_ace_refusal(&header, &bytes).unwrap();
            assert!(
                diagnostic.starts_with(&format!("reason={reason} ")),
                "{diagnostic}"
            );
            assert!(diagnostic.contains(&format!("ace_size={}", bytes.len())));
            assert!(!diagnostic.contains("private-condition"));
            assert!(!diagnostic.contains("S-1-5-32-546"));
            if reason == "unsupported_type" {
                assert!(diagnostic.contains("ace_type=9"));
                assert!(diagnostic.contains("sid_shape=unavailable"));
                assert!(!diagnostic.contains("sid_revision="));
            }
        }
    }

    fn set_critical_test_dacl(parent: &std::path::Path, control: &str, ace: &str) {
        use windows::Win32::Security::SetFileSecurityW;
        let owner = crate::sid::current_user_sid().unwrap();
        let descriptor = OwnedSd::from_sddl(&format!(
            "D:{control}(A;OICI;FA;;;{owner})(A;OICI;FA;;;SY){ace}"
        ))
        .unwrap();
        unsafe {
            SetFileSecurityW(
                pcwstr(&wstr(parent.to_str().unwrap())),
                DACL_SECURITY_INFORMATION,
                descriptor.ptr,
            )
            .unwrap();
        }
    }

    #[test]
    fn critical_allow_survives_grant_revoke_and_object_only_updates() {
        const TARGET: &str = "S-1-5-32-546";
        const CRITICAL: &str = "S-1-15-2-111-222-333-444-555-666-777";
        for (mode, control) in [("protected", "PARAI"), ("automatic", "ARAI")] {
            let (root, parent) = raw_fixture(mode);
            set_critical_test_dacl(&parent, control, &format!("(A;OICICR;FR;;;{CRITICAL})"));
            let path = parent.to_str().unwrap();
            if mode == "automatic" {
                // SetFileSecurityW stores the supplied non-propagating raw
                // descriptor. Establish the normal inherited baseline before
                // testing a deliberate propagating grant/revoke round trip.
                let initial = raw_test_snapshot(&parent);
                unsafe {
                    SetNamedSecurityInfoW(
                        pcwstr(&wstr(path)),
                        SE_FILE_OBJECT,
                        DACL_SECURITY_INFORMATION | UNPROTECTED_DACL_SECURITY_INFORMATION,
                        None,
                        None,
                        Some(initial.acl()),
                        None,
                    )
                }
                .ok()
                .unwrap();
            }
            let child = parent.join("existing");
            std::fs::create_dir(&child).unwrap();
            let initial = raw_test_snapshot(&parent);
            let child_initial = raw_test_snapshot(&child);
            let critical = critical_aces(initial.acl()).unwrap();
            assert_eq!(critical.len(), 1);
            assert_eq!(critical[0][1], 0x23);
            let target = LocalPsid::from_string(TARGET).unwrap();
            let grant = SbAceSet {
                grant: Some(GrantMask::Modify),
                ..Default::default()
            };
            apply_sandbox_aces(path, TARGET, grant).unwrap();
            let granted = raw_test_snapshot(&parent);
            assert_eq!(critical_aces(granted.acl()).unwrap(), critical);
            assert_eq!(granted.control, initial.control);
            assert_eq!(
                critical_aces(raw_test_snapshot(&child).acl()).unwrap(),
                critical_aces(child_initial.acl()).unwrap()
            );
            assert!(
                !filter_aces(raw_test_snapshot(&child).acl(), |_, bytes| ace_sid_is(
                    bytes,
                    target.as_bytes()
                ))
                .unwrap()
                .0
                .is_empty(),
                "the intentional grant propagates to the existing child"
            );
            apply_sandbox_aces(path, TARGET, grant).unwrap();
            assert_eq!(raw_test_snapshot(&parent), granted);
            apply_sandbox_aces(path, TARGET, SbAceSet::default()).unwrap();
            assert_eq!(
                raw_test_snapshot(&parent),
                initial,
                "{mode}: exact grant revoke"
            );
            assert_eq!(
                raw_test_snapshot(&child),
                child_initial,
                "{mode}: propagated revoke"
            );
            let guard = SbAceSet {
                deny_fdc: true,
                deny_delete: true,
                ..Default::default()
            };
            apply_sandbox_aces(path, TARGET, guard).unwrap();
            let guarded = raw_test_snapshot(&parent);
            assert_eq!(guarded.control, initial.control);
            let unrelated: Vec<_> = guarded
                .aces()
                .unwrap()
                .into_iter()
                .filter(|ace| !ace_sid_is(ace, target.as_bytes()))
                .collect();
            assert_eq!(unrelated, initial.aces().unwrap());
            assert_eq!(raw_test_snapshot(&child), child_initial);
            let later = parent.join("later");
            std::fs::create_dir(&later).unwrap();
            assert_eq!(raw_test_snapshot(&later), child_initial);
            for denied in [Mask::DELETE, Mask::FILE_DELETE_CHILD] {
                assert!(!guest_restricted_access(&parent, denied.bits()));
            }
            apply_sandbox_aces(path, TARGET, SbAceSet::default()).unwrap();
            assert_eq!(raw_test_snapshot(&parent), initial);
            assert_eq!(raw_test_snapshot(&child), child_initial);
            std::fs::remove_dir_all(root).unwrap();
        }
    }

    #[test]
    fn critical_target_and_inherited_refuse_before_mutation() {
        const TARGET: &str = "S-1-5-32-546";
        for (ace, use_child, reason) in [
            ("(A;OICICR;FR;;;S-1-5-32-546)", false, "critical_target"),
            ("(A;OICICR;FR;;;BU)", true, "critical_inherited"),
        ] {
            let (root, parent) = raw_fixture("protected");
            set_critical_test_dacl(&parent, "PARAI", ace);
            let path = if use_child {
                let child = parent.join("child");
                std::fs::create_dir(&child).unwrap();
                child
            } else {
                parent
            };
            let before = raw_test_snapshot(&path);
            let id = crate::path_id::capture_file_id(path.to_str().unwrap()).unwrap();
            let error =
                preflight_sandbox_aces_for_id(path.to_str().unwrap(), TARGET, id).unwrap_err();
            assert!(format!("{error:#}").contains(reason), "{error:#}");
            for requested in [
                SbAceSet {
                    grant: Some(GrantMask::Modify),
                    ..Default::default()
                },
                SbAceSet {
                    deny_fdc: true,
                    ..Default::default()
                },
                SbAceSet::default(),
            ] {
                let error =
                    apply_sandbox_aces(path.to_str().unwrap(), TARGET, requested).unwrap_err();
                assert!(format!("{error:#}").contains(reason), "{error:#}");
                assert_eq!(raw_test_snapshot(&path), before);
            }
            std::fs::remove_dir_all(root).unwrap();
        }
    }

    #[test]
    #[ignore = "creates one fresh owned current-user AppContainer profile; explicit opt-in"]
    fn fresh_appcontainer_profile_preserves_critical_allow() {
        assert_eq!(
            std::env::var("ALYSIS_TEST_FRESH_PROFILE_CRITICAL_ACL").as_deref(),
            Ok("1")
        );
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let name = format!("Alysis.Execution.{nonce:032x}");
        // create() rejects an existing name. Cleanup is permitted only after
        // this exact fresh creation succeeded; never derive/adopt an old one.
        crate::appcontainer::create(&name, &[]).unwrap();
        let result = std::panic::catch_unwind(|| -> Result<()> {
            let environment = crate::appcontainer::environment(&name)?;
            let path = &environment
                .iter()
                .find(|(key, _)| key == "USERPROFILE")
                .unwrap()
                .1;
            let before = raw_test_snapshot(std::path::Path::new(path));
            let critical = critical_aces(before.acl())?;
            ensure!(
                !critical.is_empty(),
                "fresh OS profile lacks expected critical allow"
            );
            ensure!(
                critical
                    .iter()
                    .all(|ace| ace[0] == 0 && ace[1] & 0x30 == 0x20),
                "unexpected fresh-profile critical shape"
            );
            let target = "S-1-5-21-1-2-3-987654";
            preflight_sandbox_aces_for_id(path, target, crate::path_id::capture_file_id(path)?)?;
            apply_sandbox_aces(
                path,
                target,
                SbAceSet {
                    grant: Some(GrantMask::Modify),
                    ..Default::default()
                },
            )?;
            ensure!(
                critical_aces(raw_test_snapshot(std::path::Path::new(path)).acl())? == critical,
                "fresh profile critical grant mismatch"
            );
            apply_sandbox_aces(path, target, SbAceSet::default())?;
            ensure!(
                critical_aces(raw_test_snapshot(std::path::Path::new(path)).acl())? == critical,
                "fresh profile critical revoke mismatch"
            );
            println!(
                "fresh_profile_critical_acl_pass critical_count={}",
                critical.len()
            );
            Ok(())
        });
        let cleanup = crate::appcontainer::delete(&name);
        println!(
            "fresh_profile_cleanup name={name} success={}",
            cleanup.is_ok()
        );
        cleanup.unwrap();
        result.unwrap().unwrap();
    }

    #[test]
    fn critical_deny_and_unknown_flags_remain_refused() {
        let sid = LocalPsid::from_string("S-1-5-32-546").unwrap();
        for (ace_type, flags, reason) in [
            (1, 0x20, "critical_deny"),
            (0, 0x40, "unsupported_flags"),
            (0, 0x80, "unsupported_flags"),
        ] {
            // Windows SDDL rejects a critical deny itself (ERROR_INVALID_FLAGS).
            // Mutate only this owned in-memory descriptor to exercise our
            // independent validation of a malformed ACL; never write it.
            let descriptor = OwnedSd::from_sddl("D:(A;;FR;;;BU)").unwrap();
            let mut acl = std::ptr::null_mut();
            let mut present = false.into();
            let mut defaulted = false.into();
            unsafe {
                windows::Win32::Security::GetSecurityDescriptorDacl(
                    descriptor.ptr,
                    &mut present,
                    &mut acl,
                    &mut defaulted,
                )
                .unwrap();
                let mut ace = std::ptr::null_mut();
                GetAce(acl, 0, &mut ace).unwrap();
                let header = &mut *ace.cast::<ACE_HEADER>();
                header.AceType = ace_type;
                header.AceFlags = flags;
            }
            let error = selected_sid_aces(acl, sid.as_bytes()).unwrap_err();
            assert!(format!("{error:#}").contains(reason), "{error:#}");
        }
    }

    #[test]
    fn object_only_acl_refuses_object_ace_before_mutation() {
        use windows::Win32::Security::SetFileSecurityW;
        let (root, parent) = raw_fixture("protected");
        let owner = crate::sid::current_user_sid().unwrap();
        let descriptor = OwnedSd::from_sddl(&format!(
            "D:P(A;;FA;;;{owner})(A;;FA;;;SY)(OA;;FR;11111111-1111-1111-1111-111111111111;;BU)"
        ))
        .unwrap();
        unsafe {
            SetFileSecurityW(
                pcwstr(&wstr(parent.to_str().unwrap())),
                DACL_SECURITY_INFORMATION,
                descriptor.ptr,
            )
        }
        .unwrap();
        let before = raw_test_snapshot(&parent);
        // The unsupported trustee is unrelated. New grants must not succeed
        // if their later empty-set cleanup would refuse this descriptor.
        for requested in [
            SbAceSet {
                grant: Some(GrantMask::ReadOnly),
                ..Default::default()
            },
            SbAceSet {
                deny_fdc: true,
                ..Default::default()
            },
            SbAceSet::default(),
        ] {
            let error = apply_sandbox_aces(parent.to_str().unwrap(), "S-1-5-32-546", requested)
                .unwrap_err();
            let diagnostic = format!("{error:#}");
            assert!(diagnostic.contains("ordinary bounded"), "{diagnostic}");
            assert!(diagnostic.contains("canonical_path="), "{diagnostic}");
            assert!(diagnostic.contains("view=raw"), "{diagnostic}");
            assert!(
                diagnostic.contains("reason=unsupported_type ace_type=5"),
                "{diagnostic}"
            );
            assert!(diagnostic.contains("sid_shape=unavailable"), "{diagnostic}");
            assert!(!diagnostic.contains("S-1-5-32-546"), "{diagnostic}");
            assert_eq!(raw_test_snapshot(&parent), before);
        }
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn inheritable_grant_removal_still_propagates_after_object_only_admission() {
        const SID: &str = "S-1-5-32-546";
        let (root, parent) = raw_fixture("automatic");
        let child = parent.join("child");
        std::fs::create_dir(&child).unwrap();
        let sid = LocalPsid::from_string(SID).unwrap();
        apply_sandbox_aces(
            parent.to_str().unwrap(),
            SID,
            SbAceSet {
                grant: Some(GrantMask::ReadOnly),
                ..Default::default()
            },
        )
        .unwrap();
        let granted = raw_test_snapshot(&child);
        assert!(
            !selected_sid_aces(granted.acl(), sid.as_bytes())
                .unwrap()
                .is_empty()
        );
        apply_sandbox_aces(parent.to_str().unwrap(), SID, SbAceSet::default()).unwrap();
        assert!(
            selected_sid_aces(raw_test_snapshot(&parent).acl(), sid.as_bytes())
                .unwrap()
                .is_empty()
        );
        assert!(
            selected_sid_aces(raw_test_snapshot(&child).acl(), sid.as_bytes())
                .unwrap()
                .is_empty()
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn recorded_identity_rejects_replacement_without_changing_either_acl() {
        const FAKE_SB_SID: &str = "S-1-5-32-546";
        let dir = std::env::temp_dir().join(format!("alysis-acl-identity-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let original = dir.join("original");
        let moved = dir.join("moved");
        std::fs::create_dir(&original).unwrap();
        let id = crate::path_id::capture_file_id(original.to_str().unwrap()).unwrap();
        std::fs::rename(&original, &moved).unwrap();
        std::fs::create_dir(&original).unwrap();
        let snapshot = |path: &std::path::Path| {
            let (_sd, acl) = read_file_dacl(path.to_str().unwrap()).unwrap();
            unsafe { std::slice::from_raw_parts(acl.cast::<u8>(), (*acl).AclSize as usize) }
                .to_vec()
        };
        let before = (snapshot(&original), snapshot(&moved));
        let preflight_error =
            preflight_sandbox_aces_for_id(original.to_str().unwrap(), FAKE_SB_SID, id).unwrap_err();
        assert!(preflight_error.to_string().contains("target changed"));
        preflight_sandbox_aces_for_id(moved.to_str().unwrap(), FAKE_SB_SID, id).unwrap();
        assert_eq!(
            (snapshot(&original), snapshot(&moved)),
            before,
            "preflight is read-only"
        );
        let error = apply_sandbox_aces_for_id(
            original.to_str().unwrap(),
            FAKE_SB_SID,
            SbAceSet {
                grant: Some(GrantMask::Modify),
                ..Default::default()
            },
            Some(id),
        )
        .unwrap_err();
        assert!(error.to_string().contains("target changed"));
        assert_eq!((snapshot(&original), snapshot(&moved)), before);
        // Recovery can revoke/apply through the new name only when that same
        // open handle proves the recorded identity.
        apply_sandbox_aces_for_id(
            moved.to_str().unwrap(),
            FAKE_SB_SID,
            SbAceSet::default(),
            Some(id),
        )
        .unwrap();
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn pinned_acl_path_prevents_leaf_and_ancestor_replacement() {
        let dir = std::env::temp_dir().join(format!("alysis-acl-pin-{}", std::process::id()));
        let parent = dir.join("parent");
        let leaf = parent.join("leaf");
        std::fs::create_dir_all(&leaf).unwrap();
        let pinned = LockedAclTarget::open(leaf.to_str().unwrap(), None).unwrap();
        assert!(std::fs::rename(&leaf, parent.join("moved-leaf")).is_err());
        assert!(std::fs::rename(&parent, dir.join("moved-parent")).is_err());
        drop(pinned);
        std::fs::rename(&parent, dir.join("moved-parent")).unwrap();
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn workspace_owner_grant_is_idempotent_and_rejects_foreign_owner() {
        use windows::Win32::Storage::FileSystem::CreateDirectoryW;

        let owner = crate::sid::current_user_sid().unwrap();
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("alysis-owner-{}-{nonce}", std::process::id()));
        let path = dir.to_str().unwrap();
        // Elevated Windows CI tokens may default new objects to Administrators.
        // This fixture needs the exact user owner required by production, set at
        // exclusive creation rather than by adopting/chowning an existing path.
        // Start with object-only user access so the inheritable grant is tested.
        let descriptor =
            OwnedSd::from_sddl(&format!("O:{owner}D:P(A;;FA;;;{owner})(A;OICI;FA;;;SY)")).unwrap();
        let attributes = SECURITY_ATTRIBUTES {
            nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: descriptor.ptr.0,
            bInheritHandle: false.into(),
        };
        unsafe { CreateDirectoryW(pcwstr(&wstr(path)), Some(&attributes)) }.unwrap();
        ensure_workspace_owner_access(path, &owner).unwrap();
        let (_sd, dacl) = read_file_dacl(path).unwrap();
        let before =
            unsafe { std::slice::from_raw_parts(dacl.cast::<u8>(), (*dacl).AclSize as usize) }
                .to_vec();
        ensure_workspace_owner_access(path, &owner).unwrap();
        assert!(ensure_workspace_owner_access(path, "S-1-5-32-546").is_err());
        let (_sd, dacl) = read_file_dacl(path).unwrap();
        let after =
            unsafe { std::slice::from_raw_parts(dacl.cast::<u8>(), (*dacl).AclSize as usize) }
                .to_vec();
        assert_eq!(before, after);
        std::fs::remove_dir(dir).unwrap();
    }

    #[test]
    fn mask_consts_regression() {
        let m = Mask::MODIFY_NO_FDC;
        assert_eq!(m.bits(), 0x0013_01bf);
        assert_ne!(m.bits() & Mask::DELETE.bits(), 0, "must carry DELETE");
        assert_eq!(m.bits() & Mask::FILE_DELETE_CHILD.bits(), 0);
        assert_eq!(m.bits() & 0xffe0_0000, 0, "stray high bits");

        let ow = Allow::OWNER_RIGHTS.1;
        assert_eq!(ow.bits(), Mask::READ_CONTROL.bits());
        assert_ne!(ow.bits(), 0);
        assert_eq!(ow.bits() & Mask::WRITE_DAC.bits(), 0);
    }
}
