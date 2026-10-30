//! Alysis policy boundary. The vendored runner supplies token/desktop/job isolation.
//! Requests come from the trusted engine over stdin, never from workspace files.
use anyhow::{Context, Result, bail, ensure};
use serde::Deserialize;
use serde_json::{Value, json};
use std::cell::{Cell, RefCell};
use std::io::{Read, Write};
use std::net::TcpListener;
use std::os::windows::fs::MetadataExt;
use std::path::PathBuf;
use windows::Win32::Foundation::{FILETIME, HANDLE, WAIT_OBJECT_0};
use windows::Win32::System::Threading::{
    GetCurrentProcess, GetProcessTimes, INFINITE, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
    PROCESS_SYNCHRONIZE, WaitForSingleObject,
};
use windows::core::PCWSTR;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    protocol: u32,
    cwd: PathBuf,
    argv: Vec<String>,
    read: Vec<String>,
    #[serde(default)]
    traverse: Vec<String>,
    write: Vec<String>,
    deny_read: Vec<String>,
    deny_write: Vec<String>,
    env: Vec<(String, String)>,
    network: srt_win::appcontainer::NetworkAccess,
    parent_pid: u32,
    parent_created: u64,
}

pub(crate) use srt_win::execution_lease::ExecutionLease as Lease;

fn process_created(handle: HANDLE) -> Result<u64> {
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

/// Explicit recovery never terminates a process or discards a failed ACL update.
pub fn recover() -> Result<i32> {
    let _lease = Lease::acquire()?;
    let real_user = srt_win::sid::current_user_sid()?;
    let setup = srt_win::install::read_setup()?.context("sandbox is not installed")?;
    ensure!(
        setup.owner_user_sid == real_user,
        "sandbox belongs to another Windows user"
    );
    let journal = srt_win::state_db::session_store_dir()?.join("execution-pending.json");
    if journal.exists() {
        let marker: Value = serde_json::from_slice(&std::fs::read(&journal)?)?;
        ensure!(
            marker["owner"].as_str() == Some(real_user.as_str()),
            "execution journal owner mismatch"
        );
        ensure!(
            marker["version"].as_u64() == Some(2),
            "execution journal predates verified job recovery; manual repair is required"
        );
        let pid: u32 = marker["pid"]
            .as_u64()
            .context("invalid execution journal")?
            .try_into()?;
        let created = marker["created"]
            .as_u64()
            .context("invalid execution journal creation time")?;
        match unsafe {
            OpenProcess(
                PROCESS_SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION,
                false,
                pid,
            )
        } {
            Ok(handle) => {
                let owner = srt_win::util::OwnedHandle(handle);
                ensure!(
                    process_created(owner.raw())? != created
                        || unsafe { WaitForSingleObject(owner.raw(), 0) } == WAIT_OBJECT_0,
                    "execution owner is still alive; stop it before recovery"
                );
            }
            Err(error) => ensure!(
                error.code() == windows::Win32::Foundation::ERROR_INVALID_PARAMETER.to_hresult(),
                "cannot establish that the old execution has stopped: {error}"
            ),
        }
        if !marker["container"].is_null() {
            let container: crate::container_policy::ContainerPolicy =
                serde_json::from_value(marker["container"].clone())?;
            container.validate()?;
            srt_win::job::wait_for_execution_exit(&container.name)?;
            cleanup_container(&container)?;
            save_journal(&journal, &real_user, pid, created, None)?;
        }
    }
    srt_win::state_db::with_init_lock(
        srt_win::state_db::HolderPid(std::process::id()),
        false,
        |_db| Ok(()),
    )?;
    if journal.exists() {
        std::fs::remove_file(journal)?;
    }
    println!("{}", json!({"recovered":true}));
    Ok(0)
}

struct Grants {
    sid: String,
    pid: u32,
    created: u64,
    journal: PathBuf,
    finished: Cell<bool>,
    owner: String,
    container: RefCell<Option<crate::container_policy::ContainerPolicy>>,
}

fn save_journal(
    path: &std::path::Path,
    owner: &str,
    pid: u32,
    created: u64,
    container: Option<&crate::container_policy::ContainerPolicy>,
) -> Result<()> {
    use windows::Win32::Storage::FileSystem::{
        MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH, MoveFileExW,
    };
    let staging = path.with_extension(format!("{}.tmp", std::process::id()));
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .open(&staging)?;
    file.write_all(&serde_json::to_vec(
        &json!({"version":2,"pid":pid,"created":created,"owner":owner,"container":container}),
    )?)?;
    file.sync_all()?;
    drop(file);
    let from = srt_win::util::wstr(staging.to_str().context("invalid journal staging path")?);
    let to = srt_win::util::wstr(path.to_str().context("invalid journal path")?);
    unsafe {
        MoveFileExW(
            PCWSTR(from.as_ptr()),
            PCWSTR(to.as_ptr()),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )?;
    }
    Ok(())
}

fn cleanup_container(container: &crate::container_policy::ContainerPolicy) -> Result<()> {
    container.cleanup()?;
    srt_win::winsta::revoke_container_from_winsta(&container.sid)?;
    srt_win::appcontainer::delete(&container.name)?;
    Ok(())
}

impl Grants {
    fn set_container(&self, container: crate::container_policy::ContainerPolicy) -> Result<()> {
        save_journal(
            &self.journal,
            &self.owner,
            self.pid,
            self.created,
            Some(&container),
        )?;
        *self.container.borrow_mut() = Some(container);
        Ok(())
    }
    fn command(&self, verb: &str, input: Option<Value>) -> Result<()> {
        match verb {
            "stamp" | "grant" => srt_win::cli::apply_policy_acls(
                self.pid,
                &self.sid,
                verb == "grant",
                input.context("missing ACL policy")?,
            ),
            _ => bail!("invalid ACL operation"),
        }
    }
    fn cleanup(&self) -> Result<()> {
        if self.finished.get() {
            return Ok(());
        }
        if let Some(container) = self.container.borrow().as_ref() {
            cleanup_container(container)?;
            save_journal(&self.journal, &self.owner, self.pid, self.created, None)?;
        }
        *self.container.borrow_mut() = None;
        // Remove allows before denies; failed operations retain their journal.
        srt_win::cli::release_policy_acls(self.pid, &self.sid, true)?;
        srt_win::cli::release_policy_acls(self.pid, &self.sid, false)?;
        if self.journal.exists() {
            std::fs::remove_file(&self.journal)?;
        }
        self.finished.set(true);
        Ok(())
    }
}
impl Drop for Grants {
    fn drop(&mut self) {
        if let Err(e) = self.cleanup() {
            eprintln!("alysis-sandbox: ACL recovery required: {e:#}");
        }
    }
}

/// Inherited denies do not override an explicit child allow, a protected DACL,
/// or every owner right. Check effective access on each protected object before
/// accepting user code. Never traverse a reparse point while enumerating.
fn protected_objects(roots: &[String]) -> Result<Vec<String>> {
    let mut pending: Vec<PathBuf> = roots.iter().map(PathBuf::from).collect();
    let mut paths = std::collections::BTreeSet::new();
    while let Some(path) = pending.pop() {
        let meta = std::fs::symlink_metadata(&path)
            .with_context(|| format!("cannot inspect protected path {}", path.display()))?;
        ensure!(
            meta.file_attributes() & 0x400 == 0,
            "protected path is a reparse point: {}",
            path.display()
        );
        let name = path
            .to_str()
            .context("protected path is not Unicode")?
            .to_owned();
        if !paths.insert(name) {
            continue;
        }
        if meta.is_dir() {
            for child in std::fs::read_dir(&path)? {
                pending.push(child?.path());
            }
        }
    }
    Ok(paths.into_iter().collect())
}

/// A protected nested name is replaceable if its writable ancestor can move.
/// Reserve those ancestor identities without denying ordinary sibling writes.
/// Deny targets already exist here: the first stamp materializes placeholders.
fn protected_ancestors(write: &[String], denied: &[String]) -> Result<Vec<String>> {
    let roots = write
        .iter()
        .map(|path| {
            let (canonical, is_dir) = srt_win::path_id::canonicalize_path(path)?;
            ensure!(is_dir, "workspace write root must be a directory");
            Ok(PathBuf::from(canonical))
        })
        .collect::<Result<Vec<_>>>()?;
    let mut ancestors = std::collections::BTreeSet::new();
    for path in denied {
        let (canonical, _) = srt_win::path_id::canonicalize_path(path)?;
        let path = PathBuf::from(canonical);
        for root in &roots {
            if !path.starts_with(root) {
                continue;
            }
            for ancestor in path.ancestors().skip(1) {
                if ancestor == root || !ancestor.starts_with(root) {
                    break;
                }
                ancestors.insert(
                    ancestor
                        .to_str()
                        .context("protected ancestor is not Unicode")?
                        .to_owned(),
                );
            }
        }
    }
    Ok(ancestors.into_iter().collect())
}

pub fn run() -> Result<i32> {
    let mut bytes = Vec::new();
    std::io::stdin()
        .take(1024 * 1024 + 1)
        .read_to_end(&mut bytes)?;
    ensure!(bytes.len() <= 1024 * 1024, "request exceeds 1 MiB");
    let request: Request = serde_json::from_slice(&bytes).context("invalid execution request")?;
    ensure!(
        request.protocol == 3 && !request.argv.is_empty(),
        "unsupported protocol or empty argv"
    );
    ensure!(
        request.cwd.is_absolute() && request.cwd.is_dir(),
        "cwd must be an existing absolute directory"
    );
    // Known incompatible host jobs fail before the lease, ACL journal, profile
    // creation or enforcement probes. Launch checks this again before spawn.
    srt_win::job::require_compatible_host_job()?;
    let _lease = Lease::acquire()?;
    // Pin the actual parent instance. Killing/crashing the engine also kills the
    // broker, which closes the job; a recycled PID cannot prolong its lifetime.
    let parent = srt_win::util::OwnedHandle(unsafe {
        OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
            false,
            request.parent_pid,
        )?
    });
    let created = process_created(parent.raw())?;
    ensure!(
        created == request.parent_created
            && unsafe { WaitForSingleObject(parent.raw(), 0) } != WAIT_OBJECT_0,
        "engine parent is no longer alive"
    );
    let raw = parent.into_raw().0 as usize;
    std::thread::spawn(move || {
        unsafe {
            WaitForSingleObject(HANDLE(raw as *mut _), INFINITE);
        }
        std::process::exit(125);
    });
    let _exe_lock = srt_win::self_protect::share_lock_current_exe()?;
    let real_user = srt_win::sid::current_user_sid()?;
    srt_win::self_protect::install_broker_dacl(Some(&real_user))?;
    let setup = srt_win::install::read_setup()?
        .context("sandbox is not installed; run alysis sandbox setup --native-windows")?;
    ensure!(
        setup.marker_version == srt_win::install::SETUP_VERSION,
        "sandbox setup needs repair"
    );
    ensure!(
        setup.owner_user_sid == real_user,
        "sandbox preview belongs to a different Windows user; per-user identity slots are not available yet"
    );
    let cred = srt_win::install::read_cred()?;
    let sid = setup.sandbox_user_sid;
    let ambient = srt_win::install::ambient_recorded_paths()?;
    ensure!(
        !ambient.is_empty(),
        "sandbox ambient protections are missing; rerun setup"
    );
    // A crash leaves a durable marker. Never reuse grants after an unverified
    // cleanup; recovery must be performed explicitly after checking process exit.
    drop(srt_win::state_db::open_db()?);
    let journal = srt_win::state_db::session_store_dir()?.join("execution-pending.json");
    let broker_created = process_created(unsafe { GetCurrentProcess() })?;
    let mut marker = std::fs::OpenOptions::new().write(true).create_new(true).open(&journal)
        .context("unfinished native sandbox execution; recover ACLs after stopping sandbox processes before retrying")?;
    marker.write_all(&serde_json::to_vec(
        &json!({"version":2,"pid":std::process::id(),"created":broker_created,"owner":real_user}),
    )?)?;
    marker.sync_all()?;
    drop(marker);
    let grants = Grants {
        sid: sid.clone(),
        pid: std::process::id(),
        created: broker_created,
        finished: Cell::new(false),
        journal,
        owner: real_user.clone(),
        container: RefCell::new(None),
    };
    for path in &request.write {
        srt_win::acl::ensure_workspace_owner_access(path, &real_user)?;
    }
    for path in &request.read {
        srt_win::acl::ensure_owned_read_access(path, &real_user)?;
    }
    grants.command(
        "stamp",
        Some(json!({"denyRead":request.deny_read,"denyWrite":request.deny_write})),
    )?;
    let protected_ancestors = protected_ancestors(&request.write, &request.deny_write)?;
    grants.command("stamp", Some(json!({"denyDelete":protected_ancestors})))?;
    grants.command(
        "grant",
        Some(json!({"read":request.read,"write":request.write,"traverse":request.traverse})),
    )?;
    let cwd = request.cwd.to_str().context("cwd is not Unicode")?;
    let container = crate::container_policy::ContainerPolicy::build(
        &request.read,
        &request.write,
        &request.traverse,
        &request.deny_read,
        &request.deny_write,
    )?;
    let container_name = container.name.clone();
    grants.set_container(container.clone())?;
    container.apply()?;
    let mut protected = ambient;
    protected.extend(protected_objects(&request.deny_write)?);
    // Some Windows system directories deny READ_CONTROL to the real user.
    // Verify effective permissions under the sandbox identity instead of
    // mistaking an unreadable security descriptor for a missing protection.
    let code = srt_win::logon::spawn_runner(
        &cred.user,
        &cred.pw,
        &sid,
        Some(cwd),
        &srt_win::runner::RunnerCmd::ProbeFilesystem {
            paths: protected.clone(),
            delete_paths: protected_ancestors.clone(),
        },
        true,
    )?;
    ensure!(
        code == 0,
        "filesystem enforcement probe failed (exit {code}); no command was launched"
    );
    // Test actual enforcement; an unreachable destination is never evidence of isolation.
    // Include IPv6 and a formerly permitted proxy port to detect stale upstream rules.
    for address in ["127.0.0.1:0", "[::1]:0", "127.0.0.1:60080"] {
        let listener =
            TcpListener::bind(address).context("cannot bind network enforcement probe")?;
        let code = srt_win::logon::spawn_runner(
            &cred.user,
            &cred.pw,
            &sid,
            Some(cwd),
            &srt_win::runner::RunnerCmd::ProbeEgress {
                target: listener.local_addr()?.to_string(),
            },
            true,
        )?;
        ensure!(
            code == 0,
            "network enforcement probe failed (exit {code}); no command was launched"
        );
    }
    let result = srt_win::launch::run_appcontainer(
        std::path::Path::new(&request.argv[0]),
        &request.argv[1..],
        &request.env,
        &container_name,
        &request.cwd,
        &sid,
        &protected,
        &protected_ancestors,
        request.network,
    );
    // The native job has closed and all descendants have been killed before grants are revoked.
    grants.cleanup()?;
    // Cleanup is idempotent; Drop covers every earlier error as well.
    match result {
        Ok(code) => Ok(code as i32),
        Err(error) => bail!(error),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn nested_protection_reserves_only_workspace_intermediates() -> Result<()> {
        let unique = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_nanos();
        let base = std::env::temp_dir().join(format!("alysis-protected-ancestors-{unique}"));
        struct Cleanup(PathBuf);
        impl Drop for Cleanup {
            fn drop(&mut self) {
                let _ = std::fs::remove_dir_all(&self.0);
            }
        }
        let _cleanup = Cleanup(base.clone());
        let root = base.join("workspace");
        let skills = root.join(".github/skills");
        std::fs::create_dir_all(&skills)?;
        let outside = base.join("outside");
        std::fs::create_dir(&outside)?;
        let instructions = root.join("AGENTS.md");
        std::fs::write(&instructions, b"trusted")?;
        let write = vec![root.to_string_lossy().into_owned()];
        let denied = vec![
            skills.to_string_lossy().into_owned(),
            instructions.to_string_lossy().into_owned(),
            outside.to_string_lossy().into_owned(),
        ];
        let (github, _) =
            srt_win::path_id::canonicalize_path(root.join(".github").to_str().unwrap())?;
        assert_eq!(protected_ancestors(&write, &denied)?, vec![github]);
        assert!(protected_ancestors(&[], &denied)?.is_empty());
        Ok(())
    }

    #[test]
    fn rejects_unknown_policy_fields() {
        let mut input = json!({"protocol":3,"cwd":"C:\\work","argv":["cmd"],"read":[],"write":[],"deny_read":[],"deny_write":[],"env":[],"network":"off","parent_pid":1,"parent_created":2});
        assert!(serde_json::from_value::<Request>(input.clone()).is_ok());
        input["allow_host"] = json!(true);
        assert!(serde_json::from_value::<Request>(input).is_err());
    }
}
