use super::*;
use windows::Win32::Security::Cryptography::{BCRYPT_USE_SYSTEM_PREFERRED_RNG, BCryptGenRandom};

#[path = "account_profile_registry_post_use_tests.rs"]
mod post_use;

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().collect()
}
fn bytes(s: &str) -> Vec<u8> {
    s.encode_utf16().flat_map(u16::to_le_bytes).collect()
}
fn create(parent: HKEY, name: &str) -> RetainedKey {
    let name = crate::util::wstr(name);
    let mut raw = HKEY::default();
    let mut disposition = REG_CREATE_KEY_DISPOSITION::default();
    unsafe {
        RegCreateKeyExW(
            parent,
            PCWSTR(name.as_ptr()),
            None,
            PCWSTR::null(),
            REG_OPTION_NON_VOLATILE,
            KEY_ALL_ACCESS | KEY_WOW64_64KEY,
            None,
            &mut raw,
            Some(&mut disposition),
        )
    }
    .ok()
    .unwrap();
    let key = RetainedKey::from_read_handle(raw).unwrap();
    assert_eq!(
        disposition, REG_CREATED_NEW_KEY,
        "synthetic key collision; preserve it"
    );
    key
}
fn remove(parent: HKEY, name: &str) {
    let name = crate::util::wstr(name);
    unsafe { RegDeleteKeyExW(parent, PCWSTR(name.as_ptr()), KEY_WOW64_64KEY.0, None) }
        .ok()
        .unwrap();
}
struct Fixture {
    name: String,
    base: RetainedKey,
    root: RetainedKey,
}
impl Fixture {
    fn new() -> Self {
        let mut nonce = [0u8; 16];
        unsafe { BCryptGenRandom(None, &mut nonce, BCRYPT_USE_SYSTEM_PREFERRED_RNG) }
            .ok()
            .unwrap();
        let name = format!(
            "AlysisProfileRegistryTest-{}",
            nonce.iter().map(|b| format!("{b:02x}")).collect::<String>()
        );
        // Exercise the same canonical actor Classes hive as production. The
        // earlier NTUSER/Software fixture ancestry failed basic/full-name
        // agreement on the hosted runner and is outside the admitted layout.
        let base = RetainedKey::actor_classes(&crate::sid::current_user_sid().unwrap()).unwrap();
        let root = create(base.raw_for_inventory().unwrap(), &name);
        for leaf in ["SDK", "Mapping"] {
            create(root.raw_for_inventory().unwrap(), leaf)
                .close()
                .unwrap();
        }
        Self { name, base, root }
    }
    fn capture(&self) -> RetainedProfileRegistry {
        let mut total = 0;
        let open = || RetainedKey::actor_classes(&crate::sid::current_user_sid().unwrap()).unwrap();
        let prefix = self.name.split('\\').map(wide).collect::<Vec<_>>();
        let mut sdk_parts = prefix.clone();
        sdk_parts.push(wide("SDK"));
        let mut mapping_parts = prefix;
        mapping_parts.push(wide("Mapping"));
        let (sdk_ancestors, sdk) = retained_chain(open(), &sdk_parts).unwrap();
        let (ancestors, mapping) = retained_chain(open(), &mapping_parts).unwrap();
        RetainedProfileRegistry {
            sdk: Tree::capture(sdk, &mut total).unwrap(),
            mapping: Tree::capture(mapping, &mut total).unwrap(),
            sdk_ancestors,
            ancestors,
        }
    }
    fn remove_leaf(&self, name: &str) {
        remove(self.root.raw_for_inventory().unwrap(), name);
    }
    fn finish(self) {
        self.root.close().unwrap();
        remove(self.base.raw_for_inventory().unwrap(), &self.name);
        self.base.close().unwrap();
    }
    // No Drop cleanup: failed tests preserve their exclusively created fixtures.
}

#[test]
fn account_registry_parent_absence_requires_both_trees_and_refuses_replacement() {
    let f = Fixture::new();
    let parent = f.capture().close_trees_for_observation().unwrap();
    assert!(parent.observe_absent().is_err());
    f.remove_leaf("SDK");
    assert!(parent.observe_absent().is_err());
    f.remove_leaf("Mapping");
    parent.observe_absent().unwrap();
    create(f.root.raw_for_inventory().unwrap(), "SDK")
        .close()
        .unwrap();
    assert!(parent.observe_absent().is_err());
    f.remove_leaf("SDK");
    parent.observe_absent().unwrap();
    parent.close().unwrap();
    f.finish();
}

#[test]
fn account_registry_parent_observation_refuses_deleted_original_parent() {
    let f = Fixture::new();
    let parent = f.capture().close_trees_for_observation().unwrap();
    f.remove_leaf("SDK");
    f.remove_leaf("Mapping");
    parent.observe_absent().unwrap();
    remove(f.base.raw_for_inventory().unwrap(), &f.name);
    assert!(parent.observe_absent().is_err());
    parent.close().unwrap();
    f.root.close().unwrap();
    f.base.close().unwrap();
}

#[test]
fn account_registry_parent_observation_refuses_renamed_original_parent() {
    let f = Fixture::new();
    let parent = f.capture().close_trees_for_observation().unwrap();
    f.remove_leaf("SDK");
    f.remove_leaf("Mapping");
    parent.observe_absent().unwrap();
    let leaf = f.name.clone() + "-moved";
    let moved = crate::util::wstr(&leaf);
    unsafe {
        RegRenameKey(
            f.root.raw_for_inventory().unwrap(),
            PCWSTR::null(),
            PCWSTR(moved.as_ptr()),
        )
    }
    .ok()
    .unwrap();
    create(f.base.raw_for_inventory().unwrap(), &f.name)
        .close()
        .unwrap();
    assert!(parent.observe_absent().is_err());
    parent.close().unwrap();
    f.root.close().unwrap();
    remove(f.base.raw_for_inventory().unwrap(), &f.name);
    remove(f.base.raw_for_inventory().unwrap(), &leaf);
    f.base.close().unwrap();
}

#[test]
fn account_registry_tree_rename_and_replacement_prevent_parent_transfer() {
    let f = Fixture::new();
    let tree = f.capture();
    let raw = f.root.raw_for_inventory().unwrap();
    let original = crate::util::wstr("SDK");
    let moved = crate::util::wstr("SDK-moved");
    unsafe { RegRenameKey(raw, PCWSTR(original.as_ptr()), PCWSTR(moved.as_ptr())) }
        .ok()
        .unwrap();
    create(raw, "SDK").close().unwrap();
    assert!(tree.close_trees_for_observation().is_err());
    f.remove_leaf("SDK-moved");
    f.remove_leaf("SDK");
    f.remove_leaf("Mapping");
    f.finish();
}

#[test]
fn account_registry_drift_prevents_parent_transfer() {
    let f = Fixture::new();
    let tree = f.capture();
    let raw = f.root.raw_for_inventory().unwrap();
    create(raw, "SDK\\Added").close().unwrap();
    assert!(tree.close_trees_for_observation().is_err());
    remove(raw, "SDK\\Added");
    f.remove_leaf("SDK");
    f.remove_leaf("Mapping");
    f.finish();
}

#[test]
fn account_registry_sdk_parent_path_requires_exact_actor_container_and_layout() {
    let actor = "\\REGISTRY\\USER\\S-1-5-21-1-2-3-4_Classes";
    let container = "Alysis.Execution.0123456789abcdef0123456789abcdef";
    let sdk = format!(
        "{actor}\\Local Settings\\Software\\Microsoft\\Windows\\CurrentVersion\\AppContainer\\Storage\\{container}"
    );
    let parts = sdk_relative_parts(&bytes(actor), &bytes(&sdk), container).unwrap();
    assert_eq!(parts.len(), 8);
    assert_eq!(parts[7], wide(container));
    assert!(
        sdk_relative_parts(&bytes(actor), &bytes(&sdk.to_ascii_lowercase()), container).is_ok()
    );
    for invalid in [
        sdk.replace("-4_Classes", "-5_Classes"),
        sdk.replace("Storage", "Mappings"),
        sdk.clone() + "\\Child",
        sdk.replace(
            container,
            "Alysis.Execution.ffffffffffffffffffffffffffffffff",
        ),
        "\\REGISTRY\\MACHINE\\Unrelated".into(),
    ] {
        assert!(sdk_relative_parts(&bytes(actor), &bytes(&invalid), container).is_err());
    }
    assert!(sdk_relative_parts(&bytes(actor), &[0], container).is_err());
    assert!(sdk_relative_parts(&bytes(actor), &bytes(&sdk), "bad").is_err());
}
