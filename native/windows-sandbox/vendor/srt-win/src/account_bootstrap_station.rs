//! Shared station grant composed with the original namespace/job ownership.
//! Explicit restoration needs the original live namespace completion; errors
//! preserve unresolved grants and their durable evidence. Drop never restores.
use super::super::resources::station::{Station, proposed};
use super::*;
use crate::{account_control::ControlServer, acl::object_grants::Snapshot};
use lifecycle::resume::logon::delivery::birth::AccountBirthObservation;
use namespace::{CompletedNamespace, NamespacedJob};
use std::time::Duration;

const TABLE: &str = "candidate_atomic_station";
const MAX_EVENT: usize = 140000;
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_atomic_station(sequence INTEGER PRIMARY KEY CHECK(sequence BETWEEN 0 AND 3), event_json TEXT NOT NULL)",
    "CREATE TRIGGER atomic_station_no_update BEFORE UPDATE ON candidate_atomic_station BEGIN SELECT RAISE(ABORT,'atomic station event is immutable'); END",
    "CREATE TRIGGER atomic_station_no_delete BEFORE DELETE ON candidate_atomic_station BEGIN SELECT RAISE(ABORT,'atomic station event is immutable'); END",
    "CREATE TRIGGER atomic_station_no_replace BEFORE INSERT ON candidate_atomic_station WHEN EXISTS(SELECT 1 FROM candidate_atomic_station WHERE sequence=NEW.sequence) BEGIN SELECT RAISE(ABORT,'atomic station event is immutable'); END",
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
        "atomic station schema differs; preserve state"
    );
    Ok(())
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Binding {
    operation: String,
    nonce: String,
    session: u32,
    package_sid: String,
    policy: u32,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum Event {
    Intent {
        binding: Binding,
        before: Snapshot,
        after: Snapshot,
    },
    Applied {
        binding: Binding,
    },
    RestoreIntent {
        binding: Binding,
        retirement: String,
    },
    Restored {
        binding: Binding,
        retirement: String,
    },
}
fn validate(sql: &Connection, plan: &Plan, events: &[Event]) -> Result<()> {
    ensure!(events.len() <= 4, "atomic station inventory exceeds bound");
    if events.is_empty() {
        return Ok(());
    }
    let Event::Intent {
        binding,
        before,
        after,
    } = &events[0]
    else {
        anyhow::bail!("atomic station intent missing");
    };
    validate_random_id(&binding.nonce)?;
    ensure!(
        binding.operation == plan.operation
            && binding.nonce != plan.operation
            && binding.session == plan.session
            && binding.package_sid == plan.execution.identity.package_sid
            && binding.policy == 1,
        "atomic station binding differs from plan"
    );
    ensure!(
        proposed(before, &binding.package_sid)? == *after,
        "atomic station proposal differs from exact package policy"
    );
    if let Some(event) = events.get(1) {
        ensure!(
            *event
                == Event::Applied {
                    binding: binding.clone()
                },
            "atomic station applied receipt differs"
        );
    }
    if let Some(event) = events.get(2) {
        let nonce = namespace::retired_nonce(sql, plan)?;
        ensure!(
            nonce != binding.nonce
                && *event
                    == Event::RestoreIntent {
                        binding: binding.clone(),
                        retirement: nonce.clone()
                    },
            "station restoration lacks exact completed namespace/job"
        );
        if let Some(event) = events.get(3) {
            ensure!(
                *event
                    == Event::Restored {
                        binding: binding.clone(),
                        retirement: nonce
                    },
                "station restored receipt differs"
            );
        }
    }
    Ok(())
}
fn replay(sql: &Connection, plan: &Plan) -> Result<Vec<Event>> {
    require_plan(sql, plan)?;
    let (count, max): (usize, Option<usize>) = sql.query_row(
        "SELECT count(*),max(length(CAST(event_json AS BLOB))) FROM candidate_atomic_station",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    ensure!(
        count <= 4 && max.unwrap_or(0) <= MAX_EVENT,
        "atomic station inventory exceeds bound"
    );
    let mut s =
        sql.prepare("SELECT sequence,event_json FROM candidate_atomic_station ORDER BY sequence")?;
    let mut events = Vec::new();
    for row in s.query_map([], |r| Ok((r.get::<_, usize>(0)?, r.get::<_, String>(1)?)))? {
        let (sequence, bytes) = row?;
        ensure!(
            sequence == events.len(),
            "atomic station sequence has a gap"
        );
        events.push(serde_json::from_str(&bytes).context("malformed atomic station event")?);
    }
    validate(sql, plan, &events)?;
    Ok(events)
}
// Diagnostic binding only; a saved nonce is never restoration authority.
pub(super) fn grant_nonce(sql: &Connection, plan: &Plan) -> Result<String> {
    let events = replay(sql, plan)?;
    ensure!(events.len() >= 2, "station grant is not recorded");
    let Event::Intent { binding, .. } = &events[0] else {
        anyhow::bail!("station intent absent")
    };
    Ok(binding.nonce.clone())
}
fn append(sql: &Connection, plan: &Plan, event: Event) -> Result<()> {
    let mut expected = replay(sql, plan)?;
    let sequence = expected.len();
    expected.push(event.clone());
    validate(sql, plan, &expected)?;
    let bytes = serde_json::to_string(&event)?;
    ensure!(
        bytes.len() <= MAX_EVENT,
        "atomic station event exceeds bound"
    );
    let tx = sql.unchecked_transaction()?;
    tx.execute(
        "INSERT INTO candidate_atomic_station VALUES(?1,?2)",
        params![sequence, bytes],
    )?;
    tx.commit()
        .context("atomic station commit uncertain; preserve grant and pending state")?;
    ensure!(
        replay(sql, plan)? == expected,
        "atomic station readback differs"
    );
    Ok(())
}
/// Field order drains the job/releases namespace references before releasing
/// the original station handle on every error. No rollback is hidden in Drop.
pub(crate) struct StationJob<'p, 'a, 'lease> {
    job: Option<NamespacedJob<'p, 'a, 'lease>>,
    station: OwnedStation<'p, 'a, 'lease>,
}
pub(super) struct OwnedStation<'p, 'a, 'lease> {
    native: Station,
    plan: &'p CommittedPlan<'a, 'lease>,
    binding: Binding,
    before: Snapshot,
    after: Snapshot,
}
impl<'p, 'a, 'lease> NamespacedJob<'p, 'a, 'lease> {
    pub(crate) fn with_station(self) -> Result<StationJob<'p, 'a, 'lease>> {
        self.require_before_bootstrap()?;
        let plan = self.plan();
        let sql = plan.database.connection()?;
        ensure!(
            replay(sql, &plan.plan)?.is_empty(),
            "atomic station grant already attempted"
        );
        let (native, before, after) = Station::prepare(&plan.plan.execution.identity.package_sid)?;
        let binding = Binding {
            operation: plan.plan.operation.clone(),
            nonce: random_id()?,
            session: plan.plan.session,
            package_sid: plan.plan.execution.identity.package_sid.clone(),
            policy: 1,
        };
        let mut bundle = StationJob {
            job: Some(self),
            station: OwnedStation {
                native,
                plan,
                binding: binding.clone(),
                before: before.clone(),
                after: after.clone(),
            },
        };
        append(
            sql,
            &plan.plan,
            Event::Intent {
                binding: binding.clone(),
                before,
                after,
            },
        )?;
        #[cfg(test)]
        bundle.station.checkpoint("intent")?;
        bundle.job.as_ref().unwrap().require_before_bootstrap()?;
        bundle
            .station
            .native
            .write_exact(&bundle.station.before, &bundle.station.after)?;
        #[cfg(test)]
        bundle.station.checkpoint("applied")?;
        bundle.station.verify(&bundle.station.after)?;
        append(sql, &plan.plan, Event::Applied { binding })?;
        #[cfg(test)]
        bundle.station.checkpoint("ready")?;
        bundle.station.verify(&bundle.station.after)?;
        Ok(bundle)
    }
}
impl<'p, 'a, 'lease> StationJob<'p, 'a, 'lease> {
    pub(super) fn plan(&self) -> &'p CommittedPlan<'a, 'lease> {
        self.station.plan
    }
    pub(super) fn require_before_bootstrap(&self) -> Result<()> {
        self.job
            .as_ref()
            .context("original station job absent")?
            .require_before_bootstrap()?;
        self.station.verify_applied()
    }
    pub(crate) fn observe_account_birth(
        self,
        server: ControlServer,
        timeout: Duration,
    ) -> Result<AccountBirthObservation> {
        self.observe(
            server,
            timeout,
            #[cfg(test)]
            None,
        )
    }
    #[cfg(test)]
    pub(crate) fn fixture_observe_account_birth(
        self,
        server: ControlServer,
        password: &str,
        timeout: Duration,
    ) -> Result<AccountBirthObservation> {
        self.observe(server, timeout, Some(password))
    }
    fn observe(
        self,
        server: ControlServer,
        timeout: Duration,
        #[cfg(test)] password: Option<&str>,
    ) -> Result<AccountBirthObservation> {
        let (observation, completed) = self.observe_before_station_restore(
            None,
            server,
            timeout,
            #[cfg(test)]
            password,
        )?;
        completed.restore()?;
        Ok(observation)
    }
    pub(super) fn observe_before_station_restore(
        mut self,
        desktop: Option<&desktop::OwnedDesktop<'p, 'a, 'lease>>,
        server: ControlServer,
        timeout: Duration,
        #[cfg(test)] password: Option<&str>,
    ) -> Result<(AccountBirthObservation, StationRestoration<'p, 'a, 'lease>)> {
        self.station.verify_applied()?;
        let job = self.job.take().context("missing original station job")?;
        let (observation, completed) = job.observe_completed(
            desktop.map(|d| (d, &self.station)),
            server,
            timeout,
            #[cfg(test)]
            password,
        )?;
        Ok((
            observation,
            StationRestoration {
                station: self.station,
                completed,
            },
        ))
    }
}
/// Retains the original station grant and live same-plan namespace/job
/// completion. It cannot be constructed from a cold diagnostic receipt.
pub(super) struct StationRestoration<'p, 'a, 'lease> {
    station: OwnedStation<'p, 'a, 'lease>,
    completed: CompletedNamespace<'p, 'a, 'lease>,
}
impl StationRestoration<'_, '_, '_> {
    pub(super) fn verify_for(&self, plan: &CommittedPlan<'_, '_>) -> Result<&str> {
        ensure!(
            std::ptr::eq(self.station.plan, plan),
            "station completion belongs to another live plan"
        );
        self.station.verify_applied()?;
        self.completed.verify_for(plan)
    }
    pub(super) fn restore(mut self) -> Result<()> {
        self.station.restore(&self.completed)
    }
}
impl OwnedStation<'_, '_, '_> {
    fn verify_applied(&self) -> Result<()> {
        self.verify(&self.after)?;
        ensure!(
            replay(self.plan.database.connection()?, &self.plan.plan)?
                == vec![
                    Event::Intent {
                        binding: self.binding.clone(),
                        before: self.before.clone(),
                        after: self.after.clone()
                    },
                    Event::Applied {
                        binding: self.binding.clone()
                    },
                ],
            "original station grant receipts differ"
        );
        Ok(())
    }
    fn verify(&self, expected: &Snapshot) -> Result<()> {
        self.plan.verify()?;
        self.native.verify_current()?;
        ensure!(
            self.native.read()? == *expected,
            "atomic station descriptor changed; do not overwrite"
        );
        Ok(())
    }
    fn restore(&mut self, completed: &CompletedNamespace<'_, '_, '_>) -> Result<()> {
        let nonce = completed.verify_for(self.plan)?.to_owned();
        let sql = self.plan.database.connection()?;
        ensure!(
            replay(sql, &self.plan.plan)?
                == vec![
                    Event::Intent {
                        binding: self.binding.clone(),
                        before: self.before.clone(),
                        after: self.after.clone()
                    },
                    Event::Applied {
                        binding: self.binding.clone()
                    },
                ],
            "station restoration requires original exact grant receipts"
        );
        self.verify(&self.after)?;
        append(
            sql,
            &self.plan.plan,
            Event::RestoreIntent {
                binding: self.binding.clone(),
                retirement: nonce.clone(),
            },
        )?;
        #[cfg(test)]
        self.checkpoint("restoring")?;
        completed.verify_for(self.plan)?;
        self.verify(&self.after)?;
        self.native.write_exact(&self.after, &self.before)?;
        #[cfg(test)]
        self.checkpoint("restored")?;
        self.verify(&self.before)?;
        completed.verify_for(self.plan)?;
        append(
            sql,
            &self.plan.plan,
            Event::Restored {
                binding: self.binding.clone(),
                retirement: nonce,
            },
        )
    }
    #[cfg(test)]
    fn checkpoint(&self, phase: &str) -> Result<()> {
        STATION_CHECKPOINT.with(|s| {
            if let Some(hook) = s.borrow_mut().as_mut() {
                hook(phase, self.plan.database.connection()?)?;
            }
            Ok(())
        })
    }
}
#[cfg(test)]
thread_local! {
    pub(crate) static STATION_CHECKPOINT:std::cell::RefCell<Option<Box<dyn FnMut(&str,&Connection)->Result<()>>>>=const{std::cell::RefCell::new(None)};
}
#[cfg(test)]
#[path = "account_bootstrap_station_tests.rs"]
mod tests;

impl OwnedStation<'_, '_, '_> {
    pub(super) fn require_initializer(&self, plan: &CommittedPlan<'_, '_>) -> Result<()> {
        ensure!(
            std::ptr::eq(self.plan, plan),
            "initializer station belongs to another plan"
        );
        self.verify_applied()
    }
}
