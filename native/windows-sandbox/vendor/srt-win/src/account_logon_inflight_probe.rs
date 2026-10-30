//! Manually selected x64 diagnostic, not execution or recovery authority.
//! A private executable oplock holds the native creation call. An exact assembly
//! call frame distinguishes an outstanding call from a before/after marker.
//! No account is installed, no profile loaded and no child ever resumed.
use super::*;
use std::{os::windows::io::AsRawHandle, path::Path, sync::mpsc};
use windows::Win32::{
    Security::{
        Authorization::{SE_FILE_OBJECT, SetNamedSecurityInfoW},
        DACL_SECURITY_INFORMATION, GetSecurityDescriptorDacl, PROTECTED_DACL_SECURITY_INFORMATION,
    },
    System::{Diagnostics::Debug::*, IO::*, LibraryLoader::*},
};

pub(in crate::account_control) struct HeldOplock {
    file: OwnedHandle,
    event: OwnedHandle,
    overlapped: Box<OVERLAPPED>,
}
impl HeldOplock {
    pub(in crate::account_control) fn acquire(path: &Path) -> Result<Self> {
        let file = OwnedHandle(unsafe {
            CreateFileW(
                pcwstr(&wstr(path.to_str().context("non-Unicode probe image")?)),
                GENERIC_READ.0,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_OVERLAPPED | FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )?
        });
        let event = OwnedHandle(unsafe { CreateEventW(None, true, false, None)? });
        let mut overlapped = Box::new(OVERLAPPED {
            hEvent: event.raw(),
            ..Default::default()
        });
        let request = unsafe {
            DeviceIoControl(
                file.raw(),
                0x90000, // FSCTL_REQUEST_OPLOCK_LEVEL_1
                None,
                0,
                None,
                0,
                None,
                Some(&mut *overlapped),
            )
        };
        ensure!(
            request.as_ref().err().map(|e| e.code()) == Some(ERROR_IO_PENDING.to_hresult()),
            "private image oplock was not granted as pending: {request:?}"
        );
        Ok(Self {
            file,
            event,
            overlapped,
        })
    }
    pub(in crate::account_control) fn wait_for_break(&self) -> Result<()> {
        ensure!(
            unsafe { WaitForSingleObject(self.event.raw(), 5000) } == WAIT_OBJECT_0,
            "private image oplock did not break within bounds"
        );
        let mut returned = 0;
        unsafe { GetOverlappedResult(self.file.raw(), &*self.overlapped, &mut returned, false)? };
        Ok(())
    }
}
impl Drop for HeldOplock {
    fn drop(&mut self) {
        // Always drain the original overlapped request before its storage dies.
        // Closing the file afterward acknowledges the break and unblocks opens.
        unsafe {
            let cancel = CancelIoEx(self.file.raw(), Some(&*self.overlapped));
            if cancel
                .as_ref()
                .err()
                .is_some_and(|e| e.code() != ERROR_NOT_FOUND.to_hresult())
                || WaitForSingleObject(self.event.raw(), 5000) != WAIT_OBJECT_0
            {
                std::process::abort();
            }
            let mut returned = 0;
            let result =
                GetOverlappedResult(self.file.raw(), &*self.overlapped, &mut returned, false);
            if result
                .as_ref()
                .err()
                .is_some_and(|e| e.code() != ERROR_OPERATION_ABORTED.to_hresult())
            {
                std::process::abort();
            }
        }
    }
}

#[repr(C)]
#[derive(Clone, Copy, Default)]
struct ObservedCall {
    args: [usize; 11],
    function: usize,
    return_slot: usize,
    return_address: usize,
}

// `invoke` uses these exact offsets for either native API. Keep the frame
// checked in every build, including the ordinary nonignored control.
const _: () = {
    assert!(size_of::<usize>() == 8);
    assert!(std::mem::offset_of!(ObservedCall, args) == 0);
    assert!(std::mem::offset_of!(ObservedCall, function) == 88);
    assert!(std::mem::offset_of!(ObservedCall, return_slot) == 96);
    assert!(std::mem::offset_of!(ObservedCall, return_address) == 104);
    assert!(size_of::<ObservedCall>() == 112);
};

unsafe fn invoke(call: &mut ObservedCall) -> i32 {
    let result: i32;
    // x64 Windows ABI: 32 bytes shadow space plus seven stack arguments,
    // rounded to 16-byte alignment. r12 is nonvolatile across the native call.
    // No function is called after return before rsp leaves this call frame.
    // The observer freezes this thread and reads through ReadProcessMemory;
    // it never creates a Rust reference to concurrently changing storage.
    unsafe {
        std::arch::asm!(
            "sub rsp, 96",
            "mov qword ptr [rsp - 8], 0",
            "lea rax, [rsp - 8]",
            "mov [r12 + 96], rax",
            "lea rax, [rip + 2f]",
            "mov [r12 + 104], rax",
            "mov rcx, [r12]",
            "mov rdx, [r12 + 8]",
            "mov r8, [r12 + 16]",
            "mov r9, [r12 + 24]",
            "mov rax, [r12 + 32]",
            "mov [rsp + 32], rax",
            "mov rax, [r12 + 40]",
            "mov [rsp + 40], rax",
            "mov rax, [r12 + 48]",
            "mov [rsp + 48], rax",
            "mov rax, [r12 + 56]",
            "mov [rsp + 56], rax",
            "mov rax, [r12 + 64]",
            "mov [rsp + 64], rax",
            "mov rax, [r12 + 72]",
            "mov [rsp + 72], rax",
            "mov rax, [r12 + 80]",
            "mov [rsp + 80], rax",
            "call qword ptr [r12 + 88]",
            "2:",
            "mov qword ptr [r12 + 96], 0",
            "add rsp, 96",
            in("r12") call,
            lateout("eax") result,
            clobber_abi("system"),
        );
    }
    result
}

fn protect(path: &Path) -> Result<()> {
    let owner = crate::sid::current_user_sid()?;
    let descriptor = OwnedSd::from_sddl(&format!("D:P(A;;FA;;;{owner})(A;;FA;;;SY)"))?;
    let mut dacl = std::ptr::null_mut();
    let mut present = false.into();
    let mut defaulted = false.into();
    unsafe {
        GetSecurityDescriptorDacl(descriptor.ptr, &mut present, &mut dacl, &mut defaulted)?;
        ensure!(present.as_bool() && !dacl.is_null(), "missing probe DACL");
        crate::util::win32_ok(
            SetNamedSecurityInfoW(
                pcwstr(&wstr(
                    path.to_str().context("non-Unicode private probe path")?,
                )),
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                Some(dacl),
                None,
            ),
            "protect synthetic logon probe",
        )?;
    }
    Ok(())
}

fn read_native<T: Copy + Default>(process: HANDLE, address: usize) -> Result<T> {
    ensure!(address != 0, "null observation address");
    let mut value = T::default();
    let mut read = 0;
    unsafe {
        ReadProcessMemory(
            process,
            address as *const _,
            (&mut value as *mut T).cast(),
            size_of::<T>(),
            Some(&mut read),
        )?;
    }
    ensure!(read == size_of::<T>(), "short native observation");
    Ok(value)
}

struct SuspendedThread(HANDLE, u32);
impl SuspendedThread {
    fn hold(thread: HANDLE) -> Result<Self> {
        let previous = unsafe { SuspendThread(thread) };
        ensure!(previous != u32::MAX, "cannot suspend original probe thread");
        let guard = Self(thread, previous);
        ensure!(previous == 0, "probe thread already suspended");
        Ok(guard)
    }
}
impl Drop for SuspendedThread {
    fn drop(&mut self) {
        if unsafe { ResumeThread(self.0) } != self.1 + 1 {
            std::process::abort();
        }
    }
}

pub(in crate::account_control) fn observe_outstanding(
    process: HANDLE,
    thread: HANDLE,
    address: usize,
) -> Result<(usize, usize)> {
    let _suspension = SuspendedThread::hold(thread)?;
    // The generated binding is only repr(C); x64 GetThreadContext requires a
    // 16-byte-aligned buffer even when only control registers are requested.
    #[repr(C, align(16))]
    struct AlignedContext(CONTEXT);
    let mut aligned = AlignedContext(CONTEXT {
        ContextFlags: CONTEXT_CONTROL_AMD64,
        ..Default::default()
    });
    unsafe { GetThreadContext(thread, &mut aligned.0) }
        .context("read original logon thread context")?;
    let context = &aligned.0;
    let call: ObservedCall =
        read_native(process, address).context("read original logon call frame")?;
    ensure!(
        call.return_slot != 0 && call.return_address != 0,
        "native call frame not published"
    );
    let saved: usize =
        read_native(process, call.return_slot).context("read native call return slot")?;
    ensure!(
        saved == call.return_address,
        "native CALL has not installed its exact return address"
    );
    ensure!(
        context.Rsp as usize <= call.return_slot
            && call.return_slot - context.Rsp as usize <= 1024 * 1024
            && context.Rip as usize != call.return_address,
        "thread is outside the outstanding native call frame"
    );
    Ok((
        call.return_slot - context.Rsp as usize,
        context.Rip as usize,
    ))
}

fn synthetic_logon(path: &Path, before: impl FnOnce(usize) -> Result<()>) -> Result<SpawnedChild> {
    let password = wstr("NotARealCredential!SyntheticOnly");
    observed_logon(
        path,
        "AlysisSyntheticNeverValidated",
        &password,
        LOGON_NETCREDENTIALS_ONLY,
        before,
    )
}

/// Ordinary control under the filtered worker's current primary token. It
/// exercises the original-handle observer without manufacturing a second logon
/// session. Actual-account qualification uses `observed_logon` separately.
pub(in crate::account_control) fn observed_same_user_create(
    path: &Path,
    before: impl FnOnce(usize) -> Result<()>,
) -> Result<SpawnedChild> {
    ensure!(
        path.is_absolute() && path.to_str().is_some_and(|p| !p.contains('\0')),
        "invalid observed image path"
    );
    let app = wstr(path.to_str().unwrap());
    let mut command = wstr(&crate::launch::quote_arg(path.to_str().unwrap()));
    let cwd = wstr(&os_directory(false)?);
    let startup = STARTUPINFOW {
        cb: size_of::<STARTUPINFOW>() as u32,
        ..Default::default()
    };
    let mut info = PROCESS_INFORMATION::default();
    let module = unsafe { GetModuleHandleW(windows::core::w!("kernel32.dll"))? };
    let function = unsafe { GetProcAddress(module, windows::core::s!("CreateProcessW")) }
        .context("native same-user creation export missing")?;
    let mut call = ObservedCall {
        // CreateProcessW has ten arguments. The observer's eleventh x64 stack
        // slot is unused; all handles stay noninheritable and the original
        // strict job is inherited normally, with no breakaway or assignment.
        args: [
            app.as_ptr() as usize,                            // lpApplicationName
            command.as_mut_ptr() as usize,                    // lpCommandLine
            0,                                                // lpProcessAttributes
            0,                                                // lpThreadAttributes
            0,                                                // bInheritHandles
            (CREATE_SUSPENDED | CREATE_NO_WINDOW).0 as usize, // dwCreationFlags
            0,                                                // lpEnvironment
            cwd.as_ptr() as usize,                            // lpCurrentDirectory
            &startup as *const _ as usize,                    // lpStartupInfo
            &mut info as *mut _ as usize,                     // lpProcessInformation
            0,                                                // unused eleventh slot
        ],
        function: function as usize,
        ..Default::default()
    };
    before(&mut call as *mut _ as usize)?;
    let result = unsafe { invoke(&mut call) };
    let error = unsafe { GetLastError() };
    ensure!(
        result != 0,
        "observed same-user creation failed after release: {}",
        error.0
    );
    let child = SpawnedChild::new(info);
    let image = PinnedRunnerImage::fixture(path)?;
    image.verify_process(child.process())?;
    Ok(child)
}

/// Test-only native call observer. Real synthetic-account credentials remain
/// borrowed from the authenticated harness's scrubbed buffer; none are stored
/// in diagnostics, the environment, command line or filesystem.
pub(in crate::account_control) fn observed_logon(
    path: &Path,
    user: &str,
    password: &[u16],
    flags: CREATE_PROCESS_LOGON_FLAGS,
    before: impl FnOnce(usize) -> Result<()>,
) -> Result<SpawnedChild> {
    ensure!(
        path.is_absolute() && path.to_str().is_some_and(|p| !p.contains('\0')),
        "invalid observed image path"
    );
    ensure!(
        !user.is_empty() && user.len() <= 64 && !user.contains('\0'),
        "invalid observed user shape"
    );
    ensure!(
        (2..=128).contains(&password.len())
            && password.last() == Some(&0)
            && !password[..password.len() - 1].contains(&0),
        "invalid observed credential shape"
    );
    ensure!(
        flags.0 == 0 || flags == LOGON_NETCREDENTIALS_ONLY,
        "unsupported observed logon flags"
    );
    let user = wstr(user);
    let domain = wstr(".");
    let app = wstr(path.to_str().unwrap());
    let mut command = wstr(&crate::launch::quote_arg(path.to_str().unwrap()));
    let cwd = wstr(&os_directory(false)?);
    let startup = STARTUPINFOW {
        cb: size_of::<STARTUPINFOW>() as u32,
        ..Default::default()
    };
    let mut info = PROCESS_INFORMATION::default();
    let module = unsafe { GetModuleHandleW(windows::core::w!("advapi32.dll"))? };
    let function = unsafe { GetProcAddress(module, windows::core::s!("CreateProcessWithLogonW")) }
        .context("native logon export missing")?;
    let mut call = ObservedCall {
        args: [
            user.as_ptr() as usize,
            domain.as_ptr() as usize,
            password.as_ptr() as usize,
            flags.0 as usize,
            app.as_ptr() as usize,
            command.as_mut_ptr() as usize,
            (CREATE_SUSPENDED | CREATE_NO_WINDOW).0 as usize,
            0,
            cwd.as_ptr() as usize,
            &startup as *const _ as usize,
            &mut info as *mut _ as usize,
        ],
        function: function as usize,
        ..Default::default()
    };
    before(&mut call as *mut _ as usize)?;
    let result = unsafe { invoke(&mut call) };
    let error = unsafe { GetLastError() };
    ensure!(
        result != 0,
        "observed logon failed after release: {}",
        error.0
    );
    let child = SpawnedChild::new(info);
    let image = PinnedRunnerImage::fixture(&path)?;
    image.verify_process(child.process())?;
    Ok(child)
}

#[test]
fn observed_logon_rejects_invalid_inputs_before_callback_or_native_call() {
    let path = std::env::current_exe().unwrap();
    let valid = wstr("SyntheticOnly!");
    for (path, user, password, flags) in [
        (
            Path::new("relative.exe"),
            "aQAinvalid",
            valid.as_slice(),
            CREATE_PROCESS_LOGON_FLAGS(0),
        ),
        (
            path.as_path(),
            "",
            valid.as_slice(),
            CREATE_PROCESS_LOGON_FLAGS(0),
        ),
        (
            path.as_path(),
            "aQAinvalid",
            &[][..],
            CREATE_PROCESS_LOGON_FLAGS(0),
        ),
        (
            path.as_path(),
            "aQAinvalid",
            &[65, 0, 66, 0][..],
            CREATE_PROCESS_LOGON_FLAGS(0),
        ),
        (
            path.as_path(),
            "aQAinvalid",
            valid.as_slice(),
            CREATE_PROCESS_LOGON_FLAGS(8),
        ),
    ] {
        assert!(
            observed_logon(path, user, password, flags, |_| panic!(
                "invalid input reached native call preparation"
            ))
            .is_err()
        );
    }
}

#[test]
#[ignore = "bounded manual native oplock diagnostic; not logon death qualification"]
fn bootstrap_secondary_logon_oplock_outstanding_diagnostic() {
    assert_eq!(std::mem::offset_of!(ObservedCall, return_slot), 96);
    assert_eq!(std::mem::offset_of!(ObservedCall, return_address), 104);
    let root = crate::util::test_temp_dir().join(format!(
        "logon-inflight-{}",
        crate::account_state::execution::random_id().unwrap()
    ));
    std::fs::create_dir(&root).unwrap();
    protect(&root).unwrap();
    let path = root.join("never-resumed.exe");
    std::fs::copy(std::env::current_exe().unwrap(), &path).unwrap();
    protect(&path).unwrap();
    // The temporary validation pin is released before requesting an exclusive
    // oplock. This diagnostic therefore does not qualify production image pins.
    drop(PinnedRunnerImage::fixture(&path).unwrap());
    let oplock = HeldOplock::acquire(&path).unwrap();
    assert_eq!(
        unsafe { WaitForSingleObject(oplock.event.raw(), 0) },
        WAIT_TIMEOUT
    );
    let (send, receive) = mpsc::sync_channel(1);
    let worker = std::thread::spawn(move || -> Result<()> {
        let child = synthetic_logon(&path, |address| {
            send.send(address)?;
            Ok(())
        })?;
        drop(child);
        Ok(())
    });
    let checked = (|| -> Result<_> {
        let address = receive.recv_timeout(Duration::from_secs(5))?;
        oplock.wait_for_break()?;
        let observation = observe_outstanding(
            unsafe { GetCurrentProcess() },
            HANDLE(worker.as_raw_handle()),
            address,
        )?;
        ensure!(
            !worker.is_finished(),
            "native call completed while oplock held"
        );
        Ok(observation)
    })();
    drop(oplock); // release on both success and every ordinary error path
    let status = unsafe { WaitForSingleObject(HANDLE(worker.as_raw_handle()), 10000) };
    if status != WAIT_OBJECT_0 {
        std::process::abort();
    }
    worker.join().unwrap().unwrap();
    let (depth, _) = checked.unwrap();
    eprintln!(
        "secondary_logon_oplock_observation={}",
        serde_json::json!({
            "outstanding_native_call_observed":true,"native_stack_depth":depth,
            "oplock_released":true,"child_created_after_release":true,"child_never_resumed":true,
            "original_child_exit_verified":true,"synthetic_credentials_unvalidated":true,
            "broker_death_tested":false,"inflight_death_qualified":false,"production_ready":false
        })
    );
}

#[path = "account_logon_death_probe.rs"]
mod death;
#[path = "account_process_event_probe.rs"]
pub(in crate::account_control) mod process_events;
