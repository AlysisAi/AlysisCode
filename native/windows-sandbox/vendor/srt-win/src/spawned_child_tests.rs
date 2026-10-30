use super::*;
use windows::Win32::Foundation::{
    DUPLICATE_HANDLE_OPTIONS, DUPLICATE_SAME_ACCESS, DuplicateHandle, ERROR_ACCESS_DENIED,
    WAIT_TIMEOUT,
};
use windows::Win32::System::Threading::{
    CreateProcessW, GetCurrentProcess, GetProcessId, PROCESS_SYNCHRONIZE,
};

fn suspended_fixture() -> SpawnedChild {
    // Fixed Windows binary, no user commands, no inherited handles. Never resumed.
    let mut directory = [0u16; 32768];
    let length = unsafe {
        windows::Win32::System::SystemInformation::GetSystemDirectoryW(Some(&mut directory))
    } as usize;
    assert!(length > 0 && length < directory.len());
    let executable = format!(
        "{}\\cmd.exe",
        String::from_utf16(&directory[..length]).unwrap()
    );
    let app = wstr(&executable);
    let mut command = wstr(&format!("{} /d /c exit 0", quote_arg(&executable)));
    let info = STARTUPINFOW {
        cb: size_of::<STARTUPINFOW>() as u32,
        ..Default::default()
    };
    let mut pi = PROCESS_INFORMATION::default();
    unsafe {
        CreateProcessW(
            pcwstr(&app),
            Some(PWSTR(command.as_mut_ptr())),
            None,
            None,
            false,
            CREATE_SUSPENDED | CREATE_NO_WINDOW,
            None,
            None,
            &info,
            &mut pi,
        )
        .expect("create fixed suspended process fixture");
    }
    SpawnedChild::new(pi)
}

fn duplicate(process: HANDLE) -> OwnedHandle {
    let mut result = HANDLE::default();
    unsafe {
        DuplicateHandle(
            GetCurrentProcess(),
            process,
            GetCurrentProcess(),
            &mut result,
            0,
            false,
            DUPLICATE_SAME_ACCESS,
        )
        .unwrap();
    }
    OwnedHandle(result)
}

#[test]
fn spawned_child_drop_observes_suspended_process_exit_before_returning() {
    // A separate retained reference lets the test check the exact original
    // object after guard destruction. Repetition exposes asynchronous exit;
    // each fixture is reaped before a failed assertion can unwind.
    for _ in 0..32 {
        let child = suspended_fixture();
        let observer = duplicate(child.process());
        assert_eq!(
            unsafe { WaitForSingleObject(observer.raw(), 0) },
            WAIT_TIMEOUT
        );
        drop(child);
        let immediate = unsafe { WaitForSingleObject(observer.raw(), 0) };
        assert_eq!(
            unsafe { WaitForSingleObject(observer.raw(), 5000) },
            WAIT_OBJECT_0
        );
        assert_eq!(
            immediate, WAIT_OBJECT_0,
            "cleanup returned before the process exited"
        );
    }
}

#[test]
fn spawned_child_already_exited_is_proven_by_original_handle_even_with_code_259() {
    let child = suspended_fixture();
    let observer = duplicate(child.process());
    unsafe {
        TerminateProcess(child.process(), 259).unwrap();
    }
    wait_for_process_exit(observer.raw(), 5000).unwrap();
    let mut code = 0;
    unsafe { GetExitCodeProcess(observer.raw(), &mut code).unwrap() };
    assert_eq!(code, 259);
    // Windows rejects another termination after exit. This must not cause a
    // false fatal error: the retained object's signaled state is the authority.
    assert_eq!(
        unsafe { TerminateProcess(child.process(), 1) }
            .unwrap_err()
            .code(),
        ERROR_ACCESS_DENIED.to_hresult()
    );
    drop(child);
    wait_for_process_exit(observer.raw(), 0).unwrap();
}

fn wait_only_handle(child: &SpawnedChild, inherit: bool) -> OwnedHandle {
    let mut result = HANDLE::default();
    unsafe {
        DuplicateHandle(
            GetCurrentProcess(),
            child.process(),
            GetCurrentProcess(),
            &mut result,
            PROCESS_SYNCHRONIZE.0,
            inherit,
            DUPLICATE_HANDLE_OPTIONS(0),
        )
        .unwrap();
    }
    OwnedHandle(result)
}

#[test]
fn spawned_child_wait_timeout_and_denied_termination_do_not_prove_exit() {
    let child = suspended_fixture();
    let read_only = wait_only_handle(&child, false);
    assert!(wait_for_process_exit(read_only.raw(), 1).is_err());
    let denied = unsafe { TerminateProcess(read_only.raw(), 1) }.unwrap_err();
    assert_eq!(denied.code(), ERROR_ACCESS_DENIED.to_hresult());
    let error = terminate_and_wait_for_child_with_timeout(read_only.raw(), 10).unwrap_err();
    assert!(
        error
            .to_string()
            .contains("termination request also failed")
    );
    assert_eq!(
        unsafe { WaitForSingleObject(child.process(), 0) },
        WAIT_TIMEOUT
    );
    let observer = duplicate(child.process());
    drop(child); // The separate original guard still has termination authority.
    wait_for_process_exit(observer.raw(), 0).unwrap();
    assert!(wait_for_process_exit(HANDLE::default(), 0).is_err());
}

const FAILURE_FIXTURE: &str = "launch::spawned_child_tests::spawned_child_failure_fixture";
const FIXTURE_HANDLE: &str = "ALYSIS_TEST_SPAWNED_CHILD_HANDLE";
const FIXTURE_MARKER: &str = "ALYSIS_TEST_SPAWNED_CHILD_MARKER";

#[test]
#[ignore = "bounded subprocess fixture invoked by spawned_child_drop_failure_stops_outer_cleanup"]
fn spawned_child_failure_fixture() {
    // Only the test binary exposes this entry. Its parent supplies a wait-only
    // reference, never mutation authority, to a never-resumed synthetic child.
    // Zero is the separate invalid-handle case; no pseudo-handle is admitted.
    let value: usize = std::env::var(FIXTURE_HANDLE).unwrap().parse().unwrap();
    assert!(value == 0 || value < isize::MAX as usize);
    let marker = std::path::PathBuf::from(std::env::var_os(FIXTURE_MARKER).unwrap());
    assert!(marker.is_absolute() && marker.file_name().unwrap() == "outer-cleanup-ran");
    struct OuterCleanup(std::path::PathBuf);
    impl Drop for OuterCleanup {
        fn drop(&mut self) {
            std::fs::write(&self.0, b"outer cleanup ran before proven exit").unwrap();
        }
    }
    let _outer = OuterCleanup(marker);
    let child = SpawnedChild::new(PROCESS_INFORMATION {
        hProcess: HANDLE(value as *mut _),
        ..Default::default()
    });
    drop(child);
    panic!("uncertain child exit returned into outer cleanup");
}

#[test]
fn spawned_child_drop_failure_stops_outer_cleanup() {
    let original = suspended_fixture();
    let inherited = wait_only_handle(&original, true);
    let original_pid = unsafe { GetProcessId(original.process()) };
    assert!(original_pid > 0 && original_pid != std::process::id());
    for (value, broken_stderr) in [(0, false), (inherited.raw().0 as usize, false), (0, true)] {
        let root = crate::util::test_temp_dir().join(format!(
            "spawned-child-failure-{}-{value}-{broken_stderr}",
            std::process::id()
        ));
        std::fs::create_dir(&root).unwrap();
        let marker = root.join("outer-cleanup-ran");
        let exe = std::env::current_exe().unwrap();
        let app = wstr(exe.to_str().unwrap());
        let mut command = wstr(&format!(
            "{} --exact {FAILURE_FIXTURE} --ignored --nocapture",
            quote_arg(exe.to_str().unwrap())
        ));
        let mut environment = build_env_block_with_inheritance(
            &[
                (FIXTURE_HANDLE.into(), value.to_string()),
                (FIXTURE_MARKER.into(), marker.to_str().unwrap().into()),
                ("SystemRoot".into(), std::env::var("SystemRoot").unwrap()),
                ("TEMP".into(), root.to_str().unwrap().into()),
                ("TMP".into(), root.to_str().unwrap().into()),
            ],
            false,
        );
        let mut attributes = ProcThreadAttrs::new(1).unwrap();
        // Keep stdout's reader alive so the harness can print its progress.
        // Closing only stderr's reader forces a real ERROR_BROKEN_PIPE.
        let (stdout_read, stdout_write) = fixture_pipe();
        let (stderr_read, stderr_write) = fixture_pipe();
        drop(stderr_read);
        let mut handles = vec![inherited.raw()];
        if broken_stderr {
            handles.extend([stdout_write.raw(), stderr_write.raw()]);
        }
        attributes.set_handle_list(&handles).unwrap();
        let mut info = STARTUPINFOEXW::default();
        info.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
        if broken_stderr {
            info.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
            info.StartupInfo.hStdOutput = stdout_write.raw();
            info.StartupInfo.hStdError = stderr_write.raw();
        }
        info.lpAttributeList = attributes.list();
        let mut pi = PROCESS_INFORMATION::default();
        unsafe {
            CreateProcessW(
                pcwstr(&app),
                Some(PWSTR(command.as_mut_ptr())),
                None,
                None,
                true,
                CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT,
                Some(environment.as_mut_ptr().cast()),
                None,
                &info.StartupInfo,
                &mut pi,
            )
            .unwrap();
        }
        let helper = SpawnedChild::new(pi);
        // The real guard allows 30 seconds for an asynchronous exit, even
        // when the termination request is denied. The still-live wait-only
        // case must exhaust that bound and then stop outer cleanup.
        let observed = wait_for_process_exit(helper.process(), 35_000);
        let mut code = 0;
        let exit_code = unsafe { GetExitCodeProcess(helper.process(), &mut code) };
        drop(helper); // Also reaps the owned helper if its test timed out.
        drop(stdout_read);
        observed.unwrap();
        exit_code.unwrap();
        assert_eq!(code, 125, "cleanup uncertainty did not stop the helper");
        assert!(
            !marker.exists(),
            "outer cleanup ran despite unverified child exit"
        );
        // Failed termination through the wait-only reference never affected
        // the parent's separately owned, suspended process.
        assert_eq!(
            unsafe { WaitForSingleObject(original.process(), 0) },
            WAIT_TIMEOUT
        );
        std::fs::remove_dir(root).unwrap();
    }
    let observer = duplicate(original.process());
    drop(original);
    wait_for_process_exit(observer.raw(), 0).unwrap();
}

fn fixture_pipe() -> (OwnedHandle, OwnedHandle) {
    let mut read = HANDLE::default();
    let mut write = HANDLE::default();
    unsafe {
        windows::Win32::System::Pipes::CreatePipe(&mut read, &mut write, None, 0).unwrap();
    }
    let read = OwnedHandle(read);
    let write = OwnedHandle(write);
    unsafe {
        SetHandleInformation(write.raw(), HANDLE_FLAG_INHERIT.0, HANDLE_FLAG_INHERIT).unwrap()
    };
    (read, write)
}
