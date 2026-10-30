use super::*;
use crate::util::{pcwstr, win32_ok, wstr};
use std::path::PathBuf;
use windows::Win32::{
    Foundation::{HANDLE_FLAGS, SetHandleInformation},
    Security::{
        Authorization::{SE_FILE_OBJECT, SetNamedSecurityInfoW},
        DACL_SECURITY_INFORMATION, GetSecurityDescriptorDacl, PROTECTED_DACL_SECURITY_INFORMATION,
    },
};

fn fixture() -> (PathBuf, PathBuf) {
    let root = crate::util::test_temp_dir().join(format!(
        "runner-image-{}",
        crate::account_state::execution::random_id().unwrap()
    ));
    std::fs::create_dir(&root).unwrap();
    let image = root.join("fixture.exe");
    // Synthetic bytes never executed. Process-query tests use the real test
    // image read-only, and never change its content or permissions.
    std::fs::write(&image, b"MZsynthetic-image-not-for-execution").unwrap();
    protect(&image, "(A;;FRFX;;;WD)");
    (root, image)
}

fn protect(path: &Path, extra: &str) {
    let owner = crate::sid::current_user_sid().unwrap();
    let descriptor =
        OwnedSd::from_sddl(&format!("D:P(A;;FA;;;{owner})(A;;FA;;;SY){extra}")).unwrap();
    let mut dacl = std::ptr::null_mut();
    let mut present = false.into();
    let mut defaulted = false.into();
    unsafe {
        GetSecurityDescriptorDacl(descriptor.ptr, &mut present, &mut dacl, &mut defaulted).unwrap();
    }
    assert!(present.as_bool() && !dacl.is_null());
    win32_ok(
        unsafe {
            SetNamedSecurityInfoW(
                pcwstr(&wstr(&fs::extended_path(path).unwrap())),
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                Some(dacl),
                None,
            )
        },
        "set disposable image fixture DACL",
    )
    .unwrap();
}

#[test]
fn runner_image_pins_file_and_full_parent_chain_against_replacement() {
    let (root, path) = fixture();
    let pin = PinnedRunnerImage::fixture(&path).unwrap();
    pin.verify().unwrap();
    assert!(std::fs::write(&path, b"replacement").is_err());
    assert!(std::fs::rename(&path, path.with_file_name("moved.exe")).is_err());
    assert!(std::fs::remove_file(&path).is_err());
    let moved = root.with_extension("moved");
    assert!(std::fs::rename(&root, &moved).is_err());
    assert_eq!(
        std::fs::read(&path).unwrap(),
        b"MZsynthetic-image-not-for-execution"
    );
    pin.verify().unwrap();
    drop(pin);
    std::fs::rename(&root, &moved).unwrap();
}

#[test]
fn runner_image_refuses_untrusted_mutation_grants_before_admission() {
    let (_root, path) = fixture();
    for rights in ["GW", "GA", "WD", "WO", "SD", "0x2", "0x4", "0x10", "0x100"] {
        protect(&path, &format!("(A;;{rights};;;WD)"));
        assert!(
            PinnedRunnerImage::fixture(&path).is_err(),
            "admitted mutation {rights}"
        );
    }
    protect(&path, "(A;;FRFX;;;WD)");
    PinnedRunnerImage::fixture(&path).unwrap().verify().unwrap();
}

#[test]
fn runner_image_refuses_aliases_hardlinks_directories_and_existing_writer() {
    use std::os::windows::fs::OpenOptionsExt;
    let (root, path) = fixture();
    let writer = std::fs::OpenOptions::new()
        .write(true)
        .share_mode(1 | 2 | 4)
        .open(&path)
        .unwrap();
    assert!(PinnedRunnerImage::fixture(&path).is_err());
    drop(writer);
    for bad in [
        PathBuf::from("relative.exe"),
        PathBuf::from(format!("{}.", path.display())),
        PathBuf::from(format!("{}:stream", path.display())),
        root.clone(),
    ] {
        assert!(PinnedRunnerImage::fixture(&bad).is_err());
    }
    let link = root.join("hardlink.exe");
    std::fs::hard_link(&path, &link).unwrap();
    assert!(PinnedRunnerImage::fixture(&path).is_err());
    assert!(PinnedRunnerImage::fixture(&link).is_err());
}

#[test]
fn runner_image_observes_late_link_change_or_native_prevention() {
    let (root, path) = fixture();
    let guard = PinnedRunnerImage::fixture(&path).unwrap();
    match std::fs::hard_link(&path, root.join("late-link.exe")) {
        Ok(()) => assert!(guard.verify().is_err()),
        Err(error) => {
            assert!(
                matches!(error.raw_os_error(), Some(5 | 32)),
                "unexpected hardlink failure {error}"
            );
            guard.verify().unwrap();
        }
    }
}

#[test]
fn runner_image_refuses_actual_junction_parent_without_following_it() {
    use windows::Win32::{Storage::FileSystem::*, System::IO::DeviceIoControl};
    let (target, image) = fixture();
    let link = target.with_extension("junction");
    std::fs::create_dir(&link).unwrap();
    let handle = OwnedHandle(unsafe {
        CreateFileW(
            pcwstr(&wstr(&fs::extended_path(&link).unwrap())),
            0x40000000,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        .unwrap()
    });
    let text = target.to_str().unwrap();
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
    drop(handle);
    // Prove this is a functioning redirect, then require the guard to refuse it.
    assert_eq!(
        std::fs::read(link.join("fixture.exe")).unwrap(),
        std::fs::read(&image).unwrap()
    );
    assert!(PinnedRunnerImage::fixture(&link.join("fixture.exe")).is_err());
    assert_eq!(
        std::fs::read(&image).unwrap(),
        b"MZsynthetic-image-not-for-execution"
    );
}

#[test]
fn runner_image_observes_original_leaf_and_parent_security_drift() {
    for parent in [false, true] {
        let (root, path) = fixture();
        let guard = PinnedRunnerImage::fixture(&path).unwrap();
        protect(if parent { &root } else { &path }, "(A;;FR;;;WD)");
        assert!(guard.verify().is_err());
    }
}

#[test]
fn runner_image_binds_original_process_path() {
    let path = std::env::current_exe().unwrap();
    let image = PinnedRunnerImage::fixture(&path).unwrap();
    image
        .verify_process(unsafe { GetCurrentProcess() })
        .unwrap();
    assert!(image.verify_process(HANDLE::default()).is_err());
    let (_root, foreign) = fixture();
    assert!(
        PinnedRunnerImage::fixture(&foreign)
            .unwrap()
            .verify_process(unsafe { GetCurrentProcess() })
            .is_err()
    );
}

#[test]
fn runner_image_handle_drift_permanently_refuses_control_channel() {
    use crate::account_control::{ControlChannel, ControlServer, ExpectedPeer};
    use std::time::Duration;
    let image = PinnedRunnerImage::fixture(&std::env::current_exe().unwrap()).unwrap();
    let (server, address) = ControlServer::prepare("S-1-5-21-1-2-3-9999").unwrap();
    let peer = || ExpectedPeer::retain(unsafe { GetCurrentProcess() }).unwrap();
    let _client = ControlChannel::connect(&address, peer()).unwrap();
    let mut server = server
        .accept(peer().with_image(&image).unwrap(), Duration::from_secs(1))
        .unwrap();
    unsafe {
        SetHandleInformation(
            image.image.handle.raw(),
            HANDLE_FLAG_INHERIT.0,
            HANDLE_FLAG_INHERIT,
        )
        .unwrap();
    }
    assert!(
        server
            .send(b"must-not-send", Duration::from_secs(1))
            .is_err()
    );
    unsafe {
        SetHandleInformation(
            image.image.handle.raw(),
            HANDLE_FLAG_INHERIT.0,
            HANDLE_FLAGS(0),
        )
        .unwrap();
    }
    image.verify().unwrap();
    assert!(server.send(b"restored", Duration::from_secs(1)).is_err());
    assert!(server.receive(Duration::from_secs(1)).is_err());
}
