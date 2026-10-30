//! Private native adapter, not an execution or journal authority. The unused
//! desktop coordinator commits intent before birth and retains this original
//! handle until the owning job drains. No caller receives a launch path yet.
//!
//! CreateDesktopW silently opens existing objects and does not distinguish that
//! outcome with GetLastError. Resolve the Windows 10+ native create export and
//! omit OBJ_OPENIF instead; unsupported systems fail without a Win32 fallback.
use super::super::resources::{namespace_native as security, station::Station};
use super::*;
use crate::{
    acl::object_grants::Snapshot,
    util::{OwnedSd, counted_utf16},
};
use std::mem::size_of;
use windows::{
    Wdk::{Foundation::OBJECT_ATTRIBUTES, Storage::FileSystem::NtSetSecurityObject},
    Win32::{
        Foundation::*,
        Security::*,
        System::{
            LibraryLoader::{GetModuleHandleW, GetProcAddress},
            RemoteDesktop::ProcessIdToSessionId,
            StationsAndDesktops::*,
            Threading::{GetCurrentProcess, GetCurrentThreadId},
        },
    },
    core::{PWSTR, s, w},
};

const DESKTOP_ALL: u32 = 0xf01ff;
// This is a fixed never-resumed bootstrap desktop. No switch, hooks, journaling
// or descriptor mutation is granted to the package. Command use is unqualified.
const PACKAGE_ACCESS: u32 = 0x20083; // READ_CONTROL, READ/WRITEOBJECTS, CREATEWINDOW
const QUERY_ACCESS: u32 = 0x20081; // READ_CONTROL + required READ/WRITEOBJECTS
const PARENT_ACCESS: u32 = 0x2000a; // READ_CONTROL, READATTRIBUTES, CREATEDESKTOP
const FLAGS: OBJECT_ATTRIBUTE_FLAGS =
    OBJECT_ATTRIBUTE_FLAGS(OBJ_CASE_INSENSITIVE.0 | OBJ_DONT_REPARSE.0);

type Create = unsafe extern "system" fn(
    *const OBJECT_ATTRIBUTES,
    *const UNICODE_STRING,
    *const core::ffi::c_void,
    u32,
    u32,
    u32,
) -> HDESK;
type Open = unsafe extern "system" fn(*const OBJECT_ATTRIBUTES, u32, u32) -> HDESK;

struct Api {
    create: Create,
    open: Open,
}
impl Api {
    fn load() -> Result<Self> {
        // USER initialization loads the system win32u module. Do not search the
        // working directory or load an arbitrary DLL to satisfy a missing API.
        unsafe { GetProcessWindowStation() }?;
        let module = unsafe { GetModuleHandleW(w!("win32u.dll")) }
            .context("native exclusive desktop API unavailable")?;
        let create = unsafe { GetProcAddress(module, s!("NtUserCreateDesktopEx")) }
            .context("native exclusive desktop create unavailable")?;
        let open = unsafe { GetProcAddress(module, s!("NtUserOpenDesktop")) }
            .context("native relative desktop open unavailable")?;
        Ok(Self {
            create: unsafe { std::mem::transmute(create) },
            open: unsafe { std::mem::transmute(open) },
        })
    }
}

/// Owns only independently opened USER handles. Drop closes them without
/// recording retirement, changing a shared DACL, or switching the input desktop.
pub(in crate::account_state::execution) struct NativeDesktop {
    handle: Option<HDESK>,
    parent: Station,
    subject: Subject,
    api: Api,
}
impl Drop for NativeDesktop {
    fn drop(&mut self) {
        if let Some(handle) = self.handle.take() {
            let _ = unsafe { CloseDesktop(handle) };
        }
    }
}

// Native policy only: neither variant carries live job/journal authority.
// Keep full original validation and closed naming domains, never convert a
// paired plan into a legacy plan to share native Windows operations.
#[derive(Clone)]
enum Subject {
    Atomic(Plan),
    Paired(super::super::paired_plan::Plan),
}
impl Subject {
    fn validate(&self) -> Result<()> {
        match self {
            Self::Atomic(p) => p.validate(),
            Self::Paired(p) => p.validate(),
        }
    }
    fn execution(&self) -> &ExecutionRecord {
        match self {
            Self::Atomic(p) => &p.execution,
            Self::Paired(p) => p.desktop_subject().0,
        }
    }
    fn session(&self) -> u32 {
        match self {
            Self::Atomic(p) => p.session,
            Self::Paired(p) => p.desktop_subject().2,
        }
    }
    fn name(&self) -> Result<String> {
        self.validate()?;
        Ok(match self {
            Self::Atomic(p) => format!("alysis-atomic-desktop-{}", p.operation),
            Self::Paired(p) => format!("alysis-paired-desktop-{}", p.desktop_subject().1),
        })
    }
}
fn actor(subject: &Subject) -> Result<()> {
    subject.validate()?;
    let execution = subject.execution();
    crate::account_impersonation::require_process_identity()?;
    let mut session = 0;
    unsafe { ProcessIdToSessionId(std::process::id(), &mut session) }?;
    ensure!(
        execution.pid == std::process::id()
            && execution.created == process_created(unsafe { GetCurrentProcess() })?
            && execution.owner == crate::sid::current_user_sid()?
            && subject.session() == session,
        "desktop actor differs from original plan"
    );
    Ok(())
}
pub(super) fn name(plan: &Plan) -> Result<String> {
    Subject::Atomic(plan.clone()).name()
}
pub(in crate::account_state::execution) fn paired_name(
    plan: &super::super::paired_plan::Plan,
) -> Result<String> {
    Subject::Paired(plan.clone()).name()
}
fn subject_descriptor(subject: &Subject, published: bool, labelled: bool) -> Result<OwnedSd> {
    subject.validate()?;
    let owner = &subject.execution().owner;
    let package = &subject.execution().identity.package_sid;
    ensure!(
        owner != "S-1-5-18" && owner != package,
        "desktop subjects must be distinct"
    );
    let package = if published {
        format!("(A;;0x{PACKAGE_ACCESS:x};;;{package})")
    } else {
        String::new()
    };
    let label = if labelled { "S:(ML;;NW;;;LW)" } else { "" };
    OwnedSd::from_sddl(&format!(
        "O:{owner}D:P(A;;0x{DESKTOP_ALL:x};;;SY)(A;;0x{DESKTOP_ALL:x};;;{owner}){package}{label}"
    ))
}
#[cfg(test)]
fn descriptor(plan: &Plan, published: bool, labelled: bool) -> Result<OwnedSd> {
    subject_descriptor(&Subject::Atomic(plan.clone()), published, labelled)
}
fn bytes(sd: &OwnedSd) -> Result<&[u8]> {
    let size = unsafe { GetSecurityDescriptorLength(sd.ptr) } as usize;
    ensure!(
        (20..=65536).contains(&size),
        "desktop descriptor exceeds bound"
    );
    Ok(unsafe { std::slice::from_raw_parts(sd.ptr.0.cast(), size) })
}
fn attributes(
    name: &mut UNICODE_STRING,
    parent: HANDLE,
    sd: Option<&OwnedSd>,
) -> OBJECT_ATTRIBUTES {
    OBJECT_ATTRIBUTES {
        Length: size_of::<OBJECT_ATTRIBUTES>() as u32,
        RootDirectory: parent,
        ObjectName: name,
        Attributes: FLAGS,
        SecurityDescriptor: sd.map_or(std::ptr::null(), |s| s.ptr.0.cast()),
        ..Default::default()
    }
}
fn relative_open(api: &Api, parent: HANDLE, text: &str) -> Result<HDESK> {
    let (mut buffer, length) = counted_utf16(text);
    let mut name = UNICODE_STRING {
        Length: length,
        MaximumLength: length,
        Buffer: PWSTR(buffer.as_mut_ptr()),
    };
    let raw = unsafe { (api.open)(&attributes(&mut name, parent, None), 0, QUERY_ACCESS) };
    if raw.is_invalid() {
        return Err(windows::core::Error::from_thread().into());
    }
    Ok(raw)
}
impl NativeDesktop {
    pub(super) fn create(plan: &Plan) -> Result<Self> {
        Self::create_subject(Subject::Atomic(plan.clone()))
    }
    pub(in crate::account_state::execution) fn create_paired(
        plan: &super::super::paired_plan::Plan,
    ) -> Result<Self> {
        Self::create_subject(Subject::Paired(plan.clone()))
    }
    fn create_subject(subject: Subject) -> Result<Self> {
        actor(&subject)?;
        let text = subject.name()?;
        let private = subject_descriptor(&subject, false, false)?;
        let low_private = subject_descriptor(&subject, false, true)?;
        let published = subject_descriptor(&subject, true, true)?;
        let api = Api::load()?;
        let parent = Station::open_current("WinSta0", PARENT_ACCESS)?;
        parent.verify_current()?;
        let original_thread = unsafe { GetThreadDesktop(GetCurrentThreadId()) }?;
        let (mut buffer, length) = counted_utf16(&text);
        let mut name = UNICODE_STRING {
            Length: length,
            MaximumLength: length,
            Buffer: PWSTR(buffer.as_mut_ptr()),
        };
        let raw = unsafe {
            (api.create)(
                &attributes(&mut name, parent.raw(), Some(&private)),
                std::ptr::null(),
                std::ptr::null(),
                0,
                DESKTOP_ALL,
                0,
            )
        };
        if raw.is_invalid() {
            return Err(windows::core::Error::from_thread())
                .context("exclusive desktop birth refused; preserve intent");
        }
        let guard = Self {
            handle: Some(raw),
            parent,
            subject,
            api,
        };
        ensure!(
            raw != original_thread
                && unsafe { GetThreadDesktop(GetCurrentThreadId()) }? == original_thread,
            "native desktop birth changed current thread attachment"
        );
        guard.verify_reference()?;
        ensure!(
            Snapshot::read(HANDLE(raw.0))? == Snapshot::from_raw_descriptor(bytes(&private)?)?,
            "private desktop birth DACL differs"
        );
        #[cfg(test)]
        checkpoint(BirthStage::Private)?;
        unsafe {
            NtSetSecurityObject(HANDLE(raw.0), LABEL_SECURITY_INFORMATION.0, low_private.ptr)
        }
        .ok()
        .context("private desktop Low label unverified")?;
        guard.verify_policy(&low_private)?;
        #[cfg(test)]
        checkpoint(BirthStage::Labelled)?;
        unsafe {
            NtSetSecurityObject(
                HANDLE(raw.0),
                (DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION).0,
                published.ptr,
            )
        }
        .ok()
        .context("desktop package publication uncertain")?;
        guard.verify()?;
        #[cfg(test)]
        checkpoint(BirthStage::Published)?;
        Ok(guard)
    }
    fn verify_reference(&self) -> Result<()> {
        actor(&self.subject)?;
        self.parent.verify_current()?;
        let raw = self.handle.context("original desktop already closed")?;
        crate::account_namespace::verify_noninheritable(HANDLE(raw.0))?;
        let opened = relative_open(&self.api, self.parent.raw(), &self.subject.name()?)?;
        let same = unsafe { CompareObjectHandles(HANDLE(raw.0), HANDLE(opened.0)) }.as_bool();
        unsafe { CloseDesktop(opened) }.context("desktop comparison handle close uncertain")?;
        ensure!(same, "desktop name differs from original object");
        Ok(())
    }
    fn verify_policy(&self, wanted: &OwnedSd) -> Result<()> {
        self.verify_reference()?;
        let raw = self.handle.context("original desktop missing")?;
        ensure!(
            security::security_shape(&security::query_security(HANDLE(raw.0))?)?
                == security::security_shape(bytes(wanted)?)?,
            "desktop security differs from exact policy"
        );
        Ok(())
    }
    pub(in crate::account_state::execution) fn verify(&self) -> Result<()> {
        self.verify_policy(&subject_descriptor(&self.subject, true, true)?)
    }
    pub(in crate::account_state::execution) fn close(&mut self) -> Result<()> {
        self.verify()?;
        let raw = self
            .handle
            .take()
            .context("original desktop close already attempted")?;
        unsafe { CloseDesktop(raw) }.context("original desktop close uncertain; do not retry")?;
        self.require_absent()
    }
    pub(in crate::account_state::execution) fn verify_closed(&self) -> Result<()> {
        ensure!(self.handle.is_none(), "original desktop is still open");
        self.require_absent()
    }
    fn require_absent(&self) -> Result<()> {
        actor(&self.subject)?;
        self.parent.verify_current()?;
        match relative_open(&self.api, self.parent.raw(), &self.subject.name()?) {
            Ok(h) => {
                unsafe { CloseDesktop(h) }.context("remaining desktop observer close failed")?;
                anyhow::bail!("desktop remains referenced; preserve unresolved state")
            }
            Err(e) => {
                ensure!(
                    e.downcast_ref::<windows::core::Error>()
                        .is_some_and(|e| e.code()
                            == windows::core::HRESULT::from_win32(ERROR_FILE_NOT_FOUND.0)),
                    "desktop absence query uncertain"
                );
                Ok(())
            }
        }
    }
}

#[cfg(test)]
#[derive(Clone, Copy, PartialEq, Eq)]
enum BirthStage {
    Private,
    Labelled,
    Published,
}
#[cfg(test)]
thread_local! {static FAIL_STAGE:std::cell::Cell<Option<BirthStage>>=const {std::cell::Cell::new(None)};}
#[cfg(test)]
fn checkpoint(stage: BirthStage) -> Result<()> {
    ensure!(
        !FAIL_STAGE.with(|s| s.get() == Some(stage)),
        "injected desktop birth failure"
    );
    Ok(())
}

#[cfg(test)]
#[path = "account_bootstrap_desktop_native_tests.rs"]
mod tests;

#[cfg(test)]
pub(in crate::account_state::execution) struct DesktopObserver(Option<HDESK>);
#[cfg(test)]
impl Drop for DesktopObserver {
    fn drop(&mut self) {
        if let Some(h) = self.0.take() {
            let _ = unsafe { CloseDesktop(h) };
        }
    }
}
#[cfg(test)]
impl DesktopObserver {
    pub(in crate::account_state::execution) fn verify(&self) -> Result<()> {
        Snapshot::read(HANDLE(self.0.context("desktop observer already closed")?.0))?;
        Ok(())
    }
    pub(in crate::account_state::execution) fn close(mut self) -> Result<()> {
        let h = self
            .0
            .take()
            .context("desktop observer close already attempted")?;
        unsafe { CloseDesktop(h) }.context("desktop observer close uncertain")
    }
}
#[cfg(test)]
impl NativeDesktop {
    pub(in crate::account_state::execution) fn fixture_original_observer(
        &self,
    ) -> Result<DesktopObserver> {
        self.verify()?;
        let h = relative_open(&self.api, self.parent.raw(), &self.subject.name()?)?;
        let observer = DesktopObserver(Some(h));
        ensure!(
            unsafe {
                CompareObjectHandles(
                    HANDLE(h.0),
                    HANDLE(self.handle.context("original desktop missing")?.0),
                )
            }
            .as_bool(),
            "desktop observer differs from original"
        );
        Ok(observer)
    }
}
#[cfg(test)]
pub(in crate::account_state::execution) fn fixture_paired_absent(
    plan: &super::super::paired_plan::Plan,
) -> Result<()> {
    let subject = Subject::Paired(plan.clone());
    actor(&subject)?;
    fixture_observe_absent(&subject)
}
// An original controller observes the dead broker's name in the same owner/session.
// It is not the broker actor and cannot obtain a NativeDesktop, mutate an object,
// reconstruct a guard or write a completion receipt through this test-only API.
#[cfg(test)]
pub(in crate::account_state::execution) fn fixture_paired_absent_from_controller(
    plan: &super::super::paired_plan::Plan,
) -> Result<()> {
    let subject = Subject::Paired(plan.clone());
    fixture_observer_actor(&subject)?;
    fixture_observe_absent(&subject)
}
#[cfg(test)]
fn fixture_observer_actor(subject: &Subject) -> Result<()> {
    subject.validate()?;
    crate::account_impersonation::require_process_identity()?;
    let mut session = 0;
    unsafe { ProcessIdToSessionId(std::process::id(), &mut session) }?;
    ensure!(
        subject.execution().owner == crate::sid::current_user_sid()?
            && subject.session() == session,
        "desktop observation requires the original owner and session"
    );
    Ok(())
}
#[cfg(test)]
fn fixture_observe_absent(subject: &Subject) -> Result<()> {
    let parent = Station::open_current("WinSta0", PARENT_ACCESS)?;
    match relative_open(&Api::load()?, parent.raw(), &subject.name()?) {
        Ok(h) => {
            unsafe { CloseDesktop(h) }?;
            anyhow::bail!("paired desktop still present")
        }
        Err(e) => {
            ensure!(
                e.downcast_ref::<windows::core::Error>().is_some_and(
                    |e| e.code() == windows::core::HRESULT::from_win32(ERROR_FILE_NOT_FOUND.0)
                ),
                "paired desktop absence uncertain"
            );
            Ok(())
        }
    }
}
