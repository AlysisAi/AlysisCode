//! Native birth and trusted-bootstrap startup of the versioned single-job plan.
//! No account logon, cleanup receipt or cold ownership reconstruction.
//! Drop drains/closes original native guards and preserves all durable evidence.
use super::*;
use crate::{
    account_control::{bootstrap::SuspendedBootstrap, containment},
    job::Job,
};
use std::{cell::Cell, mem::size_of};
use windows::Win32::System::JobObjects::{
    JOBOBJECT_BASIC_ACCOUNTING_INFORMATION, JobObjectBasicAccountingInformation,
    QueryInformationJobObject,
};

#[path = "account_bootstrap_initializer.rs"]
pub(super) mod initializer;
#[cfg(test)]
pub(crate) use initializer::{INITIALIZER_CHECKPOINT, RPC_SELF_CHECK};
#[path = "account_bootstrap_resume.rs"]
pub(crate) mod resume;

const TABLE: &str = "candidate_atomic_bootstrap_events";
const MAX_EVENT_BYTES: usize = 1024;
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_atomic_bootstrap_events(sequence INTEGER PRIMARY KEY CHECK(sequence BETWEEN 0 AND 3), event_json TEXT NOT NULL)",
    "CREATE TRIGGER atomic_events_no_update BEFORE UPDATE ON candidate_atomic_bootstrap_events BEGIN SELECT RAISE(ABORT,'atomic bootstrap event is immutable'); END",
    "CREATE TRIGGER atomic_events_no_delete BEFORE DELETE ON candidate_atomic_bootstrap_events BEGIN SELECT RAISE(ABORT,'atomic bootstrap event is immutable'); END",
    "CREATE TRIGGER atomic_events_no_replace BEFORE INSERT ON candidate_atomic_bootstrap_events WHEN EXISTS(SELECT 1 FROM candidate_atomic_bootstrap_events WHERE sequence=NEW.sequence) BEGIN SELECT RAISE(ABORT,'atomic bootstrap event is immutable'); END",
];

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum Event {
    JobIntent {
        nonce: String,
    },
    JobCreated {
        nonce: String,
        policy: u32,
    },
    BootstrapIntent {
        nonce: String,
    },
    BootstrapCreated {
        nonce: String,
        pid: u32,
        created: u64,
        image_id: [u8; 24],
    },
}

pub(super) fn create_schema(connection: &Connection) -> Result<()> {
    for sql in SCHEMA {
        connection.execute_batch(sql)?;
    }
    resume::create_schema(connection)?;
    Ok(())
}
pub(super) fn require_schema(connection: &Connection) -> Result<()> {
    resume::require_schema(connection)?;
    let mut statement = connection.prepare("SELECT sql FROM sqlite_schema WHERE tbl_name=?1")?;
    let rows: Vec<String> = statement
        .query_map([TABLE], |r| r.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    ensure!(
        rows.len() == SCHEMA.len() && SCHEMA.iter().all(|sql| rows.iter().any(|r| r == sql)),
        "atomic bootstrap event schema differs; preserve recovery state"
    );
    Ok(())
}

fn validate_events(plan: &Plan, events: &[Event]) -> Result<()> {
    ensure!(
        events.len() <= 4,
        "atomic bootstrap event inventory exceeds bounds"
    );
    for (index, event) in events.iter().enumerate() {
        match (index, event) {
            (0, Event::JobIntent { nonce }) => validate_random_id(nonce)?,
            (1, Event::JobCreated { nonce, policy }) => {
                ensure!(
                    *policy == 1
                        && events[0]
                            == (Event::JobIntent {
                                nonce: nonce.clone()
                            }),
                    "atomic job receipt differs from its intent/policy"
                );
            }
            (2, Event::BootstrapIntent { nonce }) => {
                validate_random_id(nonce)?;
                ensure!(
                    events[0]
                        != (Event::JobIntent {
                            nonce: nonce.clone()
                        }),
                    "bootstrap reused the job operation"
                );
            }
            (
                3,
                Event::BootstrapCreated {
                    nonce,
                    pid,
                    created,
                    image_id,
                },
            ) => {
                ensure!(
                    events[2]
                        == (Event::BootstrapIntent {
                            nonce: nonce.clone()
                        })
                        && *pid != 0
                        && *pid != plan.execution.pid
                        && *created != 0
                        && *image_id == plan.image_id,
                    "atomic bootstrap receipt differs from its intent/original process"
                );
            }
            _ => anyhow::bail!("atomic bootstrap event is out of order"),
        }
    }
    Ok(())
}

fn replay(connection: &Connection, plan: &Plan) -> Result<Vec<Event>> {
    require_plan(connection, plan)?;
    let (count,max):(u64,Option<u64>)=connection.query_row(
        "SELECT count(*),max(length(CAST(event_json AS BLOB))) FROM candidate_atomic_bootstrap_events",[],|r|Ok((r.get(0)?,r.get(1)?)))?;
    ensure!(
        count <= 4 && max.unwrap_or(0) <= MAX_EVENT_BYTES as u64,
        "atomic bootstrap event inventory exceeds bounds"
    );
    let mut statement = connection.prepare(
        "SELECT sequence,event_json FROM candidate_atomic_bootstrap_events ORDER BY sequence",
    )?;
    let rows = statement.query_map([], |r| Ok((r.get::<_, u64>(0)?, r.get::<_, String>(1)?)))?;
    let mut events = Vec::new();
    for row in rows {
        let (sequence, json) = row?;
        ensure!(
            sequence == events.len() as u64,
            "atomic bootstrap event sequence has a gap"
        );
        events.push(
            serde_json::from_str(&json).context("malformed atomic bootstrap event; preserve")?,
        );
    }
    validate_events(plan, &events)?;
    Ok(events)
}

fn append(connection: &Connection, plan: &Plan, event: Event) -> Result<()> {
    let mut events = replay(connection, plan)?;
    let sequence = events.len();
    events.push(event.clone());
    validate_events(plan, &events)?;
    let encoded = serde_json::to_string(&event)?;
    ensure!(
        encoded.len() <= MAX_EVENT_BYTES,
        "atomic bootstrap event oversized"
    );
    let transaction = connection.unchecked_transaction()?;
    transaction.execute(
        "INSERT INTO candidate_atomic_bootstrap_events VALUES(?1,?2)",
        params![sequence, encoded],
    )?;
    #[cfg(test)]
    {
        let hook = AFTER_EVENT_INSERT.with(|slot| slot.borrow_mut().take());
        if let Some(hook) = hook {
            hook(&transaction);
        }
    }
    transaction
        .commit()
        .context("atomic bootstrap event commit uncertain; preserve intent and original guard")?;
    ensure!(
        replay(connection, plan)? == events,
        "atomic bootstrap event readback differs"
    );
    Ok(())
}

// One-shot instrumentation of the actual write-ahead transaction, absent from
// production builds. Crash fixtures flush dirty SQLite pages and are externally
// terminated before commit; no environment switch reaches the production path.
#[cfg(test)]
thread_local! {
    pub(crate) static AFTER_EVENT_INSERT: std::cell::RefCell<Option<Box<dyn FnOnce(&Connection)>>> = const { std::cell::RefCell::new(None) };
}

/// Owns only the freshly created original job. It exposes no raw handle or
/// assignment/resume method and cannot be deserialized from a name/receipt.
pub(crate) struct JournaledJob<'p, 'a, 'lease> {
    job: Job,
    pub(super) plan: &'p CommittedPlan<'a, 'lease>,
    nonce: String,
    birth_attempted: Cell<bool>,
}

impl<'a, 'lease> CommittedPlan<'a, 'lease> {
    pub(crate) fn create_job(&self) -> Result<JournaledJob<'_, 'a, 'lease>> {
        self.verify()?;
        ensure!(
            !self.job_attempted.replace(true),
            "atomic job creation already attempted"
        );
        let connection = self.database.connection()?;
        ensure!(
            replay(connection, &self.plan)?.is_empty(),
            "atomic job intent already exists"
        );
        let nonce = random_id()?;
        append(
            connection,
            &self.plan,
            Event::JobIntent {
                nonce: nonce.clone(),
            },
        )?;
        self.verify()?;
        let job = Job::for_execution(&self.plan.execution.identity.container)?;
        let guard = JournaledJob {
            job,
            plan: self,
            nonce,
            birth_attempted: Cell::new(false),
        };
        guard.verify_empty()?;
        Ok(guard)
    }
}

impl<'p, 'a, 'lease> JournaledJob<'p, 'a, 'lease> {
    pub(super) fn require_resource_birth(&self) -> Result<()> {
        self.verify_empty()?;
        ensure!(
            replay(self.plan.database.connection()?, &self.plan.plan)?
                == vec![
                    Event::JobIntent {
                        nonce: self.nonce.clone()
                    },
                    Event::JobCreated {
                        nonce: self.nonce.clone(),
                        policy: 1
                    },
                ],
            "namespace birth requires original freshly empty recorded job"
        );
        Ok(())
    }
    fn verify(&self) -> Result<()> {
        self.plan.verify()?;
        containment::prepare(&self.job)
    }
    pub(super) fn verify_empty(&self) -> Result<()> {
        self.verify()?;
        let mut accounting = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
        let mut returned = 0;
        unsafe {
            QueryInformationJobObject(
                Some(self.job.raw()),
                JobObjectBasicAccountingInformation,
                (&mut accounting as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
                size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
                Some(&mut returned),
            )?;
        }
        ensure!(
            returned as usize == size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>()
                && accounting.ActiveProcesses == 0
                && accounting.TotalProcesses == 0,
            "atomic bootstrap job is not a freshly empty original object"
        );
        Ok(())
    }
    /// May retry acknowledgment with the same live original job after an
    /// uncertain SQL commit; never repeats native creation or accepts a name.
    pub(crate) fn record_birth(&self) -> Result<()> {
        self.verify_empty()?;
        let connection = self.plan.database.connection()?;
        let event = Event::JobCreated {
            nonce: self.nonce.clone(),
            policy: 1,
        };
        let expected = vec![
            Event::JobIntent {
                nonce: self.nonce.clone(),
            },
            event.clone(),
        ];
        let events = replay(connection, &self.plan.plan)?;
        if events == expected {
            return Ok(());
        }
        ensure!(
            events == expected[..1],
            "atomic job acknowledgment differs from original creation"
        );
        append(connection, &self.plan.plan, event)
    }
    pub(crate) fn prepare_bootstrap(&self) -> Result<JournaledBootstrap<'_, 'p, 'a, 'lease>> {
        self.verify_empty()?;
        ensure!(
            !self.birth_attempted.replace(true),
            "atomic bootstrap creation already attempted"
        );
        let connection = self.plan.database.connection()?;
        ensure!(
            replay(connection, &self.plan.plan)?
                == vec![
                    Event::JobIntent {
                        nonce: self.nonce.clone()
                    },
                    Event::JobCreated {
                        nonce: self.nonce.clone(),
                        policy: 1
                    }
                ],
            "bootstrap requires exact committed original job birth"
        );
        let nonce = random_id()?;
        append(
            connection,
            &self.plan.plan,
            Event::BootstrapIntent {
                nonce: nonce.clone(),
            },
        )?;
        let mut intent = BirthIntent {
            owner: self,
            nonce: nonce.clone(),
            attempted: false,
        };
        let child = SuspendedBootstrap::prepare_recorded(&mut intent)?;
        child.durable_observation()?;
        Ok(JournaledBootstrap {
            child,
            owner: self,
            nonce,
        })
    }
}

/// Single-use live ticket; the suspended-birth adapter consumes it immediately
/// before effects. Serialized events cannot manufacture a ticket.
pub(crate) struct BirthIntent<'j, 'p, 'a, 'lease> {
    owner: &'j JournaledJob<'p, 'a, 'lease>,
    nonce: String,
    attempted: bool,
}
impl<'j> BirthIntent<'j, '_, '_, '_> {
    pub(crate) fn consume(
        &mut self,
    ) -> Result<(&'j PinnedRunnerImage, &'j Job, &'j ControlAddress)> {
        ensure!(
            !std::mem::replace(&mut self.attempted, true),
            "atomic bootstrap birth ticket already consumed"
        );
        self.owner.verify_empty()?;
        let plan = self.owner.plan;
        let events = replay(plan.database.connection()?, &plan.plan)?;
        ensure!(
            events
                == vec![
                    Event::JobIntent {
                        nonce: self.owner.nonce.clone()
                    },
                    Event::JobCreated {
                        nonce: self.owner.nonce.clone(),
                        policy: 1
                    },
                    Event::BootstrapIntent {
                        nonce: self.nonce.clone()
                    }
                ],
            "bootstrap write-ahead ticket no longer matches"
        );
        Ok((plan.image, &self.owner.job, plan.address))
    }
}

/// Child drops before its borrowed job and pinned image can be released. It
/// stays suspended after receipt commit; consuming start() records resume intent
/// before its single attempt and authenticates the exact original peer.
pub(crate) struct JournaledBootstrap<'j, 'p, 'a, 'lease> {
    child: SuspendedBootstrap<'j>,
    owner: &'j JournaledJob<'p, 'a, 'lease>,
    nonce: String,
}
impl JournaledBootstrap<'_, '_, '_, '_> {
    #[cfg(test)]
    pub(crate) fn fixture_readonly_observer(&self) -> Result<crate::util::OwnedHandle> {
        self.child.fixture_readonly_observer()
    }

    pub(crate) fn record_birth(&self) -> Result<()> {
        self.owner.verify()?;
        let (pid, created, image_id) = self.child.durable_observation()?;
        let plan = self.owner.plan;
        let connection = plan.database.connection()?;
        let event = Event::BootstrapCreated {
            nonce: self.nonce.clone(),
            pid,
            created,
            image_id,
        };
        let mut expected = vec![
            Event::JobIntent {
                nonce: self.owner.nonce.clone(),
            },
            Event::JobCreated {
                nonce: self.owner.nonce.clone(),
                policy: 1,
            },
            Event::BootstrapIntent {
                nonce: self.nonce.clone(),
            },
        ];
        let events = replay(connection, &plan.plan)?;
        if events == expected {
            return append(connection, &plan.plan, event);
        }
        expected.push(event);
        ensure!(
            events == expected,
            "bootstrap acknowledgment differs from original native child"
        );
        Ok(())
    }
}

#[cfg(test)]
#[path = "account_bootstrap_lifecycle_tests.rs"]
mod tests;
