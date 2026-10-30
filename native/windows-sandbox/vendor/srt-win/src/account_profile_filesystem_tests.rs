use super::*;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};

#[path = "account_profile_filesystem_post_use_tests.rs"]
mod post_use;

fn fixture() -> PathBuf {
    static SEQUENCE: AtomicU64 = AtomicU64::new(0);
    let p = std::env::temp_dir().join(format!(
        "alysis-profile-filesystem-{}-{}",
        std::process::id(),
        SEQUENCE.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::create_dir(&p).unwrap();
    // CI can provide an 8.3 TEMP alias. Canonicalize only the exclusively
    // created synthetic fixture; production profile admission still refuses
    // aliases and never canonicalizes caller-selected paths.
    let canonical = std::fs::canonicalize(&p).unwrap();
    PathBuf::from(crate::path_id::strip_extended_prefix(
        canonical.to_str().unwrap(),
    ))
}
fn capture(p: &std::path::Path) -> RetainedProfileFilesystem {
    RetainedProfileFilesystem::capture_path(p.to_str().unwrap()).unwrap()
}

#[test]
fn account_profile_filesystem_populated_tree_detects_content_and_topology_drift() {
    let p = fixture();
    let nested = p.join("Nested");
    std::fs::create_dir(&nested).unwrap();
    let f = nested.join("synthetic.txt");
    std::fs::write(&f, b"original").unwrap();
    let guard = capture(&p);
    assert_eq!(guard.nodes.len(), 3);
    assert!(matches!(
        guard.observe().unwrap(),
        FilesystemObservation::Present
    ));
    std::fs::write(&f, b"modified").unwrap();
    assert!(guard.observe().is_err());
    std::fs::write(&f, b"original").unwrap();
    assert!(guard.observe().is_ok());
    std::fs::write(p.join("unexpected"), b"x").unwrap();
    assert!(guard.observe().is_err());
    guard.close().unwrap();
    // Bounded named fixtures only. Preserve evidence if an assertion fails.
    std::fs::remove_file(f).unwrap();
    std::fs::remove_file(p.join("unexpected")).unwrap();
    std::fs::remove_dir(nested).unwrap();
    std::fs::remove_dir(p).unwrap();
}
#[test]
fn account_profile_filesystem_original_handles_refuse_replacement() {
    let p = fixture();
    let original = p.join("original");
    std::fs::create_dir(&original).unwrap();
    std::fs::write(original.join("data"), b"same").unwrap();
    let guard = capture(&original);
    let moved = original.join("moved");
    std::fs::rename(original.join("data"), &moved).unwrap();
    std::fs::write(original.join("data"), b"same").unwrap();
    assert!(guard.observe().is_err());
    assert_eq!(std::fs::read(original.join("data")).unwrap(), b"same");
    guard.close().unwrap();
    std::fs::remove_file(original.join("data")).unwrap();
    std::fs::remove_file(moved).unwrap();
    std::fs::remove_dir(original).unwrap();
    std::fs::remove_dir(p).unwrap();
}
#[test]
fn account_profile_filesystem_retained_descendant_blocks_parent_rename() {
    let p = fixture();
    let original = p.join("original");
    std::fs::create_dir(&original).unwrap();
    std::fs::write(original.join("data"), b"synthetic").unwrap();
    let guard = capture(&original);
    let moved = p.join("moved");
    assert_eq!(
        std::fs::rename(&original, &moved)
            .unwrap_err()
            .raw_os_error(),
        Some(5)
    );
    assert!(guard.observe().is_ok());
    guard.close().unwrap();
    // Positive control: the same operation succeeds after our handles close.
    std::fs::rename(&original, &moved).unwrap();
    std::fs::remove_file(moved.join("data")).unwrap();
    std::fs::remove_dir(moved).unwrap();
    std::fs::remove_dir(p).unwrap();
}
#[test]
fn account_profile_filesystem_hardlink_and_alternate_stream_are_refused() {
    let p = fixture();
    let f = p.join("data");
    std::fs::write(&f, b"same").unwrap();
    let guard = capture(&p);
    std::fs::hard_link(&f, p.join("alias")).unwrap();
    assert!(guard.observe().is_err());
    assert!(RetainedProfileFilesystem::capture_path(p.to_str().unwrap()).is_err());
    std::fs::remove_file(p.join("alias")).unwrap();
    std::fs::write(p.join("data:synthetic"), b"stream").unwrap();
    assert!(guard.observe().is_err());
    assert!(RetainedProfileFilesystem::capture_path(p.to_str().unwrap()).is_err());
    guard.close().unwrap();
    std::fs::remove_file(f).unwrap();
    std::fs::remove_dir(p).unwrap();
}
#[test]
fn account_profile_filesystem_partial_unlink_is_not_complete_deletion() {
    let p = fixture();
    let f = p.join("data");
    std::fs::write(&f, b"same").unwrap();
    let guard = capture(&p);
    std::fs::remove_file(&f).unwrap();
    assert!(guard.observe().is_err());
    guard.close().unwrap();
    std::fs::remove_dir(p).unwrap();
}
#[test]
fn account_profile_filesystem_junction_does_not_read_outside_target() {
    let p = fixture();
    let root = p.join("root");
    let outside = p.join("outside");
    std::fs::create_dir(&root).unwrap();
    std::fs::create_dir(&outside).unwrap();
    std::fs::write(outside.join("canary"), b"unchanged").unwrap();
    let guard = capture(&root);
    let link = root.join("redirect");
    let mut system = vec![0u16; 32768];
    let len = unsafe {
        windows::Win32::System::SystemInformation::GetSystemDirectoryW(Some(&mut system))
    } as usize;
    assert!(len > 0 && len < system.len());
    let cmd = PathBuf::from(String::from_utf16(&system[..len]).unwrap()).join("cmd.exe");
    let output = std::process::Command::new(cmd)
        .args(["/d", "/c", "mklink", "/J"])
        .arg(&link)
        .arg(&outside)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "synthetic junction creation failed"
    );
    assert!(guard.observe().is_err());
    assert!(RetainedProfileFilesystem::capture_path(root.to_str().unwrap()).is_err());
    assert_eq!(std::fs::read(outside.join("canary")).unwrap(), b"unchanged");
    guard.close().unwrap();
    // Remove the junction itself only; never traverse it recursively.
    std::fs::remove_dir(link).unwrap();
    std::fs::remove_dir(root).unwrap();
    std::fs::remove_file(outside.join("canary")).unwrap();
    std::fs::remove_dir(outside).unwrap();
    std::fs::remove_dir(p).unwrap();
}
#[test]
fn account_profile_filesystem_bounds_and_binary_extents_fail_closed() {
    for bad in [
        "", ".", "..", "a:b", "a/b", "a\\b", "a\0b", "tail.", "tail ",
    ] {
        assert!(component(&bad.encode_utf16().collect::<Vec<_>>()).is_err());
    }
    assert!(component(&[0xd800]).is_err());
    assert!(component(&vec![65; MAX_NAME + 1]).is_err());
    for bad in [
        "relative",
        "C:\\",
        "\\\\server\\share",
        "C:\\a\\..\\b",
        "C:\\a\\",
        "C:/a",
    ] {
        assert!(local_components(bad).is_err());
    }
    assert!(directory_entry(&[0; 11]).is_err());
    let mut entry = vec![0; 14];
    entry[8] = 2;
    entry[12] = 65;
    assert_eq!(directory_entry(&entry).unwrap(), [65]);
    entry[0] = 8;
    assert!(directory_entry(&entry).is_err());
    entry[0] = 0;
    entry[8] = 3;
    assert!(directory_entry(&entry).is_err());
    entry[8] = 4;
    assert!(directory_entry(&entry).is_err());
    assert!(streams(&[], true, 0).is_ok());
    assert!(streams(&[], false, 0).is_err());
    let mut stream = vec![0; 38];
    stream[4] = 14;
    stream[24..].copy_from_slice(
        &"::$DATA"
            .encode_utf16()
            .flat_map(u16::to_le_bytes)
            .collect::<Vec<_>>(),
    );
    assert!(streams(&stream, false, 0).is_ok());
    assert!(streams(&stream, true, 0).is_err());
    stream[0] = 24;
    assert!(streams(&stream, false, 0).is_err());
    stream[0] = 0;
    stream[24] = b'x';
    assert!(streams(&stream, false, 0).is_err());
    let p = fixture();
    std::fs::write(p.join("large"), vec![0; MAX_QUERY + 1]).unwrap();
    let error = RetainedProfileFilesystem::capture_path(p.to_str().unwrap())
        .err()
        .expect("large file admitted");
    assert!(format!("{error:#}").contains("profile file data bound exceeded"));
    std::fs::remove_file(p.join("large")).unwrap();
    std::fs::remove_dir(p).unwrap();
}
#[test]
fn account_profile_filesystem_failed_close_is_consumed_and_actor_checked_first() {
    let mut raw = Some(HANDLE(123usize as *mut _));
    let mut calls = 0;
    assert!(
        close_once(&mut raw, |_| {
            calls += 1;
            anyhow::bail!("synthetic close uncertainty")
        })
        .is_err()
    );
    close_once(&mut raw, |_| {
        calls += 1;
        Ok(())
    })
    .unwrap();
    assert_eq!(calls, 1);
    assert!(
        RetainedProfileFilesystem::open(
            "Alysis.Execution.00000000000000000000000000000000",
            "S-1-5-18"
        )
        .is_err()
    );
}
#[test]
fn account_profile_filesystem_security_drift_is_refused() {
    let p = fixture();
    let f = p.join("data");
    std::fs::write(&f, b"same").unwrap();
    let guard = capture(&p);
    let actor = crate::sid::current_user_sid().unwrap();
    let sd = crate::util::OwnedSd::from_sddl(&format!("D:P(A;;FA;;;{actor})(A;;FR;;;SY)")).unwrap();
    let name = crate::util::wstr(f.to_str().unwrap());
    unsafe {
        SetFileSecurityW(
            PCWSTR(name.as_ptr()),
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            sd.ptr,
        )
    }
    .unwrap();
    assert!(guard.observe().is_err());
    guard.close().unwrap();
    std::fs::remove_file(f).unwrap();
    std::fs::remove_dir(p).unwrap();
}
#[test]
fn account_profile_filesystem_depth_and_total_bytes_are_bounded() {
    let p = fixture();
    let mut dirs = Vec::new();
    let mut path = p.clone();
    for _ in 0..=MAX_DEPTH {
        path = path.join("nested");
        std::fs::create_dir(&path).unwrap();
        dirs.push(path.clone());
    }
    let error = RetainedProfileFilesystem::capture_path(p.to_str().unwrap())
        .err()
        .expect("deep tree admitted");
    assert!(format!("{error:#}").contains("profile filesystem node/depth bound exceeded"));
    for dir in dirs.into_iter().rev() {
        std::fs::remove_dir(dir).unwrap();
    }
    std::fs::remove_dir(p).unwrap();
    let snapshot = Snapshot {
        metadata: Metadata {
            id: [1; 24],
            name: vec![65],
            security: vec![],
            directory: false,
            attributes: 0,
        },
        children: vec![],
        data: vec![0; MAX_QUERY],
    };
    let mut used = MAX_BYTES - 1;
    assert!(charge(&mut used, &snapshot).is_err());
    let mut used = usize::MAX;
    assert!(charge(&mut used, &snapshot).is_err());
}

#[test]
fn account_profile_filesystem_parent_absence_refuses_present_and_replacement() {
    let p = fixture();
    let root = p.join("package");
    std::fs::create_dir(&root).unwrap();
    std::fs::write(root.join("data"), b"synthetic").unwrap();
    let parent = capture(&root).close_tree_for_observation().unwrap();
    assert!(parent.observe_absent().is_err());
    std::fs::remove_file(root.join("data")).unwrap();
    std::fs::remove_dir(&root).unwrap();
    parent.observe_absent().unwrap();
    std::fs::create_dir(&root).unwrap();
    assert!(parent.observe_absent().is_err());
    std::fs::remove_dir(&root).unwrap();
    std::fs::write(&root, b"replacement file").unwrap();
    assert!(parent.observe_absent().is_err());
    std::fs::remove_file(&root).unwrap();
    parent.observe_absent().unwrap();
    parent.close().unwrap();
    std::fs::remove_dir(p).unwrap();
}

#[test]
fn account_profile_filesystem_parent_absence_refuses_renamed_ancestor() {
    let p = fixture();
    let root = p.join("package");
    std::fs::create_dir(&root).unwrap();
    let parent = capture(&root).close_tree_for_observation().unwrap();
    std::fs::remove_dir(&root).unwrap();
    parent.observe_absent().unwrap();
    let moved = p.with_extension("moved");
    std::fs::rename(&p, &moved).unwrap();
    std::fs::create_dir(&p).unwrap();
    // Both paths have an absent final component, but the original ancestor
    // moved and a new directory now occupies its name. Neither is completion.
    assert!(parent.observe_absent().is_err());
    parent.close().unwrap();
    std::fs::remove_dir(p).unwrap();
    std::fs::remove_dir(moved).unwrap();
}

#[test]
fn account_profile_filesystem_tree_drift_refuses_parent_transfer() {
    let p = fixture();
    std::fs::write(p.join("data"), b"before").unwrap();
    let tree = capture(&p);
    std::fs::write(p.join("data"), b"after").unwrap();
    assert!(tree.close_tree_for_observation().is_err());
    std::fs::remove_file(p.join("data")).unwrap();
    std::fs::remove_dir(p).unwrap();
}

#[test]
fn account_profile_filesystem_absence_requires_exact_missing_leaf_status() {
    assert!(exact_child_absence(NTSTATUS(0xc0000034u32 as i32)));
    for status in [
        0u32, 0x103, 0x80000005, 0xc000003a, 0xc0000022, 0xc0000056, 0xc000050b, 0xc0000001,
    ] {
        assert!(!exact_child_absence(NTSTATUS(status as i32)));
    }
}
