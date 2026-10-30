//! Original broker host profile retained alongside both original jobs and the
//! original namespace. Capture/deletion requires same-live-pair completion.
//! Cold rows, names and profile observation tokens never reconstruct ownership.
use super::*;
use crate::account_state::execution::resources::{NativeHostProfile, UsedNativeHostProfile};

const TABLE: &str = "candidate_paired_host_profile";
const MAX_EVENT: usize = 4096;
const SCHEMA: [&str; 4] = [
    "CREATE TABLE candidate_paired_host_profile(sequence INTEGER PRIMARY KEY CHECK(sequence BETWEEN 0 AND 5), event_json TEXT NOT NULL)",
    "CREATE TRIGGER paired_host_profile_no_update BEFORE UPDATE ON candidate_paired_host_profile BEGIN SELECT RAISE(ABORT,'paired host profile event is immutable'); END",
    "CREATE TRIGGER paired_host_profile_no_delete BEFORE DELETE ON candidate_paired_host_profile BEGIN SELECT RAISE(ABORT,'paired host profile event is immutable'); END",
    "CREATE TRIGGER paired_host_profile_no_replace BEFORE INSERT ON candidate_paired_host_profile WHEN EXISTS(SELECT 1 FROM candidate_paired_host_profile WHERE sequence=NEW.sequence) BEGIN SELECT RAISE(ABORT,'paired host profile event is immutable'); END",
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
        "paired host profile schema differs; preserve state"
    );
    Ok(())
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Binding {
    operation: String,
    namespace: String,
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
        retirement: [String; 2],
    },
    RemoveIntent {
        binding: Binding,
        folder_id: [u8; 24],
        retirement: [String; 2],
    },
    Removed {
        binding: Binding,
        folder_id: [u8; 24],
        retirement: [String; 2],
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
    super::require_new_nonce(plan, &binding.nonce)?;
    ensure!(
        binding.namespace == super::birth_nonce(sql, plan)? && binding.nonce != binding.namespace,
        "paired host profile namespace binding differs"
    );
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
            super::require_new_nonce(plan, nonce)?;
            ensure!(
                nonce != &binding.namespace,
                "paired host profile use aliases namespace"
            );
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
            let retirement = super::retirement_nonces(sql, plan)?;
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
            let retirement = super::retirement_nonces(sql, plan)?;
            ensure!(
                retirement.iter().all(|n| n != &binding.nonce)
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
        "SELECT count(*),max(length(CAST(event_json AS BLOB))) FROM candidate_paired_host_profile",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    ensure!(
        count <= 6 && max.unwrap_or(0) <= MAX_EVENT,
        "host profile inventory exceeds bound"
    );
    let mut s = sql.prepare(
        "SELECT sequence,event_json FROM candidate_paired_host_profile ORDER BY sequence",
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
        "INSERT INTO candidate_paired_host_profile VALUES(?1,?2)",
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

/// Held inside NamespacedPair after both original jobs and before the namespace.
/// Drop closes only original handles; it never deletes SDK storage or records
/// completion. No raw storage/token handles or reconstructed ownership escape.
pub(super) struct OwnedHostProfile<'p, 'a, 'lease> {
    native: Option<NativeHostProfile>,
    used: Option<UsedNativeHostProfile>,
    use_nonce: Option<String>,
    plan: &'p CommittedPlan<'a, 'lease>,
    binding: Binding,
    folder_id: [u8; 24],
    use_attempted: bool,
    retirement_attempted: bool,
}
impl<'p, 'a, 'lease> OwnedHostProfile<'p, 'a, 'lease> {
    fn verify_completed(
        &self,
        jobs: &Jobs<'_, '_, '_>,
        completed: &CompletedPair<'_, '_, '_, '_>,
    ) -> Result<[String; 2]> {
        ensure!(
            std::ptr::eq(completed.jobs, jobs) && std::ptr::eq(jobs.plan, self.plan),
            "paired host profile completion belongs to another original pair"
        );
        completed.verify()?;
        super::retirement_nonces(self.plan.database.connection()?, &self.plan.plan)
    }
    pub(super) fn verify_retired(&self, jobs: &Jobs<'_, '_, '_>) -> Result<()> {
        ensure!(
            std::ptr::eq(jobs.plan, self.plan) && self.retirement_attempted && self.used.is_none(),
            "paired host profile completion differs from original guard"
        );
        for role in [Role::Owner, Role::Consumers] {
            jobs.verify_empty_retired_role(role)?;
        }
        let retirement =
            super::retirement_nonces(self.plan.database.connection()?, &self.plan.plan)?;
        let mut expected = self.birth_events();
        if let Some(nonce) = &self.use_nonce {
            expected.push(Event::UseIntent {
                binding: self.binding.clone(),
                folder_id: self.folder_id,
                nonce: nonce.clone(),
            });
            expected.push(Event::Captured {
                binding: self.binding.clone(),
                folder_id: self.folder_id,
                nonce: nonce.clone(),
                retirement: retirement.clone(),
            });
        }
        expected.push(Event::RemoveIntent {
            binding: self.binding.clone(),
            folder_id: self.folder_id,
            retirement: retirement.clone(),
        });
        expected.push(Event::Removed {
            binding: self.binding.clone(),
            folder_id: self.folder_id,
            retirement,
        });
        ensure!(
            replay(self.plan.database.connection()?, &self.plan.plan)? == expected,
            "paired host profile original completion receipts differ"
        );
        self.native
            .as_ref()
            .context("paired retired native profile missing")?
            .verify_absent()
    }
    pub(super) fn create(plan: &'p CommittedPlan<'a, 'lease>) -> Result<Self> {
        plan.verify()?;
        let sql = plan.database.connection()?;
        ensure!(
            replay(sql, &plan.plan)?.is_empty(),
            "host profile birth already attempted"
        );
        let binding = Binding {
            operation: plan.plan.operation.clone(),
            namespace: super::birth_nonce(sql, &plan.plan)?,
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
        #[cfg(test)]
        HOST_PROFILE_CHECKPOINT.with(|s| -> Result<()> {
            if let Some(hook) = s.borrow_mut().as_mut() {
                hook("intent", sql, None)?;
            }
            Ok(())
        })?;
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
            use_attempted: false,
            retirement_attempted: false,
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
        ensure!(
            !std::mem::replace(&mut self.use_attempted, true),
            "paired host profile use already attempted"
        );
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
    pub(super) fn retire(
        &mut self,
        jobs: &Jobs<'_, '_, '_>,
        completed: &CompletedPair<'_, '_, '_, '_>,
    ) -> Result<()> {
        ensure!(
            !std::mem::replace(&mut self.retirement_attempted, true),
            "paired host profile retirement already attempted"
        );
        let retirement = self.verify_completed(jobs, completed)?;
        self.verify_open()?;
        let sql = self.plan.database.connection()?;
        #[cfg(test)]
        self.checkpoint("before_capture")?;
        if let Some(nonce) = &self.use_nonce {
            // Whole-job completion is a live capability, not a stored nonce.
            // Failed capture consumes only handles and cannot reach SDK deletion.
            self.verify_completed(jobs, completed)?;
            self.native = Some(
                self.used
                    .take()
                    .context("original in-use profile missing")?
                    .capture()?,
            );
            #[cfg(test)]
            self.checkpoint("observed_after_use")?;
            self.verify_completed(jobs, completed)?;
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
        self.verify_completed(jobs, completed)?;
        self.native
            .as_ref()
            .context("frozen original profile absent")?
            .verify_unused()?;
        self.native.as_mut().unwrap().close_original_trees()?;
        #[cfg(test)]
        self.checkpoint("closed")?;
        self.verify_completed(jobs, completed)?;
        self.native.as_mut().unwrap().delete_original()?;
        #[cfg(test)]
        self.checkpoint("deleted")?;
        self.native.as_ref().unwrap().verify_absent()?;
        #[cfg(test)]
        self.checkpoint("absent")?;
        self.verify_completed(jobs, completed)?;
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
#[path = "account_paired_host_profile_tests.rs"]
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

// Frozen diagnostic binding for outer resources. Replay never grants ownership.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct ResourceBinding {
    pub(super) birth: String,
    pub(super) use_nonce: Option<String>,
}
pub(super) fn resource_binding(sql: &Connection, plan: &Plan) -> Result<Option<ResourceBinding>> {
    let events = replay(sql, plan)?;
    if events.is_empty() {
        return Ok(None);
    }
    let Some(Event::Created { binding, .. }) = events.get(1) else {
        anyhow::bail!("outer resource requires original host profile birth");
    };
    let use_nonce = match events.get(2) {
        Some(Event::UseIntent { nonce, .. }) => Some(nonce.clone()),
        _ => None,
    };
    Ok(Some(ResourceBinding {
        birth: binding.nonce.clone(),
        use_nonce,
    }))
}
