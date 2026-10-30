//! Bounded original-key inventory, deliberately not profile cleanup authority.
//! Mapping ancestors are retained without enumerating unrelated user values.
use super::*;
use windows::Win32::Foundation::{ERROR_FILE_NOT_FOUND, ERROR_NO_MORE_ITEMS};
use windows::Win32::System::Registry::*;
use windows::core::{PCWSTR, PWSTR};

const MAX_KEYS: usize = 128;
const MAX_DEPTH: usize = 16;
const MAX_VALUES: usize = 64;
const MAX_NAME: usize = 512;
const MAX_DATA: usize = 8192;
const MAX_INVENTORY: usize = 2 * 1024 * 1024;

/// Test-only native observation diagnostic. It never changes a refusal into
/// success and carries no registry handle, reopen or cleanup authority.
#[cfg(test)]
#[derive(Debug)]
struct FixtureRegistryLinkRefusal {
    key: String,
    name: Vec<u16>,
    data: Vec<u8>,
}
#[cfg(test)]
impl std::fmt::Display for FixtureRegistryLinkRefusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("unsupported registry value type or extent (links refused)")
    }
}
#[cfg(test)]
impl std::error::Error for FixtureRegistryLinkRefusal {}
#[cfg(test)]
pub(crate) fn fixture_is_writer_link_refusal(
    error: &anyhow::Error,
    container: &str,
    nonce: &str,
) -> bool {
    let suffix = format!("\\AppContainer\\Storage\\{container}\\FixedConsumer-{nonce}");
    error
        .chain()
        .filter_map(|cause| cause.downcast_ref::<FixtureRegistryLinkRefusal>())
        .any(|cause| {
            cause
                .key
                .get(cause.key.len().saturating_sub(suffix.len())..)
                .is_some_and(|observed| observed.eq_ignore_ascii_case(&suffix))
                && cause.name == "SyntheticValue".encode_utf16().collect::<Vec<_>>()
                && cause.data == b"bounded synthetic runtime setting"
        })
}

#[derive(PartialEq, Eq)]
struct Value {
    name: Vec<u16>,
    kind: u32,
    data: Vec<u8>,
}
#[derive(PartialEq, Eq)]
struct Snapshot {
    metadata: KeyMetadata,
    children: Vec<Vec<u16>>,
    values: Vec<Value>,
}
struct Node {
    key: RetainedKey,
    initial: Snapshot,
}
struct Tree {
    nodes: Vec<Node>,
}
struct Anchor {
    key: RetainedKey,
    initial: KeyMetadata,
}

/// Private bytes only, never durable ownership or a path to reopen.
#[derive(PartialEq, Eq)]
pub(crate) struct RegistryObservation {
    sdk: Vec<Snapshot>,
    mapping: Vec<Snapshot>,
}
#[cfg(test)]
impl RegistryObservation {
    pub(super) fn distinct_roots(&self, other: &Self) -> bool {
        self.sdk[0].metadata.name != other.sdk[0].metadata.name
            && self.mapping[0].metadata.name != other.mapping[0].metadata.name
    }
    pub(super) fn has_sdk_descendants(&self) -> bool {
        self.sdk.len() >= 3
    }
}
pub(crate) enum KeyObservation {
    Present(RegistryObservation),
    /// Every originally inventoried key reports KEY_DELETED. This does not
    /// prove absence of replacement storage, filesystem cleanup or ownership.
    OriginalKeyDeleted,
}
pub(crate) struct RetainedProfileRegistry {
    sdk: Tree,
    mapping: Tree,
    ancestors: Vec<Anchor>,
    sdk_ancestors: Vec<Anchor>,
}
#[path = "account_profile_registry_post_use.rs"]
mod post_use;
pub(crate) use post_use::ProfileRegistryRoots;

/// Original parents only, after checked closure of the SDK and mapping trees.
/// This cannot authorize deletion or reconstruct ownership after a restart.
pub(crate) struct RetainedRegistryParents {
    mapping: Vec<Anchor>,
    sdk: Vec<Anchor>,
    mapping_leaf: Vec<u16>,
    sdk_leaf: Vec<u16>,
}
fn unchanged_ancestors(anchors: &[Anchor]) -> Result<()> {
    ensure!(!anchors.is_empty(), "empty registry ancestor inventory");
    // Each retained ancestor includes its own KeyBasicInformation name:
    // KeyNameInformation alone can cache an old path after RegRenameKey.
    for anchor in anchors {
        anchor.key.refuse_link()?;
        ensure!(
            anchor.key.metadata()? == anchor.initial,
            "registry ancestor metadata drifted"
        );
    }
    Ok(())
}
fn close_ancestors(anchors: Vec<Anchor>) -> Result<()> {
    for anchor in anchors.into_iter().rev() {
        anchor.key.close()?;
    }
    Ok(())
}
impl RetainedRegistryParents {
    pub(crate) fn observe_absent(&self) -> Result<()> {
        crate::account_impersonation::require_process_identity()?;
        unchanged_ancestors(&self.mapping)?;
        unchanged_ancestors(&self.sdk)?;
        for (anchors, leaf) in [
            (&self.mapping, &self.mapping_leaf),
            (&self.sdk, &self.sdk_leaf),
        ] {
            if let Some(present) = anchors
                .last()
                .context("registry parent missing")?
                .key
                .child_or_absent(leaf)?
            {
                present.close()?;
                anyhow::bail!("profile registry name is still present or replaced");
            }
        }
        // These are sequential observations, not an atomic absence proof.
        unchanged_ancestors(&self.mapping)?;
        unchanged_ancestors(&self.sdk)
    }
    pub(crate) fn close(self) -> Result<()> {
        close_ancestors(self.sdk)?;
        close_ancestors(self.mapping)
    }
}

fn native_name(bytes: &[u8]) -> Result<String> {
    ensure!(
        !bytes.is_empty() && bytes.len() % 2 == 0,
        "invalid native registry name extent"
    );
    Ok(String::from_utf16(
        &bytes
            .chunks_exact(2)
            .map(|b| u16::from_le_bytes([b[0], b[1]]))
            .collect::<Vec<_>>(),
    )?)
}
fn sdk_relative_parts(classes: &[u8], sdk: &[u8], container: &str) -> Result<Vec<Vec<u16>>> {
    crate::job::execution_name(container)?;
    let classes = native_name(classes)?;
    let sdk = native_name(sdk)?;
    let expected = format!(
        "{classes}\\Local Settings\\Software\\Microsoft\\Windows\\CurrentVersion\\AppContainer\\Storage\\{container}"
    );
    ensure!(
        sdk.eq_ignore_ascii_case(&expected),
        "SDK registry layout is outside the verified actor's execution storage"
    );
    let parts = sdk[classes.len() + 1..]
        .split('\\')
        .map(|s| s.encode_utf16().collect::<Vec<_>>())
        .collect::<Vec<_>>();
    for part in &parts {
        component(part)?;
    }
    Ok(parts)
}
fn retained_chain(mut key: RetainedKey, parts: &[Vec<u16>]) -> Result<(Vec<Anchor>, RetainedKey)> {
    ensure!(
        !parts.is_empty() && parts.len() <= MAX_DEPTH,
        "registry ancestor depth refused"
    );
    let mut anchors = Vec::new();
    for part in parts {
        let initial = key.metadata()?;
        let child = key.child(part)?;
        anchors.push(Anchor { key, initial });
        key = child;
    }
    Ok((anchors, key))
}
fn leaf_name(tree: &Tree) -> Result<Vec<u16>> {
    let name = native_name(
        &tree
            .nodes
            .first()
            .context("empty registry tree")?
            .initial
            .metadata
            .name,
    )?;
    let leaf = name
        .rsplit('\\')
        .next()
        .context("registry leaf missing")?
        .encode_utf16()
        .collect::<Vec<_>>();
    component(&leaf)?;
    Ok(leaf)
}

impl RetainedKey {
    fn actor_classes(actor_sid: &str) -> Result<Self> {
        crate::account_token::verify_raw_profile_actor(actor_sid)?;
        // HKCU\Software\Classes is a Windows symbolic-link alias on supported
        // hosts. Open the actor-derived canonical hive directly; do not follow
        // that alias or trust its stored destination. The OS root is predefined,
        // but the returned child handle is real, retained and checked below.
        let canonical_actor = crate::sid::current_user_sid()?;
        let component = format!("{canonical_actor}_Classes");
        let name = crate::util::wstr(&component);
        let mut raw = HKEY::default();
        unsafe {
            RegOpenKeyExW(
                HKEY_USERS,
                PCWSTR(name.as_ptr()),
                Some(REG_OPTION_OPEN_LINK.0),
                KEY_READ | KEY_WOW64_64KEY,
                &mut raw,
            )
        }
        .ok()
        .context("open verified actor's canonical Classes hive")?;
        let key = Self::from_read_handle(raw)?;
        key.refuse_link()?;
        let expected = format!("\\REGISTRY\\USER\\{component}")
            .encode_utf16()
            .flat_map(u16::to_le_bytes)
            .collect::<Vec<_>>();
        ensure!(
            key.metadata()?.name.eq_ignore_ascii_case(&expected),
            "canonical Classes hive does not belong to verified actor"
        );
        Ok(key)
    }
    fn from_read_handle(raw: HKEY) -> Result<Self> {
        ensure!(!raw.0.is_null(), "empty registry inventory handle");
        let key = Self { key: Some(raw) };
        crate::account_namespace::verify_noninheritable(HANDLE(raw.0))?;
        Ok(key)
    }
    fn raw_for_inventory(&self) -> Result<HKEY> {
        self.key.context("registry inventory handle consumed")
    }
    fn metadata(&self) -> Result<KeyMetadata> {
        match self.observe_native()? {
            MetadataObservation::Present(value) => Ok(value),
            MetadataObservation::OriginalKeyDeleted => {
                anyhow::bail!("original registry key deleted")
            }
        }
    }
    fn child(&self, name: &[u16]) -> Result<Self> {
        self.child_or_absent(name)?
            .context("original registry child is absent")
    }
    fn child_or_absent(&self, name: &[u16]) -> Result<Option<Self>> {
        component(name)?;
        let mut terminated = name.to_vec();
        terminated.push(0);
        let mut raw = HKEY::default();
        let status = unsafe {
            RegOpenKeyExW(
                self.raw_for_inventory()?,
                PCWSTR(terminated.as_ptr()),
                Some(REG_OPTION_OPEN_LINK.0),
                KEY_READ | KEY_WOW64_64KEY,
                &mut raw,
            )
        };
        if status == ERROR_FILE_NOT_FOUND {
            ensure!(raw.0.is_null(), "missing registry child returned a handle");
            return Ok(None);
        }
        // PATH_NOT_FOUND, KEY_DELETED, access errors and all other failures
        // remain uncertain; none can acknowledge absence or authorize cleanup.
        status
            .ok()
            .context("open original registry child without following links")?;
        let key = Self::from_read_handle(raw)?;
        key.refuse_link()?;
        let mut expected = self.metadata()?.name;
        expected.extend_from_slice(&[92, 0]);
        for c in name {
            expected.extend_from_slice(&c.to_le_bytes());
        }
        ensure!(
            key.metadata()?.name == expected,
            "registry child resolved outside original parent or used an alias"
        );
        Ok(Some(key))
    }
    fn refuse_link(&self) -> Result<()> {
        let mut kind = REG_VALUE_TYPE::default();
        let mut size = 0;
        let status = unsafe {
            RegQueryValueExW(
                self.raw_for_inventory()?,
                windows::core::w!("SymbolicLinkValue"),
                None,
                Some(&mut kind),
                None,
                Some(&mut size),
            )
        };
        if status == ERROR_FILE_NOT_FOUND {
            return Ok(());
        }
        status.ok().context("check original registry link marker")?;
        ensure!(kind != REG_LINK, "registry symbolic link refused");
        Ok(())
    }
    fn snapshot(&self) -> Result<Snapshot> {
        let metadata = self.metadata()?;
        self.refuse_link()?;
        let key = self.raw_for_inventory()?;
        let mut children = Vec::new();
        // One extra call must return NO_MORE_ITEMS. Truncation is never a
        // successful bounded inventory, and MORE_DATA is never retried larger.
        for index in 0..=MAX_KEYS {
            let mut name = [0u16; MAX_NAME + 1];
            let mut length = name.len() as u32;
            let status = unsafe {
                RegEnumKeyExW(
                    key,
                    index as u32,
                    Some(PWSTR(name.as_mut_ptr())),
                    &mut length,
                    None,
                    None,
                    None,
                    None,
                )
            };
            if status == ERROR_NO_MORE_ITEMS {
                break;
            }
            status.ok().context("enumerate bounded registry children")?;
            ensure!(
                index < MAX_KEYS && (length as usize) <= MAX_NAME,
                "registry child inventory bound exceeded"
            );
            let name = name[..length as usize].to_vec();
            component(&name)?;
            children.push(name);
        }
        children.sort();
        ensure!(
            children.windows(2).all(|p| p[0] != p[1]),
            "duplicate child observation"
        );
        let mut values = Vec::new();
        for index in 0..=MAX_VALUES {
            let mut name = [0u16; MAX_NAME + 1];
            let mut length = name.len() as u32;
            let mut data = [0u8; MAX_DATA];
            let mut size = data.len() as u32;
            let mut kind = 0;
            let status = unsafe {
                RegEnumValueW(
                    key,
                    index as u32,
                    Some(PWSTR(name.as_mut_ptr())),
                    &mut length,
                    None,
                    Some(&mut kind),
                    Some(data.as_mut_ptr()),
                    Some(&mut size),
                )
            };
            if status == ERROR_NO_MORE_ITEMS {
                break;
            }
            status.ok().context("enumerate bounded registry values")?;
            ensure!(
                index < MAX_VALUES && (length as usize) <= MAX_NAME && (size as usize) <= MAX_DATA,
                "registry value inventory bound exceeded"
            );
            #[cfg(test)]
            if kind == REG_LINK.0 {
                return Err(FixtureRegistryLinkRefusal {
                    key: native_name(&metadata.name)?,
                    name: name[..length as usize].to_vec(),
                    data: data[..size as usize].to_vec(),
                }
                .into());
            }
            supported_value(kind, size as usize)?;
            let name = name[..length as usize].to_vec();
            ensure!(
                !name.contains(&0) && String::from_utf16(&name).is_ok(),
                "invalid registry value name"
            );
            values.push(Value {
                name,
                kind,
                data: data[..size as usize].to_vec(),
            });
        }
        values.sort_by(|a, b| a.name.cmp(&b.name));
        ensure!(
            values.windows(2).all(|p| p[0].name != p[1].name),
            "duplicate value observation"
        );
        ensure!(
            self.metadata()? == metadata,
            "registry metadata changed during inventory"
        );
        Ok(Snapshot {
            metadata,
            children,
            values,
        })
    }
}

/// Before any SDK effect, require the raw actor's loaded canonical Classes hive.
/// A fixed maintenance child is launched with LOGON_WITH_PROFILE; a missing or
/// aliased hive refuses creation, never changes identity or loads a caller path.
pub(crate) fn require_loaded_actor_classes(actor_sid: &str) -> Result<()> {
    RetainedKey::actor_classes(actor_sid)?.close()
}

fn component(name: &[u16]) -> Result<()> {
    ensure!(
        !name.is_empty()
            && name.len() <= MAX_NAME
            && !name.iter().any(|c| matches!(*c, 0 | 47 | 92))
            && name != [46]
            && name != [46, 46]
            && String::from_utf16(name).is_ok(),
        "invalid registry child component"
    );
    Ok(())
}
fn supported_value(kind: u32, length: usize) -> Result<()> {
    // Exact bytes are compared, never interpreted as a string or followed.
    ensure!(
        length <= MAX_DATA && matches!(kind, 0..=5 | 7 | 11),
        "unsupported registry value type or extent (links refused)"
    );
    ensure!(
        (kind != REG_DWORD.0 && kind != REG_DWORD_BIG_ENDIAN.0) || length == 4,
        "invalid DWORD extent"
    );
    ensure!(kind != REG_QWORD.0 || length == 8, "invalid QWORD extent");
    Ok(())
}
fn charge(total: &mut usize, bytes: usize) -> Result<()> {
    let next = total
        .checked_add(bytes)
        .context("registry inventory size overflow")?;
    ensure!(
        next <= MAX_INVENTORY,
        "registry inventory byte budget exceeded"
    );
    *total = next;
    Ok(())
}
fn charge_snapshot(total: &mut usize, value: &Snapshot) -> Result<()> {
    charge(total, value.metadata.name.len())?;
    charge(total, value.metadata.leaf_name.len())?;
    charge(total, value.metadata.owner_group_dacl.len())?;
    for name in &value.children {
        charge(total, name.len() * 2)?;
    }
    for value in &value.values {
        charge(total, value.name.len() * 2 + value.data.len() + 4)?;
    }
    Ok(())
}
impl Tree {
    fn capture(key: RetainedKey, total: &mut usize) -> Result<Self> {
        let mut tree = Self { nodes: Vec::new() };
        tree.append(key, 0, total)?;
        tree.observe()?; // Recheck all retained originals after the walk.
        Ok(tree)
    }
    fn append(&mut self, key: RetainedKey, depth: usize, total: &mut usize) -> Result<()> {
        ensure!(
            depth <= MAX_DEPTH && self.nodes.len() < MAX_KEYS,
            "registry tree bound exceeded"
        );
        let initial = key.snapshot()?;
        ensure!(
            self.nodes
                .iter()
                .all(|n| n.initial.metadata.name != initial.metadata.name),
            "registry alias or repeated identity refused"
        );
        charge_snapshot(total, &initial)?;
        let index = self.nodes.len();
        self.nodes.push(Node { key, initial });
        for position in 0..self.nodes[index].initial.children.len() {
            let child = self.nodes[index]
                .key
                .child(&self.nodes[index].initial.children[position])?;
            let mut expected = self.nodes[index].initial.metadata.name.clone();
            expected.extend_from_slice(&[92, 0]);
            for character in &self.nodes[index].initial.children[position] {
                expected.extend_from_slice(&character.to_le_bytes());
            }
            ensure!(
                child.metadata()?.name == expected,
                "registry child resolved outside original parent"
            );
            self.append(child, depth + 1, total)?;
        }
        Ok(())
    }
    fn observe(&self) -> Result<Option<Vec<Snapshot>>> {
        let mut result = Vec::new();
        let mut deleted = 0;
        let mut bytes = 0;
        for node in &self.nodes {
            match node.key.observe_native()? {
                MetadataObservation::OriginalKeyDeleted => deleted += 1,
                MetadataObservation::Present(_) => {
                    let value = node.key.snapshot()?;
                    ensure!(value == node.initial, "original registry inventory drifted");
                    charge_snapshot(&mut bytes, &value)?;
                    result.push(value);
                }
            }
        }
        classify_deletion(self.nodes.len(), deleted)?;
        Ok(if deleted > 0 { None } else { Some(result) })
    }
    fn close(self) -> Result<()> {
        for node in self.nodes.into_iter().rev() {
            node.key.close()?;
        }
        Ok(())
    }
}
fn classify_deletion(count: usize, deleted: usize) -> Result<()> {
    ensure!(
        count > 0 && deleted <= count && (deleted == 0 || deleted == count),
        "partial or empty original registry deletion inventory"
    );
    Ok(())
}
impl RetainedProfileRegistry {
    pub(crate) fn open(source: &OwnedHandle, container: &str, actor_sid: &str) -> Result<Self> {
        // SDK access can materialize profile storage. Future birth composition
        // must write its durable create intent before calling this method.
        let sdk = RetainedKey::open(source, container, actor_sid)?;
        crate::account_token::verify_raw_profile_actor(actor_sid)?;
        let package = crate::appcontainer::derive(container)?.string()?;
        let sdk_classes = RetainedKey::actor_classes(actor_sid)?;
        let sdk_initial = sdk.metadata()?;
        let parts =
            sdk_relative_parts(&sdk_classes.metadata()?.name, &sdk_initial.name, container)?;
        let (sdk_ancestors, relative_sdk) = retained_chain(sdk_classes, &parts)?;
        ensure!(
            relative_sdk.metadata()? == sdk_initial,
            "SDK key differs from the retained actor-relative storage"
        );
        relative_sdk.close()?;
        let mut key = RetainedKey::actor_classes(actor_sid)?;
        let mut ancestors = Vec::new();
        for name in [
            "Local Settings",
            "Software",
            "Microsoft",
            "Windows",
            "CurrentVersion",
            "AppContainer",
            "Mappings",
            &package,
        ] {
            let initial = key.metadata()?;
            let child = key.child(&name.encode_utf16().collect::<Vec<_>>())?;
            ancestors.push(Anchor { key, initial });
            key = child;
        }
        let mut bytes = 0;
        let sdk = Tree::capture(sdk, &mut bytes).context("capture original SDK registry tree")?;
        let mapping =
            Tree::capture(key, &mut bytes).context("capture original SID mapping tree")?;
        ensure!(
            sdk.nodes.iter().all(|s| mapping
                .nodes
                .iter()
                .all(|m| s.initial.metadata.name != m.initial.metadata.name)),
            "registry roots overlap"
        );
        let result = Self {
            sdk,
            mapping,
            ancestors,
            sdk_ancestors,
        };
        ensure!(
            matches!(result.observe()?, KeyObservation::Present(_)),
            "registry inventory disappeared"
        );
        Ok(result)
    }
    pub(crate) fn observe(&self) -> Result<KeyObservation> {
        crate::account_impersonation::require_process_identity()?;
        unchanged_ancestors(&self.ancestors)?;
        unchanged_ancestors(&self.sdk_ancestors)?;
        match (self.sdk.observe()?, self.mapping.observe()?) {
            (Some(sdk), Some(mapping)) => Ok(KeyObservation::Present(RegistryObservation {
                sdk,
                mapping,
            })),
            (None, None) => Ok(KeyObservation::OriginalKeyDeleted),
            _ => anyhow::bail!("SDK and mapping original deletion states differ"),
        }
    }
    pub(crate) fn close(self) -> Result<()> {
        self.sdk.close()?;
        self.mapping.close()?;
        close_ancestors(self.sdk_ancestors)?;
        close_ancestors(self.ancestors)
    }
    pub(crate) fn close_trees_for_observation(self) -> Result<RetainedRegistryParents> {
        ensure!(
            matches!(self.observe()?, KeyObservation::Present(_)),
            "cannot transfer deleted registry trees to parent observation"
        );
        let sdk_leaf = leaf_name(&self.sdk)?;
        let mapping_leaf = leaf_name(&self.mapping)?;
        self.sdk.close()?;
        self.mapping.close()?;
        let result = RetainedRegistryParents {
            mapping: self.ancestors,
            sdk: self.sdk_ancestors,
            mapping_leaf,
            sdk_leaf,
        };
        unchanged_ancestors(&result.mapping)?;
        unchanged_ancestors(&result.sdk)?;
        Ok(result)
    }
}

#[cfg(test)]
#[path = "account_profile_registry_parent_tests.rs"]
mod parent_tests;

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn account_registry_canonical_classes_is_current_actor_and_read_only() {
        let actor = crate::sid::current_user_sid().unwrap();
        let key = RetainedKey::actor_classes(&actor).unwrap();
        let original = key.metadata().unwrap();
        key.refuse_link().unwrap();
        assert!(key.metadata().unwrap() == original);
        key.close().unwrap();
        // No profile API, arbitrary user hive, registry enumeration or writes.
        assert!(RetainedKey::actor_classes("S-1-5-18").is_err());
    }
    #[test]
    fn account_registry_writer_refusal_diagnostic_requires_exact_observed_value() {
        let container = "Alysis.Execution.0123456789abcdef0123456789abcdef";
        let nonce = "1123456789abcdef0123456789abcdef";
        let error = || -> anyhow::Error {
            FixtureRegistryLinkRefusal {
            key: format!("\\REGISTRY\\USER\\synthetic_Classes\\Local Settings\\Software\\Microsoft\\Windows\\CurrentVersion\\AppContainer\\Storage\\{container}\\FixedConsumer-{nonce}"),
            name: "SyntheticValue".encode_utf16().collect(), data: b"bounded synthetic runtime setting".to_vec()
        }.into()
        };
        assert!(fixture_is_writer_link_refusal(&error(), container, nonce));
        let mut lower = error().downcast::<FixtureRegistryLinkRefusal>().unwrap();
        lower.key = lower.key.to_ascii_lowercase();
        assert!(fixture_is_writer_link_refusal(
            &lower.into(),
            container,
            nonce
        ));
        assert!(!fixture_is_writer_link_refusal(
            &anyhow::anyhow!("unrelated profile capture failed"),
            container,
            nonce
        ));
        assert!(!fixture_is_writer_link_refusal(
            &error(),
            container,
            "2123456789abcdef0123456789abcdef"
        ));
        assert!(!fixture_is_writer_link_refusal(
            &error(),
            "Alysis.Execution.other",
            nonce
        ));
        for field in ["name", "data"] {
            let mut value = error().downcast::<FixtureRegistryLinkRefusal>().unwrap();
            if field == "name" {
                value.name.push(65);
            } else {
                value.data.push(0);
            }
            assert!(!fixture_is_writer_link_refusal(
                &value.into(),
                container,
                nonce
            ));
        }
    }
    #[test]
    fn account_registry_inventory_bounds_reject_links_bad_names_and_overflow() {
        for name in ["", ".", "..", "a\\b", "a/b", "a\0b"] {
            assert!(component(&name.encode_utf16().collect::<Vec<_>>()).is_err());
        }
        assert!(component(&[0xd800]).is_err());
        assert!(component(&vec![65; MAX_NAME + 1]).is_err());
        assert!(component(&[65]).is_ok());
        for kind in [REG_LINK.0, 8, 9, 10, 12, u32::MAX] {
            assert!(supported_value(kind, 4).is_err());
        }
        assert!(supported_value(REG_DWORD.0, 3).is_err());
        assert!(supported_value(REG_QWORD.0, 4).is_err());
        assert!(supported_value(REG_BINARY.0, MAX_DATA + 1).is_err());
        assert!(supported_value(REG_BINARY.0, MAX_DATA).is_ok());
        let mut bytes = MAX_INVENTORY - 1;
        charge(&mut bytes, 1).unwrap();
        assert!(charge(&mut bytes, 1).is_err());
        assert_eq!(bytes, MAX_INVENTORY);
        assert!(charge(&mut bytes, usize::MAX).is_err());
    }
    #[test]
    fn account_registry_partial_deletion_never_counts_as_complete() {
        for (count, deleted) in [(0, 0), (0, 1), (2, 1), (2, 3), (128, 127)] {
            assert!(classify_deletion(count, deleted).is_err());
        }
        assert!(classify_deletion(128, 0).is_ok());
        assert!(classify_deletion(128, 128).is_ok());
    }
}
