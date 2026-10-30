use super::*;
use std::cell::RefCell;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use windows::Win32::Foundation::ERROR_SHARING_VIOLATION;
use windows::Win32::Storage::FileSystem::FILE_SHARE_DELETE;
use windows::Win32::System::IO::DeviceIoControl;

static NEXT_FIXTURE: AtomicU64 = AtomicU64::new(0);

fn fixture() -> PathBuf {
    let root = crate::util::test_temp_dir().join(format!(
        "alysis-placeholder-mxc-{}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos(),
        NEXT_FIXTURE.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::create_dir(&root).unwrap();
    root
}

fn sharing_denied(result: std::io::Result<()>) {
    let error = result.expect_err("the retained object must remain protected");
    assert_eq!(error.raw_os_error(), Some(32), "{error}");
}

fn open_access_denied(path: &Path, access: u32) {
    let result = unsafe {
        CreateFileW(
            pcwstr(&wstr(path.to_str().unwrap())),
            access,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
    };
    match result {
        Ok(handle) => {
            drop(OwnedHandle(handle));
            panic!("unexpected access {access:#x} to retained object");
        }
        Err(error) => assert_eq!(error.code(), ERROR_SHARING_VIOLATION.to_hresult()),
    }
}

#[test]
fn mxc_writable_ancestor_allows_child_rename_but_stays_pinned() {
    let root = fixture();
    let workspace = root.join("workspace");
    let leaf = workspace.join("protected.txt");
    let expected_workspace = std::fs::canonicalize(&root).unwrap().join("workspace");
    let admitted = RefCell::new(Vec::new());
    let mut created = Vec::new();
    let chain = create_retained_with_writable_ancestors(
        leaf.to_str().unwrap(),
        false,
        |path| {
            created.push(path.to_owned());
            Ok(())
        },
        |path| {
            admitted.borrow_mut().push(path.to_owned());
            path == expected_workspace.to_str().unwrap()
        },
    )
    .unwrap();
    assert_eq!(created.len(), 2, "exercise directory and leaf creation");
    assert!(
        admitted
            .borrow()
            .iter()
            .any(|path| path == expected_workspace.to_str().unwrap())
    );
    assert!(
        !admitted
            .borrow()
            .iter()
            .any(|path| path.ends_with("protected.txt"))
    );
    let original = workspace.join("ordinary.txt");
    let renamed = workspace.join("renamed.txt");
    std::fs::write(&original, "ordinary").unwrap();
    std::fs::rename(&original, &renamed).unwrap();
    assert_eq!(std::fs::read_to_string(&renamed).unwrap(), "ordinary");
    sharing_denied(std::fs::rename(&workspace, root.join("moved")));
    // DELETE access is denied by sharing, independently of a nonempty-directory
    // failure. This proves retaining no-delete sharing still pins the ancestor.
    open_access_denied(&workspace, 0x10000);
    chain.verify().unwrap();
    drop(chain);
    std::fs::remove_file(&renamed).unwrap();
    std::fs::remove_file(&leaf).unwrap();
    std::fs::remove_dir(&workspace).unwrap();
    std::fs::remove_dir(&root).unwrap();
}

#[test]
fn mxc_opt_in_never_relaxes_protected_file_leaf() {
    let root = fixture();
    let leaf = root.join("protected.txt");
    let replacement = root.join("replacement.txt");
    std::fs::write(&leaf, "protected").unwrap();
    std::fs::write(&replacement, "replacement").unwrap();
    let chain = create_retained_with_writable_ancestors(
        leaf.to_str().unwrap(),
        false,
        |_| Ok(()),
        |_| true,
    )
    .unwrap();
    sharing_denied(std::fs::write(&leaf, "changed"));
    // Replacing an existing open file can report ACCESS_DENIED instead of
    // SHARING_VIOLATION. Both must leave the original and replacement intact.
    let replacement_error = std::fs::rename(&replacement, &leaf).unwrap_err();
    assert!(matches!(replacement_error.raw_os_error(), Some(5 | 32)));
    sharing_denied(std::fs::rename(&leaf, root.join("moved.txt")));
    sharing_denied(std::fs::remove_file(&leaf));
    assert_eq!(std::fs::read_to_string(&leaf).unwrap(), "protected");
    assert_eq!(
        std::fs::read_to_string(&replacement).unwrap(),
        "replacement"
    );
    chain.verify().unwrap();
    drop(chain);
    std::fs::remove_file(&replacement).unwrap();
    std::fs::remove_file(&leaf).unwrap();
    std::fs::remove_dir(&root).unwrap();
}

#[test]
fn mxc_opt_in_never_relaxes_protected_directory_leaf() {
    let root = fixture();
    let leaf = root.join("protected");
    let seen = RefCell::new(Vec::new());
    let chain = create_retained_with_writable_ancestors(
        leaf.to_str().unwrap(),
        true,
        |_| Ok(()),
        |path| {
            seen.borrow_mut().push(path.to_owned());
            true
        },
    )
    .unwrap();
    assert!(!seen.borrow().iter().any(|path| path == chain.canonical()));
    let original = leaf.join("ordinary.txt");
    std::fs::write(&original, "ordinary").unwrap();
    sharing_denied(std::fs::rename(&original, leaf.join("renamed.txt")));
    open_access_denied(&leaf, 2); // FILE_ADD_FILE / FILE_WRITE_DATA
    sharing_denied(std::fs::rename(&leaf, root.join("moved")));
    chain.verify().unwrap();
    drop(chain);
    std::fs::remove_file(&original).unwrap();
    std::fs::remove_dir(&leaf).unwrap();
    std::fs::remove_dir(&root).unwrap();
}

#[test]
fn mxc_default_and_unadmitted_ancestors_remain_strict() {
    for explicitly_refuse in [false, true] {
        let root = fixture();
        let leaf = root.join("protected.txt");
        let chain = if explicitly_refuse {
            create_retained_with_writable_ancestors(
                leaf.to_str().unwrap(),
                false,
                |_| Ok(()),
                |_| false,
            )
        } else {
            create_retained(leaf.to_str().unwrap(), false, |_| Ok(()))
        }
        .unwrap();
        let original = root.join("ordinary.txt");
        let renamed = root.join("renamed.txt");
        std::fs::write(&original, "ordinary").unwrap();
        sharing_denied(std::fs::rename(&original, &renamed));
        chain.verify().unwrap();
        drop(chain);
        // A host positive control proves failure came from the retained pins.
        std::fs::rename(&original, &renamed).unwrap();
        std::fs::remove_file(&renamed).unwrap();
        std::fs::remove_file(&leaf).unwrap();
        std::fs::remove_dir(&root).unwrap();
    }
}

// Reproduce metadata-only mutation without administrator/symlink privileges.
// As in the strict-pin regression, WRITE_ATTRIBUTES is deliberately different
// from generic write access: sharing alone is not the reparse defense.
fn metadata_junction(link: &Path, target: &Path) {
    let writer = OwnedHandle(unsafe {
        CreateFileW(
            pcwstr(&wstr(link.to_str().unwrap())),
            0x100,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        .unwrap()
    });
    let text = crate::path_id::strip_extended_prefix(target.to_str().unwrap());
    let substitute = wstr(&format!(r"\??\{text}"));
    let printable = wstr(text);
    let payload = 8 + 2 * (substitute.len() + printable.len());
    let mut buffer = Vec::new();
    buffer.extend_from_slice(&0xa0000003u32.to_le_bytes());
    buffer.extend_from_slice(&(payload as u16).to_le_bytes());
    buffer.extend_from_slice(&0u16.to_le_bytes());
    for value in [
        0,
        ((substitute.len() - 1) * 2) as u16,
        (substitute.len() * 2) as u16,
        ((printable.len() - 1) * 2) as u16,
    ] {
        buffer.extend_from_slice(&value.to_le_bytes());
    }
    for value in substitute.into_iter().chain(printable) {
        buffer.extend_from_slice(&value.to_le_bytes());
    }
    let mut returned = 0;
    unsafe {
        DeviceIoControl(
            writer.raw(),
            0x900a4,
            Some(buffer.as_ptr().cast()),
            buffer.len() as u32,
            None,
            0,
            Some(&mut returned),
            None,
        )
        .unwrap();
    }
}

#[test]
fn mxc_writable_ancestor_metadata_substitution_refuses_without_outside_effect() {
    let root = fixture();
    let outside = root.join("outside");
    std::fs::create_dir(&outside).unwrap();
    let parent = root.join("parent");
    let target = parent.join("missing");
    let mut intents = Vec::new();
    let result = create_retained_with_writable_ancestors(
        target.to_str().unwrap(),
        false,
        |path| {
            intents.push(path.to_owned());
            if intents.len() == 2 {
                metadata_junction(&parent, &outside);
            }
            Ok(())
        },
        |_| true,
    );
    let error = result
        .err()
        .expect("mutated writable ancestor must be refused");
    assert!(error.to_string().contains("reparse"), "{error:#}");
    assert_eq!(intents.len(), 2);
    assert_eq!(std::fs::read_dir(&outside).unwrap().count(), 0);
    // Remove the synthetic junction itself, never recurse through its target.
    std::fs::remove_dir(&parent).unwrap();
    std::fs::remove_dir(&outside).unwrap();
    std::fs::remove_dir(&root).unwrap();
}

#[test]
fn mxc_opt_in_cannot_admit_a_file_as_an_intermediate_directory() {
    let root = fixture();
    let file = root.join("file");
    std::fs::write(&file, "unchanged").unwrap();
    let mut recorded = false;
    let result = create_retained_with_writable_ancestors(
        file.join("missing").to_str().unwrap(),
        false,
        |_| {
            recorded = true;
            Ok(())
        },
        |_| true,
    );
    assert!(result.is_err());
    assert!(!recorded);
    assert_eq!(std::fs::read_to_string(&file).unwrap(), "unchanged");
    std::fs::remove_file(&file).unwrap();
    std::fs::remove_dir(&root).unwrap();
}
