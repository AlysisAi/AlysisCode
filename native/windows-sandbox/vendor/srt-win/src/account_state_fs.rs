//! Private filesystem admission for candidate SQLite state. Never repairs an
//! existing descriptor. Named opens are anchored by a retained no-delete-share
//! data/list handle on every previously validated lexical ancestor.

use crate::{
    path_id::{FileId, file_id_from_handle},
    util::{OwnedHandle, OwnedSd, pcwstr, win32_ok, wstr},
};
use anyhow::{Context, Result, ensure};
use std::path::{Path, PathBuf};
use windows::Wdk::{
    Foundation::OBJECT_ATTRIBUTES,
    Storage::FileSystem::{
        FILE_CREATE, FILE_DIRECTORY_FILE, FILE_OPEN, FILE_OPEN_REPARSE_POINT,
        FILE_SYNCHRONOUS_IO_NONALERT, NtCreateFile,
    },
};
use windows::Win32::{
    Foundation::{HANDLE, OBJ_CASE_INSENSITIVE, STATUS_OBJECT_NAME_COLLISION, UNICODE_STRING},
    Security::{
        ACCESS_ALLOWED_ACE, ACE_HEADER, ACL,
        Authorization::{GetSecurityInfo, SE_FILE_OBJECT},
        DACL_SECURITY_INFORMATION, GetAce, GetSecurityDescriptorControl, GetSecurityDescriptorDacl,
        GetSecurityDescriptorOwner, INHERIT_ONLY_ACE, IsValidAcl, OWNER_SECURITY_INFORMATION,
        PSECURITY_DESCRIPTOR, PSID, SE_DACL_PROTECTED, SECURITY_ATTRIBUTES,
    },
    Storage::FileSystem::{
        BY_HANDLE_FILE_INFORMATION, CREATE_NEW, CreateFileW, FILE_ACCESS_RIGHTS,
        FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS,
        FILE_FLAG_OPEN_REPARSE_POINT, FILE_NAME_NORMALIZED, FILE_SHARE_READ, FILE_SHARE_WRITE,
        FlushFileBuffers, GETFINALPATHNAMEBYHANDLE_FLAGS, GetDriveTypeW,
        GetFileInformationByHandle, GetFileSizeEx, GetFinalPathNameByHandleW,
        GetVolumeInformationW, OPEN_EXISTING, ReadFile, VOLUME_NAME_DOS, WriteFile,
    },
    System::IO::IO_STATUS_BLOCK,
    UI::Shell::{
        FOLDERID_LocalAppData, FOLDERID_ProgramFilesX64, KF_FLAG_DEFAULT, SHGetKnownFolderPath,
    },
};
use windows::core::PWSTR;

const READ_CONTROL: u32 = 0x20000;
const FILE_ALL: &str = "FA";

/// Encode only already-lexical local drive paths for the candidate's Win32
/// file opens. Extended syntax bypasses DOS normalization, so reject aliases
/// first; never canonicalize, resolve a link, or accept a caller-supplied prefix.
pub(super) fn extended_path(path: &Path) -> Result<String> {
    let text = path
        .to_str()
        .context("candidate state path is not Unicode")?;
    ensure!(
        text.len() >= 3
            && text.as_bytes()[0].is_ascii_alphabetic()
            && &text.as_bytes()[1..3] == b":\\",
        "candidate state requires an unprefixed absolute local drive path"
    );
    if text.len() > 3 {
        for component in text[3..].split('\\') {
            ensure!(
                !component.is_empty()
                    && component != "."
                    && component != ".."
                    && !component.ends_with([' ', '.'])
                    && !component
                        .chars()
                        .any(|c| c.is_control() || "/:*?\"<>|".contains(c)),
                "candidate state contains an ambiguous path component"
            );
            let base = component
                .split('.')
                .next()
                .unwrap()
                .trim_end_matches(' ')
                .to_ascii_uppercase();
            let numbered_device = |prefix: &str| {
                base.strip_prefix(prefix).is_some_and(|suffix| {
                    matches!(
                        suffix,
                        "1" | "2"
                            | "3"
                            | "4"
                            | "5"
                            | "6"
                            | "7"
                            | "8"
                            | "9"
                            | "\u{b9}"
                            | "\u{b2}"
                            | "\u{b3}"
                    )
                })
            };
            ensure!(
                !matches!(
                    base.as_str(),
                    "CON" | "PRN" | "AUX" | "NUL" | "CONIN$" | "CONOUT$"
                ) && !numbered_device("COM")
                    && !numbered_device("LPT"),
                "candidate state contains a DOS device alias"
            );
        }
    }
    let encoded = format!(r"\\?\{text}");
    ensure!(
        encoded.encode_utf16().count() + 1 <= 32767,
        "candidate state path exceeds the UTF-16 limit including prefix and terminator"
    );
    Ok(encoded)
}

pub(super) struct Policy {
    pub owner: String,
    pub offline: String,
    pub online: String,
    pub group: String,
}

impl Policy {
    pub fn sddl(&self, directory: bool) -> String {
        let flags = if directory { "OICI" } else { "" };
        format!(
            "O:{}D:P(D;{flags};{FILE_ALL};;;{})(D;{flags};{FILE_ALL};;;{})(D;{flags};{FILE_ALL};;;{})(A;{flags};{FILE_ALL};;;{})(A;{flags};{FILE_ALL};;;SY)(A;{flags};{FILE_ALL};;;BA)",
            self.owner, self.offline, self.online, self.group, self.owner
        )
    }

    pub fn validate(&self, object: &Pinned, directory: bool) -> Result<()> {
        self.validate_descriptor(&object.security()?, directory)
    }

    fn validate_descriptor(&self, descriptor: &OwnedSd, directory: bool) -> Result<()> {
        let mut owner = PSID::default();
        let mut defaulted = false.into();
        let mut control = 0;
        let mut revision = 0;
        unsafe {
            GetSecurityDescriptorOwner(descriptor.ptr, &mut owner, &mut defaulted)?;
            GetSecurityDescriptorControl(descriptor.ptr, &mut control, &mut revision)?;
        }
        ensure!(
            !owner.0.is_null() && crate::sid::psid_to_string(owner)? == self.owner,
            "candidate state has a foreign owner; refusing repair or adoption"
        );
        ensure!(
            control & SE_DACL_PROTECTED.0 != 0,
            "candidate state DACL is not protected"
        );
        ensure!(
            ace_bytes(descriptor)? == ace_bytes(&OwnedSd::from_sddl(&self.sddl(directory))?)?,
            "candidate state DACL differs from its exact private policy"
        );
        Ok(())
    }

    #[cfg(test)]
    pub fn validate_inherited_file(&self, object: &Pinned) -> Result<()> {
        let descriptor = object.security()?;
        let mut owner = PSID::default();
        let mut defaulted = false.into();
        unsafe {
            GetSecurityDescriptorOwner(descriptor.ptr, &mut owner, &mut defaulted)?;
        }
        ensure!(
            crate::sid::psid_to_string(owner)? == self.owner,
            "auxiliary file owner differs"
        );
        let mut expected = ace_bytes(&OwnedSd::from_sddl(&self.sddl(false))?)?;
        for ace in &mut expected {
            ace[1] = 0x10;
        }
        ensure!(
            ace_bytes(&descriptor)? == expected,
            "auxiliary file did not inherit exact private access policy"
        );
        Ok(())
    }
}

pub(super) fn ace_bytes(descriptor: &OwnedSd) -> Result<Vec<Vec<u8>>> {
    let mut present = false.into();
    let mut defaulted = false.into();
    let mut acl: *mut ACL = std::ptr::null_mut();
    unsafe {
        GetSecurityDescriptorDacl(descriptor.ptr, &mut present, &mut acl, &mut defaulted)?;
    }
    ensure!(
        present.as_bool() && !acl.is_null() && unsafe { IsValidAcl(acl) }.as_bool(),
        "candidate state DACL absent/null/invalid"
    );
    let mut result = Vec::new();
    for index in 0..unsafe { (*acl).AceCount } {
        let mut entry = std::ptr::null_mut();
        unsafe {
            GetAce(acl, u32::from(index), &mut entry)?;
        }
        let size = unsafe { (*entry.cast::<ACE_HEADER>()).AceSize } as usize;
        ensure!(
            size >= std::mem::size_of::<ACE_HEADER>(),
            "invalid state ACE"
        );
        result.push(unsafe { std::slice::from_raw_parts(entry.cast::<u8>(), size) }.to_vec());
    }
    Ok(result)
}

pub(super) struct Pinned {
    pub handle: OwnedHandle,
    pub id: FileId,
    pub path: PathBuf,
}

impl Pinned {
    pub fn security(&self) -> Result<OwnedSd> {
        let mut descriptor = PSECURITY_DESCRIPTOR::default();
        win32_ok(
            unsafe {
                GetSecurityInfo(
                    self.handle.raw(),
                    SE_FILE_OBJECT,
                    OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                    None,
                    None,
                    None,
                    None,
                    Some(&mut descriptor),
                )
            },
            "read candidate state security",
        )?;
        Ok(OwnedSd::from_raw(descriptor))
    }
    pub fn open(path: &Path, directory: bool, deny_write: bool) -> Result<Self> {
        Self::open_with(path, directory, deny_write, false, None)
    }

    pub fn create_file(path: &Path, policy: &Policy) -> Result<Self> {
        let descriptor = OwnedSd::from_sddl(&policy.sddl(false))?;
        Self::open_with(path, false, false, true, Some(&descriptor))
    }

    fn open_with(
        path: &Path,
        directory: bool,
        deny_write: bool,
        create: bool,
        descriptor: Option<&OwnedSd>,
    ) -> Result<Self> {
        let wide = wstr(&extended_path(path)?);
        let attributes = descriptor.map(|value| SECURITY_ATTRIBUTES {
            nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: value.ptr.0,
            bInheritHandle: false.into(),
        });
        // Actual data access is essential: metadata-only handles do not pin
        // names through Windows delete-sharing checks.
        let handle = OwnedHandle(
            unsafe {
                CreateFileW(
                    pcwstr(&wide),
                    1 | 0x80 | READ_CONTROL | if create { 2 } else { 0 },
                    FILE_SHARE_READ
                        | if deny_write {
                            Default::default()
                        } else {
                            FILE_SHARE_WRITE
                        },
                    attributes.as_ref().map(|value| value as *const _),
                    if create { CREATE_NEW } else { OPEN_EXISTING },
                    FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                    None,
                )
            }
            .with_context(|| format!("pin candidate state object {}", path.display()))?,
        );
        Self::from_handle(handle, path, directory)
    }

    pub(super) fn from_handle(handle: OwnedHandle, path: &Path, directory: bool) -> Result<Self> {
        let mut info = BY_HANDLE_FILE_INFORMATION::default();
        unsafe { GetFileInformationByHandle(handle.raw(), &mut info) }
            .with_context(|| format!("query candidate object attributes {}", path.display()))?;
        ensure!(
            info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT.0 == 0,
            "candidate state contains a reparse point"
        );
        ensure!(
            (info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY.0 != 0) == directory,
            "candidate state object has wrong type"
        );
        ensure!(
            directory || info.nNumberOfLinks == 1,
            "candidate state file has multiple hardlinks"
        );
        // Also refuse short-name aliases: every later SQLite name must denote
        // the same lexical path already pinned, without resolving an alias.
        let flags = GETFINALPATHNAMEBYHANDLE_FLAGS(FILE_NAME_NORMALIZED.0 | VOLUME_NAME_DOS.0);
        let required = unsafe { GetFinalPathNameByHandleW(handle.raw(), &mut [], flags) };
        ensure!(
            required > 0 && required < 32768,
            "cannot size final candidate state path"
        );
        let mut final_path = vec![0; required as usize + 1];
        let length = unsafe { GetFinalPathNameByHandleW(handle.raw(), &mut final_path, flags) };
        ensure!(
            length > 0 && (length as usize) < final_path.len(),
            "cannot read final candidate state path"
        );
        let final_path = String::from_utf16(&final_path[..length as usize])?;
        let expected = path.to_str().unwrap().trim_end_matches('\\');
        ensure!(
            crate::path_id::strip_extended_prefix(&final_path)
                .trim_end_matches('\\')
                .eq_ignore_ascii_case(expected),
            "candidate state path is an alias or changed while opening"
        );
        let id = file_id_from_handle(handle.raw())
            .with_context(|| format!("query candidate object identity {}", path.display()))?;
        Ok(Self {
            handle,
            id,
            path: path.to_owned(),
        })
    }

    pub fn size(&self) -> Result<u64> {
        let mut size = 0;
        unsafe {
            GetFileSizeEx(self.handle.raw(), &mut size)?;
        }
        ensure!(size >= 0, "candidate state file has invalid size");
        Ok(size as u64)
    }

    pub fn read_small(&self) -> Result<Vec<u8>> {
        let size = self.size()?;
        ensure!(
            (1..=16 * 1024).contains(&size),
            "candidate identity manifest has invalid size"
        );
        let mut data = vec![0; size as usize];
        let mut count = 0;
        unsafe {
            ReadFile(self.handle.raw(), Some(&mut data), Some(&mut count), None)?;
        }
        ensure!(
            count as u64 == size,
            "candidate identity manifest changed while reading"
        );
        Ok(data)
    }

    pub fn write_new(&self, data: &[u8]) -> Result<()> {
        ensure!(
            self.size()? == 0 && data.len() <= 16 * 1024,
            "manifest write requires an empty fresh object"
        );
        let mut count = 0;
        unsafe {
            WriteFile(self.handle.raw(), Some(data), Some(&mut count), None)?;
            ensure!(
                count as usize == data.len(),
                "short candidate manifest write; preserve partial state"
            );
            FlushFileBuffers(self.handle.raw())?;
        }
        Ok(())
    }
}

/// Production passes an OS KnownFolder anchor, never an environment path. All
/// these directories must already exist; no path guessed from a public parent
/// is created/adopted. The separately cfg(test)-only fixture entry point skips
/// ancestor DACL admission; it tests mechanics inside an owned disposable root,
/// not the production trust/provenance of the host's unrelated ancestors.
pub(super) fn pin_session_path(path: &Path, policy: &Policy) -> Result<Vec<Pinned>> {
    pin_path(path, policy, true)
}

pub(super) fn validate_pinned_ancestors(pins: &[Pinned], policy: &Policy) -> Result<()> {
    ensure!(!pins.is_empty(), "storage ancestor pins missing");
    for (index, pin) in pins.iter().enumerate() {
        validate_ancestor(&pin.security()?, &policy.owner, index + 1 == pins.len())?;
    }
    Ok(())
}

#[cfg(test)]
pub(super) fn pin_fixture_path(path: &Path, policy: &Policy) -> Result<Vec<Pinned>> {
    // The test owns this disposable namespace. Production cannot select this
    // seam, and it does not assert the host's unrelated ancestors are private.
    pin_path(path, policy, false)
}

fn pin_path(path: &Path, policy: &Policy, admit_ancestors: bool) -> Result<Vec<Pinned>> {
    // Validate the whole lexical chain before any relative native opens.
    extended_path(path)?;
    let text = path.to_str().unwrap();
    let anchor = &text[..3];
    let wide = wstr(anchor);
    let mut filesystem = [0u16; 32];
    unsafe {
        ensure!(
            GetDriveTypeW(pcwstr(&wide)) == 3,
            "candidate state requires a fixed local volume"
        );
        GetVolumeInformationW(pcwstr(&wide), None, None, None, None, Some(&mut filesystem))
            .context("query candidate NTFS volume")?;
    }
    ensure!(
        filesystem.starts_with(&[b'N' as u16, b'T' as u16, b'F' as u16, b'S' as u16, 0]),
        "candidate state requires NTFS"
    );
    let mut chain = vec![Pinned::open(Path::new(anchor), true, false)?];
    if admit_ancestors {
        validate_ancestor(&chain[0].security()?, &policy.owner, false)?;
    }
    let mut current = PathBuf::from(anchor);
    for part in text[3..].split('\\').filter(|_| text.len() > 3) {
        current.push(part);
        let pinned = open_child_directory(chain.last().unwrap(), part, None)?.0;
        if admit_ancestors {
            validate_ancestor(&pinned.security()?, &policy.owner, current == path).with_context(
                || format!("untrusted candidate storage ancestor {}", current.display()),
            )?;
        }
        chain.push(pinned);
    }
    Ok(chain)
}

/// Atomically create and pin one fixed component with its final descriptor.
/// An existing component is opened without following its reparse point, then
/// the caller must validate its exact policy before descending further.
pub(super) fn open_child_directory(
    parent: &Pinned,
    component: &str,
    create: Option<&Policy>,
) -> Result<(Pinned, bool)> {
    open_child_directory_inner(parent, component, create, true)
}

/// First-install roots must never adopt an existing name, including an object
/// created by this installation before its identity could be persisted.
pub(super) fn create_child_directory(
    parent: &Pinned,
    component: &str,
    policy: &Policy,
) -> Result<Pinned> {
    let (object, fresh) = open_child_directory_inner(parent, component, Some(policy), false)?;
    ensure!(
        fresh,
        "candidate storage requires a newly created directory"
    );
    Ok(object)
}

fn open_child_directory_inner(
    parent: &Pinned,
    component: &str,
    create: Option<&Policy>,
    allow_existing: bool,
) -> Result<(Pinned, bool)> {
    ensure!(
        !component.is_empty()
            && component != "."
            && component != ".."
            && !component.contains(['\\', '/', ':', '\0'])
            && !component.ends_with([' ', '.']),
        "invalid candidate directory component"
    );
    let descriptor = create
        .map(|policy| OwnedSd::from_sddl(&policy.sddl(true)))
        .transpose()?;
    let mut wide = wstr(component);
    let name = UNICODE_STRING {
        Length: ((wide.len() - 1) * 2) as u16,
        MaximumLength: (wide.len() * 2) as u16,
        Buffer: PWSTR(wide.as_mut_ptr()),
    };
    let attributes = OBJECT_ATTRIBUTES {
        Length: std::mem::size_of::<OBJECT_ATTRIBUTES>() as u32,
        RootDirectory: parent.handle.raw(),
        ObjectName: &name,
        Attributes: OBJ_CASE_INSENSITIVE,
        SecurityDescriptor: descriptor
            .as_ref()
            .map(|sd| sd.ptr.0.cast())
            .unwrap_or(std::ptr::null_mut()),
        ..Default::default()
    };
    let mut handle = HANDLE::default();
    let mut status_block = IO_STATUS_BLOCK::default();
    let call = |disposition, handle: &mut HANDLE, status_block: &mut IO_STATUS_BLOCK| unsafe {
        NtCreateFile(
            handle,
            FILE_ACCESS_RIGHTS(1 | 0x80 | READ_CONTROL | 0x100000),
            &attributes,
            status_block,
            None,
            FILE_ATTRIBUTE_DIRECTORY,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            disposition,
            FILE_DIRECTORY_FILE | FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT,
            None,
            0,
        )
    };
    let status = call(
        if create.is_some() {
            FILE_CREATE
        } else {
            FILE_OPEN
        },
        &mut handle,
        &mut status_block,
    );
    let fresh = if allow_existing && create.is_some() && status == STATUS_OBJECT_NAME_COLLISION {
        call(FILE_OPEN, &mut handle, &mut status_block)
            .ok()
            .context("open existing candidate directory")?;
        false
    } else {
        status
            .ok()
            .context("atomically open/create candidate directory")?;
        create.is_some()
    };
    let handle = OwnedHandle(handle);
    ensure!(
        status_block.Information == if fresh { 2 } else { 1 },
        "unexpected candidate directory create disposition"
    );
    Ok((
        Pinned::from_handle(handle, &parent.path.join(component), true)?,
        fresh,
    ))
}

/// Admit a trusted owner and no effective untrusted mutation grants. Read ACEs
/// are fine. Inherit-only ACEs do not affect an existing pinned ancestor; new
/// candidate directories always have a protected explicit birth descriptor.
pub(super) fn validate_ancestor(descriptor: &OwnedSd, host: &str, storage_anchor: bool) -> Result<()> {
    let trusted = |sid: &str| {
        sid == host
            || sid == "S-1-5-18"
            || sid == "S-1-5-32-544"
            || sid == "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
    };
    let mut owner = PSID::default();
    let mut defaulted = false.into();
    unsafe {
        GetSecurityDescriptorOwner(descriptor.ptr, &mut owner, &mut defaulted)?;
    }
    ensure!(
        !owner.0.is_null() && trusted(&crate::sid::psid_to_string(owner)?),
        "storage ancestor has an untrusted owner"
    );
    for bytes in ace_bytes(descriptor)? {
        let header = unsafe { std::ptr::read_unaligned(bytes.as_ptr().cast::<ACE_HEADER>()) };
        // Conservatively reject conditional/object-specific and unknown ACEs:
        // this narrow opener does not implement their effective-access rules.
        ensure!(
            matches!(header.AceType, 0 | 1),
            "unsupported storage ancestor ACE"
        );
        if header.AceType == 1 || header.AceFlags & INHERIT_ONLY_ACE.0 as u8 != 0 {
            continue;
        }
        ensure!(
            bytes.len() >= std::mem::size_of::<ACCESS_ALLOWED_ACE>(),
            "short storage ancestor ACE"
        );
        ensure!(
            bytes.len() >= 16 && bytes.len() == 16 + 4 * bytes[9] as usize,
            "invalid storage ancestor SID size"
        );
        let mask = u32::from_le_bytes(bytes[4..8].try_into().unwrap());
        let sid =
            crate::sid::psid_to_string(PSID(unsafe { bytes.as_ptr().add(8) }.cast_mut().cast()))?;
        // ADD_SUBDIRECTORY alone on a stable higher ancestor is not authority
        // to replace our existing pinned descendant. At the storage anchor it
        // could precreate our namespace, so refuse it there too. WRITE_DATA /
        // ADD_FILE is retained conservatively because reparse controls may use
        // the same directory-handle write access.
        let mutation = 0x10000000
            | 0x40000000
            | 0x000d0000
            | 0x40
            | 0x100
            | 0x10
            | 0x2
            | if storage_anchor { 0x4 } else { 0 };
        ensure!(
            trusted(&sid) || mask & mutation == 0,
            "untrusted storage ancestor mutation grant to {sid}"
        );
    }
    Ok(())
}

pub(super) fn known_folder_anchor() -> Result<PathBuf> {
    known_folder(&FOLDERID_LocalAppData)
}

/// The preview supports x64 Windows only. The explicit x64 KnownFolder avoids
/// silently switching identity between Program Files and Program Files (x86).
pub(super) fn program_files_anchor() -> Result<PathBuf> {
    ensure!(
        cfg!(target_arch = "x86_64"),
        "candidate storage requires the x64 preview"
    );
    known_folder(&FOLDERID_ProgramFilesX64)
}

fn known_folder(folder: &windows::core::GUID) -> Result<PathBuf> {
    #[link(name = "ole32")]
    unsafe extern "system" {
        fn CoTaskMemFree(pointer: *const std::ffi::c_void);
    }
    let pointer = unsafe { SHGetKnownFolderPath(folder, KF_FLAG_DEFAULT, None)? };
    let value = unsafe { pointer.to_string() };
    unsafe {
        CoTaskMemFree(pointer.0.cast());
    }
    Ok(PathBuf::from(value?))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn candidate_disk_extended_path_refuses_dos_aliases_and_counts_utf16() {
        assert_eq!(extended_path(Path::new(r"C:\")).unwrap(), r"\\?\C:\");
        assert_eq!(
            extended_path(Path::new(
                "C:\\private folder\\\u{65e5}\u{672c}\u{8a9e}\u{1f600}\\state.db"
            ))
            .unwrap(),
            "\\\\?\\C:\\private folder\\\u{65e5}\u{672c}\u{8a9e}\u{1f600}\\state.db"
        );
        for path in [
            r"C:relative",
            r"\rooted",
            r"\\host\share\state.db",
            r"\\?\C:\private\state.db",
            r"\\.\C:\private\state.db",
            r"C:\private\.\state.db",
            r"C:\private\..\state.db",
            r"C:\private\\state.db",
            r"C:\private\",
            r"C:\private\state.db:stream",
            r"C:\private/state.db",
            r"C:\private\state.db.",
            r"C:\private\state.db ",
            r"C:\private\state*.db",
            r"C:\private\state?.db",
            r"C:\private\state|db",
            "C:\\private\\state\0.db",
            "C:\\private\\state\n.db",
            r"C:\CON",
            r"C:\con.txt",
            r"C:\NUL.db",
            r"C:\PRN.db",
            r"C:\AUX.db",
            r"C:\CONIN$",
            r"C:\CONOUT$.db",
            r"C:\COM1.db",
            r"C:\COM9 .db",
            r"C:\LPT1",
            r"C:\LPT9.db",
            "C:\\COM\u{b9}.db",
            "C:\\LPT\u{b2}.db",
            "C:\\COM\u{b3}.db",
        ] {
            assert!(extended_path(Path::new(path)).is_err(), "accepted {path:?}");
        }
        for component in ["COM10", "LPT0", "console.db", "auxiliary", "NUL-device"] {
            assert!(extended_path(&Path::new(r"C:\").join(component)).is_ok());
        }
        // Supplementary characters occupy two UTF-16 units, not one scalar.
        let boundary = format!("C:\\{}{}", "\u{1f600}\\".repeat(10919), "ab");
        assert_eq!(boundary.encode_utf16().count(), 32762);
        assert_eq!(
            extended_path(Path::new(&boundary))
                .unwrap()
                .encode_utf16()
                .count()
                + 1,
            32767
        );
        assert!(extended_path(Path::new(&format!("{boundary}x"))).is_err());
    }

    #[test]
    fn candidate_disk_descriptor_never_admits_broad_or_foreign_state() {
        let policy = Policy {
            owner: "S-1-5-21-1-2-3-1001".into(),
            offline: "S-1-5-21-1-2-3-1002".into(),
            online: "S-1-5-21-1-2-3-1003".into(),
            group: "S-1-5-21-1-2-3-1004".into(),
        };
        for directory in [false, true] {
            let expected = policy.sddl(directory);
            policy
                .validate_descriptor(&OwnedSd::from_sddl(&expected).unwrap(), directory)
                .unwrap();
            for altered in [
                expected.replace("D:P", "D:"),
                format!("{expected}(A;;FA;;;BU)"),
                expected.replacen(&policy.owner, "S-1-5-21-1-2-3-9999", 1),
                expected.replace(
                    &format!(
                        "(D;{};FA;;;{})",
                        if directory { "OICI" } else { "" },
                        policy.online
                    ),
                    "",
                ),
            ] {
                assert!(
                    policy
                        .validate_descriptor(&OwnedSd::from_sddl(&altered).unwrap(), directory)
                        .is_err()
                );
            }
        }
    }

    #[test]
    fn candidate_disk_ancestor_admission_rejects_mutation_but_allows_readers() {
        let host = "S-1-5-21-1-2-3-1001";
        let sd = |mask: u32, flags: &str| {
            OwnedSd::from_sddl(&format!(
                "O:{host}D:P(A;;FA;;;{host})(A;{flags};0x{mask:08x};;;BU)"
            ))
            .unwrap()
        };
        for mask in [
            0x40, 0x10000, 0x40000, 0x80000, 0x100, 0x10, 0x2, 0x10000000, 0x40000000,
        ] {
            assert!(
                validate_ancestor(&sd(mask, ""), host, false).is_err(),
                "accepted mutation mask {mask:x}"
            );
            assert!(validate_ancestor(&sd(mask, "OICIIO"), host, false).is_ok());
        }
        assert!(validate_ancestor(&sd(0x1200a9, ""), host, true).is_ok());
        assert!(validate_ancestor(&sd(4, ""), host, false).is_ok());
        assert!(validate_ancestor(&sd(4, ""), host, true).is_err());
        assert!(
            validate_ancestor(
                &OwnedSd::from_sddl(&format!("O:BUD:P(A;;FA;;;{host})")).unwrap(),
                host,
                true
            )
            .is_err()
        );
        assert!(
            validate_ancestor(
                &OwnedSd::from_sddl(&format!("O:{host}D:NO_ACCESS_CONTROL")).unwrap(),
                host,
                true
            )
            .is_err()
        );
        assert!(
            validate_ancestor(
                &OwnedSd::from_sddl(&format!("O:{host}D:P(A;;FA;;;OW)")).unwrap(),
                host,
                true
            )
            .is_err()
        );
    }

    #[test]
    #[ignore = "explicit normal-user read-only storage compatibility probe; no state creation"]
    fn candidate_disk_known_folder_admission_read_only() {
        let path = known_folder_anchor().unwrap();
        let host = crate::sid::current_user_sid().unwrap();
        let policy = Policy {
            owner: host,
            offline: "S-1-5-21-1-2-3-1002".into(),
            online: "S-1-5-21-1-2-3-1003".into(),
            group: "S-1-5-21-1-2-3-1004".into(),
        };
        match pin_session_path(&path, &policy) {
            Ok(_) => println!("KNOWN_FOLDER_ADMITTED_READ_ONLY: {}", path.display()),
            Err(error) => println!("KNOWN_FOLDER_REFUSED_BEFORE_WRITES: {error:#}"),
        }
    }
}
