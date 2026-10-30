//! Consuming credential transfer from a live logon preparation. Uncertain
//! delivery is never retried. The original guard remains owned through receipt
//! commit; this module grants no account birth, resume or cleanup permission.
use super::*;
use crate::account_control::bootstrap::entry::credential_wire::{PasswordFrame, Selection};

#[path = "account_bootstrap_account_birth.rs"]
pub(crate) mod birth;

const TABLE: &str = "candidate_atomic_credential_delivery";
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_atomic_credential_delivery(singleton INTEGER PRIMARY KEY CHECK(singleton=1), receipt_json TEXT NOT NULL)",
    "CREATE TRIGGER atomic_delivery_no_update BEFORE UPDATE ON candidate_atomic_credential_delivery BEGIN SELECT RAISE(ABORT,'credential delivery receipt is immutable'); END",
    "CREATE TRIGGER atomic_delivery_no_delete BEFORE DELETE ON candidate_atomic_credential_delivery BEGIN SELECT RAISE(ABORT,'credential delivery receipt is immutable'); END",
    "CREATE TRIGGER atomic_delivery_no_replace BEFORE INSERT ON candidate_atomic_credential_delivery WHEN EXISTS(SELECT 1 FROM candidate_atomic_credential_delivery) BEGIN SELECT RAISE(ABORT,'credential delivery receipt is immutable'); END",
];

pub(super) fn create_schema(connection: &Connection) -> Result<()> {
    for sql in SCHEMA {
        connection.execute_batch(sql)?;
    }
    birth::create_schema(connection)
}
pub(super) fn require_schema(connection: &Connection) -> Result<()> {
    birth::require_schema(connection)?;
    let mut s = connection.prepare("SELECT sql FROM sqlite_schema WHERE tbl_name=?1")?;
    let rows: Vec<String> = s
        .query_map([TABLE], |r| r.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    ensure!(
        rows.len() == SCHEMA.len() && SCHEMA.iter().all(|s| rows.iter().any(|r| r == s)),
        "credential delivery schema differs; preserve state"
    );
    Ok(())
}

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Receipt {
    protocol: String,
    selection: Selection,
}

fn selection(prepared: &PreparedAccountLogon<'_, '_, '_, '_>) -> Selection {
    let intent = &prepared.intent;
    Selection {
        protocol: Selection::protocol(),
        operation: intent.operation.clone(),
        logon: intent.nonce.clone(),
        ready: intent.ready.clone(),
        bootstrap_pid: intent.bootstrap_pid,
        bootstrap_created: intent.bootstrap_created,
        image_id: intent.image_id,
        session: prepared.running.birth.owner.plan.plan.session,
        account: intent.account.clone(),
        credential_generation: intent.credential_generation.clone(),
    }
}

fn read_receipt(connection: &Connection, plan: &Plan) -> Result<Option<Receipt>> {
    read(connection, plan)?;
    let (count,max):(usize,Option<usize>)=connection.query_row(
        "SELECT count(*),max(length(CAST(receipt_json AS BLOB))) FROM candidate_atomic_credential_delivery",[],|r|Ok((r.get(0)?,r.get(1)?)))?;
    ensure!(
        count <= 1 && max.unwrap_or(0) <= 8192,
        "credential delivery inventory exceeds bounds"
    );
    if count == 0 {
        return Ok(None);
    }
    let bytes:Option<String>=connection.query_row(
        "SELECT receipt_json FROM candidate_atomic_credential_delivery WHERE singleton=1 AND typeof(receipt_json)='text'",[],|r|r.get(0)).optional()?;
    let receipt: Receipt = serde_json::from_str(&bytes.context("credential receipt missing")?)
        .map_err(|_| anyhow::anyhow!("malformed credential receipt"))?;
    validate_receipt(connection, plan, &receipt)?;
    Ok(Some(receipt))
}

fn validate_receipt(connection: &Connection, plan: &Plan, receipt: &Receipt) -> Result<()> {
    let intent = read(connection, plan)?.context("credential receipt lacks logon intent")?;
    let s = &receipt.selection;
    s.validate()?;
    ensure!(
        receipt.protocol == "alysis-credential-delivered-v1"
            && s.operation == intent.operation
            && s.logon == intent.nonce
            && s.ready == intent.ready
            && s.bootstrap_pid == intent.bootstrap_pid
            && s.bootstrap_created == intent.bootstrap_created
            && s.image_id == intent.image_id
            && s.session == plan.session
            && s.account == intent.account
            && s.credential_generation == intent.credential_generation,
        "credential receipt differs from original selected logon intent"
    );
    Ok(())
}

fn persist_receipt(connection: &Connection, plan: &Plan, selection: &Selection) -> Result<()> {
    ensure!(
        read_receipt(connection, plan)?.is_none(),
        "credential delivery already recorded; no retry"
    );
    let receipt = Receipt {
        protocol: "alysis-credential-delivered-v1".into(),
        selection: selection.clone(),
    };
    // Reject substitutions before writing even when this pure-data adapter is
    // called by diagnostics. Readback alone would preserve a bad committed row.
    validate_receipt(connection, plan, &receipt)?;
    let bytes = serde_json::to_string(&receipt)?;
    ensure!(bytes.len() <= 8192, "credential receipt exceeds bounds");
    let tx = connection.unchecked_transaction()?;
    tx.execute(
        "INSERT INTO candidate_atomic_credential_delivery VALUES(1,?1)",
        [bytes],
    )?;
    #[cfg(test)]
    delivery_checkpoint("receipt-inserted", &tx);
    tx.commit()
        .context("credential delivery commit uncertain; preserve state")?;
    #[cfg(test)]
    delivery_checkpoint("receipt-committed", connection);
    ensure!(
        read_receipt(connection, plan)?.as_ref() == Some(&receipt),
        "credential receipt readback differs"
    );
    Ok(())
}

#[cfg(test)]
thread_local! {
    pub(crate) static DELIVERY_CHECKPOINT: std::cell::RefCell<Option<Box<dyn FnMut(&str, &Connection)>>> = const { std::cell::RefCell::new(None) };
}
#[cfg(test)]
fn delivery_checkpoint(phase: &str, connection: &Connection) {
    DELIVERY_CHECKPOINT.with(|slot| {
        if let Some(hook) = slot.borrow_mut().as_mut() {
            hook(phase, connection);
        }
    });
}

/// Only constructed by consuming the original journaled preparation. The sole
/// credential reader borrows this object and cannot return a password to callers.
pub(crate) struct CredentialDelivery<'j, 'p, 'a, 'lease> {
    prepared: PreparedAccountLogon<'j, 'p, 'a, 'lease>,
    selection: Selection,
    timeout: Duration,
    read_attempted: bool,
    send_attempted: bool,
    acknowledged: bool,
}

impl<'j, 'p, 'a, 'lease> CredentialDelivery<'j, 'p, 'a, 'lease> {
    fn start(
        prepared: PreparedAccountLogon<'j, 'p, 'a, 'lease>,
        timeout: Duration,
    ) -> Result<Self> {
        ensure!(
            timeout >= Duration::from_millis(1) && timeout <= Duration::from_secs(10),
            "credential timeout outside bounds"
        );
        let mut prepared = prepared.verify()?;
        let selected = selection(&prepared);
        selected.validate()?;
        let plan = prepared.running.birth.owner.plan;
        ensure!(
            read_receipt(plan.database.connection()?, &plan.plan)?.is_none(),
            "credential delivery already recorded"
        );
        prepared
            .running
            ._channel
            .send(&selected.encode()?, timeout)?;
        selected.require_ack(&prepared.running._channel.receive(timeout)?, false)?;
        let prepared = prepared.verify()?;
        Ok(Self {
            prepared,
            selection: selected,
            timeout,
            read_attempted: false,
            send_attempted: false,
            acknowledged: false,
        })
    }
    pub(crate) fn begin_read(
        &mut self,
    ) -> Result<(
        &ReadyInstallation,
        &crate::execution_lease::ExecutionLease,
        AccountMode,
    )> {
        ensure!(
            !std::mem::replace(&mut self.read_attempted, true),
            "credential read already attempted"
        );
        let plan = self.prepared.running.birth.owner.plan;
        plan.verify()?;
        ensure!(
            read(plan.database.connection()?, &plan.plan)?.as_ref() == Some(&self.prepared.intent),
            "credential read intent changed"
        );
        ensure!(
            read_receipt(plan.database.connection()?, &plan.plan)?.is_none(),
            "credential read already has a delivery receipt"
        );
        self.prepared.running.birth.child.durable_observation()?;
        let lease = plan
            .execution
            ._lease
            .context("credential delivery requires original execution lease")?;
        Ok((plan.ready, lease, self.selection.account.mode))
    }
    pub(crate) fn send_password(&mut self, user: &str, password: &str) -> Result<()> {
        ensure!(
            self.read_attempted && !std::mem::replace(&mut self.send_attempted, true),
            "credential transfer already attempted or unprepared"
        );
        ensure!(
            user == self.selection.account.account_name,
            "credential username differs from selected account"
        );
        let plan = self.prepared.running.birth.owner.plan;
        #[cfg(test)]
        delivery_checkpoint("before-send", plan.database.connection()?);
        plan.verify()?;
        ensure!(
            read(plan.database.connection()?, &plan.plan)?.as_ref() == Some(&self.prepared.intent),
            "credential delivery intent changed"
        );
        ensure!(
            read_receipt(plan.database.connection()?, &plan.plan)?.is_none(),
            "credential send already has a delivery receipt"
        );
        self.prepared.running.birth.child.durable_observation()?;
        let frame = PasswordFrame::encode(password, &self.selection)?;
        frame.send(
            &mut self.prepared.running._channel,
            &self.selection,
            self.timeout,
        )?;
        drop(frame);
        self.selection
            .require_ack(&self.prepared.running._channel.receive(self.timeout)?, true)?;
        self.acknowledged = true;
        #[cfg(test)]
        delivery_checkpoint("acknowledged", plan.database.connection()?);
        Ok(())
    }
    fn commit(self) -> Result<DeliveredCredential<'j, 'p, 'a, 'lease>> {
        ensure!(
            self.read_attempted && self.send_attempted && self.acknowledged,
            "credential transfer not acknowledged"
        );
        let prepared = self.prepared.verify()?;
        let plan = prepared.running.birth.owner.plan;
        persist_receipt(plan.database.connection()?, &plan.plan, &self.selection)?;
        let prepared = prepared.verify()?;
        Ok(DeliveredCredential {
            prepared,
            selection: self.selection,
            timeout: self.timeout,
        })
    }
}

#[cfg(test)]
#[path = "account_bootstrap_credential_delivery_tests.rs"]
mod tests;

pub(crate) struct DeliveredCredential<'j, 'p, 'a, 'lease> {
    prepared: PreparedAccountLogon<'j, 'p, 'a, 'lease>,
    selection: Selection,
    timeout: Duration,
}
impl DeliveredCredential<'_, '_, '_, '_> {
    /// Explicitly discard the held password. This cannot retire execution state,
    /// acknowledge an account child, or authorize filesystem/profile cleanup.
    pub(crate) fn discard(mut self) -> Result<()> {
        let mut request = b"discard-credential:".to_vec();
        request.extend_from_slice(self.selection.logon.as_bytes());
        self.prepared
            .running
            ._channel
            .send(&request, self.timeout)?;
        ensure!(
            self.prepared.running._channel.receive(self.timeout)? == b"credential-discarded",
            "credential discard not acknowledged"
        );
        Ok(())
    }
}

impl<'j, 'p, 'a, 'lease> PreparedAccountLogon<'j, 'p, 'a, 'lease> {
    pub(crate) fn deliver(
        self,
        timeout: Duration,
    ) -> Result<DeliveredCredential<'j, 'p, 'a, 'lease>> {
        let mut delivery = CredentialDelivery::start(self, timeout)?;
        crate::account_credentials::deliver_to_bootstrap(&mut delivery)?;
        delivery.commit()
    }
    #[cfg(test)]
    pub(crate) fn fixture_deliver(
        self,
        password: &str,
        timeout: Duration,
    ) -> Result<DeliveredCredential<'j, 'p, 'a, 'lease>> {
        let mut delivery = CredentialDelivery::start(self, timeout)?;
        // Synthetic in-memory fixture only; never reads real registry credentials.
        delivery.read_attempted = true;
        let user = delivery.selection.account.account_name.clone();
        delivery.send_password(&user, password)?;
        delivery.commit()
    }
}
