//! Shared original-object SDK primitive, not journal or cleanup authority.
//! Legacy and atomic coordinators independently gate its fixed host operation.
use super::*;
use crate::account_profile_filesystem::{
    FilesystemObservation, RetainedProfileFilesystem, RetainedProfileParent,
};
use crate::account_profile_registry::{
    KeyObservation, RetainedProfileRegistry, RetainedRegistryParents,
};
use crate::util::OwnedHandle;
use std::mem::size_of;
use windows::Wdk::Foundation::OBJECT_ATTRIBUTES;
use windows::Win32::Foundation::{CloseHandle, HANDLE, NTSTATUS};
use windows::Win32::Security::*;
use windows::Win32::System::LibraryLoader::{GetModuleHandleW, GetProcAddress};
use windows::core::{HRESULT, PCWSTR};

#[cfg(test)]
thread_local! { static EFFECT_COUNTS: std::cell::Cell<(usize, usize, usize)> = const {std::cell::Cell::new((0,0,0))}; }

#[derive(Default)]
pub(in crate::account_state::execution) struct NativeProfile {
    actor: Option<OriginalActor>,
    delete_attempted: bool,
    #[cfg(test)]
    create_calls: usize,
    #[cfg(test)]
    close_calls: usize,
    #[cfg(test)]
    delete_calls: usize,
    token: Option<OwnedHandle>,
    files: Option<RetainedProfileFilesystem>,
    registry: Option<RetainedProfileRegistry>,
    file_parent: Option<RetainedProfileParent>,
    registry_parents: Option<RetainedRegistryParents>,
}

struct OriginalActor {
    container: String,
    owner: String,
    pid: u32,
    created: u64,
    session: u32,
}

/// Original roots and token retained while descendants may change. This has no
/// deletion API; only a checked recapture can return the frozen native guard.
pub(in crate::account_state::execution) struct UsedNativeProfile {
    native: NativeProfile,
    files: crate::account_profile_filesystem::ProfileFilesystemRoot,
    registry: crate::account_profile_registry::ProfileRegistryRoots,
    folder_id: [u8; 24],
}
impl UsedNativeProfile {
    #[cfg(test)]
    pub(in crate::account_state::execution) fn fixture_require_registry_data(&self) -> Result<()> {
        self.verify()?;
        self.registry.fixture_require_runtime_value()
    }

    #[cfg(test)]
    pub(in crate::account_state::execution) fn fixture_add_registry_data(
        &self,
        unsupported: bool,
    ) -> Result<()> {
        self.verify()?;
        self.registry.fixture_add_data(unsupported)
    }

    pub(in crate::account_state::execution) fn verify(&self) -> Result<()> {
        self.native
            .actor
            .as_ref()
            .context("original profile actor missing")?
            .verify()?;
        ensure!(
            self.native.token.is_some()
                && self.native.files.is_none()
                && self.native.registry.is_none()
                && self.native.file_parent.is_none()
                && self.native.registry_parents.is_none()
                && !self.native.delete_attempted,
            "profile in-use guard has lost original ownership"
        );
        self.files.unchanged()?;
        self.registry.unchanged()?;
        self.files.unchanged()
    }
    // Read-only native recapture; the atomic coordinator independently requires
    // live original-job completion before reaching this primitive.
    pub(in crate::account_state::execution) fn capture(self) -> Result<NativeProfile> {
        self.verify()?;
        let mut native = self.native;
        native.files = Some(self.files.capture_after_use()?);
        native.registry = Some(self.registry.capture_after_use()?);
        ensure!(
            native.files.as_ref().unwrap().original_root_id()? == self.folder_id,
            "post-use profile root differs from exclusive birth"
        );
        native.verify_unused()?;
        Ok(native)
    }
}
impl OriginalActor {
    fn capture(container: &str, owner: &str) -> Result<Self> {
        crate::job::execution_name(container)?;
        crate::account_token::verify_raw_profile_actor(owner)?;
        let mut session = 0;
        unsafe {
            windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
                std::process::id(),
                &mut session,
            )?;
        }
        Ok(Self {
            container: container.into(),
            owner: owner.into(),
            pid: std::process::id(),
            created: super::super::super::process_created(unsafe {
                windows::Win32::System::Threading::GetCurrentProcess()
            })?,
            session,
        })
    }
    fn verify(&self) -> Result<()> {
        let now = Self::capture(&self.container, &self.owner)?;
        ensure!(
            now.pid == self.pid && now.created == self.created && now.session == self.session,
            "profile original host actor changed"
        );
        Ok(())
    }
}

impl ProfileAccess for NativeProfile {
    fn create(&mut self, identity: &Identity) -> Result<[u8; 24]> {
        verify_actor(identity)?;
        self.create_host(
            &identity.execution.identity.container,
            &identity.execution.owner,
        )
    }
    fn unchanged(&self) -> Result<()> {
        self.verify_unused()
    }
    fn close_trees(&mut self) -> Result<()> {
        self.close_original_trees()
    }
    fn delete(&mut self, identity: &Identity) -> Result<()> {
        verify_actor(identity)?;
        let actor = self
            .actor
            .as_ref()
            .context("original profile actor missing")?;
        ensure!(
            actor.container == identity.execution.identity.container
                && actor.owner == identity.execution.owner,
            "profile deletion identity differs from original birth"
        );
        self.delete_original()
    }
    fn absent(&self) -> Result<()> {
        self.verify_absent()
    }
}

impl NativeProfile {
    #[cfg(test)]
    pub(in crate::account_state::execution) fn fixture_write_registry(
        container: &str,
        owner: &str,
        nonce: &str,
        unsupported: bool,
    ) -> Result<()> {
        crate::account_install::validate_random_id(nonce)?;
        crate::account_profile_registry::require_loaded_actor_classes(owner)?;
        let token = observation_token(container)?;
        let registry =
            RetainedProfileRegistry::open(&token, container, owner)?.retain_roots_for_use()?;
        registry.fixture_writer_data(nonce, unsupported)?;
        // Unsupported content intentionally cannot be captured. This fixture
        // returns after original-root verification and closes observation
        // handles only; the owner must subsequently refuse cleanup.
        registry.unchanged()?;
        registry.fixture_close()?;
        close_token(token)
    }
    pub(in crate::account_state::execution) fn begin_use(mut self) -> Result<UsedNativeProfile> {
        self.verify_unused()?;
        let folder_id = self.files.as_ref().unwrap().original_root_id()?;
        let files = self.files.take().unwrap().retain_root_for_use()?;
        let registry = self.registry.take().unwrap().retain_roots_for_use()?;
        let value = UsedNativeProfile {
            native: self,
            files,
            registry,
            folder_id,
        };
        value.verify()?;
        Ok(value)
    }
    pub(in crate::account_state::execution) fn create_host(
        &mut self,
        container: &str,
        owner: &str,
    ) -> Result<[u8; 24]> {
        ensure!(
            self.actor.is_none()
                && self.token.is_none()
                && self.files.is_none()
                && self.registry.is_none()
                && self.file_parent.is_none()
                && self.registry_parents.is_none(),
            "profile native guard was already used"
        );
        self.actor = Some(OriginalActor::capture(container, owner)?);
        #[cfg(test)]
        {
            self.create_calls += 1;
            EFFECT_COUNTS.with(|s| {
                let (a, b, c) = s.get();
                s.set((a + 1, b, c));
            });
        }
        create_exclusive(container)?;
        self.token = Some(observation_token(container)?);
        self.registry = Some(RetainedProfileRegistry::open(
            self.token.as_ref().unwrap(),
            container,
            owner,
        )?);
        self.files = Some(RetainedProfileFilesystem::open(container, owner)?);
        self.unchanged()?;
        self.files.as_ref().unwrap().original_root_id()
    }
    pub(in crate::account_state::execution) fn verify_unused(&self) -> Result<()> {
        self.actor
            .as_ref()
            .context("original profile actor missing")?
            .verify()?;
        ensure!(
            self.token.is_some() && self.file_parent.is_none() && self.registry_parents.is_none(),
            "profile original storage scope is no longer open"
        );
        let files = self
            .files
            .as_ref()
            .context("profile filesystem guard missing")?;
        let registry = self
            .registry
            .as_ref()
            .context("profile registry guard missing")?;
        ensure!(
            matches!(files.observe()?, FilesystemObservation::Present),
            "profile files were deleted"
        );
        ensure!(
            matches!(registry.observe()?, KeyObservation::Present(_)),
            "profile keys were deleted"
        );
        // Cross-surface checks are sequential, never an atomic lock against the
        // trusted owner or administrator. Untrusted processes are not admitted.
        ensure!(
            matches!(files.observe()?, FilesystemObservation::Present),
            "profile files changed during observation"
        );
        Ok(())
    }
    pub(in crate::account_state::execution) fn close_original_trees(&mut self) -> Result<()> {
        self.unchanged()?;
        #[cfg(test)]
        {
            self.close_calls += 1;
            EFFECT_COUNTS.with(|s| {
                let (a, b, c) = s.get();
                s.set((a, b + 1, c));
            });
        }
        self.file_parent = Some(self.files.take().unwrap().close_tree_for_observation()?);
        self.registry_parents = Some(
            self.registry
                .take()
                .unwrap()
                .close_trees_for_observation()?,
        );
        close_token(self.token.take().context("profile token already closed")?)?;
        Ok(())
    }
    pub(in crate::account_state::execution) fn delete_original(&mut self) -> Result<()> {
        let actor = self
            .actor
            .as_ref()
            .context("original profile actor missing")?;
        actor.verify()?;
        ensure!(
            !self.delete_attempted
                && self.token.is_none()
                && self.files.is_none()
                && self.registry.is_none()
                && self.file_parent.is_some()
                && self.registry_parents.is_some(),
            "profile deletion requires checked original tree and token closure"
        );
        self.delete_attempted = true;
        #[cfg(test)]
        {
            self.delete_calls += 1;
            EFFECT_COUNTS.with(|s| {
                let (a, b, c) = s.get();
                s.set((a, b, c + 1));
            });
        }
        crate::appcontainer::delete(&actor.container)
            .context("SDK profile deletion uncertain; preserve journal without retry")
    }
    pub(in crate::account_state::execution) fn verify_absent(&self) -> Result<()> {
        self.actor
            .as_ref()
            .context("original profile actor missing")?
            .verify()?;
        ensure!(
            self.token.is_none() && self.files.is_none() && self.registry.is_none(),
            "profile still retains original storage handles"
        );
        let files = self
            .file_parent
            .as_ref()
            .context("profile original filesystem parent missing")?;
        let registry = self
            .registry_parents
            .as_ref()
            .context("profile original registry parents missing")?;
        files.observe_absent()?;
        registry.observe_absent()?;
        files.observe_absent()
    }
}

#[cfg(test)]
impl NativeProfile {
    pub(in crate::account_state::execution) fn fixture_effect_counts() -> (usize, usize, usize) {
        EFFECT_COUNTS.with(|s| s.get())
    }
    pub(super) fn counts(&self) -> (usize, usize, usize) {
        (self.create_calls, self.close_calls, self.delete_calls)
    }
}

fn close_token(token: OwnedHandle) -> Result<()> {
    unsafe { CloseHandle(token.into_raw()) }.context("profile token close uncertain; never retry")
}

fn create_exclusive(container: &str) -> Result<()> {
    crate::account_impersonation::require_process_identity()?;
    let derived = crate::appcontainer::derive(container)?;
    let name = crate::util::wstr(container);
    // Exact S_OK is required. The projection's generic success handling is not
    // enough for an exclusive-birth proof, and an existing profile is refused.
    #[link(name = "userenv")]
    unsafe extern "system" {
        #[link_name = "CreateAppContainerProfile"]
        fn create_profile(
            name: PCWSTR,
            display: PCWSTR,
            description: PCWSTR,
            capabilities: *const SID_AND_ATTRIBUTES,
            count: u32,
            sid: *mut PSID,
        ) -> HRESULT;
    }
    let mut actual = PSID::default();
    let status = unsafe {
        create_profile(
            PCWSTR(name.as_ptr()),
            PCWSTR(name.as_ptr()),
            PCWSTR(name.as_ptr()),
            std::ptr::null(),
            0,
            &mut actual,
        )
    };
    let same = !actual.0.is_null() && unsafe { EqualSid(actual, derived.raw()) }.is_ok();
    if !actual.0.is_null() {
        unsafe {
            FreeSid(actual);
        }
    }
    ensure!(
        status.0 == 0 && same,
        "exclusive profile birth/SID unverified (HRESULT 0x{:08x}); preserve intent",
        status.0 as u32
    );
    Ok(())
}

fn observation_token(container: &str) -> Result<OwnedHandle> {
    crate::account_impersonation::require_process_identity()?;
    let package = crate::appcontainer::derive(container)?;
    let source = OwnedHandle(crate::token::open_self_token()?);
    type Create = unsafe extern "system" fn(
        *mut HANDLE,
        HANDLE,
        u32,
        *const OBJECT_ATTRIBUTES,
        PSID,
        u32,
        *const SID_AND_ATTRIBUTES,
        u32,
        *const HANDLE,
    ) -> NTSTATUS;
    let module = unsafe { GetModuleHandleW(windows::core::w!("ntdll.dll"))? };
    let function = unsafe { GetProcAddress(module, windows::core::s!("NtCreateLowBoxToken")) }
        .context("NtCreateLowBoxToken unavailable")?;
    let create: Create = unsafe { std::mem::transmute(function) };
    let attributes = OBJECT_ATTRIBUTES {
        Length: size_of::<OBJECT_ATTRIBUTES>() as u32,
        ..Default::default()
    };
    let mut raw = HANDLE::default();
    let status = unsafe {
        create(
            &mut raw,
            source.raw(),
            (TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_IMPERSONATE).0,
            &attributes,
            package.raw(),
            0,
            std::ptr::null(),
            0,
            std::ptr::null(),
        )
    };
    status
        .ok()
        .context("create same-user zero-capability profile observation token")?;
    let token = OwnedHandle(raw);
    close_token(source)?;
    crate::account_namespace::verify_noninheritable(token.raw())?;
    // RetainedProfileRegistry::open checks the actual actor, package, zero
    // capabilities and thread impersonation before accessing SDK storage.
    Ok(token)
}
