//! Durable original-job retirement after the one selected birth completes.
//! This guard cannot be reconstructed from a cold journal. It grants no
//! profile/ACL retirement authority until explicit resource binding exists.
use super::*;

const TABLE: &str = "candidate_atomic_job_retirement";
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_atomic_job_retirement(sequence INTEGER PRIMARY KEY CHECK(sequence BETWEEN 0 AND 1), event_json TEXT NOT NULL)",
    "CREATE TRIGGER atomic_job_retirement_no_update BEFORE UPDATE ON candidate_atomic_job_retirement BEGIN SELECT RAISE(ABORT,'job retirement event is immutable'); END",
    "CREATE TRIGGER atomic_job_retirement_no_delete BEFORE DELETE ON candidate_atomic_job_retirement BEGIN SELECT RAISE(ABORT,'job retirement event is immutable'); END",
    "CREATE TRIGGER atomic_job_retirement_no_replace BEFORE INSERT ON candidate_atomic_job_retirement WHEN EXISTS(SELECT 1 FROM candidate_atomic_job_retirement WHERE sequence=NEW.sequence) BEGIN SELECT RAISE(ABORT,'job retirement event is immutable'); END",
];
pub(super) fn create_schema(sql: &Connection) -> Result<()> {
    for statement in SCHEMA {
        sql.execute_batch(statement)?;
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
        "job retirement schema differs; preserve state"
    );
    Ok(())
}
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Identity {
    operation: String,
    nonce: String,
    birth_nonce: String,
    pid: u32,
    created: u64,
    image_id: [u8; 24],
}
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum Event {
    Intent { identity: Identity },
    Exited { identity: Identity },
}

fn validate(sql: &Connection, plan: &Plan, events: &[Event]) -> Result<()> {
    ensure!(events.len() <= 2, "job retirement event bound exceeded");
    if events.is_empty() {
        return Ok(());
    }
    let Event::Intent { identity } = &events[0] else {
        anyhow::bail!("job retirement intent missing");
    };
    validate_random_id(&identity.nonce)?;
    let births = replay_birth(sql, plan)?;
    let [
        BirthEvent::Intent { request },
        BirthEvent::Created {
            nonce,
            pid,
            created,
            image_id,
        },
    ] = births.as_slice()
    else {
        anyhow::bail!("job retirement requires complete account birth");
    };
    ensure!(
        identity.operation == plan.operation
            && identity.birth_nonce == *nonce
            && identity.pid == *pid
            && identity.created == *created
            && identity.image_id == *image_id
            && identity.nonce != *nonce
            && identity.nonce != plan.operation
            && identity.nonce != request.selection.logon
            && identity.nonce != request.selection.ready,
        "job retirement binding differs from original birth"
    );
    if let Some(event) = events.get(1) {
        ensure!(
            *event
                == Event::Exited {
                    identity: identity.clone()
                },
            "job retirement completion differs from intent"
        );
    }
    Ok(())
}
fn replay(sql: &Connection, plan: &Plan) -> Result<Vec<Event>> {
    require_plan(sql, plan)?;
    let (count,max):(usize,Option<usize>)=sql.query_row("SELECT count(*),max(length(CAST(event_json AS BLOB))) FROM candidate_atomic_job_retirement",[],|r|Ok((r.get(0)?,r.get(1)?)))?;
    ensure!(
        count <= 2 && max.unwrap_or(0) <= 4096,
        "job retirement inventory exceeds bounds"
    );
    let mut s = sql.prepare(
        "SELECT sequence,event_json FROM candidate_atomic_job_retirement ORDER BY sequence",
    )?;
    let mut events = Vec::new();
    for row in s.query_map([], |r| Ok((r.get::<_, usize>(0)?, r.get::<_, String>(1)?)))? {
        let (sequence, bytes) = row?;
        ensure!(
            sequence == events.len(),
            "job retirement sequence has a gap"
        );
        events.push(
            serde_json::from_str(&bytes)
                .map_err(|_| anyhow::anyhow!("malformed job retirement event"))?,
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
    ensure!(bytes.len() <= 4096, "job retirement event exceeds bound");
    let tx = sql.unchecked_transaction()?;
    tx.execute(
        "INSERT INTO candidate_atomic_job_retirement VALUES(?1,?2)",
        params![sequence, bytes],
    )?;
    tx.commit()
        .context("job retirement commit uncertain; preserve pending state")?;
    #[cfg(test)]
    RETIREMENT_CHECKPOINT.with(|slot| {
        if let Some(hook) = slot.borrow_mut().as_mut() {
            hook(sequence, sql);
        }
    });
    ensure!(
        replay(sql, plan)? == expected,
        "job retirement readback differs"
    );
    Ok(())
}

#[cfg(test)]
thread_local! {
    pub(crate) static RETIREMENT_CHECKPOINT: std::cell::RefCell<Option<Box<dyn FnMut(usize,&Connection)>>> = const { std::cell::RefCell::new(None) };
}

pub(super) struct RetirementIntent<'p, 'a, 'lease> {
    plan: &'p CommittedPlan<'a, 'lease>,
    identity: Identity,
}

/// Live original operation only. No Deserialize/Clone, cold constructor, raw
/// handle, profile cleanup or shared-permission restoration API is exposed.
pub(in crate::account_state::execution::bootstrap_plan) struct CompletedJob<'p, 'a, 'lease> {
    _plan: &'p CommittedPlan<'a, 'lease>,
    _identity: Identity,
}
impl CompletedJob<'_, '_, '_> {
    pub(in crate::account_state::execution::bootstrap_plan) fn verify_for(
        &self,
        plan: &CommittedPlan<'_, '_>,
    ) -> Result<&str> {
        ensure!(
            std::ptr::eq(self._plan, plan),
            "completed job belongs to another live plan"
        );
        plan.verify()?;
        ensure!(
            completed_nonce(plan.database.connection()?, &plan.plan)? == self._identity.nonce,
            "completed job receipt differs from live ownership"
        );
        Ok(&self._identity.nonce)
    }
}
/// Diagnostic binding check only. This string cannot reconstruct CompletedJob.
pub(in crate::account_state::execution::bootstrap_plan) fn completed_nonce(
    sql: &Connection,
    plan: &Plan,
) -> Result<String> {
    let events = replay(sql, plan)?;
    let [Event::Intent { .. }, Event::Exited { identity }] = events.as_slice() else {
        anyhow::bail!("original job retirement is incomplete");
    };
    Ok(identity.nonce.clone())
}
impl<'p, 'a, 'lease> RetirementIntent<'p, 'a, 'lease> {
    pub(super) fn prepare(
        job: &JournaledJob<'p, 'a, 'lease>,
        child: &AccountBirthObservation,
    ) -> Result<Self> {
        job.verify()?;
        let plan = job.plan;
        let sql = plan.database.connection()?;
        ensure!(
            replay(sql, &plan.plan)?.is_empty(),
            "job retirement was already attempted"
        );
        let births = replay_birth(sql, &plan.plan)?;
        let Some(BirthEvent::Created {
            nonce,
            pid,
            created,
            image_id,
        }) = births.get(1)
        else {
            anyhow::bail!("job retirement birth receipt missing");
        };
        ensure!(
            (*pid, *created, *image_id) == (child.pid, child.created, child.image_id),
            "job retirement observation differs"
        );
        let identity = Identity {
            operation: plan.plan.operation.clone(),
            nonce: random_id()?,
            birth_nonce: nonce.clone(),
            pid: *pid,
            created: *created,
            image_id: *image_id,
        };
        append(
            sql,
            &plan.plan,
            Event::Intent {
                identity: identity.clone(),
            },
        )?;
        job.verify()?;
        Ok(Self { plan, identity })
    }
    pub(super) fn finish(
        self,
        job: JournaledJob<'p, 'a, 'lease>,
        exit: AccountExitObserver,
        timeout: Duration,
    ) -> Result<CompletedJob<'p, 'a, 'lease>> {
        // Ownership of this exact job is mandatory. Even a failed comparison
        // consumes it; no borrowed-job caller can keep containment alive.
        ensure!(
            std::ptr::eq(job.plan, self.plan),
            "job retirement original plan differs"
        );
        job.verify()?;
        ensure!(
            replay(self.plan.database.connection()?, &self.plan.plan)?
                == vec![Event::Intent {
                    identity: self.identity.clone()
                }],
            "job retirement intent changed before closure"
        );
        // Job::drop requests termination and requires the ORIGINAL job's native
        // active-process count to reach zero before releasing its sole handle.
        // Unverified drain exits without unwinding into outer resource cleanup.
        drop(job);
        exit.wait(timeout)?;
        self.plan.verify()?;
        append(
            self.plan.database.connection()?,
            &self.plan.plan,
            Event::Exited {
                identity: self.identity.clone(),
            },
        )?;
        Ok(CompletedJob {
            _plan: self.plan,
            _identity: self.identity,
        })
    }
}

#[cfg(test)]
#[path = "account_bootstrap_job_retirement_tests.rs"]
mod tests;
