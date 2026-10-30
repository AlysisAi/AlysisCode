//! Atomic ownership of the original bootstrap desktop. The fixed initializer
//! may use it while retained through both jobs; it closes before restoration.
//! No account child uses it yet. Original handles and completion are mandatory.
use super::*;
use crate::account_state::execution::bootstrap_plan::desktop_native::{self, NativeDesktop};

const TABLE: &str = "candidate_paired_desktop";
const MAX_EVENT: usize = 4096;
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_paired_desktop(sequence INTEGER PRIMARY KEY CHECK(sequence BETWEEN 0 AND 3), event_json TEXT NOT NULL)",
    "CREATE TRIGGER paired_desktop_no_update BEFORE UPDATE ON candidate_paired_desktop BEGIN SELECT RAISE(ABORT,'paired desktop event is immutable'); END",
    "CREATE TRIGGER paired_desktop_no_delete BEFORE DELETE ON candidate_paired_desktop BEGIN SELECT RAISE(ABORT,'paired desktop event is immutable'); END",
    "CREATE TRIGGER paired_desktop_no_replace BEFORE INSERT ON candidate_paired_desktop WHEN EXISTS(SELECT 1 FROM candidate_paired_desktop WHERE sequence=NEW.sequence) BEGIN SELECT RAISE(ABORT,'paired desktop event is immutable'); END",
];
pub(in crate::account_state::execution::paired_plan) fn create_schema(
    sql: &Connection,
) -> Result<()> {
    for s in SCHEMA {
        sql.execute_batch(s)?;
    }
    Ok(())
}
pub(in crate::account_state::execution::paired_plan) fn require_schema(
    sql: &Connection,
) -> Result<()> {
    let mut s = sql.prepare("SELECT sql FROM sqlite_schema WHERE tbl_name=?1")?;
    let rows: Vec<String> = s
        .query_map([TABLE], |r| r.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    ensure!(
        rows.len() == SCHEMA.len() && SCHEMA.iter().all(|s| rows.iter().any(|r| r == s)),
        "paired desktop schema differs; preserve state"
    );
    Ok(())
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Binding {
    operation: String,
    nonce: String,
    station_nonce: String,
    namespace: String,
    profile: Option<host_profile::ResourceBinding>,
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
        retirements: [String; 2],
    },
    Closed {
        binding: Binding,
        retirements: [String; 2],
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
    super::super::require_new_nonce(plan, &binding.nonce)?;
    ensure!(
        binding.namespace == super::super::birth_nonce(sql, plan)?
            && binding.nonce != binding.namespace,
        "desktop namespace binding differs"
    );
    ensure!(
        binding.profile == host_profile::resource_binding(sql, plan)?,
        "desktop profile chain differs"
    );
    if let Some(profile) = &binding.profile {
        ensure!(
            binding.nonce != profile.birth && profile.use_nonce.as_ref() != Some(&binding.nonce),
            "desktop nonce aliases profile phase"
        );
    }
    ensure!(
        binding.operation == plan.operation
            && binding.session == plan.session
            && binding.package_sid == plan.execution.identity.package_sid
            && binding.name == desktop_native::paired_name(plan)?
            && binding.policy == 1
            && binding.station_nonce == super::grant_nonce(sql, plan)?
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
        let retirements = super::retired_namespace(sql, plan)?;
        ensure!(
            retirements.iter().all(|n| n != &binding.nonce)
                && *event
                    == Event::CloseIntent {
                        binding: binding.clone(),
                        retirements: retirements.clone()
                    },
            "desktop closure lacks original completed job/namespace"
        );
        if let Some(event) = events.get(3) {
            ensure!(
                *event
                    == Event::Closed {
                        binding: binding.clone(),
                        retirements
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
        "SELECT count(*),max(length(CAST(event_json AS BLOB))) FROM candidate_paired_desktop",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    ensure!(
        count <= 4 && max.unwrap_or(0) <= MAX_EVENT,
        "desktop inventory exceeds bound"
    );
    let mut s =
        sql.prepare("SELECT sequence,event_json FROM candidate_paired_desktop ORDER BY sequence")?;
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
        "INSERT INTO candidate_paired_desktop VALUES(?1,?2)",
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
/// Diagnostic binding only; it cannot reconstruct the original desktop.
pub(super) fn birth_nonce(sql: &Connection, plan: &Plan) -> Result<String> {
    let events = replay(sql, plan)?;
    let Some(Event::Created { binding }) = events.get(1) else {
        anyhow::bail!("paired initializer requires original desktop birth");
    };
    Ok(binding.nonce.clone())
}

/// An original native desktop; only the intact StationPair can own one.
pub(super) struct OwnedDesktop<'p, 'a, 'lease> {
    native: NativeDesktop,
    plan: &'p CommittedPlan<'a, 'lease>,
    binding: Binding,
    close_attempted: bool,
}
impl<'p, 'a, 'lease> StationPair<'p, 'a, 'lease> {
    pub(crate) fn with_unused_desktop(mut self) -> Result<Self> {
        self.require_open()?;
        let pair = self.pair.as_ref().context("original pair missing")?;
        pair.jobs.require_namespace_birth()?;
        ensure!(
            self.desktop.is_none() && self.station.desktop_nonce.is_none(),
            "desktop birth already attempted"
        );
        let plan = self.station.plan;
        let sql = plan.database.connection()?;
        ensure!(
            replay(sql, &plan.plan)?.is_empty(),
            "desktop journal already used"
        );
        let binding = Binding {
            operation: plan.plan.operation.clone(),
            nonce: random_id()?,
            station_nonce: super::grant_nonce(sql, &plan.plan)?,
            namespace: super::super::birth_nonce(sql, &plan.plan)?,
            profile: host_profile::resource_binding(sql, &plan.plan)?,
            session: plan.plan.session,
            name: desktop_native::paired_name(&plan.plan)?,
            package_sid: plan.plan.execution.identity.package_sid.clone(),
            policy: 1,
        };
        // Set before intent. A failed attempt must never become the no-desktop
        // restoration path, even if native creation/close did not finish.
        self.station.desktop_nonce = Some(binding.nonce.clone());
        checkpoint("prepared", sql, None)?;
        append(
            sql,
            &plan.plan,
            Event::Intent {
                binding: binding.clone(),
            },
        )?;
        self.station.verify_applied()?;
        self.pair.as_ref().unwrap().require_open()?;
        self.pair.as_ref().unwrap().jobs.require_namespace_birth()?;
        let native = NativeDesktop::create_paired(&plan.plan)?;
        self.desktop = Some(OwnedDesktop {
            native,
            plan,
            binding: binding.clone(),
            close_attempted: false,
        });
        let desktop = self.desktop.as_ref().unwrap();
        checkpoint("born", sql, Some(&desktop.native))?;
        desktop.verify_open(&self.station)?;
        append(sql, &plan.plan, Event::Created { binding })?;
        checkpoint("ready", sql, Some(&desktop.native))?;
        self.require_open()?;
        Ok(self)
    }
}
impl OwnedDesktop<'_, '_, '_> {
    fn birth_events(&self) -> Vec<Event> {
        vec![
            Event::Intent {
                binding: self.binding.clone(),
            },
            Event::Created {
                binding: self.binding.clone(),
            },
        ]
    }
    fn verify_binding(&self, station: &OwnedStation<'_, '_, '_>) -> Result<()> {
        ensure!(
            std::ptr::eq(self.plan, station.plan)
                && station.desktop_nonce.as_ref() == Some(&self.binding.nonce)
                && station.binding.nonce == self.binding.station_nonce
                && station.binding.namespace == self.binding.namespace
                && station.binding.profile == self.binding.profile,
            "desktop does not belong to original station/pair"
        );
        self.plan.verify()
    }
    fn verify_open(&self, station: &OwnedStation<'_, '_, '_>) -> Result<()> {
        self.verify_binding(station)?;
        ensure!(!self.close_attempted, "desktop close already attempted");
        self.native.verify()
    }
    pub(super) fn verify_ready(&self, station: &OwnedStation<'_, '_, '_>) -> Result<()> {
        self.verify_open(station)?;
        ensure!(
            replay(self.plan.database.connection()?, &self.plan.plan)? == self.birth_events(),
            "original desktop birth receipts differ"
        );
        Ok(())
    }
    pub(super) fn retire(
        &mut self,
        completed: &CompletedNamespacedPair<'_, '_, '_>,
        station: &OwnedStation<'_, '_, '_>,
    ) -> Result<()> {
        ensure!(
            !std::mem::replace(&mut self.close_attempted, true),
            "desktop close already attempted"
        );
        self.verify_binding(station)?;
        self.native.verify()?;
        ensure!(
            replay(self.plan.database.connection()?, &self.plan.plan)? == self.birth_events(),
            "original desktop birth receipts differ"
        );
        station.verify_applied()?;
        station.verify_completed(completed)?;
        let sql = self.plan.database.connection()?;
        checkpoint("before-close", sql, Some(&self.native))?;
        self.native.verify()?;
        let retirements = super::retired_namespace(sql, &self.plan.plan)?;
        append(
            sql,
            &self.plan.plan,
            Event::CloseIntent {
                binding: self.binding.clone(),
                retirements: retirements.clone(),
            },
        )?;
        checkpoint("closing", sql, Some(&self.native))?;
        station.verify_applied()?;
        station.verify_completed(completed)?;
        self.native.close()?;
        checkpoint("closed", sql, Some(&self.native))?;
        self.native.verify_closed()?;
        station.verify_applied()?;
        station.verify_completed(completed)?;
        append(
            sql,
            &self.plan.plan,
            Event::Closed {
                binding: self.binding.clone(),
                retirements,
            },
        )?;
        checkpoint("completed", sql, Some(&self.native))?;
        self.verify_retired(completed, station)
    }
    pub(super) fn verify_retired(
        &self,
        completed: &CompletedNamespacedPair<'_, '_, '_>,
        station: &OwnedStation<'_, '_, '_>,
    ) -> Result<()> {
        self.verify_binding(station)?;
        station.verify_completed(completed)?;
        ensure!(self.close_attempted, "desktop not closed by original owner");
        self.native.verify_closed()?;
        let retirements =
            super::retired_namespace(self.plan.database.connection()?, &self.plan.plan)?;
        let mut expected = self.birth_events();
        expected.push(Event::CloseIntent {
            binding: self.binding.clone(),
            retirements: retirements.clone(),
        });
        expected.push(Event::Closed {
            binding: self.binding.clone(),
            retirements,
        });
        ensure!(
            replay(self.plan.database.connection()?, &self.plan.plan)? == expected,
            "desktop completion receipt differs"
        );
        Ok(())
    }
}
fn checkpoint(phase: &str, sql: &Connection, native: Option<&NativeDesktop>) -> Result<()> {
    #[cfg(test)]
    DESKTOP_CHECKPOINT.with(|s| -> Result<()> {
        if let Some(hook) = s.borrow_mut().as_mut() {
            hook(phase, sql, native)?;
        }
        Ok(())
    })?;
    #[cfg(not(test))]
    let _ = (phase, sql, native);
    Ok(())
}
#[cfg(test)]
thread_local! {pub(crate) static DESKTOP_CHECKPOINT:std::cell::RefCell<Option<Box<dyn FnMut(&str,&Connection,Option<&NativeDesktop>)->Result<()>>>>=const{std::cell::RefCell::new(None)};}
#[cfg(test)]
pub(crate) fn fixture_require_absent(plan: &CommittedPlan<'_, '_>) -> Result<()> {
    plan.verify()?;
    desktop_native::fixture_paired_absent(&plan.plan)
}
/// Read-only post-crash observation; it never restores a station or creates a
/// desktop/completion guard from the serialized plan.
#[cfg(test)]
pub(in crate::account_state::execution) fn fixture_require_absent_plan(plan: &Plan) -> Result<()> {
    desktop_native::fixture_paired_absent_from_controller(plan)
}
#[cfg(test)]
pub(in crate::account_state::execution) fn fixture_foreign(
    plan: &CommittedPlan<'_, '_>,
) -> Result<NativeDesktop> {
    plan.verify()?;
    NativeDesktop::create_paired(&plan.plan)
}
#[cfg(test)]
#[path = "account_paired_desktop_tests.rs"]
mod tests;
