//! Admission for the existing baseline ACL ledger. Unknown history is never
//! renamed, scrubbed, repaired or replaced. These admission pins complement
//! the caller's init lock; they do not make SQLite and ACL mutations atomic.
//! Ordinary SQLite read-only WAL admission can create/rebuild derived SHM.
//! Preservation means no replacement of authoritative recovery history, not
//! byte-immutable SHM; disabling that coordination can break cold WAL reads.
use super::*;
use crate::util::{OwnedHandle, pcwstr, wstr};
use anyhow::ensure;
use std::path::Path;
use windows::Win32::Storage::FileSystem::{
    BY_HANDLE_FILE_INFORMATION, CREATE_NEW, CreateFileW, FILE_ATTRIBUTE_DIRECTORY,
    FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT,
    FILE_NAME_NORMALIZED, FILE_SHARE_READ, FILE_SHARE_WRITE, FILE_STANDARD_INFO, FileStandardInfo,
    GETFINALPATHNAMEBYHANDLE_FLAGS, GetFileInformationByHandle, GetFileInformationByHandleEx,
    GetFileSizeEx, GetFinalPathNameByHandleW, OPEN_EXISTING, VOLUME_NAME_DOS,
};

const SIDECARS: [&str; 3] = ["-wal", "-shm", "-journal"];

struct PinnedMain {
    handle: OwnedHandle,
    requested_parents: Vec<PinnedDirectory>,
    canonical_parents: Vec<PinnedDirectory>,
    requested: PathBuf,
    path: PathBuf,
    id: FileId,
    fresh: bool,
}

// Windows package virtualization can redirect a path without reparse points.
// Pin both namespaces, binding them through parent and main FileIds, rather
// than assuming that lexical equality proves the identity SQLite will open.
struct PinnedDirectory {
    handle: OwnedHandle,
    path: PathBuf,
    resolved: String,
    id: FileId,
}
impl PinnedDirectory {
    fn open(path: &Path, physical: bool) -> Result<Self> {
        let handle = open_component(path, false, true)?;
        let resolved = final_path(handle.raw())?;
        if physical {
            ensure!(
                same_spelling(
                    &resolved,
                    path.to_str().context("state DB ancestor is not Unicode")?
                ),
                "canonical state DB ancestor resolves to a different namespace"
            );
        }
        let id = path_id::file_id_from_handle(handle.raw())?;
        Ok(Self {
            handle,
            path: path.to_owned(),
            resolved,
            id,
        })
    }
    fn verify(&self) -> Result<()> {
        validate_component(self.handle.raw(), true)?;
        let reopened = open_component(&self.path, false, true)?;
        ensure!(
            path_id::file_id_from_handle(self.handle.raw())? == self.id
                && path_id::file_id_from_handle(reopened.raw())? == self.id
                && same_spelling(&final_path(self.handle.raw())?, &self.resolved)
                && same_spelling(&final_path(reopened.raw())?, &self.resolved),
            "state DB ancestor changed during admission"
        );
        Ok(())
    }
}
fn same_spelling(left: &str, right: &str) -> bool {
    path_id::strip_extended_prefix(left)
        .trim_end_matches('\\')
        .eq_ignore_ascii_case(path_id::strip_extended_prefix(right).trim_end_matches('\\'))
}
fn ancestors(path: &Path) -> Vec<&Path> {
    let mut ancestors: Vec<_> = path.ancestors().skip(1).collect();
    ancestors.reverse();
    ancestors
}

impl PinnedMain {
    fn open(path: &Path, create: bool) -> Result<Option<Self>> {
        let text = path.to_str().context("state DB path is not Unicode")?;
        ensure!(
            path.is_absolute() && !path_id::is_unc_path(text),
            "state DB requires an absolute local path"
        );
        ensure!(
            !path.components().any(|c| matches!(
                c,
                std::path::Component::CurDir | std::path::Component::ParentDir
            )),
            "state DB path contains a relative component"
        );
        let leaf = path
            .file_name()
            .and_then(|v| v.to_str())
            .context("state DB has no filename")?;
        ensure!(
            !leaf.contains(':') && !leaf.ends_with([' ', '.']),
            "state DB filename is an alias or stream"
        );
        let mut parents = Vec::new();
        for parent in ancestors(path) {
            match PinnedDirectory::open(parent, false) {
                Ok(pin) => parents.push(pin),
                Err(error) if !create && path_id::is_not_found(&error) => return Ok(None),
                Err(error) => return Err(error),
            }
        }
        let (handle, fresh) = match open_component(path, false, false) {
            Ok(handle) => (handle, false),
            Err(error) if path_id::is_not_found(&error) => {
                require_no_sidecars(path)?;
                if !create {
                    return Ok(None);
                }
                // CREATE_NEW is the ownership boundary. A competitor's file
                // is never adopted as fresh. On any later error keep the file
                // and sidecars rather than guessing which effects committed.
                let handle = open_component(path, true, false)?;
                require_no_sidecars(path)?;
                (handle, true)
            }
            Err(error) => return Err(error),
        };
        Self::bind(path, handle, parents, fresh).map(Some)
    }
    fn bind(
        path: &Path,
        handle: OwnedHandle,
        parents: Vec<PinnedDirectory>,
        fresh: bool,
    ) -> Result<Self> {
        let leaf = path
            .file_name()
            .and_then(|name| name.to_str())
            .context("state DB has no Unicode filename")?;
        let canonical = PathBuf::from(final_path(handle.raw())?);
        let canonical_text = canonical
            .to_str()
            .context("state DB canonical path is not Unicode")?;
        ensure!(
            canonical.is_absolute() && !path_id::is_unc_path(canonical_text),
            "canonical state DB requires an absolute local path"
        );
        ensure!(
            canonical
                .file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.eq_ignore_ascii_case(leaf)),
            "state DB leaf spelling changes the sidecar namespace"
        );
        let mut canonical_parents = Vec::new();
        for parent in ancestors(&canonical) {
            canonical_parents.push(PinnedDirectory::open(parent, true)?);
        }
        ensure!(
            parents
                .last()
                .context("state DB requested parent missing")?
                .id
                == canonical_parents
                    .last()
                    .context("state DB canonical parent missing")?
                    .id,
            "requested and canonical state DB parents differ"
        );
        let id = path_id::file_id_from_handle(handle.raw())?;
        let physical = open_component(&canonical, false, false)?;
        ensure!(
            path_id::file_id_from_handle(physical.raw())? == id
                && same_spelling(&final_path(physical.raw())?, canonical_text),
            "canonical state DB main differs from original handle"
        );
        if fresh {
            require_no_sidecars(&canonical)?;
        }
        require_sidecar_agreement(path, &canonical)?;
        let mut size = 0;
        unsafe {
            GetFileSizeEx(handle.raw(), &mut size)?;
        }
        ensure!(
            if fresh { size == 0 } else { size > 0 },
            "state DB size is inconsistent with its birth; preserve ambiguous recovery state"
        );
        let pinned = Self {
            handle,
            requested_parents: parents,
            canonical_parents,
            requested: path.to_owned(),
            path: canonical,
            id,
            fresh,
        };
        pinned.verify()?;
        Ok(pinned)
    }
    fn verify(&self) -> Result<()> {
        // A final check detects observed link/type/delete-state drift; it is
        // not an atomic promise against a concurrent trusted administrator.
        validate_component(self.handle.raw(), false)?;
        for parent in self.requested_parents.iter().chain(&self.canonical_parents) {
            parent.verify()?;
        }
        let requested = open_component(&self.requested, false, false)?;
        let physical = open_component(&self.path, false, false)?;
        ensure!(
            path_id::file_id_from_handle(self.handle.raw())? == self.id
                && path_id::file_id_from_handle(requested.raw())? == self.id
                && path_id::file_id_from_handle(physical.raw())? == self.id
                && same_spelling(&final_path(self.handle.raw())?, self.path.to_str().unwrap())
                && same_spelling(&final_path(requested.raw())?, self.path.to_str().unwrap())
                && same_spelling(&final_path(physical.raw())?, self.path.to_str().unwrap()),
            "state DB identity changed during admission"
        );
        require_sidecar_agreement(&self.requested, &self.path)?;
        Ok(())
    }
}

fn open_component(path: &Path, create: bool, directory: bool) -> Result<OwnedHandle> {
    let text = path.to_str().context("state DB component is not Unicode")?;
    let extended = if text.starts_with(r"\\?\") {
        text.to_owned()
    } else {
        format!(r"\\?\{text}")
    };
    let handle = OwnedHandle(
        unsafe {
            CreateFileW(
                pcwstr(&wstr(&extended)),
                1 | 0x80 | if create { 2 } else { 0 },
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                if create { CREATE_NEW } else { OPEN_EXISTING },
                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
        }
        .with_context(|| format!("pin state DB component {}", path.display()))?,
    );
    validate_component(handle.raw(), directory)?;
    Ok(handle)
}

fn validate_component(handle: HANDLE, directory: bool) -> Result<()> {
    let mut info = BY_HANDLE_FILE_INFORMATION::default();
    unsafe {
        GetFileInformationByHandle(handle, &mut info)?;
    }
    ensure!(
        info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT.0 == 0,
        "state DB path contains a reparse point"
    );
    ensure!(
        (info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY.0 != 0) == directory,
        "state DB component has wrong type"
    );
    ensure!(
        directory || info.nNumberOfLinks == 1,
        "state DB main file has multiple links"
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
        "state DB object has pending deletion"
    );
    Ok(())
}

fn final_path(handle: HANDLE) -> Result<String> {
    let flags = GETFINALPATHNAMEBYHANDLE_FLAGS(FILE_NAME_NORMALIZED.0 | VOLUME_NAME_DOS.0);
    let needed = unsafe { GetFinalPathNameByHandleW(handle, &mut [], flags) };
    ensure!(
        needed > 0 && needed < 32768,
        "cannot size state DB final path"
    );
    let mut wide = vec![0; needed as usize + 1];
    let used = unsafe { GetFinalPathNameByHandleW(handle, &mut wide, flags) };
    ensure!(
        used > 0 && (used as usize) < wide.len(),
        "cannot read state DB final path"
    );
    Ok(String::from_utf16(&wide[..used as usize])?)
}

fn sidecar(path: &Path, suffix: &str) -> PathBuf {
    let mut name = path.as_os_str().to_os_string();
    name.push(suffix);
    PathBuf::from(name)
}
fn require_sidecar_agreement(requested: &Path, canonical: &Path) -> Result<()> {
    if same_spelling(requested.to_str().unwrap(), canonical.to_str().unwrap()) {
        return Ok(());
    }
    fn existing(path: &Path) -> Result<Option<OwnedHandle>> {
        match open_component(path, false, false) {
            Ok(handle) => Ok(Some(handle)),
            Err(error) if path_id::is_not_found(&error) => Ok(None),
            Err(error) => Err(error),
        }
    }
    for suffix in SIDECARS {
        let requested_path = sidecar(requested, suffix);
        let canonical_path = sidecar(canonical, suffix);
        match (existing(&requested_path)?, existing(&canonical_path)?) {
            (None, None) => {}
            (Some(requested), Some(canonical)) => {
                ensure!(
                    path_id::file_id_from_handle(requested.raw())?
                        == path_id::file_id_from_handle(canonical.raw())?
                        && same_spelling(
                            &final_path(canonical.raw())?,
                            canonical_path.to_str().unwrap()
                        ),
                    "state DB sidecar namespaces have different identities"
                );
            }
            _ => bail!(
                "state DB sidecar presence differs between requested and canonical namespaces"
            ),
        }
    }
    Ok(())
}

fn require_no_sidecars(path: &Path) -> Result<()> {
    for suffix in SIDECARS {
        let candidate = sidecar(path, suffix);
        match std::fs::symlink_metadata(&candidate) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return Err(error).context("cannot determine prior state DB sidecar presence");
            }
            Ok(_) => bail!(
                "state DB has a sidecar without an admitted main file; preserve recovery evidence"
            ),
        }
    }
    Ok(())
}

// Exact stored SQL includes conflict actions, CHECK constraints and triggers,
// unlike a columns-only comparison. A semantically equivalent foreign schema
// can be refused: this boundary performs no migrations. SQLite creates the
// expected representation itself using the same compiled schema and library.
type SchemaRow = (String, String, String, Option<String>);
fn schema_rows(connection: &Connection) -> Result<Vec<SchemaRow>> {
    // Bound the returned SQL text even before rusqlite allocates a String.
    // Original byte lengths are checked before reading the capped values.
    let mut statement = connection.prepare("SELECT length(CAST(type AS BLOB)),length(CAST(name AS BLOB)),length(CAST(tbl_name AS BLOB)),length(CAST(sql AS BLOB)),substr(type,1,32),substr(name,1,512),substr(tbl_name,1,512),substr(sql,1,65536) FROM sqlite_schema ORDER BY type,name LIMIT 33")?;
    let mut rows = statement.query([])?;
    let mut result = Vec::new();
    let mut bytes = 0usize;
    while let Some(row) = rows.next()? {
        ensure!(
            result.len() < 32,
            "state DB schema object count exceeds admission bound"
        );
        let type_bytes: i64 = row.get(0)?;
        let name_bytes: i64 = row.get(1)?;
        let table_bytes: i64 = row.get(2)?;
        let sql_bytes: Option<i64> = row.get(3)?;
        ensure!(
            (0..=32).contains(&type_bytes)
                && (0..=512).contains(&name_bytes)
                && (0..=512).contains(&table_bytes)
                && (0..=65536).contains(&sql_bytes.unwrap_or(0)),
            "state DB schema field exceeds admission bound"
        );
        bytes += (type_bytes + name_bytes + table_bytes + sql_bytes.unwrap_or(0)) as usize;
        ensure!(
            bytes <= 64 * 1024,
            "state DB schema exceeds admission bound"
        );
        result.push((row.get(4)?, row.get(5)?, row.get(6)?, row.get(7)?));
    }
    Ok(result)
}
fn expected_schema(schema: &str) -> Result<Vec<SchemaRow>> {
    let expected = Connection::open_in_memory()?;
    expected.execute_batch(schema)?;
    schema_rows(&expected)
}
fn validate(connection: &Connection, expected: &[SchemaRow], version: i64) -> Result<()> {
    let actual: i64 = connection.pragma_query_value(None, "user_version", |row| row.get(0))?;
    ensure!(
        actual == version,
        "state DB schema version {actual} differs from {version}; preserve journal for explicit recovery"
    );
    ensure!(
        schema_rows(connection)? == expected,
        "state DB schema differs from the supported journal; preserve it without repair"
    );
    let check: String = connection.query_row("PRAGMA quick_check(1)", [], |row| row.get(0))?;
    ensure!(
        check == "ok",
        "state DB integrity is unverified; preserve journal for explicit recovery"
    );
    Ok(())
}
fn connect(path: &Path, flags: rusqlite::OpenFlags) -> Result<Connection> {
    let connection = Connection::open_with_flags_and_vfs(path, flags, "win32-longpath")
        .with_context(|| format!("open admitted state DB {}", path.display()))?;
    connection.busy_timeout(std::time::Duration::from_secs(5))?;
    Ok(connection)
}
fn configure(connection: &Connection, memory: bool) -> Result<()> {
    connection.pragma_update(None, "synchronous", "FULL")?;
    connection.pragma_update(None, "journal_mode", "WAL")?;
    connection.pragma_update(None, "foreign_keys", "ON")?;
    let mode: String = connection.pragma_query_value(None, "journal_mode", |row| row.get(0))?;
    let sync: i64 = connection.pragma_query_value(None, "synchronous", |row| row.get(0))?;
    let foreign_keys: i64 =
        connection.pragma_query_value(None, "foreign_keys", |row| row.get(0))?;
    ensure!(
        mode == (if memory { "memory" } else { "wal" }) && sync == 2 && foreign_keys == 1,
        "state DB requires verified WAL/FULL and foreign keys before effects"
    );
    Ok(())
}
fn initialize(connection: &Connection, schema: &str, version: i64) -> Result<()> {
    let transaction = connection.unchecked_transaction()?;
    transaction.execute_batch(schema)?;
    transaction.pragma_update(None, "user_version", version)?;
    transaction
        .commit()
        .context("initialize fresh state DB; preserve evidence on ambiguous commit")?;
    Ok(())
}

pub(super) fn open_read_write(path: &Path, schema: &str, version: i64) -> Result<Connection> {
    let expected = expected_schema(schema)?;
    #[cfg(test)]
    if path == Path::new(":memory:") {
        let connection = Connection::open_in_memory()?;
        configure(&connection, true)?;
        initialize(&connection, schema, version)?;
        validate(&connection, &expected, version)?;
        return Ok(connection);
    }
    let pin = PinnedMain::open(path, true)?.context("state DB creation returned no main file")?;
    if !pin.fresh {
        let probe = connect(&pin.path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
        validate(&probe, &expected, version)
            .context("refuse unsupported existing state DB before writable open")?;
        pin.verify()?;
    }
    let connection = connect(&pin.path, rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE)?;
    pin.verify()?;
    if !pin.fresh {
        validate(&connection, &expected, version)?;
    } else {
        require_no_sidecars(&pin.path)?;
        let mut size = 0;
        unsafe {
            GetFileSizeEx(pin.handle.raw(), &mut size)?;
        }
        let version: i64 = connection.pragma_query_value(None, "user_version", |row| row.get(0))?;
        ensure!(
            size == 0 && version == 0 && schema_rows(&connection)?.is_empty(),
            "new state DB changed before initialization; preserve ambiguous state"
        );
    }
    configure(&connection, false)?;
    if pin.fresh {
        initialize(&connection, schema, version)?;
    }
    validate(&connection, &expected, version)?;
    pin.verify()?;
    // SQLite's own main-file handle now holds no-DELETE sharing. The init lock
    // serializes cooperating brokers; this is not a replacement for a private
    // storage protocol against concurrent trusted administrator modifications.
    Ok(connection)
}

pub(super) fn open_read_only(
    path: &Path,
    schema: &str,
    version: i64,
) -> Result<Option<Connection>> {
    let Some(pin) = PinnedMain::open(path, false)? else {
        return Ok(None);
    };
    let connection = connect(&pin.path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    validate(&connection, &expected_schema(schema)?, version)?;
    pin.verify()?;
    Ok(Some(connection))
}

#[cfg(test)]
#[path = "state_db_storage_tests.rs"]
mod tests;
