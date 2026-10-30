//! Fixed test-only descendant writes. Environment is handoff data, never effect
//! authority. Original peer authentication precedes the exact bounded request.
use super::*;
use crate::{launch::SpawnedChild, process_attributes::ProcThreadAttrs};
use windows::Win32::System::JobObjects::IsProcessInJob;

const TEST: &str =
    "account_control::writer_fixture::family::tests::fixed_profile_writer_descendant_entry";
const ENV: &str = "ALYSIS_TEST_PROFILE_DESCENDANT";
const TIMEOUT: Duration = Duration::from_secs(5);
const LIMIT: usize = 8192;

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Handoff {
    request: Request,
    depth: u32,
    parent: u64,
    consumer: u64,
    address: ControlAddress,
}
impl Handoff {
    fn validate(&self) -> Result<()> {
        require_request(&self.request, self.depth)?;
        self.address.binding_nonce()?;
        ensure!(
            self.parent != self.consumer,
            "descendant reference slots alias"
        );
        for value in [self.parent, self.consumer] {
            ensure!(
                value > 0 && value < isize::MAX as u64,
                "invalid descendant reference"
            );
        }
        Ok(())
    }
    fn read(bytes: &[u8]) -> Result<Self> {
        ensure!(bytes.len() <= LIMIT, "descendant handoff exceeds bound");
        let value: Self = serde_json::from_slice(bytes)?;
        value.validate()?;
        Ok(value)
    }
}
fn require_request(request: &Request, depth: u32) -> Result<()> {
    request.validate()?;
    let Purpose::FixtureProfileWriterV1 { binding } = &request.birth.purpose else {
        anyhow::bail!("descendant requires fixed profile writer")
    };
    ensure!(
        binding.descendant_files && matches!(depth, 1 | 2),
        "descendant write was not bound before birth"
    );
    Ok(())
}
fn frame(request: &Request, depth: u32, phase: &str) -> Result<Vec<u8>> {
    require_request(request, depth)?;
    ensure!(
        matches!(phase, "write" | "hold"),
        "invalid descendant phase"
    );
    let bytes = serde_json::to_vec(&("alysis-fixed-descendant-v1", request, depth, phase))?;
    ensure!(bytes.len() <= LIMIT, "descendant request exceeds bound");
    Ok(bytes)
}
fn file_receipt(file: &FixedProfileFileReceipt, request: &Request, depth: u32) -> Result<()> {
    require_request(request, depth)?;
    let Purpose::FixtureProfileWriterV1 { binding } = &request.birth.purpose else {
        unreachable!()
    };
    ensure!(
        file.depth == depth
            && file.file_id != [0; 24]
            && file.file_id != binding.folder
            && (1..=128).contains(&file.bytes),
        "descendant file receipt differs"
    );
    Ok(())
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Receipt {
    request: Request,
    depth: u32,
    file: FixedProfileFileReceipt,
    grandchild: Option<(Reference, FixedProfileFileReceipt)>,
}
impl Receipt {
    fn validate(&self, request: &Request, depth: u32) -> Result<()> {
        ensure!(
            &self.request == request && self.depth == depth,
            "descendant receipt subject differs"
        );
        file_receipt(&self.file, request, depth)?;
        ensure!(
            self.grandchild.is_some() == (depth == 1),
            "descendant tree shape differs"
        );
        if let Some(((raw, pid, created), file)) = &self.grandchild {
            ensure!(
                *raw > 0
                    && *raw < isize::MAX as u64
                    && *pid != 0
                    && *pid != request.pid
                    && *created >= request.created,
                "grandchild reference differs"
            );
            file_receipt(file, request, 2)?;
            ensure!(file.file_id != self.file.file_id, "descendant files alias");
        }
        Ok(())
    }
    fn wire(&self) -> Result<Vec<u8>> {
        self.validate(&self.request, self.depth)?;
        let bytes = serde_json::to_vec(self)?;
        ensure!(bytes.len() <= LIMIT, "descendant receipt exceeds bound");
        Ok(bytes)
    }
    fn read(bytes: &[u8], request: &Request, depth: u32) -> Result<Self> {
        ensure!(bytes.len() <= LIMIT, "descendant receipt exceeds bound");
        let value: Self = serde_json::from_slice(bytes)?;
        value.validate(request, depth)?;
        Ok(value)
    }
}

fn duplicate(source: HANDLE, process: HANDLE, inherit: bool) -> Result<OwnedHandle> {
    let mut raw = HANDLE::default();
    unsafe {
        DuplicateHandle(
            source,
            process,
            GetCurrentProcess(),
            &mut raw,
            (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
            inherit,
            DUPLICATE_HANDLE_OPTIONS(0),
        )?;
    }
    Ok(OwnedHandle(raw))
}
fn describe(process: HANDLE) -> Result<Reference> {
    noninheritable(process)?;
    ensure!(
        unsafe { WaitForSingleObject(process, 0) } == WAIT_TIMEOUT,
        "descendant already exited"
    );
    Ok((
        process.0 as usize as u64,
        unsafe { GetProcessId(process) },
        bootstrap::pull::creation_time(process)?,
    ))
}
fn member(process: HANDLE) -> Result<()> {
    let mut member = windows::core::BOOL::default();
    unsafe {
        IsProcessInJob(process, None, &mut member)?;
    }
    ensure!(member.as_bool(), "fixed descendant has no containing job");
    Ok(())
}
fn actor<'a>(
    process: HANDLE,
    request: &Request,
    image: &'a AccountImageObserver,
) -> Result<ExpectedPeer<'a>> {
    let mut peer = ExpectedPeer::retain(process)?;
    let selected = &request.birth.selection;
    peer.require_profile_runner(
        &selected.account.account_sid,
        selected.session,
        &selected.account.group_sid,
    )?;
    peer.account_image = Some(image);
    peer.alive()?;
    member(process)?;
    Ok(peer)
}
fn require_chain(
    request: &Request,
    depth: u32,
    parent: Reference,
    consumer: Reference,
) -> Result<()> {
    require_request(request, depth)?;
    ensure!(
        consumer.1 == request.pid && consumer.2 == request.created,
        "descendant original consumer differs"
    );
    ensure!(
        if depth == 1 {
            (parent.1, parent.2) == (consumer.1, consumer.2)
        } else {
            parent.1 != consumer.1 && parent.2 >= consumer.2
        },
        "descendant original parent chain differs"
    );
    Ok(())
}

/// Same-account fixture pipe only. Production ControlServer admission remains
/// distinct-account. Original process identity is checked on every transfer.
fn prepare_pipe(image: &AccountImageObserver) -> Result<(ControlServer, ControlAddress)> {
    image.verify_current()?;
    let address = ControlAddress::fresh()?;
    let owner = crate::sid::current_user_sid()?;
    let descriptor = OwnedSd::from_sddl(&format!(
        "O:{owner}D:P(A;;GA;;;SY)(A;;GA;;;{owner})(A;;RC;;;OW)S:(ML;;NWNR;;;ME)"
    ))?;
    let security = SECURITY_ATTRIBUTES {
        nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: descriptor.ptr.0,
        bInheritHandle: false.into(),
    };
    let raw = unsafe {
        CreateNamedPipeW(
            pcwstr(&wstr(&address.path()?)),
            PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE | FILE_FLAG_OVERLAPPED,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            1,
            MAX_FRAME as u32 + HEADER as u32,
            MAX_FRAME as u32 + HEADER as u32,
            0,
            Some(&security),
        )
    };
    ensure!(!raw.is_invalid(), "fixed descendant pipe creation failed");
    let handle = OwnedHandle(raw);
    noninheritable(handle.raw())?;
    Ok((
        ControlServer {
            handle,
            nonce: address.nonce,
        },
        address,
    ))
}

struct Started {
    _child: SpawnedChild,
    _terminal_pipe: OwnedHandle,
    reference: OwnedHandle,
    file: FixedProfileFileReceipt,
    grandchild: Option<(OwnedHandle, FixedProfileFileReceipt)>,
}
fn start_child(
    request: &Request,
    depth: u32,
    consumer: HANDLE,
    image: &AccountImageObserver,
) -> Result<Started> {
    require_request(request, depth)?;
    image.verify_current()?;
    actor(consumer, request, image)?;
    let here = duplicate(
        unsafe { GetCurrentProcess() },
        unsafe { GetCurrentProcess() },
        false,
    )?;
    require_chain(request, depth, describe(here.raw())?, describe(consumer)?)?;
    let (server, address) = prepare_pipe(image)?;
    let parent = duplicate(unsafe { GetCurrentProcess() }, here.raw(), true)?;
    let original = duplicate(unsafe { GetCurrentProcess() }, consumer, true)?;
    let handoff = Handoff {
        request: request.clone(),
        depth,
        parent: parent.raw().0 as usize as u64,
        consumer: original.raw().0 as usize as u64,
        address,
    };
    handoff.validate()?;
    let handoff = serde_json::to_string(&handoff)?;
    ensure!(handoff.len() <= LIMIT, "descendant handoff exceeds bound");
    let path = std::env::current_exe()?;
    let path = path
        .to_str()
        .context("fixed descendant image not Unicode")?;
    let mut command = wstr(&format!(
        "{} --exact {TEST} --ignored --nocapture --test-threads=1",
        crate::launch::quote_arg(path)
    ));
    let app = wstr(path);
    let directory = bootstrap::os_directory(false)?;
    let windows = bootstrap::os_directory(true)?;
    let mut environment = crate::launch::build_env_block_with_inheritance(
        &[
            ("SystemRoot".into(), windows.clone()),
            ("WINDIR".into(), windows),
            ("PATH".into(), directory.clone()),
            (ENV.into(), handoff),
        ],
        false,
    );
    let mut attributes = ProcThreadAttrs::new(1)?;
    attributes.set_handle_list(&[parent.raw(), original.raw()])?;
    let mut startup = STARTUPINFOEXW::default();
    startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
    startup.lpAttributeList = attributes.list();
    let mut info = PROCESS_INFORMATION::default();
    image.verify_current()?;
    member(unsafe { GetCurrentProcess() })?;
    // No breakaway flag and no job handle transfer. Inherit the existing strict
    // consumer job atomically; the broker later checks its exact original job.
    unsafe {
        CreateProcessW(
            pcwstr(&app),
            Some(windows::core::PWSTR(command.as_mut_ptr())),
            None,
            None,
            true,
            CREATE_SUSPENDED
                | CREATE_NO_WINDOW
                | CREATE_UNICODE_ENVIRONMENT
                | EXTENDED_STARTUPINFO_PRESENT,
            Some(environment.as_mut_ptr().cast()),
            pcwstr(&wstr(&directory)),
            &startup.StartupInfo,
            &mut info,
        )?;
    }
    let child = SpawnedChild::new(info);
    drop(parent);
    drop(original);
    let peer = actor(child.process(), request, image)?;
    let reference = duplicate(unsafe { GetCurrentProcess() }, child.process(), false)?;
    ensure!(
        describe(reference.raw())?.2 >= bootstrap::pull::creation_time(here.raw())?,
        "descendant creation predates original parent"
    );
    ensure!(
        unsafe { ResumeThread(child.thread()) } == 1,
        "fixed descendant resume state differs"
    );
    let mut channel = server.accept(peer, TIMEOUT)?;
    channel.send(&frame(request, depth, "write")?, TIMEOUT)?;
    let receipt = Receipt::read(&channel.receive(TIMEOUT)?, request, depth)?;
    let grandchild = if let Some(((raw, pid, created), file)) = receipt.grandchild {
        let observed = duplicate(child.process(), HANDLE(raw as usize as *mut _), false)?;
        actor(observed.raw(), request, image)?;
        let actual = describe(observed.raw())?;
        ensure!(
            (actual.1, actual.2) == (pid, created)
                && pid != describe(reference.raw())?.1
                && created >= describe(reference.raw())?.2,
            "original grandchild offer differs"
        );
        Some((observed, file))
    } else {
        None
    };
    channel.send(&frame(request, depth, "hold")?, TIMEOUT)?;
    Ok(Started {
        _child: child,
        // Retain this terminal endpoint so its close cannot race the child's
        // final receive postcheck. No further operations use this pipe.
        _terminal_pipe: channel.handle,
        reference,
        file: receipt.file,
        grandchild,
    })
}

pub(crate) struct Family {
    _started: Started,
}
impl Family {
    pub(super) fn describe(&self) -> Result<[Reference; 2]> {
        Ok([
            describe(self._started.reference.raw())?,
            describe(
                self._started
                    .grandchild
                    .as_ref()
                    .context("original grandchild absent")?
                    .0
                    .raw(),
            )?,
        ])
    }
    pub(super) fn verify(&self, verify: impl Fn(HANDLE) -> Result<()>) -> Result<()> {
        verify(self._started.reference.raw())?;
        verify(
            self._started
                .grandchild
                .as_ref()
                .context("original grandchild absent")?
                .0
                .raw(),
        )?;
        let references = self.describe()?;
        ensure!(
            references[0].1 != references[1].1,
            "writer descendants alias"
        );
        Ok(())
    }
    pub(super) fn files(&self) -> Vec<FixedProfileFileReceipt> {
        vec![
            self._started.file.clone(),
            self._started.grandchild.as_ref().unwrap().1.clone(),
        ]
    }
}
pub(super) fn start(request: &Request, image: &AccountImageObserver) -> Result<Family> {
    let consumer = duplicate(
        unsafe { GetCurrentProcess() },
        unsafe { GetCurrentProcess() },
        false,
    )?;
    Ok(Family {
        _started: start_child(request, 1, consumer.raw(), image)?,
    })
}

fn inherited(value: u64) -> Result<OwnedHandle> {
    ensure!(
        value > 0 && value < isize::MAX as u64,
        "invalid descendant inherited reference"
    );
    let raw = HANDLE(value as usize as *mut _);
    let mut flags = 0;
    unsafe {
        GetHandleInformation(raw, &mut flags)?;
    }
    ensure!(
        flags & HANDLE_FLAG_INHERIT.0 != 0,
        "descendant reference was not inherited"
    );
    let retained = duplicate(unsafe { GetCurrentProcess() }, raw, false)?;
    drop(OwnedHandle(raw));
    Ok(retained)
}
fn descendant(phase: &mut u32) -> Result<()> {
    ensure!(
        std::env::args().skip(1).collect::<Vec<_>>()
            == [
                "--exact",
                TEST,
                "--ignored",
                "--nocapture",
                "--test-threads=1"
            ],
        "descendant arguments differ"
    );
    let bytes = std::env::var(ENV)?;
    let handoff = Handoff::read(bytes.as_bytes())?;
    *phase = 1;
    let parent = inherited(handoff.parent)?;
    let consumer = inherited(handoff.consumer)?;
    let request = &handoff.request;
    require_chain(
        request,
        handoff.depth,
        describe(parent.raw())?,
        describe(consumer.raw())?,
    )?;
    ensure!(
        std::process::id() != request.pid && std::process::id() != describe(parent.raw())?.1,
        "descendant process aliases ancestor"
    );
    let selection = &request.birth.selection;
    let image =
        AccountImageObserver::prepare(&selection.account, selection.session, selection.image_id)?;
    actor(unsafe { GetCurrentProcess() }, request, &image)?;
    actor(consumer.raw(), request, &image)?;
    let peer = actor(parent.raw(), request, &image)?;
    *phase = 2;
    let mut channel = ControlChannel::connect(&handoff.address, peer)?;
    ensure!(
        channel.receive(TIMEOUT)? == frame(request, handoff.depth, "write")?,
        "descendant authenticated request differs"
    );
    *phase = 3;
    image.verify_current()?;
    require_chain(
        request,
        handoff.depth,
        describe(parent.raw())?,
        describe(consumer.raw())?,
    )?;
    channel.peer.verify(channel.handle.raw(), false)?;
    let Purpose::FixtureProfileWriterV1 { binding } = &request.birth.purpose else {
        unreachable!()
    };
    let file = write_fixed_profile_file(
        &binding.container,
        &selection.account.account_sid,
        binding.folder,
        &binding.use_nonce,
        handoff.depth,
    )?;
    *phase = 4;
    let grandchild = if handoff.depth == 1 {
        Some(start_child(request, 2, consumer.raw(), &image)?)
    } else {
        None
    };
    let receipt = Receipt {
        request: request.clone(),
        depth: handoff.depth,
        file,
        grandchild: grandchild
            .as_ref()
            .map(|c| -> Result<_> { Ok((describe(c.reference.raw())?, c.file.clone())) })
            .transpose()?,
    };
    *phase = 5;
    channel.send(&receipt.wire()?, TIMEOUT)?;
    ensure!(
        channel.receive(TIMEOUT)? == frame(request, handoff.depth, "hold")?,
        "descendant final acknowledgment differs"
    );
    // Parent/root intentionally exit before whole-job retirement. No channel
    // receives or parent-liveness checks occur after this final acknowledgment.
    // Both endpoints stay open: closing here could race the original parent's
    // final send postcheck. Retaining alone performs no parent-liveness check.
    let _terminal_channel = channel;
    std::thread::sleep(Duration::from_secs(30));
    std::process::exit(91);
}

#[cfg(test)]
#[path = "account_paired_writer_family_tests.rs"]
mod tests;
