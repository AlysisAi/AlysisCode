use super::*;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use windows::Win32::Storage::FileSystem::FILE_SHARE_DELETE;
use windows::Win32::System::IO::DeviceIoControl;

// Windows wall-clock timestamps can repeat across parallel tests. Keep each
// fixture exclusive even within one clock tick; never reuse an existing root.
static NEXT_FIXTURE: AtomicU64 = AtomicU64::new(0);

fn fixture() -> PathBuf {
    let path = crate::util::test_temp_dir().join(format!(
        "alysis-placeholder-metadata-{}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos(),
        NEXT_FIXTURE.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::create_dir(&path).unwrap();
    path
}

// FILE_WRITE_ATTRIBUTES participates differently from a generic write open.
// This must succeed while the original read-only sharing pin is still held,
// so the regression tests the reparse/identity checks rather than share denial.
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
fn placeholder_metadata_mutation_after_intent_refuses_without_outside_effect() {
    let root = fixture();
    let outside = root.join("outside");
    std::fs::create_dir(&outside).unwrap();
    let parent = root.join("parent");
    let target = parent.join("missing");
    let mut intents = Vec::new();
    let result = create_retained(target.to_str().unwrap(), false, |p| {
        intents.push(p.to_owned());
        if intents.len() == 2 {
            metadata_junction(&parent, &outside);
        }
        Ok(())
    });
    let error = result.err().expect("mutated parent must be refused");
    assert!(error.to_string().contains("reparse"), "{error:#}");
    assert_eq!(intents.len(), 2);
    assert_eq!(std::fs::read_dir(&outside).unwrap().count(), 0);
    // Remove only the synthetic junction itself, never walk through it.
    std::fs::remove_dir(&parent).unwrap();
    std::fs::remove_dir(&outside).unwrap();
    std::fs::remove_dir(&root).unwrap();
}

#[test]
fn placeholder_metadata_mutation_after_verification_cannot_redirect_relative_birth() {
    let root = fixture();
    let outside = root.join("outside");
    std::fs::create_dir(&outside).unwrap();
    let parent = root.join("parent");
    let chain = create_retained(parent.to_str().unwrap(), true, |_| Ok(())).unwrap();
    let pin = chain.pins.last().unwrap();
    pin.verify().unwrap();
    // Deterministically mutate after the last user-mode check and before the
    // actual relative NtCreateFile operation; the parent handle is unchanged.
    metadata_junction(&parent, &outside);
    let result = child(pin, "missing", Some(false), false);
    assert!(
        result.is_err(),
        "relative creation accepted a reparse parent"
    );
    assert!(chain.verify().is_err());
    assert_eq!(std::fs::read_dir(&outside).unwrap().count(), 0);
    drop(chain);
    std::fs::remove_dir(&parent).unwrap();
    std::fs::remove_dir(&outside).unwrap();
    std::fs::remove_dir(&root).unwrap();
}

#[test]
fn placeholder_metadata_mutation_of_retained_leaf_refuses_acl_admission() {
    let root = fixture();
    let outside = root.join("outside");
    std::fs::create_dir(&outside).unwrap();
    let leaf = root.join("leaf");
    let chain = create_retained(leaf.to_str().unwrap(), true, |_| Ok(())).unwrap();
    chain.verify().unwrap();
    metadata_junction(&leaf, &outside);
    assert!(
        chain.verify().is_err(),
        "ACL admission must refuse changed leaf"
    );
    assert_eq!(std::fs::read_dir(&outside).unwrap().count(), 0);
    drop(chain);
    std::fs::remove_dir(&leaf).unwrap();
    std::fs::remove_dir(&outside).unwrap();
    std::fs::remove_dir(&root).unwrap();
}
