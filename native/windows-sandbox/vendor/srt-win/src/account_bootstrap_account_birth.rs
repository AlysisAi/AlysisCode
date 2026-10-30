//! The public native operation consumes the whole job, not just its bootstrap.
//! It records one selected account child and observes its exit without resuming.
//! Cold rows are evidence; they cannot restart logon or authorize cleanup.
use super::*;
use crate::account_control::bootstrap::{
    entry::credential_wire::child::{BirthRequest, Purpose},
    pull::{AccountExitObserver, ProcessReceiver},
};
use crate::account_state::execution::bootstrap_plan::lifecycle::initializer::Initialization;

#[path = "account_bootstrap_job_retirement.rs"]
pub(crate) mod retirement;

#[path = "account_bootstrap_account_resume.rs"]
pub(crate) mod account_resume;

const TABLE: &str = "candidate_atomic_account_birth";
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_atomic_account_birth(sequence INTEGER PRIMARY KEY CHECK(sequence BETWEEN 0 AND 1), event_json TEXT NOT NULL)",
    "CREATE TRIGGER atomic_account_birth_no_update BEFORE UPDATE ON candidate_atomic_account_birth BEGIN SELECT RAISE(ABORT,'account birth event is immutable'); END",
    "CREATE TRIGGER atomic_account_birth_no_delete BEFORE DELETE ON candidate_atomic_account_birth BEGIN SELECT RAISE(ABORT,'account birth event is immutable'); END",
    "CREATE TRIGGER atomic_account_birth_no_replace BEFORE INSERT ON candidate_atomic_account_birth WHEN EXISTS(SELECT 1 FROM candidate_atomic_account_birth WHERE sequence=NEW.sequence) BEGIN SELECT RAISE(ABORT,'account birth event is immutable'); END",
];
pub(super) fn create_schema(connection: &Connection) -> Result<()> {
    for sql in SCHEMA {
        connection.execute_batch(sql)?;
    }
    account_resume::create_schema(connection)?;
    retirement::create_schema(connection)
}
pub(super) fn require_schema(connection: &Connection) -> Result<()> {
    retirement::require_schema(connection)?;
    account_resume::require_schema(connection)?;
    let mut s = connection.prepare("SELECT sql FROM sqlite_schema WHERE tbl_name=?1")?;
    let rows: Vec<String> = s
        .query_map([TABLE], |r| r.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    ensure!(
        rows.len() == SCHEMA.len() && SCHEMA.iter().all(|s| rows.iter().any(|r| r == s)),
        "account birth schema differs; preserve state"
    );
    Ok(())
}

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum BirthEvent {
    Intent {
        request: BirthRequest,
    },
    Created {
        nonce: String,
        pid: u32,
        created: u64,
        image_id: [u8; 24],
    },
}
fn validate(connection: &Connection, plan: &Plan, events: &[BirthEvent]) -> Result<()> {
    ensure!(events.len() <= 2, "account birth event bound exceeded");
    if events.is_empty() {
        return Ok(());
    }
    let receipt = read_receipt(connection, plan)?
        .context("account birth requires committed credential delivery")?;
    let BirthEvent::Intent { request } = &events[0] else {
        anyhow::bail!("account birth intent missing");
    };
    request.validate()?;
    #[cfg(test)]
    ensure!(
        !matches!(
            request.purpose,
            Purpose::FixtureProfileWriterV1 { .. } | Purpose::FixtureStationLowboxV1 { .. }
        ),
        "fixed writer cannot use single-job birth journal"
    );
    ensure!(
        !matches!(request.purpose, Purpose::TrustedPairedProfileV1 { .. }),
        "paired profile owner cannot use the single-job birth journal"
    );
    if let Purpose::TrustedRpcSelfCheckV1 { package_sid, .. } = &request.purpose {
        ensure!(
            package_sid == &plan.execution.identity.package_sid,
            "RPC probe differs from plan package"
        );
    }
    if let Purpose::TrustedUnusedProfileV1 {
        container,
        package_sid,
        ..
    } = &request.purpose
    {
        ensure!(
            container == &plan.execution.identity.container
                && package_sid == &plan.execution.identity.package_sid,
            "unused account profile differs from original plan"
        );
    }
    ensure!(
        request.selection == receipt.selection,
        "account birth differs from original delivered selection"
    );
    if let Some(event) = events.get(1) {
        let BirthEvent::Created {
            nonce,
            pid,
            created,
            image_id,
        } = event
        else {
            anyhow::bail!("account birth receipt missing");
        };
        ensure!(
            nonce == &request.nonce
                && *pid != 0
                && *pid != plan.execution.pid
                && *pid != request.selection.bootstrap_pid
                && *created >= request.selection.bootstrap_created
                && *image_id == plan.image_id,
            "account birth receipt differs from intent/native identity"
        );
    }
    Ok(())
}
fn replay_birth(connection: &Connection, plan: &Plan) -> Result<Vec<BirthEvent>> {
    require_plan(connection, plan)?;
    let (count, max): (usize, Option<usize>) = connection.query_row(
        "SELECT count(*),max(length(CAST(event_json AS BLOB))) FROM candidate_atomic_account_birth",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    ensure!(
        count <= 2 && max.unwrap_or(0) <= 8192,
        "account birth inventory exceeds bounds"
    );
    let mut s = connection.prepare(
        "SELECT sequence,event_json FROM candidate_atomic_account_birth ORDER BY sequence",
    )?;
    let rows = s.query_map([], |r| Ok((r.get::<_, usize>(0)?, r.get::<_, String>(1)?)))?;
    let mut events = Vec::new();
    for row in rows {
        let (index, bytes) = row?;
        ensure!(index == events.len(), "account birth sequence has a gap");
        events.push(
            serde_json::from_str(&bytes)
                .map_err(|_| anyhow::anyhow!("malformed account birth event"))?,
        );
    }
    validate(connection, plan, &events)?;
    Ok(events)
}
fn append_birth(connection: &Connection, plan: &Plan, event: BirthEvent) -> Result<()> {
    let mut expected = replay_birth(connection, plan)?;
    let sequence = expected.len();
    expected.push(event.clone());
    validate(connection, plan, &expected)?;
    let encoded = serde_json::to_string(&event)?;
    ensure!(encoded.len() <= 8192, "account birth event exceeds bounds");
    let tx = connection.unchecked_transaction()?;
    tx.execute(
        "INSERT INTO candidate_atomic_account_birth VALUES(?1,?2)",
        params![sequence, encoded],
    )?;
    #[cfg(test)]
    checkpoint("inserted", sequence, &tx);
    tx.commit()
        .context("account birth commit uncertain; preserve pending state")?;
    #[cfg(test)]
    checkpoint("committed", sequence, connection);
    ensure!(
        replay_birth(connection, plan)? == expected,
        "account birth readback differs"
    );
    Ok(())
}

#[cfg(test)]
thread_local! {
    pub(crate) static BIRTH_CHECKPOINT: std::cell::RefCell<Option<Box<dyn FnMut(&str,usize,&Connection)>>> = const { std::cell::RefCell::new(None) };
    pub(crate) static BIRTH_NATIVE_CHECKPOINT: std::cell::RefCell<Option<Box<dyn FnMut(&str,AccountExitObserver,&Connection)->Result<()>>>> = const { std::cell::RefCell::new(None) };
}
#[cfg(test)]
fn checkpoint(phase: &str, sequence: usize, connection: &Connection) {
    BIRTH_CHECKPOINT.with(|slot| {
        if let Some(hook) = slot.borrow_mut().as_mut() {
            hook(phase, sequence, connection);
        }
    });
}

#[cfg(test)]
fn native_checkpoint(
    phase: &str,
    observed: &crate::account_control::bootstrap::pull::ObservedRunner<'_>,
    connection: &Connection,
) -> Result<()> {
    BIRTH_NATIVE_CHECKPOINT.with(|slot| {
        if let Some(hook) = slot.borrow_mut().as_mut() {
            hook(phase, observed.exit_observer()?, connection)?;
        }
        Ok(())
    })
}

#[cfg(test)]
#[path = "account_bootstrap_account_birth_tests.rs"]
mod tests;

/// Observations from an already terminated original account process. No handles
/// or live execution/retirement authority escape the consuming job operation.
pub(crate) struct AccountBirthObservation {
    pub(crate) pid: u32,
    pub(crate) created: u64,
    pub(crate) image_id: [u8; 24],
}

impl<'p, 'a, 'lease> JournaledJob<'p, 'a, 'lease> {
    pub(crate) fn observe_account_birth(
        self,
        server: ControlServer,
        timeout: Duration,
    ) -> Result<AccountBirthObservation> {
        let result = self.record_account_birth(
            None,
            Purpose::ObserveOnly,
            server,
            timeout,
            #[cfg(test)]
            None,
        );
        self.retire_observed_birth(result, timeout)
            .map(|(observation, _)| observation)
    }
    #[cfg(test)]
    pub(crate) fn fixture_observe_account_birth(
        self,
        server: ControlServer,
        password: &str,
        timeout: Duration,
    ) -> Result<AccountBirthObservation> {
        let result =
            self.record_account_birth(None, Purpose::ObserveOnly, server, timeout, Some(password));
        self.retire_observed_birth(result, timeout)
            .map(|(observation, _)| observation)
    }
    #[cfg(test)]
    pub(crate) fn fixture_run_account_self_check(
        self,
        server: ControlServer,
        password: &str,
        timeout: Duration,
    ) -> Result<AccountBirthObservation> {
        let result = self.record_account_birth(
            None,
            if PRIVATE_STATION_SELF_CHECK.with(|s| s.get()) {
                Purpose::FixturePrivateStationV1
            } else if UNUSED_PROFILE_SELF_CHECK.with(|s| s.get()) {
                Purpose::TrustedUnusedProfileV1 {
                    container: self.plan.plan.execution.identity.container.clone(),
                    package_sid: self.plan.plan.execution.identity.package_sid.clone(),
                    policy: 1,
                }
            } else if HANDOFF_SELF_CHECK.with(|s| s.get()) {
                Purpose::TrustedHandoffSelfCheckV1
            } else {
                Purpose::TrustedSelfCheckV1
            },
            server,
            timeout,
            Some(password),
        );
        self.retire_observed_birth(result, timeout)
            .map(|(observation, _)| observation)
    }
    pub(in crate::account_state::execution::bootstrap_plan) fn observe_for_namespace(
        self,
        initialization: Option<&mut Initialization<'_, 'p, 'a, 'lease>>,
        server: ControlServer,
        timeout: Duration,
        #[cfg(test)] password: Option<&str>,
    ) -> Result<(
        AccountBirthObservation,
        retirement::CompletedJob<'p, 'a, 'lease>,
    )> {
        let result = self.record_account_birth(
            initialization,
            Purpose::ObserveOnly,
            server,
            timeout,
            #[cfg(test)]
            password,
        );
        self.retire_observed_birth(result, timeout)
    }
    fn retire_observed_birth(
        self,
        result: Result<(AccountBirthObservation, AccountExitObserver)>,
        timeout: Duration,
    ) -> Result<(
        AccountBirthObservation,
        retirement::CompletedJob<'p, 'a, 'lease>,
    )> {
        let (observation, exit) = match result {
            Ok(value) => value,
            Err(error) => {
                drop(self);
                return Err(error);
            }
        };
        let intent = match retirement::RetirementIntent::prepare(&self, &observation) {
            Ok(intent) => intent,
            Err(error) => {
                drop(self);
                return Err(error);
            }
        };
        let completed = intent.finish(self, exit, timeout)?;
        Ok((observation, completed))
    }
    // No borrowed-job public creation entry exists. The two callers above own
    // and close the job on every outcome before returning data to the caller.
    fn record_account_birth(
        &self,
        mut initialization: Option<&mut Initialization<'_, 'p, 'a, 'lease>>,
        purpose: Purpose,
        server: ControlServer,
        timeout: Duration,
        #[cfg(test)] password: Option<&str>,
    ) -> Result<(AccountBirthObservation, AccountExitObserver)> {
        self.record_birth()?;
        let child = self.prepare_bootstrap()?;
        child.record_birth()?;
        let mut running = child.start(server, timeout)?;
        if let Some(initialization) = initialization.as_mut() {
            initialization.begin(&mut running)?;
            initialization.verify_ready()?;
        }
        let prepared = running.prepare_account_logon()?;
        #[cfg(test)]
        let delivered = if let Some(password) = password {
            prepared.fixture_deliver(password, timeout)?
        } else {
            prepared.deliver(timeout)?
        };
        #[cfg(not(test))]
        let delivered = prepared.deliver(timeout)?;
        let selected = delivered.selection;
        let prepared = delivered.prepared.verify()?;
        let plan = self.plan;
        let mut request = BirthRequest::new(selected, random_id()?)?;
        #[cfg(test)]
        let purpose = if crate::account_state::execution::bootstrap_plan::lifecycle::initializer::RPC_SELF_CHECK.with(|s| s.get()) {
            ensure!(purpose == Purpose::ObserveOnly, "RPC fixture purpose selection differs");
            initialization.as_ref().context("RPC probe requires retained initializer")?.rpc_probe_purpose()?
        } else { purpose };
        request.purpose = purpose;
        if matches!(&request.purpose, Purpose::TrustedRpcSelfCheckV1 { .. }) {
            initialization
                .as_ref()
                .context("RPC probe lacks retained initializer")?
                .verify_rpc_probe(&request.purpose)?;
        }
        request.validate()?;
        ensure!(
            replay_birth(plan.database.connection()?, &plan.plan)?.is_empty(),
            "account birth already attempted; no retry"
        );
        append_birth(
            plan.database.connection()?,
            &plan.plan,
            BirthEvent::Intent {
                request: request.clone(),
            },
        )?;
        let prepared = prepared.verify()?;
        let RunningBootstrap {
            mut birth,
            mut _channel,
        } = prepared.running;
        _channel
            .send(&request.encode()?, timeout)
            .context("send committed account birth request")?;
        if matches!(request.purpose, Purpose::TrustedUnusedProfileV1 { .. }) {
            _channel
                .receive_loaded_profile_birth_status(&request)
                .context("receive original loaded-profile birth status")?;
        } else {
            request.require_status(
                &_channel
                    .receive(timeout)
                    .context("receive original account birth status")?,
            )?;
        }
        let receiver = ProcessReceiver::prepare(
            &mut birth.child,
            _channel,
            plan.ready,
            request.selection.account.mode,
        )?
        .bind_birth(&request)?;
        let mut observed = receiver
            .receive(timeout)
            .context("receive original account process observation")?;
        self.verify()?;
        birth.child.durable_observation()?;
        let (pid, created, image_id) = observed.birth_identity()?;
        #[cfg(test)]
        native_checkpoint("observed", &observed, plan.database.connection()?)?;
        append_birth(
            plan.database.connection()?,
            &plan.plan,
            BirthEvent::Created {
                nonce: request.nonce.clone(),
                pid,
                created,
                image_id,
            },
        )?;
        self.verify()?;
        ensure!(
            observed.birth_identity()? == (pid, created, image_id),
            "account child changed after receipt commit"
        );
        observed.acknowledge_birth(&request, timeout)?;
        #[cfg(test)]
        native_checkpoint("acknowledged", &observed, plan.database.connection()?)?;
        if let Some(initialization) = initialization.as_mut() {
            initialization.verify_ready()?;
        }
        let exit = observed.exit_observer()?;
        if request.purpose.is_self_check() {
            account_resume::run(self, &request, observed, timeout)?;
        }
        Ok((
            AccountBirthObservation {
                pid,
                created,
                image_id,
            },
            exit,
        ))
    }
}

#[cfg(test)]
thread_local! {pub(crate) static HANDOFF_SELF_CHECK:std::cell::Cell<bool>=const{std::cell::Cell::new(false)};}
#[cfg(test)]
thread_local! {pub(crate) static UNUSED_PROFILE_SELF_CHECK:std::cell::Cell<bool>=const{std::cell::Cell::new(false)};}

#[cfg(test)]
thread_local! {pub(crate) static PRIVATE_STATION_SELF_CHECK:std::cell::Cell<bool>=const{std::cell::Cell::new(false)};}
