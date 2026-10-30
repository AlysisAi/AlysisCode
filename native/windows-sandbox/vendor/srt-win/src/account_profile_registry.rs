//! Retained SDK registry observations for a future composite profile adapter.
//!
//! No profile birth/deletion, pathname reopen, value write, journal receipt or
//! cleanup authority. Original read-only keys and bounded descendants are retained. Native
//! profile effects belong exclusively to the ignored disposable-host fixture.
#![allow(dead_code)] // Deliberately not wired into account activation yet.

use anyhow::{Context, Result, ensure};
use std::mem::size_of;
use windows::Wdk::Storage::FileSystem::NtQuerySecurityObject;
use windows::Win32::Foundation::{HANDLE, NTSTATUS};
use windows::Win32::Security::Isolation::GetAppContainerRegistryLocation;
use windows::Win32::Security::*;
use windows::Win32::System::LibraryLoader::{GetModuleHandleW, GetProcAddress};
use windows::Win32::System::Registry::{HKEY, KEY_READ, KEY_WOW64_64KEY, RegCloseKey};
use windows::Win32::System::Threading::{GetCurrentThread, OpenThreadToken};

use crate::util::OwnedHandle;

const MAX_QUERY: usize = 65536;
const KEY_DELETED: NTSTATUS = NTSTATUS(0xc000017cu32 as i32);

struct QueryBuffer {
    words: Vec<usize>,
    length: usize,
}
impl QueryBuffer {
    fn new() -> Self {
        Self {
            words: vec![0; MAX_QUERY / size_of::<usize>()],
            length: 0,
        }
    }
    fn bytes(&self) -> Result<&[u8]> {
        ensure!(
            self.length > 0 && self.length <= self.words.len() * size_of::<usize>(),
            "native query length is outside its allocation"
        );
        Ok(unsafe { std::slice::from_raw_parts(self.words.as_ptr().cast(), self.length) })
    }
    fn token(token: HANDLE, class: TOKEN_INFORMATION_CLASS) -> Result<Self> {
        let mut buffer = Self::new();
        let mut returned = 0;
        unsafe {
            GetTokenInformation(
                token,
                class,
                Some(buffer.words.as_mut_ptr().cast()),
                MAX_QUERY as u32,
                &mut returned,
            )?
        };
        buffer.length = returned as usize;
        buffer.bytes()?;
        Ok(buffer)
    }
    fn number(&self) -> Result<u32> {
        let bytes = self.bytes()?;
        ensure!(bytes.len() >= 4, "truncated token number");
        Ok(u32::from_ne_bytes(bytes[..4].try_into()?))
    }
    fn sid(&self, header: usize) -> Result<&[u8]> {
        let bytes = self.bytes()?;
        ensure!(
            header >= size_of::<usize>() && bytes.len() >= header,
            "truncated token SID header"
        );
        let address = usize::from_ne_bytes(bytes[..size_of::<usize>()].try_into()?);
        let start = self.words.as_ptr() as usize;
        let offset = address
            .checked_sub(start)
            .context("token SID precedes its allocation")?;
        ensure!(
            offset >= header && offset <= bytes.len().saturating_sub(8),
            "token SID is outside returned information"
        );
        let sid = &bytes[offset..];
        ensure!(
            sid[0] == 1 && sid[1] <= 15,
            "invalid bounded token SID header"
        );
        let length = 8 + 4 * sid[1] as usize;
        ensure!(
            length <= sid.len(),
            "token SID exceeds returned information"
        );
        Ok(&sid[..length])
    }
}

pub(crate) fn verify_profile_token(token: HANDLE, package: &[u8], actor: &[u8]) -> Result<()> {
    ensure!(
        QueryBuffer::token(token, TokenIsAppContainer)?.number()? == 1,
        "registry context requires an AppContainer token"
    );
    ensure!(
        QueryBuffer::token(token, TokenCapabilities)?.number()? == 0,
        "registry maintenance requires zero capabilities"
    );
    ensure!(
        QueryBuffer::token(token, TokenUser)?.sid(size_of::<TOKEN_USER>())? == actor,
        "registry context actor mismatch"
    );
    ensure!(
        QueryBuffer::token(token, TokenAppContainerSid)?
            .sid(size_of::<TOKEN_APPCONTAINER_INFORMATION>())?
            == package,
        "registry context package mismatch"
    );
    Ok(())
}

fn verify_effective_profile(package: &[u8], actor: &[u8]) -> Result<()> {
    let mut raw = HANDLE::default();
    unsafe { OpenThreadToken(GetCurrentThread(), TOKEN_QUERY, true, &mut raw)? };
    let token = OwnedHandle(raw);
    verify_profile_token(token.raw(), package, actor)
}

/// Private bytes are observations, never paths to reopen or serialized ownership.
/// Exact descriptor/name equality detects observed drift, not intervening edits.
#[derive(PartialEq, Eq)]
struct KeyMetadata {
    name: Vec<u8>,
    leaf_name: Vec<u8>,
    owner_group_dacl: Vec<u8>,
}

enum MetadataObservation {
    Present(KeyMetadata),
    /// The original key reports KEY_DELETED. This says nothing about descendants,
    /// replacement keys, other profile storage or permission to retire a receipt.
    OriginalKeyDeleted,
}

/// No Clone, Deserialize, Debug, raw handle getter or construction from a path.
struct RetainedKey {
    key: Option<HKEY>,
}

impl RetainedKey {
    pub(crate) fn open(source: &OwnedHandle, container: &str, actor_sid: &str) -> Result<Self> {
        crate::account_token::verify_raw_profile_actor(actor_sid)?;
        let actor = crate::sid::LocalPsid::from_string(actor_sid)?;
        let package = crate::appcontainer::derive(container)?;
        let package = crate::sid::sid_bytes(&package.string()?)?;
        verify_profile_token(source.raw(), &package, actor.as_bytes())?;
        crate::account_impersonation::with_token(source, || {
            // The token on the actual thread is checked, not just the offered
            // primary token. The scope also checks exact duplicate identity.
            verify_effective_profile(&package, actor.as_bytes())?;
            let raw = unsafe { GetAppContainerRegistryLocation((KEY_READ | KEY_WOW64_64KEY).0)? };
            ensure!(!raw.0.is_null(), "SDK returned an empty registry handle");
            let guard = Self { key: Some(raw) };
            crate::account_namespace::verify_noninheritable(HANDLE(raw.0))?;
            ensure!(
                matches!(guard.observe_native()?, MetadataObservation::Present(_)),
                "new SDK registry observation is unavailable"
            );
            Ok(guard)
        })
    }

    pub(crate) fn observe(&self) -> Result<MetadataObservation> {
        crate::account_impersonation::require_process_identity()?;
        self.observe_native()
    }

    fn observe_native(&self) -> Result<MetadataObservation> {
        let key = self.key.context("SDK registry handle already consumed")?;
        let mut buffer = QueryBuffer::new();
        let mut returned = 0;
        let status = unsafe {
            query_key(
                HANDLE(key.0),
                3, // KeyNameInformation
                buffer.words.as_mut_ptr().cast(),
                &mut returned,
            )?
        };
        if status == KEY_DELETED {
            return Ok(MetadataObservation::OriginalKeyDeleted);
        }
        status
            .ok()
            .context("observe original SDK registry key name")?;
        buffer.length = returned as usize;
        let name = bounded_key_name(buffer.bytes()?)?.to_vec();
        // KeyNameInformation can retain a cached full path after RegRenameKey.
        // Query the key's own current name separately for every retained node.
        let mut basic = QueryBuffer::new();
        unsafe {
            query_key(
                HANDLE(key.0),
                0, // KeyBasicInformation
                basic.words.as_mut_ptr().cast(),
                &mut returned,
            )?
        }
        .ok()
        .context("observe original registry key's current leaf name")?;
        basic.length = returned as usize;
        let leaf_name = bounded_basic_name(basic.bytes()?)?.to_vec();
        ensure!(
            name.ends_with(&leaf_name)
                && name.len() >= leaf_name.len() + 2
                && name[name.len() - leaf_name.len() - 2..name.len() - leaf_name.len()] == [92, 0],
            "registry current leaf differs from its reported full path"
        );
        let mut descriptor = QueryBuffer::new();
        unsafe {
            NtQuerySecurityObject(
                HANDLE(key.0),
                (OWNER_SECURITY_INFORMATION
                    | GROUP_SECURITY_INFORMATION
                    | DACL_SECURITY_INFORMATION)
                    .0,
                Some(PSECURITY_DESCRIPTOR(descriptor.words.as_mut_ptr().cast())),
                MAX_QUERY as u32,
                &mut returned,
            )
        }
        .ok()
        .context("observe original SDK registry owner/group/DACL")?;
        descriptor.length = returned as usize;
        let security = descriptor.bytes()?;
        ensure!(
            security.len() >= size_of::<SECURITY_DESCRIPTOR_RELATIVE>(),
            "truncated registry security descriptor"
        );
        Ok(MetadataObservation::Present(KeyMetadata {
            name,
            leaf_name,
            owner_group_dacl: security.to_vec(),
        }))
    }

    /// An uncertain close consumes the handle exactly once and returns an error;
    /// it must stop any enclosing cleanup. Drop never retries that close.
    pub(crate) fn close(mut self) -> Result<()> {
        close_once(&mut self.key, close_key)
    }
}

impl Drop for RetainedKey {
    fn drop(&mut self) {
        if close_once(&mut self.key, close_key).is_err() {
            // A destructor cannot return an uncertain-lifetime error to the
            // future composite cleanup owner. Do not continue that cleanup.
            std::process::abort();
        }
    }
}

fn close_key(key: HKEY) -> Result<()> {
    unsafe { RegCloseKey(key) }
        .ok()
        .context("close original SDK registry handle")
}
fn close_once(key: &mut Option<HKEY>, close: impl FnOnce(HKEY) -> Result<()>) -> Result<()> {
    if let Some(key) = key.take() {
        close(key)?;
    }
    Ok(())
}
fn bounded_key_name(bytes: &[u8]) -> Result<&[u8]> {
    ensure!(bytes.len() >= 4, "truncated registry name header");
    let length = u32::from_ne_bytes(bytes[..4].try_into()?) as usize;
    ensure!(
        length > 0 && length % 2 == 0 && length <= bytes.len() - 4,
        "registry name length exceeds returned information"
    );
    Ok(&bytes[4..4 + length])
}
fn bounded_basic_name(bytes: &[u8]) -> Result<&[u8]> {
    ensure!(
        bytes.len() >= 16,
        "truncated registry basic information header"
    );
    let length = u32::from_ne_bytes(bytes[12..16].try_into()?) as usize;
    ensure!(
        length > 0 && length % 2 == 0 && length <= 1024 && length <= bytes.len() - 16,
        "registry basic name exceeds bounded returned information"
    );
    let name = &bytes[16..16 + length];
    let units = name
        .chunks_exact(2)
        .map(|b| u16::from_le_bytes([b[0], b[1]]))
        .collect::<Vec<_>>();
    ensure!(
        !units.iter().any(|c| matches!(*c, 0 | 47 | 92)) && String::from_utf16(&units).is_ok(),
        "invalid registry basic leaf name"
    );
    Ok(name)
}
unsafe fn query_key(
    key: HANDLE,
    class: u32,
    buffer: *mut std::ffi::c_void,
    returned: &mut u32,
) -> Result<NTSTATUS> {
    type Query =
        unsafe extern "system" fn(HANDLE, u32, *mut std::ffi::c_void, u32, *mut u32) -> NTSTATUS;
    let module = unsafe { GetModuleHandleW(windows::core::w!("ntdll.dll"))? };
    let function = unsafe { GetProcAddress(module, windows::core::s!("NtQueryKey")) }
        .context("NtQueryKey unavailable")?;
    let query: Query = unsafe { std::mem::transmute(function) };
    Ok(unsafe { query(key, class, buffer, MAX_QUERY as u32, returned) })
}

#[path = "account_profile_registry_tree.rs"]
mod tree;
#[cfg(test)]
use tree::RegistryObservation;
#[cfg(test)]
pub(crate) use tree::fixture_is_writer_link_refusal;
pub(crate) use tree::{
    KeyObservation, ProfileRegistryRoots, RetainedProfileRegistry, RetainedRegistryParents,
    require_loaded_actor_classes,
};

#[cfg(test)]
#[path = "account_profile_registry_tests.rs"]
mod tests;
