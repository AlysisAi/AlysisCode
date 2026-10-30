//! Write-ahead account-logon preparation, before any credential read or delivery.
//! Cold intent is evidence only. Its live consuming credential adapter cannot
//! log on, resume an account child or retire resources. Production stays gated.
use super::*;

#[path = "account_bootstrap_credential_delivery.rs"]
pub(crate) mod delivery;

const TABLE: &str = "candidate_atomic_account_logon";
const MAX_BYTES: usize = 4096;
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_atomic_account_logon(singleton INTEGER PRIMARY KEY CHECK(singleton=1), intent_json TEXT NOT NULL)",
    "CREATE TRIGGER atomic_logon_no_update BEFORE UPDATE ON candidate_atomic_account_logon BEGIN SELECT RAISE(ABORT,'account logon intent is immutable'); END",
    "CREATE TRIGGER atomic_logon_no_delete BEFORE DELETE ON candidate_atomic_account_logon BEGIN SELECT RAISE(ABORT,'account logon intent is immutable'); END",
    "CREATE TRIGGER atomic_logon_no_replace BEFORE INSERT ON candidate_atomic_account_logon WHEN EXISTS(SELECT 1 FROM candidate_atomic_account_logon) BEGIN SELECT RAISE(ABORT,'account logon intent is immutable'); END",
];

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Intent {
    protocol: String,
    operation: String,
    nonce: String,
    ready: String,
    bootstrap_pid: u32,
    bootstrap_created: u64,
    image_id: [u8; 24],
    account: crate::account_install::AccountBinding,
    credential_generation: String,
}

pub(super) fn create_schema(connection: &Connection) -> Result<()> {
    for sql in SCHEMA {
        connection.execute_batch(sql)?;
    }
    delivery::create_schema(connection)
}
pub(super) fn require_schema(connection: &Connection) -> Result<()> {
    delivery::require_schema(connection)?;
    let mut s = connection.prepare("SELECT sql FROM sqlite_schema WHERE tbl_name=?1")?;
    let rows: Vec<String> = s
        .query_map([TABLE], |r| r.get(0))?
        .collect::<std::result::Result<_, _>>()?;
    ensure!(
        rows.len() == SCHEMA.len() && SCHEMA.iter().all(|s| rows.iter().any(|r| r == s)),
        "account logon schema differs; preserve state"
    );
    Ok(())
}

fn intent(connection: &Connection, plan: &Plan) -> Result<Intent> {
    let resume = replay_resume(connection, plan)?;
    ensure!(
        resume.len() == 3,
        "account logon requires complete durable bootstrap readiness"
    );
    let ResumeEvent::Intent {
        nonce,
        pid,
        created,
        image_id,
        ..
    } = &resume[0]
    else {
        anyhow::bail!("account logon has no original bootstrap resume intent");
    };
    Ok(Intent {
        protocol: "alysis-account-logon-intent-v1".into(),
        operation: plan.operation.clone(),
        nonce: random_id()?,
        ready: nonce.clone(),
        bootstrap_pid: *pid,
        bootstrap_created: *created,
        image_id: *image_id,
        account: plan.execution.identity.binding.clone(),
        credential_generation: plan.credential_generation.clone(),
    })
}

fn validate(connection: &Connection, plan: &Plan, observed: &Intent) -> Result<()> {
    plan.validate()?;
    validate_random_id(&observed.nonce)?;
    let resume = replay_resume(connection, plan)?;
    ensure!(
        resume.len() == 3,
        "account logon requires complete bootstrap readiness"
    );
    let ResumeEvent::Intent {
        nonce,
        birth,
        pid,
        created,
        image_id,
    } = &resume[0]
    else {
        anyhow::bail!("account logon bootstrap binding missing");
    };
    ensure!(
        observed.protocol == "alysis-account-logon-intent-v1"
            && observed.operation == plan.operation
            && observed.nonce != plan.operation
            && &observed.nonce != nonce
            && &observed.nonce != birth
            && &observed.ready == nonce
            && observed.bootstrap_pid == *pid
            && observed.bootstrap_created == *created
            && observed.image_id == *image_id
            && observed.account == plan.execution.identity.binding
            && observed.credential_generation == plan.credential_generation,
        "account logon intent differs from original ready bootstrap or selected installation"
    );
    Ok(())
}

fn read(connection: &Connection, plan: &Plan) -> Result<Option<Intent>> {
    require_plan(connection, plan)?;
    let (count, max): (usize, Option<usize>) = connection.query_row(
        "SELECT count(*),max(length(CAST(intent_json AS BLOB))) FROM candidate_atomic_account_logon",
        [], |r| Ok((r.get(0)?, r.get(1)?)))?;
    ensure!(
        count <= 1 && max.unwrap_or(0) <= MAX_BYTES,
        "account logon intent inventory exceeds bounds"
    );
    if count == 0 {
        return Ok(None);
    }
    let encoded: Option<String> = connection.query_row(
        "SELECT intent_json FROM candidate_atomic_account_logon WHERE singleton=1 AND typeof(intent_json)='text'",
        [], |r|r.get(0)).optional()?;
    let observed: Intent =
        serde_json::from_str(&encoded.context("account logon intent missing or not text")?)
            .context("account logon intent malformed; preserve state")?;
    validate(connection, plan, &observed)?;
    Ok(Some(observed))
}

fn persist_intent(connection: &Connection, plan: &Plan, intent: &Intent) -> Result<()> {
    ensure!(
        read(connection, plan)?.is_none(),
        "account logon was already prepared; no retry/adoption"
    );
    validate(connection, plan, intent)?;
    let encoded = serde_json::to_string(intent)?;
    ensure!(encoded.len() <= MAX_BYTES, "account logon intent oversized");
    let tx = connection.unchecked_transaction()?;
    tx.execute(
        "INSERT INTO candidate_atomic_account_logon VALUES(1,?1)",
        [encoded],
    )?;
    tx.commit()
        .context("account logon intent commit uncertain; preserve original state")?;
    #[cfg(test)]
    AFTER_LOGON_INTENT.with(|slot| {
        if let Some(hook) = slot.borrow_mut().take() {
            hook(connection);
        }
    });
    ensure!(
        read(connection, plan)?.as_ref() == Some(intent),
        "account logon intent readback differs"
    );
    Ok(())
}

#[cfg(test)]
thread_local! {
    pub(crate) static AFTER_LOGON_INTENT:std::cell::RefCell<Option<Box<dyn FnOnce(&Connection)>>>=const{std::cell::RefCell::new(None)};
}

/// Owns the original running-bootstrap guard and channel. It cannot be rebuilt
/// from the persisted intent, cloned, or used to extract credentials or resume a
/// child. Delivery errors consume and drain the original guard; intent is retained.
pub(crate) struct PreparedAccountLogon<'j, 'p, 'a, 'lease> {
    running: RunningBootstrap<'j, 'p, 'a, 'lease>,
    intent: Intent,
}

impl<'j, 'p, 'a, 'lease> RunningBootstrap<'j, 'p, 'a, 'lease> {
    pub(crate) fn prepare_account_logon(self) -> Result<PreparedAccountLogon<'j, 'p, 'a, 'lease>> {
        let RunningBootstrap { birth, _channel } = self;
        // Recheck the original authenticated server endpoint before any write.
        let channel = _channel.into_role(true)?;
        birth.owner.verify()?;
        let plan = birth.owner.plan;
        let expected = intent(plan.database.connection()?, &plan.plan)?;
        ensure!(
            birth.child.durable_observation()?
                == (
                    expected.bootstrap_pid,
                    expected.bootstrap_created,
                    expected.image_id
                ),
            "account logon original bootstrap changed"
        );
        persist_intent(plan.database.connection()?, &plan.plan, &expected)?;
        let prepared = PreparedAccountLogon {
            running: RunningBootstrap {
                birth,
                _channel: channel,
            },
            intent: expected,
        };
        prepared.verify()
    }
}

impl<'j, 'p, 'a, 'lease> PreparedAccountLogon<'j, 'p, 'a, 'lease> {
    // Consuming verification cannot leave a reusable guard after an error.
    // Only the child delivery module borrows this validation boundary.
    fn verify(self) -> Result<Self> {
        let Self { running, intent } = self;
        let RunningBootstrap { birth, _channel } = running;
        let channel = _channel.into_role(true)?;
        birth.owner.verify()?;
        let plan = birth.owner.plan;
        ensure!(
            read(plan.database.connection()?, &plan.plan)?.as_ref() == Some(&intent),
            "live account logon preparation differs from its durable intent"
        );
        ensure!(
            birth.child.durable_observation()?
                == (
                    intent.bootstrap_pid,
                    intent.bootstrap_created,
                    intent.image_id
                ),
            "prepared original bootstrap changed"
        );
        Ok(Self {
            running: RunningBootstrap {
                birth,
                _channel: channel,
            },
            intent,
        })
    }
}

#[cfg(test)]
#[path = "account_bootstrap_logon_intent_tests.rs"]
mod tests;
