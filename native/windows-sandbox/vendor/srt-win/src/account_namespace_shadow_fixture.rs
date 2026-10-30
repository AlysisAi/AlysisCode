//! Disposable comparison only: exclusive private root with the same fixed
//! session shadow used by the observed Windows2022 AppContainer constructor.
//! The read-only original shadow pin drops after the private root and both jobs.
use super::*;
use windows::Win32::System::LibraryLoader::{GetModuleHandleW, GetProcAddress};
pub(in crate::account_state::execution) struct FixtureShadow {
    handle: OwnedHandle,
    name: String,
}
impl FixtureShadow {
    pub(in crate::account_state::execution) fn open(
        identity: &impl NamespacePolicySource,
    ) -> Result<Self> {
        crate::account_impersonation::require_process_identity()?;
        ensure!(
            crate::sid::current_user_sid()? == identity.execution().owner,
            "shadow fixture requires original host owner"
        );
        let name = format!("\\Sessions\\{}\\BaseNamedObjects", identity.session());
        let handle = open_directory(&name, HANDLE::default(), QUERY_ACCESS)
            .map_err(|s| status_error(s, "open original session shadow without aliases"))?;
        let value = Self { handle, name };
        value.verify(identity)?;
        Ok(value)
    }
    pub(in crate::account_state::execution) fn verify(
        &self,
        identity: &impl NamespacePolicySource,
    ) -> Result<()> {
        crate::account_impersonation::require_process_identity()?;
        ensure!(
            self.name == format!("\\Sessions\\{}\\BaseNamedObjects", identity.session()),
            "shadow fixture session differs"
        );
        crate::account_namespace::verify_noninheritable(self.handle.raw())?;
        ensure!(
            object_name::directory_name(self.handle.raw())? == self.name,
            "shadow fixture canonical object differs"
        );
        let current = open_directory(&self.name, HANDLE::default(), QUERY_ACCESS)
            .map_err(|s| status_error(s, "verify original session shadow"))?;
        ensure!(
            unsafe { CompareObjectHandles(self.handle.raw(), current.raw()) }.as_bool(),
            "shadow fixture original object changed"
        );
        unsafe { CloseHandle(current.into_raw()) }.context("shadow observation close uncertain")?;
        Ok(())
    }
    pub(super) fn create(
        &self,
        identity: &impl NamespacePolicySource,
        output: &mut HANDLE,
        attrs: &OBJECT_ATTRIBUTES,
    ) -> Result<NTSTATUS> {
        self.verify(identity)?;
        ensure!(
            attrs.Attributes == OBJECT_FLAGS,
            "shadow birth cannot adopt or inherit"
        );
        type Create = unsafe extern "system" fn(
            *mut HANDLE,
            u32,
            *const OBJECT_ATTRIBUTES,
            HANDLE,
            u32,
        ) -> NTSTATUS;
        let module = unsafe { GetModuleHandleW(windows::core::w!("ntdll.dll"))? };
        let function =
            unsafe { GetProcAddress(module, windows::core::s!("NtCreateDirectoryObjectEx")) }
                .context("shadow directory constructor unavailable")?;
        let create: Create = unsafe { std::mem::transmute(function) };
        // Flags=1 is observed in the exact hosted KernelBase image. Keep the
        // caller's private SD and strict no-OPENIF attributes, unlike OS OPENIF.
        Ok(unsafe { create(output, DIRECTORY_ALL, attrs, self.handle.raw(), 1) })
    }
}
