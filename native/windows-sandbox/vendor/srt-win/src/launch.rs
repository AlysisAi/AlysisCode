//! `srt-win runner`'s lockdown stack: build the restricted token,
//! self-protect the runner, spawn the target suspended under a
//! locked-down job + non-interactive desktop + mitigation-policy
//! stack + explicit handle whitelist, resume, wait, propagate exit
//! code.
//!
//! Stateless — no marker file, no proxy thread, no FS-deny
//! handling here. Network egress for the child reaches the host's
//! JS-side proxies (whose ports the caller passes via the env
//! overlay) via the WFP loopback permit installed by `srt-win
//! install`.

use anyhow::{Context, Result, anyhow};
use std::ffi::c_void;
use std::mem::{size_of, zeroed};
use std::path::Path;
use windows::Win32::Foundation::{
    CloseHandle, HANDLE, HANDLE_FLAG_INHERIT, SetHandleInformation, WAIT_FAILED, WAIT_OBJECT_0,
};
use windows::Win32::System::Console::{
    GetStdHandle, STD_ERROR_HANDLE, STD_INPUT_HANDLE, STD_OUTPUT_HANDLE,
};
use windows::Win32::System::Threading::{
    CREATE_BREAKAWAY_FROM_JOB, CREATE_NO_WINDOW, CREATE_SUSPENDED, CREATE_UNICODE_ENVIRONMENT,
    CreateProcessAsUserW, EXTENDED_STARTUPINFO_PRESENT, GetExitCodeProcess, INFINITE,
    PROCESS_CREATION_FLAGS, PROCESS_INFORMATION, ResumeThread, STARTF_USESTDHANDLES,
    STARTUPINFOEXW, STARTUPINFOW, TerminateProcess, WaitForSingleObject,
};
use windows::core::{PCWSTR, PWSTR};

use crate::job::Job;
use crate::self_protect;
use crate::token::{self, open_self_token, to_primary};
use crate::util::{pcwstr, wstr};
use crate::winsta::{current_desktop_name, current_winsta_name, on_default_desktop};

// ─── RAII handle wrappers ───────────────────────────────────────────

use crate::util::OwnedHandle;

/// Owns a freshly-spawned (suspended) child. If dropped before
/// [`defuse`] is called, terminates and observes the child's exit — so an error
/// between `CreateProcess*` and `WaitForSingleObject` can't orphan
/// a suspended process that's not yet in the job. Always closes
/// both handles after confirmed exit. An uncertain exit stops this helper with
/// code 125 before outer cleanup can revoke access or remove profile state.
/// Shared with [`crate::logon::spawn_runner`]
/// for the same suspended-then-assign-then-resume window.
pub(crate) struct SpawnedChild {
    pi: PROCESS_INFORMATION,
    armed: bool,
}
impl SpawnedChild {
    pub(crate) fn new(pi: PROCESS_INFORMATION) -> Self {
        Self { pi, armed: true }
    }
    pub(crate) fn process(&self) -> HANDLE {
        self.pi.hProcess
    }
    pub(crate) fn thread(&self) -> HANDLE {
        self.pi.hThread
    }
    /// Disarm the terminate-on-drop. Call after the child has been
    /// assigned to the job AND resumed — past that point
    /// `KILL_ON_JOB_CLOSE` covers cleanup.
    pub(crate) fn defuse(&mut self) {
        self.armed = false;
    }
}
impl Drop for SpawnedChild {
    fn drop(&mut self) {
        if self.armed {
            if let Err(error) = terminate_and_wait_for_child(self.pi.hProcess) {
                // Like Job::drop, refuse to unwind into outer resource cleanup.
                // TerminateProcess is asynchronous, even for a suspended child.
                // This cannot close the external broker-death/assignment gap or
                // establish descendant exit; the containing job is still required.
                crate::util::exit_for_unverified_cleanup("child", &error);
            }
        }
        unsafe {
            let _ = CloseHandle(self.pi.hThread);
            let _ = CloseHandle(self.pi.hProcess);
        }
    }
}

fn wait_for_process_exit(process: HANDLE, timeout_ms: u32) -> Result<()> {
    let status = unsafe { WaitForSingleObject(process, timeout_ms) };
    if status == WAIT_FAILED {
        return Err(std::io::Error::last_os_error()).context("wait for original child exit");
    }
    anyhow::ensure!(
        status == WAIT_OBJECT_0,
        "original child exit is unverified (wait status 0x{:x})",
        status.0
    );
    Ok(())
}

fn terminate_and_wait_for_child(process: HANDLE) -> Result<()> {
    terminate_and_wait_for_child_with_timeout(process, 30_000)
}

fn terminate_and_wait_for_child_with_timeout(process: HANDLE, timeout_ms: u32) -> Result<()> {
    let request = unsafe { TerminateProcess(process, 1) };
    // A failed request can race an exit already in progress. Windows can deny
    // termination before the process object is signaled (including after a
    // debugger continues EXIT_PROCESS_DEBUG_EVENT). Give that same original
    // object the full bounded wait in either case. Only its signaled state
    // proves exit; errors, PIDs and exit-code values never do.
    let observed = wait_for_process_exit(process, timeout_ms);
    match (observed, request) {
        (Ok(()), _) => Ok(()),
        (Err(wait), Ok(())) => Err(wait),
        (Err(wait), Err(request)) => {
            Err(wait).context(format!("child termination request also failed: {request}"))
        }
    }
}

#[cfg(test)]
#[path = "spawned_child_tests.rs"]
mod spawned_child_tests;

// ─── Process-creation mitigation-policy bits ────────────────────────
//
// The `windows` crate exposes `PROC_THREAD_ATTRIBUTE_MITIGATION_POLICY`
// but not the per-bit DWORD64 macros (they're winnt.h preprocessor
// `#define`s, still absent as of 0.62). Each policy occupies a 4-bit
// slot in the u64; `..._ALWAYS_ON` flips bit 0 of its slot.
//
// Only mitigations that don't break Node/Python JIT or mingw-built
// shells are enabled here. Specifically NOT enabled:
//   - `IMAGE_LOAD_PREFER_SYSTEM32` — flips DLL search-order so System32
//     wins over the EXE's directory; breaks the cygwin1.dll /
//     msys-2.0.dll resolution model.
//   - `CONTROL_FLOW_GUARD_ALWAYS_ON` — forces CFG even when the EXE
//     wasn't built with `/guard:cf`; stock mingw-built `bash.exe`
//     dies in `dofork`. CFG is defense-in-depth, not a primary
//     boundary.

/// Bit 32 — block legacy AppInit / IME / Winsock-LSP DLL injection
/// and `SetWindowsHookEx`.
const MITIGATION_EXTENSION_POINT_DISABLE: u64 = 1u64 << 32;
/// Bit 48 — block GDI from loading non-system fonts (historic
/// kernel font-parser RCE surface; sandbox children are
/// console/network workloads).
const MITIGATION_FONT_DISABLE: u64 = 1u64 << 48;
/// Bit 52 — refuse `LoadLibrary` from UNC / network paths.
const MITIGATION_IMAGE_LOAD_NO_REMOTE: u64 = 1u64 << 52;
/// Bit 56 — refuse `LoadLibrary` of any image whose mandatory label
/// is Low IL.
const MITIGATION_IMAGE_LOAD_NO_LOW_LABEL: u64 = 1u64 << 56;

/// Run `target_exe target_args…` under the lockdown stack and
/// return its exit code.
///
/// Called only from `srt-win runner` — this process is the
/// `alysis-sandbox` user (via `CreateProcessWithLogonW`), has NO
/// console (broker spawned it `CREATE_NO_WINDOW`; stdio are
/// anonymous pipes), and the child runs under a restricted token
/// with `BUILTIN\Administrators` flipped deny-only (LUA shape).
///
/// `env_overlay` is `(KEY, VALUE)` pairs overlaid on the runner's
/// own environment when building the child's env block. Overlay
/// wins on case-insensitive key conflict; everything else is
/// passed through verbatim — so the broker's `PATH` + proxy vars
/// ride here while the sandbox-user-profile `USERPROFILE`/`TEMP`
/// stay isolated.
pub fn run_lockdown(
    target_exe: &Path,
    target_args: &[String],
    env_overlay: &[(String, String)],
    owner_sid: Option<&str>,
) -> Result<u32> {
    run_isolated(
        target_exe,
        target_args,
        env_overlay,
        owner_sid,
        None,
        None,
        &[],
        &[],
        crate::appcontainer::NetworkAccess::Off,
    )
}

/// Host broker entry point: a fresh AppContainer with the requested network policy on its own
/// desktop. The kernel creates the lowbox token; no host environment is inherited.
pub fn run_appcontainer(
    target_exe: &Path,
    target_args: &[String],
    env: &[(String, String)],
    name: &str,
    cwd: &Path,
    write_sid: &str,
    protected: &[String],
    protected_ancestors: &[String],
    network: crate::appcontainer::NetworkAccess,
) -> Result<u32> {
    run_isolated(
        target_exe,
        target_args,
        env,
        Some(write_sid),
        Some(name),
        Some(cwd),
        protected,
        protected_ancestors,
        network,
    )
}

fn run_isolated(
    target_exe: &Path,
    target_args: &[String],
    env_overlay: &[(String, String)],
    owner_sid: Option<&str>,
    appcontainer_name: Option<&str>,
    cwd: Option<&Path>,
    protected: &[String],
    protected_ancestors: &[String],
    network: crate::appcontainer::NetworkAccess,
) -> Result<u32> {
    crate::job::require_compatible_host_job()?;
    // SID allocations and the array must outlive profile/process creation.
    let capability_sids = network
        .capability_sids()
        .iter()
        .map(|sid| crate::sid::LocalPsid::from_string(sid))
        .collect::<Result<Vec<_>>>()?;
    let mut capabilities: Vec<_> = capability_sids
        .iter()
        .map(|sid| windows::Win32::Security::SID_AND_ATTRIBUTES {
            Sid: sid.as_psid(),
            Attributes: windows::Win32::System::SystemServices::SE_GROUP_ENABLED as u32,
        })
        .collect();
    if let Some(name) = appcontainer_name {
        crate::appcontainer::create(name, &capabilities)?;
    }
    let container_sid = appcontainer_name
        .map(crate::appcontainer::derive)
        .transpose()?;
    let sid_string = container_sid.as_ref().map(|sid| sid.string()).transpose()?;
    let mut desktop = sid_string
        .as_ref()
        .map(|sid| {
            crate::winsta::IsolatedDesk::new_with_container(
                owner_sid.context("missing write identity")?,
                Some(sid),
            )
        })
        .transpose()?;
    let _station = sid_string
        .as_ref()
        .map(|sid| crate::winsta::ContainerStationGrant::new(sid))
        .transpose()?;
    // The host broker retains its owner's access so termination and recovery
    // can open it. No package SID is granted. Legacy separate-account runners
    // omit their user SID because their descendants share that ordinary SID.
    let broker_owner = if appcontainer_name.is_some() {
        Some(crate::sid::current_user_sid()?)
    } else {
        None
    };
    self_protect::install_broker_dacl(broker_owner.as_deref()).context("protect runner process")?;

    // 2) Restricted token. Each handle is RAII-owned so any `?`
    //    below closes whatever was already opened.
    let self_tok = OwnedHandle(open_self_token()?);
    let restricted = OwnedHandle(if appcontainer_name.is_some() {
        token::make_write_restricted_token(
            self_tok.raw(),
            owner_sid.context("missing write identity")?,
        )?
    } else {
        token::make_sandbox_token_for_owner(self_tok.raw(), owner_sid)
            .context("make_sandbox_token")?
    });
    let primary = OwnedHandle(to_primary(restricted.raw()).context("to_primary")?);

    // 3) Job. `breakaway_ok = false` — this is the load-bearing
    //    containment Job; the child must NOT be able to break away.
    let job = match appcontainer_name {
        Some(name) => Job::for_execution(name),
        None => Job::new(false),
    }
    .context("create containment job")?;

    // `on_default` gates the fail-closed assertion; the breakaway
    // flag is gated separately on `IsProcessInJob(self)`. Computed
    // (and logged) BEFORE step 4 so the diagnostic identifies which
    // desktop the runner landed on.
    let on_default = on_default_desktop();
    let caller_in_job = crate::job::require_compatible_host_job()?;
    // Breakaway: the caller is in seclogon's job (and the
    // broker→runner job, both `BREAKAWAY_OK`); without breakaway
    // the child inherits them and `AssignProcessToJobObject` below
    // fails.
    let breakaway = if caller_in_job {
        CREATE_BREAKAWAY_FROM_JOB
    } else {
        PROCESS_CREATION_FLAGS(0)
    };
    let dbg = std::env::var_os("SANDBOX_RUNTIME_WIN_DEBUG").is_some();
    if dbg {
        eprintln!(
            "srt-win: run_lockdown: caller_in_job={} caller_desk={}\\{} \
             breakaway={}",
            caller_in_job,
            current_winsta_name().ok().as_deref().unwrap_or("?"),
            current_desktop_name().as_deref().unwrap_or("?"),
            breakaway.0 != 0,
        );
    }

    // 4) Desktop isolation — fail closed. The broker created a
    //    `WinSta0\srt-sb-…` desktop and passed it via `lpDesktop` to
    //    `CreateProcessWithLogonW`, so this runner is already on it
    //    (not `Default`). The child inherits via `lpDesktop = NULL`
    //    in step 8. If we're still on `Default`, the broker-side
    //    creation/attach failed and the child would share the
    //    interactive desktop — `WH_KEYBOARD_LL` keylogging is the
    //    threat (the Job's `UILIMIT_HANDLES` does NOT gate low-level
    //    hooks), so refuse rather than fall through.
    if on_default && desktop.is_none() {
        return Err(anyhow!(
            "desktop isolation required: runner is on Default — \
             broker IsolatedDesk creation or WinSta0 grant failed"
        ));
    }

    // AppContainer commands receive only the trusted allowlist and fresh profile.
    // The legacy separate-account runner retains its existing environment overlay.
    let mut overlay = env_overlay.to_vec();
    if let Some(name) = appcontainer_name {
        let profile_env = crate::appcontainer::environment(name)?;
        let profile = profile_env
            .iter()
            .find(|(key, _)| key == "USERPROFILE")
            .context("missing isolated profile")?;
        crate::acl::apply_sandbox_aces(
            &profile.1,
            owner_sid.context("missing write identity")?,
            crate::acl::SbAceSet {
                grant: Some(crate::acl::GrantMask::Modify),
                ..Default::default()
            },
        )?;
        overlay.extend(profile_env);
    }
    let mut env = build_env_block_with_inheritance(&overlay, appcontainer_name.is_none());

    // 6) Command line + application name.
    let cmdline = build_cmdline(target_exe, target_args);
    let mut cmdline_w = wstr(&cmdline);
    let app_w = wstr(&target_exe.display().to_string());

    // 7) PROC_THREAD_ATTRIBUTE_LIST: mitigation policy + explicit
    //    handle whitelist.
    let mitigation: u64 = MITIGATION_EXTENSION_POINT_DISABLE
        | MITIGATION_FONT_DISABLE
        | MITIGATION_IMAGE_LOAD_NO_REMOTE
        | MITIGATION_IMAGE_LOAD_NO_LOW_LABEL;
    let std_handles = collect_inheritable_std_handles();
    let mut handle_list: Vec<HANDLE> = std_handles
        .iter()
        .copied()
        .filter(|h| !h.0.is_null())
        .collect();
    if handle_list.is_empty() {
        return Err(anyhow!(
            "no std handle is inheritable; refusing to spawn. \
             srt-win runner requires at least one piped stdio stream."
        ));
    }
    let caps = container_sid
        .as_ref()
        .map(|sid| windows::Win32::Security::SECURITY_CAPABILITIES {
            AppContainerSid: sid.raw(),
            Capabilities: if capabilities.is_empty() {
                std::ptr::null_mut()
            } else {
                capabilities.as_mut_ptr()
            },
            CapabilityCount: capabilities.len() as u32,
            ..Default::default()
        });
    let mut attrs = ProcThreadAttrs::new(if caps.is_some() { 4 } else { 3 })?;
    attrs.set_mitigation_policy(&mitigation)?;
    attrs.set_handle_list(&mut handle_list)?;
    // Atomic job assignment closes the broker-crash window between creating
    // a suspended child and AssignProcessToJobObject. Windows 10+ fails the
    // creation itself if it cannot place the child in this job.
    attrs.set_job(&job)?;
    if let Some(caps) = &caps {
        // Original package/capability SID allocations precede this list and
        // remain alive on success and every error-unwind path.
        unsafe {
            attrs.set_security_capabilities(caps)?;
        }
    }

    // 8) STARTUPINFOEXW. `STARTF_USESTDHANDLES` + the caller's std
    //    handles is load-bearing: the runner has NO console (the
    //    broker spawned it with `CREATE_NO_WINDOW`; its stdio are the
    //    broker's anonymous pipes), so without an explicit `hStd*`
    //    wiring the child would try to allocate a conhost on the
    //    non-interactive desktop — which under the restricted token
    //    hangs.
    let mut six: STARTUPINFOEXW = unsafe { zeroed() };
    six.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
    six.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    six.StartupInfo.hStdInput = std_handles[0];
    six.StartupInfo.hStdOutput = std_handles[1];
    six.StartupInfo.hStdError = std_handles[2];
    six.lpAttributeList = attrs.list();
    if let Some(desktop) = &mut desktop {
        six.StartupInfo.lpDesktop = PWSTR(desktop.desktop_name_ptr());
    }
    // `lpDesktop = NULL` (zeroed) → child inherits this runner's
    // station+desktop, which is the broker-created `srt-sb-…` per
    // step 4's assertion.

    // 9) Spawn suspended. `breakaway` was derived above (gated on
    //    `IsProcessInJob(self)`).
    let mut pi: PROCESS_INFORMATION = unsafe { zeroed() };
    let cwd_w = cwd.map(|path| wstr(&path.to_string_lossy()));
    unsafe {
        CreateProcessAsUserW(
            Some(primary.raw()),
            pcwstr(&app_w),
            Some(PWSTR(cmdline_w.as_mut_ptr())),
            None,
            None,
            // Must be TRUE for `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`
            // to take effect (documented Vista-era quirk: with
            // FALSE the kernel ignores the attribute entirely).
            true,
            // `CREATE_NO_WINDOW`: the runner has no console for the
            // child to attach to.
            CREATE_SUSPENDED
                | CREATE_UNICODE_ENVIRONMENT
                | EXTENDED_STARTUPINFO_PRESENT
                | CREATE_NO_WINDOW
                | breakaway,
            Some(env.as_mut_ptr() as *const c_void),
            // Inherit cwd.
            cwd_w.as_deref().map(pcwstr).unwrap_or(PCWSTR::null()),
            // STARTUPINFOEXW is layout-compatible (StartupInfo is
            // first member); EXTENDED_STARTUPINFO_PRESENT tells the
            // kernel to read past it for lpAttributeList.
            &six.StartupInfo as *const STARTUPINFOW,
            &mut pi,
        )
        .with_context(|| format!("CreateProcessAsUserW({})", target_exe.display()))?;
    }

    // The child is already in the job, still suspended. Keep the explicit
    // guard too so all validation failures terminate it before returning.
    let mut child = SpawnedChild::new(pi);
    if let Some(sid) = &container_sid {
        token::verify_appcontainer_process(
            child.process(),
            sid.raw(),
            owner_sid.context("missing write identity")?,
            network,
        )?;
        token::verify_child_filesystem(child.process(), protected, protected_ancestors)?;
    }

    // 10) Verify atomic job assignment → resume. ResumeThread returns the
    //     previous suspend count, or u32::MAX on failure — a
    //     failure here would leave the child suspended in the
    //     job and `WaitForSingleObject(INFINITE)` below would
    //     hang the broker forever. Check before defusing the
    //     terminate-on-drop guard.
    let mut assigned = windows::core::BOOL(0);
    unsafe {
        windows::Win32::System::JobObjects::IsProcessInJob(
            child.process(),
            Some(job.raw()),
            &mut assigned,
        )
        .context("verify atomic containment job")?;
    }
    anyhow::ensure!(
        assigned.as_bool(),
        "child was not created in containment job"
    );
    let prev_suspend = unsafe { ResumeThread(child.thread()) };
    if prev_suspend == u32::MAX {
        return Err(anyhow!("ResumeThread: {}", std::io::Error::last_os_error()));
    }
    // From here the job owns lifetime; disarm terminate-on-drop.
    child.defuse();
    if dbg {
        // Post-spawn diagnostic — paired with the pre-spawn line above
        // so a hung CI run can tell whether `WaitForSingleObject` is
        // the wait point (this line present) or spawn/assign/resume
        // itself is the stall (this line absent).
        eprintln!(
            "srt-win: run_lockdown: child pid={} assigned+resumed \
             (prev_suspend={prev_suspend}); waiting",
            pi.dwProcessId,
        );
    }

    // 11) Wait + collect exit code.
    wait_for_process_exit(child.process(), INFINITE)?;
    let mut code: u32 = 0;
    unsafe {
        GetExitCodeProcess(child.process(), &mut code).context("GetExitCodeProcess")?;
    }
    // `child` (closes hProcess/hThread), `primary`/`restricted`/
    // `self_tok` (CloseHandle) all drop here.
    // Keep `attrs` (its backing buffer + the borrowed `mitigation`
    // and `handle_list`) and `job` alive until here. The kernel
    // snapshots the attribute list at CreateProcess time, but
    // DeleteProcThreadAttributeList (in attrs.drop) may re-read
    // pointers.
    drop(attrs);
    drop(handle_list);
    // Close the exited leader before waiting for zero job processes. A
    // surviving descendant is terminated and observed before ACL cleanup.
    drop(child);
    drop(job);
    Ok(code)
}

// ─── Environment block ──────────────────────────────────────────────

/// Build a `CREATE_UNICODE_ENVIRONMENT` block from the runner's own
/// environment, **verbatim**.
///
/// `srt-win exec` is a dumb passthrough for proxy configuration: it
/// does NOT synthesize `HTTP_PROXY` / `ALL_PROXY` / `NO_PROXY` and has
/// no `--http-proxy` / `--socks-proxy` flags. The single source of
/// proxy env is the TS `generateProxyEnvVars`, which the caller passes
/// via `--env`; this function just forwards the runner's environment
/// (= the sandbox user's `LOGON_WITH_PROFILE` defaults) with the
/// overlay applied on top to the child. No proxy value is invented,
/// no inherited var is stripped or blanked.
///
/// Entries are sorted case-insensitively by name for block ordering —
/// not the strict case-insensitive Unicode collation the
/// `CreateProcess` docs describe, but in practice the loader and
/// `GetEnvironmentVariableW` don't enforce ordering; `cmd /c set` and
/// every consumer we've tested work regardless. Names are NOT folded
/// or deduplicated, so if both `HTTP_PROXY` and `http_proxy` are
/// present both survive into the child.
///
/// Two adjustments on top of the verbatim copy: (a) restoring the
/// missing-case variants of `*_PROXY` variables (see
/// [`add_proxy_case_twins`]) — casing repair of caller-provided
/// values, not proxy synthesis; (b) applying `overlay` on top, where
/// each `(KEY, VALUE)` REPLACES any base entry whose name matches
/// case-insensitively (so the runner's broker-supplied `PATH`
/// supersedes the sandbox-user default `PATH` while everything else
/// passes through). Nothing else is added: consumers that need the
/// broker's identity (e.g. the self-protect probe) discover it by
/// walking the parent-process chain rather than via an environment
/// variable.
#[cfg(test)]
fn build_env_block(overlay: &[(String, String)]) -> Vec<u16> {
    build_env_block_with_inheritance(overlay, true)
}

pub(crate) fn build_env_block_with_inheritance(
    overlay: &[(String, String)],
    inherit: bool,
) -> Vec<u16> {
    use std::os::windows::ffi::OsStrExt;

    // Lossless base set — `env::vars()` PANICS on any entry whose
    // key or value is not valid UTF-8 (e.g. a PATH segment with an
    // unpaired surrogate from a profile path). Build from
    // `vars_os()` and encode each via `encode_wide` so nothing is
    // dropped and nothing panics.
    let overlay_upper: std::collections::HashSet<String> = overlay
        .iter()
        .map(|(k, _)| k.to_ascii_uppercase())
        .collect();
    let mut entries: Vec<(std::ffi::OsString, std::ffi::OsString)> = std::env::vars_os()
        .filter(|_| inherit)
        .filter(|(k, _)| {
            // Drop base entries the overlay replaces. The
            // overlay keys are ASCII (PATH, *_PROXY, …); a base
            // key that doesn't round-trip as UTF-8 cannot match
            // one and is kept.
            k.to_str()
                .map(|s| !overlay_upper.contains(&s.to_ascii_uppercase()))
                .unwrap_or(true)
        })
        .collect();
    for (k, v) in overlay {
        entries.push((k.into(), v.into()));
    }

    // Proxy case-twin repair operates on the UTF-8-decodable
    // subset: proxy variable NAMES are ASCII by convention so
    // filtering to entries whose key round-trips as UTF-8 misses
    // nothing relevant; values are passed through lossily (the
    // helper only inspects names, never values). Built from the
    // post-overlay `entries` so an overlay-supplied `HTTP_PROXY`
    // gets its lowercase twin too.
    let mut twin_view: Vec<(String, String)> = entries
        .iter()
        .filter_map(|(k, v)| Some((k.to_str()?.to_owned(), v.to_string_lossy().into_owned())))
        .collect();
    let before = twin_view.len();
    add_proxy_case_twins(&mut twin_view);
    for (k, v) in twin_view.into_iter().skip(before) {
        entries.push((k.into(), v.into()));
    }

    // Order the block case-insensitively by name; values pass
    // through verbatim. No dedup — case-variant duplicates are
    // preserved. The sort key uses `to_string_lossy` only for
    // ordering; the encoded bytes use `encode_wide` losslessly.
    entries.sort_by_cached_key(|(k, _)| k.to_string_lossy().to_ascii_uppercase());

    // Encode: `KEY=VALUE\0`… `\0`.
    let mut out: Vec<u16> = Vec::new();
    for (k, v) in entries {
        out.extend(k.encode_wide());
        out.push(b'=' as u16);
        out.extend(v.encode_wide());
        out.push(0);
    }
    out.push(0);
    out
}

/// Re-add the missing-case variants of `*_PROXY` variables (the host
/// spawn layer collapses case-duplicate keys) so that Cygwin/MSYS2
/// programs — which see a case-sensitive environment — still find
/// them. For every entry whose name ends with `_PROXY`
/// (case-insensitively), the all-uppercase and all-lowercase forms of
/// that name are appended where missing, with the same value. Existing
/// keys are never overwritten and nothing is added for names that are
/// not `*_PROXY`.
fn add_proxy_case_twins(entries: &mut Vec<(String, String)>) {
    let mut names: std::collections::HashSet<String> =
        entries.iter().map(|(k, _)| k.clone()).collect();
    let mut twins: Vec<(String, String)> = Vec::new();
    for (k, v) in entries.iter() {
        if !k.to_ascii_uppercase().ends_with("_PROXY") {
            continue;
        }
        for form in [k.to_ascii_uppercase(), k.to_ascii_lowercase()] {
            if !names.contains(&form) {
                names.insert(form.clone());
                twins.push((form, v.clone()));
            }
        }
    }
    entries.extend(twins);
}

// ─── Command-line quoting ───────────────────────────────────────────

/// MSVCRT / `CommandLineToArgvW` quoting for one argument.
/// Public so `main.rs`'s self-elevate path can rebuild
/// `lpParameters` from `std::env::args()`.
pub fn quote_arg(a: &str) -> String {
    if !a.is_empty() && !a.chars().any(|c| matches!(c, ' ' | '\t' | '"' | '\\')) {
        return a.to_string();
    }
    let mut out = String::with_capacity(a.len() + 2);
    out.push('"');
    let mut backslashes = 0usize;
    for c in a.chars() {
        match c {
            '\\' => {
                backslashes += 1;
                out.push('\\');
            }
            '"' => {
                // Double the run of backslashes, then escape the
                // quote.
                for _ in 0..backslashes {
                    out.push('\\');
                }
                out.push('\\');
                out.push('"');
                backslashes = 0;
            }
            _ => {
                backslashes = 0;
                out.push(c);
            }
        }
    }
    // Trailing backslash run before the closing quote must double.
    for _ in 0..backslashes {
        out.push('\\');
    }
    out.push('"');
    out
}

fn target_is_cmd(exe: &Path) -> bool {
    exe.file_name()
        .and_then(|n| n.to_str())
        .map(|s| {
            // Win32 strips trailing dots/spaces from the final
            // path component, so `cmd.exe.` launches real cmd —
            // match it here so it gets cmd quoting, not MSVCRT.
            let s = s.trim_end_matches(['.', ' ']);
            s.eq_ignore_ascii_case("cmd.exe") || s.eq_ignore_ascii_case("cmd")
        })
        .unwrap_or(false)
}

/// Build the full command line.
///
/// **Non-cmd targets:** every arg is MSVCRT-quoted via
/// [`quote_arg`] so `CommandLineToArgvW` in the child recovers
/// the exact argv.
///
/// **`cmd.exe` targets:** cmd does NOT use `CommandLineToArgvW`;
/// it parses `lpCommandLine` itself. With `/s`, it strips the
/// first and last `"` of the post-`/c` portion and runs what's
/// between *verbatim* under cmd's own rules. The caller is
/// expected to include `/s`; without it cmd falls back to the
/// legacy "if exactly two quotes and they wrap a runnable
/// command, strip them; otherwise leave alone" heuristic, and
/// the wrapper quote may not strip cleanly. (The TS
/// `wrapWithSandboxArgv` always passes `/d /s /c`.) So we:
///   1. Emit the exe + flags up to and including `/c|/k|/r`
///      using `quote_arg` (these are simple tokens; quoting is
///      a no-op unless the exe path has spaces).
///   2. Join the remaining argv elements with single spaces —
///      this is the user's cmd command string, reconstructed.
///   3. Wrap that in ONE outer `"…"` pair for `/s` to strip.
///
/// The post-`/c` content is **passed through unmodified**. We
/// do NOT caret-escape `& | < > ^ ( )` and do NOT touch `"` —
/// the contract is "this is a cmd.exe command string" and the
/// caller (the TS `wrapWithSandboxArgv`) supplies it as such.
/// `&` chains commands, `"…"` quotes — exactly as the user
/// typed. The child IS the sandbox, so cmd metachars here are
/// the user's tool, not an escape vector. (The host-shell
/// injection concern is about the OUTER spawn, which is solved
/// by argv-mode in the TS layer; this is the inner sandboxed
/// cmd.)
///
/// An earlier revision per-arg-doubled `"` → `""`, which cmd
/// treats as a quote-state *toggle*, not a literal — that
/// mis-parsed payloads containing `&` and was reverted.
pub fn build_cmdline(exe: &Path, args: &[String]) -> String {
    let cmd_split = if target_is_cmd(exe) {
        args.iter()
            .position(|a| matches!(a.to_ascii_lowercase().as_str(), "/c" | "/k" | "/r"))
    } else {
        None
    };
    let mut s = quote_arg(&exe.display().to_string());
    match cmd_split {
        Some(p) => {
            for a in &args[..=p] {
                s.push(' ');
                s.push_str(&quote_arg(a));
            }
            // One outer pair of quotes around the whole post-/c
            // command for `/s` to strip; contents verbatim.
            s.push_str(" \"");
            s.push_str(&args[p + 1..].join(" "));
            s.push('"');
        }
        None => {
            for a in args {
                s.push(' ');
                s.push_str(&quote_arg(a));
            }
        }
    }
    s
}

// ─── PROC_THREAD_ATTRIBUTE_LIST helper ──────────────────────────────

pub(crate) use crate::process_attributes::ProcThreadAttrs;

/// Mark this process's std handles inheritable and return them as
/// `[stdin, stdout, stderr]`. A slot whose handle is unavailable
/// (null / `INVALID_HANDLE_VALUE` / `SetHandleInformation` refused)
/// is `HANDLE::default()`.
///
/// `run_lockdown` plugs the array into BOTH `STARTUPINFO.hStd*`
/// (`STARTF_USESTDHANDLES`) and the `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`
/// inherit whitelist — one source of truth so a handle that didn't
/// make the whitelist is also `default()` in `hStd*` (the child sees
/// a null std handle for that stream rather than a stale value the
/// kernel never duplicated).
pub(crate) fn collect_inheritable_std_handles() -> [HANDLE; 3] {
    let mut out = [HANDLE::default(); 3];
    for (i, which) in [STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE]
        .into_iter()
        .enumerate()
    {
        let h = match unsafe { GetStdHandle(which) } {
            Ok(h) => h,
            Err(_) => continue,
        };
        if h.0.is_null() || (h.0 as isize) == -1 {
            continue;
        }
        // Best-effort: a detached caller may have non-inheritable
        // (or pseudo) handles here; skip rather than fail.
        let r = unsafe { SetHandleInformation(h, HANDLE_FLAG_INHERIT.0, HANDLE_FLAG_INHERIT) };
        if r.is_ok() {
            out[i] = h;
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn appcontainer_environment_does_not_inherit_the_broker() {
        let overlay = vec![("ALYSIS_TEST_ONLY".to_owned(), "present".to_owned())];
        let isolated = build_env_block_with_inheritance(&overlay, false);
        assert_eq!(
            String::from_utf16(&isolated).unwrap(),
            "ALYSIS_TEST_ONLY=present\0\0"
        );
        // The legacy runner still gets its separate account's environment.
        let legacy = String::from_utf16(&build_env_block(&overlay)).unwrap();
        assert!(legacy.contains("ALYSIS_TEST_ONLY=present\0"));
        assert!(legacy.to_ascii_uppercase().contains("SYSTEMROOT="));
    }

    #[test]
    fn quote_arg_simple() {
        assert_eq!(quote_arg("foo"), "foo");
        assert_eq!(quote_arg(""), "\"\"");
        assert_eq!(quote_arg("a b"), "\"a b\"");
    }

    #[test]
    fn quote_arg_backslash_quote() {
        // a\"b → "a\\\"b"
        assert_eq!(quote_arg(r#"a\"b"#), r#""a\\\"b""#);
        // trailing backslashes double before closing quote
        assert_eq!(quote_arg(r"a\"), r#""a\\""#);
        assert_eq!(quote_arg(r"a\\"), r#""a\\\\""#);
    }

    #[test]
    fn build_cmdline_cmd_passthrough() {
        let exe = Path::new(r"C:\Windows\System32\cmd.exe");
        // post-/c content is wrapped once in "…" for /s to strip;
        // inner quotes and metachars are NOT touched.
        let line = build_cmdline(
            exe,
            &[
                "/d".into(),
                "/s".into(),
                "/c".into(),
                r#"echo "x & y""#.into(),
            ],
        );
        assert_eq!(
            line,
            r#""C:\Windows\System32\cmd.exe" /d /s /c "echo "x & y"""#
        );
        // Multiple post-/c argv elements are joined with a space.
        let line2 = build_cmdline(
            exe,
            &[
                "/c".into(),
                "echo".into(),
                "a".into(),
                "&".into(),
                "echo".into(),
                "b".into(),
            ],
        );
        assert_eq!(
            line2,
            r#""C:\Windows\System32\cmd.exe" /c "echo a & echo b""#
        );
    }

    #[test]
    fn build_cmdline_cmd_no_split_when_no_c_flag() {
        // cmd.exe without /c|/k|/r → MSVCRT quoting throughout.
        let exe = Path::new("cmd.exe");
        let line = build_cmdline(exe, &["/?".into()]);
        assert_eq!(line, r#"cmd.exe /?"#);
    }

    #[test]
    fn build_cmdline_non_cmd_uses_msvcrt_quoting() {
        let exe = Path::new(r"C:\foo\bar.exe");
        let args = vec![r#"a "b"#.into()];
        let line = build_cmdline(exe, &args);
        assert!(line.ends_with(r#""a \"b""#), "got: {line}");
    }

    #[test]
    fn proxy_case_twins_suffix_rule_covers_any_proxy_var() {
        let mut entries = vec![
            // Any *_PROXY name is twinned, not just the standard trio.
            (
                "GRPC_PROXY".to_string(),
                "socks5h://localhost:60081".to_string(),
            ),
            // Mixed-case input → BOTH canonical forms appended.
            (
                "Http_Proxy".to_string(),
                "http://localhost:60080".to_string(),
            ),
            // Names that merely contain or extend the suffix are not.
            ("FOO_PROXYX".to_string(), "x".to_string()),
            ("PATH".to_string(), r"C:\Windows".to_string()),
        ];
        add_proxy_case_twins(&mut entries);
        let matching = |name: &str| {
            entries
                .iter()
                .filter(|(k, _)| k == name)
                .collect::<Vec<_>>()
        };
        assert_eq!(matching("grpc_proxy").len(), 1);
        assert_eq!(matching("grpc_proxy")[0].1, "socks5h://localhost:60081");
        assert_eq!(matching("GRPC_PROXY").len(), 1);
        // Mixed-case original is preserved AND both canonical forms added.
        assert_eq!(matching("Http_Proxy").len(), 1);
        assert_eq!(matching("HTTP_PROXY").len(), 1);
        assert_eq!(matching("http_proxy").len(), 1);
        assert_eq!(matching("http_proxy")[0].1, "http://localhost:60080");
        // Non-matching names untouched, nothing appended for them.
        assert_eq!(matching("FOO_PROXYX").len(), 1);
        assert!(matching("foo_proxyx").is_empty());
        assert_eq!(matching("PATH").len(), 1);
        assert!(matching("path").is_empty());
    }
}
