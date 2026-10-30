//! Fixed read-only account entry and one bound source-side resume request.
//! Data supplied to the entry is not authentication or cleanup authority. The
//! broker independently verifies the original child, image, token and strict job.
use super::*;

pub(crate) const ENVIRONMENT_KEY: &str = "ALYSIS_ACCOUNT_SELF_CHECK";
pub(crate) const EXIT_CODE: u32 = 0x4150;
const PREFIX: &[u8] = b"alysis-account-self-check-resume-v1:";

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Request {
    pub birth: BirthRequest,
    pub nonce: String,
    pub pid: u32,
    pub created: u64,
}
impl Request {
    pub(crate) fn validate(&self) -> Result<()> {
        self.birth.validate()?;
        validate_random_id(&self.nonce)?;
        if let Purpose::TrustedRpcSelfCheckV1 { initializer, .. } = &self.birth.purpose {
            ensure!(
                &self.nonce != initializer,
                "RPC self-check resume nonce overlaps initializer"
            );
        }
        ensure!(
            self.birth.purpose.is_self_check(),
            "account resume purpose is not fixed self-check"
        );
        ensure!(
            self.pid != 0
                && self.pid != self.birth.selection.bootstrap_pid
                && self.created >= self.birth.selection.bootstrap_created
                && ![
                    &self.birth.nonce,
                    &self.birth.selection.operation,
                    &self.birth.selection.logon,
                    &self.birth.selection.ready
                ]
                .contains(&&self.nonce),
            "account resume identity differs"
        );
        Ok(())
    }
    pub(crate) fn frame(&self, phase: &str) -> Result<Vec<u8>> {
        self.validate()?;
        ensure!(
            matches!(phase, "resume" | "resumed" | "exited"),
            "invalid account resume phase"
        );
        let mut value = PREFIX.to_vec();
        value.extend_from_slice(phase.as_bytes());
        value.push(b':');
        value.extend_from_slice(&serde_json::to_vec(self)?);
        ensure!(value.len() <= 6144, "account resume frame exceeds bounds");
        Ok(value)
    }
    pub(crate) fn decode(frame: &[u8], birth: &BirthRequest, child: &SpawnedChild) -> Result<Self> {
        let mut prefix = PREFIX.to_vec();
        prefix.extend_from_slice(b"resume:");
        ensure!(
            frame.len() <= 6144 && frame.starts_with(&prefix),
            "invalid account resume frame"
        );
        let request: Self = serde_json::from_slice(&frame[prefix.len()..])
            .map_err(|_| anyhow::anyhow!("invalid account resume request"))?;
        request.validate()?;
        ensure!(
            &request.birth == birth
                && request.pid == unsafe { GetProcessId(child.process()) }
                && request.created
                    == super::super::super::super::pull::creation_time(child.process())?,
            "account resume is not original child"
        );
        Ok(request)
    }
}

pub(super) fn entry_arguments() -> String {
    #[cfg(test)]
    return "--exact account_control::bootstrap::entry::credential_wire::child::probe::tests::account_self_check_entry_fixture --ignored --nocapture --test-threads=1".into();
    #[cfg(not(test))]
    format!(
        "{} candidate-account-self-check",
        crate::cli::SRT_WIN_DISPATCH_ARG1
    )
}

/// Fixed entry with a distinctive exit code. The unused-profile purpose admits
/// one authenticated SDK create/remove round trip, with no consumers or commands.
/// The paired owner has a separate live profile lifecycle; a cfg(test) fixed
/// writer authenticates its original source before synthetic writes. Environment
/// data alone grants no effects. No arbitrary command purpose is admitted here.
pub(crate) fn run() -> Result<()> {
    crate::account_impersonation::require_process_identity()?;
    let bytes = std::env::var(ENVIRONMENT_KEY).context("account self-check binding missing")?;
    ensure!(
        bytes.len() <= 4608,
        "account self-check binding exceeds bounds"
    );
    let birth: BirthRequest = serde_json::from_str(&bytes)
        .map_err(|_| anyhow::anyhow!("invalid account self-check binding"))?;
    birth.validate()?;
    ensure!(
        birth.purpose.is_self_check(),
        "account self-check purpose differs"
    );
    let selected = &birth.selection;
    ensure!(
        crate::user::local_user_sid(&selected.account.account_name)?.as_deref()
            == Some(&selected.account.account_sid)
            && crate::user::local_group_sid(crate::account_install::ACCOUNT_GROUP)?.as_deref()
                == Some(&selected.account.group_sid),
        "account self-check native identities differ"
    );
    let peer = ExpectedPeer::retain(unsafe { GetCurrentProcess() })?;
    peer.token.require_dedicated_runner(
        &selected.account.account_sid,
        selected.session,
        &selected.account.group_sid,
    )?;
    ensure!(
        std::process::id() != selected.bootstrap_pid
            && super::super::super::super::pull::creation_time(unsafe { GetCurrentProcess() })?
                >= selected.bootstrap_created,
        "account self-check birth identity differs"
    );
    // This local observation is only a self-check. Exact original job membership
    // and policy are verified separately by the retained broker guard.
    let mut member = windows::core::BOOL::default();
    unsafe {
        windows::Win32::System::JobObjects::IsProcessInJob(GetCurrentProcess(), None, &mut member)?;
    }
    ensure!(member.as_bool(), "account self-check has no containing job");
    if let Purpose::TrustedRpcSelfCheckV1 { package_sid, .. } = &birth.purpose {
        let image = crate::account_state::image::account::AccountImageObserver::prepare(
            &selected.account,
            selected.session,
            selected.image_id,
        )?;
        crate::account_state::execution::resources::read_only_account_rpc_probe(
            &selected.account.owner_sid,
            &selected.account.account_sid,
            package_sid,
            selected.session,
        )?;
        image.verify_current()?;
        #[cfg(test)]
        image.fixture_refusal_controls()?;
    }
    if birth.purpose.uses_handoff() {
        crate::account_control::handoff::client(&birth)?;
    }
    std::process::exit(EXIT_CODE as i32);
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    #[ignore = "fixed dedicated-account self-check subprocess on disposable host only"]
    fn account_self_check_entry_fixture() {
        run().unwrap();
    }

    #[test]
    fn account_self_check_wire_binds_purpose_nonce_birth_and_phase() {
        let selection = super::super::super::tests::selected();
        let mut birth =
            BirthRequest::new(selection, "6123456789abcdef0123456789abcdef".into()).unwrap();
        let mut request = Request {
            birth: birth.clone(),
            nonce: "7123456789abcdef0123456789abcdef".into(),
            pid: birth.selection.bootstrap_pid + 1,
            created: birth.selection.bootstrap_created + 1,
        };
        assert!(request.validate().is_err());
        birth.purpose = Purpose::TrustedSelfCheckV1;
        request.birth = birth;
        request.validate().unwrap();
        assert_ne!(
            request.frame("resume").unwrap(),
            request.frame("resumed").unwrap()
        );
        assert_ne!(
            request.frame("resumed").unwrap(),
            request.frame("exited").unwrap()
        );
        assert!(request.frame("exec").is_err());
        for case in 0..5 {
            let mut wrong = request.clone();
            match case {
                0 => wrong.nonce = wrong.birth.nonce.clone(),
                1 => wrong.pid = 0,
                2 => wrong.pid = wrong.birth.selection.bootstrap_pid,
                3 => wrong.created = 0,
                _ => wrong.birth.purpose = Purpose::ObserveOnly,
            }
            assert!(wrong.validate().is_err());
        }
        let mut value = serde_json::to_value(&request).unwrap();
        value["command"] = serde_json::json!("forbidden");
        assert!(serde_json::from_value::<Request>(value).is_err());
    }
    #[test]
    fn account_rpc_self_check_wire_refuses_wrong_subjects_and_extended_commands() {
        let selection = super::super::super::tests::selected();
        let mut birth =
            BirthRequest::new(selection, "6123456789abcdef0123456789abcdef".into()).unwrap();
        birth.purpose = Purpose::TrustedRpcSelfCheckV1 {
            package_sid: "S-1-15-2-1-2-3-4-5-6-7".into(),
            initializer: "8123456789abcdef0123456789abcdef".into(),
        };
        birth.validate().unwrap();
        let encoded = birth.encode().unwrap();
        assert!(
            serde_json::from_slice::<BirthRequest>(&encoded)
                .unwrap()
                .purpose
                .is_self_check()
        );
        for value in ["S-1-1-0", "not-a-sid", "S-1-15-2-1\\RPC Control"] {
            let mut wrong = birth.clone();
            if let Purpose::TrustedRpcSelfCheckV1 { package_sid, .. } = &mut wrong.purpose {
                *package_sid = value.into();
            }
            assert!(wrong.validate().is_err());
        }
        let mut value = serde_json::to_value(&birth).unwrap();
        value["purpose"]["trusted_rpc_self_check_v1"]["command"] = serde_json::json!("forbidden");
        assert!(serde_json::from_value::<BirthRequest>(value).is_err());
        let request = Request {
            pid: birth.selection.bootstrap_pid + 1,
            created: birth.selection.bootstrap_created + 1,
            nonce: "8123456789abcdef0123456789abcdef".into(),
            birth,
        };
        assert!(request.validate().is_err());
    }
}
