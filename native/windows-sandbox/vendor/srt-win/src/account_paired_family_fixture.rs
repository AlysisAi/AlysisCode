//! Test-only fixed process tree. Uses the running bootstrap's inherited strict
//! job and original process references; no job handles or arbitrary commands.
use super::*;
use std::{
    io::{Read, Write},
    os::windows::{io::AsRawHandle, process::CommandExt},
    process::{Child, Command, Stdio},
    time::Instant,
};
use windows::Win32::System::Pipes::PeekNamedPipe;

const TEST: &str = "account_control::bootstrap::entry::tests::paired_family_descendant_fixture";
const DEPTH: &str = "ALYSIS_TEST_PAIRED_DEPTH";
type Reference = (u64, u32, u64);

struct ChildGuard(Child);
impl Drop for ChildGuard {
    fn drop(&mut self) {
        if self.0.try_wait().ok().flatten().is_none() {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
}
fn spawn(depth: u32) -> Result<ChildGuard> {
    ensure!(
        depth == 1 || depth == 2,
        "family fixture depth outside bound"
    );
    let system = os_directory(true)?;
    Ok(ChildGuard(
        Command::new(std::env::current_exe()?)
            .args([
                "--exact",
                TEST,
                "--ignored",
                "--nocapture",
                "--test-threads=1",
            ])
            .env_clear()
            .env("SystemRoot", &system)
            .env("WINDIR", &system)
            .env(DEPTH, depth.to_string())
            .creation_flags(CREATE_NO_WINDOW.0)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()?,
    ))
}
fn retain(process: HANDLE) -> Result<OwnedHandle> {
    let mut raw = HANDLE::default();
    unsafe {
        DuplicateHandle(
            GetCurrentProcess(),
            process,
            GetCurrentProcess(),
            &mut raw,
            (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
            false,
            DUPLICATE_HANDLE_OPTIONS(0),
        )?;
    }
    Ok(OwnedHandle(raw))
}
fn describe(process: &OwnedHandle) -> Result<Reference> {
    ensure!(
        unsafe { WaitForSingleObject(process.raw(), 0) } == WAIT_TIMEOUT,
        "family process already exited"
    );
    Ok((
        process.raw().0 as usize as u64,
        unsafe { GetProcessId(process.raw()) },
        pull::creation_time(process.raw())?,
    ))
}

pub(super) fn descendant() -> ! {
    assert_eq!(
        std::env::args().skip(1).collect::<Vec<_>>(),
        [
            "--exact",
            TEST,
            "--ignored",
            "--nocapture",
            "--test-threads=1"
        ]
    );
    let depth = std::env::var(DEPTH).unwrap();
    assert!(depth == "1" || depth == "2");
    // The child retains its grandchild's original native process handle. The
    // parent pulls only query/synchronize rights from that original table.
    let _grandchild = if depth == "1" {
        let mut child = spawn(2).unwrap();
        assert_eq!(
            read_message(&mut child, b"alysis-family-ready:").unwrap(),
            b"true"
        );
        let reference = retain(HANDLE(child.0.as_raw_handle())).unwrap();
        println!(
            "alysis-family-reference:{}",
            serde_json::to_string(&describe(&reference).unwrap()).unwrap()
        );
        std::io::stdout().flush().unwrap();
        Some((child, reference))
    } else {
        println!("alysis-family-ready:true");
        std::io::stdout().flush().unwrap();
        None
    };
    // No successful natural return or destructor cleanup after readiness. The
    // controller rejects this watchdog code; normal explicit retirement is 1.
    std::thread::sleep(Duration::from_secs(30));
    std::process::exit(91);
}

fn read_message(child: &mut ChildGuard, prefix: &[u8]) -> Result<Vec<u8>> {
    let until = Instant::now() + Duration::from_secs(5);
    let mut bytes = Vec::new();
    loop {
        ensure!(
            child.0.try_wait()?.is_none(),
            "family child exited before its offer"
        );
        ensure!(
            Instant::now() < until && bytes.len() <= 2048,
            "family offer timeout/size limit"
        );
        let output = child.0.stdout.as_mut().context("family stdout missing")?;
        let mut available = 0;
        unsafe {
            PeekNamedPipe(
                HANDLE(output.as_raw_handle()),
                None,
                0,
                None,
                Some(&mut available),
                None,
            )?;
        }
        if available == 0 {
            std::thread::sleep(Duration::from_millis(5));
            continue;
        }
        let mut chunk = [0u8; 256];
        let n = output.read(&mut chunk[..(available as usize).min(256)])?;
        ensure!(n > 0, "family offer closed");
        bytes.extend_from_slice(&chunk[..n]);
        // libtest prefixes the child's stdout with its test name. Match only a
        // complete bounded fixture frame, not a guessed PID or external file.
        if let Some(start) = bytes.windows(prefix.len()).position(|w| w == prefix) {
            let body = &bytes[start + prefix.len()..];
            if let Some(end) = body.iter().position(|b| *b == b'\n') {
                return Ok(body[..end]
                    .strip_suffix(b"\r")
                    .unwrap_or(&body[..end])
                    .to_vec());
            }
        }
    }
}

pub(crate) struct Family {
    _child: ChildGuard,
    references: [OwnedHandle; 2],
}
impl Family {
    pub(crate) fn describe(&self) -> Result<[Reference; 2]> {
        Ok([
            describe(&self.references[0])?,
            describe(&self.references[1])?,
        ])
    }
    pub(crate) fn verify(&self, verify: impl Fn(HANDLE) -> Result<()>) -> Result<()> {
        for reference in &self.references {
            verify(reference.raw())?;
        }
        let references = self.describe()?;
        ensure!(references[0].1 != references[1].1, "family processes alias");
        Ok(())
    }
}

pub(crate) fn start() -> Result<Family> {
    let mut child = spawn(1)?;
    let child_ref = retain(HANDLE(child.0.as_raw_handle()))?;
    let (source, pid, created): Reference =
        serde_json::from_slice(&read_message(&mut child, b"alysis-family-reference:")?)?;
    ensure!(
        source > 0 && source < isize::MAX as u64,
        "invalid grandchild offer"
    );
    let mut raw = HANDLE::default();
    unsafe {
        DuplicateHandle(
            HANDLE(child.0.as_raw_handle()),
            HANDLE(source as usize as *mut _),
            GetCurrentProcess(),
            &mut raw,
            (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
            false,
            DUPLICATE_HANDLE_OPTIONS(0),
        )?;
    }
    let grandchild_ref = OwnedHandle(raw);
    ensure!(
        unsafe { GetProcessId(raw) } == pid && pull::creation_time(raw)? == created,
        "grandchild original identity differs"
    );
    Ok(Family {
        _child: child,
        references: [child_ref, grandchild_ref],
    })
}

pub(super) fn run(channel: &mut ControlChannel<'_>) -> Result<()> {
    let family = start()?;
    let image = PinnedRunnerImage::fixture(&std::env::current_exe()?)?;
    family.verify(|process| image.verify_process(process))?;
    channel.send(
        &serde_json::to_vec(&family.describe()?)?,
        Duration::from_secs(5),
    )?;
    // No command or graceful return is accepted. Explicit retirement or kernel
    // last-handle closure must stop the whole job before this distinct watchdog.
    std::thread::sleep(Duration::from_secs(30));
    std::process::exit(91);
}
