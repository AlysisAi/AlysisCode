//! Native effects are intentionally limited to one exclusively born empty root.
//! Existing parents are opened without reparse traversal and never modified.
use crate::{account_state::execution::ExecutionRecord, util::OwnedHandle};
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};

/// Constructor identity is independent of ACL/RPC policy. Historical policy2
/// described both constructors, so omitted data cannot assert either one.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum RootBirthKind {
    #[default]
    LegacyUnspecified,
    OrdinaryDirectoryV1,
    SessionShadowDirectoryV1,
}
impl RootBirthKind {
    pub(in crate::account_state::execution) fn is_legacy(&self) -> bool {
        *self == Self::LegacyUnspecified
    }
    pub(in crate::account_state::execution) fn require_live(self) -> Result<()> {
        match self {
            Self::LegacyUnspecified => Ok(()),
            #[cfg(test)]
            Self::SessionShadowDirectoryV1 => {
                crate::account_rpc_v3_lifecycle_fixture::require_shadow_root_live()
            }
            _ => anyhow::bail!("explicit namespace constructor lifecycle is unqualified"),
        }
    }
}

/// Policy subjects only; this interface conveys no live lifecycle authority.
/// Both callers separately require their own exact, incompatible journal scope.
pub(in crate::account_state::execution) trait NamespacePolicySource {
    fn execution(&self) -> &ExecutionRecord;
    fn session(&self) -> u32;
    fn namespace_birth(&self) -> RootBirthKind {
        RootBirthKind::LegacyUnspecified
    }
    fn rpc_policy(&self) -> rpc::PolicyKind {
        rpc::PolicyKind::BaselineV1
    }
}
use crate::util::{OwnedSd, counted_utf16};
use std::mem::size_of;
use windows::Wdk::{
    Foundation::OBJECT_ATTRIBUTES,
    Storage::FileSystem::{
        NtCreateDirectoryObject, NtOpenDirectoryObject, NtQueryDirectoryObject,
        NtQuerySecurityObject, NtSetSecurityObject,
    },
};
use windows::Win32::{Foundation::*, Security::*};
use windows::core::PWSTR;

const DIRECTORY_ALL: u32 = 0x000f000f;
const PARENT_ACCESS: u32 = 0x0000000b; // query, traverse, create subdirectory
const QUERY_ACCESS: u32 = 0x00020003; // READ_CONTROL, query, traverse
const NO_MORE_ENTRIES: NTSTATUS = NTSTATUS(0x8000001au32 as i32);
const NAME_NOT_FOUND: NTSTATUS = NTSTATUS(0xc0000034u32 as i32);
const OBJECT_FLAGS: OBJECT_ATTRIBUTE_FLAGS =
    OBJECT_ATTRIBUTE_FLAGS(OBJ_CASE_INSENSITIVE.0 | OBJ_DONT_REPARSE.0);
#[path = "account_resource_namespace_inventory.rs"]
pub(in crate::account_state::execution) mod inventory;
#[path = "account_namespace_object_name.rs"]
mod object_name;
#[path = "account_initializer_rpc.rs"]
pub(in crate::account_state::execution) mod rpc;
#[cfg(test)]
#[path = "account_namespace_shadow_fixture.rs"]
mod shadow_fixture;
#[cfg(test)]
pub(in crate::account_state::execution) use shadow_fixture::FixtureShadow;

fn parent_name(identity: &impl NamespacePolicySource) -> String {
    format!(
        "\\Sessions\\{}\\AppContainerNamedObjects",
        identity.session()
    )
}
fn attributes<'a>(name: &'a mut UNICODE_STRING, parent: HANDLE) -> OBJECT_ATTRIBUTES {
    OBJECT_ATTRIBUTES {
        Length: size_of::<OBJECT_ATTRIBUTES>() as u32,
        RootDirectory: parent,
        ObjectName: name,
        Attributes: OBJECT_FLAGS,
        ..Default::default()
    }
}
fn unicode(text: &str) -> Result<(Vec<u16>, u16)> {
    ensure!(
        !text.is_empty() && !text.contains('\0') && text.encode_utf16().count() <= 2048,
        "invalid bounded namespace name"
    );
    Ok(counted_utf16(text))
}
fn open_directory(name: &str, parent: HANDLE, access: u32) -> Result<OwnedHandle, NTSTATUS> {
    let (mut wide, length) = unicode(name).map_err(|_| NTSTATUS(0xc000000du32 as i32))?;
    let mut name = UNICODE_STRING {
        Length: length,
        MaximumLength: length,
        Buffer: PWSTR(wide.as_mut_ptr()),
    };
    let attrs = attributes(&mut name, parent);
    let mut raw = HANDLE::default();
    let status = unsafe { NtOpenDirectoryObject(&mut raw, access, &attrs) };
    if status.0 == 0 {
        Ok(OwnedHandle(raw))
    } else {
        Err(status)
    }
}
fn status_error(status: NTSTATUS, context: &str) -> anyhow::Error {
    anyhow::anyhow!("{context}: NTSTATUS=0x{:08x}", status.0 as u32)
}

pub(in crate::account_state::execution) fn open_parent(
    identity: &impl NamespacePolicySource,
) -> Result<OwnedHandle> {
    let parent = open_directory(&parent_name(identity), HANDLE::default(), PARENT_ACCESS)
        .map_err(|s| status_error(s, "open existing session namespace parent without reparses"))?;
    crate::account_namespace::verify_noninheritable(parent.raw())?;
    Ok(parent)
}
pub(in crate::account_state::execution) fn verify_parent(
    parent: HANDLE,
    identity: &impl NamespacePolicySource,
) -> Result<()> {
    crate::account_namespace::verify_noninheritable(parent)?;
    let current = open_parent(identity)?;
    ensure!(
        unsafe { CompareObjectHandles(parent, current.raw()) }.as_bool(),
        "session namespace parent no longer matches retained object"
    );
    Ok(())
}
pub(in crate::account_state::execution) fn descriptor(
    identity: &impl NamespacePolicySource,
) -> Result<OwnedSd> {
    let owner = &identity.execution().owner;
    let account = &identity.execution().identity.binding.account_sid;
    let package = &identity.execution().identity.package_sid;
    ensure!(
        owner != account && owner != package && account != package && owner != "S-1-5-18",
        "namespace identities must be distinct ordinary broker/account/package subjects"
    );
    OwnedSd::from_sddl(&format!(
        "O:{owner}D:P(A;;0x{DIRECTORY_ALL:x};;;SY)(A;;0x{DIRECTORY_ALL:x};;;{owner})(A;;0x{DIRECTORY_ALL:x};;;{account})(A;;0x{DIRECTORY_ALL:x};;;{package})S:(ML;;NW;;;LW)"
    ))
}
/// Birth starts with a protected host/SYSTEM-only DACL. Supplying a SACL to
/// NtCreateDirectoryObject requires SeSecurityPrivilege on supported Windows;
/// this broker must not enable that privilege or require elevation. Apply only
/// LABEL_SECURITY_INFORMATION through the original WRITE_OWNER handle, verify
/// the Low label while the DACL is still private, then publish the final DACL.
fn private_descriptor(identity: &impl NamespacePolicySource) -> Result<OwnedSd> {
    let owner = &identity.execution().owner;
    // Validate all subjects through the final descriptor before any effect.
    drop(descriptor(identity)?);
    OwnedSd::from_sddl(&format!(
        "O:{owner}D:P(A;;0x{DIRECTORY_ALL:x};;;SY)(A;;0x{DIRECTORY_ALL:x};;;{owner})"
    ))
}
fn descriptor_shape(descriptor: &OwnedSd) -> Result<SecurityShape> {
    let length = unsafe { GetSecurityDescriptorLength(descriptor.ptr) } as usize;
    ensure!(
        (20..=65536).contains(&length),
        "invalid owned security descriptor length"
    );
    security_shape(unsafe { std::slice::from_raw_parts(descriptor.ptr.0.cast::<u8>(), length) })
}
pub(in crate::account_state::execution) fn verify_private_birth(
    root: HANDLE,
    identity: &impl NamespacePolicySource,
    labelled: bool,
) -> Result<()> {
    crate::account_namespace::verify_noninheritable(root)?;
    let actual = security_shape(&query_security(root)?)?;
    let expected = descriptor_shape(&private_descriptor(identity)?)?;
    // Owner and every DACL control/ACE byte must remain exact. The inherited
    // audit SACL is not granted authority and is never rewritten by this code.
    const OWNER_DACL_CONTROL: u16 = 0x150d;
    ensure!(
        actual.owner == expected.owner
            && actual.dacl == expected.dacl
            && actual.control & OWNER_DACL_CONTROL == expected.control & OWNER_DACL_CONTROL,
        "private namespace birth owner or protected host/SYSTEM DACL changed"
    );
    if labelled {
        let final_policy = descriptor_shape(&descriptor(identity)?)?;
        ensure!(
            actual.label == final_policy.label,
            "private namespace birth Low label unverified"
        );
    }
    require_empty(root)
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(in crate::account_state::execution) enum BirthStage {
    Private,
    Labelled,
    Published,
}
pub(in crate::account_state::execution) fn create_native(
    identity: &impl NamespacePolicySource,
    parent: HANDLE,
) -> Result<OwnedHandle> {
    create_native_inner(
        identity,
        parent,
        |_, _| Ok(()),
        #[cfg(test)]
        None,
    )
}
fn create_native_inner(
    identity: &impl NamespacePolicySource,
    parent: HANDLE,
    mut hook: impl FnMut(BirthStage, HANDLE) -> Result<()>,
    #[cfg(test)] shadow: Option<&FixtureShadow>,
) -> Result<OwnedHandle> {
    identity.namespace_birth().require_live()?;
    #[cfg(test)]
    if identity.namespace_birth() == RootBirthKind::SessionShadowDirectoryV1 {
        ensure!(
            shadow.is_some()
                && identity.rpc_policy() == rpc::PolicyKind::SystemNsiReadInheritanceV3,
            "explicit shadow constructor differs from admitted source"
        );
    }
    verify_parent(parent, identity)?;
    let private = private_descriptor(identity)?;
    let policy = descriptor(identity)?;
    let (mut wide, length) = unicode(&identity.execution().identity.package_sid)?;
    let mut name = UNICODE_STRING {
        Length: length,
        MaximumLength: length,
        Buffer: PWSTR(wide.as_mut_ptr()),
    };
    let mut attrs = attributes(&mut name, parent);
    // No OPENIF, PERMANENT, INHERIT or OPENLINK. Only exact success is birth.
    attrs.SecurityDescriptor = private.ptr.0.cast();
    let mut raw = HANDLE::default();
    #[cfg(not(test))]
    let status = unsafe { NtCreateDirectoryObject(&mut raw, DIRECTORY_ALL, &attrs) };
    #[cfg(test)]
    let status = match shadow {
        Some(shadow) => shadow.create(identity, &mut raw, &attrs)?,
        None => unsafe { NtCreateDirectoryObject(&mut raw, DIRECTORY_ALL, &attrs) },
    };
    if status.0 != 0 {
        if !raw.is_invalid() {
            drop(OwnedHandle(raw));
        }
        return Err(status_error(
            status,
            "exclusive private namespace birth refused",
        ));
    }
    let handle = OwnedHandle(raw);
    verify_private_birth(handle.raw(), identity, false)?;
    hook(BirthStage::Private, handle.raw())?;
    verify_private_birth(handle.raw(), identity, false)?;
    unsafe { NtSetSecurityObject(handle.raw(), LABEL_SECURITY_INFORMATION.0, policy.ptr) }
        .ok()
        .context("apply exact Low namespace label through retained ownership")?;
    verify_private_birth(handle.raw(), identity, true)?;
    hook(BirthStage::Labelled, handle.raw())?;
    // Recheck after the seam and immediately before granting package/account
    // access. No raw namespace handle is exposed to a production caller.
    verify_private_birth(handle.raw(), identity, true)?;
    unsafe {
        NtSetSecurityObject(
            handle.raw(),
            (DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION).0,
            policy.ptr,
        )
    }
    .ok()
    .context("publish exact protected namespace DACL")?;
    verify_native(handle.raw(), parent, identity)?;
    hook(BirthStage::Published, handle.raw())?;
    verify_native(handle.raw(), parent, identity)?;
    Ok(handle)
}
#[cfg(test)]
pub(in crate::account_state::execution) fn fixture_create_stages(
    identity: &impl NamespacePolicySource,
    parent: HANDLE,
    hook: impl FnMut(BirthStage, HANDLE) -> Result<()>,
) -> Result<OwnedHandle> {
    create_native_inner(identity, parent, hook, None)
}
#[cfg(test)]
pub(in crate::account_state::execution) fn fixture_create_shadow(
    identity: &impl NamespacePolicySource,
    parent: HANDLE,
    shadow: &FixtureShadow,
) -> Result<OwnedHandle> {
    create_native_inner(identity, parent, |_, _| Ok(()), Some(shadow))
}
pub(in crate::account_state::execution) fn verify_native(
    root: HANDLE,
    parent: HANDLE,
    identity: &impl NamespacePolicySource,
) -> Result<()> {
    verify_identity(root, parent, identity)?;
    require_empty(root)
}
/// Identity and exact root descriptor only; does not admit populated contents.
/// Existing empty-root callers still require emptiness through verify_native.
pub(in crate::account_state::execution) fn verify_identity(
    root: HANDLE,
    parent: HANDLE,
    identity: &impl NamespacePolicySource,
) -> Result<()> {
    verify_parent(parent, identity)?;
    crate::account_namespace::verify_noninheritable(root)?;
    let named = open_directory(
        &identity.execution().identity.package_sid,
        parent,
        QUERY_ACCESS,
    )
    .map_err(|s| status_error(s, "query retained namespace name"))?;
    ensure!(
        unsafe { CompareObjectHandles(root, named.raw()) }.as_bool(),
        "namespace name no longer matches original object"
    );
    let expected = descriptor(identity)?;
    let length = unsafe { GetSecurityDescriptorLength(expected.ptr) } as usize;
    ensure!(
        (20..=65536).contains(&length),
        "unexpected expected namespace security length"
    );
    let expected = unsafe { std::slice::from_raw_parts(expected.ptr.0.cast::<u8>(), length) };
    let actual = security_shape(&query_security(root)?)?;
    let expected = security_shape(expected)?;
    ensure!(
        actual == expected,
        "namespace security differs from exact policy: control=0x{:04x}/0x{:04x}, owner_equal={}, dacl_equal={}, label_equal={}",
        actual.control,
        expected.control,
        actual.owner == expected.owner,
        actual.dacl == expected.dacl,
        actual.label == expected.label
    );
    Ok(())
}
pub(in crate::account_state::execution) fn query_security(handle: HANDLE) -> Result<Vec<u8>> {
    const CAPACITY: usize = 65536;
    let mut storage = vec![0usize; CAPACITY / size_of::<usize>()];
    let mut returned = 0;
    unsafe {
        NtQuerySecurityObject(
            handle,
            (OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION | LABEL_SECURITY_INFORMATION).0,
            Some(PSECURITY_DESCRIPTOR(storage.as_mut_ptr().cast())),
            CAPACITY as u32,
            &mut returned,
        )
    }
    .ok()
    .context("read namespace security through original handle")?;
    ensure!(
        (20..=CAPACITY).contains(&(returned as usize)),
        "namespace security length exceeds bound"
    );
    Ok(
        unsafe { std::slice::from_raw_parts(storage.as_ptr().cast::<u8>(), returned as usize) }
            .to_vec(),
    )
}

#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub(in crate::account_state::execution) struct SecurityShape {
    control: u16,
    owner: Vec<u8>,
    pub(in crate::account_state::execution) dacl: Vec<u8>,
    pub(in crate::account_state::execution) label: Vec<u8>,
}
// Parse the bounded SELF_RELATIVE result without dereferencing embedded pointers.
// Exact component bytes preserve ACE order, flags and masks. No reconstruction.
pub(in crate::account_state::execution) fn security_shape(bytes: &[u8]) -> Result<SecurityShape> {
    ensure!(
        bytes.len() >= 20 && bytes[0] == 1,
        "invalid relative security header"
    );
    let u16_at = |at: usize| -> Result<u16> {
        Ok(u16::from_le_bytes(
            bytes
                .get(at..at + 2)
                .context("truncated security field")?
                .try_into()?,
        ))
    };
    let u32_at = |at: usize| -> Result<usize> {
        Ok(u32::from_le_bytes(
            bytes
                .get(at..at + 4)
                .context("truncated security offset")?
                .try_into()?,
        ) as usize)
    };
    let control = u16_at(2)?;
    ensure!(
        control & 0x8000 != 0 && control & 0x4 != 0,
        "security must have relative DACL"
    );
    let owner = u32_at(4)?;
    let sacl = u32_at(12)?;
    let dacl = u32_at(16)?;
    let mut ranges = Vec::new();
    let mut component = |offset: usize, sid: bool| -> Result<Vec<u8>> {
        ensure!(
            offset >= 20
                && offset % 4 == 0
                && offset.checked_add(8).is_some_and(|n| n <= bytes.len()),
            "security component outside descriptor"
        );
        let len = if sid {
            ensure!(
                bytes[offset] == 1 && bytes[offset + 1] <= 15,
                "invalid bounded owner SID"
            );
            8 + 4 * bytes[offset + 1] as usize
        } else {
            u16_at(offset + 2)? as usize
        };
        ensure!(
            len >= 8 && offset.checked_add(len).is_some_and(|n| n <= bytes.len()),
            "security component length exceeds descriptor"
        );
        if !sid {
            ensure!(matches!(bytes[offset], 2 | 4), "unexpected ACL revision");
            let count = u16_at(offset + 4)? as usize;
            ensure!(count <= 32, "namespace ACL count exceeds policy bound");
            let mut cursor = offset + 8;
            for _ in 0..count {
                ensure!(cursor + 4 <= offset + len, "ACL entry header exceeds ACL");
                let size = u16_at(cursor + 2)? as usize;
                ensure!(
                    size >= 4 && size % 4 == 0 && cursor + size <= offset + len,
                    "ACL entry exceeds ACL"
                );
                cursor += size;
            }
            ensure!(cursor == offset + len, "unexpected trailing ACL bytes");
        }
        ranges.push((offset, offset + len));
        Ok(bytes[offset..offset + len].to_vec())
    };
    let owner = component(owner, true)?;
    let label = if control & 0x10 != 0 {
        component(sacl, false)?
    } else {
        ensure!(sacl == 0, "absent SACL has an unexpected offset");
        Vec::new()
    };
    let dacl = component(dacl, false)?;
    ranges.sort_unstable();
    ensure!(
        ranges.windows(2).all(|pair| pair[0].1 <= pair[1].0),
        "overlapping security components"
    );
    Ok(SecurityShape {
        control: control & 0x3f3d,
        owner,
        dacl,
        label,
    })
}
fn require_empty(root: HANDLE) -> Result<()> {
    let mut buffer = [0usize; 1024];
    let mut context = 0;
    let mut returned = 0;
    let status = unsafe {
        NtQueryDirectoryObject(
            root,
            Some(buffer.as_mut_ptr().cast()),
            size_of::<[usize; 1024]>() as u32,
            true,
            true,
            &mut context,
            Some(&mut returned),
        )
    };
    ensure!(
        status == NO_MORE_ENTRIES,
        "namespace is nonempty or emptiness is unverified (NTSTATUS=0x{:08x})",
        status.0 as u32
    );
    Ok(())
}
pub(in crate::account_state::execution) fn require_absent(
    parent: HANDLE,
    identity: &impl NamespacePolicySource,
) -> Result<()> {
    ensure!(
        observe_absent(parent, identity)?,
        "namespace remains present; preserve retained close proof and removal intent"
    );
    Ok(())
}
/// Kernel reference teardown can trail whole-job exit and checked handle
/// closure. Observe the fixed original parent for at most five seconds; no
/// deletion, target traversal or retry of a native mutation is performed.
/// Only NAME_NOT_FOUND proves absence. All other query errors stop immediately.
pub(in crate::account_state::execution) fn wait_absent(
    parent: HANDLE,
    identity: &impl NamespacePolicySource,
) -> Result<()> {
    crate::account_impersonation::require_process_identity()?;
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    loop {
        verify_parent(parent, identity)?;
        if observe_absent(parent, identity)? {
            return Ok(());
        }
        ensure!(
            std::time::Instant::now() < deadline,
            "namespace remains present after bounded retirement wait; preserve original removal intent"
        );
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
}
fn observe_absent(parent: HANDLE, identity: &impl NamespacePolicySource) -> Result<bool> {
    match open_directory(
        &identity.execution().identity.package_sid,
        parent,
        QUERY_ACCESS,
    ) {
        Ok(handle) => {
            unsafe { CloseHandle(handle.into_raw()) }
                .context("namespace observation close uncertain; preserve state")?;
            Ok(false)
        }
        Err(status) if status == NAME_NOT_FOUND => Ok(true),
        Err(status) => Err(status_error(status, "namespace absence unproven")),
    }
}

#[cfg(test)]
pub(in crate::account_state::execution) fn child_fixture(
    root: HANDLE,
    identity: &impl NamespacePolicySource,
) -> Result<OwnedHandle> {
    let (mut wide, length) = unicode("unexpected-fixture-child")?;
    let mut name = UNICODE_STRING {
        Length: length,
        MaximumLength: length,
        Buffer: PWSTR(wide.as_mut_ptr()),
    };
    let mut attrs = attributes(&mut name, root);
    let sd = private_descriptor(identity)?;
    attrs.SecurityDescriptor = sd.ptr.0.cast();
    let mut raw = HANDLE::default();
    let status = unsafe { NtCreateDirectoryObject(&mut raw, DIRECTORY_ALL, &attrs) };
    ensure!(
        status.0 == 0,
        "test-only child birth failed: 0x{:08x}",
        status.0 as u32
    );
    Ok(OwnedHandle(raw))
}

/// Read-only regression snapshot of the shared target. Never returned as ACL
/// mutation authority and never used for sandbox cleanup.
#[cfg(test)]
pub(in crate::account_state::execution) fn fixture_global_security() -> Result<Vec<u8>> {
    crate::account_impersonation::require_process_identity()?;
    let original = open_directory("\\BaseNamedObjects", HANDLE::default(), 0x20000)
        .map_err(|s| status_error(s, "open shared namespace READ_CONTROL only"))?;
    crate::account_namespace::verify_noninheritable(original.raw())?;
    let security = query_security(original.raw());
    unsafe { CloseHandle(original.into_raw()) }.context("shared snapshot handle close failed")?;
    security
}

/// Bounded diagnostic settlement only. Never authorizes cleanup or receipts.
#[cfg(test)]
pub(in crate::account_state::execution) fn fixture_observe_settlement(
    identity: &impl NamespacePolicySource,
) -> Result<u32> {
    let parent = open_parent(identity)?;
    for attempt in 0..=50 {
        verify_parent(parent.raw(), identity)?;
        if observe_absent(parent.raw(), identity)? {
            return Ok(attempt);
        }
        if attempt == 50 {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
    let root = open_directory(
        &identity.execution().identity.package_sid,
        parent.raw(),
        QUERY_ACCESS,
    )
    .map_err(|s| status_error(s, "open surviving root query-only"))?;
    let children = inventory::observe_children(root.raw())?;
    anyhow::bail!(
        "namespace remains present after five seconds; observed children={children:?}; no cleanup authority claimed"
    )
}

/// Read-only name observation after a failed fixture process has exited. This
/// deliberately does not reconstruct a NamespacePolicySource or owned guard.
#[cfg(test)]
pub(in crate::account_state::execution) fn fixture_recorded_namespace_absent(
    session: u32,
    package: &str,
) -> Result<bool> {
    ensure!(
        package.len() <= 256 && package.starts_with("S-1-15-2-"),
        "invalid diagnostic package SID"
    );
    let _sid = crate::sid::LocalPsid::from_string(package)?;
    let parent = open_directory(
        &format!("\\Sessions\\{session}\\AppContainerNamedObjects"),
        HANDLE::default(),
        QUERY_ACCESS,
    )
    .map_err(|s| status_error(s, "diagnostic session parent open failed"))?;
    let observed = match open_directory(package, parent.raw(), QUERY_ACCESS) {
        Ok(h) => {
            unsafe { CloseHandle(h.into_raw()) }?;
            Ok(false)
        }
        Err(s) if s == NAME_NOT_FOUND => Ok(true),
        Err(s) => Err(status_error(s, "diagnostic root query failed")),
    };
    unsafe { CloseHandle(parent.into_raw()) }?;
    observed
}
