//! Single durable resume for a fixed trusted self-check, never a user command.
//! Original live job/child guards are mandatory. Cold rows convey no authority.
use super::*;
use crate::account_control::bootstrap::{
    entry::credential_wire::child::probe, pull::ObservedRunner,
};

#[path = "account_bootstrap_account_profile.rs"]
pub(crate) mod account_profile;

const TABLE: &str = "candidate_atomic_account_resume";
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_atomic_account_resume(sequence INTEGER PRIMARY KEY CHECK(sequence BETWEEN 0 AND 2), event_json TEXT NOT NULL)",
    "CREATE TRIGGER atomic_account_resume_no_update BEFORE UPDATE ON candidate_atomic_account_resume BEGIN SELECT RAISE(ABORT,'account resume event is immutable'); END",
    "CREATE TRIGGER atomic_account_resume_no_delete BEFORE DELETE ON candidate_atomic_account_resume BEGIN SELECT RAISE(ABORT,'account resume event is immutable'); END",
    "CREATE TRIGGER atomic_account_resume_no_replace BEFORE INSERT ON candidate_atomic_account_resume WHEN EXISTS(SELECT 1 FROM candidate_atomic_account_resume WHERE sequence=NEW.sequence) BEGIN SELECT RAISE(ABORT,'account resume event is immutable'); END",
];
pub(super) fn create_schema(sql: &Connection) -> Result<()> {
    for statement in SCHEMA {
        sql.execute_batch(statement)?;
    }
    account_profile::create_schema(sql)
}
pub(super) fn require_schema(sql: &Connection) -> Result<()> {
    account_profile::require_schema(sql)?;
    let mut s = sql.prepare("SELECT sql FROM sqlite_schema WHERE tbl_name=?1")?;
    let rows: Vec<String> = s
        .query_map([TABLE], |r| r.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    ensure!(
        rows.len() == SCHEMA.len() && SCHEMA.iter().all(|s| rows.iter().any(|r| r == s)),
        "account resume schema differs; preserve state"
    );
    Ok(())
}
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum Event {
    Intent { request: probe::Request },
    Resumed { nonce: String },
    Exited { nonce: String, code: u32 },
}
fn validate(sql: &Connection, plan: &Plan, events: &[Event]) -> Result<()> {
    ensure!(events.len() <= 3, "account resume event bound exceeded");
    if events.is_empty() {
        return Ok(());
    }
    let Event::Intent { request } = &events[0] else {
        anyhow::bail!("account resume intent missing");
    };
    request.validate()?;
    let births = replay_birth(sql, plan)?;
    let [
        BirthEvent::Intent { request: birth },
        BirthEvent::Created {
            nonce,
            pid,
            created,
            image_id,
        },
    ] = births.as_slice()
    else {
        anyhow::bail!("account resume requires complete original birth");
    };
    ensure!(
        &request.birth == birth
            && nonce == &birth.nonce
            && request.pid == *pid
            && request.created == *created
            && request.birth.selection.image_id == *image_id,
        "account resume differs from original birth"
    );
    if let Some(event) = events.get(1) {
        ensure!(
            *event
                == Event::Resumed {
                    nonce: request.nonce.clone()
                },
            "account resume receipt differs"
        );
    }
    if let Some(event) = events.get(2) {
        ensure!(
            *event
                == Event::Exited {
                    nonce: request.nonce.clone(),
                    code: probe::EXIT_CODE
                },
            "account self-check exit receipt differs"
        );
    }
    Ok(())
}
fn replay(sql: &Connection, plan: &Plan) -> Result<Vec<Event>> {
    require_plan(sql, plan)?;
    let (count,max):(usize,Option<usize>) = sql.query_row("SELECT count(*),max(length(CAST(event_json AS BLOB))) FROM candidate_atomic_account_resume", [], |r| Ok((r.get(0)?,r.get(1)?)))?;
    ensure!(
        count <= 3 && max.unwrap_or(0) <= 8192,
        "account resume inventory exceeds bounds"
    );
    let mut s = sql.prepare(
        "SELECT sequence,event_json FROM candidate_atomic_account_resume ORDER BY sequence",
    )?;
    let mut events = Vec::new();
    for row in s.query_map([], |r| Ok((r.get::<_, usize>(0)?, r.get::<_, String>(1)?)))? {
        let (sequence, bytes) = row?;
        ensure!(
            sequence == events.len(),
            "account resume sequence has a gap"
        );
        events.push(
            serde_json::from_str(&bytes)
                .map_err(|_| anyhow::anyhow!("malformed account resume event"))?,
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
    ensure!(bytes.len() <= 8192, "account resume event exceeds bounds");
    let tx = sql.unchecked_transaction()?;
    tx.execute(
        "INSERT INTO candidate_atomic_account_resume VALUES(?1,?2)",
        params![sequence, bytes],
    )?;
    #[cfg(test)]
    checkpoint("inserted", sequence, &tx);
    tx.commit()
        .context("account resume commit uncertain; no retry")?;
    #[cfg(test)]
    checkpoint("committed", sequence, sql);
    ensure!(
        replay(sql, plan)? == expected,
        "account resume readback differs"
    );
    Ok(())
}
#[cfg(test)]
thread_local! {
    pub(crate) static ACCOUNT_RESUME_CHECKPOINT: std::cell::RefCell<Option<Box<dyn FnMut(&str,usize,&Connection)>>> = const { std::cell::RefCell::new(None) };
}
#[cfg(test)]
fn checkpoint(phase: &str, sequence: usize, sql: &Connection) {
    ACCOUNT_RESUME_CHECKPOINT.with(|slot| {
        if let Some(hook) = slot.borrow_mut().as_mut() {
            hook(phase, sequence, sql);
        }
    });
}
/// Constructible only here after the committed original child birth. Consuming
/// an attempted ticket is irreversible even if any later query/send fails.
pub(crate) struct ResumeIntent<'j, 'p, 'a, 'lease> {
    owner: &'j JournaledJob<'p, 'a, 'lease>,
    request: probe::Request,
    attempted: bool,
}
impl ResumeIntent<'_, '_, '_, '_> {
    pub(crate) fn consume(&mut self, observed: &ObservedRunner<'_>) -> Result<probe::Request> {
        ensure!(
            !std::mem::replace(&mut self.attempted, true),
            "account resume already attempted"
        );
        self.owner.verify()?;
        let plan = self.owner.plan;
        ensure!(
            replay(plan.database.connection()?, &plan.plan)?
                == [Event::Intent {
                    request: self.request.clone()
                }],
            "account resume intent changed"
        );
        ensure!(
            observed.birth_identity()?
                == (
                    self.request.pid,
                    self.request.created,
                    self.request.birth.selection.image_id
                ),
            "original account resume identity changed"
        );
        Ok(self.request.clone())
    }
}
pub(super) fn run(
    job: &JournaledJob<'_, '_, '_>,
    birth: &BirthRequest,
    observed: ObservedRunner<'_>,
    timeout: Duration,
) -> Result<()> {
    job.verify()?;
    let plan = job.plan;
    let sql = plan.database.connection()?;
    ensure!(
        replay(sql, &plan.plan)?.is_empty(),
        "account resume previously attempted"
    );
    let (pid, created, image) = observed.birth_identity()?;
    ensure!(
        image == birth.selection.image_id,
        "account resume image differs"
    );
    let request = probe::Request {
        birth: birth.clone(),
        nonce: random_id()?,
        pid,
        created,
    };
    append(
        sql,
        &plan.plan,
        Event::Intent {
            request: request.clone(),
        },
    )?;
    let mut ticket = ResumeIntent {
        owner: job,
        request: request.clone(),
        attempted: false,
    };
    let mut running = observed.start_self_check(&mut ticket, timeout)?;
    job.verify()?;
    append(
        sql,
        &plan.plan,
        Event::Resumed {
            nonce: request.nonce.clone(),
        },
    )?;
    if matches!(birth.purpose, Purpose::TrustedUnusedProfileV1 { .. }) {
        account_profile::run(job, &request, &mut running, timeout)?;
    }
    #[cfg(test)]
    if matches!(birth.purpose, Purpose::FixturePrivateStationV1) {
        running.fixture_private_station(timeout)?;
    }
    running.finish(timeout)?;
    job.verify()?;
    append(
        sql,
        &plan.plan,
        Event::Exited {
            nonce: request.nonce,
            code: probe::EXIT_CODE,
        },
    )
}

#[cfg(test)]
#[path = "account_bootstrap_account_resume_tests.rs"]
mod tests;
