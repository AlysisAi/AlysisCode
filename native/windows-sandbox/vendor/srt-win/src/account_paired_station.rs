//! Original shared station grant wrapped around the intact paired resource bundle.
//! Drop closes handles only. Restoration requires the original completed pair;
//! immutable disk rows are diagnostic and never reconstruct live authority.
use super::*;
#[path = "account_paired_initializer.rs"]
pub(crate) mod initializer;
use crate::account_state::execution::resources::station::{Station, proposed};
use crate::acl::object_grants::Snapshot;

#[path = "account_paired_desktop.rs"]
pub(crate) mod desktop;

const TABLE: &str = "candidate_paired_station";
const MAX_EVENT: usize = 140000;
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_paired_station(sequence INTEGER PRIMARY KEY CHECK(sequence BETWEEN 0 AND 3), event_json TEXT NOT NULL)",
    "CREATE TRIGGER paired_station_no_update BEFORE UPDATE ON candidate_paired_station BEGIN SELECT RAISE(ABORT,'paired station event is immutable'); END",
    "CREATE TRIGGER paired_station_no_delete BEFORE DELETE ON candidate_paired_station BEGIN SELECT RAISE(ABORT,'paired station event is immutable'); END",
    "CREATE TRIGGER paired_station_no_replace BEFORE INSERT ON candidate_paired_station WHEN EXISTS(SELECT 1 FROM candidate_paired_station WHERE sequence=NEW.sequence) BEGIN SELECT RAISE(ABORT,'paired station event is immutable'); END",
];
pub(in crate::account_state::execution::paired_plan) fn create_schema(
    sql: &Connection,
) -> Result<()> {
    for statement in SCHEMA {
        sql.execute_batch(statement)?;
    }
    desktop::create_schema(sql)?;
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
        "paired station schema differs; preserve state"
    );
    desktop::require_schema(sql)?;
    Ok(())
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Binding {
    operation: String,
    nonce: String,
    namespace: String,
    profile: Option<host_profile::ResourceBinding>,
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
        retirements: [String; 2],
    },
    Restored {
        binding: Binding,
        retirements: [String; 2],
    },
}
fn retired_namespace(sql: &Connection, plan: &Plan) -> Result<[String; 2]> {
    let events = super::replay(sql, plan)?;
    ensure!(
        events.len() == 4,
        "station restoration lacks complete namespace history"
    );
    let Some(super::Event::Removed { retirements, .. }) = events.last() else {
        anyhow::bail!("station restoration lacks namespace removal receipt");
    };
    Ok(retirements.clone())
}
fn validate(sql: &Connection, plan: &Plan, events: &[Event]) -> Result<()> {
    ensure!(events.len() <= 4, "paired station inventory exceeds bound");
    if events.is_empty() {
        return Ok(());
    }
    let Event::Intent {
        binding,
        before,
        after,
    } = &events[0]
    else {
        anyhow::bail!("paired station intent missing");
    };
    super::require_new_nonce(plan, &binding.nonce)?;
    ensure!(
        binding.namespace == super::birth_nonce(sql, plan)? && binding.namespace != binding.nonce,
        "paired station namespace differs"
    );
    ensure!(
        binding.profile == host_profile::resource_binding(sql, plan)?,
        "paired station profile chain changed"
    );
    if let Some(profile) = &binding.profile {
        ensure!(
            binding.nonce != profile.birth && profile.use_nonce.as_ref() != Some(&binding.nonce),
            "paired station nonce aliases profile phase"
        );
    }
    ensure!(
        binding.operation == plan.operation
            && binding.nonce != plan.operation
            && binding.session == plan.session
            && binding.package_sid == plan.execution.identity.package_sid
            && binding.policy == 1,
        "paired station binding differs from plan"
    );
    ensure!(
        proposed(before, &binding.package_sid)? == *after,
        "paired station proposal differs from exact package policy"
    );
    if let Some(event) = events.get(1) {
        ensure!(
            *event
                == Event::Applied {
                    binding: binding.clone()
                },
            "paired station applied receipt differs"
        );
    }
    if let Some(event) = events.get(2) {
        let retirements = retired_namespace(sql, plan)?;
        ensure!(
            retirements.iter().all(|n| n != &binding.nonce)
                && *event
                    == Event::RestoreIntent {
                        binding: binding.clone(),
                        retirements: retirements.clone()
                    },
            "station restoration lacks exact completed paired namespace/jobs"
        );
        if let Some(event) = events.get(3) {
            ensure!(
                *event
                    == Event::Restored {
                        binding: binding.clone(),
                        retirements
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
        "SELECT count(*),max(length(CAST(event_json AS BLOB))) FROM candidate_paired_station",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    ensure!(
        count <= 4 && max.unwrap_or(0) <= MAX_EVENT,
        "paired station inventory exceeds bound"
    );
    let mut s =
        sql.prepare("SELECT sequence,event_json FROM candidate_paired_station ORDER BY sequence")?;
    let mut events = Vec::new();
    for row in s.query_map([], |r| Ok((r.get::<_, usize>(0)?, r.get::<_, String>(1)?)))? {
        let (sequence, bytes) = row?;
        ensure!(
            sequence == events.len(),
            "paired station sequence has a gap"
        );
        events.push(serde_json::from_str(&bytes).context("malformed paired station event")?);
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
        "paired station event exceeds bound"
    );
    let tx = sql.unchecked_transaction()?;
    tx.execute(
        "INSERT INTO candidate_paired_station VALUES(?1,?2)",
        params![sequence, bytes],
    )?;
    tx.commit()
        .context("paired station commit uncertain; preserve grant and pending state")?;
    ensure!(
        replay(sql, plan)? == expected,
        "paired station readback differs"
    );
    Ok(())
}
/// The original pair is first and stays intact across every fallible operation.
/// The wrapper seals profile construction before publishing the station grant.
pub(crate) struct StationPair<'p, 'a, 'lease> {
    pair: Option<NamespacedPair<'p, 'a, 'lease>>,
    desktop: Option<desktop::OwnedDesktop<'p, 'a, 'lease>>,
    station: OwnedStation<'p, 'a, 'lease>,
}
struct OwnedStation<'p, 'a, 'lease> {
    native: Station,
    plan: &'p CommittedPlan<'a, 'lease>,
    binding: Binding,
    before: Snapshot,
    after: Snapshot,
    apply_attempted: bool,
    restore_attempted: bool,
    desktop_nonce: Option<String>,
}
pub(crate) struct CompletedStationPair<'p, 'a, 'lease> {
    pair: CompletedNamespacedPair<'p, 'a, 'lease>,
    desktop: Option<desktop::OwnedDesktop<'p, 'a, 'lease>>,
    station: OwnedStation<'p, 'a, 'lease>,
}
impl<'p, 'a, 'lease> NamespacedPair<'p, 'a, 'lease> {
    pub(crate) fn with_station(self) -> Result<StationPair<'p, 'a, 'lease>> {
        self.require_open()?;
        self.jobs.require_namespace_birth()?;
        let plan = self.jobs.plan;
        let sql = plan.database.connection()?;
        ensure!(
            replay(sql, &plan.plan)?.is_empty(),
            "paired station grant already attempted"
        );
        let (native, before, after) = Station::prepare(&plan.plan.execution.identity.package_sid)?;
        let binding = Binding {
            operation: plan.plan.operation.clone(),
            nonce: random_id()?,
            namespace: super::birth_nonce(sql, &plan.plan)?,
            profile: host_profile::resource_binding(sql, &plan.plan)?,
            session: plan.plan.session,
            package_sid: plan.plan.execution.identity.package_sid.clone(),
            policy: 1,
        };
        let mut bundle = StationPair {
            pair: Some(self),
            desktop: None,
            station: OwnedStation {
                native,
                plan,
                binding,
                before,
                after,
                apply_attempted: false,
                restore_attempted: false,
                desktop_nonce: None,
            },
        };
        // Latch before intent and preserve the intact pair on every failure.
        ensure!(
            !std::mem::replace(&mut bundle.station.apply_attempted, true),
            "station apply repeated"
        );
        bundle.station.checkpoint("prepared")?;
        append(sql, &plan.plan, bundle.station.birth_events()[0].clone())?;
        bundle.station.checkpoint("intent")?;
        bundle.pair.as_ref().unwrap().require_open()?;
        bundle
            .pair
            .as_ref()
            .unwrap()
            .jobs
            .require_namespace_birth()?;
        bundle.station.verify(&bundle.station.before)?;
        bundle
            .station
            .native
            .write_exact(&bundle.station.before, &bundle.station.after)?;
        bundle.station.checkpoint("applied")?;
        bundle.station.verify(&bundle.station.after)?;
        append(sql, &plan.plan, bundle.station.birth_events()[1].clone())?;
        bundle.station.checkpoint("ready")?;
        bundle.require_open()?;
        Ok(bundle)
    }
}
impl<'p, 'a, 'lease> StationPair<'p, 'a, 'lease> {
    fn require_open(&self) -> Result<()> {
        let pair = self.pair.as_ref().context("original station pair absent")?;
        ensure!(
            std::ptr::eq(pair.jobs.plan, self.station.plan),
            "station pair plan differs"
        );
        pair.require_open()?;
        ensure!(
            self.desktop.is_some() == self.station.desktop_nonce.is_some(),
            "desktop attempt lacks original guard"
        );
        if let Some(desktop) = &self.desktop {
            desktop.verify_ready(&self.station)?;
        }
        self.station.verify_applied()
    }
    pub(crate) fn run_profile_self_check(
        mut self,
        owner: ControlServer,
        consumer: ControlServer,
        timeout: Duration,
    ) -> Result<CompletedStationPair<'p, 'a, 'lease>> {
        self.require_open()?;
        let pair = self
            .pair
            .take()
            .context("original station pair absent")?
            .run_profile_self_check(owner, consumer, timeout)?;
        self.finish(pair)
    }
    #[cfg(test)]
    pub(crate) fn fixture_run_profile_lifetime(
        mut self,
        owner: ControlServer,
        consumer: ControlServer,
        timeout: Duration,
        password: &str,
    ) -> Result<CompletedStationPair<'p, 'a, 'lease>> {
        self.require_open()?;
        let pair = self
            .pair
            .take()
            .context("original station pair absent")?
            .fixture_run_profile_lifetime(owner, consumer, timeout, password)?;
        self.finish(pair)
    }
    #[cfg(test)]
    pub(crate) fn fixture_run_host_family(
        mut self,
        owner: ControlServer,
        consumer: ControlServer,
    ) -> Result<CompletedStationPair<'p, 'a, 'lease>> {
        self.require_open()?;
        let pair = self
            .pair
            .take()
            .context("original station pair absent")?
            .fixture_run_host_family(owner, consumer)?;
        self.finish(pair)
    }
    fn finish(
        mut self,
        pair: CompletedNamespacedPair<'p, 'a, 'lease>,
    ) -> Result<CompletedStationPair<'p, 'a, 'lease>> {
        // No live job can move into this local: the opaque completion contains
        // both original retired jobs and their original retired resource guards.
        if let Some(desktop) = &mut self.desktop {
            desktop.retire(&pair, &self.station)?;
        }
        self.station.restore(&pair, self.desktop.as_ref())?;
        let done = CompletedStationPair {
            pair,
            desktop: self.desktop,
            station: self.station,
        };
        done.verify()?;
        Ok(done)
    }
}
impl CompletedStationPair<'_, '_, '_> {
    pub(crate) fn verify(&self) -> Result<()> {
        self.station.verify_completed(&self.pair)?;
        self.station
            .verify_desktop(&self.pair, self.desktop.as_ref())?;
        ensure!(
            self.station.restore_attempted,
            "station restoration was not attempted"
        );
        self.station.verify(&self.station.before)?;
        let mut expected = self.station.birth_events();
        let retirements = retired_namespace(
            self.station.plan.database.connection()?,
            &self.station.plan.plan,
        )?;
        expected.push(Event::RestoreIntent {
            binding: self.station.binding.clone(),
            retirements: retirements.clone(),
        });
        expected.push(Event::Restored {
            binding: self.station.binding.clone(),
            retirements,
        });
        ensure!(
            replay(
                self.station.plan.database.connection()?,
                &self.station.plan.plan
            )? == expected,
            "station completion receipt differs"
        );
        Ok(())
    }
}
impl OwnedStation<'_, '_, '_> {
    fn birth_events(&self) -> Vec<Event> {
        vec![
            Event::Intent {
                binding: self.binding.clone(),
                before: self.before.clone(),
                after: self.after.clone(),
            },
            Event::Applied {
                binding: self.binding.clone(),
            },
        ]
    }
    fn verify_applied(&self) -> Result<()> {
        ensure!(
            self.apply_attempted && !self.restore_attempted,
            "station phase differs"
        );
        self.verify(&self.after)?;
        ensure!(
            replay(self.plan.database.connection()?, &self.plan.plan)? == self.birth_events(),
            "original station grant receipts differ"
        );
        Ok(())
    }
    fn verify(&self, expected: &Snapshot) -> Result<()> {
        self.plan.verify()?;
        self.native.verify_current()?;
        ensure!(
            self.native.read()? == *expected,
            "paired station descriptor changed; do not overwrite"
        );
        Ok(())
    }
    fn verify_completed(&self, completed: &CompletedNamespacedPair<'_, '_, '_>) -> Result<()> {
        ensure!(
            std::ptr::eq(completed.bundle.jobs.plan, self.plan)
                && completed.bundle.namespace.binding.nonce == self.binding.namespace,
            "station completion belongs to another original pair"
        );
        completed.verify()
    }
    fn verify_desktop(
        &self,
        completed: &CompletedNamespacedPair<'_, '_, '_>,
        desktop: Option<&desktop::OwnedDesktop<'_, '_, '_>>,
    ) -> Result<()> {
        ensure!(
            self.desktop_nonce.is_some() == desktop.is_some(),
            "station restoration lacks original desktop guard"
        );
        if let Some(desktop) = desktop {
            desktop.verify_retired(completed, self)?;
        }
        Ok(())
    }
    fn restore(
        &mut self,
        completed: &CompletedNamespacedPair<'_, '_, '_>,
        desktop: Option<&desktop::OwnedDesktop<'_, '_, '_>>,
    ) -> Result<()> {
        ensure!(
            !std::mem::replace(&mut self.restore_attempted, true),
            "station restoration already attempted"
        );
        self.verify_completed(completed)?;
        self.verify_desktop(completed, desktop)?;
        ensure!(self.apply_attempted, "station apply not attempted");
        let sql = self.plan.database.connection()?;
        ensure!(
            replay(sql, &self.plan.plan)? == self.birth_events(),
            "station grant receipts differ"
        );
        self.checkpoint("before-restore")?;
        self.verify(&self.after)?;
        let retirements = retired_namespace(sql, &self.plan.plan)?;
        append(
            sql,
            &self.plan.plan,
            Event::RestoreIntent {
                binding: self.binding.clone(),
                retirements: retirements.clone(),
            },
        )?;
        self.checkpoint("restoring")?;
        self.verify_completed(completed)?;
        self.verify_desktop(completed, desktop)?;
        self.verify(&self.after)?;
        self.native.write_exact(&self.after, &self.before)?;
        self.checkpoint("restored")?;
        self.verify(&self.before)?;
        self.verify_completed(completed)?;
        self.verify_desktop(completed, desktop)?;
        append(
            sql,
            &self.plan.plan,
            Event::Restored {
                binding: self.binding.clone(),
                retirements,
            },
        )?;
        self.checkpoint("completed")?;
        self.verify(&self.before)
    }
    fn checkpoint(&mut self, phase: &str) -> Result<()> {
        #[cfg(test)]
        STATION_CHECKPOINT.with(|s| -> Result<()> {
            if let Some(hook) = s.borrow_mut().as_mut() {
                hook(phase, self.plan.database.connection()?, &mut self.native)?;
            }
            Ok(())
        })?;
        #[cfg(not(test))]
        let _ = phase;
        Ok(())
    }
}
#[cfg(test)]
thread_local! {
    pub(crate) static STATION_CHECKPOINT: std::cell::RefCell<Option<Box<dyn FnMut(&str,&Connection,&mut Station)->Result<()>>>> = const {std::cell::RefCell::new(None)};
}
#[cfg(test)]
#[path = "account_paired_station_tests.rs"]
mod tests;
