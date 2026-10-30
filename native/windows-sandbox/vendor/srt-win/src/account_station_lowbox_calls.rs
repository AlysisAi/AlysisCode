//! Test-only, original-thread USER initialization observation. Include as a child
//! module of `account_station_lowbox_loader`; no process is launched here.
//!
//! `module` and `exception` must run while a delivered debug event suspends the
//! original process. On a terminal event OR any error, call `restore` BEFORE
//! ContinueDebugEvent. On a deadline outside an event, or failed restoration,
//! the owner must terminate this original synthetic child, drain/continue its
//! pending debug events, and establish exit before detaching. Do not wait for
//! process exit while withholding a pending event's continuation. There is no
//! Drop restoration: touching a running thread's context would be unsafe.
//! The caller retains both handles, owns the 5s/4096-event deadline, and reserves
//! this evidence's 4096 bytes INSIDE the loader's existing 16384-byte wire limit.
#![cfg(test)]

#[cfg(target_arch = "x86_64")]
pub(super) use amd64::{Evidence, Observer};

#[cfg(target_arch = "x86_64")]
mod amd64 {

    use anyhow::{Context, Result, ensure};
    use serde::{Deserialize, Serialize};
    use std::{marker::PhantomData, rc::Rc};
    use windows::{
        Win32::{
            Foundation::{CompareObjectHandles, HANDLE, WAIT_OBJECT_0},
            System::{
                Diagnostics::Debug::{
                    CONTEXT, CONTEXT_CONTROL_AMD64, CONTEXT_DEBUG_REGISTERS_AMD64,
                    CONTEXT_INTEGER_AMD64, GetThreadContext, ReadProcessMemory, SetThreadContext,
                },
                LibraryLoader::{GetModuleFileNameW, GetModuleHandleW, GetProcAddress},
                Memory::{
                    MEM_COMMIT, MEM_IMAGE, MEMORY_BASIC_INFORMATION, PAGE_EXECUTE,
                    PAGE_EXECUTE_READ, PAGE_EXECUTE_READWRITE, PAGE_EXECUTE_WRITECOPY, PAGE_GUARD,
                    VirtualQueryEx,
                },
                StationsAndDesktops::GetThreadDesktop,
                SystemInformation::GetSystemDirectoryW,
                Threading::{
                    GetCurrentProcess, GetCurrentThreadId, GetProcessId, GetProcessIdOfThread,
                    GetThreadId, WaitForSingleObject,
                },
            },
        },
        core::{PCSTR, PCWSTR},
    };

    const MAX_WIRE: usize = 4096;
    const MAX_CALLS: usize = 12;
    const MAX_DEPTH: usize = 4;
    const BREAKPOINT: u32 = 0x80000003;
    const SINGLE_STEP: u32 = 0x80000004;
    const RF: u32 = 1 << 16;
    const TF: u32 = 1 << 8;
    const DR_STATUS: u64 = 0xe00f; // B0..3, BD, BS, BT (reserved bits excluded).
    const DR_CONTROL: u64 = 0xffff00ff; // Four address enables and RW/LEN fields.

    #[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
    #[serde(rename_all = "snake_case")]
    enum Module {
        Ntdll,
        Gdi32full,
        Win32u,
    }
    impl Module {
        fn from_name(name: &str) -> Option<Self> {
            [Self::Ntdll, Self::Gdi32full, Self::Win32u]
                .into_iter()
                .find(|m| name.eq_ignore_ascii_case(m.name()))
        }
        fn name(self) -> &'static str {
            match self {
                Self::Ntdll => "ntdll.dll",
                Self::Gdi32full => "gdi32full.dll",
                Self::Win32u => "win32u.dll",
            }
        }
    }
    #[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
    enum Call {
        GdiDllInitialize,
        RtlCreateHeap,
        NtGdiInit,
    }
    const CALLS: [(Module, Call, &[u8]); 3] = [
        (
            Module::Gdi32full,
            Call::GdiDllInitialize,
            b"GdiDllInitialize\0",
        ),
        (Module::Ntdll, Call::RtlCreateHeap, b"RtlCreateHeap\0"),
        (Module::Win32u, Call::NtGdiInit, b"NtGdiInit\0"),
    ];
    #[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Location {
        module: Module,
        rva: u32,
    }
    #[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
    #[serde(rename_all = "snake_case")]
    enum Caller {
        Known(Location),
        // Executable MEM_IMAGE address, without attribution to an unverified DLL.
        Unknown(u64),
    }
    #[derive(Clone, Serialize, Deserialize)]
    #[serde(deny_unknown_fields)]
    struct ModuleProof {
        module: Module,
        size: u32,
        file_id: String,
        exports: Vec<(Call, u32)>,
    }
    #[derive(Clone, Serialize, Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Record {
        call: Call,
        entry: Location,
        caller: Caller,
        dll_reason: Option<u32>,
        entered: u8,
        returned: Option<u8>,
        raw_return: Option<u64>,
    }
    #[derive(Clone, Serialize, Deserialize)]
    #[serde(deny_unknown_fields)]
    struct RegisterMismatch {
        expected_address: [u64; 4],
        actual_address: [u64; 4],
        expected_control: u64,
        actual_control: u64,
    }
    #[derive(Clone, Serialize, Deserialize)]
    #[serde(deny_unknown_fields)]
    struct ErrorSnapshot {
        sequence: u8,
        win32: u32,
        ntstatus: u32,
        desktop_matches_original: Option<bool>,
        desktop_query_hresult: Option<i32>,
    }
    #[derive(Clone, Serialize, Deserialize)]
    #[serde(deny_unknown_fields)]
    pub(in super::super) struct Evidence {
        diagnostic_only: bool,
        restored: bool,
        initial_breakpoint: bool,
        modules: Vec<ModuleProof>,
        calls: Vec<Record>,
        register_mismatch: Option<RegisterMismatch>,
        normalized_control: bool,
        nested_return_sites_skipped: u32,
        // Scalars read while stopped, never cleared or modified. A status may
        // predate this call; comparison with its entry is diagnostic only.
        error_layout: Option<[u32; 2]>,
        teb_verified: bool,
        error_snapshots: Vec<ErrorSnapshot>,
    }
    impl Evidence {
        pub(in super::super) fn require(&self) -> Result<()> {
            ensure!(self.diagnostic_only, "call observation is diagnostic only");
            ensure!(
                self.modules.len() <= 3 && self.calls.len() <= MAX_CALLS,
                "call inventory bound"
            );
            ensure!(
                self.nested_return_sites_skipped <= 32,
                "call nested-return bound"
            );
            for (i, module) in self.modules.iter().enumerate() {
                ensure!(
                    (4096..=64 * 1024 * 1024).contains(&module.size)
                        && module.file_id.len() == 48
                        && module.file_id.bytes().all(|b| b.is_ascii_hexdigit())
                        && !self.modules[..i].iter().any(|m| m.module == module.module),
                    "call module identity/range differs"
                );
                let expected = CALLS
                    .iter()
                    .filter(|c| c.0 == module.module)
                    .collect::<Vec<_>>();
                ensure!(
                    module.exports.len() == expected.len(),
                    "call export inventory differs"
                );
                for ((call, rva), expected) in module.exports.iter().zip(expected) {
                    ensure!(
                        *call == expected.1 && *rva < module.size,
                        "call export outside module"
                    );
                }
            }
            let mut sequence = Vec::new();
            for record in &self.calls {
                let slot = CALLS.iter().position(|c| c.1 == record.call).unwrap();
                let module = self
                    .modules
                    .iter()
                    .find(|m| m.module == record.entry.module)
                    .context("call entry module absent")?;
                ensure!(
                    record.entry.module == CALLS[slot].0
                        && module.exports.contains(&(record.call, record.entry.rva))
                        && match record.caller {
                            Caller::Known(c) => self
                                .modules
                                .iter()
                                .any(|m| m.module == c.module && c.rva < m.size),
                            Caller::Unknown(address) => address > 0 && address < (1 << 63),
                        }
                        && record.dll_reason.is_some() == (record.call == Call::GdiDllInitialize)
                        && record.raw_return.is_some() == record.returned.is_some(),
                    "call binding or return differs"
                );
                sequence.push(record.entered);
                if let Some(returned) = record.returned {
                    ensure!(returned > record.entered, "return precedes entry");
                    sequence.push(returned);
                }
            }
            sequence.sort_unstable();
            ensure!(
                sequence.iter().copied().eq(1..=sequence.len() as u8),
                "call sequence differs"
            );
            ensure!(
                self.error_snapshots.len() <= MAX_CALLS * 2,
                "call error-state bound"
            );
            if let Some(offsets) = self.error_layout {
                ensure!(
                    offsets[0] != offsets[1]
                        && offsets
                            .iter()
                            .all(|o| (0x60..0x2000).contains(o) && o % 4 == 0),
                    "call error layout differs"
                );
            }
            for (i, snapshot) in self.error_snapshots.iter().enumerate() {
                ensure!(
                    self.teb_verified
                        && self.error_layout.is_some()
                        && snapshot.desktop_matches_original.is_some()
                            != snapshot.desktop_query_hresult.is_some()
                        && !self.error_snapshots[..i]
                            .iter()
                            .any(|s| s.sequence == snapshot.sequence)
                        && self.calls.iter().any(|r| r.call == Call::NtGdiInit
                            && (r.entered == snapshot.sequence
                                || r.returned == Some(snapshot.sequence))),
                    "call error state lacks original NtGdiInit frame"
                );
            }
            ensure!(
                serde_json::to_vec(self)?.len() <= MAX_WIRE,
                "call wire bound exceeded"
            );
            Ok(())
        }
    }

    #[derive(Clone)]
    struct Frame {
        record: usize,
        return_address: u64,
        return_rsp: u64,
    }
    #[derive(Clone)]
    struct State {
        evidence: Evidence,
        stack: Vec<Frame>,
        sequence: u8,
    }
    impl State {
        fn scoped_entries(&self, entries: [u64; 3]) -> [u64; 3] {
            // Heap creation is frequent during unrelated DLL initialization.
            // Observe helpers only inside the original pending GDI initializer;
            // returning from a nested helper keeps that scope, returning from
            // GDI removes it. No global heap trace or raised event bound.
            if self
                .stack
                .iter()
                .any(|frame| self.evidence.calls[frame.record].call == Call::GdiDllInitialize)
            {
                entries
            } else {
                [entries[0], 0, 0]
            }
        }
        fn new() -> Self {
            Self {
                evidence: Evidence {
                    diagnostic_only: true,
                    restored: true,
                    initial_breakpoint: false,
                    modules: Vec::new(),
                    calls: Vec::new(),
                    register_mismatch: None,
                    normalized_control: false,
                    nested_return_sites_skipped: 0,
                    error_layout: None,
                    teb_verified: false,
                    error_snapshots: Vec::new(),
                },
                stack: Vec::new(),
                sequence: 0,
            }
        }
        fn entry(
            &mut self,
            slot: usize,
            entry: Location,
            caller: Caller,
            ret: u64,
            rsp: u64,
            reason: Option<u32>,
        ) -> Result<()> {
            ensure!(
                slot < 3 && self.stack.len() < MAX_DEPTH && self.evidence.calls.len() < MAX_CALLS,
                "call/depth bound exceeded"
            );
            let return_rsp = rsp.checked_add(8).context("call stack address overflow")?;
            let mut next = self.clone();
            next.sequence += 1;
            next.stack.push(Frame {
                record: next.evidence.calls.len(),
                return_address: ret,
                return_rsp,
            });
            next.evidence.calls.push(Record {
                call: CALLS[slot].1,
                entry,
                caller,
                dll_reason: reason,
                entered: next.sequence,
                returned: None,
                raw_return: None,
            });
            next.evidence.require()?;
            // Reserve room for all pending raw u64 returns and final flags.
            ensure!(
                serde_json::to_vec(&next.evidence)?.len() <= MAX_WIRE - 768,
                "call admission wire bound"
            );
            *self = next;
            Ok(())
        }
        fn nested_return(&mut self, address: u64, rsp: u64) -> Result<bool> {
            let frame = self.stack.last().context("call unpaired return")?;
            ensure!(
                frame.return_address == address,
                "call return address differs"
            );
            if rsp < frame.return_rsp {
                // LdrpCallInitRoutine reuses one return site for nested DLL
                // initialization. A lower stack pointer belongs to a deeper,
                // unobserved call, not the pending targeted initialization.
                ensure!(
                    frame.return_rsp - rsp <= 1024 * 1024
                        && self.evidence.nested_return_sites_skipped < 32,
                    "call nested return exceeds stack/event bound"
                );
                self.evidence.nested_return_sites_skipped += 1;
                return Ok(true);
            }
            ensure!(
                rsp == frame.return_rsp,
                "call return unwound past original frame"
            );
            Ok(false)
        }
        fn returned(&mut self, address: u64, rsp: u64, value: u64) -> Result<()> {
            let frame = self.stack.last().context("call unpaired return")?;
            ensure!(
                frame.return_address == address && frame.return_rsp == rsp,
                "call return frame differs"
            );
            let mut next = self.clone();
            let frame = next.stack.pop().unwrap();
            next.sequence += 1;
            let record = &mut next.evidence.calls[frame.record];
            record.returned = Some(next.sequence);
            record.raw_return = Some(value);
            next.evidence.require()?;
            *self = next;
            Ok(())
        }
    }

    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    struct Registers {
        address: [u64; 4],
        status: u64,
        control: u64,
    }
    impl Registers {
        fn same_configuration(self, other: Self) -> bool {
            self.address == other.address
                && effective_control(self.control) == effective_control(other.control)
        }
        fn read(c: &CONTEXT) -> Self {
            Self {
                address: [c.Dr0, c.Dr1, c.Dr2, c.Dr3],
                status: c.Dr6,
                control: c.Dr7,
            }
        }
        fn write(self, c: &mut CONTEXT) {
            [c.Dr0, c.Dr1, c.Dr2, c.Dr3] = self.address;
            c.Dr6 = self.status;
            c.Dr7 = self.control;
        }
    }
    // Windows documents that it normalizes privileged and global-enabling
    // context bits. Compare enabled slots and exact execution/watchpoint modes,
    // not reserved bits or the local/global representation of an enabled slot.
    // Addresses, RW/LEN fields and General Detect remain checked explicitly.
    fn effective_control(control: u64) -> u64 {
        let mut value = control & (0xffff0000 | (1 << 13));
        for slot in 0..4 {
            if control & (3 << (slot * 2)) != 0 {
                value |= 1 << slot;
            }
        }
        value
    }
    #[repr(C, align(16))]
    struct AlignedContext(CONTEXT);
    fn context(thread: HANDLE) -> Result<AlignedContext> {
        let mut c = AlignedContext(CONTEXT {
            ContextFlags: CONTEXT_CONTROL_AMD64
                | CONTEXT_INTEGER_AMD64
                | CONTEXT_DEBUG_REGISTERS_AMD64,
            ..Default::default()
        });
        unsafe { GetThreadContext(thread, &mut c.0) }
            .context("read original call thread context")?;
        Ok(c)
    }
    fn read(process: HANDLE, address: usize, length: usize) -> Result<Vec<u8>> {
        ensure!(
            length > 0 && length <= 4096 && address.checked_add(length).is_some(),
            "call read bound"
        );
        let mut bytes = vec![0; length];
        let mut actual = 0;
        unsafe {
            ReadProcessMemory(
                process,
                address as *const _,
                bytes.as_mut_ptr().cast(),
                length,
                Some(&mut actual),
            )
        }?;
        ensure!(actual == length, "short call image/frame read");
        Ok(bytes)
    }
    fn u16_at(bytes: &[u8], offset: usize) -> Result<u16> {
        Ok(u16::from_le_bytes(
            bytes
                .get(offset..offset + 2)
                .context("short PE word")?
                .try_into()?,
        ))
    }
    fn u32_at(bytes: &[u8], offset: usize) -> Result<u32> {
        Ok(u32::from_le_bytes(
            bytes
                .get(offset..offset + 4)
                .context("short PE dword")?
                .try_into()?,
        ))
    }
    fn error_offset(bytes: &[u8]) -> Result<u32> {
        // Decode only the two fixed, straight-line AMD64 getter forms. Do not
        // execute a target getter or assume a local OS's TEB field offsets.
        let offset =
            if bytes.get(..4) == Some(&[0x65, 0x8b, 0x04, 0x25]) && bytes.get(8) == Some(&0xc3) {
                u32_at(bytes, 4)?
            } else if bytes.get(..9) == Some(&[0x65, 0x48, 0x8b, 0x04, 0x25, 0x30, 0, 0, 0]) {
                if bytes.get(9..11) == Some(&[0x8b, 0x40]) && bytes.get(12) == Some(&0xc3) {
                    *bytes.get(11).context("call getter short displacement")? as u32
                } else {
                    ensure!(
                        bytes.get(9..11) == Some(&[0x8b, 0x80]) && bytes.get(15) == Some(&0xc3),
                        "call getter encoding differs"
                    );
                    u32_at(bytes, 11)?
                }
            } else {
                anyhow::bail!("call getter encoding differs")
            };
        ensure!(
            (0x60..0x2000).contains(&offset) && offset % 4 == 0,
            "call getter offset outside bound"
        );
        Ok(offset)
    }
    #[derive(Clone)]
    struct Image {
        module: Module,
        base: usize,
        size: u32,
        executable: Vec<(u32, u32)>,
    }
    impl Image {
        fn location(&self, address: u64) -> Option<Location> {
            let rva = address.checked_sub(self.base as u64)?;
            (rva < self.size as u64).then_some(Location {
                module: self.module,
                rva: rva as u32,
            })
        }
        fn code(&self, address: u64, length: u32) -> bool {
            self.location(address).is_some_and(|l| {
                l.rva
                    .checked_add(length)
                    .is_some_and(|end| self.executable.iter().any(|&(a, b)| l.rva >= a && end <= b))
            })
        }
    }
    fn image(module: Module, base: usize, header: &[u8]) -> Result<Image> {
        ensure!(
            header.get(..2) == Some(b"MZ"),
            "call module lacks DOS header"
        );
        let pe = u32_at(header, 0x3c)? as usize;
        ensure!(
            (64..=1024).contains(&pe) && header.get(pe..pe + 4) == Some(b"PE\0\0"),
            "call PE signature differs"
        );
        ensure!(
            u16_at(header, pe + 4)? == 0x8664 && u16_at(header, pe + 24)? == 0x20b,
            "call module is not AMD64 PE32+"
        );
        let sections = u16_at(header, pe + 6)? as usize;
        let optional = u16_at(header, pe + 20)? as usize;
        let size = u32_at(header, pe + 24 + 56)?;
        ensure!(
            (1..=24).contains(&sections)
                && (112..=512).contains(&optional)
                && (4096..=64 * 1024 * 1024).contains(&size),
            "call PE bounds differ"
        );
        ensure!(
            base != 0 && base.checked_add(size as usize).is_some(),
            "call image range overflow"
        );
        let mut executable = Vec::new();
        for index in 0..sections {
            let section = pe + 24 + optional + index * 40;
            if u32_at(header, section + 36)? & 0x20000000 != 0 {
                let start = u32_at(header, section + 12)?;
                let end = start
                    .checked_add(u32_at(header, section + 8)?)
                    .context("call section overflow")?;
                ensure!(
                    start < end && end <= size,
                    "call executable section outside image"
                );
                executable.push((start, end));
            }
        }
        ensure!(!executable.is_empty(), "call image has no code");
        Ok(Image {
            module,
            base,
            size,
            executable,
        })
    }

    fn owned_slot(
        tid: u32,
        original: u32,
        first: bool,
        code: u32,
        address: u64,
        dr6: u64,
        armed: &[u64; 4],
    ) -> Option<usize> {
        if tid != original || !first || code != SINGLE_STEP {
            return None;
        }
        (0..4).find(|&slot| {
            armed[slot] != 0 && address == armed[slot] && dr6 & DR_STATUS == 1 << slot
        })
    }
    fn initial_candidate(
        tid: u32,
        original: u32,
        first: bool,
        code: u32,
        seen: bool,
        calls_started: bool,
        ntdll_code: bool,
    ) -> bool {
        tid == original && first && code == BREAKPOINT && !seen && !calls_started && ntdll_code
    }

    pub(in super::super) struct Observer {
        process: HANDLE,
        thread: HANDLE,
        pid: u32,
        tid: u32,
        debugger_tid: u32,
        images: Vec<Image>,
        entries: [u64; 3],
        original: Option<Registers>,
        programmed: Option<Registers>,
        active: bool,
        finished: bool,
        state: State,
        teb: Option<usize>,
        expected_desktop: HANDLE,
        _thread_affine: PhantomData<Rc<()>>,
    }
    impl Observer {
        pub(in super::super) fn new(
            process: HANDLE,
            thread: HANDLE,
            expected_desktop: HANDLE,
        ) -> Result<Self> {
            let pid = unsafe { GetProcessId(process) };
            let tid = unsafe { GetThreadId(thread) };
            ensure!(
                pid != 0
                    && tid != 0
                    && unsafe { GetProcessIdOfThread(thread) } == pid
                    && !expected_desktop.is_invalid(),
                "call original process/thread differ"
            );
            Ok(Self {
                process,
                thread,
                pid,
                tid,
                debugger_tid: unsafe { GetCurrentThreadId() },
                images: Vec::new(),
                entries: [0; 3],
                original: None,
                programmed: None,
                active: false,
                finished: false,
                state: State::new(),
                teb: None,
                expected_desktop,
                _thread_affine: PhantomData,
            })
        }
        fn identity(&self) -> Result<()> {
            ensure!(
                unsafe { GetCurrentThreadId() } == self.debugger_tid
                    && unsafe { GetProcessId(self.process) } == self.pid
                    && unsafe { GetThreadId(self.thread) } == self.tid
                    && unsafe { GetProcessIdOfThread(self.thread) } == self.pid,
                "call observer original identity/thread differs"
            );
            Ok(())
        }
        pub(in super::super) fn active(&self) -> bool {
            self.active
        }
        pub(in super::super) fn thread_environment(
            &mut self,
            tid: u32,
            address: usize,
        ) -> Result<()> {
            self.identity()?;
            ensure!(
                tid == self.tid && self.teb.is_none() && address != 0 && address % 4096 == 0,
                "call original TEB event differs"
            );
            // Fixed AMD64 NT_TIB.Self and CLIENT_ID only; no whole TEB dump.
            let value = |offset| -> Result<u64> {
                Ok(u64::from_le_bytes(
                    read(
                        self.process,
                        address.checked_add(offset).context("call TEB overflow")?,
                        8,
                    )?
                    .as_slice()
                    .try_into()?,
                ))
            };
            ensure!(
                value(0x30)? == address as u64
                    && value(0x40)? == self.pid as u64
                    && value(0x48)? == self.tid as u64,
                "call original TEB identity differs"
            );
            self.teb = Some(address);
            self.state.evidence.teb_verified = true;
            Ok(())
        }
        fn error_snapshot(&mut self) -> Result<()> {
            let teb = self.teb.context("call original TEB absent")?;
            let offsets = self
                .state
                .evidence
                .error_layout
                .context("call error layout absent")?;
            let value = |offset: u32| -> Result<u32> {
                u32_at(
                    &read(
                        self.process,
                        teb.checked_add(offset as usize)
                            .context("call TEB overflow")?,
                        4,
                    )?,
                    0,
                )
            };
            let win32 = value(offsets[0])?;
            let ntstatus = value(offsets[1])?;
            // This is a borrowed handle returned to the debugger, not a target
            // call or a desktop switch. Never CloseDesktop on this handle.
            let (desktop_matches_original, desktop_query_hresult) =
                match unsafe { GetThreadDesktop(self.tid) } {
                    Ok(desktop) => (
                        Some(unsafe {
                            CompareObjectHandles(HANDLE(desktop.0), self.expected_desktop).as_bool()
                        }),
                        None,
                    ),
                    Err(error) => (None, Some(error.code().0)),
                };
            self.state.evidence.error_snapshots.push(ErrorSnapshot {
                sequence: self.state.sequence,
                win32,
                ntstatus,
                desktop_matches_original,
                desktop_query_hresult,
            });
            self.state.evidence.require()
        }
        pub(in super::super) fn evidence(&self) -> Evidence {
            self.state.evidence.clone()
        }
        fn location(&self, address: u64) -> Result<Location> {
            self.images
                .iter()
                .find(|m| m.code(address, 1))
                .and_then(|m| m.location(address))
                .context("call address outside validated code")
        }
        fn caller(&self, address: u64) -> Result<Caller> {
            if self.images.iter().any(|m| m.location(address).is_some()) {
                return Ok(Caller::Known(self.location(address)?));
            }
            let mut info = MEMORY_BASIC_INFORMATION::default();
            let returned = unsafe {
                VirtualQueryEx(
                    self.process,
                    Some(usize::try_from(address)? as *const _),
                    &mut info,
                    std::mem::size_of::<MEMORY_BASIC_INFORMATION>(),
                )
            };
            let executable =
                PAGE_EXECUTE | PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY;
            ensure!(
                returned == std::mem::size_of_val(&info)
                    && info.State == MEM_COMMIT
                    && info.Type == MEM_IMAGE
                    && info.Protect.0 & executable.0 != 0
                    && info.Protect.0 & PAGE_GUARD.0 == 0,
                "unknown call return is not committed executable image memory"
            );
            Ok(Caller::Unknown(address))
        }
        /// A borrowed LOAD_DLL_DEBUG_EVENT file; this method never closes it.
        pub(in super::super) fn module(
            &mut self,
            file: HANDLE,
            basename: &str,
            remote_base: usize,
        ) -> Result<()> {
            let Some(module) = Module::from_name(basename) else {
                return Ok(());
            };
            self.identity()?;
            ensure!(
                !self.finished
                    && !file.is_invalid()
                    && !self.images.iter().any(|m| m.module == module),
                "call module repeated/closed"
            );
            let wide = module
                .name()
                .encode_utf16()
                .chain(Some(0))
                .collect::<Vec<_>>();
            let parent = unsafe { GetModuleHandleW(PCWSTR(wide.as_ptr())) }?;
            let mut path = vec![0; 32768];
            let n = unsafe { GetModuleFileNameW(Some(parent), &mut path) } as usize;
            ensure!(
                n > 0 && n < path.len(),
                "parent call module path unavailable"
            );
            let path = String::from_utf16(&path[..n])?;
            let mut system = vec![0; 32768];
            let n = unsafe { GetSystemDirectoryW(Some(&mut system)) } as usize;
            ensure!(n > 0 && n < system.len(), "call System32 path unavailable");
            let system_path = format!("{}\\{}", String::from_utf16(&system[..n])?, module.name());
            let id = crate::path_id::file_id_from_handle(file)?;
            ensure!(
                id == crate::path_id::capture_file_id(&path)?
                    && id == crate::path_id::capture_file_id(&system_path)?,
                "call debug file differs from already-loaded parent System32 module"
            );
            let parent_base = parent.0 as usize;
            let header = read(unsafe { GetCurrentProcess() }, parent_base, 4096)?;
            ensure!(
                read(self.process, remote_base, 4096)? == header,
                "call remote PE header differs"
            );
            let local = image(module, parent_base, &header)?;
            let remote = image(module, remote_base, &header)?;
            ensure!(
                !self
                    .images
                    .iter()
                    .any(|m| remote.base < m.base + m.size as usize
                        && m.base < remote.base + remote.size as usize),
                "call module ranges overlap"
            );
            let mut entries = self.entries;
            let mut exports = Vec::new();
            for (slot, &(owner, call, name)) in CALLS.iter().enumerate() {
                if owner != module {
                    continue;
                }
                let proc = unsafe { GetProcAddress(parent, PCSTR(name.as_ptr())) }
                    .context("fixed call export missing")? as usize;
                ensure!(
                    local.code(proc as u64, 32),
                    "call export forwarded/outside validated code"
                );
                let rva = (proc - parent_base) as u32;
                let target = remote_base + rva as usize;
                ensure!(
                    remote.code(target as u64, 32),
                    "call target outside remote code"
                );
                ensure!(
                    read(self.process, target, 32)?
                        == read(unsafe { GetCurrentProcess() }, proc, 32)?,
                    "call remote export code differs"
                );
                ensure!(
                    !entries.contains(&(target as u64)),
                    "call export aliases another target"
                );
                entries[slot] = target as u64;
                exports.push((call, rva));
            }
            let mut next = self.state.evidence.clone();
            if module == Module::Ntdll {
                let mut offsets = [0; 2];
                for (index, name) in [
                    b"RtlGetLastWin32Error\0".as_slice(),
                    b"RtlGetLastNtStatus\0".as_slice(),
                ]
                .iter()
                .enumerate()
                {
                    let proc = unsafe { GetProcAddress(parent, PCSTR(name.as_ptr())) }
                        .context("fixed call getter missing")?
                        as usize;
                    ensure!(
                        local.code(proc as u64, 32),
                        "call getter outside verified NTDLL"
                    );
                    let target = remote_base + (proc - parent_base);
                    ensure!(
                        remote.code(target as u64, 32),
                        "call remote getter outside NTDLL"
                    );
                    let code = read(unsafe { GetCurrentProcess() }, proc, 32)?;
                    ensure!(
                        read(self.process, target, 32)? == code,
                        "call remote getter code differs"
                    );
                    offsets[index] = error_offset(&code)?;
                }
                next.error_layout = Some(offsets);
            }
            next.modules.push(ModuleProof {
                module,
                size: remote.size,
                file_id: id.as_bytes().iter().map(|b| format!("{b:02x}")).collect(),
                exports,
            });
            next.require()?;
            self.images.push(remote);
            self.entries = entries;
            self.state.evidence = next;
            if module != Module::Ntdll && self.state.evidence.initial_breakpoint {
                self.program(false)?;
            }
            Ok(())
        }
        fn program(&mut self, resume: bool) -> Result<()> {
            let mut c = context(self.thread)?;
            let current = Registers::read(&c.0);
            if let Some(expected) = self.programmed {
                self.require_configuration(expected, current)?;
            } else {
                ensure!(
                    current.control & (0xff | (1 << 13)) == 0 && c.0.EFlags & TF == 0,
                    "call thread already debug-stepped/armed"
                );
                self.original = Some(current);
            }
            let mut desired = self.original.context("call original registers absent")?;
            let entries = self.state.scoped_entries(self.entries);
            desired.address = [
                entries[0],
                entries[1],
                entries[2],
                self.state.stack.last().map_or(0, |f| f.return_address),
            ];
            ensure!(
                desired.address[3] == 0 || !self.entries.contains(&desired.address[3]),
                "call return aliases entry breakpoint"
            );
            desired.control &= !DR_CONTROL;
            for (slot, address) in desired.address.iter().enumerate() {
                if *address != 0 {
                    desired.control |= 1 << (slot * 2);
                }
            }
            desired.status = current.status & !DR_STATUS;
            desired.write(&mut c.0);
            c.0.ContextFlags = CONTEXT_DEBUG_REGISTERS_AMD64;
            if resume {
                c.0.EFlags |= RF;
                c.0.ContextFlags |= CONTEXT_CONTROL_AMD64;
            }
            // Set intent BEFORE the fallible write: even partial failure requires
            // restoration or original-child termination by the event-loop owner.
            self.active = true;
            self.state.evidence.restored = false;
            unsafe { SetThreadContext(self.thread, &c.0) }
                .context("program bounded call breakpoints")?;
            let actual = Registers::read(&context(self.thread)?.0);
            self.require_configuration(desired, actual)?;
            self.programmed = Some(actual);
            Ok(())
        }
        fn require_configuration(&mut self, expected: Registers, actual: Registers) -> Result<()> {
            if !expected.same_configuration(actual) {
                self.state.evidence.register_mismatch = Some(RegisterMismatch {
                    expected_address: expected.address,
                    actual_address: actual.address,
                    expected_control: expected.control,
                    actual_control: actual.control,
                });
                anyhow::bail!("call effective debug-register configuration differs");
            }
            self.state.evidence.normalized_control |= expected.control != actual.control;
            Ok(())
        }
        pub(in super::super) fn exception(
            &mut self,
            tid: u32,
            first_chance: bool,
            address: usize,
            code: u32,
        ) -> Result<bool> {
            self.identity()?;
            ensure!(!self.finished, "call observer already restored");
            if tid != self.tid || !first_chance {
                return Ok(false);
            }
            if code == BREAKPOINT {
                let initial = initial_candidate(
                    tid,
                    self.tid,
                    first_chance,
                    code,
                    self.state.evidence.initial_breakpoint,
                    !self.state.evidence.calls.is_empty(),
                    self.images
                        .iter()
                        .any(|m| m.module == Module::Ntdll && m.code(address as u64, 1)),
                );
                if initial && read(self.process, address, 1)? == [0xcc] {
                    self.state.evidence.initial_breakpoint = true;
                    // The loader has established the initial debug stop. Do
                    // not arm hardware probes in its earlier DLL-mapping phase.
                    self.program(false)?;
                    return Ok(true);
                }
                return Ok(false);
            }
            if code != SINGLE_STEP || !self.active {
                return Ok(false);
            }
            let c = context(self.thread)?;
            let expected = self
                .programmed
                .context("call programmed registers absent")?;
            let Some(slot) = owned_slot(
                tid,
                self.tid,
                first_chance,
                code,
                address as u64,
                c.0.Dr6,
                &expected.address,
            ) else {
                return Ok(false);
            };
            ensure!(
                c.0.Rip == address as u64 && c.0.EFlags & TF == 0,
                "call exception instruction/step differs"
            );
            let actual = Registers::read(&c.0);
            self.require_configuration(expected, actual)?;
            if slot == 3 {
                if !self.state.nested_return(address as u64, c.0.Rsp)? {
                    let capture = self.state.stack.last().is_some_and(|f| {
                        self.state.evidence.calls[f.record].call == Call::NtGdiInit
                    });
                    self.state.returned(address as u64, c.0.Rsp, c.0.Rax)?;
                    if capture {
                        self.error_snapshot()?;
                    }
                }
            } else {
                let entry = self.location(address as u64)?;
                let bytes = read(self.process, usize::try_from(c.0.Rsp)?, 8)?;
                let ret = u64::from_le_bytes(bytes.try_into().unwrap());
                let caller = self.caller(ret)?;
                let reason = if slot == 0 {
                    Some(u32::try_from(c.0.Rdx).context("DLL reason exceeds u32")?)
                } else {
                    None
                };
                self.state
                    .entry(slot, entry, caller, ret, c.0.Rsp, reason)?;
                if CALLS[slot].1 == Call::NtGdiInit {
                    self.error_snapshot()?;
                }
            }
            self.program(true)?;
            Ok(true)
        }
        /// Only while a delivered event suspends the process, or after exact exit.
        /// An error leaves active=true: the caller must not detach a live child.
        pub(in super::super) fn restore(&mut self) -> Result<()> {
            self.identity()?;
            if self.active {
                let exited = unsafe { WaitForSingleObject(self.thread, 0) } == WAIT_OBJECT_0
                    || unsafe { WaitForSingleObject(self.process, 0) } == WAIT_OBJECT_0;
                if !exited {
                    let mut c = context(self.thread)?;
                    self.original
                        .context("call restore registers absent")?
                        .write(&mut c.0);
                    c.0.ContextFlags = CONTEXT_DEBUG_REGISTERS_AMD64;
                    unsafe { SetThreadContext(self.thread, &c.0) }
                        .context("restore original call debug registers")?;
                    let actual = Registers::read(&context(self.thread)?.0);
                    let original = self.original.unwrap();
                    self.require_configuration(original, actual)?;
                    ensure!(
                        actual.status & DR_STATUS == original.status & DR_STATUS,
                        "call debug-register status restore did not persist"
                    );
                }
                self.active = false;
            }
            self.finished = true;
            self.state.evidence.restored = true;
            self.state.evidence.require()
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        fn state() -> State {
            let mut state = State::new();
            for module in [Module::Ntdll, Module::Gdi32full, Module::Win32u] {
                state.evidence.modules.push(ModuleProof {
                    module,
                    size: 4096,
                    file_id: "00".repeat(24),
                    exports: CALLS
                        .iter()
                        .enumerate()
                        .filter(|(_, c)| c.0 == module)
                        .map(|(i, c)| (c.1, 100 + i as u32))
                        .collect(),
                });
            }
            state
        }
        fn entry(slot: usize) -> Location {
            Location {
                module: CALLS[slot].0,
                rva: 100 + slot as u32,
            }
        }
        fn caller() -> Caller {
            Caller::Known(Location {
                module: Module::Ntdll,
                rva: 900,
            })
        }
        #[test]
        fn station_calls_decode_only_bounded_straight_line_error_getters() {
            let direct = [0x65, 0x8b, 0x04, 0x25, 0x50, 0x12, 0, 0, 0xc3];
            let indirect = [
                0x65, 0x48, 0x8b, 0x04, 0x25, 0x30, 0, 0, 0, 0x8b, 0x40, 0x68, 0xc3,
            ];
            let wide = [
                0x65, 0x48, 0x8b, 0x04, 0x25, 0x30, 0, 0, 0, 0x8b, 0x80, 0x50, 0x12, 0, 0, 0xc3,
            ];
            assert_eq!(error_offset(&direct).unwrap(), 0x1250);
            assert_eq!(error_offset(&indirect).unwrap(), 0x68);
            assert_eq!(error_offset(&wide).unwrap(), 0x1250);
            for n in 0..direct.len() {
                assert!(error_offset(&direct[..n]).is_err());
            }
            for (index, value) in [(0, 0x64), (8, 0xe9), (5, 0x40), (4, 0x51)] {
                let mut wrong = direct;
                wrong[index] = value;
                assert!(error_offset(&wrong).is_err());
            }
        }
        #[test]
        fn station_calls_error_samples_require_original_frame_layout_and_query_result() {
            let mut s = state();
            s.entry(2, entry(2), caller(), 900, 1000, None).unwrap();
            s.evidence.error_layout = Some([0x68, 0x1250]);
            s.evidence.teb_verified = true;
            s.evidence.error_snapshots.push(ErrorSnapshot {
                sequence: 1,
                win32: 5,
                ntstatus: 0xc0000022,
                desktop_matches_original: Some(true),
                desktop_query_hresult: None,
            });
            s.evidence.require().unwrap();
            s.evidence.error_snapshots[0].sequence = 2;
            assert!(s.evidence.require().is_err());
            s.evidence.error_snapshots[0].sequence = 1;
            s.evidence.error_snapshots[0].desktop_query_hresult = Some(-1);
            assert!(s.evidence.require().is_err());
            s.evidence.error_snapshots[0].desktop_matches_original = None;
            s.evidence.require().unwrap();
            s.evidence.teb_verified = false;
            assert!(s.evidence.require().is_err());
        }
        #[test]
        fn station_calls_helper_probes_are_scoped_to_pending_gdi_initializer() {
            let mut s = state();
            assert_eq!(s.scoped_entries([100, 200, 300]), [100, 0, 0]);
            s.entry(0, entry(0), caller(), 900, 1000, Some(1)).unwrap();
            assert_eq!(s.scoped_entries([100, 200, 300]), [100, 200, 300]);
            s.entry(1, entry(1), Caller::Known(entry(0)), 400, 800, None)
                .unwrap();
            s.returned(400, 808, 1234).unwrap();
            assert_eq!(s.scoped_entries([100, 200, 300]), [100, 200, 300]);
            s.returned(900, 1008, 0xc0000017).unwrap();
            assert_eq!(s.scoped_entries([100, 200, 300]), [100, 0, 0]);
        }
        #[test]
        fn station_calls_pair_nested_returns_and_reject_wrong_frames() {
            let mut s = state();
            s.entry(0, entry(0), caller(), 900, 1000, Some(1)).unwrap();
            s.entry(1, entry(1), Caller::Known(entry(0)), 100, 800, None)
                .unwrap();
            assert!(s.returned(900, 1008, 1).is_err());
            assert!(s.returned(100, 809, 1).is_err());
            assert_eq!(s.stack.len(), 2);
            s.returned(100, 808, 0xc0000022).unwrap();
            s.returned(900, 1008, 0).unwrap();
            assert_eq!(s.evidence.calls[0].raw_return, Some(0));
            assert_eq!(s.evidence.calls[1].raw_return, Some(0xc0000022));
            assert_eq!(s.evidence.calls[1].returned, Some(3));
            s.evidence.require().unwrap();
            assert!(s.returned(900, 1008, 0).is_err());
        }
        #[test]
        fn station_calls_refuse_depth_and_record_overflow_without_partial_admission() {
            let mut s = state();
            for n in 0..MAX_DEPTH {
                s.entry(0, entry(0), caller(), 900, n as u64 * 16, Some(1))
                    .unwrap();
            }
            assert!(s.entry(0, entry(0), caller(), 900, 64, Some(1)).is_err());
            assert_eq!(s.evidence.calls.len(), MAX_DEPTH);
            let mut s = state();
            for _ in 0..MAX_CALLS {
                s.entry(2, entry(2), caller(), 900, 1000, None).unwrap();
                s.returned(900, 1008, u64::MAX).unwrap();
            }
            assert!(s.entry(2, entry(2), caller(), 900, 1000, None).is_err());
            assert_eq!(s.evidence.calls.len(), MAX_CALLS);
            assert!(serde_json::to_vec(&s.evidence).unwrap().len() <= MAX_WIRE);
        }
        #[test]
        fn station_calls_claim_only_exact_original_thread_hardware_events() {
            let targets = [10, 20, 30, 40];
            assert_eq!(
                owned_slot(7, 7, true, SINGLE_STEP, 20, 0xffff0ff2, &targets),
                Some(1)
            );
            for (tid, first, code, addr, dr6) in [
                (8, true, SINGLE_STEP, 20, 2),
                (7, false, SINGLE_STEP, 20, 2),
                (7, true, BREAKPOINT, 20, 2),
                (7, true, SINGLE_STEP, 21, 2),
                (7, true, SINGLE_STEP, 20, 3),
                (7, true, SINGLE_STEP, 20, 0x4002),
            ] {
                assert_eq!(owned_slot(tid, 7, first, code, addr, dr6, &targets), None);
            }
        }
        #[test]
        fn station_calls_wire_rejects_false_authority_unknown_fields_and_invalid_locations() {
            let mut s = state();
            assert!(
                s.entry(
                    0,
                    entry(0),
                    Caller::Known(Location {
                        module: Module::Ntdll,
                        rva: 4096
                    }),
                    900,
                    1000,
                    Some(1)
                )
                .is_err()
            );
            assert!(s.evidence.calls.is_empty());
            s.evidence.diagnostic_only = false;
            assert!(s.evidence.require().is_err());
            s.evidence.diagnostic_only = true;
            let mut value = serde_json::to_value(&s.evidence).unwrap();
            value["permissions_qualified"] = true.into();
            assert!(serde_json::from_value::<Evidence>(value).is_err());
            s.evidence.modules[0].file_id.push('0');
            assert!(s.evidence.require().is_err());
        }
        #[test]
        fn station_calls_initial_breakpoint_is_first_chance_original_ntdll_only() {
            assert!(initial_candidate(
                7, 7, true, BREAKPOINT, false, false, true
            ));
            for (tid, first, code, seen, calls, ntdll) in [
                (8, true, BREAKPOINT, false, false, true),
                (7, false, BREAKPOINT, false, false, true),
                (7, true, SINGLE_STEP, false, false, true),
                (7, true, BREAKPOINT, true, false, true),
                (7, true, BREAKPOINT, false, true, true),
                (7, true, BREAKPOINT, false, false, false),
            ] {
                assert!(!initial_candidate(tid, 7, first, code, seen, calls, ntdll));
            }
        }
        #[test]
        fn station_calls_unknown_caller_keeps_raw_address_without_false_module_attribution() {
            let mut s = state();
            s.entry(
                1,
                entry(1),
                Caller::Unknown(0x700000001234),
                0x700000001234,
                1000,
                None,
            )
            .unwrap();
            s.returned(0x700000001234, 1008, 0).unwrap();
            assert_eq!(s.evidence.calls[0].caller, Caller::Unknown(0x700000001234));
            s.evidence.require().unwrap();
        }
        #[test]
        fn station_calls_debug_context_has_required_alignment_and_preserves_general_registers() {
            assert!(std::mem::align_of::<AlignedContext>() >= 16);
            let mut c = CONTEXT {
                Rax: 1,
                Rsp: 2,
                Rip: 3,
                EFlags: 4,
                ..Default::default()
            };
            let regs = Registers {
                address: [10, 20, 30, 40],
                status: 50,
                control: 60,
            };
            regs.write(&mut c);
            assert_eq!(Registers::read(&c), regs);
            assert_eq!((c.Rax, c.Rsp, c.Rip, c.EFlags), (1, 2, 3, 4));
        }
        #[test]
        fn station_calls_control_normalization_preserves_exact_effective_probes() {
            let expected = Registers {
                address: [10, 20, 0, 0],
                status: 0,
                control: 0x5,
            };
            let mut actual = expected;
            actual.control = 0x40a; // same enabled slots; OS-reserved/local-global representation.
            assert!(expected.same_configuration(actual));
            for control in [0x401, 0x415, 0x10005, 0x2005] {
                actual.control = control; // missing/extra slot, write watchpoint, General Detect.
                assert!(!expected.same_configuration(actual));
            }
            actual.control = expected.control;
            actual.address[1] = 21;
            assert!(!expected.same_configuration(actual));
        }
        #[test]
        fn station_calls_shared_return_site_never_attributes_nested_dll_result() {
            let mut s = state();
            s.entry(0, entry(0), caller(), 900, 1000, Some(1)).unwrap();
            assert!(s.nested_return(900, 800).unwrap());
            assert!(s.evidence.calls[0].raw_return.is_none());
            assert_eq!(s.stack.len(), 1);
            assert_eq!(s.evidence.nested_return_sites_skipped, 1);
            assert!(s.nested_return(901, 800).is_err());
            assert!(s.nested_return(900, 1016).is_err());
            assert!(!s.nested_return(900, 1008).unwrap());
            s.returned(900, 1008, 0).unwrap();
            assert_eq!(s.evidence.calls[0].raw_return, Some(0));
            let mut s = state();
            s.entry(0, entry(0), caller(), 900, 1000, Some(1)).unwrap();
            for _ in 0..32 {
                assert!(s.nested_return(900, 800).unwrap());
            }
            assert!(s.nested_return(900, 800).is_err());
            assert!(s.evidence.calls[0].raw_return.is_none());
            s.evidence.require().unwrap();
        }
    }
}

// Keep other-architecture candidate test builds usable. This diagnostic refuses
// admission there before touching a process, rather than making them fail to build.
#[cfg(not(target_arch = "x86_64"))]
#[derive(Clone, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Evidence;
#[cfg(not(target_arch = "x86_64"))]
impl Evidence {
    pub(super) fn require(&self) -> anyhow::Result<()> {
        anyhow::bail!("fixed USER call observer requires native AMD64")
    }
}
#[cfg(not(target_arch = "x86_64"))]
pub(super) struct Observer;
#[cfg(not(target_arch = "x86_64"))]
impl Observer {
    pub(super) fn new(
        _: windows::Win32::Foundation::HANDLE,
        _: windows::Win32::Foundation::HANDLE,
        _: windows::Win32::Foundation::HANDLE,
    ) -> anyhow::Result<Self> {
        anyhow::bail!("fixed USER call observer requires native AMD64")
    }
    pub(super) fn active(&self) -> bool {
        false
    }
    pub(super) fn thread_environment(&mut self, _: u32, _: usize) -> anyhow::Result<()> {
        anyhow::bail!("fixed USER call observer requires native AMD64")
    }
    pub(super) fn evidence(&self) -> Evidence {
        Evidence
    }
    pub(super) fn module(
        &mut self,
        _: windows::Win32::Foundation::HANDLE,
        _: &str,
        _: usize,
    ) -> anyhow::Result<()> {
        anyhow::bail!("fixed USER call observer requires native AMD64")
    }
    pub(super) fn exception(&mut self, _: u32, _: bool, _: usize, _: u32) -> anyhow::Result<bool> {
        anyhow::bail!("fixed USER call observer requires native AMD64")
    }
    pub(super) fn restore(&mut self) -> anyhow::Result<()> {
        Ok(())
    }
}
