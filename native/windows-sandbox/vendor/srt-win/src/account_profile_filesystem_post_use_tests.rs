use super::*;

#[test]
fn account_profile_post_use_filesystem_recaptures_contents_and_freezes_new_inventory() {
    let p = fixture();
    std::fs::write(p.join("old"), b"before").unwrap();
    let tree = capture(&p);
    let id = tree.original_root_id().unwrap();
    let root = tree.retain_root_for_use().unwrap();
    std::fs::write(p.join("old"), b"edited").unwrap();
    std::fs::remove_file(p.join("old")).unwrap();
    std::fs::create_dir(p.join("cache")).unwrap();
    let data = p.join("cache").join("data");
    std::fs::write(&data, b"after").unwrap();
    let updated = root.capture_after_use().unwrap();
    assert_eq!(updated.original_root_id().unwrap(), id);
    assert_eq!(updated.nodes.len(), 3);
    assert!(updated.observe().is_ok());
    std::fs::write(&data, b"late mutation").unwrap();
    assert!(updated.observe().is_err());
    std::fs::write(&data, b"after").unwrap();
    let parent = updated.close_tree_for_observation().unwrap();
    assert!(parent.observe_absent().is_err());
    std::fs::remove_file(data).unwrap();
    std::fs::remove_dir(p.join("cache")).unwrap();
    std::fs::remove_dir(&p).unwrap();
    parent.observe_absent().unwrap();
    parent.close().unwrap();
}

#[test]
fn account_profile_post_use_filesystem_never_adopts_replaced_or_deleted_root() {
    for deleted in [false, true] {
        let p = fixture();
        let path = p.join("root");
        std::fs::create_dir(&path).unwrap();
        let root = capture(&path).retain_root_for_use().unwrap();
        let moved = p.join("moved");
        if deleted {
            std::fs::remove_dir(&path).unwrap();
        } else {
            std::fs::rename(&path, &moved).unwrap();
        }
        // A deleted directory's name may remain unavailable until its original
        // handle closes. Neither state can become a newly captured root.
        let replacement = std::fs::create_dir(&path).is_ok();
        assert!(root.capture_after_use().is_err());
        if replacement {
            std::fs::remove_dir(path).unwrap();
        }
        if !deleted {
            std::fs::remove_dir(moved).unwrap();
        }
        std::fs::remove_dir(p).unwrap();
    }
}

#[test]
fn account_profile_post_use_filesystem_refuses_root_and_ancestor_security_changes() {
    for ancestor in [false, true] {
        let p = fixture();
        let path = p.join("root");
        std::fs::create_dir(&path).unwrap();
        let root = capture(&path).retain_root_for_use().unwrap();
        let actor = crate::sid::current_user_sid().unwrap();
        let sd =
            crate::util::OwnedSd::from_sddl(&format!("D:P(A;;FA;;;{actor})(A;;FR;;;SY)")).unwrap();
        let changed = crate::util::wstr(if ancestor {
            p.to_str().unwrap()
        } else {
            path.to_str().unwrap()
        });
        unsafe {
            SetFileSecurityW(
                PCWSTR(changed.as_ptr()),
                DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                sd.ptr,
            )
        }
        .unwrap();
        assert!(root.capture_after_use().is_err());
        std::fs::remove_dir(path).unwrap();
        std::fs::remove_dir(p).unwrap();
    }
}

#[test]
fn account_profile_post_use_filesystem_retains_link_stream_and_size_refusals() {
    for kind in ["hardlink", "stream", "size", "junction"] {
        let p = fixture();
        let path = p.join("root");
        std::fs::create_dir(&path).unwrap();
        let root = capture(&path).retain_root_for_use().unwrap();
        let outside = p.join("outside");
        std::fs::create_dir(&outside).unwrap();
        std::fs::write(outside.join("canary"), b"untouched").unwrap();
        let data = path.join("data");
        match kind {
            "hardlink" => std::fs::hard_link(outside.join("canary"), &data).unwrap(),
            "stream" => {
                std::fs::write(&data, b"data").unwrap();
                std::fs::write(path.join("data:hidden"), b"stream").unwrap();
            }
            "size" => std::fs::write(&data, vec![0; MAX_QUERY + 1]).unwrap(),
            "junction" => {
                let mut system = vec![0u16; 32768];
                let n = unsafe {
                    windows::Win32::System::SystemInformation::GetSystemDirectoryW(Some(
                        &mut system,
                    ))
                } as usize;
                assert!(n > 0 && n < system.len());
                let cmd = PathBuf::from(String::from_utf16(&system[..n]).unwrap()).join("cmd.exe");
                assert!(
                    std::process::Command::new(cmd)
                        .args(["/d", "/c", "mklink", "/J"])
                        .arg(&data)
                        .arg(&outside)
                        .output()
                        .unwrap()
                        .status
                        .success()
                );
            }
            _ => unreachable!(),
        }
        assert!(root.capture_after_use().is_err());
        assert_eq!(std::fs::read(outside.join("canary")).unwrap(), b"untouched");
        if kind == "junction" {
            std::fs::remove_dir(data).unwrap();
        } else {
            std::fs::remove_file(data).unwrap();
        }
        std::fs::remove_file(outside.join("canary")).unwrap();
        std::fs::remove_dir(outside).unwrap();
        std::fs::remove_dir(path).unwrap();
        std::fs::remove_dir(p).unwrap();
    }
}

#[test]
fn account_profile_post_use_filesystem_requires_unchanged_pre_use_inventory() {
    let p = fixture();
    let tree = capture(&p);
    std::fs::write(p.join("late"), b"must be refused").unwrap();
    assert!(tree.retain_root_for_use().is_err());
    std::fs::remove_file(p.join("late")).unwrap();
    std::fs::remove_dir(p).unwrap();
}
