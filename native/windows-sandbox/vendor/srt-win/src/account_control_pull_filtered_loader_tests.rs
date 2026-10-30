//! Bounded diagnostic of one original synthetic child, never a qualification
//! fallback. No credentials, accounts, profiles or installed-helper effects.
use super::*;
use windows::Win32::System::Diagnostics::Debug::*;
const EXIT_FIXTURE: &str = "account_control::bootstrap::pull::tests::hosted::filtered::loader::bootstrap_loader_exit_fixture";

#[derive(Serialize)]
struct Trace {
    main_entered: bool,
    exit_code: Option<u32>,
    event_count: u32,
    modules: Vec<String>,
    loader_output: Vec<String>,
    exceptions: Vec<u32>,
    environment: desktop::Probe,
    shared_creation_process_handle: Option<bool>,
    shared_creation_thread_handle: Option<bool>,
}

struct DebugAttachment {
    process: OwnedHandle,
    pid: u32,
}
impl DebugAttachment {
    fn new(child: &SpawnedChild) -> Self {
        // No fallible return before detachment responsibility is established.
        let pid = unsafe { GetProcessId(child.process()) };
        let mut raw = HANDLE::default();
        if unsafe {
            DuplicateHandle(
                GetCurrentProcess(),
                child.process(),
                GetCurrentProcess(),
                &mut raw,
                (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
                false,
                DUPLICATE_HANDLE_OPTIONS(0),
            )
        }
        .is_err()
            || pid == 0
        {
            std::process::abort(); // Default debugger exit kills its debuggee.
        }
        Self {
            process: OwnedHandle(raw),
            pid,
        }
    }
}
impl Drop for DebugAttachment {
    fn drop(&mut self) {
        // Detach before SpawnedChild termination/wait: an attached debuggee's
        // exit needs ContinueDebugEvent, so ordinary guard drop alone can hang.
        if unsafe { DebugActiveProcessStop(self.pid) }.is_err()
            && unsafe { WaitForSingleObject(self.process.raw(), 30_000) } != WAIT_OBJECT_0
        {
            std::process::abort();
        }
    }
}

fn module_name(file: HANDLE) -> Result<String> {
    let mut path = vec![0u16; 32768];
    let n = unsafe { GetFinalPathNameByHandleW(file, &mut path, GETFINALPATHNAMEBYHANDLE_FLAGS(0)) }
        as usize;
    ensure!(n > 0 && n < path.len(), "loader image path unavailable");
    let path = String::from_utf16(&path[..n])?;
    let name = path.rsplit(['\\', '/']).next().unwrap_or("");
    ensure!(
        !name.is_empty()
            && name.len() <= 128
            && name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b)),
        "loader module basename refused"
    );
    Ok(name.to_owned())
}

fn debug_string(process: HANDLE, info: OUTPUT_DEBUG_STRING_INFO) -> Result<String> {
    let length = (info.nDebugStringLength as usize).min(2048);
    let width = if info.fUnicode == 0 { 1 } else { 2 };
    let mut bytes = vec![0u8; length * width];
    let mut read = 0;
    unsafe {
        ReadProcessMemory(
            process,
            info.lpDebugStringData.0.cast(),
            bytes.as_mut_ptr().cast(),
            bytes.len(),
            Some(&mut read),
        )?;
    }
    ensure!(read == bytes.len(), "short loader string read");
    let text = if width == 1 {
        String::from_utf8_lossy(&bytes).into_owned()
    } else {
        String::from_utf16_lossy(
            &bytes
                .chunks_exact(2)
                .map(|b| u16::from_le_bytes([b[0], b[1]]))
                .collect::<Vec<_>>(),
        )
    };
    Ok(text.trim_end_matches('\0').to_owned())
}

fn capture(private_station: bool, exit_fixture: bool) -> Result<Trace> {
    capture_with_checkpoint(private_station, exit_fixture, &mut |_, _, _| Ok(()))
}

fn native_hresult(error: &anyhow::Error) -> Option<i32> {
    error
        .chain()
        .find_map(|e| e.downcast_ref::<windows::core::Error>())
        .map(|e| e.code().0)
}

fn capture_with_checkpoint(
    private_station: bool,
    exit_fixture: bool,
    checkpoint: &mut dyn FnMut(&str, &Trace, Option<i32>) -> Result<()>,
) -> Result<Trace> {
    let image = image();
    let (_server, address) = desktop::controller_pipe()?;
    // Caller selects the same filtered token and original image as qualification.
    // The diagnostic supplies no mode or secret frame, and stops at Rust entry.
    let (child, _output, _desktop, probe) = create(
        &image,
        &address,
        private_station,
        Some(if exit_fixture { EXIT_FIXTURE } else { FIXTURE }),
    )?;
    let attachment = DebugAttachment::new(&child);
    let mut trace = Trace {
        main_entered: false,
        exit_code: None,
        event_count: 0,
        modules: vec![],
        loader_output: vec![],
        exceptions: vec![],
        environment: probe,
        shared_creation_process_handle: None,
        shared_creation_thread_handle: None,
    };
    checkpoint("created", &trace, None)?;
    let observed = (|| -> Result<()> {
        unsafe {
            DebugSetProcessKillOnExit(true)?;
        }
        image.verify_process(child.process())?;
        ensure!(
            unsafe { ResumeThread(child.thread()) } == 1,
            "loader initial suspension differs"
        );
        let deadline = std::time::Instant::now() + Duration::from_secs(15);
        let mut total_text = 0usize;
        let mut stop = false;
        while std::time::Instant::now() < deadline && trace.event_count < 4096 {
            let mut event = DEBUG_EVENT::default();
            unsafe {
                WaitForDebugEventEx(&mut event, 1000)?;
            }
            ensure!(
                event.dwProcessId == attachment.pid,
                "unexpected debugger process"
            );
            trace.event_count += 1;
            let mut continuation = DBG_CONTINUE;
            let result = (|| -> Result<()> {
                unsafe {
                    match event.dwDebugEventCode {
                        CREATE_PROCESS_DEBUG_EVENT => {
                            trace.shared_creation_process_handle =
                                Some(event.u.CreateProcessInfo.hProcess == child.process());
                            trace.shared_creation_thread_handle =
                                Some(event.u.CreateProcessInfo.hThread == child.thread());
                            let file = event.u.CreateProcessInfo.hFile;
                            if !file.is_invalid() {
                                drop(OwnedHandle(file));
                            }
                        }
                        LOAD_DLL_DEBUG_EVENT => {
                            let file = event.u.LoadDll.hFile;
                            if !file.is_invalid() {
                                let file = OwnedHandle(file);
                                let name = module_name(file.raw())?;
                                if trace.modules.len() < 256 && !trace.modules.contains(&name) {
                                    trace.modules.push(name);
                                }
                            }
                        }
                        OUTPUT_DEBUG_STRING_EVENT => {
                            let text = debug_string(child.process(), event.u.DebugString)?;
                            if text == "ALYSIS_FILTERED_MAIN_ENTERED" {
                                trace.main_entered = true;
                                stop = true;
                            } else if total_text + text.len() <= 65536
                                && trace.loader_output.len() < 256
                            {
                                total_text += text.len();
                                trace.loader_output.push(text);
                            }
                        }
                        EXCEPTION_DEBUG_EVENT => {
                            let code = event.u.Exception.ExceptionRecord.ExceptionCode.0 as u32;
                            if trace.exceptions.len() < 64 {
                                trace.exceptions.push(code);
                            }
                            if code != 0x80000003 {
                                continuation = DBG_EXCEPTION_NOT_HANDLED;
                            }
                        }
                        EXIT_PROCESS_DEBUG_EVENT => {
                            trace.exit_code = Some(event.u.ExitProcess.dwExitCode);
                            stop = true;
                        }
                        _ => {}
                    }
                }
                Ok(())
            })();
            // Keep the latest bounded trace before releasing the event or entering
            // cleanup, either of which can fail. This diagnostic never sees a
            // credential or mode frame. The report is one retained exclusive file.
            let saved = checkpoint(
                "event_observed",
                &trace,
                result.as_ref().err().and_then(native_hresult),
            );
            // Always release the delivered event, including parser/query failures.
            unsafe {
                ContinueDebugEvent(event.dwProcessId, event.dwThreadId, continuation)?;
            }
            saved?;
            result?;
            if stop {
                break;
            }
        }
        ensure!(stop, "loader diagnostic exceeded bounds");
        Ok(())
    })();
    checkpoint(
        if observed.is_ok() {
            "captured"
        } else {
            "capture_failed"
        },
        &trace,
        observed.as_ref().err().and_then(native_hresult),
    )?;
    if exit_fixture {
        eprintln!(
            "loader exit observer: shared_process={:?}, shared_thread={:?}, exit={:?}, wait_before_detach={:#x}",
            trace.shared_creation_process_handle,
            trace.shared_creation_thread_handle,
            trace.exit_code,
            unsafe { WaitForSingleObject(child.process(), 0) }.0
        );
    }
    drop(attachment);
    checkpoint(
        "detached",
        &trace,
        observed.as_ref().err().and_then(native_hresult),
    )?;
    drop(child);
    checkpoint(
        "retired",
        &trace,
        observed.as_ref().err().and_then(native_hresult),
    )?;
    observed?;
    Ok(trace)
}

#[test]
fn bootstrap_filtered_loader_observer_reaches_rust_without_credentials() {
    let trace = capture(false, false).unwrap();
    assert!(trace.main_entered);
    assert!(
        trace
            .modules
            .iter()
            .any(|name| name.eq_ignore_ascii_case("ntdll.dll"))
    );
}

#[test]
#[ignore = "bounded loader-snaps diagnostic, disposable hosted VM only"]
fn bootstrap_filtered_disposable_loader_trace() {
    let values = [
        "ALYSIS_RUN_DISPOSABLE_BOOTSTRAP_ACCOUNT",
        "GITHUB_ACTIONS",
        "RUNNER_ENVIRONMENT",
        "RUNNER_OS",
        "GITHUB_REPOSITORY",
        "GITHUB_EVENT_NAME",
    ]
    .map(|n| std::env::var(n).ok());
    let run = std::env::var("GITHUB_RUN_ID").unwrap_or_default();
    let attempt = std::env::var("GITHUB_RUN_ATTEMPT").unwrap_or_default();
    assert!(admission(&values, &run, &attempt));
    let exe = std::env::current_exe().unwrap();
    assert_eq!(
        exe.file_name().unwrap().to_str().unwrap(),
        format!("alysis-loader-{run}.exe")
    );
    let path = std::path::PathBuf::from(std::env::var_os("RUNNER_TEMP").unwrap())
        .join("alysis-bootstrap-loader.json");
    let mut report = serde_json::json!({"run_id":run,"run_attempt":attempt,"test":"bootstrap_filtered_disposable_loader_trace",
        "trace":null,"native_hresult":null,"stage":"admitted","capture_succeeded":false,
        "diagnostic_only":true,"credentials_sent":false,"production_ready":false});
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .unwrap();
    write_report(&mut file, &report).unwrap();
    let trace = capture_with_checkpoint(true, false, &mut |stage, trace, hresult| {
        report["stage"] = stage.into();
        report["trace"] = serde_json::to_value(trace)?;
        report["native_hresult"] = serde_json::to_value(hresult)?;
        write_report(&mut file, &report)
    });
    report["capture_succeeded"] = trace.is_ok().into();
    report["native_hresult"] =
        serde_json::to_value(trace.as_ref().err().and_then(native_hresult)).unwrap();
    write_report(&mut file, &report).unwrap();
    assert!(
        trace.is_ok(),
        "bounded loader observation failed; inspect diagnostic"
    );
}

fn write_report(file: &mut std::fs::File, report: &serde_json::Value) -> Result<()> {
    use std::io::{Seek, SeekFrom, Write};
    let encoded = serde_json::to_vec_pretty(report)?;
    ensure!(encoded.len() < 262144, "loader report exceeded bounds");
    file.seek(SeekFrom::Start(0))?;
    file.write_all(&encoded)?;
    file.set_len(encoded.len() as u64)?;
    file.flush()?;
    Ok(())
}

#[test]
#[ignore = "bounded test-only early-exit debuggee"]
fn bootstrap_loader_exit_fixture() {
    std::process::exit(75);
}

#[test]
fn bootstrap_filtered_loader_observer_retires_child_that_exits_without_marker() {
    let mut stages = Vec::new();
    let trace = capture_with_checkpoint(false, true, &mut |stage, trace, hresult| {
        stages.push((stage.to_owned(), trace.exit_code));
        assert!(hresult.is_none());
        Ok(())
    })
    .unwrap();
    assert!(!trace.main_entered);
    assert_eq!(trace.exit_code, Some(75));
    assert!(
        stages
            .iter()
            .any(|(stage, code)| stage == "event_observed" && *code == Some(75))
    );
    assert_eq!(
        &stages[stages.len() - 3..],
        &[
            ("captured".into(), Some(75)),
            ("detached".into(), Some(75)),
            ("retired".into(), Some(75))
        ]
    );
}
