//! Bounded per-role credential and suspended-account birth evidence.
//! No parsed row authorizes credential access, native birth, resume or cleanup.
//! Parent readiness is a prerequisite; retirement preserves earlier evidence.
use super::*;
use crate::account_control::bootstrap::entry::credential_wire::{
    Selection,
    child::{BirthRequest, Purpose},
};

#[path = "account_paired_resume_journal.rs"]
pub(super) mod resume_journal;

#[path = "account_paired_profile_journal.rs"]
pub(super) mod profile_journal;

const TABLE: &str = "candidate_paired_account_events";
const MAX_BYTES: usize = 8192;
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_paired_account_events(role INTEGER NOT NULL CHECK(role IN (0,1)), sequence INTEGER NOT NULL CHECK(sequence BETWEEN 0 AND 4), event_json TEXT NOT NULL, PRIMARY KEY(role,sequence)) WITHOUT ROWID",
    "CREATE TRIGGER paired_account_no_update BEFORE UPDATE ON candidate_paired_account_events BEGIN SELECT RAISE(ABORT,'paired account event is immutable'); END",
    "CREATE TRIGGER paired_account_no_delete BEFORE DELETE ON candidate_paired_account_events BEGIN SELECT RAISE(ABORT,'paired account event is immutable'); END",
    "CREATE TRIGGER paired_account_no_replace BEFORE INSERT ON candidate_paired_account_events WHEN EXISTS(SELECT 1 FROM candidate_paired_account_events WHERE role=NEW.role AND sequence=NEW.sequence) BEGIN SELECT RAISE(ABORT,'paired account event is immutable'); END",
];

pub(super) fn create_schema(sql: &Connection) -> Result<()> {
    for statement in SCHEMA {
        sql.execute_batch(statement)?;
    }
    resume_journal::create_schema(sql)?;
    profile_journal::create_schema(sql)
}

/// Schema only: the parent's require_plan calls this without recursing into
/// account replay. Account replay may then check the complete parent journal.
pub(super) fn require_schema(sql: &Connection) -> Result<()> {
    resume_journal::require_schema(sql)?;
    profile_journal::require_schema(sql)?;
    let mut statement = sql.prepare("SELECT sql FROM sqlite_schema WHERE tbl_name=?1")?;
    let rows: Vec<String> = statement
        .query_map([TABLE], |r| r.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    ensure!(
        rows.len() == SCHEMA.len() && SCHEMA.iter().all(|s| rows.iter().any(|r| r == s)),
        "paired account schema differs; preserve evidence"
    );
    Ok(())
}

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(super) enum Event {
    Intent {
        selection: Selection,
    },
    Delivered {
        selection: Selection,
    },
    BirthIntent {
        request: BirthRequest,
    },
    Born {
        request: BirthRequest,
        pid: u32,
        created: u64,
        image_id: [u8; 24],
    },
    Acknowledged {
        request: BirthRequest,
        pid: u32,
        created: u64,
        image_id: [u8; 24],
    },
}

fn reserved_nonces(plan: &Plan) -> Vec<String> {
    let mut values = vec![plan.operation.clone(), plan.execution.transaction.clone()];
    for role in &plan.roles {
        values.push(
            role.control_nonce
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect(),
        );
        values.extend([
            role.job_nonce.clone(),
            role.birth_nonce.clone(),
            role.resume_nonce.clone(),
            role.retire_nonce.clone(),
        ]);
    }
    values
}
fn reserve(nonces: &mut Vec<String>, nonce: &str) -> Result<()> {
    validate_random_id(nonce)?;
    ensure!(
        !nonces.iter().any(|used| used == nonce),
        "paired account nonce was reused"
    );
    nonces.push(nonce.into());
    Ok(())
}

fn validate_selection(
    plan: &Plan,
    role: Role,
    parent: &[super::Event],
    selected: &Selection,
) -> Result<()> {
    selected.encode()?;
    ensure!(
        parent.get(6) == Some(&super::Event::Ready),
        "paired account evidence requires durable original role readiness"
    );
    let Some(super::Event::Born {
        pid,
        created,
        image_id,
    }) = parent.get(3)
    else {
        anyhow::bail!("paired account original bootstrap birth missing");
    };
    ensure!(
        selected.operation == plan.operation
            && selected.ready == plan.roles[role.index()].resume_nonce
            && selected.bootstrap_pid == *pid
            && selected.bootstrap_created == *created
            && selected.image_id == *image_id
            && selected.image_id == plan.image_id
            && selected.session == plan.session
            && selected.account == plan.execution.identity.binding
            && selected.credential_generation == plan.credential_generation,
        "paired account selection differs from original role/plan"
    );
    Ok(())
}
fn validate_request(
    request: &BirthRequest,
    selected: &Selection,
    role: Role,
    plan: &Plan,
) -> Result<()> {
    request.encode()?;
    ensure!(
        &request.selection == selected
            && (request.purpose == Purpose::ObserveOnly
                || (role == Role::Consumers && request.purpose.is_paired_consumer())
                || (role == Role::Owner
                    && request.purpose
                        == Purpose::TrustedPairedProfileV1 {
                            container: plan.execution.identity.container.clone(),
                            package_sid: plan.execution.identity.package_sid.clone(),
                            policy: 1,
                        })),
        "paired account birth requires exact selection and fixed role-bound purpose"
    );
    #[cfg(test)]
    if let Purpose::FixtureProfileWriterV1 { binding } = &request.purpose {
        ensure!(
            role == Role::Consumers
                && binding.container == plan.execution.identity.container
                && binding.package_sid == plan.execution.identity.package_sid,
            "writer birth differs from paired profile plan"
        );
    }
    #[cfg(test)]
    if let Purpose::FixtureStationLowboxV1 { binding } = &request.purpose {
        binding.require_rpc_policy(plan.rpc_policy)?;
        binding.require_namespace_birth(plan.namespace_birth)?;
        ensure!(
            role == Role::Consumers
                && binding.profile.container == plan.execution.identity.container
                && binding.profile.package_sid == plan.execution.identity.package_sid
                && binding.initializer.resources.owner_job
                    == plan.roles[Role::Owner.index()].job_nonce
                && binding.initializer.resources.consumer_job
                    == plan.roles[Role::Consumers.index()].job_nonce,
            "station lowbox birth differs from original paired plan"
        );
    }
    Ok(())
}

fn validate_inventory(
    plan: &Plan,
    parents: &[Vec<super::Event>; 2],
    inventory: &[Vec<Event>; 2],
) -> Result<()> {
    let mut nonces = reserved_nonces(plan);
    let mut processes = Vec::new();
    for role in [Role::Owner, Role::Consumers] {
        let events = &inventory[role.index()];
        ensure!(
            events.len() <= 5,
            "paired account role inventory exceeds bound"
        );
        let Some(first) = events.first() else {
            continue;
        };
        let Event::Intent { selection } = first else {
            anyhow::bail!("paired account inventory lacks initial intent");
        };
        validate_selection(plan, role, &parents[role.index()], selection)?;
        reserve(&mut nonces, &selection.logon)?;
        for (index, event) in events.iter().enumerate() {
            match (index, event) {
                (0, Event::Intent { .. }) => {}
                (
                    1,
                    Event::Delivered {
                        selection: delivered,
                    },
                ) => {
                    ensure!(delivered == selection, "paired delivered selection changed");
                }
                (2, Event::BirthIntent { request }) => {
                    validate_request(request, selection, role, plan)?;
                    reserve(&mut nonces, &request.nonce)?;
                }
                (
                    3,
                    Event::Born {
                        request,
                        pid,
                        created,
                        image_id,
                    },
                ) => {
                    let Event::BirthIntent { request: intended } = &events[2] else {
                        anyhow::bail!("paired account birth intent missing");
                    };
                    ensure!(request == intended, "paired account birth request changed");
                    ensure!(
                        *pid != 0
                            && *pid != plan.execution.pid
                            && *pid != selection.bootstrap_pid
                            && *created >= selection.bootstrap_created
                            && *image_id == selection.image_id,
                        "paired account original process/image identity differs"
                    );
                    let identity = (*pid, *created);
                    for parent in parents {
                        if let Some(super::Event::Born { pid, created, .. }) = parent.get(3) {
                            ensure!(
                                identity != (*pid, *created),
                                "account birth reused a host bootstrap"
                            );
                        }
                    }
                    ensure!(
                        !processes.contains(&identity),
                        "paired account roles reused one original process"
                    );
                    processes.push(identity);
                }
                (
                    4,
                    Event::Acknowledged {
                        request,
                        pid,
                        created,
                        image_id,
                    },
                ) => {
                    ensure!(
                        events[3]
                            == Event::Born {
                                request: request.clone(),
                                pid: *pid,
                                created: *created,
                                image_id: *image_id,
                            },
                        "paired account acknowledgment differs from original birth"
                    );
                }
                _ => anyhow::bail!("paired account event is out of order"),
            }
        }
    }
    Ok(())
}

fn parent_inventory(sql: &Connection, plan: &Plan) -> Result<[Vec<super::Event>; 2]> {
    // Parent replay validates both role journals and retirement ordering. Never
    // call this from require_schema, which is used inside parent require_plan.
    Ok([
        super::replay(sql, plan, Role::Owner)?,
        super::replay(sql, plan, Role::Consumers)?,
    ])
}
fn inventory(sql: &Connection, plan: &Plan) -> Result<[Vec<Event>; 2]> {
    super::require_plan(sql, plan)?;
    require_schema(sql)?;
    let parents = parent_inventory(sql, plan)?;
    let (count, max, nontext): (u64, Option<u64>, u64) = sql.query_row(
        "SELECT count(*),max(length(CAST(event_json AS BLOB))),coalesce(sum(typeof(event_json)!='text'),0) FROM candidate_paired_account_events",
        [], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
    )?;
    ensure!(
        count <= 10 && max.unwrap_or(0) <= MAX_BYTES as u64 && nontext == 0,
        "paired account inventory exceeds bounds or type"
    );
    let mut statement = sql.prepare(
        "SELECT role,sequence,event_json FROM candidate_paired_account_events ORDER BY role,sequence",
    )?;
    let mut events = [Vec::new(), Vec::new()];
    for row in statement.query_map([], |r| {
        Ok((
            r.get::<_, usize>(0)?,
            r.get::<_, usize>(1)?,
            r.get::<_, String>(2)?,
        ))
    })? {
        let (role, sequence, json) = row?;
        ensure!(
            role < 2 && sequence == events[role].len(),
            "paired account role/sequence gap"
        );
        let event: Event = serde_json::from_str(&json)
            .map_err(|_| anyhow::anyhow!("malformed paired account event; preserve evidence"))?;
        ensure!(
            serde_json::from_str::<serde_json::Value>(&json)? == serde_json::to_value(&event)?,
            "paired account event has unexpected fields"
        );
        events[role].push(event);
    }
    validate_inventory(plan, &parents, &events)?;
    Ok(events)
}
pub(super) fn replay(sql: &Connection, plan: &Plan, role: Role) -> Result<Vec<Event>> {
    let mut events = inventory(sql, plan)?;
    Ok(std::mem::take(&mut events[role.index()]))
}
pub(super) fn append(sql: &Connection, plan: &Plan, role: Role, event: Event) -> Result<()> {
    let mut expected = inventory(sql, plan)?;
    let parents = parent_inventory(sql, plan)?;
    // Retired roles retain readable evidence, never permission to deliver a
    // credential or continue an uncertain native birth after retirement starts.
    ensure!(
        parents[role.index()].len() == 7,
        "paired account append requires live Ready phase"
    );
    let sequence = expected[role.index()].len();
    expected[role.index()].push(event.clone());
    validate_inventory(plan, &parents, &expected)?;
    let encoded = serde_json::to_string(&event)?;
    ensure!(
        encoded.len() <= MAX_BYTES,
        "paired account event exceeds bound"
    );
    let tx = sql.unchecked_transaction()?;
    tx.execute(
        "INSERT INTO candidate_paired_account_events VALUES(?1,?2,?3)",
        params![role.index(), sequence, encoded],
    )?;
    tx.commit()
        .context("paired account commit uncertain; preserve original state")?;
    ensure!(
        inventory(sql, plan)? == expected,
        "paired account readback differs"
    );
    Ok(())
}

#[cfg(test)]
#[path = "account_paired_account_journal_tests.rs"]
mod tests;
