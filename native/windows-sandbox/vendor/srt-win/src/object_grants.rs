//! Experimental, explicit-object read grants. No recursive ACL setter is used.
//!
//! `NtSetSecurityObject` updates the pinned object with minimum WRITE_DAC
//! authority, without SetSecurityInfo's hierarchy propagation. This narrow
//! primitive preserves every inherited/inheritable ACE and all DACL control;
//! only explicit non-inheriting grants may change. Every
//! existing object is pinned and journaled before the first mutation. New
//! descendants receive none of this module's grants. This is not a writable-
//! workspace implementation and does not change the baseline runner.
//!
//! The host owner/admin and the caller supplying policy are trusted. Concurrent
//! host ACL edits are detected when observable, not synchronized by this API.
//! No whole security descriptor, owner, or unrelated ACE is intentionally
//! changed. Revocation refuses descriptor drift instead of guessing a repair.

use super::*;
use crate::{account_state::CandidateJournalIdentity, path_id::FileId, util::OwnedHandle};
use serde::{Deserialize, Serialize};
use std::{collections::HashSet, path::Path};
use windows::Wdk::Storage::FileSystem::NtSetSecurityObject;
use windows::Win32::{
    Foundation::HANDLE,
    Security::{IsValidAcl, IsValidSid, SECURITY_DESCRIPTOR_CONTROL},
    Storage::FileSystem::{
        BY_HANDLE_FILE_INFORMATION, CreateFileW, FILE_ATTRIBUTE_DIRECTORY,
        FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT,
        FILE_NAME_NORMALIZED, FILE_SHARE_READ, FILE_SHARE_WRITE, FILE_STANDARD_INFO,
        FileStandardInfo, GETFINALPATHNAMEBYHANDLE_FLAGS, GetFileInformationByHandle,
        GetFileInformationByHandleEx, GetFinalPathNameByHandleW, OPEN_EXISTING, VOLUME_NAME_DOS,
    },
};

#[path = "object_grant_journal.rs"]
mod journal;
pub use journal::ReadTreeJournal;

// Version 1 recorded GetSecurityInfo's reconstructed view, not stored ACEs.
// It cannot prove restoration of the original raw descriptor; never migrate it.
const RECORD_VERSION: u32 = 2;
const DACL_CONTROL: u16 = 0x150c; // PRESENT, DEFAULTED, AUTO_INHERIT_REQ/ED, PROTECTED.
const MAX_OBJECTS: usize = 1024;
const MAX_DEPTH: usize = 64;
const MAX_RECORD_BYTES: usize = 256 * 1024;
const MAX_TREE_BYTES: usize = 8 * 1024 * 1024;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Snapshot {
    pub(crate) revision: u32,
    pub(crate) control: u16,
    pub(crate) owner: String,
    pub(crate) aces: Vec<Vec<u8>>,
}

impl Snapshot {
    pub(crate) fn read(handle: HANDLE) -> Result<Self> {
        use windows::Wdk::Storage::FileSystem::NtQuerySecurityObject;

        // Query OWNER+DACL together from the retained object. GetSecurityInfo
        // can reconstruct legacy ACE inheritance/order even without a write.
        // usize storage is aligned for native descriptor/SID/ACL access.
        const CAPACITY: usize = 128 * 1024;
        const HEADER: usize = 20; // SECURITY_DESCRIPTOR_RELATIVE
        let mut words = vec![0usize; CAPACITY / size_of::<usize>()];
        let descriptor = PSECURITY_DESCRIPTOR(words.as_mut_ptr().cast());
        let mut returned = 0;
        unsafe {
            NtQuerySecurityObject(
                handle,
                (DACL_SECURITY_INFORMATION | OWNER_SECURITY_INFORMATION).0,
                Some(descriptor),
                CAPACITY as u32,
                &mut returned,
            )
        }
        .ok()
        .context("read pinned raw object grant descriptor")?;
        ensure!(
            (HEADER..=CAPACITY).contains(&(returned as usize)),
            "invalid raw object descriptor length"
        );
        let bytes =
            unsafe { std::slice::from_raw_parts(words.as_ptr().cast::<u8>(), returned as usize) };
        Self::from_raw_descriptor(bytes)
    }

    pub(crate) fn from_raw_descriptor(bytes: &[u8]) -> Result<Self> {
        const HEADER: usize = 20; // SECURITY_DESCRIPTOR_RELATIVE
        ensure!(
            (HEADER..=128 * 1024).contains(&bytes.len()),
            "invalid raw object descriptor length"
        );
        ensure!(
            bytes.as_ptr() as usize % 4 == 0,
            "raw object descriptor storage is not DWORD aligned"
        );
        ensure!(bytes[0] == 1, "unsupported raw object descriptor revision");
        let control = u16::from_le_bytes(bytes[2..4].try_into().unwrap());
        ensure!(
            control & 0x8000 != 0 && control & 4 != 0,
            "object grant requires a self-relative descriptor with a DACL"
        );
        let span = |offset: usize, length: usize| -> Result<&[u8]> {
            ensure!(
                offset >= HEADER && offset % 4 == 0,
                "raw object descriptor has an invalid component offset"
            );
            let end = offset
                .checked_add(length)
                .context("raw object component overflow")?;
            bytes
                .get(offset..end)
                .context("raw object component exceeds descriptor")
        };
        let owner_offset = u32::from_le_bytes(bytes[4..8].try_into().unwrap()) as usize;
        let owner_header = span(owner_offset, 8)?;
        ensure!(
            owner_header[0] == 1 && owner_header[1] <= 15,
            "raw object owner SID has an invalid header"
        );
        let owner_bytes = span(owner_offset, 8 + usize::from(owner_header[1]) * 4)?;
        let owner = PSID(owner_bytes.as_ptr().cast_mut().cast());
        ensure!(
            unsafe { IsValidSid(owner) }.as_bool(),
            "invalid raw object owner SID"
        );

        let acl_offset = u32::from_le_bytes(bytes[16..20].try_into().unwrap()) as usize;
        let acl_header = span(acl_offset, size_of::<ACL>())?;
        let acl_size = u16::from_le_bytes(acl_header[2..4].try_into().unwrap()) as usize;
        ensure!(acl_size >= size_of::<ACL>(), "invalid raw object DACL size");
        let acl_bytes = span(acl_offset, acl_size)?;
        ensure!(
            unsafe { IsValidAcl(acl_bytes.as_ptr().cast()) }.as_bool(),
            "invalid raw object DACL"
        );
        let count = u16::from_le_bytes(acl_header[4..6].try_into().unwrap());
        let mut offset = size_of::<ACL>();
        let mut aces = Vec::new();
        for _ in 0..count {
            let header = acl_bytes
                .get(offset..offset + size_of::<ACE_HEADER>())
                .context("raw object ACE header exceeds DACL")?;
            let length = u16::from_le_bytes(header[2..4].try_into().unwrap()) as usize;
            ensure!(
                length >= 8 && length % 4 == 0,
                "invalid raw object ACE size"
            );
            let end = offset
                .checked_add(length)
                .context("raw object ACE overflow")?;
            aces.push(
                acl_bytes
                    .get(offset..end)
                    .context("raw object ACE exceeds DACL")?
                    .to_vec(),
            );
            offset = end;
        }
        Ok(Self {
            revision: u32::from(acl_header[0]),
            control: control & DACL_CONTROL,
            owner: crate::sid::psid_to_string(owner)?,
            aces,
        })
    }

    pub(crate) fn acl(&self) -> Result<BuiltAcl> {
        ensure!(
            self.revision == 2 || self.revision == 4,
            "unsupported recorded ACL revision"
        );
        let mut bytes = 0;
        let mut pointers = Vec::new();
        for ace in &self.aces {
            ensure!(
                ace.len() >= 8 && ace.len() <= u16::MAX as usize && ace.len() % 4 == 0,
                "invalid recorded ACE length"
            );
            ensure!(
                u16::from_le_bytes([ace[2], ace[3]]) as usize == ace.len(),
                "recorded ACE header/length mismatch"
            );
            pointers.push((ace.as_ptr().cast(), ace.len() as u16));
            bytes += ace.len();
        }
        ensure!(
            bytes + size_of::<ACL>() <= u16::MAX as usize,
            "recorded ACL too large"
        );
        let revision = ACE_REVISION(self.revision);
        let acl = rebuild_acl(revision, &[], &(pointers, bytes, revision), &[])?;
        ensure!(
            unsafe { IsValidAcl(acl.as_ptr()) }.as_bool(),
            "invalid recorded ACL"
        );
        Ok(acl)
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Record {
    version: u32,
    path: String,
    file_id: [u8; 24],
    directory: bool,
    before: Snapshot,
    // Exact added ACE bytes, never an instruction to replace all ACEs for a SID.
    added: Vec<Vec<u8>>,
}

impl Record {
    fn after(&self) -> Snapshot {
        let mut after = self.before.clone();
        // Windows places explicit entries before inherited entries when
        // applying an unprotected DACL. Admission rejects inherited denies so
        // this insertion cannot jump ahead of an existing denial.
        let inherited = after
            .aces
            .iter()
            .position(|ace| ace[1] & INHERITED_ACE != 0)
            .unwrap_or(after.aces.len());
        after
            .aces
            .splice(inherited..inherited, self.added.iter().cloned());
        after
    }

    fn validate(&self, identity: &CandidateJournalIdentity) -> Result<()> {
        ensure!(
            self.version == RECORD_VERSION,
            "unsupported object grant record; preserve for explicit recovery"
        );
        ensure!(
            strict_path(&self.path)? == self.path,
            "noncanonical recorded grant path"
        );
        FileId::from_bytes(&self.file_id)?;
        ensure!(
            self.before.control & !DACL_CONTROL == 0 && self.before.control & 4 != 0,
            "invalid recorded DACL control"
        );
        LocalPsid::from_string(&self.before.owner)?;
        self.before.acl()?;
        let expected = additions(&self.before, identity)?;
        ensure!(
            self.added == expected,
            "recorded grants differ from exact read-only policy"
        );
        self.after().acl()?;
        Ok(())
    }
}

fn additions(before: &Snapshot, identity: &CandidateJournalIdentity) -> Result<Vec<Vec<u8>>> {
    // Defaulted descriptors and outstanding inheritance requests are outside
    // the exact-object contract. A nonautomatic unprotected DACL is preserved
    // by the native setter, without importing unrelated parent ACEs.
    ensure!(
        before.control & 0x108 == 0,
        "DACL inheritance control is unsupported for an exact object grant"
    );
    let mut inherited_seen = false;
    for ace in &before.aces {
        ensure!(ace.len() >= 8, "invalid original ACE");
        if ace[1] & INHERITED_ACE != 0 {
            inherited_seen = true;
            ensure!(
                ace[0] == 0,
                "inherited deny or unsupported ACE prevents a precedence-preserving grant"
            );
        } else {
            ensure!(
                !inherited_seen,
                "noncanonical explicit/inherited ACE order is unsupported"
            );
        }
    }
    let package = LocalPsid::from_string(&identity.package_sid)?;
    ensure!(
        !before.aces.iter().any(|ace| ace
            .windows(package.as_bytes().len())
            .any(|part| part == package.as_bytes())),
        "execution package already occurs in an object DACL; refusing to adopt its grant"
    );
    let acl = build_allow_dacl(&[
        Allow(
            &identity.binding.account_sid,
            Mask::FILE_READ_EXEC,
            NO_INHERIT,
        ),
        Allow(&identity.package_sid, Mask::FILE_READ_EXEC, NO_INHERIT),
    ])?;
    let mut result = Vec::new();
    filter_aces(acl.as_ptr(), |_, bytes| {
        if !before.aces.iter().any(|old| old == bytes) {
            result.push(bytes.to_vec());
        }
        true
    })?;
    Ok(result)
}

/// Only local, unambiguous lexical names. Final handle names are checked too;
/// accepting an 8.3 alias or resolving a reparse point is not normalization.
fn strict_path(path: &str) -> Result<String> {
    let path = path.strip_prefix(r"\\?\").unwrap_or(path);
    let bytes = path.as_bytes();
    ensure!(
        bytes.len() > 3 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':' && bytes[2] == b'\\',
        "object grants require a local absolute non-root path"
    );
    for part in path[3..].split('\\') {
        ensure!(
            !part.is_empty()
                && part != "."
                && part != ".."
                && !part.ends_with(['.', ' '])
                && !part.chars().any(|c| c < ' ' || "/:*?\"<>|".contains(c)),
            "object grant path contains an alias or unsupported component"
        );
    }
    Ok(format!(r"\\?\{path}"))
}

struct Pinned {
    handle: OwnedHandle,
    _parents: Vec<OwnedHandle>,
    path: String,
    id: FileId,
    directory: bool,
}

impl Pinned {
    fn open(path: &str, expected: Option<FileId>, granting: bool) -> Result<Self> {
        Self::open_inner(path, expected, granting, true)
    }

    // Inventory descendants already have their entire ancestor chain in the
    // retained objects vector. Do not reopen those names during inventory.
    fn open_inner(
        path: &str,
        expected: Option<FileId>,
        granting: bool,
        pin_parents: bool,
    ) -> Result<Self> {
        let path = strict_path(path)?;
        let mut ancestors: Vec<_> = Path::new(&path).ancestors().skip(1).collect();
        ancestors.reverse();
        ensure!(
            ancestors.len() <= MAX_DEPTH,
            "object grant path is too deep"
        );
        let mut parents = Vec::new();
        if pin_parents {
            for parent in ancestors {
                parents.push(LockedAclTarget::open_component(parent, 0)?);
            }
        }
        let wide = wstr(&path);
        // Minimum authority avoids requesting DELETE, which conflicts with a
        // process cwd handle. Data/list access plus no delete sharing pins the
        // object, while READ_CONTROL/WRITE_DAC permit exact verification/update.
        let handle = OwnedHandle(
            unsafe {
                CreateFileW(
                    pcwstr(&wide),
                    Mask::READ_CONTROL.bits() | Mask::WRITE_DAC.bits() | 0x81,
                    FILE_SHARE_READ | FILE_SHARE_WRITE,
                    None,
                    OPEN_EXISTING,
                    FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                    None,
                )
            }
            .context("cannot pin object with exact ACL authority")?,
        );
        let id = crate::path_id::file_id_from_handle(handle.raw())?;
        if let Some(expected) = expected {
            ensure!(id == expected, "object grant target identity changed");
        }
        let mut info = BY_HANDLE_FILE_INFORMATION::default();
        unsafe {
            GetFileInformationByHandle(handle.raw(), &mut info)?;
        }
        let directory = info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY.0 != 0;
        let pinned = Self {
            handle,
            _parents: parents,
            path,
            id,
            directory,
        };
        pinned.verify(granting)?;
        Ok(pinned)
    }

    fn verify(&self, granting: bool) -> Result<()> {
        let mut basic = BY_HANDLE_FILE_INFORMATION::default();
        let mut standard = FILE_STANDARD_INFO::default();
        unsafe {
            GetFileInformationByHandle(self.handle.raw(), &mut basic)?;
            GetFileInformationByHandleEx(
                self.handle.raw(),
                FileStandardInfo,
                (&mut standard as *mut FILE_STANDARD_INFO).cast(),
                size_of::<FILE_STANDARD_INFO>() as u32,
            )?;
        }
        ensure!(
            basic.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT.0 == 0,
            "object grant target is a reparse point"
        );
        ensure!(
            !standard.DeletePending && standard.NumberOfLinks != 0,
            "object grant target is delete-pending"
        );
        ensure!(
            standard.Directory == self.directory,
            "object grant target kind changed"
        );
        ensure!(
            !granting || self.directory || standard.NumberOfLinks == 1,
            "object grants refuse multiply linked files"
        );
        ensure!(
            crate::path_id::file_id_from_handle(self.handle.raw())? == self.id,
            "object grant target identity changed"
        );
        let mut name = vec![0u16; 32768];
        let size = unsafe {
            GetFinalPathNameByHandleW(
                self.handle.raw(),
                &mut name,
                GETFINALPATHNAMEBYHANDLE_FLAGS(FILE_NAME_NORMALIZED.0 | VOLUME_NAME_DOS.0),
            )
        } as usize;
        ensure!(
            size != 0 && size < name.len(),
            "cannot verify pinned object final path"
        );
        let name = String::from_utf16(&name[..size]).context("non-Unicode object grant path")?;
        ensure!(
            name.eq_ignore_ascii_case(&self.path),
            "object grant path is an alias"
        );
        Ok(())
    }

    fn write(&self, expected: &Snapshot, wanted: &Snapshot, granting: bool) -> Result<()> {
        self.verify(granting)?;
        ensure!(
            Snapshot::read(self.handle.raw())? == *expected,
            "object descriptor changed; preserve recovery state for explicit repair"
        );
        ensure!(
            expected.owner == wanted.owner && expected.control == wanted.control,
            "object grant cannot change owner or inheritance control"
        );
        ensure!(
            expected
                .aces
                .iter()
                .filter(|ace| ace[1] & (0x1f | CRITICAL_ACE_FLAG as u8) != 0)
                .eq(wanted
                    .aces
                    .iter()
                    .filter(|ace| ace[1] & (0x1f | CRITICAL_ACE_FLAG as u8) != 0)),
            "object grant cannot change inherited or inheritable ACEs, or critical ACEs"
        );
        set_native_dacl(self.handle.raw(), wanted)?;
        let observed = Snapshot::read(self.handle.raw())?;
        #[cfg(test)]
        if observed != *wanted {
            eprintln!("object ACL readback expected {wanted:?}; observed {observed:?}");
        }
        ensure!(
            observed == *wanted,
            "non-propagating ACL readback mismatch; preserve recovery state"
        );
        Ok(())
    }
}

// This crate-private primitive requires Pinned::write or the disabled station
// Operation to check exact identity, intent, owner/control and policy first.
// The API contrast test also
// exercises it with a synthetic inheritable entry solely to detect propagation.
pub(crate) fn set_native_dacl(handle: HANDLE, wanted: &Snapshot) -> Result<()> {
    let acl = wanted.acl()?;
    let mut raw = SECURITY_DESCRIPTOR::default();
    let descriptor = PSECURITY_DESCRIPTOR((&mut raw as *mut SECURITY_DESCRIPTOR).cast());
    // NtSetSecurityObject consumes AUTO_INHERIT_REQ to retain AUTO_INHERITED;
    // copying AUTO_INHERITED alone clears it. This is input encoding, not a
    // changed desired control state. No request is made for nonautomatic ACLs.
    let control = (wanted.control & 0x1500)
        | if wanted.control & 0x400 != 0 {
            0x100
        } else {
            0
        };
    unsafe {
        InitializeSecurityDescriptor(descriptor, SECURITY_DESCRIPTOR_REVISION)?;
        SetSecurityDescriptorDacl(descriptor, true, Some(acl.as_ptr()), false)?;
        SetSecurityDescriptorControl(
            descriptor,
            SECURITY_DESCRIPTOR_CONTROL(0x1500),
            SECURITY_DESCRIPTOR_CONTROL(control),
        )?;
        NtSetSecurityObject(handle, DACL_SECURITY_INFORMATION.0, descriptor)
            .ok()
            .context("write pinned exact object grant")?;
    }
    Ok(())
}

/// An inventory of already-existing objects, pinned until this guard drops.
/// Preparation has no filesystem mutations. Limits are deliberate fail-closed
/// compatibility limits for this disabled prototype, not silent truncation.
pub struct PreparedReadTree {
    identity: CandidateJournalIdentity,
    objects: Vec<(Pinned, Record)>,
}

impl PreparedReadTree {
    pub fn prepare(root: &str, identity: &CandidateJournalIdentity) -> Result<Self> {
        Self::prepare_roots(&[root], identity)
    }

    /// Admit several disjoint read roots in one write-ahead transaction. A
    /// caller must resolve overlaps deliberately; broadening to a common parent
    /// or silently skipping a duplicate subtree is not performed here.
    pub fn prepare_roots(roots: &[&str], identity: &CandidateJournalIdentity) -> Result<Self> {
        identity.validate(&identity.binding)?;
        ensure!(
            !roots.is_empty() && roots.len() <= MAX_OBJECTS,
            "invalid read-root count"
        );
        let roots: Vec<String> = roots
            .iter()
            .map(|path| strict_path(path))
            .collect::<Result<_>>()?;
        for (index, path) in roots.iter().enumerate() {
            for other in &roots[..index] {
                let left = path.to_ascii_lowercase();
                let right = other.to_ascii_lowercase();
                ensure!(
                    left != right
                        && !left.starts_with(&format!("{right}\\"))
                        && !right.starts_with(&format!("{left}\\")),
                    "overlapping read roots are unsupported"
                );
            }
        }
        let mut objects = Vec::new();
        let mut queue: Vec<_> = roots.into_iter().map(|path| (path, 0usize)).collect();
        let mut identities = HashSet::new();
        let mut total = 0usize;
        while let Some((path, depth)) = queue.pop() {
            ensure!(
                depth <= MAX_DEPTH && objects.len() < MAX_OBJECTS,
                "read-tree inventory exceeds its bounded object/depth limit"
            );
            let pinned = Pinned::open_inner(&path, None, true, depth == 0)?;
            ensure!(
                identities.insert(pinned.id.as_bytes()),
                "read-tree repeats a file identity"
            );
            let before = Snapshot::read(pinned.handle.raw())?;
            let added = additions(&before, identity)?;
            let record = Record {
                version: RECORD_VERSION,
                path: pinned.path.clone(),
                file_id: pinned.id.as_bytes(),
                directory: pinned.directory,
                before,
                added,
            };
            record.validate(identity)?;
            let size = serde_json::to_vec(&record)?.len();
            total += size;
            ensure!(
                size <= MAX_RECORD_BYTES && total <= MAX_TREE_BYTES,
                "read-tree ACL inventory exceeds its bounded journal size"
            );
            if pinned.directory {
                for entry in std::fs::read_dir(&pinned.path)? {
                    let entry = entry?;
                    let path = entry
                        .path()
                        .to_str()
                        .context("non-Unicode read-tree entry")?
                        .to_owned();
                    queue.push((path, depth + 1));
                    ensure!(
                        queue.len() + objects.len() < MAX_OBJECTS,
                        "read-tree inventory exceeds its bounded object limit"
                    );
                }
            }
            objects.push((pinned, record));
        }
        Ok(Self {
            identity: identity.clone(),
            objects,
        })
    }

    pub fn object_count(&self) -> usize {
        self.objects.len()
    }

    /// The journal requires a pinned, durably published version-3 execution
    /// record and live authority. The caller must not launch until success.
    /// The per-object transaction commits before the first ACL write. Errors
    /// (including an ambiguous SQLite commit) leave recovery state intact.
    /// The final inventory check is not atomic: changes after an object's
    /// recheck, including after return, remain possible. This does not qualify
    /// writable trees, new-output inheritance, or general alias-race closure.
    pub fn grant(&self, journal: &ReadTreeJournal<'_>) -> Result<()> {
        #[cfg(test)]
        {
            self.grant_impl(journal, || {})
        }
        #[cfg(not(test))]
        {
            self.grant_impl(journal)
        }
    }

    fn grant_impl(
        &self,
        journal: &ReadTreeJournal<'_>,
        #[cfg(test)] after_writes: impl FnOnce(),
    ) -> Result<()> {
        ensure!(
            self.identity == journal.identity,
            "read-tree/journal identity mismatch"
        );
        journal.record(&self.objects)?;
        for (pinned, record) in &self.objects {
            pinned.write(&record.before, &record.after(), true)?;
        }
        #[cfg(test)]
        after_writes();
        self.verify_granted_inventory()
    }

    fn verify_granted_inventory(&self) -> Result<()> {
        // An earlier object can change while later grants are written. Recheck
        // every inventoried root/descendant through its retained handle, not
        // merely the last object. External ancestors retain their existing
        // no-delete-share pins; this adds no new external-ancestor identity audit.
        // Any refusal leaves the whole journal intact for stopped recovery,
        // without guessing a compensating ACL write or retiring an intent.
        for (pinned, record) in &self.objects {
            pinned.verify(true)?;
            ensure!(
                Snapshot::read(pinned.handle.raw())? == record.after(),
                "object descriptor changed after grants; preserve recovery state for explicit repair"
            );
        }
        Ok(())
    }
}

#[cfg(test)]
#[path = "object_grants_tests.rs"]
mod tests;
