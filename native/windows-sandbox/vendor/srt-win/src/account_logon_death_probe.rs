//! Fault injection with an original strict-job handle and a debugger observer.
//! Closing the last job handle is distinct from a full external broker crash.
//! Debug events provide original birth references; bounded silence does not
//! prove that a secondary-service child can never appear after the observation.
use super::*;
use serde::{Deserialize, Serialize};
use std::{collections::BTreeSet, io::Write, path::PathBuf};

const WORKER: &str = "account_control::bootstrap::tests::logon_inflight::death::bootstrap_inflight_logon_worker_fixture";

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Ready {
    frame: usize,
    thread: usize,
}

fn save_once(path: &Path, value: &impl Serialize) -> Result<()> {
    let data = serde_json::to_vec(value)?;
    ensure!(data.len() <= 1024, "probe readiness exceeds bounds");
    let temporary = path.with_extension("writing");
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)?;
    file.write_all(&data)?;
    file.sync_all()?;
    drop(file);
    ensure!(!path.exists(), "probe readiness collision");
    std::fs::rename(temporary, path)?;
    Ok(())
}

#[test]
#[ignore = "private never-resumed-child fixture for bounded native logon diagnostic"]
fn bootstrap_inflight_logon_worker_fixture() {
    let path = PathBuf::from(std::env::var_os("ALYSIS_TEST_INFLIGHT_IMAGE").unwrap());
    let root = path.parent().unwrap();
    assert!(path.is_absolute() && path.file_name().unwrap() == "never-resumed.exe");
    let mut raw = HANDLE::default();
    unsafe {
        DuplicateHandle(
            GetCurrentProcess(),
            GetCurrentThread(),
            GetCurrentProcess(),
            &mut raw,
            (THREAD_GET_CONTEXT
                | THREAD_SUSPEND_RESUME
                | THREAD_SYNCHRONIZE
                | THREAD_QUERY_LIMITED_INFORMATION)
                .0,
            false,
            DUPLICATE_HANDLE_OPTIONS(0),
        )
        .unwrap();
    }
    let thread = OwnedHandle(raw);
    let child = synthetic_logon(&path, |frame| {
        save_once(
            &root.join("ready.json"),
            &Ready {
                frame,
                thread: thread.raw().0 as usize,
            },
        )
    })
    .unwrap();
    save_once(
        &root.join("returned.json"),
        &serde_json::json!({"created":true,"never_resumed":true,"process":child.process().0 as usize}),
    )
    .unwrap();
    if std::env::var("ALYSIS_TEST_INFLIGHT_HOLD").as_deref() == Ok("1") {
        let deadline = Instant::now() + Duration::from_secs(10);
        while !root.join("release.json").exists() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        assert!(
            root.join("release.json").exists(),
            "control observer did not acknowledge the child"
        );
    }
    drop(child);
}

struct DebugScope {
    pids: BTreeSet<u32>,
}
impl Drop for DebugScope {
    fn drop(&mut self) {
        for &pid in &self.pids {
            // Always after the last delivered event has been continued.
            // Live originals are drained by separate process guards afterward.
            if unsafe { DebugActiveProcessStop(pid) }.is_err() {
                // An exit event may already have detached this original.
                // No PID reopening or termination by PID is used here.
            }
        }
    }
}

struct Captured {
    process: OwnedHandle,
    pid: u32,
    target: bool,
    image: String,
}
impl Drop for Captured {
    fn drop(&mut self) {
        if unsafe { WaitForSingleObject(self.process.raw(), 0) } != WAIT_OBJECT_0 {
            unsafe {
                let _ = TerminateProcess(self.process.raw(), 125);
            }
            if unsafe { WaitForSingleObject(self.process.raw(), 5000) } != WAIT_OBJECT_0 {
                std::process::abort();
            }
        }
    }
}

fn file_path(file: HANDLE) -> Result<String> {
    let mut buffer = vec![0u16; 32768];
    let length =
        unsafe { GetFinalPathNameByHandleW(file, &mut buffer, GETFINALPATHNAMEBYHANDLE_FLAGS(0)) }
            as usize;
    ensure!(
        length > 0 && length < buffer.len(),
        "missing debug image identity"
    );
    Ok(String::from_utf16(&buffer[..length])?)
}

fn create_worker(
    image: &PinnedRunnerImage,
    job: &Job,
    target: &Path,
    debug: bool,
) -> Result<SpawnedChild> {
    containment::prepare(job)?;
    let executable = image
        .application()?
        .to_str()
        .context("non-Unicode worker")?;
    let app = wstr(executable);
    let mut command = wstr(&format!(
        "{} --exact {WORKER} --ignored --nocapture --test-threads=1",
        crate::launch::quote_arg(executable)
    ));
    let system = os_directory(false)?;
    let windows = os_directory(true)?;
    let mut environment = crate::launch::build_env_block_with_inheritance(
        &[
            ("SystemRoot".into(), windows.clone()),
            ("WINDIR".into(), windows),
            ("PATH".into(), system.clone()),
            (
                "ALYSIS_TEST_INFLIGHT_IMAGE".into(),
                target.to_str().context("non-Unicode target")?.into(),
            ),
            (
                "ALYSIS_TEST_INFLIGHT_HOLD".into(),
                if debug { "0" } else { "1" }.into(),
            ),
        ],
        false,
    );
    let mut attributes = ProcThreadAttrs::new(1)?;
    attributes.set_job(job)?;
    let mut startup = STARTUPINFOEXW::default();
    startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
    startup.lpAttributeList = attributes.list();
    let mut info = PROCESS_INFORMATION::default();
    unsafe {
        CreateProcessW(
            pcwstr(&app),
            Some(windows::core::PWSTR(command.as_mut_ptr())),
            None,
            None,
            false,
            CREATE_SUSPENDED
                | CREATE_NO_WINDOW
                | CREATE_UNICODE_ENVIRONMENT
                | EXTENDED_STARTUPINFO_PRESENT
                | if debug {
                    DEBUG_PROCESS
                } else {
                    PROCESS_CREATION_FLAGS(0)
                },
            Some(environment.as_mut_ptr().cast()),
            pcwstr(&wstr(&system)),
            &startup.StartupInfo,
            &mut info,
        )?;
    }
    Ok(SpawnedChild::new(info))
}

fn capture(close_job: bool) -> Result<serde_json::Value> {
    let root = crate::util::test_temp_dir().join(format!(
        "logon-death-{}",
        crate::account_state::execution::random_id()?
    ));
    std::fs::create_dir(&root)?;
    protect(&root)?;
    let target = root.join("never-resumed.exe");
    std::fs::copy(std::env::current_exe()?, &target)?;
    protect(&target)?;
    drop(PinnedRunnerImage::fixture(&target)?);
    let mut oplock = Some(HeldOplock::acquire(&target)?);
    ensure!(
        unsafe { WaitForSingleObject(oplock.as_ref().unwrap().event.raw(), 0) } == WAIT_TIMEOUT,
        "premature image break"
    );
    let image = image();
    let mut job = Some(Job::new(false)?);
    let child = create_worker(&image, job.as_ref().unwrap(), &target, true)?;
    let pid = unsafe { GetProcessId(child.process()) };
    // Declare original-process cleanup before attachment so debug detachment
    // runs first on every return. Never use debugger exit to establish success.
    let mut captured = Vec::<Captured>::new();
    let mut debug = DebugScope { pids: [pid].into() };
    let mut thread = None;
    let mut frame = None;
    let mut observed_depth = None;
    let mut released_at = None;
    let mut worker_exit = None;
    let mut events = 0;
    let mut target_births = 0;
    let mut target_exits = 0;
    let mut exited = BTreeSet::new();
    let expected_target = format!(r"\\?\{}", target.to_str().unwrap());
    let started = Instant::now();
    let result = (|| -> Result<()> {
        unsafe {
            DebugSetProcessKillOnExit(true)?;
        }
        image.verify_process(child.process())?;
        containment::prepare(job.as_ref().unwrap())?;
        ensure!(
            unsafe { ResumeThread(child.thread()) } == 1,
            "worker initial suspension differs"
        );
        while started.elapsed() < Duration::from_secs(15) && events < 8192 {
            let mut event = DEBUG_EVENT::default();
            let event_result = unsafe { WaitForDebugEventEx(&mut event, 20) };
            match event_result {
                Ok(()) => {
                    events += 1;
                    let mut continuation = DBG_CONTINUE;
                    let handled = (|| -> Result<()> {
                        unsafe {
                            match event.dwDebugEventCode {
                                CREATE_PROCESS_DEBUG_EVENT => {
                                    let info = event.u.CreateProcessInfo;
                                    debug.pids.insert(event.dwProcessId);
                                    let name = if !info.hFile.is_invalid() {
                                        let file = OwnedHandle(info.hFile);
                                        file_path(file.raw())?
                                    } else {
                                        String::new()
                                    };
                                    if event.dwProcessId != pid {
                                        ensure!(
                                            captured.len() < 8,
                                            "too many diagnostic descendants"
                                        );
                                        let mut raw = HANDLE::default();
                                        DuplicateHandle(
                                            GetCurrentProcess(),
                                            info.hProcess,
                                            GetCurrentProcess(),
                                            &mut raw,
                                            (PROCESS_QUERY_LIMITED_INFORMATION
                                                | PROCESS_SYNCHRONIZE
                                                | PROCESS_TERMINATE)
                                                .0,
                                            false,
                                            DUPLICATE_HANDLE_OPTIONS(0),
                                        )?;
                                        let target = name.eq_ignore_ascii_case(&expected_target);
                                        captured.push(Captured {
                                            process: OwnedHandle(raw),
                                            pid: event.dwProcessId,
                                            target,
                                            image: name
                                                .rsplit(['\\', '/'])
                                                .next()
                                                .unwrap_or("")
                                                .to_owned(),
                                        });
                                        if target {
                                            target_births += 1;
                                        }
                                    }
                                }
                                LOAD_DLL_DEBUG_EVENT => {
                                    let file = event.u.LoadDll.hFile;
                                    if !file.is_invalid() {
                                        drop(OwnedHandle(file));
                                    }
                                }
                                EXCEPTION_DEBUG_EVENT => {
                                    if event.u.Exception.ExceptionRecord.ExceptionCode.0 as u32
                                        != 0x80000003
                                    {
                                        continuation = DBG_EXCEPTION_NOT_HANDLED;
                                    }
                                }
                                EXIT_PROCESS_DEBUG_EVENT => {
                                    exited.insert(event.dwProcessId);
                                    debug.pids.remove(&event.dwProcessId);
                                    if event.dwProcessId == pid {
                                        worker_exit = Some(event.u.ExitProcess.dwExitCode);
                                    }
                                    if captured
                                        .iter()
                                        .any(|c| c.pid == event.dwProcessId && c.target)
                                    {
                                        target_exits += 1;
                                    }
                                }
                                _ => {}
                            }
                        }
                        Ok(())
                    })();
                    unsafe {
                        ContinueDebugEvent(event.dwProcessId, event.dwThreadId, continuation)?;
                    }
                    handled?;
                }
                Err(e) if e.code() == ERROR_SEM_TIMEOUT.to_hresult() => {}
                Err(e) => return Err(e.into()),
            }
            if frame.is_none() && root.join("ready.json").exists() {
                let bytes = std::fs::read(root.join("ready.json"))?;
                ensure!(bytes.len() <= 1024, "oversized worker frame");
                let ready: Ready = serde_json::from_slice(&bytes)?;
                ensure!(
                    ready.thread > 0 && ready.thread < isize::MAX as usize,
                    "invalid worker thread reference"
                );
                let mut raw = HANDLE::default();
                unsafe {
                    DuplicateHandle(
                        child.process(),
                        HANDLE(ready.thread as *mut _),
                        GetCurrentProcess(),
                        &mut raw,
                        (THREAD_GET_CONTEXT
                            | THREAD_SUSPEND_RESUME
                            | THREAD_SYNCHRONIZE
                            | THREAD_QUERY_LIMITED_INFORMATION)
                            .0,
                        false,
                        DUPLICATE_HANDLE_OPTIONS(0),
                    )?;
                }
                let original = OwnedHandle(raw);
                ensure!(
                    unsafe { GetProcessIdOfThread(original.raw()) } == pid,
                    "thread differs from original worker"
                );
                thread = Some(original);
                frame = Some(ready.frame);
            }
            if released_at.is_none()
                && frame.is_some()
                && unsafe { WaitForSingleObject(oplock.as_ref().unwrap().event.raw(), 0) }
                    == WAIT_OBJECT_0
            {
                oplock.as_ref().unwrap().wait_for_break()?;
                observed_depth = Some(
                    observe_outstanding(
                        child.process(),
                        thread.as_ref().unwrap().raw(),
                        frame.unwrap(),
                    )?
                    .0,
                );
                if close_job {
                    // Fault injection only: close the exact last handle without
                    // Job::drop's explicit TerminateJobObject/empty wait. There
                    // are no borrowed job bindings or attribute lists remaining.
                    let original = job.take().unwrap();
                    let raw = original.raw();
                    std::mem::forget(original);
                    unsafe {
                        CloseHandle(raw)?;
                    }
                }
                drop(oplock.take());
                released_at = Some(Instant::now());
            }
            if let Some(at) = released_at {
                // The live control must prove descendant debug visibility.
                // The fault case's bounded silence is recorded, never universal
                // proof that a service cannot create a late unobserved child.
                if worker_exit.is_some()
                    && captured.iter().all(|c| exited.contains(&c.pid))
                    && at.elapsed() >= Duration::from_millis(750)
                {
                    break;
                }
            }
        }
        ensure!(
            observed_depth.is_some(),
            "native in-flight frame was not observed"
        );
        ensure!(
            worker_exit.is_some(),
            "original worker did not exit within diagnostic bounds"
        );
        ensure!(
            unsafe { WaitForSingleObject(child.process(), 5000) } == WAIT_OBJECT_0,
            "original worker not signaled"
        );
        ensure!(
            captured
                .iter()
                .all(|c| unsafe { WaitForSingleObject(c.process.raw(), 5000) } == WAIT_OBJECT_0),
            "observed descendant survived; diagnostic cleanup required"
        );
        if !close_job {
            ensure!(
                worker_exit == Some(0) && root.join("returned.json").exists(),
                "live control did not finish"
            );
            ensure!(
                target_births == 1 && target_exits == 1,
                "debugger did not observe the original secondary-logon child"
            );
        } else {
            ensure!(
                !root.join("returned.json").exists(),
                "native call returned before death cut"
            );
        }
        Ok(())
    })();
    let report = serde_json::json!({"last_job_handle_closed_in_native_call":close_job,
        "outstanding_stack_depth":observed_depth,"worker_exit":worker_exit,"debug_events":events,
        "target_birth_events":target_births,"target_exit_events":target_exits,
        "observed_images":captured.iter().map(|c|c.image.as_str()).collect::<Vec<_>>(),
        "failure":result.as_ref().err().map(|e|format!("{e:#}")),
        "all_observed_descendants_exited":captured.iter().all(|c| unsafe {WaitForSingleObject(c.process.raw(),0)}==WAIT_OBJECT_0),
        "diagnostic_succeeded":result.is_ok(),"elapsed_ms":started.elapsed().as_millis(),
        "full_broker_crash_tested":false,"separate_account_tested":false,"inflight_death_qualified":false,
        "no_child_resumed":true,"production_ready":false});
    let saved = save_once(&root.join("observation.json"), &report);
    eprintln!("secondary_logon_job_close_observation={report}");
    drop(oplock.take());
    drop(debug);
    drop(captured);
    drop(child);
    drop(job);
    saved?;
    result?;
    Ok(report)
}

#[test]
#[ignore = "bounded manual debugger/oplock death diagnostic; not full lifecycle qualification"]
fn bootstrap_secondary_logon_job_close_diagnostic() {
    capture(false).unwrap();
    capture(true).unwrap();
}

fn process_created(process: HANDLE) -> Result<u64> {
    let mut times = [FILETIME::default(); 4];
    unsafe {
        GetProcessTimes(
            process,
            &mut times[0] as *mut _,
            &mut times[1] as *mut _,
            &mut times[2] as *mut _,
            &mut times[3] as *mut _,
        )?;
    }
    Ok(u64::from(times[0].dwLowDateTime) | (u64::from(times[0].dwHighDateTime) << 32))
}

fn read_small<T: for<'de> Deserialize<'de>>(path: &Path) -> Result<T> {
    let file = std::fs::File::open(path)?;
    ensure!(
        file.metadata()?.len() <= 1024,
        "oversized private probe message"
    );
    use std::io::Read;
    let mut bytes = Vec::new();
    file.take(1025).read_to_end(&mut bytes)?;
    ensure!(bytes.len() <= 1024, "growing private probe message");
    Ok(serde_json::from_slice(&bytes)?)
}

#[path = "account_logon_broker_probe.rs"]
mod broker;

fn kernel_capture(close_job: bool, broker_crash: bool) -> Result<serde_json::Value> {
    let root = crate::util::test_temp_dir().join(format!(
        "logon-kernel-cut-{}",
        crate::account_state::execution::random_id()?
    ));
    std::fs::create_dir(&root)?;
    protect(&root)?;
    let target = root.join("never-resumed.exe");
    std::fs::copy(std::env::current_exe()?, &target)?;
    protect(&target)?;
    drop(PinnedRunnerImage::fixture(&target)?);
    let observer = super::process_events::Observer::start(&target)?;
    let mut oplock = Some(HeldOplock::acquire(&target)?);
    let image = image();
    let mut child = broker::KernelWorker::start(&image, &target, broker_crash)?;
    let mut depth = None;
    let mut original_target = None;
    let mut target_identity = None;
    let mut cut_completed = false;
    let started = Instant::now();
    let tested = (|| -> Result<()> {
        image.verify_process(child.process())?;
        while !root.join("ready.json").exists() && started.elapsed() < Duration::from_secs(5) {
            ensure!(
                unsafe { WaitForSingleObject(child.process(), 10) } == WAIT_TIMEOUT,
                "kernel probe worker exited before native call"
            );
        }
        let ready: Ready = read_small(&root.join("ready.json"))?;
        ensure!(
            ready.thread > 0 && ready.thread < isize::MAX as usize,
            "invalid original thread handle"
        );
        let mut raw = HANDLE::default();
        unsafe {
            DuplicateHandle(
                child.process(),
                HANDLE(ready.thread as *mut _),
                GetCurrentProcess(),
                &mut raw,
                (THREAD_GET_CONTEXT
                    | THREAD_SUSPEND_RESUME
                    | THREAD_SYNCHRONIZE
                    | THREAD_QUERY_LIMITED_INFORMATION)
                    .0,
                false,
                DUPLICATE_HANDLE_OPTIONS(0),
            )?;
        }
        let thread = OwnedHandle(raw);
        ensure!(
            unsafe { GetProcessIdOfThread(thread.raw()) }
                == unsafe { GetProcessId(child.process()) },
            "worker thread identity differs"
        );
        oplock.as_ref().unwrap().wait_for_break()?;
        depth = Some(observe_outstanding(child.process(), thread.raw(), ready.frame)?.0);
        if close_job {
            child.cut()?;
            cut_completed = true;
        }
        drop(oplock.take());
        let deadline = Instant::now() + Duration::from_secs(5);
        if !close_job {
            while !root.join("returned.json").exists() && Instant::now() < deadline {
                ensure!(
                    unsafe { WaitForSingleObject(child.process(), 10) } == WAIT_TIMEOUT,
                    "worker exited before child offer"
                );
            }
            let returned: serde_json::Value = read_small(&root.join("returned.json"))?;
            let source = usize::try_from(
                returned["process"]
                    .as_u64()
                    .context("missing original child reference")?,
            )?;
            ensure!(
                source > 0 && source < isize::MAX as usize,
                "invalid original child reference"
            );
            let mut raw = HANDLE::default();
            unsafe {
                DuplicateHandle(
                    child.process(),
                    HANDLE(source as *mut _),
                    GetCurrentProcess(),
                    &mut raw,
                    (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
                    false,
                    DUPLICATE_HANDLE_OPTIONS(0),
                )?;
            }
            let target_handle = OwnedHandle(raw);
            PinnedRunnerImage::fixture(&target)?.verify_process(target_handle.raw())?;
            target_identity = Some((
                unsafe { GetProcessId(target_handle.raw()) },
                process_created(target_handle.raw())?,
            ));
            original_target = Some(target_handle);
            while Instant::now() < deadline && observer.events()?.iter().all(|e| e.id != 1) {
                std::thread::sleep(Duration::from_millis(20));
            }
            let (pid, created) = target_identity.unwrap();
            ensure!(
                observer
                    .events()?
                    .iter()
                    .filter(|e| e.id == 1 && e.pid == pid && e.created == created)
                    .count()
                    == 1,
                "kernel observer missed the original secondary-logon child"
            );
            save_once(&root.join("release.json"), &true)?;
        }
        ensure!(
            unsafe { WaitForSingleObject(child.process(), 5000) } == WAIT_OBJECT_0,
            "original worker survived kernel cut"
        );
        if close_job {
            ensure!(
                !root.join("returned.json").exists(),
                "logon returned before job-close cut"
            );
            // Bounded late-event window; this is not a universal no-late-birth guarantee.
            let until = Instant::now() + Duration::from_secs(2);
            while Instant::now() < until {
                std::thread::sleep(Duration::from_millis(20));
            }
        } else {
            let mut exit = u32::MAX;
            unsafe {
                GetExitCodeProcess(child.process(), &mut exit)?;
            }
            ensure!(exit == 0, "live kernel control worker failed ({exit})");
            child.verify_control_exit()?;
            ensure!(
                unsafe { WaitForSingleObject(original_target.as_ref().unwrap().raw(), 5000) }
                    == WAIT_OBJECT_0,
                "original control child survived"
            );
        }
        Ok(())
    })();
    // Release before any observer or process cleanup on all ordinary errors.
    drop(oplock.take());
    let events = observer.finish();
    let qualified = (|| -> Result<()> {
        tested?;
        let records = events
            .as_ref()
            .map_err(|e| anyhow::anyhow!("kernel observer failed: {e:#}"))?;
        if close_job {
            ensure!(
                records.is_empty(),
                "late target process event after in-flight job close"
            );
        } else {
            let (pid, created) = target_identity.context("missing original target identity")?;
            ensure!(
                records.len() == 2
                    && records
                        .iter()
                        .filter(|e| e.id == 2 && e.pid == pid && e.created == created)
                        .count()
                        == 1,
                "kernel observer did not match original target birth and exit"
            );
        }
        Ok(())
    })();
    // If a late synthetic child actually appears, never leave it suspended.
    // This diagnostic lookup is not production authority: verify both the
    // native creation time and pinned private image before touching a process.
    if close_job {
        if let Ok(records) = &events {
            for event in records.iter().filter(|e| e.id == 1) {
                match unsafe {
                    OpenProcess(
                        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE | PROCESS_TERMINATE,
                        false,
                        event.pid,
                    )
                } {
                    Ok(raw) => {
                        let process = OwnedHandle(raw);
                        ensure!(
                            process_created(process.raw())? == event.created,
                            "late PID was reused; no termination attempted"
                        );
                        PinnedRunnerImage::fixture(&target)?.verify_process(process.raw())?;
                        if unsafe { WaitForSingleObject(process.raw(), 0) } != WAIT_OBJECT_0 {
                            unsafe {
                                let _ = TerminateProcess(process.raw(), 125);
                            }
                            ensure!(
                                unsafe { WaitForSingleObject(process.raw(), 5000) }
                                    == WAIT_OBJECT_0,
                                "late synthetic child exit unverified"
                            );
                        }
                    }
                    Err(e)
                        if e.code() == ERROR_INVALID_PARAMETER.to_hresult()
                            && records.iter().any(|x| {
                                x.id == 2 && x.pid == event.pid && x.created == event.created
                            }) => {}
                    Err(e) => return Err(e).context("cannot inspect late synthetic child"),
                }
            }
        }
    }
    let report = serde_json::json!({"last_job_handle_closed_in_native_call":cut_completed && !broker_crash,"outstanding_stack_depth":depth,
        "events":events.as_ref().ok(),"diagnostic_succeeded":qualified.is_ok(),"failure":qualified.as_ref().err().map(|e|format!("{e:#}")),
        "original_target_handle_control":target_identity.is_some(),"lost_events":if events.is_ok(){Some(0)}else{None},
        "debugger_used":false,"full_broker_crash_tested":cut_completed && broker_crash,"separate_broker_control":broker_crash,
        "observer_owns_test_job":!broker_crash,"separate_account_tested":false,
        "inflight_death_qualified":false,"no_child_resumed":true,"production_ready":false});
    save_once(&root.join("kernel-observation.json"), &report)?;
    eprintln!("secondary_logon_kernel_job_close_observation={report}");
    drop(child);
    qualified?;
    Ok(report)
}

#[test]
#[ignore = "bounded manual kernel-event/oplock diagnostic; requires elevated trace permission"]
fn bootstrap_secondary_logon_kernel_job_close_diagnostic() {
    kernel_capture(false, false).unwrap();
    kernel_capture(true, false).unwrap();
}

#[test]
#[ignore = "bounded manual full synthetic broker crash; requires elevated kernel observer"]
fn bootstrap_secondary_logon_kernel_broker_crash_diagnostic() {
    kernel_capture(false, true).unwrap();
    kernel_capture(true, true).unwrap();
}
