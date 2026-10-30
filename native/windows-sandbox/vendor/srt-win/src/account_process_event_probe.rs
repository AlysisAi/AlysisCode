//! Bounded, opt-in kernel-event observer for one private test executable.
//! It records no unrelated images or command lines and creates no trace file.
//! Events are observations only: they cannot construct production authority.
use super::*;
use serde::Serialize;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicU32, Ordering},
};
use windows::{
    Win32::System::Diagnostics::Etw::*,
    core::{GUID, PCWSTR, PWSTR},
};

const PROVIDER: GUID = GUID::from_u128(0x22fb2cd6_0e7b_422b_a0c7_2fad1fd0e716);

#[derive(Clone, Serialize)]
pub(in crate::account_control) struct ProcessEvent {
    pub id: u16,
    pub pid: u32,
    pub created: u64,
}
struct CallbackState {
    target: String,
    records: Mutex<Vec<ProcessEvent>>,
    errors: AtomicU32,
    first_error: Mutex<Option<String>>,
}

fn property(event: &EVENT_RECORD, name: &str, max: u32) -> Result<Vec<u8>> {
    let name = wstr(name);
    let descriptor = PROPERTY_DATA_DESCRIPTOR {
        PropertyName: name.as_ptr() as u64,
        ArrayIndex: u32::MAX,
        Reserved: 0,
    };
    let mut size = 0;
    let status = unsafe { TdhGetPropertySize(event, None, &[descriptor], &mut size) };
    ensure!(
        status == 0 && size > 0 && size <= max,
        "kernel event property size refused ({status})"
    );
    let mut bytes = vec![0; size as usize];
    let status = unsafe { TdhGetProperty(event, None, &[descriptor], &mut bytes) };
    ensure!(status == 0, "kernel event property read refused ({status})");
    Ok(bytes)
}
fn observe(event: &EVENT_RECORD, state: &CallbackState) -> Result<()> {
    if event.EventHeader.ProviderId != PROVIDER
        || !matches!(event.EventHeader.EventDescriptor.Id, 1 | 2)
    {
        return Ok(());
    }
    let id = event.EventHeader.EventDescriptor.Id;
    if id == 1 {
        let name = property(event, "ImageName", 65536).context("start image property")?;
        ensure!(name.len() % 2 == 0, "invalid kernel image encoding");
        let wide: Vec<u16> = name
            .chunks_exact(2)
            .map(|b| u16::from_le_bytes([b[0], b[1]]))
            .collect();
        ensure!(wide.last() == Some(&0), "unterminated kernel image");
        let name = String::from_utf16(&wide[..wide.len() - 1])?;
        if !name.eq_ignore_ascii_case(&state.target) {
            return Ok(());
        }
    }
    // ProcessStop ImageName is ANSI (and may be shortened), unlike the
    // Unicode ProcessStart path. Never reinterpret it or match by basename.
    // An exit is relevant only when BOTH native identity fields match a
    // previously observed exact target birth. No unrelated names are retained.
    let pid = u32::from_le_bytes(
        property(event, "ProcessID", 4)
            .context("process identity property")?
            .try_into()
            .map_err(|_| anyhow::anyhow!("invalid process ID extent"))?,
    );
    let created = u64::from_le_bytes(
        property(event, "CreateTime", 8)
            .context("creation identity property")?
            .try_into()
            .map_err(|_| anyhow::anyhow!("invalid process time extent"))?,
    );
    retain(state, id, pid, created)
}

fn retain(state: &CallbackState, id: u16, pid: u32, created: u64) -> Result<()> {
    let mut records = state
        .records
        .lock()
        .map_err(|_| anyhow::anyhow!("kernel observer poisoned"))?;
    if id == 2
        && !records
            .iter()
            .any(|e| e.id == 1 && e.pid == pid && e.created == created)
    {
        return Ok(());
    }
    ensure!(pid != 0 && created != 0, "missing kernel birth identity");
    ensure!(
        records.len() < 32,
        "kernel observer target-event limit reached"
    );
    records.push(ProcessEvent { id, pid, created });
    Ok(())
}
unsafe extern "system" fn callback(raw: *mut EVENT_RECORD) {
    if raw.is_null() {
        return;
    }
    let event = unsafe { &*raw };
    if event.UserContext.is_null() {
        return;
    }
    let state = unsafe { &*(event.UserContext as *const CallbackState) };
    // No Rust unwind is permitted to escape the native callback.
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| observe(event, state)));
    let failure = match result {
        Ok(Ok(())) => None,
        Ok(Err(error)) => Some(format!("{error:#}")),
        Err(_) => Some("kernel callback panicked".into()),
    };
    if let Some(error) = failure {
        state.errors.fetch_add(1, Ordering::Relaxed);
        if let Ok(mut first) = state.first_error.lock() {
            if first.is_none() {
                // Static decoder diagnostics and event schema numbers only;
                // never store payload bytes, command lines or unrelated paths.
                *first = Some(format!(
                    "event {} version {}: {error}",
                    event.EventHeader.EventDescriptor.Id, event.EventHeader.EventDescriptor.Version
                ));
            }
        }
    }
}

#[repr(C)]
struct Properties {
    properties: EVENT_TRACE_PROPERTIES,
    name: [u16; 128],
}
pub(in crate::account_control) struct Observer {
    properties: Box<Properties>,
    session: Option<CONTROLTRACE_HANDLE>,
    consumer: Option<PROCESSTRACE_HANDLE>,
    worker: Option<std::thread::JoinHandle<u32>>,
    state: Arc<CallbackState>,
}
impl Observer {
    pub(in crate::account_control) fn start(path: &Path) -> Result<Self> {
        // Get the native-volume spelling before the caller requests an oplock.
        let file = OwnedHandle(unsafe {
            CreateFileW(
                pcwstr(&wstr(path.to_str().context("non-Unicode event target")?)),
                FILE_READ_ATTRIBUTES.0,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )?
        });
        let mut buffer = vec![0u16; 32768];
        let n =
            unsafe { GetFinalPathNameByHandleW(file.raw(), &mut buffer, VOLUME_NAME_NT) } as usize;
        ensure!(n > 0 && n < buffer.len(), "native event target unavailable");
        let target = String::from_utf16(&buffer[..n])?;
        drop(file);
        let name = wstr(&format!(
            "AlysisLogonProbe.{}",
            crate::account_state::execution::random_id()?
        ));
        let mut properties = Box::new(Properties {
            properties: EVENT_TRACE_PROPERTIES::default(),
            name: [0; 128],
        });
        properties.name[..name.len()].copy_from_slice(&name);
        let p = &mut properties.properties;
        p.Wnode.BufferSize = size_of::<Properties>() as u32;
        p.Wnode.Flags = WNODE_FLAG_TRACED_GUID;
        p.Wnode.ClientContext = 1;
        p.BufferSize = 64;
        p.MinimumBuffers = 2;
        p.MaximumBuffers = 4;
        p.LogFileMode = EVENT_TRACE_REAL_TIME_MODE | EVENT_TRACE_NO_PER_PROCESSOR_BUFFERING;
        p.FlushTimer = 1;
        p.LoggerNameOffset = std::mem::offset_of!(Properties, name) as u32;
        let mut session = CONTROLTRACE_HANDLE::default();
        unsafe { StartTraceW(&mut session, pcwstr(&name), &mut properties.properties) }
            .ok()
            .context("start private process-event session")?;
        // This guard owns only the successfully created, random session. A name
        // collision returns before a guard exists; it cannot stop another trace.
        let mut observer = Self {
            properties,
            session: Some(session),
            consumer: None,
            worker: None,
            state: Arc::new(CallbackState {
                target,
                records: Mutex::new(Vec::new()),
                errors: AtomicU32::new(0),
                first_error: Mutex::new(None),
            }),
        };
        let mut logfile = EVENT_TRACE_LOGFILEW::default();
        logfile.LoggerName = PWSTR(observer.properties.name.as_mut_ptr());
        logfile.Anonymous1.ProcessTraceMode =
            PROCESS_TRACE_MODE_EVENT_RECORD | PROCESS_TRACE_MODE_REAL_TIME;
        logfile.Anonymous2.EventRecordCallback = Some(callback);
        logfile.Context = Arc::as_ptr(&observer.state) as *mut _;
        let consumer = unsafe { OpenTraceW(&mut logfile) };
        ensure!(
            consumer.Value != u64::MAX,
            "open private process-event consumer failed"
        );
        observer.consumer = Some(consumer);
        observer.worker = Some(std::thread::spawn(move || unsafe {
            ProcessTrace(&[consumer], None, None).0
        }));
        unsafe {
            EnableTraceEx2(
                session,
                &PROVIDER,
                EVENT_CONTROL_CODE_ENABLE_PROVIDER.0,
                4,
                0x10,
                0,
                1000,
                None,
            )
        }
        .ok()
        .context("enable kernel process observer")?;
        Ok(observer)
    }
    pub(in crate::account_control) fn events(&self) -> Result<Vec<ProcessEvent>> {
        ensure!(
            self.state.errors.load(Ordering::Relaxed) == 0,
            "kernel observer could not decode an event: {}",
            self.state
                .first_error
                .lock()
                .ok()
                .and_then(|s| s.clone())
                .unwrap_or_else(|| "diagnostic unavailable".into())
        );
        Ok(self
            .state
            .records
            .lock()
            .map_err(|_| anyhow::anyhow!("kernel observer poisoned"))?
            .clone())
    }
    fn stop(&mut self) -> Result<()> {
        if let Some(session) = self.session {
            unsafe {
                ControlTraceW(
                    session,
                    PCWSTR::null(),
                    &mut self.properties.properties,
                    EVENT_TRACE_CONTROL_STOP,
                )
            }
            .ok()
            .context("stop exact private process-event session")?;
            self.session = None;
        }
        if let Some(worker) = self.worker.take() {
            if unsafe { WaitForSingleObject(HANDLE(worker.as_raw_handle()), 5000) } != WAIT_OBJECT_0
            {
                std::process::abort();
            }
            let result = worker
                .join()
                .map_err(|_| anyhow::anyhow!("process-event consumer panicked"))?;
            ensure!(result == 0, "process-event consumer failed ({result})");
        }
        if let Some(consumer) = self.consumer.take() {
            unsafe { CloseTrace(consumer) }.ok()?;
        }
        Ok(())
    }
    pub(in crate::account_control) fn finish(mut self) -> Result<Vec<ProcessEvent>> {
        self.stop()?;
        let p = &self.properties.properties;
        ensure!(
            p.EventsLost == 0 && p.LogBuffersLost == 0 && p.RealTimeBuffersLost == 0,
            "kernel trace lost events or buffers"
        );
        self.events()
    }
}
impl Drop for Observer {
    fn drop(&mut self) {
        if self.stop().is_err() {
            std::process::abort();
        }
    }
}

#[test]
fn kernel_observer_matches_manifest_exit_to_exact_birth_identity() {
    fn event(id: u16, bytes: &mut [u8]) -> EVENT_RECORD {
        let mut event = EVENT_RECORD::default();
        event.EventHeader.Size = size_of::<EVENT_HEADER>() as u16;
        event.EventHeader.ProviderId = PROVIDER;
        event.EventHeader.Flags = EVENT_HEADER_FLAG_64_BIT_HEADER as u16;
        event.EventHeader.EventDescriptor.Id = id;
        event.EventHeader.EventDescriptor.Task = id;
        event.EventHeader.EventDescriptor.Opcode = id as u8;
        event.EventHeader.EventDescriptor.Level = 4;
        event.EventHeader.EventDescriptor.Keyword = 0x8000000000000010;
        event.UserDataLength = bytes.len() as u16;
        event.UserData = bytes.as_mut_ptr().cast();
        event
    }
    fn start(name: &str, pid: u32, created: u64) -> Vec<u8> {
        // Kernel-Process manifest ProcessStart v0: PID, FILETIME,
        // ParentProcessID, SessionID, then a Unicode ImageName.
        let mut bytes = pid.to_le_bytes().to_vec();
        bytes.extend(created.to_le_bytes());
        bytes.extend(123u32.to_le_bytes());
        bytes.extend(1u32.to_le_bytes());
        bytes.extend(wstr(name).iter().flat_map(|w| w.to_le_bytes()));
        bytes
    }
    fn stop(pid: u32, created: u64) -> Vec<u8> {
        // ProcessStop v0 has an ANSI image name; it cannot identify a full
        // path. Deliberately use a shortened name to guard against that bug.
        let mut bytes = pid.to_le_bytes().to_vec();
        bytes.extend(created.to_le_bytes());
        bytes.extend((created + 100).to_le_bytes());
        bytes.extend([0u8; 12]); // exit code, elevation type, handle count
        bytes.extend([0u8; 16]); // commit charge and peak
        bytes.extend(b"never-resumed.e\0");
        bytes
    }
    let state = CallbackState {
        target: "\\Device\\HarddiskVolume3\\Private\\never-resumed.exe".into(),
        records: Mutex::new(Vec::new()),
        errors: AtomicU32::new(0),
        first_error: Mutex::new(None),
    };
    observe(
        &event(
            1,
            &mut start(
                "\\Device\\HarddiskVolume3\\Other\\never-resumed.exe",
                111,
                222,
            ),
        ),
        &state,
    )
    .unwrap();
    observe(&event(2, &mut stop(111, 222)), &state).unwrap();
    assert!(state.records.lock().unwrap().is_empty());
    observe(&event(1, &mut start(&state.target, 111, 222)), &state).unwrap();
    observe(&event(2, &mut stop(111, 223)), &state).unwrap();
    observe(&event(2, &mut stop(112, 222)), &state).unwrap();
    assert_eq!(state.records.lock().unwrap().len(), 1);
    observe(&event(2, &mut stop(111, 222)), &state).unwrap();
    let records = state.records.lock().unwrap();
    assert_eq!(records.len(), 2);
    assert_eq!(
        (records[1].id, records[1].pid, records[1].created),
        (2, 111, 222)
    );
    drop(records);
    // Truncated native payloads remain failures and carry only schema/error
    // context, not the untrusted payload or any unrelated process path.
    let mut malformed = event(1, &mut []);
    malformed.UserContext = (&state as *const CallbackState).cast_mut().cast();
    unsafe { callback(&mut malformed) };
    assert_eq!(state.errors.load(Ordering::Relaxed), 1);
    let error = state.first_error.lock().unwrap().clone().unwrap();
    assert!(error.starts_with("event 1 version 0: start image property:"));
    assert!(!error.contains("never-resumed"));
}

#[test]
#[ignore = "bounded manual private kernel-event observer control; no containment qualification"]
fn bootstrap_secondary_logon_kernel_event_control() {
    let root = crate::util::test_temp_dir().join(format!(
        "logon-events-{}",
        crate::account_state::execution::random_id().unwrap()
    ));
    std::fs::create_dir(&root).unwrap();
    protect(&root).unwrap();
    let path = root.join("never-resumed.exe");
    std::fs::copy(std::env::current_exe().unwrap(), &path).unwrap();
    protect(&path).unwrap();
    let observer = Observer::start(&path).unwrap();
    let child = synthetic_logon(&path, |_| Ok(())).unwrap();
    let pid = unsafe { GetProcessId(child.process()) };
    let mut times = [FILETIME::default(); 4];
    unsafe {
        GetProcessTimes(
            child.process(),
            &mut times[0] as *mut _,
            &mut times[1] as *mut _,
            &mut times[2] as *mut _,
            &mut times[3] as *mut _,
        )
        .unwrap();
    }
    let created = u64::from(times[0].dwLowDateTime) | (u64::from(times[0].dwHighDateTime) << 32);
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline && observer.events().unwrap().is_empty() {
        std::thread::sleep(Duration::from_millis(25));
    }
    let before = observer.events().unwrap();
    drop(child);
    let events = observer.finish().unwrap();
    assert_eq!(
        before
            .iter()
            .filter(|e| e.id == 1 && e.pid == pid && e.created == created)
            .count(),
        1
    );
    assert_eq!(
        events
            .iter()
            .filter(|e| e.id == 2 && e.pid == pid && e.created == created)
            .count(),
        1
    );
    eprintln!(
        "secondary_logon_kernel_event_control={}",
        serde_json::json!({"birth_and_exit_match_original_process":true,"events":events,"lost_events":0,"child_never_resumed":true,"inflight_death_qualified":false,"production_ready":false})
    );
}
