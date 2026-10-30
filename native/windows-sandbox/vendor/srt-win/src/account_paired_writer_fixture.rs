//! Disposable fixed consumer workload. Writes occur only after original-source
//! authentication. A separately prebound fixture selects authenticated descendant
//! file writes. No job or mutation handle is offered; no untrusted execution.
use super::*;
use crate::account_profile_filesystem::{FixedProfileFileReceipt, write_fixed_profile_file};
use crate::account_state::image::{PinnedRunnerImage, account::AccountImageObserver};
use bootstrap::entry::{
    credential_wire::child::{Purpose, probe::Request},
    paired_family,
};
use serde::{Deserialize, Serialize};

#[path = "account_paired_writer_family.rs"]
mod family;

pub(crate) enum Family {
    Idle(paired_family::Family),
    Writing(family::Family),
}
impl Family {
    pub(super) fn describe(&self) -> Result<[Reference; 2]> {
        match self {
            Self::Idle(v) => v.describe(),
            Self::Writing(v) => v.describe(),
        }
    }
    fn verify(&self, verify: impl Fn(HANDLE) -> Result<()>) -> Result<()> {
        match self {
            Self::Idle(v) => v.verify(verify),
            Self::Writing(v) => v.verify(verify),
        }
    }
    fn files(&self) -> Vec<FixedProfileFileReceipt> {
        match self {
            Self::Idle(_) => Vec::new(),
            Self::Writing(v) => v.files(),
        }
    }
}

pub(crate) type Reference = (u64, u32, u64);
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Offer {
    request: Request,
    pub references: [Reference; 2],
    pub file: FixedProfileFileReceipt,
    pub descendant_files: Vec<FixedProfileFileReceipt>,
    registry_written: bool,
}
impl Offer {
    pub(crate) fn fixture_proof(&self, request: &Request) -> Result<serde_json::Value> {
        self.validate(request)?;
        Ok(serde_json::to_value(self)?)
    }
    fn validate(&self, request: &Request) -> Result<()> {
        request.validate()?;
        let Purpose::FixtureProfileWriterV1 { binding } = &request.birth.purpose else {
            anyhow::bail!("fixed writer purpose missing");
        };
        ensure!(
            &self.request == request
                && self.registry_written
                && self.file.depth == 0
                && self.file.file_id != [0; 24]
                && self.file.file_id != binding.folder
                && (1..=128).contains(&self.file.bytes),
            "fixed writer receipt differs"
        );
        for &(handle, pid, created) in &self.references {
            ensure!(
                handle > 0
                    && handle < isize::MAX as u64
                    && pid != 0
                    && pid != request.pid
                    && pid != binding.owner_pid
                    && pid != request.birth.selection.bootstrap_pid
                    && created >= request.created,
                "fixed writer family identity differs"
            );
        }
        ensure!(
            self.references[0].0 != self.references[1].0
                && self.references[0].1 != self.references[1].1,
            "fixed writer family aliases"
        );
        ensure!(
            self.descendant_files.len() == if binding.descendant_files { 2 } else { 0 },
            "fixed descendant file count differs"
        );
        let mut ids = vec![binding.folder, self.file.file_id];
        for (index, file) in self.descendant_files.iter().enumerate() {
            ensure!(
                file.depth == index as u32 + 1
                    && file.file_id != [0; 24]
                    && !ids.contains(&file.file_id)
                    && (1..=128).contains(&file.bytes),
                "fixed descendant file receipt differs or aliases"
            );
            ids.push(file.file_id);
        }
        Ok(())
    }
    pub(crate) fn read(bytes: &[u8], request: &Request) -> Result<Self> {
        const PREFIX: &[u8] = b"alysis-fixed-profile-writer-family-v1:";
        ensure!(
            bytes.len() <= 8192 && bytes.starts_with(PREFIX),
            "writer family frame exceeds bounds or differs"
        );
        let value: Self = serde_json::from_slice(&bytes[PREFIX.len()..])?;
        value.validate(request)?;
        Ok(value)
    }
    fn wire(&self) -> Result<Vec<u8>> {
        self.validate(&self.request)?;
        let mut frame = b"alysis-fixed-profile-writer-family-v1:".to_vec();
        frame.extend_from_slice(&serde_json::to_vec(self)?);
        ensure!(frame.len() <= 8192, "writer family frame exceeds bounds");
        Ok(frame)
    }
}
pub(crate) fn acknowledgment(request: &Request) -> Result<Vec<u8>> {
    ensure!(
        matches!(
            request.birth.purpose,
            Purpose::FixtureProfileWriterV1 { .. }
        ),
        "writer acknowledgment purpose differs"
    );
    let mut frame = b"alysis-fixed-profile-writer-observed-v1:".to_vec();
    frame.extend_from_slice(&request.frame("resume")?);
    Ok(frame)
}

/// Fixed progress of this authenticated trusted fixture. It cannot carry a
/// command or profile effect, and each round is committed in its birth purpose.
pub(crate) fn progress(request: &Request, round: u32, acknowledge: bool) -> Result<Vec<u8>> {
    request.validate()?;
    let Purpose::FixtureProfileWriterV1 { binding } = &request.birth.purpose else {
        anyhow::bail!("writer progress purpose differs")
    };
    ensure!(
        round > 0 && round <= binding.hold_rounds,
        "writer progress round outside fixed bound"
    );
    let mut frame = b"alysis-fixed-writer-progress-v1:".to_vec();
    frame.push(u8::from(acknowledge));
    frame.extend_from_slice(&round.to_le_bytes());
    frame.extend_from_slice(&request.frame("resume")?);
    ensure!(frame.len() <= 8192, "writer progress exceeds bound");
    Ok(frame)
}

/// The only caller is the authenticated handoff client. Environment data alone
/// never reaches this function. Keep Family alive until terminal process::exit.
pub(super) fn client(
    channel: &mut ControlChannel<'_>,
    request: &Request,
    image: &AccountImageObserver,
    phase: &mut u32,
) -> Result<Family> {
    request.validate()?;
    let Purpose::FixtureProfileWriterV1 { binding } = &request.birth.purpose else {
        anyhow::bail!("writer fixture requires fixed purpose");
    };
    channel.peer.verify(channel.handle.raw(), false)?;
    image.verify_current()?;
    let selected = &request.birth.selection;
    *phase = 40;
    let file = write_fixed_profile_file(
        &binding.container,
        &selected.account.account_sid,
        binding.folder,
        &binding.use_nonce,
        0,
    )?;
    *phase = 41;
    crate::account_state::execution::paired_plan::native::account::profile::fixture_write_registry(
        binding,
        &selected.account.account_sid,
    )?;
    *phase = 42;
    let family = if binding.descendant_files {
        Family::Writing(family::start(request, image)?)
    } else {
        Family::Idle(paired_family::start()?)
    };
    family.verify(|process| {
        image.verify_process(process)?;
        let peer = ExpectedPeer::retain(process)?;
        peer.token.require_dedicated_runner(
            &selected.account.account_sid,
            selected.session,
            &selected.account.group_sid,
        )?;
        peer.alive()
    })?;
    *phase = 43;
    let offer = Offer {
        request: request.clone(),
        references: family.describe()?,
        file,
        descendant_files: family.files(),
        registry_written: true,
    };
    channel.send(&offer.wire()?, Duration::from_secs(5))?;
    ensure!(
        channel.receive(Duration::from_secs(5))? == acknowledgment(request)?,
        "writer source observation acknowledgment differs"
    );
    family.describe()?;
    // Keep both account and source receives short. The trusted wrapper reports
    // bounded progress; no canceled/partial read is restarted as a new frame.
    for round in 1..=binding.hold_rounds {
        std::thread::sleep(Duration::from_secs(1));
        image.verify_current()?;
        family.describe()?;
        channel.send(&progress(request, round, false)?, Duration::from_secs(5))?;
        ensure!(
            channel.receive(Duration::from_secs(5))? == progress(request, round, true)?,
            "writer progress acknowledgment differs"
        );
    }
    Ok(family)
}

/// Pull upward from the original account process table. The broker cannot do
/// this directly: its account reference intentionally lacks DUP_HANDLE rights.
pub(super) fn relay(
    broker: &mut ControlChannel<'_>,
    account: &mut ControlChannel<'_>,
    child: &crate::launch::SpawnedChild,
    request: &Request,
    image: &PinnedRunnerImage,
    timeout: Duration,
) -> Result<Vec<OwnedHandle>> {
    let mut offer = Offer::read(&account.receive(timeout)?, request)?;
    let mut originals = Vec::new();
    for reference in &mut offer.references {
        let (source, pid, created) = *reference;
        let mut raw = HANDLE::default();
        unsafe {
            DuplicateHandle(
                child.process(),
                HANDLE(source as usize as *mut _),
                GetCurrentProcess(),
                &mut raw,
                (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
                false,
                DUPLICATE_HANDLE_OPTIONS(0),
            )?;
        }
        let observed = OwnedHandle(raw);
        noninheritable(raw)?;
        let peer = ExpectedPeer::retain(raw)?.with_image(image)?;
        let selection = &request.birth.selection;
        peer.token.require_dedicated_runner(
            &selection.account.account_sid,
            selection.session,
            &selection.account.group_sid,
        )?;
        ensure!(
            peer.pid == pid && bootstrap::pull::creation_time(raw)? == created,
            "writer source original descendant differs"
        );
        peer.alive()?;
        reference.0 = raw.0 as usize as u64;
        originals.push(observed);
    }
    broker.send(&offer.wire()?, timeout)?;
    ensure!(
        broker.receive(timeout)? == acknowledgment(request)?,
        "writer broker observation acknowledgment differs"
    );
    for original in &originals {
        ensure!(
            unsafe { WaitForSingleObject(original.raw(), 0) } == WAIT_TIMEOUT,
            "writer descendant exited before broker observation"
        );
    }
    account.send(&acknowledgment(request)?, timeout)?;
    let Purpose::FixtureProfileWriterV1 { binding } = &request.birth.purpose else {
        unreachable!()
    };
    for round in 1..=binding.hold_rounds {
        let tick = progress(request, round, false)?;
        let ack = progress(request, round, true)?;
        ensure!(
            account.receive(timeout)? == tick,
            "writer original account progress differs"
        );
        broker.send(&tick, timeout)?;
        ensure!(
            broker.receive(timeout)? == ack,
            "writer original broker progress differs"
        );
        for original in &originals {
            ensure!(
                unsafe { WaitForSingleObject(original.raw(), 0) } == WAIT_TIMEOUT,
                "writer descendant ended during live progress"
            );
        }
        account.send(&ack, timeout)?;
    }
    Ok(originals)
}

#[cfg(test)]
thread_local! {pub(crate) static REPORT: std::cell::RefCell<Option<serde_json::Value>> = const {std::cell::RefCell::new(None)};}
thread_local! {
    pub(crate) static FAMILY_OBSERVED: std::cell::RefCell<Option<Box<dyn FnMut(HANDLE)>>> = const {std::cell::RefCell::new(None)};
    pub(crate) static PROGRESS: std::cell::Cell<u32> = const {std::cell::Cell::new(0)};
    pub(crate) static FAMILY_PROGRESS: std::cell::RefCell<Option<Box<dyn FnMut(u32, &rusqlite::Connection, &Request, &Offer, u64)>>> = const {std::cell::RefCell::new(None)};
}
