//! Opt-in PSEC adapter. Never invokes MXC's backend dispatcher or host ACL setup.
//! The protocol is deliberately distinct from the installed AppContainer helper.
use anyhow::{Context, Result, ensure};
use mxc_sdk::mxc_common::{
    cmdline::{CommandLineContext, cmdline_from_argv_for_context},
    logger::{Logger, Mode},
    models::{
        BaseProcessUiConfig, ContainerPolicy, ExecutionRequest, FallbackPolicy, NetworkAction,
        NetworkEgressPolicy, NetworkIngressPolicy, UiPolicy,
    },
    sandbox_process::{SandboxBackend, StdioMode},
};
use mxc_sdk::process_container_common::base_container_runner::BaseContainerRunner;
use serde::{Deserialize, Serialize};
use serde_json::json;
use srt_win::util::OwnedHandle;
use std::{
    collections::BTreeSet,
    io::Read,
    mem::{ManuallyDrop, size_of, size_of_val},
    os::windows::fs::MetadataExt,
    path::{Component, Path},
    time::{Duration, Instant},
};
use windows::{
    Win32::{
        Foundation::{ERROR_ACCESS_DENIED, FILETIME, HANDLE, WAIT_TIMEOUT},
        System::{
            JobObjects::{
                AssignProcessToJobObject, CreateJobObjectW, IsProcessInJob,
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOBOBJECT_BASIC_ACCOUNTING_INFORMATION,
                JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectBasicAccountingInformation,
                JobObjectExtendedLimitInformation, QueryInformationJobObject,
                SetInformationJobObject,
            },
            Threading::{
                GetCurrentProcess, GetProcessTimes, INFINITE, OpenProcess, PROCESS_CREATE_PROCESS,
                PROCESS_CREATE_THREAD, PROCESS_DUP_HANDLE, PROCESS_QUERY_LIMITED_INFORMATION,
                PROCESS_SYNCHRONIZE, PROCESS_TERMINATE, PROCESS_VM_WRITE, WaitForSingleObject,
            },
        },
    },
    core::PCWSTR,
};

const PROTOCOL: u32 = 4;
const MAX_REQUEST: u64 = 1024 * 1024;
const UPSTREAM: &str = "c6f301d53a1430c4c921a05c57af838f7392348f";

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
enum Network {
    Off,
    On,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Request {
    protocol: u32,
    cwd: String,
    argv: Vec<String>,
    read: Vec<String>,
    #[serde(default)]
    enumerate: Vec<String>,
    #[serde(default)]
    discovery_deny: Vec<String>,
    write: Vec<String>,
    deny_read: Vec<String>,
    deny_write: Vec<String>,
    env: Vec<(String, String)>,
    network: Network,
    #[serde(default)]
    local_preview: bool,
    parent_pid: u32,
    parent_created: u64,
    timeout_ms: u64,
}

pub fn dispatch(operation: Option<&str>) -> Option<Result<i32>> {
    match operation {
        Some("mxc-protocol") => Some(Ok({
            println!(
                "{}",
                json!({"protocol":PROTOCOL,"security_revision":11,
                "implementation":"alysis-windows-sandbox","containment":"mxc-psec",
                "qualification":"prototype-unqualified","upstream_commit":UPSTREAM,
                "network_modes":["off","on"],"host_acl_mutation":false,
                "ancestor_grants":false,"automatic_fallback":false,
                "metadata_reservations":"persistent-protected-directories",
                "scoped_local_preview":true,"directory_discovery":true})
            );
            0
        })),
        Some("mxc-probe") => Some(probe()),
        Some("mxc-process-access-probe") => Some(process_access_probe()),
        Some("mxc-run-request") => Some(run()),
        _ => None,
    }
}

// Read-only qualification operation: open/close rights on a known live synthetic
// owner. It never duplicates handles, writes memory or terminates another process.
fn process_access_probe() -> Result<i32> {
    let args: Vec<_> = std::env::args().skip(2).collect();
    ensure!(args.len() == 2, "process probe requires PID and allow/deny");
    let pid: u32 = args[0].parse()?;
    ensure!(pid > 4, "invalid process probe target");
    ensure!(
        matches!(args[1].as_str(), "allow" | "deny"),
        "invalid probe expectation"
    );
    let mut passed = true;
    for (name, access) in [
        ("duplicate_handles", PROCESS_DUP_HANDLE),
        ("spoof_parent", PROCESS_CREATE_PROCESS),
        ("create_thread", PROCESS_CREATE_THREAD),
        ("write_memory", PROCESS_VM_WRITE),
        ("terminate", PROCESS_TERMINATE),
    ] {
        let (allowed, denied) = match unsafe { OpenProcess(access, false, pid) } {
            Ok(handle) => {
                drop(OwnedHandle(handle));
                (true, false)
            }
            Err(error) => (false, error.code() == ERROR_ACCESS_DENIED.to_hresult()),
        };
        let ok = if args[1] == "allow" { allowed } else { denied };
        passed &= ok;
        println!(
            "{}",
            json!({"right":name,"allowed":allowed,"access_denied":denied,"ok":ok})
        );
    }
    Ok(if passed { 0 } else { 1 })
}

fn probe() -> Result<i32> {
    // This is a native validation probe, not an installation or a workload launch.
    let runner = BaseContainerRunner::new();
    let request = ExecutionRequest {
        env: Some(vec![
            "SYSTEMROOT=C:\\Windows".into(),
            "LOCALAPPDATA=C:\\Windows".into(),
        ]),
        policy: ContainerPolicy {
            fallback: FallbackPolicy {
                allow_dacl_mutation: false,
            },
            ..Default::default()
        },
        ..Default::default()
    };
    let available = runner.validate(&request).is_ok();
    println!(
        "{}",
        json!({"containment":"mxc-psec","available":available,
        "qualification":"prototype-unqualified","automatic_fallback":false,
        "request_policy_checked_at_launch":true,"upstream_commit":UPSTREAM})
    );
    Ok(if available { 0 } else { 125 })
}

fn path_key(path: &str) -> String {
    path.replace('/', "\\")
        .trim_end_matches('\\')
        .to_lowercase()
}

fn contains(root: &str, path: &str) -> bool {
    let root = path_key(root);
    let path = path_key(path);
    path == root
        || path
            .strip_prefix(&root)
            .is_some_and(|tail| tail.starts_with('\\'))
}

fn validate_path(path: &str) -> Result<()> {
    let bytes = path.as_bytes();
    ensure!(
        bytes.len() >= 3
            && bytes[0].is_ascii_alphabetic()
            && bytes[1] == b':'
            && matches!(bytes[2], b'\\' | b'/')
            && !path.contains('\0')
            && !path[2..].contains(':'),
        "MXC requires a normal absolute local drive path"
    );
    ensure!(
        !Path::new(path)
            .components()
            .any(|part| matches!(part, Component::ParentDir | Component::CurDir)),
        "MXC paths cannot contain relative components"
    );
    ensure!(
        !path
            .split(['\\', '/'])
            .skip(1)
            .any(|part| part.ends_with(['.', ' '])),
        "MXC paths cannot contain ambiguous trailing dots or spaces"
    );
    Ok(())
}

fn validate_existing_path(path: &str) -> Result<()> {
    validate_path(path)?;
    for ancestor in Path::new(path).ancestors() {
        let metadata = std::fs::symlink_metadata(ancestor)
            .context("MXC prototype requires existing, inspectable policy paths")?;
        ensure!(
            metadata.file_attributes() & 0x400 == 0,
            "MXC policy paths cannot traverse reparse points"
        );
    }
    let canonical = std::fs::canonicalize(path).context("cannot canonicalize MXC policy path")?;
    let canonical = canonical
        .to_str()
        .context("MXC policy path is not Unicode")?;
    let canonical = canonical.strip_prefix(r"\\?\").unwrap_or(canonical);
    ensure!(
        path_key(canonical) == path_key(path),
        "MXC prototype rejects policy aliases; use canonical long paths"
    );
    Ok(())
}

fn validate_denied_path(path: &str) -> Result<()> {
    // Native qualification found that absent PSEC carveouts do not prevent
    // later creation. Refuse, rather than silently dropping that protection.
    validate_existing_path(path).context("MXC protected paths must already exist")
}

fn protection_plan(request: &Request) -> Result<Vec<&str>> {
    let mut pins = Vec::new();
    for path in &request.deny_write {
        srt_win::path_id::validate_placeholder_path(path)?;
        let writable = request.write.iter().any(|root| contains(root, path));
        match std::fs::symlink_metadata(path) {
            Ok(_) => validate_existing_path(path)?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                // With no writable grants, these missing exclusions are already
                // covered by the global read-only ceiling. Never create them.
                if request.write.is_empty() {
                    continue;
                }
                ensure!(
                    writable && path.ends_with(['/', '\\']),
                    "missing MXC protected files require prior instruction reservation; directory reservations must be inside a writable root"
                );
            }
            Err(error) => return Err(error).context("cannot inspect MXC protection target"),
        }
        if writable {
            pins.push(path.as_str());
        }
    }
    Ok(pins)
}

fn writable_pin_ancestor(request: &Request, path: &str) -> bool {
    let local = srt_win::path_id::strip_extended_prefix(path);
    request.write.iter().any(|root| contains(root, local))
        && !request
            .deny_write
            .iter()
            .chain(&request.deny_read)
            .any(|protected| contains(protected, local))
}

fn retain_protections(
    request: &Request,
    plan: &[&str],
) -> Result<Vec<srt_win::path_id::RetainedPlaceholderChain>> {
    let mut guards = Vec::new();
    for path in plan {
        let guard = srt_win::path_id::create_placeholder_chain_retained_with_writable_ancestors(
            path,
            path.ends_with(['/', '\\']),
            |intent| {
                let local = srt_win::path_id::strip_extended_prefix(intent);
                ensure!(
                    request
                        .write
                        .iter()
                        .any(|root| contains(root, local) && path_key(root) != path_key(local)),
                    "MXC reservation would escape a writable root"
                );
                ensure!(
                    path.ends_with(['/', '\\']) || path_key(local) != path_key(path),
                    "MXC instruction file changed before original-handle admission"
                );
                Ok(())
            },
            // These directories contain ordinary writable work. Holding them
            // without write sharing also blocks Windows' rename-target open.
            // Keep no-delete pins; the shared API never relaxes protected leaves.
            |ancestor| writable_pin_ancestor(request, ancestor),
        )?;
        guard.verify()?;
        guards.push(guard);
    }
    Ok(guards)
}

fn translate(request: &Request) -> Result<ExecutionRequest> {
    ensure!(
        request.protocol == PROTOCOL,
        "unsupported MXC request protocol"
    );
    ensure!(!request.argv.is_empty(), "MXC request needs an executable");
    ensure!(
        (1..=86_400_000).contains(&request.timeout_ms),
        "invalid MXC execution timeout"
    );
    ensure!(
        request.parent_pid > 4 && request.parent_created > 0,
        "missing engine identity"
    );
    validate_path(&request.cwd)?;
    validate_path(&request.argv[0])?;
    ensure!(
        !request.argv.iter().any(|arg| arg.contains('\0')),
        "invalid MXC argv"
    );
    for path in request
        .read
        .iter()
        .chain(&request.write)
        .chain(&request.deny_read)
        .chain(&request.deny_write)
        .chain(&request.enumerate)
        .chain(&request.discovery_deny)
    {
        validate_path(path)?;
    }
    ensure!(
        request
            .read
            .iter()
            .chain(&request.write)
            .any(|root| contains(root, &request.cwd)),
        "MXC cwd must be inside a readable grant"
    );
    ensure!(
        request.write.iter().all(|path| path_key(path).len() > 2),
        "MXC forbids drive-wide writes"
    );
    ensure!(request.discovery_deny.iter().all(|boundary| !request.read.iter()
        .chain(&request.write).any(|grant| contains(grant, boundary))),
        "MXC discovery boundaries cannot be inside a content grant");
    ensure!(
        !request
            .deny_read
            .iter()
            .any(|path| contains(path, &request.cwd)),
        "MXC cwd is denied"
    );
    let mut keys = BTreeSet::new();
    for (key, value) in &request.env {
        ensure!(
            !key.is_empty()
                && !key.contains(['=', '\0'])
                && !value.contains('\0')
                && keys.insert(key.to_uppercase()),
            "invalid or duplicate MXC environment name"
        );
    }
    ensure!(
        keys.contains("SYSTEMROOT") && keys.contains("LOCALAPPDATA"),
        "MXC requires SYSTEMROOT and LOCALAPPDATA"
    );
    let mut reads = request.read.clone();
    // A read-only carveout must never create new read access outside positive roots.
    for protected in &request.deny_write {
        if request
            .read
            .iter()
            .chain(&request.write)
            .any(|root| contains(root, protected))
        {
            reads.push(protected.clone());
        }
    }
    let mut writes = request.write.clone();
    writes.retain(|path| {
        !request
            .deny_write
            .iter()
            .chain(&request.deny_read)
            .any(|deny| contains(deny, path))
    });
    reads.retain(|path| {
        !writes.iter().any(|write| path_key(path) == path_key(write))
            && !request.deny_read.iter().any(|deny| contains(deny, path))
    });
    reads.sort();
    reads.dedup();
    Ok(ExecutionRequest {
        script_code: cmdline_from_argv_for_context(
            &request.argv,
            CommandLineContext::WindowsCreateProcess,
        )?,
        working_directory: request.cwd.clone(),
        env: Some(
            request
                .env
                .iter()
                .map(|(k, v)| format!("{k}={v}"))
                .collect(),
        ),
        inherit_default_env: false,
        // The adapter owns deadline and strict drain before any upstream teardown.
        script_timeout: 0,
        policy: ContainerPolicy {
            readwrite_paths: writes,
            readonly_paths: reads,
            // Ordinary private roots remove all overlapping positive grants
            // above. Discovery boundaries instead preserve narrower admitted
            // tools/workspaces using native most-specific-object precedence.
            denied_paths: request.deny_read.iter().chain(&request.discovery_deny).cloned().collect(),
            // Native directory/metadata access is separate from file contents.
            // Never translate enumeration into readonly_paths or host ACL grants.
            enumerate_paths: request.enumerate.clone(),
            capabilities: vec!["registryRead".into()],
            fallback: FallbackPolicy {
                allow_dacl_mutation: false,
            },
            network_egress: Some(NetworkEgressPolicy {
                default: if request.network == Network::On {
                    NetworkAction::Allow
                } else {
                    NetworkAction::Deny
                },
                ..Default::default()
            }),
            network_ingress: Some(NetworkIngressPolicy {
                default: NetworkAction::Deny,
                host_loopback: if request.local_preview {
                    NetworkAction::Allow
                } else {
                    NetworkAction::Deny
                },
            }),
            network_specified: true,
            network_mode_specified: true,
            ui: UiPolicy {
                disable: false,
                ..Default::default()
            },
            base_process_ui: BaseProcessUiConfig {
                // "desktop" permits access to external USER objects in MXC.
                // Keep Win32k available for PowerShell while denying host UI.
                isolation: "container".into(),
                ..Default::default()
            },
            ..Default::default()
        },
        ..Default::default()
    })
}

fn created(handle: HANDLE) -> Result<u64> {
    let mut times = [FILETIME::default(); 4];
    unsafe {
        GetProcessTimes(
            handle,
            &mut times[0] as *mut _,
            &mut times[1] as *mut _,
            &mut times[2] as *mut _,
            &mut times[3] as *mut _,
        )?;
    }
    Ok(((times[0].dwHighDateTime as u64) << 32) | times[0].dwLowDateTime as u64)
}

fn watch_parent(request: &Request) -> Result<()> {
    let parent = OwnedHandle(unsafe {
        OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
            false,
            request.parent_pid,
        )?
    });
    ensure!(
        created(parent.raw())? == request.parent_created
            && unsafe { WaitForSingleObject(parent.raw(), 0) } == WAIT_TIMEOUT,
        "original engine parent is not live"
    );
    let raw = parent.into_raw().0 as usize;
    std::thread::Builder::new()
        .name("mxc-engine-lifetime".into())
        .spawn(move || {
            let _handle = OwnedHandle(HANDLE(raw as *mut _));
            unsafe {
                WaitForSingleObject(_handle.raw(), INFINITE);
            }
            // Also fail closed on a wait error. Exiting closes our original outer job.
            std::process::exit(125);
        })?;
    Ok(())
}

/// The helper joins before MXC can create a child. No breakaway is permitted.
/// The handle must survive until OS process exit: closing it kills this helper too.
struct LifetimeJob(HANDLE);
impl LifetimeJob {
    fn enter() -> Result<Self> {
        let job = OwnedHandle(unsafe { CreateJobObjectW(None, PCWSTR::null())? });
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        unsafe {
            SetInformationJobObject(
                job.raw(),
                JobObjectExtendedLimitInformation,
                (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                size_of_val(&limits) as u32,
            )?;
            AssignProcessToJobObject(job.raw(), GetCurrentProcess())
                .context("cannot enter MXC lifetime job; no workload launched")?;
        }
        Ok(Self(job.into_raw()))
    }
    fn drain(&self) -> Result<()> {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            let mut info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
            let mut member = windows::core::BOOL::default();
            unsafe {
                IsProcessInJob(GetCurrentProcess(), Some(self.0), &mut member)?;
                QueryInformationJobObject(
                    Some(self.0),
                    JobObjectBasicAccountingInformation,
                    (&mut info as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
                    size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
                    None,
                )?;
            }
            ensure!(
                member.as_bool(),
                "MXC helper lost its original lifetime job"
            );
            if info.ActiveProcesses == 1 {
                return Ok(());
            }
            ensure!(
                Instant::now() < deadline,
                "MXC descendants did not drain; enforcement retained until helper exit"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
    }
}

fn run() -> Result<i32> {
    let mut bytes = Vec::new();
    std::io::stdin()
        .take(MAX_REQUEST + 1)
        .read_to_end(&mut bytes)?;
    ensure!(
        bytes.len() as u64 <= MAX_REQUEST,
        "MXC request exceeds 1 MiB"
    );
    // serde errors can contain arbitrary request data, including environment values.
    let request: Request =
        serde_json::from_slice(&bytes).map_err(|_| anyhow::anyhow!("invalid MXC request"))?;
    let mut native = translate(&request)?;
    for path in request
        .read
        .iter()
        .chain(&request.write)
        .chain([&request.cwd, &request.argv[0]])
    {
        validate_existing_path(path)?;
    }
    for path in &request.deny_read {
        validate_denied_path(path)?;
    }
    for path in &request.enumerate {
        validate_existing_path(path)?;
        ensure!(Path::new(path).is_dir(), "MXC discovery roots must be directories");
    }
    for path in &request.discovery_deny {
        validate_existing_path(path)?;
        ensure!(Path::new(path).is_dir(), "MXC discovery boundaries must be directories");
    }
    let plan = protection_plan(&request)?;
    ensure!(
        Path::new(&request.cwd).is_dir(),
        "MXC cwd must be a directory"
    );
    let mut runner = BaseContainerRunner::new();
    runner.validate(&native).map_err(|_| {
        anyhow::anyhow!("MXC native capability/policy validation failed; no fallback")
    })?;
    watch_parent(&request)?;
    let lifetime = LifetimeJob::enter()?;
    // Retain original reservation handles until the entire native job drained.
    // After any launch error, process exit releases them; no pathname cleanup.
    let mut guards = ManuallyDrop::new(retain_protections(&request, &plan)?);
    let mut logger = Logger::new(Mode::Buffer);
    if let Some(normalized) = mxc_sdk::mxc_common::filesystem_object::normalize_object_conflicts(
        &native.policy,
        &mut logger,
    )
    .map_err(|_| anyhow::anyhow!("MXC filesystem identity normalization failed"))?
    {
        native.policy = normalized;
    }
    runner.validate(&native).map_err(|_| {
        anyhow::anyhow!("MXC native capability/policy validation failed; no fallback")
    })?;
    for guard in guards.iter() {
        guard.verify()?;
    }
    // Logger's internal buffer can contain argv/environment: never print it.
    let child = runner
        .spawn(&native, &mut logger, StdioMode::Inherit)
        .map_err(|response| {
            anyhow::anyhow!("MXC native launch failed ({:?})", response.failure_phase)
        })?;
    // Any error from here retains the process/security-environment until helper
    // exit closes the original outer job. Never run warning-only Drop cleanup.
    let mut child = ManuallyDrop::new(child);
    let deadline = Instant::now() + Duration::from_millis(request.timeout_ms);
    let mut timed_out = false;
    loop {
        if child.try_wait()?.is_some() {
            break;
        }
        if Instant::now() >= deadline {
            timed_out = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    // Kill descendants even when the root already exited; then prove the whole
    // tree drained BEFORE calling upstream wait(), which releases enforcement.
    child
        .kill()
        .context("MXC process-tree termination failed")?;
    lifetime.drain()?;
    let code = child.wait().context("MXC native teardown failed")?;
    unsafe {
        ManuallyDrop::drop(&mut child);
    }
    lifetime.drain()?;
    for guard in guards.iter() {
        guard.verify()?;
    }
    unsafe {
        ManuallyDrop::drop(&mut guards);
    }
    Ok(if timed_out { 124 } else { code })
}

#[cfg(test)]
mod tests {
    use super::*;
    fn request() -> Request {
        serde_json::from_value(json!({"protocol":4,"cwd":"C:\\work",
        "argv":["C:\\Windows\\System32\\cmd.exe","/d","/c","echo ok"],
        "read":["C:\\work","C:\\Windows"],"write":["C:\\work"],
        "deny_read":["C:\\outside"],"deny_write":["C:\\work\\.git","C:\\private"],
        "env":[["SYSTEMROOT","C:\\Windows"],["LOCALAPPDATA","C:\\temp"]],
        "network":"off","parent_pid":100,"parent_created":42,"timeout_ms":1000}))
        .unwrap()
    }
    #[test]
    fn write_sharing_is_limited_to_actual_unprotected_writable_ancestors() {
        let mut r = request();
        for allowed in [r"C:\work", r"\\?\C:\work\src"] {
            assert!(writable_pin_ancestor(&r, allowed), "{allowed}");
        }
        for denied in [
            r"C:\",
            r"C:\workspace",
            r"C:\Windows",
            r"C:\work\.git",
            r"C:\work\.git\hooks",
        ] {
            assert!(!writable_pin_ancestor(&r, denied), "{denied}");
        }
        r.deny_read.push(r"C:\work\private".into());
        assert!(!writable_pin_ancestor(&r, r"C:\work\private\nested"));
        r.deny_write.push(r"C:\work".into());
        assert!(!writable_pin_ancestor(&r, r"C:\work\src"));
        r.deny_write.clear();
        r.write.clear();
        assert!(!writable_pin_ancestor(&r, r"C:\work"));
    }
    #[test]
    fn explicit_local_preview_keeps_remote_ingress_denied() {
        for network in [Network::Off, Network::On] {
            let mut r = request();
            r.network = network;
            r.local_preview = true;
            let policy = translate(&r).unwrap().policy;
            assert_eq!(policy.network_ingress.unwrap(), NetworkIngressPolicy {
                default: NetworkAction::Deny,
                host_loopback: NetworkAction::Allow,
            });
            assert!(!policy.fallback.allow_dacl_mutation);
            assert_eq!(policy.base_process_ui.isolation, "container");
        }
    }

    #[test]
    fn directional_network_never_enables_ingress_or_loopback() {
        for network in [Network::Off, Network::On] {
            let mut r = request();
            r.network = network;
            let p = translate(&r).unwrap().policy;
            assert_eq!(
                p.network_egress.unwrap().default,
                if network == Network::On {
                    NetworkAction::Allow
                } else {
                    NetworkAction::Deny
                }
            );
            assert_eq!(
                p.network_ingress.unwrap(),
                NetworkIngressPolicy {
                    default: NetworkAction::Deny,
                    host_loopback: NetworkAction::Deny
                }
            );
            assert!(!p.fallback.allow_dacl_mutation);
            assert!(!p.least_privilege_mode);
            assert!(p.capture_denials.is_none());
            assert!(p.allowed_proxy_peer.is_none());
            assert_eq!(p.base_process_ui.isolation, "container");
        }
    }
    #[test]
    fn readonly_carveout_does_not_grant_outside_reads() {
        let p = translate(&request()).unwrap().policy;
        assert_eq!(p.readwrite_paths, vec!["C:\\work"]);
        assert!(p.readonly_paths.contains(&"C:\\work\\.git".into()));
        assert!(!p.readonly_paths.contains(&"C:\\private".into()));
        assert!(!p.readonly_paths.contains(&"C:\\work".into()));
        assert!(p.enumerate_paths.is_empty());
    }
    #[test]
    fn directory_discovery_is_separate_from_content_and_write_authority() {
        let mut r = request();
        r.enumerate = vec![r"C:\".into(), r"D:\".into()];
        let p = translate(&r).unwrap().policy;
        assert_eq!(p.enumerate_paths, r.enumerate);
        assert_eq!(p.readwrite_paths, vec![r"C:\work"]);
        for root in &r.enumerate {
            assert!(!p.readonly_paths.contains(root));
        }
        assert_eq!(p.denied_paths, r.deny_read);
        assert!(!p.fallback.allow_dacl_mutation);
        r.enumerate = vec![r"\\server\share".into()];
        assert!(translate(&r).is_err());
    }
    #[test]
    fn discovery_boundaries_preserve_only_narrower_content_grants() {
        let mut r = request();
        r.enumerate = vec![r"C:\".into()];
        r.discovery_deny = vec![r"C:\private".into()];
        r.read.push(r"C:\private\tools".into());
        let p = translate(&r).unwrap().policy;
        assert!(p.readonly_paths.contains(&r"C:\private\tools".into()));
        assert!(p.denied_paths.contains(&r"C:\private".into()));
        r.deny_read.push(r"C:\private\tools".into());
        assert!(!translate(&r).unwrap().policy.readonly_paths.contains(&r"C:\private\tools".into()));
        r.discovery_deny = vec![r"C:\work\private".into()];
        assert!(translate(&r).is_err());
    }
    #[test]
    fn denies_override_equal_write_root() {
        let mut r = request();
        r.deny_write.push("C:\\work".into());
        let p = translate(&r).unwrap().policy;
        assert!(p.readwrite_paths.is_empty());
        assert!(p.readonly_paths.contains(&"C:\\work".into()));
    }
    #[test]
    fn request_cannot_select_fallback_or_learning_mode() {
        let mut v = serde_json::to_value(request()).unwrap();
        v["fallback"] = json!(true);
        assert!(serde_json::from_value::<Request>(v).is_err());
        let mut r = request();
        r.protocol = 3;
        assert!(translate(&r).is_err());
    }
    #[test]
    fn environment_is_exact_and_names_unique_case_insensitively() {
        let mut r = request();
        let p = translate(&r).unwrap();
        assert!(!p.inherit_default_env);
        assert_eq!(p.env.unwrap().len(), 2);
        r.env.push(("systemroot".into(), "D:\\unexpected".into()));
        assert!(translate(&r).is_err());
    }
    #[test]
    fn aliases_relative_and_device_paths_are_rejected() {
        for p in [
            r"\\server\share",
            r"\\?\C:\work",
            r"C:\work\..\private",
            r"C:work",
            r"C:\work:stream",
            r"C:\work.\file",
        ] {
            assert!(validate_path(p).is_err(), "{p}");
        }
        assert!(!contains(r"C:\work", r"C:\workspace"));
    }
    #[test]
    fn missing_reservation_plan_is_bounded_and_pure() {
        let root = std::env::temp_dir().join(format!(
            "alysis-mxc-plan-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        assert!(!root.exists());
        let mut r = request();
        r.write = vec![root.to_str().unwrap().to_owned()];
        r.deny_write = vec![format!("{}\\.git\\", root.display())];
        assert_eq!(protection_plan(&r).unwrap().len(), 1);
        // A later invalid member rejects the entire plan before reservation.
        r.deny_write.push(format!("{}\\AGENTS.md", root.display()));
        assert!(protection_plan(&r).is_err());
        r.deny_write.pop();
        r.deny_write
            .push(format!("{}-outside\\.git\\", root.display()));
        assert!(protection_plan(&r).is_err());
        r.write.clear();
        assert!(protection_plan(&r).unwrap().is_empty());
        r.deny_write.push(format!("{}\\NUL\\", root.display()));
        assert!(protection_plan(&r).is_err());
        assert!(!root.exists());
    }
}
