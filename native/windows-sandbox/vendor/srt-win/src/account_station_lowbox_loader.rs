//! Bounded diagnostic only, for one original fixed DEBUG_ONLY_THIS_PROCESS
//! child. Neither a parsed trace nor reaching Rust entry qualifies containment.
#![cfg(test)]

use super::*;
use crate::launch::SpawnedChild;
use std::{marker::PhantomData, rc::Rc};
use windows::Win32::System::Diagnostics::Debug::*;

#[path = "account_station_lowbox_calls.rs"]
mod calls;

pub(super) const MARKER: &str = "ALYSIS_STATION_LOWBOX_MAIN_ENTERED";
const FILTERED_MARKER: &str = "ALYSIS_STATION_FILTERED_MAIN_ENTERED";
pub(super) const FILTERED_MAX_WIRE: usize = 8192;
const MAX_EVENTS: u32 = 4096;
const MAX_MODULES: usize = 64;
const MAX_EXCEPTIONS: usize = 32;
const MAX_TEXT_ENTRIES: usize = 32;
const MAX_TEXT_BYTES: usize = 8192;
const MAX_TEXT_CHARS: usize = 512;
const MAX_WIRE: usize = 16384;
// Leave room for terminal status/HRESULT/counter changes without losing the
// final error message merely because the observer itself is now finishing.
const ADMISSION_WIRE: usize = MAX_WIRE - 256;

#[derive(Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum Stop {
    New,
    MainEntered,
    Exited,
    Deadline,
    EventLimit,
    Refused,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct ExceptionObservation {
    code: u32,
    thread: u32,
    first_chance: bool,
    address: u64,
    handled: bool,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct LoaderTrace {
    pub(super) diagnostic_only: bool,
    pub(super) production_ready: bool,
    pub(super) permissions_qualified: bool,
    pub(super) capture_complete: bool,
    pub(super) stop: Stop,
    pub(super) original_pid: u32,
    pub(super) original_created: u64,
    pub(super) main_entered: bool,
    pub(super) exit_code: Option<u32>,
    pub(super) event_count: u32,
    pub(super) modules: Vec<String>,
    pub(super) modules_dropped: u32,
    pub(super) exceptions: Vec<u32>,
    exception_details: Vec<ExceptionObservation>,
    pub(super) exceptions_dropped: u32,
    calls: Option<calls::Evidence>,
    pub(super) loader_output: Vec<String>,
    pub(super) text_dropped: u32,
    pub(super) text_truncated: u32,
    pub(super) native_hresult: Option<i32>,
    diagnostic_error: Option<String>,
    pub(super) shared_creation_process_handle: Option<bool>,
    pub(super) shared_creation_thread_handle: Option<bool>,
}
impl LoaderTrace {
    pub(super) fn new() -> Self {
        Self {
            diagnostic_only: true,
            production_ready: false,
            permissions_qualified: false,
            capture_complete: false,
            stop: Stop::New,
            original_pid: 0,
            original_created: 0,
            main_entered: false,
            exit_code: None,
            event_count: 0,
            modules: Vec::new(),
            modules_dropped: 0,
            exceptions: Vec::new(),
            exception_details: Vec::new(),
            exceptions_dropped: 0,
            calls: None,
            loader_output: Vec::new(),
            text_dropped: 0,
            text_truncated: 0,
            native_hresult: None,
            diagnostic_error: None,
            shared_creation_process_handle: None,
            shared_creation_thread_handle: None,
        }
    }
    pub(super) fn require(&self) -> Result<()> {
        if let Some(calls) = &self.calls {
            calls.require()?;
        }
        if let Some(error) = &self.diagnostic_error {
            ensure!(
                !error.is_empty() && error.len() <= 160 && !error.chars().any(char::is_control),
                "loader diagnostic error bound differs"
            );
        }
        ensure!(
            self.exception_details.len() == self.exceptions.len()
                && self
                    .exception_details
                    .iter()
                    .zip(&self.exceptions)
                    .all(|(detail, code)| detail.code == *code
                        && detail.thread != 0
                        && detail.address != 0
                        && (!detail.handled
                            || (detail.first_chance
                                && matches!(detail.code, 0x80000003 | 0x80000004)))),
            "loader exception detail differs"
        );
        ensure!(
            self.diagnostic_only && !self.production_ready && !self.permissions_qualified,
            "loader trace cannot qualify permissions or deployment"
        );
        ensure!(
            self.event_count <= MAX_EVENTS
                && self.modules.len() <= MAX_MODULES
                && self.exceptions.len() <= MAX_EXCEPTIONS
                && self.loader_output.len() <= MAX_TEXT_ENTRIES,
            "loader trace exceeds inventory bounds"
        );
        ensure!(
            self.modules_dropped <= MAX_EVENTS
                && self.exceptions_dropped <= MAX_EVENTS
                && self.text_dropped <= MAX_EVENTS
                && self.text_truncated <= MAX_EVENTS,
            "loader drop counter exceeds event bound"
        );
        ensure!(
            self.modules.len() + self.modules_dropped as usize <= self.event_count as usize
                && self.exceptions.len() + self.exceptions_dropped as usize
                    <= self.event_count as usize
                && self.loader_output.len() + self.text_dropped as usize
                    <= self.event_count as usize
                && self.text_truncated <= self.event_count
                && ((!self.main_entered && self.exit_code.is_none()) || self.event_count > 0)
                && self.shared_creation_process_handle.is_some()
                    == self.shared_creation_thread_handle.is_some(),
            "loader observations exceed delivered events"
        );
        for (index, name) in self.modules.iter().enumerate() {
            require_basename(name)?;
            ensure!(
                !self.modules[..index]
                    .iter()
                    .any(|old| old.eq_ignore_ascii_case(name)),
                "loader module basename repeated"
            );
        }
        let mut text_bytes = 0;
        for text in &self.loader_output {
            ensure!(
                !text.is_empty()
                    && text.chars().count() <= MAX_TEXT_CHARS
                    && !text.chars().any(|c| c.is_ascii_control()),
                "loader text is unbounded or unsanitized"
            );
            text_bytes += text.len();
        }
        ensure!(
            text_bytes <= MAX_TEXT_BYTES,
            "loader text byte bound exceeded"
        );
        if self.stop == Stop::New {
            ensure!(
                self.original_pid == 0
                    && self.original_created == 0
                    && self.event_count == 0
                    && !self.main_entered
                    && self.exit_code.is_none()
                    && !self.capture_complete
                    && self.modules.is_empty()
                    && self.exceptions.is_empty()
                    && self.calls.is_none()
                    && self.loader_output.is_empty()
                    && self.modules_dropped == 0
                    && self.exceptions_dropped == 0
                    && self.text_dropped == 0
                    && self.text_truncated == 0
                    && self.native_hresult.is_none()
                    && self.diagnostic_error.is_none()
                    && self.shared_creation_process_handle.is_none()
                    && self.shared_creation_thread_handle.is_none(),
                "new loader trace already contains observations"
            );
        } else {
            ensure!(
                self.original_pid != 0 && self.original_created != 0,
                "loader trace original process identity absent"
            );
        }
        ensure!(
            !(self.main_entered && self.exit_code.is_some()),
            "loader continued beyond first terminal observation"
        );
        if self.capture_complete {
            ensure!(
                self.native_hresult.is_none()
                    && self.diagnostic_error.is_none()
                    && self.event_count > 0
                    && ((self.stop == Stop::MainEntered
                        && self.main_entered
                        && self.exit_code.is_none())
                        || (self.stop == Stop::Exited
                            && !self.main_entered
                            && self.exit_code.is_some())),
                "loader completion lacks original marker/exit"
            );
        } else {
            ensure!(
                !matches!(self.stop, Stop::MainEntered | Stop::Exited),
                "unfinished loader claims terminal completion"
            );
        }
        ensure!(
            serde_json::to_vec(self)?.len() <= MAX_WIRE,
            "loader trace exceeds serialized bound"
        );
        Ok(())
    }
    // The nested filtered trace shares the terminal envelope with the original
    // lowbox proofs. Retain call evidence; compact only bounded inventories and
    // account for every dropped record. The original loader budget is unchanged.
    pub(super) fn compact_filtered(&mut self) -> Result<()> {
        while serde_json::to_vec(self)?.len() > FILTERED_MAX_WIRE {
            if !self.loader_output.is_empty() {
                self.loader_output.remove(0);
                self.text_dropped += 1;
            } else if !self.modules.is_empty() {
                self.modules.remove(0);
                self.modules_dropped += 1;
            } else if !self.exceptions.is_empty() {
                self.exceptions.remove(0);
                self.exception_details.remove(0);
                self.exceptions_dropped += 1;
            } else {
                anyhow::bail!("filtered loader fixed evidence exceeds nested budget");
            }
        }
        self.require()
    }
    fn trim_text(&mut self) -> Result<()> {
        while self.loader_output.len() > MAX_TEXT_ENTRIES
            || self.loader_output.iter().map(String::len).sum::<usize>() > MAX_TEXT_BYTES
            || serde_json::to_vec(self)?.len() > ADMISSION_WIRE
        {
            ensure!(
                !self.loader_output.is_empty(),
                "loader nontext evidence exceeds serialized budget"
            );
            self.loader_output.remove(0);
            self.text_dropped += 1;
        }
        Ok(())
    }
    fn module(&mut self, name: String) -> Result<()> {
        require_basename(&name)?;
        if !self
            .modules
            .iter()
            .any(|old| old.eq_ignore_ascii_case(&name))
        {
            if self.modules.len() < MAX_MODULES {
                self.modules.push(name);
            } else {
                self.modules_dropped += 1;
            }
        }
        self.trim_text()
    }
    fn output(&mut self, text: &str, truncated: bool) -> Result<()> {
        let (text, shortened) = sanitize(text);
        if truncated || shortened {
            self.text_truncated += 1;
        }
        if !text.is_empty() {
            self.loader_output.push(text);
        }
        self.trim_text()
    }
}

fn require_basename(name: &str) -> Result<()> {
    ensure!(
        !name.is_empty()
            && name.len() <= 128
            && name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
            && !matches!(name, "." | ".."),
        "loader module basename refused"
    );
    Ok(())
}
fn sanitize(text: &str) -> (String, bool) {
    let mut chars = text.chars();
    let cleaned = chars
        .by_ref()
        .take(MAX_TEXT_CHARS)
        .map(|c| if c.is_ascii_control() { ' ' } else { c })
        .collect::<String>();
    (cleaned, chars.next().is_some())
}
fn module_name(file: HANDLE) -> Result<String> {
    let mut path = vec![0u16; 32768];
    let length =
        unsafe { GetFinalPathNameByHandleW(file, &mut path, GETFINALPATHNAMEBYHANDLE_FLAGS(0)) }
            as usize;
    ensure!(
        length > 0 && length < path.len(),
        "loader module path unavailable"
    );
    let path = String::from_utf16(&path[..length])?;
    let name = path.rsplit(['\\', '/']).next().unwrap_or("");
    require_basename(name)?;
    Ok(name.to_owned())
}
fn debug_text(process: HANDLE, info: OUTPUT_DEBUG_STRING_INFO) -> Result<(String, bool)> {
    ensure!(info.fUnicode <= 1, "loader string encoding differs");
    let total = info.nDebugStringLength as usize;
    let length = total.min(MAX_TEXT_CHARS);
    if length == 0 {
        return Ok((String::new(), false));
    }
    let width = if info.fUnicode == 0 { 1 } else { 2 };
    // Keep the bounded tail of a long message, as well as the latest messages.
    // A truncated suffix can never satisfy the exact entry-marker predicate.
    let offset = (total - length) * width;
    let address = (info.lpDebugStringData.0 as usize)
        .checked_add(offset)
        .context("loader string address overflow")?;
    let mut bytes = vec![0u8; length * width];
    let mut read = 0;
    unsafe {
        ReadProcessMemory(
            process,
            address as *const _,
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
    Ok((text.trim_end_matches('\0').to_owned(), total > length))
}

/// Own independent copies through EXIT event continuation. Original creation
/// handles can be identical to Windows-owned debug handles; they are never
/// closed based on an exit code or a guessed alias. Only this sealed diagnostic
/// uses this wrapper; ordinary SpawnedChild ownership remains unchanged.
pub(super) struct DebugBirth {
    creation: PROCESS_INFORMATION,
    child: SpawnedChild,
    observed: std::cell::Cell<bool>,
}
impl DebugBirth {
    pub(super) fn new(creation: PROCESS_INFORMATION) -> Self {
        let retain = || -> Result<SpawnedChild> {
            let mut process = HANDLE::default();
            unsafe {
                DuplicateHandle(
                    GetCurrentProcess(),
                    creation.hProcess,
                    GetCurrentProcess(),
                    &mut process,
                    0,
                    false,
                    DUPLICATE_SAME_ACCESS,
                )?;
            }
            let process = OwnedHandle(process);
            let mut thread = HANDLE::default();
            unsafe {
                DuplicateHandle(
                    GetCurrentProcess(),
                    creation.hThread,
                    GetCurrentProcess(),
                    &mut thread,
                    0,
                    false,
                    DUPLICATE_SAME_ACCESS,
                )?;
            }
            let thread = OwnedHandle(thread);
            noninheritable(process.raw())?;
            noninheritable(thread.raw())?;
            Ok(SpawnedChild::new(PROCESS_INFORMATION {
                hProcess: process.into_raw(),
                hThread: thread.into_raw(),
                dwProcessId: creation.dwProcessId,
                dwThreadId: creation.dwThreadId,
            }))
        };
        // Before the first debug event there is no proven custody split. A
        // failed duplicate must not unwind into private USER/profile cleanup.
        let child = retain().unwrap_or_else(|_| std::process::abort());
        Self {
            creation,
            child,
            observed: std::cell::Cell::new(false),
        }
    }
    pub(super) fn child(&self) -> &SpawnedChild {
        &self.child
    }
    fn observe_creation(&self, process: HANDLE, thread: HANDLE) -> Result<()> {
        ensure!(
            !self.observed.get(),
            "debug creation custody observed twice"
        );
        ensure!(
            process != self.child.process()
                && thread != self.child.thread()
                && unsafe { CompareObjectHandles(process, self.child.process()).as_bool() }
                && unsafe { CompareObjectHandles(thread, self.child.thread()).as_bool() },
            "debug event differs from independently retained child"
        );
        // Windows closes exact aliases on exit/detach. Nonaliases still belong
        // to this wrapper and are closed once now, before any handle can recycle.
        if process != self.creation.hProcess {
            unsafe {
                CloseHandle(self.creation.hProcess)?;
            }
        }
        if thread != self.creation.hThread {
            unsafe {
                CloseHandle(self.creation.hThread)?;
            }
        }
        self.observed.set(true);
        Ok(())
    }
}
impl Drop for DebugBirth {
    fn drop(&mut self) {
        if !self.observed.get() {
            std::process::abort(); // Unproved original-handle custody; no outer cleanup.
        }
        // The independent armed SpawnedChild drains after debugger detachment.
    }
}

/// Immediately construct after successful debug birth. The lifetime enforces
/// detach-before-SpawnedChild cleanup, including all fallible setup paths.
/// Rc marker prevents moving Windows' thread-owned debug session elsewhere.
pub(super) struct Guard<'a> {
    child: &'a SpawnedChild,
    _desktop: &'a windows::Win32::System::StationsAndDesktops::HDESK,
    original: Option<OwnedHandle>,
    pid: u32,
    created: u64,
    thread: u32,
    attempted: bool,
    calls: Option<calls::Observer>,
    filtered_birth: Option<&'a DebugBirth>,
    marker: &'static str,
    _thread_affine: PhantomData<Rc<()>>,
}
impl<'a> Guard<'a> {
    pub(super) fn new(
        child: &'a SpawnedChild,
        desktop: &'a windows::Win32::System::StationsAndDesktops::HDESK,
    ) -> Result<Self> {
        // Establish detach responsibility BEFORE the first fallible operation.
        let mut guard = Self {
            child,
            _desktop: desktop,
            original: None,
            pid: unsafe { GetProcessId(child.process()) },
            created: 0,
            thread: unsafe { GetCurrentThreadId() },
            attempted: false,
            calls: None,
            filtered_birth: None,
            marker: MARKER,
            _thread_affine: PhantomData,
        };
        ensure!(guard.pid != 0, "debug child identity unavailable");
        let mut raw = HANDLE::default();
        unsafe {
            DuplicateHandle(
                GetCurrentProcess(),
                child.process(),
                GetCurrentProcess(),
                &mut raw,
                (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
                false,
                DUPLICATE_HANDLE_OPTIONS(0),
            )?;
        }
        guard.original = Some(OwnedHandle(raw));
        noninheritable(raw)?;
        guard.created = bootstrap::pull::creation_time(raw)?;
        unsafe {
            DebugSetProcessKillOnExit(true)?;
        }
        guard.calls = Some(calls::Observer::new(
            child.process(),
            child.thread(),
            HANDLE(desktop.0),
        )?);
        Ok(guard)
    }
    pub(super) fn new_filtered(
        birth: &'a DebugBirth,
        desktop: &'a windows::Win32::System::StationsAndDesktops::HDESK,
    ) -> Result<Self> {
        let mut guard = Self::new(birth.child(), desktop)?;
        guard.filtered_birth = Some(birth);
        guard.marker = FILTERED_MARKER;
        Ok(guard)
    }
    pub(super) fn observe(&mut self, trace: &mut LoaderTrace) -> Result<()> {
        ensure!(!self.attempted, "loader observer is one-shot");
        self.attempted = true;
        trace.require()?;
        ensure!(
            trace.stop == Stop::New,
            "loader trace cannot be resumed/reconstructed"
        );
        trace.original_pid = self.pid;
        trace.original_created = self.created;
        trace.stop = Stop::Refused;
        let result = self.observe_inner(trace);
        if let Err(error) = &result {
            trace.native_hresult = native_code(error);
            // Only fixed observer messages are admitted, never arbitrary
            // native error text, paths, memory contents or nested error chains.
            let message = error.to_string();
            trace.diagnostic_error = Some(
                if message.starts_with("call ")
                    || matches!(
                        message.as_str(),
                        "read original call thread context"
                            | "program bounded call breakpoints"
                            | "restore original call debug registers"
                            | "fixed call export missing"
                            | "short call image/frame read"
                    )
                {
                    message.chars().take(160).collect()
                } else {
                    "loader observer refused; inspect native HRESULT and bounded observations"
                        .into()
                },
            );
            trace.capture_complete = false;
            if !matches!(trace.stop, Stop::Deadline | Stop::EventLimit) {
                trace.stop = Stop::Refused;
            }
        }
        trace.trim_text()?;
        trace.require()?;
        result
    }
    fn observe_inner(&mut self, trace: &mut LoaderTrace) -> Result<()> {
        ensure!(
            unsafe { GetCurrentThreadId() } == self.thread,
            "loader observer thread differs"
        );
        let original = self
            .original
            .as_ref()
            .context("original debugger process reference absent")?;
        ensure!(
            unsafe { GetProcessId(original.raw()) } == self.pid
                && bootstrap::pull::creation_time(original.raw())? == self.created,
            "loader original process differs"
        );
        let calls = self.calls.as_mut().context("loader call observer absent")?;
        trace.calls = Some(calls.evidence());
        let end = Instant::now() + Duration::from_secs(5);
        loop {
            if Instant::now() >= end {
                trace.stop = Stop::Deadline;
                anyhow::bail!("loader diagnostic deadline exceeded");
            }
            if trace.event_count == MAX_EVENTS {
                trace.stop = Stop::EventLimit;
                anyhow::bail!("loader diagnostic event bound exceeded");
            }
            let wait = end
                .saturating_duration_since(Instant::now())
                .as_millis()
                .clamp(1, 250) as u32;
            let mut event = DEBUG_EVENT::default();
            match unsafe { WaitForDebugEventEx(&mut event, wait) } {
                Ok(()) => {}
                Err(error) if error.code() == ERROR_SEM_TIMEOUT.to_hresult() => continue,
                Err(error) => return Err(error.into()),
            }
            trace.event_count += 1;
            let mut continuation = if event.dwDebugEventCode == EXCEPTION_DEBUG_EVENT {
                DBG_EXCEPTION_NOT_HANDLED
            } else {
                DBG_CONTINUE
            };
            // Own ONLY hFile. Windows owns debug-event process/thread handles
            // through exit-event continuation, even if they alias spawn handles.
            let file = unsafe {
                match event.dwDebugEventCode {
                    CREATE_PROCESS_DEBUG_EVENT => Some(event.u.CreateProcessInfo.hFile),
                    LOAD_DLL_DEBUG_EVENT => Some(event.u.LoadDll.hFile),
                    _ => None,
                }
            }
            .filter(|h| !h.is_invalid())
            .map(OwnedHandle);
            let observed = (|| -> Result<bool> {
                ensure!(
                    event.dwProcessId == self.pid,
                    "loader received another process event"
                );
                unsafe {
                    match event.dwDebugEventCode {
                        CREATE_PROCESS_DEBUG_EVENT => {
                            let info = event.u.CreateProcessInfo;
                            if let Some(birth) = self.filtered_birth {
                                birth.observe_creation(info.hProcess, info.hThread)?;
                            }
                            trace.shared_creation_process_handle = Some(
                                info.hProcess
                                    == self
                                        .filtered_birth
                                        .map_or(self.child.process(), |b| b.creation.hProcess),
                            );
                            trace.shared_creation_thread_handle = Some(
                                info.hThread
                                    == self
                                        .filtered_birth
                                        .map_or(self.child.thread(), |b| b.creation.hThread),
                            );
                            ensure!(
                                CompareObjectHandles(info.hProcess, original.raw()).as_bool(),
                                "loader creation event differs from original child"
                            );
                            calls.thread_environment(
                                event.dwThreadId,
                                info.lpThreadLocalBase as usize,
                            )?;
                            if let Some(file) = &file {
                                trace.module(module_name(file.raw())?)?;
                            }
                        }
                        LOAD_DLL_DEBUG_EVENT => {
                            if let Some(file) = &file {
                                let name = module_name(file.raw())?;
                                trace.module(name.clone())?;
                                calls.module(
                                    file.raw(),
                                    &name,
                                    event.u.LoadDll.lpBaseOfDll as usize,
                                )?;
                            }
                        }
                        OUTPUT_DEBUG_STRING_EVENT => {
                            let (text, truncated) =
                                debug_text(self.child.process(), event.u.DebugString)?;
                            if !truncated && text == self.marker {
                                trace.main_entered = true;
                                return Ok(true);
                            }
                            trace.output(&text, truncated)?;
                        }
                        EXCEPTION_DEBUG_EVENT => {
                            let exception = event.u.Exception;
                            let code = exception.ExceptionRecord.ExceptionCode.0 as u32;
                            let address = exception.ExceptionRecord.ExceptionAddress as usize;
                            let first_chance = exception.dwFirstChance == 1;
                            let handled =
                                calls.exception(event.dwThreadId, first_chance, address, code)?;
                            if handled {
                                continuation = DBG_CONTINUE;
                            }
                            if trace.exceptions.len() < MAX_EXCEPTIONS {
                                trace.exceptions.push(code);
                                trace.exception_details.push(ExceptionObservation {
                                    code,
                                    thread: event.dwThreadId,
                                    first_chance,
                                    address: address as u64,
                                    handled,
                                });
                            } else {
                                trace.exceptions_dropped += 1;
                            }
                        }
                        EXIT_PROCESS_DEBUG_EVENT => {
                            trace.exit_code = Some(event.u.ExitProcess.dwExitCode);
                            return Ok(true);
                        }
                        _ => {}
                    }
                }
                trace.trim_text()?;
                Ok(false)
            })();
            // A delivered event suspends the process. Restore only in that
            // state, before continuation, on every terminal or error path.
            // An error must never skip continuation of the delivered event.
            let restored = if observed.is_err() || matches!(observed, Ok(true)) {
                calls.restore()
            } else {
                Ok(())
            };
            trace.calls = Some(calls.evidence());
            drop(file);
            // No ? or other fallible operation may skip this call after an
            // event has been delivered, including wrong-PID and query errors.
            let continued =
                unsafe { ContinueDebugEvent(event.dwProcessId, event.dwThreadId, continuation) };
            continued?;
            restored?;
            let stop = observed?;
            if stop {
                ensure!(
                    Instant::now() <= end,
                    "loader terminal observation exceeded deadline"
                );
                trace.stop = if trace.main_entered {
                    Stop::MainEntered
                } else {
                    Stop::Exited
                };
                trace.capture_complete = true;
                return Ok(());
            }
        }
    }
}
impl Drop for Guard<'_> {
    fn drop(&mut self) {
        let process = self
            .original
            .as_ref()
            .map_or(self.child.process(), |p| p.raw());
        // A deadline can occur between events, when the original thread is
        // running. Never modify its context in that state or detach a live
        // child that still has diagnostic breakpoints installed. Terminate
        // only this retained synthetic child; detachment then permits exit.
        if self.calls.as_ref().is_some_and(|calls| calls.active())
            && unsafe { WaitForSingleObject(process, 0) } != WAIT_OBJECT_0
            && unsafe { TerminateProcess(self.child.process(), 0x4161) }.is_err()
            && unsafe { WaitForSingleObject(process, 0) } != WAIT_OBJECT_0
        {
            std::process::abort();
        }
        // On an early exit the kernel may already remove the debug attachment.
        // Accept failed detach only after THIS original retained process exits.
        let detached = self.pid != 0 && unsafe { DebugActiveProcessStop(self.pid) }.is_ok();
        if !detached && unsafe { WaitForSingleObject(process, 30_000) } != WAIT_OBJECT_0 {
            std::process::abort(); // Neither safe detach nor exact exit is established.
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn active() -> LoaderTrace {
        let mut t = LoaderTrace::new();
        t.original_pid = 7;
        t.original_created = 9;
        t.stop = Stop::Refused;
        t
    }
    #[test]
    fn station_loader_trace_is_diagnostic_even_when_entry_or_exit_is_observed() {
        LoaderTrace::new().require().unwrap();
        let mut t = active();
        t.event_count = 1;
        t.main_entered = true;
        t.stop = Stop::MainEntered;
        t.capture_complete = true;
        t.require().unwrap();
        t.production_ready = true;
        assert!(t.require().is_err());
        t.production_ready = false;
        t.permissions_qualified = true;
        assert!(t.require().is_err());
        t.permissions_qualified = false;
        t.exit_code = Some(0xc0000142);
        assert!(t.require().is_err());
        t.main_entered = false;
        t.stop = Stop::Exited;
        t.require().unwrap();
        t.native_hresult = Some(-1);
        assert!(t.require().is_err());
        t.capture_complete = false;
        t.stop = Stop::Refused;
        t.require().unwrap();
    }
    #[test]
    fn station_loader_text_ring_keeps_latest_messages_and_sanitizes_controls() {
        let mut t = active();
        for n in 0..100 {
            t.output(&format!("message{n}\r\n\t\u{7f}"), false).unwrap();
        }
        t.event_count = 100;
        assert_eq!(t.loader_output.len(), 32);
        assert_eq!(t.text_dropped, 68);
        assert!(t.loader_output.last().unwrap().starts_with("message99"));
        assert!(
            t.loader_output
                .iter()
                .all(|s| !s.chars().any(|c| c.is_ascii_control()))
        );
        t.output(&"界".repeat(1000), false).unwrap();
        t.event_count += 1;
        assert_eq!(t.loader_output.last().unwrap().chars().count(), 512);
        assert_eq!(t.text_truncated, 1);
        t.require().unwrap();
    }
    #[test]
    fn station_loader_ring_reserves_serialized_budget_under_adversarial_escaping() {
        let mut t = active();
        for n in 0..64 {
            t.module(format!("{:03}{}.dll", n, "a".repeat(120)))
                .unwrap();
        }
        for _ in 0..80 {
            t.output(&"\\\"".repeat(256), false).unwrap();
        }
        t.event_count = 144;
        assert!(t.text_dropped > 0);
        assert!(t.loader_output.iter().map(String::len).sum::<usize>() <= MAX_TEXT_BYTES);
        assert!(serde_json::to_vec(&t).unwrap().len() <= ADMISSION_WIRE);
        t.require().unwrap();
        t.loader_output = vec!["\\".repeat(512); 16];
        assert!(t.require().is_err()); // Raw text is 8192, but JSON exceeds 16KiB.
    }
    #[test]
    fn station_loader_parser_refuses_paths_unknown_fields_and_unbounded_inventories() {
        for s in [
            "C:\\Windows\\ntdll.dll",
            "../bad.dll",
            "",
            "..",
            "bad\n.dll",
        ] {
            assert!(require_basename(s).is_err());
        }
        assert!(require_basename(&"x".repeat(129)).is_err());
        let mut t = active();
        t.modules = vec!["ntdll.dll".into(), "NTDLL.DLL".into()];
        assert!(t.require().is_err());
        t.modules.clear();
        t.exceptions = vec![1; 33];
        assert!(t.require().is_err());
        t.exceptions.clear();
        t.event_count = 4097;
        assert!(t.require().is_err());
        t.event_count = 0;
        t.loader_output = vec!["unsafe\0text".into()];
        assert!(t.require().is_err());
        let mut v = serde_json::to_value(LoaderTrace::new()).unwrap();
        v["resume_authorized"] = true.into();
        assert!(serde_json::from_value::<LoaderTrace>(v).is_err());
        let mut v = serde_json::to_value(LoaderTrace::new()).unwrap();
        v["stop"] = "qualified".into();
        assert!(serde_json::from_value::<LoaderTrace>(v).is_err());
    }

    #[test]
    fn station_loader_exception_evidence_preserves_chance_and_handling() {
        let mut t = active();
        t.event_count = 1;
        t.exceptions.push(0x80000003);
        t.exception_details.push(ExceptionObservation {
            code: 0x80000003,
            thread: 11,
            first_chance: true,
            address: 0x1000,
            handled: true,
        });
        t.require().unwrap();
        t.exception_details[0].first_chance = false;
        assert!(t.require().is_err());
        t.exception_details[0].handled = false;
        t.require().unwrap();
        t.exceptions[0] = 0xc0000005;
        assert!(t.require().is_err());
        t.exception_details[0].code = 0xc0000005;
        t.require().unwrap();
        t.exception_details[0].first_chance = true;
        t.exception_details[0].handled = true;
        assert!(t.require().is_err());
    }
}
