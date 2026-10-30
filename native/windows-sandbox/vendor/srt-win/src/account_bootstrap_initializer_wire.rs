//! Fixed suspended initializer inside the original trusted bootstrap job.
//! The broker pulls query/synchronize observation, never a pushed handle. No
//! arbitrary image/argv/environment, account credential, resume or ACL action.
use super::*;
use crate::account_state::execution::bootstrap_plan::initializer_image::SystemInitializerImage;
use crate::{launch::SpawnedChild, process_attributes::ProcThreadAttrs};
use serde::{Deserialize, Serialize};
use std::mem::size_of;
use windows::core::PWSTR;

const PREFIX: &[u8] = b"alysis-fixed-initializer-v2:";
#[path = "account_paired_initializer_wire.rs"]
pub(crate) mod paired;
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Request {
    pub operation: String,
    pub nonce: String,
    pub bootstrap_pid: u32,
    pub bootstrap_created: u64,
    pub container: String,
    pub package_sid: String,
    pub owner: String,
    pub session: u32,
    pub image_id: [u8; 24],
    #[cfg(test)]
    pub omit_profile_environment: bool,
}
impl Request {
    pub(crate) fn validate(&self) -> Result<()> {
        crate::account_install::validate_random_id(&self.operation)?;
        crate::account_install::validate_random_id(&self.nonce)?;
        crate::account_install::validate_random_id(
            self.container
                .strip_prefix("Alysis.Execution.")
                .context("initializer execution profile differs")?,
        )?;
        ensure!(
            self.operation != self.nonce
                && self.bootstrap_pid != 0
                && self.bootstrap_created != 0
                && self.image_id != [0; 24]
                && self.owner.len() <= 256
                && self.package_sid.len() <= 256,
            "initializer binding invalid"
        );
        let _owner = crate::sid::LocalPsid::from_string(&self.owner)?;
        ensure!(
            crate::appcontainer::derive(&self.container)?.string()? == self.package_sid,
            "initializer package differs from execution profile"
        );
        Ok(())
    }
    pub(crate) fn encode(&self) -> Result<Vec<u8>> {
        self.validate()?;
        let mut bytes = PREFIX.to_vec();
        bytes.extend(serde_json::to_vec(self)?);
        ensure!(bytes.len() <= 2048, "initializer request exceeds bounds");
        Ok(bytes)
    }
    fn decode(bytes: &[u8]) -> Result<Self> {
        ensure!(
            bytes.len() <= 2048 && bytes.starts_with(PREFIX),
            "initializer request format differs"
        );
        let value: Self = serde_json::from_slice(&bytes[PREFIX.len()..])
            .context("invalid initializer request")?;
        value.validate()?;
        Ok(value)
    }
    pub(crate) fn frame(&self, phase: &str, pid: u32, created: u64) -> Result<Vec<u8>> {
        ensure!(
            matches!(phase, "stop" | "stopped") && pid != 0 && created != 0,
            "initializer phase identity invalid"
        );
        let mut frame = self.encode()?;
        frame.extend_from_slice(phase.as_bytes());
        frame.extend_from_slice(&pid.to_le_bytes());
        frame.extend_from_slice(&created.to_le_bytes());
        Ok(frame)
    }
    fn require_recipient(&self) -> Result<()> {
        self.validate()?;
        crate::account_impersonation::require_process_identity()?;
        let mut session = 0;
        unsafe {
            windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
                std::process::id(),
                &mut session,
            )?;
        }
        ensure!(
            self.bootstrap_pid == std::process::id()
                && self.bootstrap_created
                    == super::super::pull::creation_time(unsafe { GetCurrentProcess() })?
                && self.owner == crate::sid::current_user_sid()?
                && self.session == session,
            "initializer recipient identity differs"
        );
        let mut in_job = false.into();
        unsafe {
            windows::Win32::System::JobObjects::IsProcessInJob(
                GetCurrentProcess(),
                None,
                &mut in_job,
            )?;
        }
        ensure!(
            in_job.as_bool(),
            "initializer source lacks inherited containment"
        );
        Ok(())
    }
}
#[derive(Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum Offer {
    Born {
        request: Request,
        source_handle: u64,
        pid: u32,
        created: u64,
    },
    Failed {
        request: Request,
        hresult: Option<i32>,
    },
}

/// Only the two closed request types below implement this private adapter.
/// A parsed domain supplies its fixed desktop and wire shape, never an image,
/// arbitrary arguments/environment, raw handle authority or a resume operation.
trait InitializerRequest {
    fn subject(&self) -> &Request;
    fn validate(&self) -> Result<()>;
    fn desktop_name(&self) -> Result<String>;
    fn frame(&self, phase: &str, pid: u32, created: u64) -> Result<Vec<u8>>;
    fn born_offer(&self, source_handle: u64, pid: u32, created: u64) -> Result<Vec<u8>>;
    fn failed_offer(&self, hresult: Option<i32>) -> Result<Vec<u8>>;
    fn parse_offer(&self, bytes: &[u8]) -> Result<OfferedProcess>;

    fn require_recipient(&self) -> Result<()> {
        self.validate()?;
        self.subject().require_recipient()
    }
}

/// Untrusted wire values only. The common receive adapter must pull from the
/// original source and verify the resulting native process before returning it.
struct OfferedProcess {
    source_handle: u64,
    pid: u32,
    created: u64,
}

fn source_failure(hresult: Option<i32>) -> Result<OfferedProcess> {
    if let Some(value) = hresult {
        ensure!(value < 0, "invalid initializer native failure");
        return Err(windows::core::Error::from_hresult(windows::core::HRESULT(
            value,
        )))
        .context("initializer source refused native creation");
    }
    anyhow::bail!("initializer source admission refused")
}

impl InitializerRequest for Request {
    fn subject(&self) -> &Request {
        self
    }
    fn validate(&self) -> Result<()> {
        Request::validate(self)
    }
    fn desktop_name(&self) -> Result<String> {
        self.validate()?;
        Ok(format!("WinSta0\\alysis-atomic-desktop-{}", self.operation))
    }
    fn frame(&self, phase: &str, pid: u32, created: u64) -> Result<Vec<u8>> {
        Request::frame(self, phase, pid, created)
    }
    fn born_offer(&self, source_handle: u64, pid: u32, created: u64) -> Result<Vec<u8>> {
        Ok(serde_json::to_vec(&Offer::Born {
            request: self.clone(),
            source_handle,
            pid,
            created,
        })?)
    }
    fn failed_offer(&self, hresult: Option<i32>) -> Result<Vec<u8>> {
        Ok(serde_json::to_vec(&Offer::Failed {
            request: self.clone(),
            hresult,
        })?)
    }
    fn parse_offer(&self, bytes: &[u8]) -> Result<OfferedProcess> {
        self.validate()?;
        ensure!(bytes.len() <= 4096, "initializer offer exceeds bounds");
        let offer: Offer = serde_json::from_slice(bytes).context("invalid initializer offer")?;
        match offer {
            Offer::Born {
                request,
                source_handle,
                pid,
                created,
            } => {
                ensure!(&request == self, "initializer offer binding differs");
                Ok(OfferedProcess {
                    source_handle,
                    pid,
                    created,
                })
            }
            Offer::Failed { request, hresult } => {
                ensure!(&request == self, "initializer failure binding differs");
                source_failure(hresult)
            }
        }
    }
}

/// Read the first selection frame, admitting at most one initializer request
/// before credentials. A repeated initializer is parsed as an invalid selection.
pub(super) fn before_selection(channel: &mut ControlChannel<'_>) -> Result<Vec<u8>> {
    let first = channel.receive(TIMEOUT)?;
    if first.starts_with(PREFIX) {
        return serve(channel, &Request::decode(&first)?);
    }
    if first.starts_with(paired::PREFIX) {
        return serve(channel, &paired::Request::decode(&first)?);
    }
    Ok(first)
}

/// Field order is load-bearing on every error: stop the original child before
/// releasing the image pin. Keep the bundle intact through stop acknowledgment
/// and the following selection frame rather than reversing local drop order.
struct SuspendedInitializer {
    child: Option<SpawnedChild>,
    image: SystemInitializerImage,
}

fn serve(channel: &mut ControlChannel<'_>, request: &impl InitializerRequest) -> Result<Vec<u8>> {
    request.require_recipient()?;
    let born = create_suspended(request);
    let mut born = match born {
        Ok(value) => value,
        Err(error) => {
            let hresult = error
                .chain()
                .find_map(|e| e.downcast_ref::<windows::core::Error>())
                .map(|e| e.code().0);
            channel.send(&request.failed_offer(hresult)?, TIMEOUT)?;
            // Keep the authenticated peer alive until its broker receives the
            // native failure, then let the original child/job guard terminate
            // this process. No second operation or retry can be dispatched.
            let _refused = FrameStorage(channel.receive(TIMEOUT)?);
            return Err(error).context("fixed initializer source failed; no retry");
        }
    };
    let process = born
        .child
        .as_ref()
        .context("initializer original child absent")?
        .process();
    let pid = unsafe { GetProcessId(process) };
    let created = super::super::pull::creation_time(process)?;
    channel.send(
        &request.born_offer(process.0 as usize as u64, pid, created)?,
        TIMEOUT,
    )?;
    ensure!(
        channel.receive(TIMEOUT)? == request.frame("stop", pid, created)?,
        "initializer stop request differs"
    );
    request.require_recipient()?;
    ensure!(
        unsafe { WaitForSingleObject(process, 0) } == WAIT_TIMEOUT,
        "initializer exited before committed stop"
    );
    unsafe {
        TerminateProcess(process, 0)?;
    }
    ensure!(
        unsafe { WaitForSingleObject(process, 5000) } == WAIT_OBJECT_0,
        "initializer source termination unverified"
    );
    drop(born.child.take());
    channel.send(&request.frame("stopped", pid, created)?, TIMEOUT)?;
    // Deliberately no dispatch loop: a second initializer frame is returned to
    // the credential-selection decoder and refused there, in either domain.
    channel.receive(TIMEOUT)
}
fn create_suspended(request: &impl InitializerRequest) -> Result<SuspendedInitializer> {
    request.require_recipient()?;
    let subject = request.subject();
    let image = SystemInitializerImage::prepare(&subject.owner)?;
    ensure!(
        image.identity()? == subject.image_id,
        "initializer system image differs from broker pin"
    );
    let package = crate::appcontainer::derive(&subject.container)?;
    let capabilities = windows::Win32::Security::SECURITY_CAPABILITIES {
        AppContainerSid: package.raw(),
        ..Default::default()
    };
    let mut attrs = ProcThreadAttrs::new(1)?;
    unsafe {
        attrs.set_security_capabilities(&capabilities)?;
    }
    let path = image
        .application()?
        .to_str()
        .context("initializer image is not Unicode")?;
    let directory = image
        .application()?
        .parent()
        .context("initializer system directory absent")?
        .to_str()
        .context("initializer directory is not Unicode")?;
    let app = wstr(path);
    let mut command = wstr(&format!("{} /d /c exit 0", crate::launch::quote_arg(path)));
    let cwd = wstr(directory);
    let mut desktop = wstr(&request.desktop_name()?);
    #[cfg(test)]
    struct ResetEnvironment;
    #[cfg(test)]
    impl Drop for ResetEnvironment {
        fn drop(&mut self) {
            crate::account_state::execution::bootstrap_plan::initializer_image::OMIT_PROFILE_ENVIRONMENT.with(|s|s.set(false));
        }
    }
    #[cfg(test)]
    let _reset = ResetEnvironment;
    #[cfg(test)]
    crate::account_state::execution::bootstrap_plan::initializer_image::OMIT_PROFILE_ENVIRONMENT
        .with(|s| s.set(subject.omit_profile_environment));
    let mut environment = image.environment()?;
    let mut startup = STARTUPINFOEXW::default();
    startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
    startup.StartupInfo.lpDesktop = PWSTR(desktop.as_mut_ptr());
    startup.lpAttributeList = attrs.list();
    let mut pi = PROCESS_INFORMATION::default();
    request.require_recipient()?;
    image.application()?;
    // The source was born atomically in the original strict job. Normal child
    // inheritance supplies that job at birth; no breakaway or assignment path.
    unsafe {
        CreateProcessW(
            pcwstr(&app),
            Some(PWSTR(command.as_mut_ptr())),
            None,
            None,
            false,
            CREATE_SUSPENDED
                | CREATE_NO_WINDOW
                | CREATE_UNICODE_ENVIRONMENT
                | EXTENDED_STARTUPINFO_PRESENT,
            Some(environment.as_mut_ptr().cast()),
            pcwstr(&cwd),
            &startup.StartupInfo,
            &mut pi,
        )
    }
    .context("create fixed suspended initializer in original bootstrap job")?;
    let born = SuspendedInitializer {
        child: Some(SpawnedChild::new(pi)),
        image,
    };
    born.image.verify_process(
        born.child
            .as_ref()
            .context("initializer original child absent")?
            .process(),
    )?;
    Ok(born)
}
/// Duplicate only query/synchronize rights from the exact original bootstrap,
/// then prove original-job membership before returning any observation.
pub(crate) fn receive_process(
    bootstrap: &super::super::SuspendedBootstrap<'_>,
    channel: &mut ControlChannel<'_>,
    request: &Request,
) -> Result<OwnedHandle> {
    receive_process_inner(bootstrap, channel, request)
}

fn receive_process_inner(
    bootstrap: &super::super::SuspendedBootstrap<'_>,
    channel: &mut ControlChannel<'_>,
    request: &impl InitializerRequest,
) -> Result<OwnedHandle> {
    request.validate()?;
    let subject = request.subject();
    let peer = bootstrap.peer()?;
    ensure!(
        unsafe { CompareObjectHandles(bootstrap.child.process(), channel.peer.process.raw()) }
            .as_bool()
            && subject.bootstrap_pid == peer.pid
            && subject.bootstrap_created == super::super::pull::creation_time(peer.process.raw())?,
        "initializer request/channel differs from original bootstrap"
    );
    let bytes = channel.receive(TIMEOUT)?;
    let OfferedProcess {
        source_handle,
        pid,
        created,
    } = request.parse_offer(&bytes)?;
    ensure!(
        source_handle > 0
            && source_handle < isize::MAX as u64
            && pid > 0
            && pid != subject.bootstrap_pid
            && created >= subject.bootstrap_created,
        "initializer source identity invalid"
    );
    bootstrap.peer()?;
    let mut raw = HANDLE::default();
    unsafe {
        DuplicateHandle(
            bootstrap.child.process(),
            HANDLE(source_handle as usize as *mut _),
            GetCurrentProcess(),
            &mut raw,
            (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
            false,
            DUPLICATE_HANDLE_OPTIONS(0),
        )?;
    }
    let process = OwnedHandle(raw);
    super::super::noninheritable(process.raw())?;
    ensure!(
        unsafe { GetProcessId(process.raw()) } == pid
            && super::super::pull::creation_time(process.raw())? == created,
        "pulled initializer identity differs"
    );
    let _peer = ExpectedPeer::retain(process.raw())?.with_job(bootstrap.job)?;
    bootstrap.peer()?;
    Ok(process)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn request() -> Request {
        let container = "Alysis.Execution.0123456789abcdef0123456789abcdef".to_owned();
        Request {
            operation: "1123456789abcdef0123456789abcdef".into(),
            nonce: "2123456789abcdef0123456789abcdef".into(),
            bootstrap_pid: 10,
            bootstrap_created: 100,
            package_sid: crate::appcontainer::derive(&container)
                .unwrap()
                .string()
                .unwrap(),
            container,
            owner: "S-1-5-21-1-2-3-1001".into(),
            session: 1,
            image_id: [1; 24],
            omit_profile_environment: false,
        }
    }
    #[test]
    fn initializer_wire_rejects_cross_scope_unknown_and_oversized_requests() {
        let good = request();
        assert_eq!(Request::decode(&good.encode().unwrap()).unwrap(), good);
        for case in 0..8 {
            let mut bad = good.clone();
            match case {
                0 => bad.nonce = bad.operation.clone(),
                1 => bad.bootstrap_pid = 0,
                2 => bad.bootstrap_created = 0,
                3 => bad.container = "Other.Profile".into(),
                4 => bad.package_sid = "S-1-1-0".into(),
                5 => bad.image_id = [0; 24],
                6 => bad.owner = "invalid".into(),
                _ => bad.operation = "x".repeat(4096),
            }
            assert!(bad.encode().is_err(), "case {case}");
        }
        let mut value = serde_json::to_value(&good).unwrap();
        value["argv"] = serde_json::json!(["arbitrary.exe"]);
        let mut bytes = PREFIX.to_vec();
        bytes.extend(serde_json::to_vec(&value).unwrap());
        assert!(Request::decode(&bytes).is_err());
        assert!(Request::decode(&vec![b'x'; 2049]).is_err());
        assert!(good.frame("resume", 11, 101).is_err());
        assert_ne!(
            good.frame("stop", 11, 101).unwrap(),
            good.frame("stopped", 11, 101).unwrap()
        );
        assert_ne!(
            good.frame("stop", 11, 101).unwrap(),
            good.frame("stop", 11, 102).unwrap()
        );
    }
    #[test]
    #[ignore = "read-only child invoked by bounded initializer environment test"]
    fn initializer_source_environment_query_child() {
        let image =
            SystemInitializerImage::prepare(&crate::sid::current_user_sid().unwrap()).unwrap();
        assert!(!image.environment().unwrap().is_empty());
    }
    #[test]
    fn initializer_source_environment_works_without_inherited_profile_variables() {
        use std::process::{Command, Stdio};
        let image =
            SystemInitializerImage::prepare(&crate::sid::current_user_sid().unwrap()).unwrap();
        let environment = image.environment().unwrap();
        let entries = environment
            .split(|c| *c == 0)
            .filter(|p| !p.is_empty())
            .map(|p| String::from_utf16(p).unwrap())
            .collect::<Vec<_>>();
        let mut command = Command::new(std::env::current_exe().unwrap());
        command.args(["account_control::bootstrap::entry::initializer_wire::tests::initializer_source_environment_query_child","--exact","--ignored","--test-threads=1"])
            .env_clear().stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
        for entry in entries {
            let (key, value) = entry.split_once('=').unwrap();
            if ["SystemRoot", "WINDIR", "PATH"].contains(&key) {
                command.env(key, value);
            }
        }
        let mut child = command.spawn().unwrap();
        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        loop {
            if let Some(status) = child.try_wait().unwrap() {
                assert!(
                    status.success(),
                    "minimal-environment query child failed: {status}"
                );
                break;
            }
            if std::time::Instant::now() >= deadline {
                child.kill().unwrap();
                child.wait().unwrap();
                panic!("bounded initializer environment query timed out");
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }
}
