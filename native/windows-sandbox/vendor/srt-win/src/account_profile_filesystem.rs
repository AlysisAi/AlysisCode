//! Original-object observations for a future composite profile lifecycle.
//! No creation, deletion, journal receipt, pathname recovery or cleanup authority.
//! Calls are limited to the verified actor's SDK-selected execution package.
#![allow(dead_code)] // Account activation deliberately remains disabled.

use anyhow::{Context, Result, ensure};
use std::mem::size_of;
use windows::Wdk::Foundation::OBJECT_ATTRIBUTES;
use windows::Wdk::Storage::FileSystem::{
    FILE_OPEN, FILE_OPEN_REPARSE_POINT, FILE_SYNCHRONOUS_IO_NONALERT, FileEaInformation,
    FileNamesInformation, FileStreamInformation, NtCreateFile, NtQueryDirectoryFileEx,
    NtQueryInformationFile, NtQuerySecurityObject,
};
use windows::Win32::Foundation::*;
use windows::Win32::Security::*;
use windows::Win32::Storage::FileSystem::*;
use windows::Win32::System::IO::IO_STATUS_BLOCK;
use windows::core::{PCWSTR, PWSTR};

const MAX_NODES: usize = 256;
const MAX_DEPTH: usize = 16;
const MAX_NAME: usize = 255;
const MAX_QUERY: usize = 65536;
const MAX_BYTES: usize = 4 * 1024 * 1024;
const READ_CONTROL: u32 = 0x20000;
const SYNCHRONIZE: u32 = 0x100000;

// Aligned native output. Every parser uses only the checked returned extent.
struct Buffer(Vec<usize>);
impl Buffer {
    fn new() -> Self {
        Self(vec![0; MAX_QUERY / size_of::<usize>()])
    }
    fn pointer(&mut self) -> *mut std::ffi::c_void {
        self.0.as_mut_ptr().cast()
    }
    fn bytes(&self, length: usize) -> Result<&[u8]> {
        ensure!(
            length <= MAX_QUERY,
            "filesystem query exceeded its allocation"
        );
        Ok(unsafe { std::slice::from_raw_parts(self.0.as_ptr().cast(), length) })
    }
}

// No Clone/Deserialize/raw getter. Failed explicit close is never retried.
#[derive(Clone, Copy)]
enum FinalNameForm {
    Dos,
    Nt,
}
fn original_final_name(handle: HANDLE, form: FinalNameForm) -> Result<Vec<u16>> {
    let mut name = vec![0u16; 32768];
    let flags = GETFINALPATHNAMEBYHANDLE_FLAGS(
        FILE_NAME_NORMALIZED.0
            | match form {
                FinalNameForm::Dos => VOLUME_NAME_DOS.0,
                FinalNameForm::Nt => VOLUME_NAME_NT.0,
            },
    );
    let length = unsafe { GetFinalPathNameByHandleW(handle, &mut name, flags) } as usize;
    if length == 0 {
        return Err(windows::core::Error::from_thread())
            .context("query original profile final name");
    }
    ensure!(length < name.len(), "profile final name query overflowed");
    name.truncate(length);
    String::from_utf16(&name).context("invalid profile final name")?;
    Ok(name)
}
struct OriginalFile(Option<HANDLE>, FinalNameForm);
impl OriginalFile {
    fn raw(&self) -> Result<HANDLE> {
        self.0.context("original file handle consumed")
    }
    fn adopt(raw: HANDLE) -> Result<Self> {
        Self::adopt_with_form(raw, FinalNameForm::Dos)
    }
    fn adopt_with_form(raw: HANDLE, form: FinalNameForm) -> Result<Self> {
        ensure!(!raw.is_invalid(), "invalid profile filesystem handle");
        let value = Self(Some(raw), form);
        crate::account_namespace::verify_noninheritable(raw)?;
        value.metadata()?;
        Ok(value)
    }
    fn child(&self, name: &[u16]) -> Result<Self> {
        self.child_or_absent(name)?
            .context("original profile child is absent")
    }
    fn child_or_absent(&self, name: &[u16]) -> Result<Option<Self>> {
        component(name)?;
        let mut wide = name.to_vec();
        let name = UNICODE_STRING {
            Length: (wide.len() * 2) as u16,
            MaximumLength: (wide.len() * 2) as u16,
            Buffer: PWSTR(wide.as_mut_ptr()),
        };
        let attributes = OBJECT_ATTRIBUTES {
            Length: size_of::<OBJECT_ATTRIBUTES>() as u32,
            RootDirectory: self.raw()?,
            ObjectName: &name,
            Attributes: OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE,
            ..Default::default()
        };
        let mut raw = HANDLE::default();
        let mut io = IO_STATUS_BLOCK::default();
        let status = unsafe {
            NtCreateFile(
                &mut raw,
                FILE_ACCESS_RIGHTS(1 | 8 | 0x80 | READ_CONTROL | SYNCHRONIZE),
                &attributes,
                &mut io,
                None,
                FILE_FLAGS_AND_ATTRIBUTES(0),
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                FILE_OPEN,
                FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT,
                None,
                0,
            )
        };
        if exact_child_absence(status) {
            ensure!(raw.is_invalid(), "absent profile child returned a handle");
            return Ok(None);
        }
        status
            .ok()
            .context("open original profile child relative to retained parent")?;
        let child = Self::adopt_with_form(raw, self.1)?;
        let mut expected = self.metadata()?.name;
        if expected.last() != Some(&92) {
            expected.push(92);
        }
        expected.extend_from_slice(&wide);
        ensure!(
            child.metadata()?.name == expected,
            "profile child escaped original parent or used an alias"
        );
        Ok(Some(child))
    }
    fn standard(&self) -> Result<FILE_STANDARD_INFO> {
        let mut value = FILE_STANDARD_INFO::default();
        unsafe {
            GetFileInformationByHandleEx(
                self.raw()?,
                FileStandardInfo,
                (&mut value as *mut FILE_STANDARD_INFO).cast(),
                size_of::<FILE_STANDARD_INFO>() as u32,
            )
        }?;
        Ok(value)
    }
    fn metadata(&self) -> Result<Metadata> {
        let h = self.raw()?;
        let mut basic = FILE_BASIC_INFO::default();
        unsafe {
            GetFileInformationByHandleEx(
                h,
                FileBasicInfo,
                (&mut basic as *mut FILE_BASIC_INFO).cast(),
                size_of::<FILE_BASIC_INFO>() as u32,
            )
        }?;
        ensure!(
            basic.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT.0 == 0,
            "profile reparse point refused"
        );
        let standard = self.standard()?;
        ensure!(
            !standard.DeletePending && standard.NumberOfLinks > 0,
            "original profile file is being deleted"
        );
        ensure!(
            standard.Directory || standard.NumberOfLinks == 1,
            "profile hardlinked file refused"
        );
        let id = crate::path_id::file_id_from_handle(h)?.as_bytes();
        ensure!(id != [0; 24], "empty profile file identity");
        let name = original_final_name(h, self.1)?;
        let mut security = Buffer::new();
        let mut returned = 0;
        unsafe {
            NtQuerySecurityObject(
                h,
                (OWNER_SECURITY_INFORMATION
                    | GROUP_SECURITY_INFORMATION
                    | DACL_SECURITY_INFORMATION)
                    .0,
                Some(PSECURITY_DESCRIPTOR(security.pointer())),
                MAX_QUERY as u32,
                &mut returned,
            )
        }
        .ok()
        .context("query original profile file security")?;
        let security = security.bytes(returned as usize)?;
        ensure!(
            security.len() >= size_of::<SECURITY_DESCRIPTOR_RELATIVE>(),
            "truncated profile file security"
        );
        Ok(Metadata {
            id,
            name,
            security: security.to_vec(),
            directory: standard.Directory,
            attributes: basic.FileAttributes,
        })
    }
    fn query(
        &self,
        class: windows::Wdk::Storage::FileSystem::FILE_INFORMATION_CLASS,
    ) -> Result<Vec<u8>> {
        let mut output = Buffer::new();
        let mut io = IO_STATUS_BLOCK::default();
        unsafe {
            NtQueryInformationFile(
                self.raw()?,
                &mut io,
                output.pointer(),
                MAX_QUERY as u32,
                class,
            )
        }
        .ok()
        .context("query bounded profile filesystem metadata")?;
        Ok(output.bytes(io.Information)?.to_vec())
    }
    fn children(&self) -> Result<Vec<Vec<u16>>> {
        let mut names = Vec::new();
        // At most two dot entries and MAX_NODES real entries; one final call
        // must prove completion. Overflow and partial output are never success.
        for index in 0..=MAX_NODES + 2 {
            let mut output = Buffer::new();
            let mut io = IO_STATUS_BLOCK::default();
            let status = unsafe {
                NtQueryDirectoryFileEx(
                    self.raw()?,
                    None,
                    None,
                    None,
                    &mut io,
                    output.pointer(),
                    MAX_QUERY as u32,
                    FileNamesInformation,
                    2 | if index == 0 { 1 } else { 0 },
                    None,
                )
            };
            if status == NTSTATUS(0x80000006u32 as i32) {
                names.sort();
                return Ok(names);
            }
            status
                .ok()
                .context("enumerate original profile directory")?;
            let name = directory_entry(output.bytes(io.Information)?)?;
            if name == [46] || name == [46, 46] {
                continue;
            }
            component(&name)?;
            ensure!(
                names.len() < MAX_NODES && !names.contains(&name),
                "profile directory count/duplicate refused"
            );
            names.push(name);
        }
        anyhow::bail!("profile directory inventory did not terminate within bounds")
    }
    fn snapshot(&self) -> Result<Snapshot> {
        let metadata = self.metadata()?;
        let ea = self.query(FileEaInformation)?;
        ensure!(
            ea.len() == 4 && u32::from_le_bytes(ea[..4].try_into()?) == 0,
            "profile extended attributes refused"
        );
        let standard = self.standard()?;
        streams(
            &self.query(FileStreamInformation)?,
            metadata.directory,
            standard.EndOfFile,
        )?;
        let (children, data) = if metadata.directory {
            (self.children()?, Vec::new())
        } else {
            ensure!(
                (0..=MAX_QUERY as i64).contains(&standard.EndOfFile),
                "profile file data bound exceeded"
            );
            let mut data = vec![0; standard.EndOfFile as usize];
            let mut read = 0;
            unsafe {
                SetFilePointerEx(self.raw()?, 0, None, FILE_BEGIN)?;
                if !data.is_empty() {
                    ReadFile(self.raw()?, Some(&mut data), Some(&mut read), None)?;
                }
            }
            ensure!(
                read as usize == data.len() && self.standard()?.EndOfFile == standard.EndOfFile,
                "profile file changed during bounded read"
            );
            (Vec::new(), data)
        };
        ensure!(
            self.metadata()? == metadata,
            "profile metadata drift during observation"
        );
        Ok(Snapshot {
            metadata,
            children,
            data,
        })
    }
    fn close(mut self) -> Result<()> {
        close_once(&mut self.0, |h| {
            unsafe { CloseHandle(h) }.map_err(Into::into)
        })
    }
}
impl Drop for OriginalFile {
    fn drop(&mut self) {
        if close_once(&mut self.0, |h| {
            unsafe { CloseHandle(h) }.map_err(Into::into)
        })
        .is_err()
        {
            std::process::abort();
        }
    }
}
fn close_once(slot: &mut Option<HANDLE>, close: impl FnOnce(HANDLE) -> Result<()>) -> Result<()> {
    if let Some(h) = slot.take() {
        close(h)?;
    }
    Ok(())
}

#[derive(PartialEq, Eq)]
struct Metadata {
    id: [u8; 24],
    name: Vec<u16>,
    security: Vec<u8>,
    directory: bool,
    attributes: u32,
}
#[derive(PartialEq, Eq)]
struct Snapshot {
    metadata: Metadata,
    children: Vec<Vec<u16>>,
    data: Vec<u8>,
}
struct Node {
    file: OriginalFile,
    initial: Snapshot,
}
struct Anchor {
    file: OriginalFile,
    initial: Metadata,
}

pub(crate) enum FilesystemObservation {
    Present,
    OriginalObjectsDeletePending,
}
pub(crate) struct RetainedProfileFilesystem {
    nodes: Vec<Node>,
    ancestors: Vec<Anchor>,
}
#[path = "account_profile_filesystem_post_use.rs"]
mod post_use;
pub(crate) use post_use::ProfileFilesystemRoot;
/// Original ancestors survive checked closure of every package handle. This is
/// a point-in-time absence observation, not exclusive birth or cleanup authority.
/// It cannot be reconstructed from paths, serialized or used to delete anything.
pub(crate) struct RetainedProfileParent {
    ancestors: Vec<Anchor>,
    package_name: Vec<u16>,
}
impl RetainedProfileParent {
    fn unchanged(&self) -> Result<()> {
        ensure!(!self.ancestors.is_empty(), "profile parent inventory empty");
        for anchor in &self.ancestors {
            ensure!(
                anchor.file.metadata()? == anchor.initial,
                "profile ancestor drifted after tree close"
            );
        }
        Ok(())
    }
    pub(crate) fn observe_absent(&self) -> Result<()> {
        crate::account_impersonation::require_process_identity()?;
        self.unchanged()?;
        let parent = &self
            .ancestors
            .last()
            .context("profile parent missing")?
            .file;
        if let Some(present) = parent.child_or_absent(&self.package_name)? {
            present.close()?;
            anyhow::bail!("profile package name is still present or replaced");
        }
        // No process/SDK effect is performed here. Recheck the retained
        // ancestors after the relative probe; observations are not atomic.
        self.unchanged()
    }
    pub(crate) fn close(self) -> Result<()> {
        for anchor in self.ancestors.into_iter().rev() {
            anchor.file.close()?;
        }
        Ok(())
    }
}
fn exact_child_absence(status: NTSTATUS) -> bool {
    // PATH_NOT_FOUND, ACCESS_DENIED, DELETE_PENDING, reparse and query errors
    // are uncertainty. Only a missing final component under the original
    // retained parent is an admitted absence observation.
    status == NTSTATUS(0xc0000034u32 as i32)
}
impl RetainedProfileFilesystem {
    pub(crate) fn original_root_id(&self) -> Result<[u8; 24]> {
        Ok(self
            .nodes
            .first()
            .context("empty profile inventory")?
            .initial
            .metadata
            .id)
    }
    pub(crate) fn open(container: &str, actor: &str) -> Result<Self> {
        Self::capture_path(&package_path(container, actor)?)
    }
    #[cfg(test)]
    pub(crate) fn fixture_ac(container: &str, actor: &str) -> Result<std::path::PathBuf> {
        Ok(std::path::PathBuf::from(package_path(container, actor)?).join("AC"))
    }
    #[cfg(test)]
    pub(crate) fn distinct_root(&self, other: &Self) -> bool {
        self.nodes[0].initial.metadata.id != other.nodes[0].initial.metadata.id
    }
    fn capture_path(path: &str) -> Result<Self> {
        let components = local_components(path)?;
        let drive = crate::util::wstr(&path[..3]);
        ensure!(
            unsafe { GetDriveTypeW(PCWSTR(drive.as_ptr())) } == 3,
            "profile requires fixed local NTFS"
        );
        let mut fs = [0u16; 32];
        unsafe {
            GetVolumeInformationW(
                PCWSTR(drive.as_ptr()),
                None,
                None,
                None,
                None,
                Some(&mut fs),
            )
        }?;
        ensure!(
            fs.starts_with(&[78, 84, 70, 83, 0]),
            "profile requires NTFS"
        );
        let raw = unsafe {
            CreateFileW(
                PCWSTR(drive.as_ptr()),
                1 | 8 | 0x80 | READ_CONTROL,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
        }?;
        let mut file = OriginalFile::adopt(raw)?;
        let mut ancestors = Vec::new();
        for part in components {
            let initial = file.metadata()?;
            ensure!(initial.directory, "profile ancestor is not a directory");
            let child = file.child(&part)?;
            ancestors.push(Anchor { file, initial });
            file = child;
        }
        ensure!(
            file.metadata()?.directory,
            "profile package is not a directory"
        );
        let mut result = Self {
            nodes: Vec::new(),
            ancestors,
        };
        result.append(file, 0, &mut 0)?;
        ensure!(
            matches!(result.observe()?, FilesystemObservation::Present),
            "profile disappeared during inventory"
        );
        Ok(result)
    }
    fn append(&mut self, file: OriginalFile, depth: usize, bytes: &mut usize) -> Result<()> {
        ensure!(
            self.nodes.len() < MAX_NODES && depth <= MAX_DEPTH,
            "profile filesystem node/depth bound exceeded"
        );
        let initial = file.snapshot()?;
        ensure!(
            self.nodes
                .iter()
                .all(|n| n.initial.metadata.id != initial.metadata.id),
            "profile repeated file identity refused"
        );
        charge(bytes, &initial)?;
        let index = self.nodes.len();
        self.nodes.push(Node { file, initial });
        for i in 0..self.nodes[index].initial.children.len() {
            let child = self.nodes[index]
                .file
                .child(&self.nodes[index].initial.children[i])?;
            self.append(child, depth + 1, bytes)?;
        }
        Ok(())
    }
    pub(crate) fn observe(&self) -> Result<FilesystemObservation> {
        crate::account_impersonation::require_process_identity()?;
        for anchor in &self.ancestors {
            ensure!(
                anchor.file.metadata()? == anchor.initial,
                "profile ancestor drifted"
            );
        }
        let mut deleted = 0;
        let mut bytes = 0;
        for node in &self.nodes {
            ensure!(
                crate::path_id::file_id_from_handle(node.file.raw()?)?.as_bytes()
                    == node.initial.metadata.id,
                "original profile identity changed"
            );
            let standard = node.file.standard()?;
            if standard.DeletePending && standard.NumberOfLinks == 0 {
                deleted += 1;
                continue;
            }
            let now = node.file.snapshot()?;
            charge(&mut bytes, &now)?;
            ensure!(
                now == node.initial,
                "original profile filesystem inventory drifted"
            );
        }
        ensure!(
            !self.nodes.is_empty() && (deleted == 0 || deleted == self.nodes.len()),
            "partial profile filesystem deletion refused"
        );
        Ok(if deleted == 0 {
            FilesystemObservation::Present
        } else {
            FilesystemObservation::OriginalObjectsDeletePending
        })
    }
    pub(crate) fn close(self) -> Result<()> {
        for node in self.nodes.into_iter().rev() {
            node.file.close()?;
        }
        for anchor in self.ancestors.into_iter().rev() {
            anchor.file.close()?;
        }
        Ok(())
    }
    pub(crate) fn close_tree_for_observation(self) -> Result<RetainedProfileParent> {
        ensure!(
            matches!(self.observe()?, FilesystemObservation::Present),
            "cannot transfer a deleted profile tree to parent observation"
        );
        let root = self
            .nodes
            .first()
            .context("profile package inventory empty")?;
        let package_name = root
            .initial
            .metadata
            .name
            .rsplit(|c| *c == 92)
            .next()
            .context("profile package name missing")?
            .to_vec();
        component(&package_name)?;
        ensure!(!self.ancestors.is_empty(), "profile parent missing");
        // No guard is returned until every package handle has closed exactly
        // once. A failed close prevents subsequent SDK deletion by the caller.
        for node in self.nodes.into_iter().rev() {
            node.file.close()?;
        }
        let parent = RetainedProfileParent {
            ancestors: self.ancestors,
            package_name,
        };
        parent.unchanged()?;
        Ok(parent)
    }
}

fn package_path(container: &str, actor: &str) -> Result<String> {
    crate::account_token::verify_raw_profile_actor(actor)?;
    crate::job::execution_name(container)?;
    let sid = crate::util::wstr(&crate::appcontainer::derive(container)?.string()?);
    let folder = task_string(unsafe {
        windows::Win32::Security::Isolation::GetAppContainerFolderPath(PCWSTR(sid.as_ptr()))
    }?)?;
    use windows::Win32::UI::Shell::{FOLDERID_LocalAppData, KF_FLAG_DEFAULT, SHGetKnownFolderPath};
    let local = task_string(unsafe {
        SHGetKnownFolderPath(&FOLDERID_LocalAppData, KF_FLAG_DEFAULT, None)
    }?)?;
    let package = format!("{local}\\Packages\\{container}");
    ensure!(
        folder.eq_ignore_ascii_case(&format!("{package}\\AC")),
        "SDK profile folder is outside actor's expected package"
    );
    // Keep the SDK's actual spelling after validating its complete location.
    // The SDK can normalize the container name's case; descendant identity
    // checks must use that spelling rather than reconstruct a different one.
    Ok(folder[..folder.len() - 3].to_owned())
}

fn component(name: &[u16]) -> Result<()> {
    let s = String::from_utf16(name)?;
    ensure!(
        !s.is_empty()
            && name.len() <= MAX_NAME
            && !matches!(s.as_str(), "." | "..")
            && !s.ends_with(['.', ' '])
            && !s
                .chars()
                .any(|c| c.is_control() || "/\\:*?\"<>|".contains(c)),
        "invalid profile filesystem component"
    );
    Ok(())
}
fn local_components(path: &str) -> Result<Vec<Vec<u16>>> {
    ensure!(
        path.len() > 3
            && path.as_bytes()[0].is_ascii_alphabetic()
            && &path.as_bytes()[1..3] == b":\\"
            && path.encode_utf16().count() < 32760,
        "profile requires bounded lexical local drive path"
    );
    let parts = path[3..]
        .split('\\')
        .map(|p| p.encode_utf16().collect::<Vec<_>>())
        .collect::<Vec<_>>();
    ensure!(parts.len() <= 64, "profile ancestor depth exceeded");
    for part in &parts {
        component(part)?;
    }
    Ok(parts)
}
fn directory_entry(bytes: &[u8]) -> Result<Vec<u16>> {
    ensure!(bytes.len() >= 12, "truncated profile directory entry");
    ensure!(
        u32::from_le_bytes(bytes[0..4].try_into()?) == 0,
        "unexpected chained single directory entry"
    );
    let length = u32::from_le_bytes(bytes[8..12].try_into()?) as usize;
    ensure!(
        length > 0 && length <= MAX_NAME * 2 && length % 2 == 0 && length <= bytes.len() - 12,
        "invalid directory entry extent"
    );
    Ok(bytes[12..12 + length]
        .chunks_exact(2)
        .map(|b| u16::from_le_bytes([b[0], b[1]]))
        .collect())
}
fn streams(bytes: &[u8], directory: bool, size: i64) -> Result<()> {
    if bytes.is_empty() {
        ensure!(directory, "missing unnamed file stream");
        return Ok(());
    }
    ensure!(
        !directory && bytes.len() >= 24,
        "profile directory alternate stream refused"
    );
    let next = u32::from_le_bytes(bytes[..4].try_into()?);
    let length = u32::from_le_bytes(bytes[4..8].try_into()?) as usize;
    ensure!(
        next == 0 && length == 14 && bytes.len() >= 24 + length,
        "profile alternate streams refused"
    );
    let expected = "::$DATA"
        .encode_utf16()
        .flat_map(u16::to_le_bytes)
        .collect::<Vec<_>>();
    ensure!(
        bytes[24..24 + length] == expected && i64::from_le_bytes(bytes[8..16].try_into()?) == size,
        "profile stream name/size differs"
    );
    Ok(())
}
fn charge(bytes: &mut usize, s: &Snapshot) -> Result<()> {
    let amount = s.metadata.name.len() * 2
        + s.metadata.security.len()
        + s.data.len()
        + s.children.iter().map(|n| n.len() * 2).sum::<usize>();
    *bytes = bytes
        .checked_add(amount)
        .context("profile observation size overflow")?;
    ensure!(
        *bytes <= MAX_BYTES,
        "profile observation byte limit exceeded"
    );
    Ok(())
}
fn task_string(pointer: PWSTR) -> Result<String> {
    #[link(name = "ole32")]
    unsafe extern "system" {
        fn CoTaskMemFree(pointer: *const std::ffi::c_void);
    }
    struct Allocation(PWSTR);
    impl Drop for Allocation {
        fn drop(&mut self) {
            unsafe {
                CoTaskMemFree(self.0.0.cast());
            }
        }
    }
    ensure!(!pointer.is_null(), "empty SDK profile path");
    let owned = Allocation(pointer);
    // Strings originate in the OS API, not untrusted native buffers. The SDK
    // guarantees NUL termination and ownership of the complete allocation.
    let text = unsafe { owned.0.to_string() }?;
    ensure!(
        text.encode_utf16().count() < 32760,
        "SDK profile path bound exceeded"
    );
    Ok(text)
}

#[cfg(test)]
#[path = "account_profile_filesystem_tests.rs"]
mod tests;

#[cfg(test)]
#[path = "account_profile_filesystem_fixed_writer.rs"]
mod fixed_writer;
#[cfg(test)]
pub(crate) use fixed_writer::{FixedProfileFileReceipt, write_fixed_profile_file};

#[cfg(test)]
#[path = "account_profile_filesystem_lowbox_fixture.rs"]
mod lowbox_fixture;
#[cfg(test)]
pub(crate) use lowbox_fixture::{
    FixedLowboxFilesystem, FixedLowboxFilesystemProof, FixedLowboxFilesystemSubject,
    observe_fixed_lowbox_filesystem,
};
