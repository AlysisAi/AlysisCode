//! Placeholder birth through retained local directory handles. No rollback by
//! pathname and no reparse traversal. Production callers retain the returned
//! guard through ACL admission/application; descendant recovery still requires
//! its own authority after these handles close.

use super::{FileId, Placeholder, file_id_from_handle, final_path_from_handle};
use crate::util::{OwnedHandle, pcwstr, wstr};
use anyhow::{Context, Result, ensure};
use windows::Wdk::{
    Foundation::OBJECT_ATTRIBUTES,
    Storage::FileSystem::{
        FILE_CREATE, FILE_DIRECTORY_FILE, FILE_NON_DIRECTORY_FILE, FILE_OPEN,
        FILE_OPEN_REPARSE_POINT, FILE_SYNCHRONOUS_IO_NONALERT, NtCreateFile,
    },
};
use windows::Win32::Globalization::{CSTR_EQUAL, CompareStringOrdinal};
use windows::Win32::{
    Foundation::{
        HANDLE, OBJ_CASE_INSENSITIVE, STATUS_OBJECT_NAME_COLLISION, STATUS_OBJECT_NAME_NOT_FOUND,
        UNICODE_STRING,
    },
    Storage::FileSystem::{
        BY_HANDLE_FILE_INFORMATION, CreateFileW, FILE_ACCESS_RIGHTS, FILE_ATTRIBUTE_DIRECTORY,
        FILE_ATTRIBUTE_NORMAL, FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS,
        FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_READ, FILE_SHARE_WRITE, FILE_STANDARD_INFO,
        FileStandardInfo, GetFileInformationByHandle, GetFileInformationByHandleEx, OPEN_EXISTING,
    },
    System::IO::IO_STATUS_BLOCK,
};
use windows::core::PWSTR;

fn components(path: &str) -> Result<(String, Vec<String>)> {
    let normalized = path.replace('/', "\\");
    let normalized = normalized.strip_prefix(r"\\?\").unwrap_or(&normalized);
    let text = normalized.trim_end_matches('\\');
    ensure!(
        !text.is_empty() && !text.starts_with('\\'),
        "placeholder needs a local drive path"
    );
    let absolute = if text.as_bytes().get(1) == Some(&b':') {
        text.to_owned()
    } else {
        std::env::current_dir()?
            .join(text)
            .to_str()
            .context("placeholder cwd is not Unicode")?
            .to_owned()
    };
    ensure!(
        absolute.len() > 3
            && absolute.as_bytes()[0].is_ascii_alphabetic()
            && &absolute.as_bytes()[1..3] == b":\\",
        "placeholder needs an absolute local path with a leaf"
    );
    ensure!(
        absolute.encode_utf16().count() + 5 <= 32767,
        "placeholder path exceeds UTF-16 bound"
    );
    let parts: Vec<_> = absolute[3..].split('\\').map(str::to_owned).collect();
    for part in &parts {
        ensure!(
            !part.is_empty()
                && part != "."
                && part != ".."
                && !part.ends_with([' ', '.'])
                && part.encode_utf16().count() <= 255
                && !part
                    .chars()
                    .any(|c| c.is_control() || "/:*?\"<>|".contains(c)),
            "placeholder contains an ambiguous path component"
        );
        let base = part
            .split('.')
            .next()
            .unwrap()
            .trim_end_matches(' ')
            .to_ascii_uppercase();
        let numbered = |prefix: &str| {
            base.strip_prefix(prefix).is_some_and(|n| {
                matches!(
                    n,
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
            ) && !numbered("COM")
                && !numbered("LPT"),
            "placeholder contains a DOS device alias"
        );
    }
    Ok((format!(r"\\?\{}", &absolute[..3]), parts))
}

fn canonical(handle: HANDLE) -> Result<String> {
    String::from_utf16(&final_path_from_handle(handle)?)
        .context("placeholder final path is not Unicode")
}

struct Pin {
    handle: OwnedHandle,
    canon: String,
    id: FileId,
    directory: bool,
}

fn attributes(handle: HANDLE) -> Result<bool> {
    let mut info = BY_HANDLE_FILE_INFORMATION::default();
    unsafe {
        GetFileInformationByHandle(handle, &mut info)?;
    }
    ensure!(
        info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT.0 == 0,
        "placeholder contains a reparse point"
    );
    let directory = info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY.0 != 0;
    ensure!(
        directory || info.nNumberOfLinks == 1,
        "placeholder file has multiple hardlinks"
    );
    let mut standard = FILE_STANDARD_INFO::default();
    unsafe {
        GetFileInformationByHandleEx(
            handle,
            FileStandardInfo,
            (&mut standard as *mut FILE_STANDARD_INFO).cast(),
            std::mem::size_of::<FILE_STANDARD_INFO>() as u32,
        )?;
    }
    ensure!(
        !standard.DeletePending,
        "placeholder object is delete-pending"
    );
    Ok(directory)
}

impl Pin {
    fn from_handle(handle: OwnedHandle, expected: &str, require_directory: bool) -> Result<Self> {
        let directory = attributes(handle.raw())?;
        ensure!(
            !require_directory || directory,
            "placeholder intermediate is not a directory"
        );
        let canon = canonical(handle.raw())?;
        // Use Windows ordinal uppercase-table comparison, not ASCII folding
        // or linguistic normalization. The handle and actual canonical spelling
        // remain authoritative; a comparison API failure is never equality.
        let actual_wide: Vec<u16> = canon.encode_utf16().collect();
        let expected_wide: Vec<u16> = expected.encode_utf16().collect();
        ensure!(
            unsafe { CompareStringOrdinal(&actual_wide, &expected_wide, true) } == CSTR_EQUAL,
            "placeholder final path differs from its pinned parent/name"
        );
        let id = file_id_from_handle(handle.raw())?;
        Ok(Self {
            handle,
            canon,
            id,
            directory,
        })
    }
    fn verify(&self) -> Result<()> {
        ensure!(
            attributes(self.handle.raw())? == self.directory
                && file_id_from_handle(self.handle.raw())? == self.id
                && canonical(self.handle.raw())? == self.canon,
            "placeholder pinned identity changed"
        );
        Ok(())
    }
}

// Relative single-component opens never re-resolve a parent pathname. The final
// component is opened as the reparse point itself and refused before descent.
// Real data/list access plus no write/delete sharing blocks rename/delete and
// generic writer opens. Metadata-only handles can still set reparse data on an
// empty directory: before/after checks and relative opens must refuse that state;
// the sharing pin alone is not exclusion against all in-place mutation.
fn child(parent: &Pin, part: &str, create: Option<bool>, directory: bool) -> Result<Option<Pin>> {
    child_with_sharing(parent, part, create, directory, false)
}

fn child_with_sharing(
    parent: &Pin,
    part: &str,
    create: Option<bool>,
    directory: bool,
    writable_ancestor: bool,
) -> Result<Option<Pin>> {
    ensure!(
        !writable_ancestor || (directory && create != Some(false)),
        "write sharing requires an intermediate directory"
    );
    let mut wide = wstr(part);
    let name = UNICODE_STRING {
        Length: ((wide.len() - 1) * 2) as u16,
        MaximumLength: (wide.len() * 2) as u16,
        Buffer: PWSTR(wide.as_mut_ptr()),
    };
    let object = OBJECT_ATTRIBUTES {
        Length: std::mem::size_of::<OBJECT_ATTRIBUTES>() as u32,
        RootDirectory: parent.handle.raw(),
        ObjectName: &name,
        Attributes: OBJ_CASE_INSENSITIVE,
        ..Default::default()
    };
    let mut raw = HANDLE::default();
    let mut io = IO_STATUS_BLOCK::default();
    let kind = match create {
        Some(true) => FILE_DIRECTORY_FILE,
        Some(false) => FILE_NON_DIRECTORY_FILE,
        None if directory => FILE_DIRECTORY_FILE,
        None => Default::default(),
    };
    let status = unsafe {
        NtCreateFile(
            &mut raw,
            FILE_ACCESS_RIGHTS(1 | 0x80 | 0x100000),
            &object,
            &mut io,
            None,
            if create == Some(true) {
                FILE_ATTRIBUTE_DIRECTORY
            } else {
                FILE_ATTRIBUTE_NORMAL
            },
            if writable_ancestor {
                FILE_SHARE_READ | FILE_SHARE_WRITE
            } else {
                FILE_SHARE_READ
            },
            if create.is_some() {
                FILE_CREATE
            } else {
                FILE_OPEN
            },
            kind | FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT,
            None,
            0,
        )
    };
    if (create.is_none() && status == STATUS_OBJECT_NAME_NOT_FOUND)
        || (create.is_some() && status == STATUS_OBJECT_NAME_COLLISION)
    {
        return Ok(None);
    }
    status.ok().with_context(|| {
        format!(
            "{} placeholder component {part:?}",
            if create.is_some() { "create" } else { "open" }
        )
    })?;
    let handle = OwnedHandle(raw);
    ensure!(
        status.0 == 0 && io.Information == if create.is_some() { 2 } else { 1 },
        "unexpected placeholder birth/open result"
    );
    let expected = format!("{}\\{part}", parent.canon.trim_end_matches('\\'));
    Ok(Some(Pin::from_handle(
        handle,
        &expected,
        directory || create == Some(true),
    )?))
}

/// Ownership carried from missing-target creation through ACL application.
/// No raw handle escape, Clone or pathname-based reconstruction is provided.
pub struct RetainedPlaceholderChain {
    canon: String,
    created: Vec<Placeholder>,
    pins: Vec<Pin>,
}

impl RetainedPlaceholderChain {
    pub fn canonical(&self) -> &str {
        &self.canon
    }
    pub(crate) fn created(&self) -> &[Placeholder] {
        &self.created
    }
    pub fn verify(&self) -> Result<()> {
        for pin in &self.pins {
            pin.verify()?;
        }
        Ok(())
    }
}

pub(super) fn create(
    path: &str,
    leaf_is_dir: bool,
    record: impl FnMut(&str) -> Result<()>,
) -> Result<(String, Vec<Placeholder>)> {
    let chain = create_retained(path, leaf_is_dir, record)?;
    // Convenience API for creation-only callers; production ACL admission uses
    // create_retained and keeps the original guard instead of discarding pins.
    Ok((chain.canon, chain.created))
}

pub fn create_retained(
    path: &str,
    leaf_is_dir: bool,
    record: impl FnMut(&str) -> Result<()>,
) -> Result<RetainedPlaceholderChain> {
    create_retained_with_writable_ancestors(path, leaf_is_dir, record, |_| false)
}

/// Retain strict protected leaves while admitting write sharing on selected
/// intermediate directories. The predicate receives the original canonical
/// parent path plus one validated component, including the extended path prefix.
/// It is never consulted for the final leaf, even when that leaf is a directory.
///
/// Directory list access and the exclusion of delete sharing remain unchanged:
/// an admitted ancestor can receive ordinary child renames without becoming
/// movable itself. Callers must restrict admission to actual writable ancestors,
/// excluding protected subtrees. This does not grant filesystem access or replace
/// the relative/no-follow opens and retained identity/reparse verification.
pub fn create_retained_with_writable_ancestors(
    path: &str,
    leaf_is_dir: bool,
    mut record: impl FnMut(&str) -> Result<()>,
    writable_ancestor: impl Fn(&str) -> bool,
) -> Result<RetainedPlaceholderChain> {
    // Validate the entire lexical input before any intent or filesystem effect.
    let (root, parts) = components(path)?;
    let handle = OwnedHandle(
        unsafe {
            CreateFileW(
                pcwstr(&wstr(&root)),
                1 | 0x80,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
        }
        .context("pin placeholder volume root")?,
    );
    let mut pins = vec![Pin::from_handle(handle, &root, true)?];
    let mut created = Vec::new();
    for (index, part) in parts.iter().enumerate() {
        let intermediate = index + 1 != parts.len();
        let parent = pins.last().unwrap();
        parent.verify()?;
        let name = format!("{}\\{part}", parent.canon.trim_end_matches('\\'));
        let share_write = intermediate && writable_ancestor(&name);
        parent.verify()?;
        let open = |create| {
            if share_write {
                child_with_sharing(parent, part, create, intermediate, true)
            } else {
                child(parent, part, create, intermediate)
            }
        };
        let pin = if let Some(pin) = open(None)? {
            pin
        } else {
            record(&name)?;
            parent.verify()?;
            if let Some(pin) = open(Some(intermediate || leaf_is_dir))? {
                created.push(Placeholder {
                    canon: pin.canon.clone(),
                    is_dir: pin.directory,
                });
                pin
            } else {
                // A collision can only be admitted through a fresh relative,
                // non-following pinned open. Never truncate or claim its birth.
                let pin = open(None)?.context("placeholder collision disappeared")?;
                ensure!(
                    pin.canon == name,
                    "placeholder collision changed the recorded spelling"
                );
                pin
            }
        };
        pins.push(pin);
    }
    for pin in &pins {
        pin.verify()?;
    }
    Ok(RetainedPlaceholderChain {
        canon: pins.last().unwrap().canon.clone(),
        created,
        pins,
    })
}

pub(super) fn validate_path(path: &str) -> Result<()> {
    components(path).map(|_| ())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn lexical_refusal_precedes_effects() {
        for path in [
            r"C:\a\..\leaf",
            r"C:\a\.\leaf",
            r"C:\a\leaf:stream",
            r"C:\a\NUL.txt",
            r"C:\a\LPT¹",
            r"C:\a\trailing.\leaf",
            r"C:\a\\leaf",
            r"\\server\share\leaf",
            r"\\.\C:\leaf",
        ] {
            let mut called = false;
            assert!(
                create(path, false, |_| {
                    called = true;
                    Ok(())
                })
                .is_err(),
                "{path}"
            );
            assert!(!called, "{path}");
        }
    }
}

#[cfg(test)]
#[path = "path_placeholder_metadata_tests.rs"]
mod metadata_tests;

#[cfg(test)]
#[path = "path_placeholder_mxc_tests.rs"]
mod mxc_tests;
