use super::*;
use std::path::{Path, PathBuf};

fn fixture() -> PathBuf {
    let root = crate::util::test_temp_dir().join(format!(
        "srt-placeholder-race-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir(&root).unwrap();
    root
}

// An ordinary local NTFS junction needs no symlink privilege. Only synthetic
// fixture directories are used; no external process or recursive cleanup.
fn junction(link: &Path, target: &Path) {
    use windows::Win32::{
        Storage::FileSystem::FILE_FLAG_OPEN_REPARSE_POINT, System::IO::DeviceIoControl,
    };
    std::fs::create_dir(link).unwrap();
    let handle = OwnedHandle(unsafe {
        CreateFileW(
            pcwstr(&wstr(link.to_str().unwrap())),
            0x40000000,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        .unwrap()
    });
    let text = strip_extended_prefix(target.to_str().unwrap());
    let substitute = wstr(&format!(r"\??\{text}"));
    let print = wstr(text);
    let payload = 8 + 2 * (substitute.len() + print.len());
    let mut buffer = Vec::new();
    buffer.extend_from_slice(&0xa0000003u32.to_le_bytes());
    buffer.extend_from_slice(&(payload as u16).to_le_bytes());
    buffer.extend_from_slice(&0u16.to_le_bytes());
    for value in [
        0,
        ((substitute.len() - 1) * 2) as u16,
        (substitute.len() * 2) as u16,
        ((print.len() - 1) * 2) as u16,
    ] {
        buffer.extend_from_slice(&value.to_le_bytes());
    }
    for value in substitute.into_iter().chain(print) {
        buffer.extend_from_slice(&value.to_le_bytes());
    }
    let mut returned = 0;
    unsafe {
        DeviceIoControl(
            handle.raw(),
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
fn placeholder_existing_junction_cannot_redirect_creation() {
    let root = fixture();
    let outside = root.join("outside");
    let link = root.join("linked");
    std::fs::create_dir(&outside).unwrap();
    junction(&link, &outside);
    let mut intents = Vec::new();
    let result = create_placeholder_chain(
        link.join("new").join("secret").to_str().unwrap(),
        false,
        |p| {
            intents.push(p.to_owned());
            Ok(())
        },
    );
    assert!(
        result.is_err(),
        "existing junction must be refused before creation; fixture {}",
        root.display()
    );
    assert!(intents.is_empty());
    assert_eq!(std::fs::read_dir(&outside).unwrap().count(), 0);
    std::fs::remove_dir(&link).unwrap();
    std::fs::remove_dir(&outside).unwrap();
    std::fs::remove_dir(&root).unwrap();
}

#[test]
fn placeholder_raced_junction_cannot_redirect_creation() {
    let root = fixture();
    let outside = root.join("outside");
    let link = root.join("linked");
    std::fs::create_dir(&outside).unwrap();
    let mut intents = Vec::new();
    let result = create_placeholder_chain(
        link.join("new").join("secret").to_str().unwrap(),
        false,
        |p| {
            intents.push(p.to_owned());
            if intents.len() == 1 {
                junction(&link, &outside);
            }
            Ok(())
        },
    );
    assert!(
        result.is_err(),
        "raced junction must be refused before creation; fixture {}",
        root.display()
    );
    assert_eq!(intents.len(), 1);
    assert_eq!(std::fs::read_dir(&outside).unwrap().count(), 0);
    std::fs::remove_dir(&link).unwrap();
    std::fs::remove_dir(&outside).unwrap();
    std::fs::remove_dir(&root).unwrap();
}

fn directory_writer(path: &Path) -> windows::core::Result<OwnedHandle> {
    use windows::Win32::Storage::FileSystem::FILE_FLAG_OPEN_REPARSE_POINT;
    unsafe {
        CreateFileW(
            pcwstr(&wstr(path.to_str().unwrap())),
            0x40000000,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        .map(OwnedHandle)
    }
}

#[test]
fn placeholder_pins_block_mutation_and_release_on_failure() {
    use windows::Win32::Foundation::ERROR_SHARING_VIOLATION;
    let root = fixture();
    let target = root.join("created").join("child");
    let root_id = capture_file_id(root.to_str().unwrap()).unwrap();
    let mut calls = 0;
    let result = create_placeholder_chain(target.to_str().unwrap(), false, |_| {
        calls += 1;
        let pinned = if calls == 1 {
            root.clone()
        } else {
            root.join("created")
        };
        assert_eq!(
            std::fs::rename(&pinned, root.with_extension("moved"))
                .unwrap_err()
                .raw_os_error(),
            Some(32)
        );
        assert_eq!(
            directory_writer(&pinned).err().unwrap().code(),
            ERROR_SHARING_VIOLATION.into()
        );
        if calls == 2 {
            bail!("injected failure while ancestry pinned");
        }
        Ok(())
    });
    assert!(result.unwrap_err().to_string().contains("injected failure"));
    assert_eq!(calls, 2);
    assert_eq!(capture_file_id(root.to_str().unwrap()).unwrap(), root_id);
    assert!(!target.exists());
    // Both pre-existing and created directories are writable after error; no
    // leaked locks, no ACL edits, and no accidental deletion of partial state.
    drop(directory_writer(&root).unwrap());
    drop(directory_writer(&root.join("created")).unwrap());
    std::fs::remove_dir(root.join("created")).unwrap();
    std::fs::remove_dir(root).unwrap();
}

#[test]
fn placeholder_existing_writer_refuses_before_intent() {
    let root = fixture();
    let writer = directory_writer(&root).unwrap();
    let target = root.join("missing");
    let mut called = false;
    assert!(
        create_placeholder_chain(target.to_str().unwrap(), false, |_| {
            called = true;
            Ok(())
        })
        .is_err()
    );
    assert!(!called && !target.exists());
    drop(writer);
    create_placeholder_chain(target.to_str().unwrap(), false, |_| Ok(())).unwrap();
    std::fs::remove_file(target).unwrap();
    std::fs::remove_dir(root).unwrap();
}

#[test]
fn placeholder_leaf_collision_preserves_bytes_and_refuses_existing_hardlink() {
    for hardlink in [false, true] {
        let root = fixture();
        let original = root.join("original");
        let target = root.join("target");
        std::fs::write(&original, b"foreign canary").unwrap();
        // Make the existing hardlink before ancestry is pinned. The pin blocks
        // std::fs::hard_link's directory-write open during the intent callback.
        if hardlink {
            std::fs::hard_link(&original, &target).unwrap();
        }
        let mut calls = 0;
        let result = create_placeholder_chain(target.to_str().unwrap(), false, |_| {
            calls += 1;
            assert!(!hardlink, "existing leaf must not record creation intent");
            std::fs::write(&target, b"foreign canary").unwrap();
            Ok(())
        });
        assert_eq!(calls, if hardlink { 0 } else { 1 });
        if hardlink {
            assert!(result.unwrap_err().to_string().contains("hardlinks"));
        } else {
            assert!(result.unwrap().1.is_empty());
        }
        assert_eq!(std::fs::read(&original).unwrap(), b"foreign canary");
        assert_eq!(std::fs::read(&target).unwrap(), b"foreign canary");
        std::fs::remove_file(&target).unwrap();
        std::fs::remove_file(&original).unwrap();
        std::fs::remove_dir(root).unwrap();
    }
}

#[test]
fn placeholder_extended_unicode_long_path_and_directory_leaf() {
    let root = fixture();
    let target = root
        .join("unicode-β")
        .join("x".repeat(120))
        .join("directory");
    let extended = format!(r"\\?\{}\", strip_extended_prefix(target.to_str().unwrap()));
    let mut calls = 0;
    let (canon, created) = create_placeholder_chain(&extended, true, |_| {
        calls += 1;
        Ok(())
    })
    .unwrap();
    assert_eq!(calls, 3);
    assert_eq!(created.len(), 3);
    assert!(created.iter().all(|x| x.is_dir));
    assert_eq!(canonicalize_path(&extended).unwrap(), (canon.clone(), true));
    let again = create_placeholder_chain(&extended, true, |_| {
        panic!("existing target must not need intent")
    })
    .unwrap();
    assert_eq!(again.0, canon);
    assert!(again.1.is_empty());
    std::fs::remove_dir(&target).unwrap();
    std::fs::remove_dir(target.parent().unwrap()).unwrap();
    std::fs::remove_dir(root.join("unicode-β")).unwrap();
    std::fs::remove_dir(root).unwrap();
}

#[test]
fn placeholder_existing_unicode_case_variant() {
    let root = fixture();
    let actual = root.join("Δ-folder");
    let requested = root.join("δ-folder").join("missing");
    std::fs::create_dir(&actual).unwrap();
    let mut intents = Vec::new();
    let (canonical, created) = create_placeholder_chain(requested.to_str().unwrap(), false, |p| {
        intents.push(p.to_owned());
        Ok(())
    })
    .unwrap();
    assert_eq!(created.len(), 1);
    assert_eq!(intents, vec![canonical.clone()]);
    assert!(canonical.contains("Δ-folder"));
    assert_eq!(
        capture_file_id(&canonical).unwrap(),
        capture_file_id(actual.join("missing").to_str().unwrap()).unwrap()
    );
    std::fs::remove_file(actual.join("missing")).unwrap();
    std::fs::remove_dir(actual).unwrap();
    std::fs::remove_dir(root).unwrap();
}
