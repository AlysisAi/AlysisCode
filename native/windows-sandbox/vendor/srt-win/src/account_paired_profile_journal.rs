//! Immutable evidence for the original owner's paired profile lifetime.
//! Rows never reconstruct an actor, authorize a native effect, or grant cleanup.
//! Consumer resume replay is deliberately not a dependency of this journal.
use super::*;
use crate::account_control::bootstrap::entry::credential_wire::child::probe;
use crate::account_state::execution::paired_plan as paired;

const TABLE: &str = "candidate_paired_profile_events";
const MAX_BYTES: usize = 16384;
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_paired_profile_events(role INTEGER NOT NULL CHECK(role=0), sequence INTEGER NOT NULL CHECK(sequence BETWEEN 0 AND 10), event_json TEXT NOT NULL, PRIMARY KEY(role,sequence)) WITHOUT ROWID",
    "CREATE TRIGGER paired_profile_no_update BEFORE UPDATE ON candidate_paired_profile_events BEGIN SELECT RAISE(ABORT,'paired profile event is immutable'); END",
    "CREATE TRIGGER paired_profile_no_delete BEFORE DELETE ON candidate_paired_profile_events BEGIN SELECT RAISE(ABORT,'paired profile event is immutable'); END",
    "CREATE TRIGGER paired_profile_no_replace BEFORE INSERT ON candidate_paired_profile_events WHEN EXISTS(SELECT 1 FROM candidate_paired_profile_events WHERE role=NEW.role AND sequence=NEW.sequence) BEGIN SELECT RAISE(ABORT,'paired profile event is immutable'); END",
];

pub(super) fn create_schema(sql: &Connection) -> Result<()> {
    for statement in SCHEMA {
        sql.execute_batch(statement)?;
    }
    Ok(())
}

/// Schema only: parent admission calls this without following replay edges.
pub(super) fn require_schema(sql: &Connection) -> Result<()> {
    let mut statement = sql.prepare("SELECT sql FROM sqlite_schema WHERE tbl_name=?1")?;
    let rows: Vec<String> = statement
        .query_map([TABLE], |r| r.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    ensure!(
        rows.len() == SCHEMA.len() && SCHEMA.iter().all(|s| rows.iter().any(|r| r == s)),
        "paired profile schema differs; preserve evidence"
    );
    Ok(())
}

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(in crate::account_state::execution::paired_plan) enum Event {
    ResumeIntent {
        request: probe::Request,
    },
    Resumed {
        request: probe::Request,
    },
    CreateIntent {
        request: probe::Request,
    },
    Created {
        request: probe::Request,
        folder: [u8; 24],
    },
    UseIntent {
        request: probe::Request,
        folder: [u8; 24],
        nonce: String,
        consumer: BirthRequest,
    },
    Using {
        request: probe::Request,
        folder: [u8; 24],
        nonce: String,
        consumer: BirthRequest,
    },
    CaptureIntent {
        request: probe::Request,
        folder: [u8; 24],
        nonce: String,
        consumer: BirthRequest,
        retirement: String,
    },
    Captured {
        request: probe::Request,
        folder: [u8; 24],
        nonce: String,
        consumer: BirthRequest,
        retirement: String,
    },
    RemoveIntent {
        request: probe::Request,
        folder: [u8; 24],
        nonce: String,
        consumer: BirthRequest,
        retirement: String,
    },
    Removed {
        request: probe::Request,
        folder: [u8; 24],
        nonce: String,
        consumer: BirthRequest,
        retirement: String,
    },
    Exited {
        request: probe::Request,
        code: u32,
    },
}
impl Event {
    fn request(&self) -> &probe::Request {
        match self {
            Self::ResumeIntent { request }
            | Self::Resumed { request }
            | Self::CreateIntent { request }
            | Self::Created { request, .. }
            | Self::UseIntent { request, .. }
            | Self::Using { request, .. }
            | Self::CaptureIntent { request, .. }
            | Self::Captured { request, .. }
            | Self::RemoveIntent { request, .. }
            | Self::Removed { request, .. }
            | Self::Exited { request, .. } => request,
        }
    }
}

fn require_owner(role: Role) -> Result<()> {
    ensure!(role == Role::Owner, "paired profile requires owner role");
    Ok(())
}

fn known_nonces(plan: &Plan, accounts: &[Vec<super::Event>; 2]) -> Result<Vec<String>> {
    let mut nonces = super::reserved_nonces(plan);
    for account in accounts {
        if let Some(super::Event::Intent { selection }) = account.first() {
            super::reserve(&mut nonces, &selection.logon)?;
        }
        if let Some(super::Event::BirthIntent { request }) = account.get(2) {
            super::reserve(&mut nonces, &request.nonce)?;
        }
    }
    Ok(nonces)
}

fn validate_owner(
    plan: &Plan,
    accounts: &[Vec<super::Event>; 2],
    request: &probe::Request,
) -> Result<Vec<String>> {
    request.validate()?;
    ensure!(
        request.birth.purpose
            == Purpose::TrustedPairedProfileV1 {
                container: plan.execution.identity.container.clone(),
                package_sid: plan.execution.identity.package_sid.clone(),
                policy: 1,
            },
        "paired profile purpose differs from original container/package/policy"
    );
    let owner = &accounts[Role::Owner.index()];
    ensure!(
        owner.len() == 5,
        "paired profile requires five original owner birth receipts"
    );
    let super::Event::Acknowledged {
        request: birth,
        pid,
        created,
        image_id,
    } = &owner[4]
    else {
        anyhow::bail!("paired profile original owner acknowledgment missing");
    };
    ensure!(
        &request.birth == birth
            && request.pid == *pid
            && request.created == *created
            && request.birth.selection.image_id == *image_id
            && *image_id == plan.image_id,
        "paired profile resume differs from original owner birth"
    );
    // Re-evaluate against both current account histories: a consumer born later
    // cannot make a previously reserved owner nonce acceptable for profile use.
    let mut nonces = known_nonces(plan, accounts)?;
    super::reserve(&mut nonces, &request.nonce)?;
    Ok(nonces)
}

fn validate_consumer(accounts: &[Vec<super::Event>; 2], consumer: &BirthRequest) -> Result<()> {
    consumer.validate()?;
    ensure!(
        consumer.purpose.is_paired_consumer(),
        "paired profile use requires fixed consumer self-check"
    );
    let events = &accounts[Role::Consumers.index()];
    ensure!(
        events.len() == 5,
        "paired profile use requires acknowledged consumer birth"
    );
    let super::Event::Acknowledged { request, .. } = &events[4] else {
        anyhow::bail!("paired profile consumer acknowledgment missing");
    };
    ensure!(request == consumer, "paired profile consumer birth changed");
    Ok(())
}

fn validate_events(
    plan: &Plan,
    accounts: &[Vec<super::Event>; 2],
    consumer_parent: &[paired::Event],
    events: &[Event],
) -> Result<()> {
    ensure!(events.len() <= 11, "paired profile row bound exceeded");
    let Some(first) = events.first() else {
        return Ok(());
    };
    let Event::ResumeIntent { request: original } = first else {
        anyhow::bail!("paired profile lacks original resume intent");
    };
    let mut nonces = validate_owner(plan, accounts, original)?;
    for (index, event) in events.iter().enumerate() {
        ensure!(
            event.request() == original,
            "paired profile owner request changed"
        );
        match (index, event) {
            (0, Event::ResumeIntent { .. })
            | (1, Event::Resumed { .. })
            | (2, Event::CreateIntent { .. }) => {}
            (3, Event::Created { folder, .. }) => {
                ensure!(
                    *folder != [0; 24],
                    "paired profile original folder identity missing"
                );
            }
            (
                4,
                Event::UseIntent {
                    folder,
                    nonce,
                    consumer,
                    ..
                },
            ) => {
                ensure!(
                    events[3]
                        == Event::Created {
                            request: original.clone(),
                            folder: *folder
                        },
                    "paired profile use folder changed"
                );
                validate_consumer(accounts, consumer)?;
                #[cfg(test)]
                if let Purpose::FixtureProfileWriterV1 { binding } = &consumer.purpose {
                    binding.require_owner(original, *folder, nonce)?;
                }
                #[cfg(test)]
                if let Purpose::FixtureStationLowboxV1 { binding } = &consumer.purpose {
                    binding.require_owner(original, *folder, nonce)?;
                }
                super::reserve(&mut nonces, nonce)?;
            }
            (
                5,
                Event::Using {
                    folder,
                    nonce,
                    consumer,
                    ..
                },
            )
            | (
                6,
                Event::CaptureIntent {
                    folder,
                    nonce,
                    consumer,
                    ..
                },
            ) => {
                ensure!(
                    events[4]
                        == Event::UseIntent {
                            request: original.clone(),
                            folder: *folder,
                            nonce: nonce.clone(),
                            consumer: consumer.clone(),
                        },
                    "paired profile use binding changed"
                );
                if let Event::CaptureIntent { retirement, .. } = event {
                    ensure!(
                        consumer_parent.len() == 9
                            && consumer_parent.last() == Some(&paired::Event::Retired)
                            && retirement == &plan.roles[Role::Consumers.index()].retire_nonce,
                        "paired profile capture requires exact consumer retirement"
                    );
                }
            }
            (
                7,
                Event::Captured {
                    folder,
                    nonce,
                    consumer,
                    retirement,
                    ..
                },
            )
            | (
                8,
                Event::RemoveIntent {
                    folder,
                    nonce,
                    consumer,
                    retirement,
                    ..
                },
            )
            | (
                9,
                Event::Removed {
                    folder,
                    nonce,
                    consumer,
                    retirement,
                    ..
                },
            ) => {
                ensure!(
                    events[6]
                        == Event::CaptureIntent {
                            request: original.clone(),
                            folder: *folder,
                            nonce: nonce.clone(),
                            consumer: consumer.clone(),
                            retirement: retirement.clone(),
                        },
                    "paired profile capture/removal binding changed"
                );
            }
            (10, Event::Exited { code, .. }) => {
                ensure!(
                    *code == probe::EXIT_CODE,
                    "paired profile exit differs from fixed helper"
                );
            }
            _ => anyhow::bail!("paired profile event is out of order"),
        }
    }
    Ok(())
}

fn inventory(sql: &Connection, plan: &Plan) -> Result<Vec<Event>> {
    // This verifies plan identity, DELETE/FULL/autocommit, exact schemas, both
    // account histories, and their original bootstrap readiness. No resume replay.
    let accounts = super::inventory(sql, plan)?;
    let consumer_parent = paired::replay(sql, plan, Role::Consumers)?;
    require_schema(sql)?;
    let (count, max, nontext): (u64, Option<u64>, u64) = sql.query_row(
        "SELECT count(*),max(length(CAST(event_json AS BLOB))),coalesce(sum(typeof(event_json)!='text'),0) FROM candidate_paired_profile_events",
        [], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
    )?;
    ensure!(
        count <= 11 && max.unwrap_or(0) <= MAX_BYTES as u64 && nontext == 0,
        "paired profile inventory exceeds bounds or type"
    );
    let mut statement = sql.prepare("SELECT role,sequence,event_json FROM candidate_paired_profile_events ORDER BY role,sequence")?;
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
            role == Role::Owner.index() && sequence == events.len(),
            "paired profile role/sequence differs"
        );
        let event: Event = serde_json::from_str(&json)
            .map_err(|_| anyhow::anyhow!("malformed paired profile event; preserve evidence"))?;
        ensure!(
            serde_json::from_str::<serde_json::Value>(&json)? == serde_json::to_value(&event)?,
            "paired profile event has unexpected fields"
        );
        events.push(event);
    }
    validate_events(plan, &accounts, &consumer_parent, &events)?;
    Ok(events)
}

pub(in crate::account_state::execution::paired_plan) fn replay(
    sql: &Connection,
    plan: &Plan,
    role: Role,
) -> Result<Vec<Event>> {
    require_owner(role)?;
    inventory(sql, plan)
}

/// A native one-shot guard must be consumed before every attempted write. Failed
/// or uncertain commits are evidence to preserve, never permission to retry.
pub(in crate::account_state::execution::paired_plan) fn append(
    sql: &Connection,
    plan: &Plan,
    role: Role,
    event: Event,
) -> Result<()> {
    require_owner(role)?;
    let mut expected = inventory(sql, plan)?;
    ensure!(
        paired::replay(sql, plan, role)?.len() == 7,
        "paired profile append requires live owner Ready phase"
    );
    let consumer_parent = paired::replay(sql, plan, Role::Consumers)?;
    if matches!(&event, Event::UseIntent { .. } | Event::Using { .. }) {
        ensure!(
            consumer_parent.len() == 7,
            "paired profile use requires live consumer Ready phase"
        );
        // Only the absence gate is needed here. Calling consumer resume replay
        // would create a dependency cycle once resume requires profile Using.
        let started: bool = sql.query_row(
            "SELECT EXISTS(SELECT 1 FROM candidate_paired_account_resume_events LIMIT 1)",
            [],
            |r| r.get(0),
        )?;
        ensure!(!started, "paired profile use must precede consumer resume");
    }
    let accounts = super::inventory(sql, plan)?;
    let sequence = expected.len();
    expected.push(event.clone());
    validate_events(plan, &accounts, &consumer_parent, &expected)?;
    let encoded = serde_json::to_string(&event)?;
    ensure!(
        encoded.len() <= MAX_BYTES,
        "paired profile event exceeds byte bound"
    );
    let tx = sql.unchecked_transaction()?;
    tx.execute(
        "INSERT INTO candidate_paired_profile_events VALUES(?1,?2,?3)",
        params![role.index(), sequence, encoded],
    )?;
    #[cfg(test)]
    checkpoint("inserted", sequence, &tx);
    tx.commit()
        .context("paired profile commit uncertain; preserve original state")?;
    #[cfg(test)]
    checkpoint("committed", sequence, sql);
    ensure!(
        inventory(sql, plan)? == expected,
        "paired profile readback differs"
    );
    Ok(())
}

#[cfg(test)]
thread_local! {pub(crate) static PROFILE_JOURNAL_CHECKPOINT:std::cell::RefCell<Option<Box<dyn FnMut(&str,usize,&Connection)>>> = const {std::cell::RefCell::new(None)};}
#[cfg(test)]
fn checkpoint(phase: &str, sequence: usize, sql: &Connection) {
    PROFILE_JOURNAL_CHECKPOINT.with(|s| {
        if let Some(callback) = s.borrow_mut().as_mut() {
            callback(phase, sequence, sql);
        }
    });
}

#[cfg(test)]
#[path = "account_paired_profile_journal_tests.rs"]
mod tests;
