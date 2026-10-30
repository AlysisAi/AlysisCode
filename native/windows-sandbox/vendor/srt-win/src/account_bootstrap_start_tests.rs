//! Real trusted-host startup; no account logon, password or user command.
use super::*;
use crate::{account_control::ControlServer, account_state::image::PinnedRunnerImage};
use std::time::Duration;

fn observe_job_processes(record: &ExecutionRecord) -> Vec<(String, OwnedHandle)> {
    use windows::Win32::System::{JobObjects::*, SystemServices::JOB_OBJECT_QUERY, Threading::*};
    #[repr(C)]
    #[derive(Default)]
    struct List {
        assigned: u32,
        listed: u32,
        ids: [usize; 8],
    }
    let job = OwnedHandle(
        unsafe { OpenJobObjectW(JOB_OBJECT_QUERY, false, pcwstr(&wstr(&record.job))) }.unwrap(),
    );
    let mut list = List::default();
    let mut returned = 0;
    unsafe {
        QueryInformationJobObject(
            Some(job.raw()),
            JobObjectBasicProcessIdList,
            (&mut list as *mut List).cast(),
            size_of::<List>() as u32,
            Some(&mut returned),
        )
    }
    .unwrap();
    assert!(list.listed > 0 && list.listed <= 8 && list.listed == list.assigned);
    assert!(
        returned as usize
            >= std::mem::offset_of!(List, ids) + list.listed as usize * size_of::<usize>()
            && returned as usize <= size_of::<List>()
    );
    let mut result = Vec::new();
    for &pid in &list.ids[..list.listed as usize] {
        let process = OwnedHandle(
            unsafe {
                OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
                    false,
                    u32::try_from(pid).unwrap(),
                )
            }
            .unwrap(),
        );
        let mut member = windows::core::BOOL::default();
        unsafe { IsProcessInJob(process.raw(), Some(job.raw()), &mut member) }.unwrap();
        assert!(member.as_bool());
        let mut buffer = vec![0u16; 32768];
        let mut length = buffer.len() as u32;
        unsafe {
            QueryFullProcessImageNameW(
                process.raw(),
                PROCESS_NAME_WIN32,
                windows::core::PWSTR(buffer.as_mut_ptr()),
                &mut length,
            )
        }
        .unwrap();
        let image = String::from_utf16(&buffer[..length as usize]).unwrap();
        eprintln!("atomic startup job member: {image}");
        result.push((image, process));
    }
    result
}

#[test]
fn atomic_start_commits_intent_and_ready_or_drains_original_on_every_error() {
    for (cut, expected) in [
        ("success", 3),
        ("unrecorded-birth", 0),
        ("wrong-server", 0),
        ("invalid-timeout", 0),
        ("resume-intent", 0),
        ("resume-receipt", 1),
        ("ready-receipt", 2),
    ] {
        let fixture = protected_fixture();
        let db = protected_database(&fixture);
        let ready = ReadyInstallation::fixture(fixture.setup.clone(), &fixture.root).unwrap();
        let active = atomic_fixture_active(&fixture, &db);
        let image = PinnedRunnerImage::fixture_installed(&ready).unwrap();
        let account = ready.binding(AccountMode::Offline).unwrap().account_sid;
        let (server, address) = ControlServer::prepare(&account).unwrap();
        let plan =
            bootstrap_plan::CommittedPlan::prepare(&db, &active, &image, &ready, &address).unwrap();
        let job = plan.create_job().unwrap();
        job.record_birth().unwrap();
        let child = job.prepare_bootstrap().unwrap();
        let observed = child.fixture_readonly_observer().unwrap();
        if cut != "unrecorded-birth" {
            child.record_birth().unwrap();
        }
        let sql = db.connection().unwrap();
        match cut {
            "resume-intent" => sql.execute_batch("PRAGMA query_only=ON").unwrap(),
            _ => {}
        }
        // Refuse the next write only after the preceding valid event commits.
        if matches!(cut, "resume-receipt" | "ready-receipt") {
            let at = if cut == "resume-receipt" { 1 } else { 2 };
            bootstrap_plan::lifecycle::resume::AFTER_RESUME_EVENT.with(|s| {
                *s.borrow_mut() = Some((
                    at,
                    Box::new(|connection| {
                        connection.execute_batch("PRAGMA query_only=ON").unwrap()
                    }),
                ))
            });
        }
        let server = if cut == "wrong-server" {
            drop(server);
            ControlServer::prepare(&account).unwrap().0
        } else {
            server
        };
        let timeout = if cut == "invalid-timeout" {
            Duration::ZERO
        } else {
            Duration::from_secs(3)
        };
        let result = child.start(server, timeout);
        let members = if cut == "success" {
            let running = result.unwrap();
            let members = observe_job_processes(active.record().unwrap());
            assert_eq!(
                unsafe { WaitForSingleObject(observed.raw(), 0) },
                WAIT_TIMEOUT
            );
            // The console loader may also put its system conhost in the job.
            // Observe the original references, then require ALL of them to exit
            // after the real original job guard terminates and drains the tree.
            let mut system = vec![0u16; 32768];
            let length = unsafe {
                windows::Win32::System::SystemInformation::GetSystemDirectoryW(Some(&mut system))
            } as usize;
            assert!(length > 0 && length < system.len());
            let conhost =
                PathBuf::from(String::from_utf16(&system[..length]).unwrap()).join("conhost.exe");
            let executable = std::env::current_exe().unwrap();
            assert!((1..=2).contains(&members.len()));
            assert_eq!(
                members
                    .iter()
                    .filter(|(p, _)| PathBuf::from(p) == executable)
                    .count(),
                1
            );
            assert!(members.iter().all(|(p, _)| PathBuf::from(p) == executable
                || p.eq_ignore_ascii_case(conhost.to_str().unwrap())));
            let original = members
                .iter()
                .find(|(p, _)| PathBuf::from(p) == executable)
                .unwrap();
            assert!(
                unsafe {
                    windows::Win32::Foundation::CompareObjectHandles(
                        original.1.raw(),
                        observed.raw(),
                    )
                }
                .as_bool()
            );
            drop(running);
            members
        } else {
            assert!(result.is_err(), "unexpected startup success at {cut}");
            drop(result);
            Vec::new()
        };
        assert_eq!(
            unsafe { WaitForSingleObject(observed.raw(), 5000) },
            WAIT_OBJECT_0
        );
        sql.execute_batch("PRAGMA query_only=OFF").unwrap();
        let count: usize = sql
            .query_row(
                "SELECT count(*) FROM candidate_atomic_bootstrap_resume",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, expected, "wrong durable resume prefix at {cut}");
        assert!(job.prepare_bootstrap().is_err());
        assert!(plan.create_job().is_err());
        let drain_started = std::time::Instant::now();
        drop(job); // Existing Job guard terminates and verifies whole-job emptiness.
        let drain_ms = drain_started.elapsed().as_millis();
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        for (image, process) in &members {
            let immediate = unsafe { WaitForSingleObject(process.raw(), 0) };
            let pid = unsafe { windows::Win32::System::Threading::GetProcessId(process.raw()) };
            let created = process_created(process.raw()).unwrap();
            let mut code = 0;
            unsafe {
                windows::Win32::System::Threading::GetExitCodeProcess(process.raw(), &mut code)
            }
            .unwrap();
            let waited = unsafe {
                WaitForSingleObject(
                    process.raw(),
                    deadline
                        .saturating_duration_since(std::time::Instant::now())
                        .as_millis() as u32,
                )
            };
            let mut after_code = 0;
            unsafe {
                windows::Win32::System::Threading::GetExitCodeProcess(
                    process.raw(),
                    &mut after_code,
                )
            }
            .unwrap();
            eprintln!(
                "atomic startup exit observation: cut={cut} image={image} pid={pid} created={created} immediate={} waited={} before_code={code} after_code={after_code} drain_ms={drain_ms} observed_ms={} job_counts={:?}",
                immediate.0,
                waited.0,
                drain_started.elapsed().as_millis(),
                atomic_job_counts(active.record().unwrap())
            );
            assert_eq!(
                waited, WAIT_OBJECT_0,
                "original member did not exit: {image}"
            );
            // Diagnostic settlement must not erase the original failed gate:
            // accounting zero alone has not proven immediate process signaling.
            assert_eq!(
                immediate, WAIT_OBJECT_0,
                "original member was not signaled when job drain returned: {image}"
            );
        }
        drop(members);
        drop(observed);
        assert_eq!(atomic_job_counts(active.record().unwrap()), None);
        bootstrap_plan::lifecycle::resume::AFTER_RESUME_EVENT
            .with(|s| assert!(s.borrow_mut().take().is_none()));
        eprintln!("atomic start {cut}: preserved {}", fixture.root.display());
    }
}
