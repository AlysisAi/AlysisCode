use super::*;

fn write(key: &RetainedKey, bytes: &[u8], kind: REG_VALUE_TYPE) {
    unsafe {
        RegSetValueExW(
            key.raw_for_inventory().unwrap(),
            windows::core::w!("value"),
            None,
            kind,
            Some(bytes),
        )
    }
    .ok()
    .unwrap();
}
fn open(f: &Fixture, name: &str) -> RetainedKey {
    f.root.child(&wide(name)).unwrap()
}
fn writable(f: &Fixture, name: &str) -> RetainedKey {
    let mut raw = HKEY::default();
    let name = crate::util::wstr(name);
    unsafe {
        RegOpenKeyExW(
            f.root.raw_for_inventory().unwrap(),
            PCWSTR(name.as_ptr()),
            None,
            KEY_ALL_ACCESS | KEY_WOW64_64KEY,
            &mut raw,
        )
    }
    .ok()
    .unwrap();
    RetainedKey::from_read_handle(raw).unwrap()
}

#[test]
fn account_profile_post_use_registry_recaptures_sdk_data_and_freezes_new_inventory() {
    let f = Fixture::new();
    let old = create(open(&f, "SDK").raw_for_inventory().unwrap(), "Old");
    write(&old, b"before", REG_BINARY);
    old.close().unwrap();
    let roots = f.capture().retain_roots_for_use().unwrap();
    remove(f.root.raw_for_inventory().unwrap(), "SDK\\Old");
    let new = create(open(&f, "SDK").raw_for_inventory().unwrap(), "New");
    write(&new, b"after", REG_BINARY);
    let tree = roots.capture_after_use().unwrap();
    assert_eq!(tree.sdk.nodes.len(), 2);
    assert!(tree.observe().is_ok());
    write(&new, b"late mutation", REG_BINARY);
    assert!(tree.observe().is_err());
    write(&new, b"after", REG_BINARY);
    let parent = tree.close_trees_for_observation().unwrap();
    new.close().unwrap();
    remove(f.root.raw_for_inventory().unwrap(), "SDK\\New");
    f.remove_leaf("SDK");
    f.remove_leaf("Mapping");
    parent.observe_absent().unwrap();
    parent.close().unwrap();
    f.finish();
}

#[test]
fn account_profile_post_use_registry_never_adopts_replaced_or_deleted_sdk_root() {
    for deleted in [false, true] {
        let f = Fixture::new();
        let roots = f.capture().retain_roots_for_use().unwrap();
        if deleted {
            f.remove_leaf("SDK");
        } else {
            unsafe {
                RegRenameKey(
                    f.root.raw_for_inventory().unwrap(),
                    windows::core::w!("SDK"),
                    windows::core::w!("Moved"),
                )
            }
            .ok()
            .unwrap();
            create(f.root.raw_for_inventory().unwrap(), "SDK")
                .close()
                .unwrap();
        }
        // Deletion can keep the same name unavailable until handles close.
        assert!(roots.capture_after_use().is_err());
        if deleted {
            create(f.root.raw_for_inventory().unwrap(), "SDK")
                .close()
                .unwrap();
        }
        f.remove_leaf("SDK");
        if !deleted {
            f.remove_leaf("Moved");
        }
        f.remove_leaf("Mapping");
        f.finish();
    }
}

#[test]
fn account_profile_post_use_registry_mapping_and_root_security_stay_immutable() {
    for kind in ["mapping", "sdk_security", "ancestor_security"] {
        let f = Fixture::new();
        let roots = f.capture().retain_roots_for_use().unwrap();
        match kind {
            "mapping" => {
                let key = writable(&f, "Mapping");
                write(&key, b"unexpected mapping mutation", REG_BINARY);
                key.close().unwrap();
            }
            _ => {
                let actor = crate::sid::current_user_sid().unwrap();
                let sd =
                    crate::util::OwnedSd::from_sddl(&format!("D:P(A;;KA;;;{actor})(A;;KR;;;SY)"))
                        .unwrap();
                let sdk;
                let raw = if kind == "sdk_security" {
                    sdk = writable(&f, "SDK");
                    sdk.raw_for_inventory().unwrap()
                } else {
                    f.root.raw_for_inventory().unwrap()
                };
                unsafe {
                    RegSetKeySecurity(
                        raw,
                        DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                        sd.ptr,
                    )
                }
                .ok()
                .unwrap();
            }
        }
        assert!(roots.capture_after_use().is_err());
        f.remove_leaf("SDK");
        f.remove_leaf("Mapping");
        f.finish();
    }
}

#[test]
fn account_profile_post_use_registry_retains_link_and_size_refusals() {
    for kind in ["link", "size"] {
        let f = Fixture::new();
        let roots = f.capture().retain_roots_for_use().unwrap();
        let added = create(open(&f, "SDK").raw_for_inventory().unwrap(), "Added");
        if kind == "link" {
            write(&added, b"unsupported link type", REG_LINK);
        } else {
            write(&added, &vec![0; MAX_DATA + 1], REG_BINARY);
        }
        assert!(roots.capture_after_use().is_err());
        added.close().unwrap();
        remove(f.root.raw_for_inventory().unwrap(), "SDK\\Added");
        f.remove_leaf("SDK");
        f.remove_leaf("Mapping");
        f.finish();
    }
}

#[test]
fn account_profile_post_use_registry_requires_unchanged_pre_use_inventory() {
    let f = Fixture::new();
    let tree = f.capture();
    let added = create(open(&f, "SDK").raw_for_inventory().unwrap(), "Added");
    assert!(tree.retain_roots_for_use().is_err());
    added.close().unwrap();
    remove(f.root.raw_for_inventory().unwrap(), "SDK\\Added");
    f.remove_leaf("SDK");
    f.remove_leaf("Mapping");
    f.finish();
}

#[test]
fn account_profile_post_use_registry_refuses_renamed_ancestor_with_cached_sdk_name() {
    let f = Fixture::new();
    let roots = f.capture().retain_roots_for_use().unwrap();
    let moved = format!("{}-moved", f.name);
    let name = crate::util::wstr(&moved);
    unsafe {
        RegRenameKey(
            f.root.raw_for_inventory().unwrap(),
            PCWSTR::null(),
            PCWSTR(name.as_ptr()),
        )
    }
    .ok()
    .unwrap();
    create(f.base.raw_for_inventory().unwrap(), &f.name)
        .close()
        .unwrap();
    assert!(roots.capture_after_use().is_err());
    f.remove_leaf("SDK");
    f.remove_leaf("Mapping");
    f.root.close().unwrap();
    remove(f.base.raw_for_inventory().unwrap(), &f.name);
    remove(f.base.raw_for_inventory().unwrap(), &moved);
    f.base.close().unwrap();
}
