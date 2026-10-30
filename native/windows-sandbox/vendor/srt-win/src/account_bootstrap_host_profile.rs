//! Original host profile under the atomic plan. Optional journaled use retains
//! its roots, then requires live whole-job completion before bounded recapture.
//! Trees/token close before namespace retirement. No cold adoption, account-
//! profile impersonation or ordinary-AppContainer initializer is enabled.
use super::super::resources::{NativeHostProfile, UsedNativeHostProfile};
use super::*;
use lifecycle::resume::logon::delivery::birth::retirement::CompletedJob;

const TABLE: &str = "candidate_atomic_host_profile";
const MAX_EVENT: usize = 4096;
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_atomic_host_profile(sequence INTEGER PRIMARY KEY CHECK(sequence BETWEEN 0 AND 5), event_json TEXT NOT NULL)",
    "CREATE TRIGGER atomic_host_profile_no_update BEFORE UPDATE ON candidate_atomic_host_profile BEGIN SELECT RAISE(ABORT,'atomic host profile event is immutable'); END",
    "CREATE TRIGGER atomic_host_profile_no_delete BEFORE DELETE ON candidate_atomic_host_profile BEGIN SELECT RAISE(ABORT,'atomic host profile event is immutable'); END",
    "CREATE TRIGGER atomic_host_profile_no_replace BEFORE INSERT ON candidate_atomic_host_profile WHEN EXISTS(SELECT 1 FROM candidate_atomic_host_profile WHERE sequence=NEW.sequence) BEGIN SELECT RAISE(ABORT,'atomic host profile event is immutable'); END",
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
        "atomic host profile schema differs; preserve state"
    );
    Ok(())
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Binding {
    operation: String,
    nonce: String,
    owner: String,
    container: String,
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
        folder_id: [u8; 24],
    },
    UseIntent {
        binding: Binding,
        folder_id: [u8; 24],
        nonce: String,
    },
    Captured {
        binding: Binding,
        folder_id: [u8; 24],
        nonce: String,
        retirement: String,
    },
    RemoveIntent {
        binding: Binding,
        folder_id: [u8; 24],
        retirement: String,
    },
    Removed {
        binding: Binding,
        folder_id: [u8; 24],
        retirement: String,
    },
}
fn validate(sql: &Connection, plan: &Plan, events: &[Event]) -> Result<()> {
    ensure!(events.len() <= 6, "host profile inventory exceeds bound");
    if events.is_empty() {
        return Ok(());
    }
    let Event::Intent { binding } = &events[0] else {
        anyhow::bail!("host profile intent absent")
    };
    validate_random_id(&binding.nonce)?;
    ensure!(
        binding.operation == plan.operation
            && binding.nonce != binding.operation
            && binding.owner == plan.execution.owner
            && binding.container == plan.execution.identity.container
            && binding.package_sid == plan.execution.identity.package_sid
            && binding.policy == 1,
        "host profile binding differs from original plan"
    );
    if let Some(event) = events.get(1) {
        let Event::Created {
            binding: born,
            folder_id,
        } = event
        else {
            anyhow::bail!("host profile birth receipt absent")
        };
        ensure!(
            born == binding && *folder_id != [0; 24],
            "host profile original storage differs"
        );
        let removal_index = if let Some(Event::UseIntent {
            binding: used,
            folder_id: id,
            nonce,
        }) = events.get(2)
        {
            validate_random_id(nonce)?;
            ensure!(
                used == binding
                    && id == folder_id
                    && nonce != &binding.nonce
                    && nonce != &binding.operation,
                "profile use intent differs from original birth"
            );
            if events.len() == 3 {
                return Ok(());
            }
            let retirement =
                lifecycle::resume::logon::delivery::birth::retirement::completed_nonce(sql, plan)?;
            ensure!(
                events[3]
                    == Event::Captured {
                        binding: binding.clone(),
                        folder_id: *folder_id,
                        nonce: nonce.clone(),
                        retirement
                    },
                "post-use profile capture lacks original job completion"
            );
            4
        } else {
            2
        };
        ensure!(
            events.len() <= removal_index + 2,
            "unexpected profile lifecycle suffix"
        );
        if let Some(event) = events.get(removal_index) {
            let retirement =
                lifecycle::resume::logon::delivery::birth::retirement::completed_nonce(sql, plan)?;
            ensure!(
                retirement != binding.nonce
                    && *event
                        == Event::RemoveIntent {
                            binding: binding.clone(),
                            folder_id: *folder_id,
                            retirement: retirement.clone()
                        },
                "host profile removal lacks original job retirement"
            );
            if let Some(event) = events.get(removal_index + 1) {
                ensure!(
                    *event
                        == Event::Removed {
                            binding: binding.clone(),
                            folder_id: *folder_id,
                            retirement
                        },
                    "host profile removal receipt differs"
                );
            }
        }
    }
    Ok(())
}
fn replay(sql: &Connection, plan: &Plan) -> Result<Vec<Event>> {
    require_plan(sql, plan)?;
    let (count, max): (usize, Option<usize>) = sql.query_row(
        "SELECT count(*),max(length(CAST(event_json AS BLOB))) FROM candidate_atomic_host_profile",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    ensure!(
        count <= 6 && max.unwrap_or(0) <= MAX_EVENT,
        "host profile inventory exceeds bound"
    );
    let mut s = sql.prepare(
        "SELECT sequence,event_json FROM candidate_atomic_host_profile ORDER BY sequence",
    )?;
    let mut events = Vec::new();
    for row in s.query_map([], |r| Ok((r.get::<_, usize>(0)?, r.get::<_, String>(1)?)))? {
        let (sequence, bytes) = row?;
        ensure!(
            sequence == events.len(),
            "host profile event sequence has a gap"
        );
        events.push(serde_json::from_str(&bytes).context("malformed host profile event")?);
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
    ensure!(bytes.len() <= MAX_EVENT, "host profile event exceeds bound");
    let tx = sql.unchecked_transaction()?;
    tx.execute(
        "INSERT INTO candidate_atomic_host_profile VALUES(?1,?2)",
        params![sequence, bytes],
    )?;
    #[cfg(test)]
    HOST_PROFILE_WRITE_CHECKPOINT.with(|s| {
        if let Some(hook) = s.borrow_mut().as_mut() {
            hook(sequence, sql);
        }
    });
    tx.commit()
        .context("host profile commit uncertain; preserve original references and evidence")?;
    ensure!(
        replay(sql, plan)? == expected,
        "host profile event readback differs"
    );
    Ok(())
}

/// Held inside NamespacedJob after its original job and before the namespace.
/// Drop closes only original handles; it never deletes SDK storage or records
/// completion. No raw storage/token handles or reconstructed ownership escape.
pub(super) struct OwnedHostProfile<'p, 'a, 'lease> {
    native: Option<NativeHostProfile>,
    used: Option<UsedNativeHostProfile>,
    use_nonce: Option<String>,
    plan: &'p CommittedPlan<'a, 'lease>,
    binding: Binding,
    folder_id: [u8; 24],
}
impl<'p, 'a, 'lease> OwnedHostProfile<'p, 'a, 'lease> {
    pub(super) fn create(plan: &'p CommittedPlan<'a, 'lease>) -> Result<Self> {
        plan.verify()?;
        let sql = plan.database.connection()?;
        ensure!(
            replay(sql, &plan.plan)?.is_empty(),
            "host profile birth already attempted"
        );
        let binding = Binding {
            operation: plan.plan.operation.clone(),
            nonce: random_id()?,
            owner: plan.plan.execution.owner.clone(),
            container: plan.plan.execution.identity.container.clone(),
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
        plan.verify()?;
        let mut native = NativeHostProfile::default();
        let folder_id = native.create_host(&binding.container, &binding.owner)?;
        let guard = Self {
            native: Some(native),
            used: None,
            use_nonce: None,
            plan,
            binding: binding.clone(),
            folder_id,
        };
        #[cfg(test)]
        guard.checkpoint("born")?;
        guard.native.as_ref().unwrap().verify_unused()?;
        append(sql, &plan.plan, Event::Created { binding, folder_id })?;
        #[cfg(test)]
        guard.checkpoint("ready")?;
        guard.verify_open()?;
        Ok(guard)
    }
    fn birth_events(&self) -> Vec<Event> {
        vec![
            Event::Intent {
                binding: self.binding.clone(),
            },
            Event::Created {
                binding: self.binding.clone(),
                folder_id: self.folder_id,
            },
        ]
    }
    pub(super) fn begin_use(&mut self) -> Result<()> {
        self.verify_open()?;
        ensure!(
            self.use_nonce.is_none() && self.used.is_none(),
            "profile use already attempted"
        );
        let nonce = random_id()?;
        append(
            self.plan.database.connection()?,
            &self.plan.plan,
            Event::UseIntent {
                binding: self.binding.clone(),
                folder_id: self.folder_id,
                nonce: nonce.clone(),
            },
        )?;
        self.plan.verify()?;
        self.used = Some(
            self.native
                .take()
                .context("original unused profile absent")?
                .begin_use()?,
        );
        self.use_nonce = Some(nonce);
        #[cfg(test)]
        self.checkpoint("using")?;
        self.verify_open()
    }
    pub(super) fn verify_open(&self) -> Result<()> {
        self.plan.verify()?;
        let mut expected = self.birth_events();
        if let Some(nonce) = &self.use_nonce {
            expected.push(Event::UseIntent {
                binding: self.binding.clone(),
                folder_id: self.folder_id,
                nonce: nonce.clone(),
            });
            ensure!(self.native.is_none(), "profile use retained a frozen guard");
            self.used
                .as_ref()
                .context("original in-use profile absent")?
                .verify()?;
        } else {
            ensure!(self.used.is_none(), "unjournaled profile use");
            self.native
                .as_ref()
                .context("original unused profile absent")?
                .verify_unused()?;
        }
        ensure!(
            replay(self.plan.database.connection()?, &self.plan.plan)? == expected,
            "host profile birth/use receipts differ"
        );
        Ok(())
    }
    pub(super) fn retire(&mut self, completed: &CompletedJob<'_, '_, '_>) -> Result<()> {
        let retirement = completed.verify_for(self.plan)?.to_owned();
        self.verify_open()?;
        let sql = self.plan.database.connection()?;
        if let Some(nonce) = &self.use_nonce {
            // Whole-job completion is a live capability, not a stored nonce.
            // Failed capture consumes only handles and cannot reach SDK deletion.
            completed.verify_for(self.plan)?;
            self.native = Some(
                self.used
                    .take()
                    .context("original in-use profile missing")?
                    .capture()?,
            );
            #[cfg(test)]
            self.checkpoint("observed_after_use")?;
            completed.verify_for(self.plan)?;
            append(
                sql,
                &self.plan.plan,
                Event::Captured {
                    binding: self.binding.clone(),
                    folder_id: self.folder_id,
                    nonce: nonce.clone(),
                    retirement: retirement.clone(),
                },
            )?;
            #[cfg(test)]
            self.checkpoint("captured")?;
        }
        append(
            sql,
            &self.plan.plan,
            Event::RemoveIntent {
                binding: self.binding.clone(),
                folder_id: self.folder_id,
                retirement: retirement.clone(),
            },
        )?;
        #[cfg(test)]
        self.checkpoint("retiring")?;
        completed.verify_for(self.plan)?;
        self.native
            .as_ref()
            .context("frozen original profile absent")?
            .verify_unused()?;
        self.native.as_mut().unwrap().close_original_trees()?;
        #[cfg(test)]
        self.checkpoint("closed")?;
        completed.verify_for(self.plan)?;
        self.native.as_mut().unwrap().delete_original()?;
        #[cfg(test)]
        self.checkpoint("deleted")?;
        self.native.as_ref().unwrap().verify_absent()?;
        #[cfg(test)]
        self.checkpoint("absent")?;
        completed.verify_for(self.plan)?;
        append(
            sql,
            &self.plan.plan,
            Event::Removed {
                binding: self.binding.clone(),
                folder_id: self.folder_id,
                retirement,
            },
        )?;
        #[cfg(test)]
        self.checkpoint("completed")?;
        self.native.as_ref().unwrap().verify_absent()
    }
    #[cfg(test)]
    fn checkpoint(&self, phase: &str) -> Result<()> {
        HOST_PROFILE_CHECKPOINT.with(|s| {
            if let Some(hook) = s.borrow_mut().as_mut() {
                hook(phase, self.plan.database.connection()?, self.used.as_ref())?;
            }
            Ok(())
        })
    }
}
#[cfg(test)]
thread_local! { pub(crate) static HOST_PROFILE_CHECKPOINT: std::cell::RefCell<Option<Box<dyn FnMut(&str,&Connection,Option<&UsedNativeHostProfile>)->Result<()>>>> = const {std::cell::RefCell::new(None)}; }
#[cfg(test)]
thread_local! { pub(crate) static HOST_PROFILE_WRITE_CHECKPOINT: std::cell::RefCell<Option<Box<dyn FnMut(usize,&Connection)>>> = const {std::cell::RefCell::new(None)}; }

#[cfg(test)]
#[path = "account_bootstrap_host_profile_tests.rs"]
mod tests;

// Diagnostic nonce only. It cannot reconstruct the retained native profile.
pub(super) fn use_nonce(sql: &Connection, plan: &Plan) -> Result<String> {
    let events = replay(sql, plan)?;
    let Some(Event::UseIntent { nonce, .. }) = events.get(2) else {
        anyhow::bail!("initializer requires original profile use intent");
    };
    Ok(nonce.clone())
}
impl OwnedHostProfile<'_, '_, '_> {
    pub(super) fn require_initializer(&self, plan: &CommittedPlan<'_, '_>) -> Result<()> {
        ensure!(
            std::ptr::eq(self.plan, plan) && self.use_nonce.is_some() && self.used.is_some(),
            "initializer requires original in-use profile"
        );
        self.verify_open()
    }
}
