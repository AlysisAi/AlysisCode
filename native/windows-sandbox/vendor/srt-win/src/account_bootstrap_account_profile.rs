//! Fixed unused account-profile round trip. The original account helper keeps
//! every native guard; no consumer, command, handle export or cold retry exists.
//! Its broker commits each intent/receipt before permitting the next effect.
use super::*;
use crate::account_control::{ControlChannel, bootstrap::pull::RunningSelfCheck};
use crate::account_state::execution::resources::NativeHostProfile;

const TABLE: &str = "candidate_atomic_account_profile";
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_atomic_account_profile(sequence INTEGER PRIMARY KEY CHECK(sequence BETWEEN 0 AND 3), event_json TEXT NOT NULL)",
    "CREATE TRIGGER atomic_account_profile_no_update BEFORE UPDATE ON candidate_atomic_account_profile BEGIN SELECT RAISE(ABORT,'account profile event is immutable'); END",
    "CREATE TRIGGER atomic_account_profile_no_delete BEFORE DELETE ON candidate_atomic_account_profile BEGIN SELECT RAISE(ABORT,'account profile event is immutable'); END",
    "CREATE TRIGGER atomic_account_profile_no_replace BEFORE INSERT ON candidate_atomic_account_profile WHEN EXISTS(SELECT 1 FROM candidate_atomic_account_profile WHERE sequence=NEW.sequence) BEGIN SELECT RAISE(ABORT,'account profile event is immutable'); END",
];
pub(super) fn create_schema(sql: &Connection) -> Result<()> {
    for s in SCHEMA {
        sql.execute_batch(s)?;
    }
    Ok(())
}
pub(super) fn require_schema(sql: &Connection) -> Result<()> {
    let mut s = sql.prepare("SELECT sql FROM sqlite_schema WHERE tbl_name=?1")?;
    let rows: Vec<String> = s
        .query_map([TABLE], |r| r.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    ensure!(
        rows.len() == SCHEMA.len() && SCHEMA.iter().all(|s| rows.iter().any(|r| r == s)),
        "account profile schema differs; preserve state"
    );
    Ok(())
}

#[derive(Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Phase {
    Create,
    Created,
    Remove,
    Removed,
}
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Event {
    request: probe::Request,
    phase: Phase,
    folder: Option<[u8; 24]>,
}
fn subject(request: &probe::Request) -> Result<&str> {
    request.validate()?;
    let Purpose::TrustedUnusedProfileV1 { container, .. } = &request.birth.purpose else {
        anyhow::bail!("account profile requires fixed unused purpose")
    };
    Ok(container)
}
impl Event {
    fn wire(&self) -> Result<Vec<u8>> {
        subject(&self.request)?;
        ensure!(
            match self.phase {
                Phase::Create => self.folder.is_none(),
                _ => self.folder.is_some_and(|f| f != [0; 24]),
            },
            "account profile folder identity differs"
        );
        let mut bytes = b"alysis-unused-account-profile-v1:".to_vec();
        bytes.extend_from_slice(&serde_json::to_vec(self)?);
        ensure!(bytes.len() <= 8192, "account profile frame exceeds bound");
        Ok(bytes)
    }
    fn receive(
        bytes: &[u8],
        request: &probe::Request,
        phase: Phase,
        folder: Option<[u8; 24]>,
    ) -> Result<Self> {
        const PREFIX: &[u8] = b"alysis-unused-account-profile-v1:";
        ensure!(
            bytes.len() <= 8192 && bytes.starts_with(PREFIX),
            "invalid account profile frame"
        );
        let event: Self = serde_json::from_slice(&bytes[PREFIX.len()..])
            .map_err(|_| anyhow::anyhow!("malformed account profile frame"))?;
        event.wire()?;
        ensure!(
            &event.request == request
                && event.phase == phase
                && (phase == Phase::Created || event.folder == folder),
            "account profile frame binding differs"
        );
        Ok(event)
    }
}
fn validate(sql: &Connection, plan: &Plan, events: &[Event]) -> Result<()> {
    ensure!(events.len() <= 4, "account profile event bound exceeded");
    let Some(first) = events.first() else {
        return Ok(());
    };
    subject(&first.request)?;
    let resume = super::replay(sql, plan)?;
    ensure!(
        resume.len() >= 2
            && resume[0]
                == super::Event::Intent {
                    request: first.request.clone()
                }
            && resume[1]
                == super::Event::Resumed {
                    nonce: first.request.nonce.clone()
                },
        "account profile requires original durable resumed helper"
    );
    for (i, event) in events.iter().enumerate() {
        event.wire()?;
        ensure!(
            event.request == first.request
                && event.phase == [Phase::Create, Phase::Created, Phase::Remove, Phase::Removed][i]
                && (i < 2 || event.folder == events[1].folder),
            "account profile sequence or original storage differs"
        );
    }
    Ok(())
}
fn replay(sql: &Connection, plan: &Plan) -> Result<Vec<Event>> {
    require_plan(sql, plan)?;
    let (count,max):(usize,Option<usize>) = sql.query_row(
        "SELECT count(*),max(length(CAST(event_json AS BLOB))) FROM candidate_atomic_account_profile", [], |r| Ok((r.get(0)?,r.get(1)?)))?;
    ensure!(
        count <= 4 && max.unwrap_or(0) <= 8192,
        "account profile inventory exceeds bounds"
    );
    let mut s = sql.prepare(
        "SELECT sequence,event_json FROM candidate_atomic_account_profile ORDER BY sequence",
    )?;
    let mut events = Vec::new();
    for row in s.query_map([], |r| Ok((r.get::<_, usize>(0)?, r.get::<_, String>(1)?)))? {
        let (seq, json) = row?;
        ensure!(seq == events.len(), "account profile sequence gap");
        events.push(
            serde_json::from_str(&json)
                .map_err(|_| anyhow::anyhow!("malformed account profile event"))?,
        );
    }
    validate(sql, plan, &events)?;
    Ok(events)
}
fn append(sql: &Connection, plan: &Plan, event: Event) -> Result<()> {
    let mut expected = replay(sql, plan)?;
    let sequence = expected.len();
    expected.push(event.clone());
    validate(sql, plan, &expected)?;
    let bytes = serde_json::to_string(&event)?;
    ensure!(bytes.len() <= 8192, "account profile event exceeds bounds");
    let tx = sql.unchecked_transaction()?;
    tx.execute(
        "INSERT INTO candidate_atomic_account_profile VALUES(?1,?2)",
        params![sequence, bytes],
    )?;
    #[cfg(test)]
    checkpoint("inserted", sequence, &tx);
    tx.commit()
        .context("account profile commit uncertain; preserve pending state")?;
    #[cfg(test)]
    checkpoint("committed", sequence, sql);
    ensure!(
        replay(sql, plan)? == expected,
        "account profile readback differs"
    );
    Ok(())
}
#[cfg(test)]
thread_local! { pub(crate) static ACCOUNT_PROFILE_CHECKPOINT: std::cell::RefCell<Option<Box<dyn FnMut(&str,usize,&Connection)>>> = const {std::cell::RefCell::new(None)}; }
#[cfg(test)]
fn checkpoint(phase: &str, seq: usize, sql: &Connection) {
    ACCOUNT_PROFILE_CHECKPOINT.with(|s| {
        if let Some(h) = s.borrow_mut().as_mut() {
            h(phase, seq, sql);
        }
    });
}

/// Nonserializable one-shot permission to forward exactly the journaled step.
/// Construction is private; copied journal rows cannot recover this live owner.
pub(crate) struct StepIntent<'j, 'p, 'a, 'l> {
    owner: &'j JournaledJob<'p, 'a, 'l>,
    event: Event,
    attempted: bool,
}
impl StepIntent<'_, '_, '_, '_> {
    pub(crate) fn consume(&mut self, request: &probe::Request) -> Result<Vec<u8>> {
        ensure!(
            !std::mem::replace(&mut self.attempted, true),
            "account profile step already attempted"
        );
        self.owner.verify()?;
        let sql = self.owner.plan.database.connection()?;
        let events = replay(sql, &self.owner.plan.plan)?;
        let length = match self.event.phase {
            Phase::Create => 1,
            Phase::Remove => 3,
            Phase::Removed => 4,
            _ => anyhow::bail!("invalid profile intent"),
        };
        ensure!(
            &self.event.request == request
                && events.len() == length
                && events.last() == Some(&self.event),
            "account profile original intent changed"
        );
        self.event.wire()
    }
}
pub(super) fn run(
    job: &JournaledJob<'_, '_, '_>,
    request: &probe::Request,
    running: &mut RunningSelfCheck<'_>,
    timeout: Duration,
) -> Result<()> {
    subject(request)?;
    job.verify()?;
    let sql = job.plan.database.connection()?;
    ensure!(
        replay(sql, &job.plan.plan)?.is_empty(),
        "account profile previously attempted"
    );
    let mut folder = None;
    for (phase, reply) in [
        (Phase::Create, Phase::Created),
        (Phase::Remove, Phase::Removed),
    ] {
        let event = Event {
            request: request.clone(),
            phase,
            folder,
        };
        append(sql, &job.plan.plan, event.clone())?;
        let mut intent = StepIntent {
            owner: job,
            event,
            attempted: false,
        };
        let bytes = running.profile_step(&mut intent, timeout)?;
        let receipt = Event::receive(&bytes, request, reply, folder)?;
        job.verify()?;
        folder = receipt.folder;
        append(sql, &job.plan.plan, receipt)?;
    }
    // Release only after the removal receipt has committed and read back. The
    // account must remain live through the ordinary receipt peer checks above.
    let mut intent = StepIntent {
        owner: job,
        event: Event {
            request: request.clone(),
            phase: Phase::Removed,
            folder,
        },
        attempted: false,
    };
    running.complete_profile(&mut intent, timeout)?;
    Ok(())
}

/// Source relay admits only these two fixed operations, in this order. Both
/// pipes continuously verify their original peer and image on every message.
pub(crate) fn relay(
    broker: &mut ControlChannel<'_>,
    account: &mut ControlChannel<'_>,
    request: &probe::Request,
    timeout: Duration,
) -> Result<()> {
    subject(request)?;
    let mut folder = None;
    for (phase, reply) in [
        (Phase::Create, Phase::Created),
        (Phase::Remove, Phase::Removed),
    ] {
        let event = Event::receive(&broker.receive(timeout)?, request, phase, folder)?;
        account.send(&event.wire()?, timeout)?;
        let receipt = Event::receive(&account.receive(timeout)?, request, reply, folder)?;
        folder = receipt.folder;
        broker.send(&receipt.wire()?, timeout)?;
    }
    Event::receive(&broker.receive(timeout)?, request, Phase::Removed, folder)?;
    Ok(())
}

/// Called only after promotion of the authenticated original-source pipe.
/// No profile use is admitted: the same helper owns creation through deletion.
/// On uncertainty Drop closes handles only; no guessed SDK deletion is allowed.
pub(crate) fn client(
    channel: &mut ControlChannel<'_>,
    request: &probe::Request,
    diagnostic_phase: &mut u32,
) -> Result<()> {
    let container = subject(request)?;
    let selected = &request.birth.selection;
    let verify = || -> Result<()> {
        crate::account_impersonation::require_process_identity()?;
        let peer = crate::account_control::ExpectedPeer::retain(unsafe {
            windows::Win32::System::Threading::GetCurrentProcess()
        })?;
        peer.require_profile_runner(
            &selected.account.account_sid,
            selected.session,
            &selected.account.group_sid,
        )?;
        ensure!(
            std::process::id() == request.pid
                && process_created(unsafe {
                    windows::Win32::System::Threading::GetCurrentProcess()
                })? == request.created,
            "account profile original actor changed"
        );
        Ok(())
    };
    let timeout = Duration::from_secs(10);
    Event::receive(&channel.receive(timeout)?, request, Phase::Create, None)?;
    *diagnostic_phase = 16;
    verify()?;
    *diagnostic_phase = 17;
    crate::account_profile_registry::require_loaded_actor_classes(&selected.account.account_sid)?;
    let mut native = NativeHostProfile::default();
    *diagnostic_phase = 18;
    let folder = Some(native.create_host(container, &selected.account.account_sid)?);
    *diagnostic_phase = 19;
    verify()?;
    channel.send(
        &Event {
            request: request.clone(),
            phase: Phase::Created,
            folder,
        }
        .wire()?,
        timeout,
    )?;
    Event::receive(&channel.receive(timeout)?, request, Phase::Remove, folder)?;
    *diagnostic_phase = 20;
    verify()?;
    native.verify_unused()?;
    native.close_original_trees()?;
    *diagnostic_phase = 21;
    native.delete_original()?;
    *diagnostic_phase = 22;
    native.verify_absent()?;
    *diagnostic_phase = 23;
    verify()?;
    channel.send(
        &Event {
            request: request.clone(),
            phase: Phase::Removed,
            folder,
        }
        .wire()?,
        timeout,
    )
}

#[cfg(test)]
#[path = "account_bootstrap_account_profile_tests.rs"]
mod tests;
