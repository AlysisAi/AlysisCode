//! Path canonicalization and file-identity (`FILE_ID_INFO`)
//! helpers — the "which file is this, exactly" layer that
//! `state_db.rs` keys on and `main.rs` validates with. Nothing here
//! touches ACL/ACE/SD types; the ACL machinery lives in
//! [`crate::acl`].

use anyhow::{Context, Result, bail};
use std::mem::size_of;
use windows::Win32::Foundation::{HANDLE, INVALID_HANDLE_VALUE};
use windows::Win32::Storage::FileSystem::{
    CreateFileW, FILE_ATTRIBUTE_DIRECTORY, FILE_FLAG_BACKUP_SEMANTICS, FILE_NAME_NORMALIZED,
    FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE, GETFINALPATHNAMEBYHANDLE_FLAGS,
    GetFinalPathNameByHandleW, OPEN_EXISTING, VOLUME_NAME_DOS,
};

use crate::util::{OwnedHandle, pcwstr, wstr};

/// True iff `decoded` round-trips back to `original` via
/// `encode_utf16` — i.e., `from_utf16_lossy(original)` was
/// lossless (no unpaired-surrogate substitution).
#[inline]
fn utf16_roundtrips(original: &[u16], decoded: &str) -> bool {
    decoded.encode_utf16().eq(original.iter().copied())
}

/// Typed [`canonicalize_path`] error. The hard-error vs
/// soft-skip decision in `main.rs` matches on the variant rather
/// than a substring of the formatted message, so a wording change
/// here cannot silently downgrade a glob to a skip.
#[derive(Debug)]
pub enum CanonError {
    /// Input contains `*` or `?` (outside the `\\?\` prefix).
    /// Always a config bug — never transient.
    Glob,
    /// `ERROR_FILE_NOT_FOUND` / `ERROR_PATH_NOT_FOUND` from
    /// `CreateFileW`. Distinguished so a deny target that does not
    /// exist yet can be materialized as a placeholder chain
    /// ([`create_placeholder_chain`]) instead of soft-skipped.
    NotFound(anyhow::Error),
    /// Open / final-path / attribute read failed for any other
    /// reason (unpaired-surrogate canonical paths, permission
    /// denied, transient IO error).
    Other(anyhow::Error),
}

impl std::fmt::Display for CanonError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Glob => write!(
                f,
                "Windows fs deny requires explicit file or directory \
                 paths; got glob"
            ),
            Self::NotFound(e) | Self::Other(e) => write!(f, "{e:#}"),
        }
    }
}
impl std::error::Error for CanonError {}

/// True iff the [`windows::core::Error`] at `e`'s root is
/// `ERROR_FILE_NOT_FOUND` (2) or `ERROR_PATH_NOT_FOUND` (3).
pub fn is_not_found(e: &anyhow::Error) -> bool {
    use windows::Win32::Foundation::{ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND};
    e.root_cause()
        .downcast_ref::<windows::core::Error>()
        .map(|we| we.code())
        .is_some_and(|c| c == ERROR_FILE_NOT_FOUND.into() || c == ERROR_PATH_NOT_FOUND.into())
}

/// True iff `p` names a UNC network path (`\\server\share\…`,
/// `//server/share/…`, `\\?\UNC\server\…`) — NOT a local extended
/// path (`\\?\C:\…`) or device namespace (`\\.\…`). Checked before
/// materializing a placeholder chain: never `mkdir` on the user's
/// SMB share; the local ACL model does not apply there.
pub fn is_unc_path(p: &str) -> bool {
    // Normalize `/` → `\` so Git-Bash-style `//server/…` and
    // mixed-separator inputs are recognized.
    let n = p.replace('/', "\\");
    if n.get(..8)
        .is_some_and(|s| s.eq_ignore_ascii_case(r"\\?\UNC\"))
    {
        return true;
    }
    n.starts_with(r"\\") && !n.starts_with(r"\\?\") && !n.starts_with(r"\\.\")
}

/// Strip a `\\?\` or `\\?\UNC\` extended-path prefix from a
/// pre-canonicalized input (either separator style). Used for
/// comparable component-count sort keys so `\\?\C:\y` and `C:\y`
/// depth-compare correctly.
pub fn strip_extended_prefix(p: &str) -> &str {
    for pfx in [r"\\?\UNC\", "//?/UNC/", r"\\?\", "//?/"] {
        if p.get(..pfx.len())
            .is_some_and(|s| s.eq_ignore_ascii_case(pfx))
        {
            return &p[pfx.len()..];
        }
    }
    p
}

/// Resolve `path` to its kernel-canonical form via
/// `GetFinalPathNameByHandleW` (handles symlinks, junctions, 8.3
/// short names, drive-letter case). Returns the `\\?\`-prefixed
/// path and whether it's a directory.
///
/// `state_db.rs` uses the canonical path as the DB key so a stamp
/// via two equivalent paths (e.g. `C:\PROGRA~1\…` and
/// `C:\Program Files\…`) refcounts correctly.
///
/// Returns [`CanonError::Other`] for any open/resolve failure,
/// including a canonical path that is not UTF-8-representable
/// (unpaired surrogates) — fail closed rather than round-trip a
/// U+FFFD-substituted string into the wrong filesystem object.
pub fn canonicalize_path(path: &str) -> Result<(String, bool), CanonError> {
    // Glob check on the INPUT, ignoring the `\\?\` extended-path
    // prefix (its `?` is not a wildcard). Without the strip,
    // canonicalize_path would reject its OWN output (which always
    // carries the prefix).
    let glob_in = path.strip_prefix(r"\\?\").unwrap_or(path);
    if glob_in.contains('*') || glob_in.contains('?') {
        return Err(CanonError::Glob);
    }
    (|| -> Result<(String, bool)> {
        // Open without requesting any data access so we don't
        // need read permission on the target.
        // `BACKUP_SEMANTICS` lets directories open too.
        let h = open_for_metadata(path)
            .with_context(|| format!("open '{path}' for canonicalization"))?;

        let buf = final_path_from_handle(h.raw())
            .with_context(|| format!("GetFinalPathNameByHandleW('{path}')"))?;
        let canonical = String::from_utf16_lossy(&buf);
        if !utf16_roundtrips(&buf, &canonical) {
            bail!(
                "canonical path for '{path}' is not representable as \
                 UTF-8 (contains unpaired surrogates); not supported"
            );
        }

        // Directory check on the OPEN HANDLE (not a path
        // re-resolve): the handle was opened without
        // `FILE_FLAG_OPEN_REPARSE_POINT`, so a symlink-to-dir was
        // already followed and `h` is the directory itself.
        use windows::Win32::Storage::FileSystem::{
            FILE_BASIC_INFO, FileBasicInfo, GetFileInformationByHandleEx,
        };
        let mut info = FILE_BASIC_INFO::default();
        unsafe {
            GetFileInformationByHandleEx(
                h.raw(),
                FileBasicInfo,
                (&mut info as *mut FILE_BASIC_INFO).cast(),
                size_of::<FILE_BASIC_INFO>() as u32,
            )
        }
        .with_context(|| format!("GetFileInformationByHandleEx(FileBasicInfo) '{path}'"))?;
        let is_dir = info.FileAttributes & FILE_ATTRIBUTE_DIRECTORY.0 != 0;
        Ok((canonical, is_dir))
    })()
    .map_err(|e| {
        if is_not_found(&e) {
            CanonError::NotFound(e)
        } else {
            CanonError::Other(e)
        }
    })
}

/// A filesystem object created by [`create_placeholder_chain`].
/// `canon` is the CANONICAL path so it keys the same as
/// `working_aces` rows.
#[derive(Debug, Clone)]
pub struct Placeholder {
    pub canon: String,
    pub is_dir: bool,
}

/// Materialize `path` (a deny target that does not exist yet):
/// `mkdir` each missing intermediate directory then create an
/// empty leaf. Returns `(leaf_canonical, components_we_created)`.
/// The caller stamps the leaf with the full [`SbAce::Deny`] mask —
/// the deny lands on the exact path, not an over-broad ancestor —
/// and the intermediates with object-only `DenyDelete`.
///
/// **Leaf is a FILE** unless `leaf_is_dir` (target ended with a
/// path separator) or a deeper deny target already created it as a
/// directory (overlapping denies are processed deepest-first). If
/// the user later needs a directory where a placeholder file
/// landed, they (non-sandboxed) delete + `mkdir`; the sandbox
/// cannot.
///
/// Placeholders are PERMANENT (leave-in-place): `acl restore`
/// strips the ACEs but never deletes the file/dir — a user who
/// wrote into a placeholder cannot lose data.
///
/// `record_intent` is called with each component's canonical path
/// BEFORE the on-disk create — intent-first, so a crash between
/// record and create leaves a harmless orphan row rather than an
/// unrecorded on-disk component a later holder cannot discover.
///
/// Existing components and exclusive-create collisions are accepted only after
/// a non-following relative open verifies their pinned type, identity and path.
/// Intermediates must be directories; an existing leaf may be either kind.
/// Reparse points, hardlinked leaves, ambiguous local names and conflicting
/// directory write/delete handles are refused. Every ancestor stays pinned
/// until this function returns. Errors preserve intent and partial components.
///
/// [`SbAce::Deny`]: crate::acl::SbAce::Deny
pub fn create_placeholder_chain(
    path: &str,
    leaf_is_dir: bool,
    record_intent: impl FnMut(&str) -> Result<()>,
) -> Result<(String, Vec<Placeholder>)> {
    placeholder::create(path, leaf_is_dir, record_intent)
}

#[path = "path_placeholder.rs"]
mod placeholder;

pub use placeholder::{
    RetainedPlaceholderChain, create_retained as create_placeholder_chain_retained,
    create_retained_with_writable_ancestors as create_placeholder_chain_retained_with_writable_ancestors,
};

/// Validate a reservation's complete lexical shape before any filesystem effect.
pub fn validate_placeholder_path(path: &str) -> Result<()> {
    placeholder::validate_path(path)
}

/// Open `path` with no data access (`dwDesiredAccess = 0`), full
/// sharing, `BACKUP_SEMANTICS` (so directories open), `OPEN_EXISTING`.
/// Shared by every metadata-query helper so a future change to the
/// open flags (e.g. `FILE_FLAG_OPEN_REPARSE_POINT`) lands once.
fn open_for_metadata(path: &str) -> Result<OwnedHandle> {
    let w = wstr(path);
    let h = unsafe {
        CreateFileW(
            pcwstr(&w),
            0,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
    }?;
    if h == INVALID_HANDLE_VALUE {
        bail!("CreateFileW('{path}'): INVALID_HANDLE_VALUE");
    }
    Ok(OwnedHandle(h))
}

/// `GetFinalPathNameByHandleW` two-call sizing pattern. Returns the
/// raw UTF-16 buffer (no NUL); caller decodes and round-trip-checks.
fn final_path_from_handle(h: HANDLE) -> Result<Vec<u16>> {
    let flags = GETFINALPATHNAMEBYHANDLE_FLAGS(FILE_NAME_NORMALIZED.0 | VOLUME_NAME_DOS.0);
    let need = unsafe { GetFinalPathNameByHandleW(h, &mut [], flags) };
    if need == 0 {
        bail!("sizing: {}", std::io::Error::last_os_error());
    }
    let mut buf = vec![0u16; need as usize + 1];
    let n = unsafe { GetFinalPathNameByHandleW(h, &mut buf, flags) };
    if n == 0 || n as usize >= buf.len() {
        // n >= buf.len() means "buffer too small" (a concurrent
        // rename grew the path between the size probe and the
        // data call). We bail rather than retry — narrow window,
        // and the caller can re-stamp.
        bail!("{}", std::io::Error::last_os_error());
    }
    buf.truncate(n as usize);
    Ok(buf)
}

/// Immediate parent of a `\\?\…` canonical path, as a string.
/// Returns `None` when there is no targetable parent (the path is
/// a root, or its immediate parent is a root — touching the
/// volume root's DACL is out of scope for the parent
/// `DenyFdc` ACE).
pub fn canonical_parent_of(canonical_path: &str) -> Option<String> {
    std::path::Path::new(canonical_path)
        .parent()
        .filter(|p| !p.as_os_str().is_empty() && p.parent().is_some())
        .map(|p| p.display().to_string())
}

// ─── File identity (FILE_ID_INFO) ───────────────────────────────────

/// A file's stable identity on a volume — the
/// `(VolumeSerialNumber, FileId128)` pair from `FILE_ID_INFO`. On
/// NTFS this is the MFT record identity, so it survives rename
/// and lets us both VALIDATE at restore time (the path still
/// resolves to the same file we stamped) and LOCATE a relocated
/// file for reporting. Stored as a 24-byte blob (8 + 16).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FileId {
    pub volume_serial: u64,
    pub id128: [u8; 16],
}

impl FileId {
    pub fn as_bytes(&self) -> [u8; 24] {
        let mut out = [0u8; 24];
        out[..8].copy_from_slice(&self.volume_serial.to_le_bytes());
        out[8..].copy_from_slice(&self.id128);
        out
    }
    pub fn from_bytes(b: &[u8]) -> Result<Self> {
        if b.len() != 24 {
            bail!("FileId::from_bytes: expected 24 bytes, got {}", b.len());
        }
        let mut vs = [0u8; 8];
        vs.copy_from_slice(&b[..8]);
        let mut id = [0u8; 16];
        id.copy_from_slice(&b[8..]);
        Ok(Self {
            volume_serial: u64::from_le_bytes(vs),
            id128: id,
        })
    }
}

/// `FILE_ID_INFO` of an already-open handle.
pub fn file_id_from_handle(h: HANDLE) -> Result<FileId> {
    use windows::Win32::Storage::FileSystem::{
        FILE_ID_INFO, FileIdInfo, GetFileInformationByHandleEx,
    };
    let mut info = FILE_ID_INFO::default();
    unsafe {
        GetFileInformationByHandleEx(
            h,
            FileIdInfo,
            (&mut info as *mut FILE_ID_INFO).cast(),
            size_of::<FILE_ID_INFO>() as u32,
        )
    }
    .context("GetFileInformationByHandleEx(FileIdInfo)")?;
    Ok(FileId {
        volume_serial: info.VolumeSerialNumber,
        id128: info.FileId.Identifier,
    })
}

/// `FILE_ID_INFO` of `canonical_path`. Opens with no data access
/// (identity query only), so a DENY-ACE on the file does not
/// interfere — the broker is the real user, not the deny trustee.
pub fn capture_file_id(canonical_path: &str) -> Result<FileId> {
    let h = open_for_metadata(canonical_path)
        .with_context(|| format!("open '{canonical_path}' for file_id"))?;
    file_id_from_handle(h.raw()).with_context(|| format!("file_id '{canonical_path}'"))
}

/// `(file_id, NumberOfLinks, is_dir)` from ONE metadata open.
/// `links > 1` on a non-directory means an alternate hardlink
/// name exists; the additive-ACE refcount in `state_db` is
/// PATH-keyed, so releasing one alias would strip the SHARED
/// DACL while another alias's holder still expects it denied.
/// Directory `NumberOfLinks` counts subdirs (NTFS has no dir
/// hardlinks), so callers gate the check on `!is_dir`.
pub fn capture_id_and_links(canonical_path: &str) -> Result<(FileId, u32, bool)> {
    use windows::Win32::Storage::FileSystem::{
        FILE_STANDARD_INFO, FileStandardInfo, GetFileInformationByHandleEx,
    };
    let h = open_for_metadata(canonical_path)
        .with_context(|| format!("open '{canonical_path}' for file_id+links"))?;
    let id = file_id_from_handle(h.raw()).with_context(|| format!("file_id '{canonical_path}'"))?;
    let mut std_info = FILE_STANDARD_INFO::default();
    unsafe {
        GetFileInformationByHandleEx(
            h.raw(),
            FileStandardInfo,
            (&mut std_info as *mut FILE_STANDARD_INFO).cast(),
            size_of::<FILE_STANDARD_INFO>() as u32,
        )
    }
    .with_context(|| {
        format!(
            "GetFileInformationByHandleEx(FileStandardInfo) \
             '{canonical_path}'"
        )
    })?;
    Ok((id, std_info.NumberOfLinks, std_info.Directory))
}

/// Best-effort: locate the CURRENT path of a file by its captured
/// `(volume_serial, file_id)`. Opens the volume root (`\\?\X:\`),
/// `OpenFileById` with an `ExtendedFileId` descriptor, then
/// `GetFinalPathNameByHandleW`. Returns `None` if the file was
/// deleted or the open fails for any reason. Used ONLY for
/// reporting `movedTo` — restore is path-anchored and never
/// relocates by inode (chasing the file by ID to remove its stamp
/// would re-expose a relocated secret).
pub fn locate_by_file_id(file_id: &FileId) -> Option<String> {
    locate_by_file_id_checked(file_id).ok().flatten()
}

/// A recovery decision that cannot safely be collapsed into "object absent".
/// Kept distinct from the underlying Win32 error so callers and tests can
/// identify the remediation without parsing localized operating-system text.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RecoveryLookupError {
    UnverifiableIdentity,
    DeletePending,
    VolumeUnavailable,
}

impl std::fmt::Display for RecoveryLookupError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::UnverifiableIdentity => write!(
                f,
                "cannot prove that the recorded object is absent: Windows rejected its file ID \
                 (error 87); recovery remains pending and the journal is preserved. If the \
                 original object was moved, return that same object to its recorded path and \
                 retry; recreating the pathname is not sufficient. If it was deleted, this \
                 build cannot recover automatically: privileged NTFS identity repair is not \
                 implemented"
            ),
            Self::DeletePending => write!(
                f,
                "recorded object is still delete-pending; recovery remains pending and the \
                 journal is preserved. Close applications retaining the object and retry; \
                 if deletion completes with an unverifiable file ID, automatic recovery \
                 remains unavailable"
            ),
            Self::VolumeUnavailable => write!(
                f,
                "cannot verify the recorded volume for ACL recovery; reconnect the same \
                 volume and retry. Recovery remains pending and the journal is preserved"
            ),
        }
    }
}

impl std::error::Error for RecoveryLookupError {}

fn file_id_open_error(error: windows::core::Error) -> Result<Option<String>> {
    let invalid_parameter =
        error.code() == windows::Win32::Foundation::ERROR_INVALID_PARAMETER.to_hresult();
    let error = anyhow::Error::new(error);
    if is_not_found(&error) {
        return Ok(None);
    }
    if invalid_parameter {
        return Err(error.context(RecoveryLookupError::UnverifiableIdentity));
    }
    Err(error.context("OpenFileById for ACL recovery; recovery remains pending"))
}

/// Recovery lookup: absence is proved only by a not-found response from
/// OpenFileById on the matching mounted volume. Inaccessible/unmounted volumes,
/// delete-pending objects and unsupported queries remain errors, never absence.
/// The lookup performs at most two opens on the matched volume (128-bit ID and
/// one lossless 64-bit compatibility retry); it never polls for deletion or
/// recursively searches the drive. See RECOVERY.md for the NTFS error-87 limit.
pub fn locate_by_file_id_checked(file_id: &FileId) -> Result<Option<String>> {
    use windows::Win32::Storage::FileSystem::{
        ExtendedFileIdType, FILE_ID_128, FILE_ID_DESCRIPTOR, FILE_ID_DESCRIPTOR_0,
        FILE_STANDARD_INFO, FileIdType, FileStandardInfo, GetFileInformationByHandleEx,
        OpenFileById,
    };
    // Open the volume root the file lived on. We need a handle ON
    // the volume to anchor OpenFileById; the captured volume
    // serial doesn't directly map to a drive letter, so try each
    // mounted local drive and match the serial — keeping the
    // locate volume-keyed (a moved file may not be on the drive
    // its canonical_path was recorded under).
    for drive in b'A'..=b'Z' {
        let root = format!(r"\\?\{}:\", drive as char);
        let vh = match open_for_metadata(&root) {
            Ok(h) => h,
            Err(_) => continue,
        };
        // Match the volume by reading FILE_ID_INFO of the root.
        match file_id_from_handle(vh.raw()) {
            Ok(id) if id.volume_serial == file_id.volume_serial => {}
            _ => continue,
        }
        let desc = FILE_ID_DESCRIPTOR {
            dwSize: std::mem::size_of::<FILE_ID_DESCRIPTOR>() as u32,
            Type: ExtendedFileIdType,
            Anonymous: FILE_ID_DESCRIPTOR_0 {
                ExtendedFileId: FILE_ID_128 {
                    Identifier: file_id.id128,
                },
            },
        };
        // dwDesiredAccess = 0: GetFinalPathNameByHandleW needs only
        // a valid handle, not read-data, so a relocated file whose
        // DACL no longer grants the broker read still resolves.
        let open = |descriptor: &FILE_ID_DESCRIPTOR| unsafe {
            OpenFileById(
                vh.raw(),
                descriptor,
                0,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                None,
                FILE_FLAG_BACKUP_SEMANTICS,
            )
        };
        let mut opened = open(&desc);
        if opened.as_ref().err().is_some_and(|error| {
            error.code() == windows::Win32::Foundation::ERROR_INVALID_PARAMETER.to_hresult()
        }) && file_id.id128[8..] == [0u8; 8]
        {
            // NTFS uses a 64-bit file reference. Some object/deletion states
            // reject an ExtendedFileId descriptor; retry the lossless native
            // form, then verify the complete identity on any returned handle.
            let legacy = FILE_ID_DESCRIPTOR {
                dwSize: size_of::<FILE_ID_DESCRIPTOR>() as u32,
                Type: FileIdType,
                Anonymous: FILE_ID_DESCRIPTOR_0 {
                    FileId: i64::from_le_bytes(file_id.id128[..8].try_into().unwrap()),
                },
            };
            opened = open(&legacy);
        }
        let fh = match opened {
            Ok(h) => OwnedHandle(h),
            Err(error) => return file_id_open_error(error),
        };
        anyhow::ensure!(
            file_id_from_handle(fh.raw())? == *file_id,
            "recovery file-ID lookup returned a different object"
        );
        let mut standard = FILE_STANDARD_INFO::default();
        unsafe {
            GetFileInformationByHandleEx(
                fh.raw(),
                FileStandardInfo,
                (&mut standard as *mut FILE_STANDARD_INFO).cast(),
                size_of::<FILE_STANDARD_INFO>() as u32,
            )?;
        }
        if standard.DeletePending || standard.NumberOfLinks == 0 {
            return Err(RecoveryLookupError::DeletePending.into());
        }
        let buf = final_path_from_handle(fh.raw())?;
        let s = String::from_utf16_lossy(&buf);
        anyhow::ensure!(
            utf16_roundtrips(&buf, &s),
            "recovery path is not valid Unicode"
        );
        return Ok(Some(s));
    }
    Err(RecoveryLookupError::VolumeUnavailable.into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recovery_open_errors_only_admit_definitive_absence() {
        use windows::Win32::Foundation::{
            ERROR_ACCESS_DENIED, ERROR_FILE_NOT_FOUND, ERROR_INVALID_PARAMETER,
            ERROR_NOT_SUPPORTED, ERROR_PATH_NOT_FOUND, ERROR_SHARING_VIOLATION,
        };

        for code in [ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND] {
            assert!(matches!(
                file_id_open_error(windows::core::Error::from_hresult(code.to_hresult())),
                Ok(None)
            ));
        }
        let error = file_id_open_error(windows::core::Error::from_hresult(
            ERROR_INVALID_PARAMETER.to_hresult(),
        ))
        .unwrap_err();
        assert_eq!(
            error.downcast_ref::<RecoveryLookupError>(),
            Some(&RecoveryLookupError::UnverifiableIdentity)
        );
        assert!(
            error
                .to_string()
                .contains("recreating the pathname is not sufficient")
        );
        assert!(
            error
                .to_string()
                .contains("privileged NTFS identity repair is not implemented")
        );
        // Neither permission failures nor an unsupported filesystem prove deletion.
        for code in [
            ERROR_ACCESS_DENIED,
            ERROR_NOT_SUPPORTED,
            ERROR_SHARING_VIOLATION,
        ] {
            assert!(
                file_id_open_error(windows::core::Error::from_hresult(code.to_hresult())).is_err()
            );
        }
    }

    #[test]
    fn delete_pending_object_is_not_reported_as_absent() {
        let root = std::env::temp_dir().join(format!("alysis-id-pending-{}", std::process::id()));
        std::fs::create_dir(&root).unwrap();
        let path = root.join("held.txt");
        std::fs::write(&path, "owned recovery fixture").unwrap();
        let handle = open_for_metadata(path.to_str().unwrap()).unwrap();
        let id = file_id_from_handle(handle.raw()).unwrap();
        std::fs::remove_file(&path).unwrap();
        let result = locate_by_file_id_checked(&id);
        drop(handle);
        std::fs::remove_dir(&root).unwrap();
        let error = result.expect_err("a retained unlinked object must not be declared absent");
        assert_eq!(
            error.downcast_ref::<RecoveryLookupError>(),
            Some(&RecoveryLookupError::DeletePending)
        );
    }

    #[test]
    fn checked_file_id_lookup_tracks_moves_and_fails_closed_for_deleted_ids() {
        let root = std::env::temp_dir().join(format!("alysis-id-recovery-{}", std::process::id()));
        std::fs::create_dir(&root).unwrap();
        let original = root.join("original.txt");
        let moved = root.join("moved.txt");
        std::fs::write(&original, "owned recovery fixture").unwrap();
        let id = capture_file_id(original.to_str().unwrap()).unwrap();
        assert_eq!(
            capture_file_id(&locate_by_file_id_checked(&id).unwrap().unwrap()).unwrap(),
            id
        );
        std::fs::rename(&original, &moved).unwrap();
        assert_eq!(
            capture_file_id(&locate_by_file_id_checked(&id).unwrap().unwrap()).unwrap(),
            id
        );
        std::fs::remove_file(&moved).unwrap();
        // NTFS can report either a delete-pending object or error 87 after
        // unlink. Neither may silently become an absent, successfully cleaned
        // record. Only a definitive FILE/PATH_NOT_FOUND response yields None.
        match locate_by_file_id_checked(&id) {
            Ok(None) => {} // An explicit not-found response is also valid.
            Err(error) => assert!(
                matches!(
                    error.downcast_ref::<RecoveryLookupError>(),
                    Some(
                        RecoveryLookupError::UnverifiableIdentity
                            | RecoveryLookupError::DeletePending
                    )
                ),
                "unexpected recovery failure: {error:#}"
            ),
            Ok(Some(path)) => panic!("deleted object unexpectedly resolves to {path}"),
        }
        let unknown = FileId {
            volume_serial: 0,
            ..id
        };
        assert_eq!(
            locate_by_file_id_checked(&unknown)
                .unwrap_err()
                .downcast_ref::<RecoveryLookupError>(),
            Some(&RecoveryLookupError::VolumeUnavailable)
        );
        std::fs::remove_dir(&root).unwrap();
    }

    #[test]
    fn canonicalize_rejects_globs() {
        for p in ["C:\\foo\\*.txt", "C:\\foo\\bar?.txt"] {
            assert!(matches!(canonicalize_path(p), Err(CanonError::Glob)), "{p}");
        }
        assert!(matches!(
            canonicalize_path(r"C:\srt-win-no-such-path"),
            Err(CanonError::NotFound(_))
        ));
    }

    /// `create_placeholder_chain` records intent before creating,
    /// builds the missing dirs + leaf, and returns exactly what it
    /// created; a second call is idempotent (`AlreadyExists`
    /// accepted, nothing new returned).
    #[test]
    fn placeholder_chain_round_trip() {
        let base = crate::util::test_temp_dir().join(format!("srt-ph-{}", std::process::id()));
        std::fs::create_dir_all(&base).unwrap();
        let leaf = base.join("a").join("b").join("secret.txt");
        let mut recorded = Vec::new();
        let mut rec = |c: &str| {
            // Intent-first: not on disk yet when recorded.
            assert!(!std::path::Path::new(c).exists(), "{c}");
            recorded.push(c.to_owned());
            Ok(())
        };
        let (leaf_canon, chain) =
            create_placeholder_chain(&leaf.display().to_string(), false, &mut rec).unwrap();
        // Two dirs + one leaf file, root-first; leaf canonical.
        assert_eq!(chain.len(), 3);
        assert!(chain[0].is_dir && chain[1].is_dir && !chain[2].is_dir);
        assert!(leaf_canon.starts_with(r"\\?\"), "{leaf_canon}");
        assert_eq!(leaf_canon, chain[2].canon);
        assert_eq!(recorded.len(), 3);
        assert!(leaf.is_file());
        // Idempotent: second call finds everything already present
        // → returns nothing new; leaf canon unchanged.
        let (again_canon, again) =
            create_placeholder_chain(&leaf.display().to_string(), false, |_| Ok(())).unwrap();
        assert!(again.is_empty(), "got {again:?}");
        assert_eq!(again_canon, leaf_canon);
        // Trailing separator ⇒ directory leaf.
        let dir_leaf = format!("{}\\", base.join("d").display());
        let (dc, _) = create_placeholder_chain(&dir_leaf, true, |_| Ok(())).unwrap();
        assert!(base.join("d").is_dir());
        assert!(dc.starts_with(r"\\?\"), "{dc}");
        std::fs::remove_dir_all(&base).ok();
    }

    fn placeholder_failure_fixture(label: &str) -> std::path::PathBuf {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let base = crate::util::test_temp_dir().join(format!(
            "srt-ph-failure-{label}-{}-{nonce}",
            std::process::id()
        ));
        std::fs::create_dir(&base).unwrap();
        base
    }

    #[test]
    fn placeholder_failure_preserves_created_directory() {
        let base = placeholder_failure_fixture("partial");
        let first = base.join("a");
        let leaf = first.join("b").join("secret.txt");
        let mut intents = Vec::new();
        let mut first_id = None;
        let error = create_placeholder_chain(&leaf.display().to_string(), false, |path| {
            intents.push(path.to_owned());
            assert!(!std::path::Path::new(path).exists());
            if intents.len() == 2 {
                first_id = Some(capture_file_id(&first.display().to_string()).unwrap());
                bail!("injected second intent failure");
            }
            Ok(())
        })
        .unwrap_err();
        assert!(error.to_string().contains("injected second intent failure"));
        assert_eq!(intents.len(), 2);
        assert!(
            first.is_dir(),
            "partial placeholder must remain: {}",
            first.display()
        );
        assert_eq!(
            capture_file_id(&first.display().to_string()).unwrap(),
            first_id.unwrap()
        );
        assert!(!first.join("b").exists());
        // Remove only these empty, test-owned objects after their identity checks.
        std::fs::remove_dir(&first).unwrap();
        std::fs::remove_dir(&base).unwrap();
    }

    #[test]
    fn placeholder_failure_preserves_replacement_identity() {
        let base = placeholder_failure_fixture("replacement");
        let first = base.join("a");
        let moved = base.join("original-moved");
        let leaf = first.join("b").join("secret.txt");
        let mut intents = Vec::new();
        let mut original_id = None;
        let mut replacement_id = None;
        let error = create_placeholder_chain(&leaf.display().to_string(), false, |path| {
            intents.push(path.to_owned());
            if intents.len() == 2 {
                original_id = Some(capture_file_id(&first.display().to_string()).unwrap());
                if let Err(error) = std::fs::rename(&first, &moved) {
                    assert_eq!(error.raw_os_error(), Some(32));
                    bail!("injected intent failure after replacement blocked");
                }
                // Keep this replacement empty: a canary child would mask unsafe
                // name-based remove_dir because Windows refuses nonempty dirs.
                std::fs::create_dir(&first).unwrap();
                replacement_id = Some(capture_file_id(&first.display().to_string()).unwrap());
                bail!("injected intent failure after replacement");
            }
            Ok(())
        })
        .unwrap_err();
        assert!(
            error
                .to_string()
                .contains("injected intent failure after replacement")
        );
        assert_eq!(intents.len(), 2);
        assert!(
            first.is_dir(),
            "foreign replacement must remain: {}",
            first.display()
        );
        if replacement_id.is_none() {
            assert_eq!(
                capture_file_id(&first.display().to_string()).unwrap(),
                original_id.unwrap()
            );
            assert!(!moved.exists());
            // Pins are released on error: the original object can now move.
            std::fs::rename(&first, &moved).unwrap();
            std::fs::remove_dir(&moved).unwrap();
            std::fs::remove_dir(&base).unwrap();
            return;
        }
        assert_ne!(original_id, replacement_id);
        assert_eq!(
            capture_file_id(&first.display().to_string()).unwrap(),
            replacement_id.unwrap()
        );
        assert_eq!(
            capture_file_id(&moved.display().to_string()).unwrap(),
            original_id.unwrap()
        );
        assert!(!first.join("b").exists());
        std::fs::remove_dir(&first).unwrap();
        std::fs::remove_dir(&moved).unwrap();
        std::fs::remove_dir(&base).unwrap();
    }

    #[test]
    fn canonicalize_round_trip_self() {
        // The test binary's own path is a real file we definitely
        // can open.
        let exe = std::env::current_exe().unwrap();
        let (canon, is_dir) = canonicalize_path(&exe.display().to_string()).unwrap();
        assert!(canon.starts_with(r"\\?\"), "got {canon}");
        assert!(!is_dir);
        // Round-trip: canonicalizing the canonical path is a no-op.
        let (again, _) = canonicalize_path(&canon).unwrap();
        assert_eq!(canon, again);
    }

    /// The parent of a file directly under a drive root IS the
    /// volume root. Touching the volume root's DACL (even with an
    /// additive `DenyFdc` ACE) is out of scope — so
    /// `canonical_parent_of` must return `None` for a top-level
    /// child. Rust's `Path::parent()` returns the root (not
    /// `None`) in this case, so the helper has to recognize and
    /// reject it.
    #[test]
    fn parent_at_volume_root_is_not_stampable() {
        for p in [r"\\?\C:\foo.txt", r"\\?\C:\ProgramData", r"\\?\D:\x"] {
            assert_eq!(
                canonical_parent_of(p),
                None,
                "would stamp the volume root for top-level child {p:?}: \
                 got {:?}",
                canonical_parent_of(p),
            );
        }
        // Anchors (already pass today, must keep passing):
        // the root itself has no parent; a nested path's parent
        // is the immediate directory.
        assert_eq!(canonical_parent_of(r"\\?\C:\"), None);
        assert_eq!(
            canonical_parent_of(r"\\?\C:\a\b").as_deref(),
            Some(r"\\?\C:\a"),
        );
    }

    #[test]
    fn lossy_canonical_path_detected() {
        // Lone high surrogate → from_utf16_lossy substitutes
        // U+FFFD, so the round-trip check must reject it.
        let bad = [0x0041, 0xD800, 0x0042];
        assert!(!utf16_roundtrips(&bad, &String::from_utf16_lossy(&bad)));
        // A valid surrogate PAIR (U+1F600 = D83D DE00) and plain
        // ASCII both round-trip.
        let pair = [0x0041, 0xD83D, 0xDE00, 0x0042];
        assert!(utf16_roundtrips(&pair, &String::from_utf16_lossy(&pair)));
        let ok = [0x0041, 0x0042];
        assert!(utf16_roundtrips(&ok, &String::from_utf16_lossy(&ok)));
    }

    #[test]
    fn unc_and_extended_prefix() {
        for p in [
            r"\\server\share\x",
            "//server/share/x",
            r"\\?\UNC\server\share",
            r"\\?\unc\server\share",
            "//?/UNC/server/share",
        ] {
            assert!(is_unc_path(p), "should be UNC: {p}");
        }
        for p in [r"\\?\C:\x", "//?/C:/x", r"\\.\PIPE\x", r"C:\x", "/tmp/x"] {
            assert!(!is_unc_path(p), "should NOT be UNC: {p}");
        }
        assert_eq!(strip_extended_prefix(r"\\?\C:\y"), r"C:\y");
        assert_eq!(strip_extended_prefix("//?/C:/y"), "C:/y");
        assert_eq!(strip_extended_prefix(r"\\?\UNC\srv\s"), r"srv\s");
        assert_eq!(strip_extended_prefix(r"C:\y"), r"C:\y");
    }
}

#[cfg(test)]
#[path = "path_placeholder_tests.rs"]
mod placeholder_tests;
