//! Record-before-resume for the trusted host bootstrap only. No account logon,
//! credential access, user command, cleanup or cold-to-live reconstruction.
use super::*;
use crate::account_control::{ControlChannel, ControlServer};
use std::time::Duration;

#[path = "account_bootstrap_logon_intent.rs"]
pub(crate) mod logon;

const TABLE: &str = "candidate_atomic_bootstrap_resume";
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_atomic_bootstrap_resume(sequence INTEGER PRIMARY KEY CHECK(sequence BETWEEN 0 AND 2), event_json TEXT NOT NULL)",
    "CREATE TRIGGER atomic_resume_no_update BEFORE UPDATE ON candidate_atomic_bootstrap_resume BEGIN SELECT RAISE(ABORT,'atomic resume event is immutable'); END",
    "CREATE TRIGGER atomic_resume_no_delete BEFORE DELETE ON candidate_atomic_bootstrap_resume BEGIN SELECT RAISE(ABORT,'atomic resume event is immutable'); END",
    "CREATE TRIGGER atomic_resume_no_replace BEFORE INSERT ON candidate_atomic_bootstrap_resume WHEN EXISTS(SELECT 1 FROM candidate_atomic_bootstrap_resume WHERE sequence=NEW.sequence) BEGIN SELECT RAISE(ABORT,'atomic resume event is immutable'); END",
];

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum ResumeEvent {
    Intent {
        nonce: String,
        birth: String,
        pid: u32,
        created: u64,
        image_id: [u8; 24],
    },
    Resumed {
        nonce: String,
    },
    Ready {
        nonce: String,
    },
}

pub(super) fn create_schema(connection: &Connection) -> Result<()> {
    for sql in SCHEMA {
        connection.execute_batch(sql)?;
    }
    logon::create_schema(connection)
}
pub(super) fn require_schema(connection: &Connection) -> Result<()> {
    logon::require_schema(connection)?;
    let mut s = connection.prepare("SELECT sql FROM sqlite_schema WHERE tbl_name=?1")?;
    let rows: Vec<String> = s
        .query_map([TABLE], |r| r.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    ensure!(
        rows.len() == SCHEMA.len() && SCHEMA.iter().all(|s| rows.iter().any(|r| r == s)),
        "atomic resume schema differs; preserve state"
    );
    Ok(())
}
fn validate(events: &[ResumeEvent], births: &[Event]) -> Result<()> {
    ensure!(events.len() <= 3, "atomic resume inventory exceeds bounds");
    if events.is_empty() {
        return Ok(());
    }
    let Some(Event::BootstrapCreated {
        nonce: birth,
        pid,
        created,
        image_id,
    }) = births.last()
    else {
        anyhow::bail!("resume requires durable bootstrap birth");
    };
    ensure!(births.len() == 4, "resume requires complete birth prefix");
    let ResumeEvent::Intent {
        nonce,
        birth: offered,
        pid: offered_pid,
        created: offered_created,
        image_id: offered_image,
    } = &events[0]
    else {
        anyhow::bail!("atomic resume intent missing");
    };
    validate_random_id(nonce)?;
    ensure!(
        nonce != birth
            && offered == birth
            && offered_pid == pid
            && offered_created == created
            && offered_image == image_id,
        "atomic resume intent differs from original bootstrap birth"
    );
    if events.len() > 1 {
        ensure!(
            events[1]
                == (ResumeEvent::Resumed {
                    nonce: nonce.clone()
                }),
            "atomic resume acknowledgment differs"
        );
    }
    if events.len() > 2 {
        ensure!(
            events[2]
                == (ResumeEvent::Ready {
                    nonce: nonce.clone()
                }),
            "atomic resume ready receipt differs"
        );
    }
    Ok(())
}
fn replay_resume(connection: &Connection, plan: &Plan) -> Result<Vec<ResumeEvent>> {
    let births = replay(connection, plan)?;
    require_schema(connection)?;
    let (count,max):(u64,Option<u64>)=connection.query_row(
        "SELECT count(*),max(length(CAST(event_json AS BLOB))) FROM candidate_atomic_bootstrap_resume",[],|r|Ok((r.get(0)?,r.get(1)?)))?;
    ensure!(
        count <= 3 && max.unwrap_or(0) <= 1024,
        "atomic resume inventory exceeds bounds"
    );
    let mut statement = connection.prepare(
        "SELECT sequence,event_json FROM candidate_atomic_bootstrap_resume ORDER BY sequence",
    )?;
    let mut events = Vec::new();
    for row in statement.query_map([], |r| Ok((r.get::<_, usize>(0)?, r.get::<_, String>(1)?)))? {
        let (sequence, json) = row?;
        ensure!(sequence == events.len(), "atomic resume sequence has a gap");
        events
            .push(serde_json::from_str(&json).context("malformed atomic resume event; preserve")?);
    }
    validate(&events, &births)?;
    Ok(events)
}
fn append_resume(connection: &Connection, plan: &Plan, event: ResumeEvent) -> Result<()> {
    let mut events = replay_resume(connection, plan)?;
    let sequence = events.len();
    events.push(event.clone());
    validate(&events, &replay(connection, plan)?)?;
    let encoded = serde_json::to_string(&event)?;
    ensure!(encoded.len() <= 1024, "atomic resume event oversized");
    let tx = connection.unchecked_transaction()?;
    tx.execute(
        "INSERT INTO candidate_atomic_bootstrap_resume VALUES(?1,?2)",
        params![sequence, encoded],
    )?;
    tx.commit()
        .context("atomic resume commit uncertain; preserve intent and original guard")?;
    ensure!(
        replay_resume(connection, plan)? == events,
        "atomic resume readback differs"
    );
    #[cfg(test)]
    {
        let hook = AFTER_RESUME_EVENT.with(|slot| {
            if slot
                .borrow()
                .as_ref()
                .is_some_and(|(count, _)| *count == events.len())
            {
                slot.borrow_mut().take().map(|(_, hook)| hook)
            } else {
                None
            }
        });
        if let Some(hook) = hook {
            hook(connection);
        }
    }
    Ok(())
}

#[cfg(test)]
thread_local! {
    pub(crate) static AFTER_RESUME_EVENT:std::cell::RefCell<Option<(usize,Box<dyn FnOnce(&Connection)>)>>=const{std::cell::RefCell::new(None)};
}

/// Not deserializable. The adapter consumes this once, then repeats native
/// observation of the original child before the one ResumeThread attempt.
pub(crate) struct ResumeIntent<'j, 'p, 'a, 'lease> {
    owner: &'j JournaledJob<'p, 'a, 'lease>,
    event: ResumeEvent,
    attempted: bool,
}
impl ResumeIntent<'_, '_, '_, '_> {
    pub(crate) fn consume(&mut self, child: &SuspendedBootstrap<'_>) -> Result<()> {
        ensure!(
            !std::mem::replace(&mut self.attempted, true),
            "atomic resume ticket already consumed"
        );
        self.owner.verify()?;
        let plan = self.owner.plan;
        ensure!(
            replay_resume(plan.database.connection()?, &plan.plan)? == [self.event.clone()],
            "atomic resume write-ahead ticket differs"
        );
        let ResumeEvent::Intent {
            pid,
            created,
            image_id,
            ..
        } = self.event
        else {
            anyhow::bail!("not a resume intent");
        };
        ensure!(
            child.durable_observation()? == (pid, created, image_id),
            "original bootstrap changed before resume"
        );
        Ok(())
    }
}

/// Original child guard drops before channel and borrowed job/image pins. This
/// exposes no account command, credentials, raw handle or retirement receipt.
pub(crate) struct RunningBootstrap<'j, 'p, 'a, 'lease> {
    pub(super) birth: JournaledBootstrap<'j, 'p, 'a, 'lease>,
    pub(super) _channel: ControlChannel<'j>,
}
impl RunningBootstrap<'_, '_, '_, '_> {
    #[cfg(test)]
    pub(crate) fn fixture_readonly_observer(&self) -> Result<crate::util::OwnedHandle> {
        self.birth.fixture_readonly_observer()
    }
}

impl<'j, 'p, 'a, 'lease> JournaledBootstrap<'j, 'p, 'a, 'lease> {
    /// Consuming this guard makes every error terminal for this birth. Even an
    /// uncertain SQL commit cannot repeat resume with a reconstructed receipt.
    pub(crate) fn start(
        mut self,
        server: ControlServer,
        timeout: Duration,
    ) -> Result<RunningBootstrap<'j, 'p, 'a, 'lease>> {
        self.owner.verify()?;
        let plan = self.owner.plan;
        server.require_address(plan.address)?;
        ensure!(
            timeout >= Duration::from_millis(1) && timeout <= Duration::from_secs(10),
            "bootstrap startup timeout outside bounds"
        );
        let (pid, created, image_id) = self.child.durable_observation()?;
        let births = replay(plan.database.connection()?, &plan.plan)?;
        ensure!(
            births.len() == 4
                && births.last()
                    == Some(&Event::BootstrapCreated {
                        nonce: self.nonce.clone(),
                        pid,
                        created,
                        image_id
                    }),
            "resume requires exact durable original bootstrap birth"
        );
        ensure!(
            replay_resume(plan.database.connection()?, &plan.plan)?.is_empty(),
            "atomic resume already attempted"
        );
        let nonce = random_id()?;
        let event = ResumeEvent::Intent {
            nonce: nonce.clone(),
            birth: self.nonce.clone(),
            pid,
            created,
            image_id,
        };
        append_resume(plan.database.connection()?, &plan.plan, event.clone())?;
        let mut intent = ResumeIntent {
            owner: self.owner,
            event,
            attempted: false,
        };
        self.child.resume_recorded(&mut intent)?;
        append_resume(
            plan.database.connection()?,
            &plan.plan,
            ResumeEvent::Resumed {
                nonce: nonce.clone(),
            },
        )?;
        let mut channel = server.accept(self.child.peer()?, timeout)?;
        // Echo is liveness data from the exact original peer. Only this live
        // coordinator can commit Ready; the message itself is not authority.
        let challenge = crate::account_control::bootstrap::entry::challenge(&nonce)?;
        channel.send(&challenge, timeout)?;
        ensure!(
            channel.receive(timeout)? == challenge,
            "bootstrap ready challenge differs"
        );
        self.child.capture_startup_exit()?;
        self.owner.verify()?;
        self.child.durable_observation()?;
        append_resume(
            plan.database.connection()?,
            &plan.plan,
            ResumeEvent::Ready { nonce },
        )?;
        self.child.allow_post_startup_work()?;
        channel.send(crate::account_control::bootstrap::STARTUP_CAPTURED, timeout)?;
        Ok(RunningBootstrap {
            birth: self,
            _channel: channel,
        })
    }
}

#[cfg(test)]
#[path = "account_bootstrap_resume_tests.rs"]
mod tests;
