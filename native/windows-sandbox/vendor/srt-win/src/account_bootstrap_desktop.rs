//! Atomic ownership of an unused bootstrap desktop. It is kept through original
//! account birth/job drain, closed before station restoration, and never handed
//! to an account child yet. Original handles and live completion are mandatory.
use super::*;
use crate::account_control::ControlServer;
use desktop_native::NativeDesktop;
use lifecycle::resume::logon::delivery::birth::AccountBirthObservation;
use station::{StationJob, StationRestoration};
use std::time::Duration;

const TABLE: &str = "candidate_atomic_desktop";
const MAX_EVENT: usize = 4096;
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_atomic_desktop(sequence INTEGER PRIMARY KEY CHECK(sequence BETWEEN 0 AND 3), event_json TEXT NOT NULL)",
    "CREATE TRIGGER atomic_desktop_no_update BEFORE UPDATE ON candidate_atomic_desktop BEGIN SELECT RAISE(ABORT,'atomic desktop event is immutable'); END",
    "CREATE TRIGGER atomic_desktop_no_delete BEFORE DELETE ON candidate_atomic_desktop BEGIN SELECT RAISE(ABORT,'atomic desktop event is immutable'); END",
    "CREATE TRIGGER atomic_desktop_no_replace BEFORE INSERT ON candidate_atomic_desktop WHEN EXISTS(SELECT 1 FROM candidate_atomic_desktop WHERE sequence=NEW.sequence) BEGIN SELECT RAISE(ABORT,'atomic desktop event is immutable'); END",
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
        "atomic desktop schema differs; preserve state"
    );
    Ok(())
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Binding {
    operation: String,
    nonce: String,
    station_nonce: String,
    session: u32,
    name: String,
    package_sid: String,
    policy: u32,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum Event {
    Intent {
        binding: Binding,
    },
    Created {
        binding: Binding,
    },
    CloseIntent {
        binding: Binding,
        retirement: String,
    },
    Closed {
        binding: Binding,
        retirement: String,
    },
}
fn validate(sql: &Connection, plan: &Plan, events: &[Event]) -> Result<()> {
    ensure!(events.len() <= 4, "desktop inventory exceeds bound");
    if events.is_empty() {
        return Ok(());
    }
    let Event::Intent { binding } = &events[0] else {
        anyhow::bail!("desktop intent absent")
    };
    validate_random_id(&binding.nonce)?;
    ensure!(
        binding.operation == plan.operation
            && binding.session == plan.session
            && binding.package_sid == plan.execution.identity.package_sid
            && binding.name == desktop_native::name(plan)?
            && binding.policy == 1
            && binding.station_nonce == station::grant_nonce(sql, plan)?
            && binding.nonce != binding.operation
            && binding.nonce != binding.station_nonce,
        "desktop binding differs from original plan/station grant"
    );
    if let Some(event) = events.get(1) {
        ensure!(
            *event
                == Event::Created {
                    binding: binding.clone()
                },
            "desktop birth receipt differs"
        );
    }
    if let Some(event) = events.get(2) {
        let retirement = namespace::retired_nonce(sql, plan)?;
        ensure!(
            retirement != binding.nonce
                && *event
                    == Event::CloseIntent {
                        binding: binding.clone(),
                        retirement: retirement.clone()
                    },
            "desktop closure lacks original completed job/namespace"
        );
        if let Some(event) = events.get(3) {
            ensure!(
                *event
                    == Event::Closed {
                        binding: binding.clone(),
                        retirement
                    },
                "desktop close receipt differs"
            );
        }
    }
    Ok(())
}
fn replay(sql: &Connection, plan: &Plan) -> Result<Vec<Event>> {
    require_plan(sql, plan)?;
    let (count, max): (usize, Option<usize>) = sql.query_row(
        "SELECT count(*),max(length(CAST(event_json AS BLOB))) FROM candidate_atomic_desktop",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    ensure!(
        count <= 4 && max.unwrap_or(0) <= MAX_EVENT,
        "desktop inventory exceeds bound"
    );
    let mut s =
        sql.prepare("SELECT sequence,event_json FROM candidate_atomic_desktop ORDER BY sequence")?;
    let mut events = Vec::new();
    for row in s.query_map([], |r| Ok((r.get::<_, usize>(0)?, r.get::<_, String>(1)?)))? {
        let (sequence, bytes) = row?;
        ensure!(sequence == events.len(), "desktop event sequence has a gap");
        events.push(serde_json::from_str(&bytes).context("malformed desktop event")?);
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
    ensure!(bytes.len() <= MAX_EVENT, "desktop event exceeds bound");
    let tx = sql.unchecked_transaction()?;
    tx.execute(
        "INSERT INTO candidate_atomic_desktop VALUES(?1,?2)",
        params![sequence, bytes],
    )?;
    tx.commit()
        .context("desktop commit uncertain; preserve original references and evidence")?;
    ensure!(
        replay(sql, plan)? == expected,
        "desktop event readback differs"
    );
    Ok(())
}
// Field order drains the original job before releasing this desktop on errors.
pub(crate) struct DesktopJob<'p, 'a, 'lease> {
    job: Option<StationJob<'p, 'a, 'lease>>,
    desktop: OwnedDesktop<'p, 'a, 'lease>,
}
pub(super) struct OwnedDesktop<'p, 'a, 'lease> {
    native: NativeDesktop,
    plan: &'p CommittedPlan<'a, 'lease>,
    binding: Binding,
}
impl<'p, 'a, 'lease> StationJob<'p, 'a, 'lease> {
    pub(crate) fn with_unused_desktop(self) -> Result<DesktopJob<'p, 'a, 'lease>> {
        self.require_before_bootstrap()?;
        let plan = self.plan();
        let sql = plan.database.connection()?;
        ensure!(
            replay(sql, &plan.plan)?.is_empty(),
            "desktop birth already attempted"
        );
        let binding = Binding {
            operation: plan.plan.operation.clone(),
            nonce: random_id()?,
            station_nonce: station::grant_nonce(sql, &plan.plan)?,
            session: plan.plan.session,
            name: desktop_native::name(&plan.plan)?,
            package_sid: plan.plan.execution.identity.package_sid.clone(),
            policy: 1,
        };
        append(
            sql,
            &plan.plan,
            Event::Intent {
                binding: binding.clone(),
            },
        )?;
        self.require_before_bootstrap()?;
        let native = NativeDesktop::create(&plan.plan)?;
        let bundle = DesktopJob {
            job: Some(self),
            desktop: OwnedDesktop {
                native,
                plan,
                binding: binding.clone(),
            },
        };
        #[cfg(test)]
        bundle.desktop.checkpoint("born")?;
        bundle.desktop.verify_open()?;
        append(sql, &plan.plan, Event::Created { binding })?;
        #[cfg(test)]
        bundle.desktop.checkpoint("ready")?;
        bundle.desktop.verify_open()?;
        Ok(bundle)
    }
}
impl DesktopJob<'_, '_, '_> {
    pub(crate) fn observe_account_birth(
        self,
        server: ControlServer,
        timeout: Duration,
    ) -> Result<AccountBirthObservation> {
        self.observe(
            false,
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
        self.observe(false, server, timeout, Some(password))
    }
    // Qualification entry uses the same guarded implementation. Production
    // account command entry remains disabled independently of this operation.
    #[cfg(test)]
    pub(crate) fn fixture_observe_initialized_account_birth(
        self,
        server: ControlServer,
        password: &str,
        timeout: Duration,
    ) -> Result<AccountBirthObservation> {
        self.observe(true, server, timeout, Some(password))
    }
    fn observe(
        mut self,
        initialize: bool,
        server: ControlServer,
        timeout: Duration,
        #[cfg(test)] password: Option<&str>,
    ) -> Result<AccountBirthObservation> {
        self.desktop.verify_open()?;
        ensure!(
            replay(
                self.desktop.plan.database.connection()?,
                &self.desktop.plan.plan
            )? == vec![
                Event::Intent {
                    binding: self.desktop.binding.clone()
                },
                Event::Created {
                    binding: self.desktop.binding.clone()
                }
            ],
            "desktop birth receipts differ before observation"
        );
        let job = self.job.take().context("original desktop job missing")?;
        let (observation, completed) = job.observe_before_station_restore(
            if initialize {
                Some(&self.desktop)
            } else {
                None
            },
            server,
            timeout,
            #[cfg(test)]
            password,
        )?;
        self.desktop.retire(&completed)?;
        completed.restore()?;
        self.desktop.native.verify_closed()?;
        Ok(observation)
    }
}
impl OwnedDesktop<'_, '_, '_> {
    fn verify_open(&self) -> Result<()> {
        self.plan.verify()?;
        self.native.verify()
    }
    fn retire(&mut self, completed: &StationRestoration<'_, '_, '_>) -> Result<()> {
        let retirement = completed.verify_for(self.plan)?.to_owned();
        let sql = self.plan.database.connection()?;
        ensure!(
            replay(sql, &self.plan.plan)?
                == vec![
                    Event::Intent {
                        binding: self.binding.clone()
                    },
                    Event::Created {
                        binding: self.binding.clone()
                    }
                ],
            "desktop closure requires original birth receipts"
        );
        self.verify_open()?;
        append(
            sql,
            &self.plan.plan,
            Event::CloseIntent {
                binding: self.binding.clone(),
                retirement: retirement.clone(),
            },
        )?;
        #[cfg(test)]
        self.checkpoint("closing")?;
        completed.verify_for(self.plan)?;
        self.verify_open()?;
        self.native.close()?;
        #[cfg(test)]
        self.checkpoint("closed")?;
        self.native.verify_closed()?;
        completed.verify_for(self.plan)?;
        append(
            sql,
            &self.plan.plan,
            Event::Closed {
                binding: self.binding.clone(),
                retirement,
            },
        )?;
        #[cfg(test)]
        self.checkpoint("completed")?;
        self.native.verify_closed()
    }
    #[cfg(test)]
    fn checkpoint(&self, phase: &str) -> Result<()> {
        DESKTOP_CHECKPOINT.with(|s| {
            if let Some(hook) = s.borrow_mut().as_mut() {
                hook(phase, self.plan.database.connection()?)?;
            }
            Ok(())
        })
    }
}
#[cfg(test)]
thread_local! {pub(crate) static DESKTOP_CHECKPOINT:std::cell::RefCell<Option<Box<dyn FnMut(&str,&Connection)->Result<()>>>>=const {std::cell::RefCell::new(None)};}
#[cfg(test)]
#[path = "account_bootstrap_desktop_tests.rs"]
mod tests;

pub(super) fn birth_nonce(sql: &Connection, plan: &Plan) -> Result<String> {
    let events = replay(sql, plan)?;
    let Some(Event::Created { binding }) = events.get(1) else {
        anyhow::bail!("initializer desktop birth missing");
    };
    Ok(binding.nonce.clone())
}
impl OwnedDesktop<'_, '_, '_> {
    pub(super) fn initializer_name(&self, plan: &CommittedPlan<'_, '_>) -> Result<String> {
        ensure!(
            std::ptr::eq(self.plan, plan),
            "initializer desktop belongs to another plan"
        );
        self.verify_open()?;
        ensure!(
            replay(plan.database.connection()?, &plan.plan)?.len() == 2,
            "initializer desktop is not in original open phase"
        );
        Ok(format!("WinSta0\\{}", self.binding.name))
    }
}
