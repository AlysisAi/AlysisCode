//! External observer for the test-only filtered broker. A passing live control
//! precedes the crash cut. The two-second late-event window is bounded evidence,
//! not a universal no-late-birth guarantee or production lifecycle authority.
//! The ordinary no-credential control observes CreateProcessW; only the real
//! account live/crash qualification observes CreateProcessWithLogonW.
use super::*;
use native::process_events::{Observer, ProcessEvent};
use std::time::Instant;
use windows::Win32::Security::{
    Authorization::{SE_FILE_OBJECT, SetNamedSecurityInfoW},
    DACL_SECURITY_INFORMATION, GetSecurityDescriptorDacl, PROTECTED_DACL_SECURITY_INFORMATION,
};

fn protect(path: &Path, account: &str) -> Result<()> {
    let owner = crate::sid::current_user_sid()?;
    crate::sid::LocalPsid::from_string(account)?;
    let descriptor = OwnedSd::from_sddl(&format!(
        "D:P(A;;FA;;;{owner})(A;;FA;;;SY)(A;;GRGX;;;{account})"
    ))?;
    let mut dacl = std::ptr::null_mut();
    let mut present = false.into();
    let mut defaulted = false.into();
    unsafe {
        GetSecurityDescriptorDacl(descriptor.ptr, &mut present, &mut dacl, &mut defaulted)?;
        ensure!(
            present.as_bool() && !dacl.is_null(),
            "missing private fixture DACL"
        );
        crate::util::win32_ok(
            SetNamedSecurityInfoW(
                pcwstr(&wstr(path.to_str().context("non-Unicode fixture")?)),
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                Some(dacl),
                None,
            ),
            "protect owned account diagnostic",
        )?;
    }
    Ok(())
}

fn wait_birth(observer: &Observer, identity: (u32, u64)) -> Result<()> {
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if observer
            .events()?
            .iter()
            .any(|e| e.id == 1 && (e.pid, e.created) == identity)
        {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    anyhow::bail!("kernel observer missed original account child birth")
}

fn cleanup_observed(events: &[ProcessEvent], spec: &Spec, separate: bool) -> Result<()> {
    for event in events.iter().filter(|e| e.id == 1) {
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
                    creation_time(process.raw())? == event.created,
                    "late process identity changed; no termination attempted"
                );
                if unsafe { WaitForSingleObject(process.raw(), 0) } == WAIT_OBJECT_0 {
                    continue;
                }
                // Test-only emergency retirement, never production PID authority.
                // Match native creation time, protected private image AND account.
                require_runner(process.raw(), spec, separate)?;
                unsafe {
                    TerminateProcess(process.raw(), 125)?;
                }
                ensure!(
                    unsafe { WaitForSingleObject(process.raw(), 5000) } == WAIT_OBJECT_0,
                    "late diagnostic child exit unverified"
                );
            }
            Err(e)
                if e.code() == ERROR_INVALID_PARAMETER.to_hresult()
                    && events.iter().any(|exit| {
                        exit.id == 2 && exit.pid == event.pid && exit.created == event.created
                    }) => {}
            Err(e) => return Err(e).context("inspect late diagnostic child"),
        }
    }
    Ok(())
}

fn reference_or_report<T: serde::de::DeserializeOwned>(
    frame: &[u8],
    broker_report: &mut Option<super::super::Report>,
) -> Result<T> {
    match serde_json::from_slice(frame) {
        Ok(reference) => Ok(reference),
        Err(error) => {
            // A broker refusal is a terminal report, never a process reference.
            // Preserve its native stage/result instead of hiding it behind the
            // reference parser's first unknown field (usually `passed`).
            if let Ok(report) = serde_json::from_slice::<super::super::Report>(frame) {
                let error = anyhow::anyhow!(
                    "filtered in-flight broker reported before original reference: stage={}, passed={}, hresult={:?}, worker_exit={:?}",
                    report.stage,
                    report.passed,
                    report.hresult,
                    report.worker_exit
                );
                *broker_report = Some(report);
                Err(error)
            } else {
                Err(error).context("invalid original in-flight reference")
            }
        }
    }
}

fn capture(
    secret: Option<&SecretLogon>,
    account: &str,
    cut: bool,
    kernel: bool,
    reports: &mut Vec<serde_json::Value>,
) -> Result<()> {
    ensure!(!cut || kernel, "fault cut requires live kernel observation");
    let separate = secret.is_some();
    // Beneath the already protected executable fixture; no normal app paths or
    // pre-existing ACLs are edited. Foreign test identity receives read/execute.
    let executable = std::env::current_exe()?;
    let root = executable
        .parent()
        .context("missing image parent")?
        .join(format!(
            "account-inflight-{}",
            crate::account_state::execution::random_id()?
        ));
    std::fs::create_dir(&root)?;
    protect(&root, account)?;
    let spec = Spec {
        target: root.join("never-resumed.exe"),
        account: account.into(),
    };
    spec.validate()?;
    std::fs::copy(&executable, &spec.target)?;
    protect(&spec.target, account)?;
    // Release this diagnostic pin before the oplock; production pin composition
    // is deliberately not claimed by this instrumented experiment.
    drop(PinnedRunnerImage::fixture(&spec.target)?);
    let observer = if kernel {
        Some(Observer::start(&spec.target)?)
    } else {
        None
    };
    let mut oplock = Some(native::HeldOplock::acquire(&spec.target)?);
    let image = image();
    let (server, address) = desktop::controller_pipe()?;
    let (broker, _diagnostic, _desktop, probe) = create(&image, &address, separate, None)?;
    let mut original_worker = None;
    let mut original_child = None;
    let mut identity = None;
    let mut depth = None;
    let mut cut_completed = false;
    let mut broker_report = None;
    let mut stage = "broker_start";
    let tested = (|| -> Result<()> {
        probe
            .startup_security
            .as_ref()
            .context("missing owned process observations")?
            .require_owned()?;
        let peer = ExpectedPeer::retain(broker.process())?.with_image(&image)?;
        ensure!(
            unsafe { ResumeThread(broker.thread()) } == 1,
            "filtered initial suspension differs"
        );
        let mut channel = server.accept(peer, HOST_TIMEOUT)?;
        channel.send(
            if separate {
                b"account-inflight"
            } else {
                b"local-inflight"
            },
            HOST_TIMEOUT,
        )?;
        if let Some(secret) = secret {
            send_secret(&mut channel, secret)?;
        }
        channel.send(&serde_json::to_vec(&spec)?, HOST_TIMEOUT)?;
        stage = "original_call_frame";
        let reference: BootstrapRef =
            reference_or_report(&channel.receive(HOST_TIMEOUT)?, &mut broker_report)?;
        let worker = reference.process.pull(
            broker.process(),
            PROCESS_QUERY_LIMITED_INFORMATION
                | PROCESS_SYNCHRONIZE
                | PROCESS_VM_READ
                | PROCESS_DUP_HANDLE,
        )?;
        image.verify_process(worker.raw())?;
        let thread = pull_handle(
            worker.raw(),
            reference.thread.source,
            (THREAD_GET_CONTEXT
                | THREAD_SUSPEND_RESUME
                | THREAD_SYNCHRONIZE
                | THREAD_QUERY_LIMITED_INFORMATION)
                .0,
        )?;
        ensure!(
            unsafe { GetProcessIdOfThread(thread.raw()) } == unsafe { GetProcessId(worker.raw()) },
            "observed thread differs from original bootstrap"
        );
        original_worker = Some(worker);
        let worker = original_worker.as_ref().unwrap();
        oplock.as_ref().unwrap().wait_for_break()?;
        depth = Some(
            native::observe_outstanding(worker.raw(), thread.raw(), reference.thread.frame)?.0,
        );
        // No job handle was transferred to this process at any point.
        if cut {
            stage = "forced_broker_exit";
            channel.peer.alive()?;
            unsafe {
                TerminateProcess(broker.process(), 77)?;
            }
            require_exit(broker.process(), 77)?;
            ensure!(
                unsafe { WaitForSingleObject(worker.raw(), 5000) } == WAIT_OBJECT_0,
                "original bootstrap survived broker crash"
            );
            cut_completed = true;
            drop(oplock.take());
            stage = "late_event_window";
            let deadline = Instant::now() + Duration::from_secs(2);
            while Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(20));
            }
        } else {
            drop(oplock.take());
            channel.send(b"observe-child", HOST_TIMEOUT)?;
            stage = "original_account_child";
            let reference: ProcessRef =
                reference_or_report(&channel.receive(HOST_TIMEOUT)?, &mut broker_report)?;
            let child = reference.pull(
                broker.process(),
                PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
            )?;
            assert_read_only(child.raw());
            require_runner(child.raw(), &spec, separate)?;
            identity = Some((
                unsafe { GetProcessId(child.raw()) },
                creation_time(child.raw())?,
            ));
            original_child = Some(child);
            if let Some(observer) = &observer {
                wait_birth(observer, identity.unwrap())?;
            }
            stage = "original_child_retirement";
            channel.send(b"retire-child", HOST_TIMEOUT)?;
            let report: super::super::Report =
                serde_json::from_slice(&channel.receive(HOST_TIMEOUT)?)?;
            let passed = report.passed
                && report.stage == "complete"
                && !report.elevated
                && !report.admin_member
                && report.only_traverse_privilege
                && report.hresult.is_none()
                && report.dedicated_runner_policy == separate;
            broker_report = Some(report);
            ensure!(passed, "filtered in-flight control refused");
            channel.write_in_flight(b"finish", HOST_TIMEOUT)?;
            require_exit(broker.process(), 0)?;
            require_exit(worker.raw(), 0)?;
            ensure!(
                unsafe { WaitForSingleObject(original_child.as_ref().unwrap().raw(), 5000) }
                    == WAIT_OBJECT_0,
                "original account child survived control"
            );
        }
        Ok(())
    })();
    // On an ordinary failure, retire the original broker before unblocking the
    // native call. Closing its sole job reference kills the original bootstrap.
    drop(broker);
    drop(oplock.take());
    let events = observer.map(Observer::finish).transpose();
    let checked = (|| -> Result<()> {
        tested?;
        let events = events
            .as_ref()
            .map_err(|e| anyhow::anyhow!("kernel observer failed: {e:#}"))?;
        if let Some(events) = events {
            if cut {
                ensure!(
                    events.is_empty(),
                    "target birth/exit observed after held-call broker death"
                );
            } else {
                let identity = identity.context("missing original account child identity")?;
                ensure!(
                    events.len() == 2
                        && [1, 2].iter().all(|id| events
                            .iter()
                            .filter(|e| e.id == *id && (e.pid, e.created) == identity)
                            .count()
                            == 1),
                    "kernel events did not match original child birth and exit"
                );
            }
        }
        Ok(())
    })();
    let cleanup = if let Ok(Some(events)) = &events {
        cleanup_observed(events, &spec, separate)
    } else {
        Ok(())
    };
    let report = serde_json::json!({"passed":checked.is_ok() && cleanup.is_ok(),"stage":stage,
        "native_hresult":checked.as_ref().err().and_then(|e|e.chain().find_map(|c|c.downcast_ref::<windows::core::Error>()).map(|e|e.code().0)).or_else(||broker_report.as_ref().and_then(|r|r.hresult)),
        "failure":checked.as_ref().err().map(|e|format!("{e:#}")),
        "cleanup_verified":cleanup.is_ok(),"separate_account":separate,
        "native_creation_api":if separate{"CreateProcessWithLogonW"}else{"CreateProcessW"},
        "inherited_token_control":!separate,"secondary_logon_exercised":separate,
        "outstanding_stack_depth":depth,"full_broker_crash_tested":cut_completed,
        "observer_has_no_job_reference":true,"original_child_observed":identity.is_some(),
        "dedicated_runner_policy":separate && identity.is_some(),"filtered_broker":broker_report,
        "events":events.as_ref().ok().and_then(|e|e.as_ref()),
        "lost_events":if kernel && events.is_ok(){Some(0)}else{None},
        "kernel_observer":kernel,"late_event_window_ms":if cut_completed{2000}else{0},
        "account_child_never_resumed":true,"full_lifecycle_qualified":false,"production_ready":false});
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(root.join("observation.json"))?;
    serde_json::to_writer_pretty(&mut file, &report)?;
    file.sync_all()?;
    eprintln!("filtered_inflight_observation={report}");
    reports.push(report);
    cleanup?;
    checked
}

pub(in crate::account_control::bootstrap::pull::tests::hosted) fn qualify(
    secret: &SecretLogon,
    account: &str,
    reports: &mut Vec<serde_json::Value>,
) -> Result<()> {
    let first = reports.len();
    capture(Some(secret), account, false, true, reports)?;
    capture(Some(secret), account, true, true, reports)?;
    ensure!(
        reports.len() == first + 2
            && reports[first..].iter().all(|report| {
                report["native_creation_api"] == "CreateProcessWithLogonW"
                    && report["separate_account"] == true
                    && report["secondary_logon_exercised"] == true
                    && report["inherited_token_control"] == false
            }),
        "real-account live/crash qualification requires Secondary Logon in both reports"
    );
    Ok(())
}

#[test]
fn inflight_observer_preserves_early_report_but_requires_original_reference() {
    let mut report = None;
    let reference: ProcessRef =
        reference_or_report(br#"{"source":16,"created":1}"#, &mut report).unwrap();
    assert_eq!((reference.source, reference.created), (16, 1));
    assert!(report.is_none());
    for passed in [false, true] {
        let frame = serde_json::json!({
            "passed":passed,"stage":"inflight_child_observation","elevated":false,
            "admin_member":false,"only_traverse_privilege":true,
            "dedicated_runner_policy":false,"hresult":-2147024891i32,"environment":null
        });
        let bytes = serde_json::to_vec(&frame).unwrap();
        let error = reference_or_report::<ProcessRef>(&bytes, &mut report)
            .err()
            .expect("an early report must never supply original-child authority");
        assert!(error.to_string().contains("inflight_child_observation"));
        assert!(error.to_string().contains("-2147024891"));
        let observed = report.take().unwrap();
        assert_eq!(observed.passed, passed);
        assert_eq!(observed.hresult, Some(-2147024891));
        assert!(reference_or_report::<BootstrapRef>(&bytes, &mut report).is_err());
        assert!(report.take().is_some());

        let mut mixed = frame;
        mixed["source"] = 16.into();
        mixed["created"] = 1.into();
        assert!(
            reference_or_report::<ProcessRef>(&serde_json::to_vec(&mixed).unwrap(), &mut report)
                .is_err()
        );
        assert!(report.is_none());
    }
}

#[test]
fn bootstrap_filtered_inflight_same_user_create_process_control_uses_original_handles_and_never_resumes_child()
 {
    let mut reports = Vec::new();
    capture(
        None,
        &crate::sid::current_user_sid().unwrap(),
        false,
        false,
        &mut reports,
    )
    .unwrap();
    assert_eq!(reports.len(), 1);
    assert_eq!(reports[0]["passed"], true);
    assert_eq!(reports[0]["full_broker_crash_tested"], false);
    assert_eq!(reports[0]["separate_account"], false);
    assert_eq!(reports[0]["native_creation_api"], "CreateProcessW");
    assert_eq!(reports[0]["inherited_token_control"], true);
    assert_eq!(reports[0]["secondary_logon_exercised"], false);
}
