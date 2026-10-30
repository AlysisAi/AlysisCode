//! Bounded evidence for one fixed consumer self-check. Parsed rows cannot
//! reconstruct an original process, authorize resume, retry, or grant cleanup.
use super::*;
use crate::account_control::bootstrap::entry::credential_wire::child::probe;
use crate::account_state::execution::paired_plan as paired;

const TABLE: &str = "candidate_paired_account_resume_events";
const MAX_BYTES: usize = 8192;
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_paired_account_resume_events(role INTEGER NOT NULL CHECK(role=1), sequence INTEGER NOT NULL CHECK(sequence BETWEEN 0 AND 2), event_json TEXT NOT NULL, PRIMARY KEY(role,sequence)) WITHOUT ROWID",
    "CREATE TRIGGER paired_account_resume_no_update BEFORE UPDATE ON candidate_paired_account_resume_events BEGIN SELECT RAISE(ABORT,'paired account resume event is immutable'); END",
    "CREATE TRIGGER paired_account_resume_no_delete BEFORE DELETE ON candidate_paired_account_resume_events BEGIN SELECT RAISE(ABORT,'paired account resume event is immutable'); END",
    "CREATE TRIGGER paired_account_resume_no_replace BEFORE INSERT ON candidate_paired_account_resume_events WHEN EXISTS(SELECT 1 FROM candidate_paired_account_resume_events WHERE role=NEW.role AND sequence=NEW.sequence) BEGIN SELECT RAISE(ABORT,'paired account resume event is immutable'); END",
];

pub(super) fn create_schema(sql: &Connection) -> Result<()> {
    for statement in SCHEMA {
        sql.execute_batch(statement)?;
    }
    Ok(())
}

/// Only inspect schema here: parent require_plan must not recurse into replay.
pub(super) fn require_schema(sql: &Connection) -> Result<()> {
    let mut statement = sql.prepare("SELECT sql FROM sqlite_schema WHERE tbl_name=?1")?;
    let rows: Vec<String> = statement
        .query_map([TABLE], |r| r.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    ensure!(
        rows.len() == SCHEMA.len() && SCHEMA.iter().all(|s| rows.iter().any(|r| r == s)),
        "paired account resume schema differs; preserve evidence"
    );
    Ok(())
}

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(in crate::account_state::execution::paired_plan) enum Event {
    Intent { request: probe::Request },
    Resumed { request: probe::Request },
    Exited { request: probe::Request, code: u32 },
}

fn require_consumer(role: Role) -> Result<()> {
    ensure!(
        role == Role::Consumers,
        "paired account resume requires consumer role"
    );
    Ok(())
}

fn validate_request(
    plan: &Plan,
    accounts: &[Vec<super::Event>; 2],
    request: &probe::Request,
) -> Result<()> {
    request.validate()?;
    #[cfg(test)]
    if let Purpose::FixtureStationLowboxV1 { binding } = &request.birth.purpose {
        binding.require_resume_nonce(&request.nonce)?;
    }
    ensure!(
        request.birth.purpose.is_paired_consumer(),
        "paired account resume requires fixed TrustedSelfCheckV1 purpose"
    );
    let consumer = &accounts[Role::Consumers.index()];
    ensure!(
        consumer.len() == 5 && accounts[Role::Owner.index()].len() == 5,
        "paired account resume requires five original account birth receipts for both roles"
    );
    let super::Event::Acknowledged {
        request: born,
        pid,
        created,
        image_id,
    } = &consumer[4]
    else {
        anyhow::bail!("paired account original birth acknowledgment missing");
    };
    // Parent inventory has already checked all five rows, exact birth/ack
    // equality, both role selections, and the complete immutable plan.
    ensure!(
        &request.birth == born
            && request.pid == *pid
            && request.created == *created
            && request.birth.selection.image_id == *image_id
            && *image_id == plan.image_id,
        "paired account resume differs from original acknowledged birth"
    );
    let mut nonces = super::reserved_nonces(plan);
    for account in accounts {
        if let Some(super::Event::Intent { selection }) = account.first() {
            super::reserve(&mut nonces, &selection.logon)?;
        }
        if let Some(super::Event::BirthIntent { request }) = account.get(2) {
            super::reserve(&mut nonces, &request.nonce)?;
        }
    }
    super::reserve(&mut nonces, &request.nonce)
}

fn validate_events(plan: &Plan, accounts: &[Vec<super::Event>; 2], events: &[Event]) -> Result<()> {
    ensure!(events.len() <= 3, "paired account resume exceeds row bound");
    let Some(first) = events.first() else {
        return Ok(());
    };
    let Event::Intent { request } = first else {
        anyhow::bail!("paired account resume lacks initial intent");
    };
    validate_request(plan, accounts, request)?;
    for (index, event) in events.iter().enumerate() {
        match (index, event) {
            (0, Event::Intent { .. }) => {}
            (1, Event::Resumed { request: resumed }) => {
                ensure!(resumed == request, "paired account resumed request changed");
            }
            (
                2,
                Event::Exited {
                    request: exited,
                    code,
                },
            ) => {
                ensure!(
                    exited == request && *code == probe::EXIT_CODE,
                    "paired account exit differs from fixed original self-check"
                );
            }
            _ => anyhow::bail!("paired account resume event is out of order"),
        }
    }
    Ok(())
}

fn inventory(sql: &Connection, plan: &Plan) -> Result<Vec<Event>> {
    // The parent inventory checks require_plan (including DELETE/FULL and
    // autocommit), both bootstrap histories, and both account histories.
    let accounts = super::inventory(sql, plan)?;
    require_schema(sql)?;
    let (count, max, nontext): (u64, Option<u64>, u64) = sql.query_row(
        "SELECT count(*),max(length(CAST(event_json AS BLOB))),coalesce(sum(typeof(event_json)!='text'),0) FROM candidate_paired_account_resume_events",
        [], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
    )?;
    ensure!(
        count <= 3 && max.unwrap_or(0) <= MAX_BYTES as u64 && nontext == 0,
        "paired account resume inventory exceeds bounds or type"
    );
    let mut statement = sql.prepare(
        "SELECT role,sequence,event_json FROM candidate_paired_account_resume_events ORDER BY role,sequence",
    )?;
    let mut events = Vec::new();
    for row in statement.query_map([], |r| {
        Ok((
            r.get::<_, usize>(0)?,
            r.get::<_, usize>(1)?,
            r.get::<_, String>(2)?,
        ))
    })? {
        let (role, sequence, json) = row?;
        ensure!(
            role == Role::Consumers.index() && sequence == events.len(),
            "paired account resume role/sequence differs"
        );
        let event: Event = serde_json::from_str(&json)
            .map_err(|_| anyhow::anyhow!("malformed paired account resume; preserve evidence"))?;
        ensure!(
            serde_json::from_str::<serde_json::Value>(&json)? == serde_json::to_value(&event)?,
            "paired account resume has unexpected fields"
        );
        events.push(event);
    }
    validate_events(plan, &accounts, &events)?;
    validate_profile_binding(sql, plan, &accounts, &events, false)?;
    Ok(events)
}

fn validate_profile_binding(
    sql: &Connection,
    plan: &Plan,
    accounts: &[Vec<super::Event>; 2],
    events: &[Event],
    appending: bool,
) -> Result<()> {
    #[cfg(test)]
    if events.first().is_some_and(|event| matches!(event, Event::Intent { request } if matches!(request.birth.purpose, Purpose::FixtureProfileWriterV1 { .. } | Purpose::FixtureStationLowboxV1 { .. }))) {
        ensure!(matches!(accounts[Role::Owner.index()].get(2), Some(super::Event::BirthIntent { request }) if matches!(request.purpose, Purpose::TrustedPairedProfileV1 { .. })), "fixed writer requires original paired profile owner");
    }
    let Some(super::Event::BirthIntent { request: owner }) = accounts[Role::Owner.index()].get(2)
    else {
        return Ok(());
    };
    if !matches!(owner.purpose, Purpose::TrustedPairedProfileV1 { .. }) {
        return Ok(());
    }
    let profile = super::profile_journal::replay(sql, plan, Role::Owner)?;
    let Some(Event::Intent { request }) = events.first() else {
        return Ok(());
    };
    let Some(super::profile_journal::Event::Using {
        request: original,
        nonce,
        consumer,
        ..
    }) = profile.get(5)
    else {
        anyhow::bail!("paired consumer resume requires durable original profile use");
    };
    ensure!(
        consumer == &request.birth && request.nonce != original.nonce && &request.nonce != nonce,
        "paired consumer resume differs from original profile use"
    );
    if appending {
        ensure!(
            profile.len() == 6 && paired::replay(sql, plan, Role::Owner)?.len() == 7,
            "paired consumer resume requires live original profile owner"
        );
    }
    Ok(())
}

pub(in crate::account_state::execution::paired_plan) fn replay(
    sql: &Connection,
    plan: &Plan,
    role: Role,
) -> Result<Vec<Event>> {
    require_consumer(role)?;
    inventory(sql, plan)
}

/// The native caller must consume its one-shot live guard before attempting
/// this write. A failed/uncertain write grants neither retry nor native effect.
pub(in crate::account_state::execution::paired_plan) fn append(
    sql: &Connection,
    plan: &Plan,
    role: Role,
    event: Event,
) -> Result<()> {
    require_consumer(role)?;
    let mut expected = inventory(sql, plan)?;
    ensure!(
        paired::replay(sql, plan, role)?.len() == 7,
        "paired account resume append requires live Ready phase"
    );
    let accounts = super::inventory(sql, plan)?;
    let sequence = expected.len();
    expected.push(event.clone());
    validate_events(plan, &accounts, &expected)?;
    validate_profile_binding(sql, plan, &accounts, &expected, true)?;
    let encoded = serde_json::to_string(&event)?;
    ensure!(
        encoded.len() <= MAX_BYTES,
        "paired account resume exceeds byte bound"
    );
    let tx = sql.unchecked_transaction()?;
    tx.execute(
        "INSERT INTO candidate_paired_account_resume_events VALUES(?1,?2,?3)",
        params![role.index(), sequence, encoded],
    )?;
    #[cfg(test)]
    checkpoint("inserted", sequence, &tx);
    tx.commit()
        .context("paired account resume commit uncertain; preserve original state")?;
    #[cfg(test)]
    checkpoint("committed", sequence, sql);
    ensure!(
        inventory(sql, plan)? == expected,
        "paired account resume readback differs"
    );
    Ok(())
}

#[cfg(test)]
thread_local! {pub(crate) static RESUME_JOURNAL_CHECKPOINT:std::cell::RefCell<Option<Box<dyn FnMut(&str,usize,&Connection)>>> = const {std::cell::RefCell::new(None)};}
#[cfg(test)]
fn checkpoint(phase: &str, sequence: usize, sql: &Connection) {
    RESUME_JOURNAL_CHECKPOINT.with(|s| {
        if let Some(callback) = s.borrow_mut().as_mut() {
            callback(phase, sequence, sql);
        }
    });
}

#[cfg(test)]
#[path = "account_paired_resume_journal_tests.rs"]
mod tests;
