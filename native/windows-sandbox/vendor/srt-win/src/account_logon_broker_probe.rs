//! Synthetic broker crash fixture. The observer never receives a job handle.
use super::*;

const BROKER: &str = "account_control::bootstrap::tests::logon_inflight::death::broker::bootstrap_inflight_broker_fixture";

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Offer {
    process: usize,
    pid: u32,
    created: u64,
}

#[test]
#[ignore = "bounded private broker fixture; invoked only by logon diagnostics"]
fn bootstrap_inflight_broker_fixture() {
    let target = PathBuf::from(std::env::var_os("ALYSIS_TEST_INFLIGHT_IMAGE").unwrap());
    assert!(target.is_absolute() && target.file_name().unwrap() == "never-resumed.exe");
    let image = image();
    let job = Job::new(false).unwrap();
    let child = create_worker(&image, &job, &target, false).unwrap();
    image.verify_process(child.process()).unwrap();
    let mut member = windows::core::BOOL::default();
    unsafe {
        windows::Win32::System::JobObjects::IsProcessInJob(
            child.process(),
            Some(job.raw()),
            &mut member,
        )
    }
    .unwrap();
    assert!(member.as_bool());
    save_once(
        &target.parent().unwrap().join("broker-offer.json"),
        &Offer {
            process: child.process().0 as usize,
            pid: unsafe { GetProcessId(child.process()) },
            created: process_created(child.process()).unwrap(),
        },
    )
    .unwrap();
    assert_eq!(unsafe { ResumeThread(child.thread()) }, 1);
    assert_eq!(
        unsafe { WaitForSingleObject(child.process(), 15000) },
        WAIT_OBJECT_0
    );
    let mut exit = u32::MAX;
    unsafe { GetExitCodeProcess(child.process(), &mut exit) }.unwrap();
    assert_eq!(exit, 0);
    // Normal control alone runs these destructors. Forced broker death must
    // close its last job reference without executing any Rust cleanup code.
    drop(child);
    drop(job);
}

fn spawn_broker(image: &PinnedRunnerImage, target: &Path) -> Result<SpawnedChild> {
    let executable = image
        .application()?
        .to_str()
        .context("non-Unicode broker")?;
    let app = wstr(executable);
    let mut command = wstr(&format!(
        "{} --exact {BROKER} --ignored --nocapture --test-threads=1",
        crate::launch::quote_arg(executable)
    ));
    let system = os_directory(false)?;
    let windows = os_directory(true)?;
    let mut env = crate::launch::build_env_block_with_inheritance(
        &[
            ("SystemRoot".into(), windows.clone()),
            ("WINDIR".into(), windows),
            ("PATH".into(), system.clone()),
            (
                "ALYSIS_TEST_INFLIGHT_IMAGE".into(),
                target.to_str().context("non-Unicode target")?.into(),
            ),
        ],
        false,
    );
    let startup = STARTUPINFOW {
        cb: size_of::<STARTUPINFOW>() as u32,
        ..Default::default()
    };
    let mut info = PROCESS_INFORMATION::default();
    unsafe {
        CreateProcessW(
            pcwstr(&app),
            Some(windows::core::PWSTR(command.as_mut_ptr())),
            None,
            None,
            false,
            CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
            Some(env.as_mut_ptr().cast()),
            pcwstr(&wstr(&system)),
            &startup,
            &mut info,
        )
    }?;
    let broker = SpawnedChild::new(info);
    image.verify_process(broker.process())?;
    ensure!(
        unsafe { ResumeThread(broker.thread()) } == 1,
        "broker initial suspension differs"
    );
    Ok(broker)
}

pub(super) enum KernelWorker {
    Direct {
        child: SpawnedChild,
        job: Option<Job>,
    },
    Separate {
        broker: SpawnedChild,
        worker: OwnedHandle,
    },
}
impl KernelWorker {
    pub(super) fn start(image: &PinnedRunnerImage, target: &Path, separate: bool) -> Result<Self> {
        if !separate {
            let job = Job::new(false)?;
            let child = create_worker(image, &job, target, false)?;
            image.verify_process(child.process())?;
            ensure!(
                unsafe { ResumeThread(child.thread()) } == 1,
                "worker initial suspension differs"
            );
            return Ok(Self::Direct {
                child,
                job: Some(job),
            });
        }
        let broker = spawn_broker(image, target)?;
        let offer = target.parent().unwrap().join("broker-offer.json");
        let deadline = Instant::now() + Duration::from_secs(5);
        while !offer.exists() && Instant::now() < deadline {
            ensure!(
                unsafe { WaitForSingleObject(broker.process(), 10) } == WAIT_TIMEOUT,
                "broker exited before original worker offer"
            );
        }
        let offer: Offer = read_small(&offer)?;
        ensure!(
            offer.process > 0 && offer.process < isize::MAX as usize,
            "invalid original worker reference"
        );
        let mut raw = HANDLE::default();
        unsafe {
            DuplicateHandle(
                broker.process(),
                HANDLE(offer.process as *mut _),
                GetCurrentProcess(),
                &mut raw,
                (PROCESS_QUERY_LIMITED_INFORMATION
                    | PROCESS_SYNCHRONIZE
                    | PROCESS_VM_READ
                    | PROCESS_DUP_HANDLE)
                    .0,
                false,
                DUPLICATE_HANDLE_OPTIONS(0),
            )
        }?;
        let worker = OwnedHandle(raw);
        image.verify_process(worker.raw())?;
        ensure!(
            unsafe { GetProcessId(worker.raw()) } == offer.pid
                && process_created(worker.raw())? == offer.created,
            "original worker identity differs"
        );
        Ok(Self::Separate { broker, worker })
    }
    pub(super) fn process(&self) -> HANDLE {
        match self {
            Self::Direct { child, .. } => child.process(),
            Self::Separate { worker, .. } => worker.raw(),
        }
    }
    pub(super) fn cut(&mut self) -> Result<()> {
        match self {
            Self::Direct { job, .. } => {
                let original = job.take().context("job close already attempted")?;
                let raw = original.raw();
                std::mem::forget(original);
                unsafe { CloseHandle(raw) }?;
            }
            Self::Separate { broker, .. } => {
                unsafe { TerminateProcess(broker.process(), 77) }?;
                ensure!(
                    unsafe { WaitForSingleObject(broker.process(), 5000) } == WAIT_OBJECT_0,
                    "original broker exit unverified"
                );
                let mut exit = u32::MAX;
                unsafe { GetExitCodeProcess(broker.process(), &mut exit) }?;
                ensure!(exit == 77, "broker did not exit through the forced cut");
            }
        }
        Ok(())
    }
    pub(super) fn verify_control_exit(&self) -> Result<()> {
        if let Self::Separate { broker, .. } = self {
            ensure!(
                unsafe { WaitForSingleObject(broker.process(), 5000) } == WAIT_OBJECT_0,
                "live broker did not finish"
            );
            let mut exit = u32::MAX;
            unsafe { GetExitCodeProcess(broker.process(), &mut exit) }?;
            ensure!(exit == 0, "live broker failed ({exit})");
        }
        Ok(())
    }
}

#[test]
fn kernel_broker_fixture_releases_original_suspended_child_in_live_control() {
    let root = crate::util::test_temp_dir().join(format!(
        "logon-broker-control-{}",
        crate::account_state::execution::random_id().unwrap()
    ));
    std::fs::create_dir(&root).unwrap();
    protect(&root).unwrap();
    let target = root.join("never-resumed.exe");
    std::fs::copy(std::env::current_exe().unwrap(), &target).unwrap();
    protect(&target).unwrap();
    let image = image();
    let worker = KernelWorker::start(&image, &target, true).unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    while !root.join("returned.json").exists() && Instant::now() < deadline {
        assert_eq!(
            unsafe { WaitForSingleObject(worker.process(), 10) },
            WAIT_TIMEOUT
        );
    }
    let result: serde_json::Value = read_small(&root.join("returned.json")).unwrap();
    let mut raw = HANDLE::default();
    unsafe {
        DuplicateHandle(
            worker.process(),
            HANDLE(result["process"].as_u64().unwrap() as usize as *mut _),
            GetCurrentProcess(),
            &mut raw,
            (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
            false,
            DUPLICATE_HANDLE_OPTIONS(0),
        )
    }
    .unwrap();
    let original = OwnedHandle(raw);
    PinnedRunnerImage::fixture(&target)
        .unwrap()
        .verify_process(original.raw())
        .unwrap();
    save_once(&root.join("release.json"), &true).unwrap();
    assert_eq!(
        unsafe { WaitForSingleObject(worker.process(), 5000) },
        WAIT_OBJECT_0
    );
    assert_eq!(
        unsafe { WaitForSingleObject(original.raw(), 5000) },
        WAIT_OBJECT_0
    );
    worker.verify_control_exit().unwrap();
}
