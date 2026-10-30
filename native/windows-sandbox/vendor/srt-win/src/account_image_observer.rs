//! Dedicated-account image observation, separate from host launch-image authority.
//! No caller-selected executable, credential/installation readiness, raw handle,
//! launch path, fixture ancestry exception or cleanup capability is exposed.
use super::*;
use crate::account_install::AccountBinding;
use windows::Win32::Security::TOKEN_QUERY;
use windows::Win32::System::Threading::OpenProcessToken;

pub(crate) struct AccountImageObserver {
    binding: AccountBinding,
    session: u32,
    expected_id: [u8; 24],
    identity: Identity,
    parents: Vec<Pinned>,
    image: Pinned,
    security: Vec<SecurityShape>,
}
impl AccountImageObserver {
    pub(crate) fn prepare(
        binding: &AccountBinding,
        session: u32,
        expected_id: [u8; 24],
    ) -> Result<Self> {
        require_actor(binding, session)?;
        ensure!(
            expected_id != [0; 24],
            "account image expected identity missing"
        );
        let identity = Identity::selected(binding)?;
        let path = std::env::current_exe()?;
        let parent = path.parent().context("account image has no parent")?;
        let parents = fs::pin_session_path(parent, &identity.policy())?;
        let image = Pinned::open(&path, false, true)?;
        let security = parents
            .iter()
            .chain(std::iter::once(&image))
            .map(|pin| shape(&pin.security()?))
            .collect::<Result<Vec<_>>>()?;
        let observer = Self {
            binding: binding.clone(),
            session,
            expected_id,
            identity,
            parents,
            image,
            security,
        };
        observer.verify_current()?;
        Ok(observer)
    }
    #[cfg(test)]
    pub(crate) fn fixture_refusal_controls(&self) -> Result<()> {
        self.verify_current()?;
        let host = PinnedRunnerImage::prepare_bootstrap_selection(&self.binding)
            .err()
            .context("dedicated account obtained host image guard")?;
        ensure!(
            host.to_string().contains("bootstrap image owner differs"),
            "host image control refused for unrelated reason"
        );
        let mut wrong_id = self.expected_id;
        wrong_id[0] ^= 0x80;
        let wrong = Self::prepare(&self.binding, self.session, wrong_id)
            .err()
            .context("wrong account image file ID admitted")?;
        ensure!(
            wrong
                .to_string()
                .contains("account helper image or retained parent differs"),
            "image ID control refused for unrelated reason"
        );
        let wrong = Self::prepare(&self.binding, self.session ^ 1, self.expected_id)
            .err()
            .context("wrong account image session admitted")?;
        ensure!(
            wrong
                .to_string()
                .contains("account image observer session differs"),
            "image session control refused for unrelated reason"
        );
        self.verify_current()
    }
    /// Read-only process image comparison against the same full retained pins.
    pub(crate) fn verify_process(&self, process: HANDLE) -> Result<()> {
        self.verify_current()?;
        verify_process_path(process, &self.image.path)?;
        self.verify_current()
    }
    pub(crate) fn verify_current(&self) -> Result<()> {
        require_actor(&self.binding, self.session)?;
        ensure!(
            self.identity == Identity::selected(&self.binding)?,
            "account image native installation subjects changed"
        );
        ensure!(
            self.image.id.as_bytes() == self.expected_id
                && self.image.path == std::env::current_exe()?
                && self.parents.last().map(|pin| pin.path.as_path()) == self.image.path.parent(),
            "account helper image or retained parent differs from expected identity"
        );
        verify_pins(
            &self.identity,
            &self.parents,
            &self.image,
            &self.security,
            true,
        )
    }
}
fn require_actor(binding: &AccountBinding, session: u32) -> Result<()> {
    crate::account_impersonation::require_process_identity()?;
    binding.validate()?;
    ensure!(
        crate::sid::current_user_sid()? == binding.account_sid,
        "account image observer requires selected dedicated actor"
    );
    let mut observed = 0;
    unsafe {
        windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
            std::process::id(),
            &mut observed,
        )?;
    }
    ensure!(
        observed == session,
        "account image observer session differs"
    );
    let mut raw = HANDLE::default();
    unsafe {
        OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut raw)?;
    }
    let token = OwnedHandle(raw);
    crate::account_namespace::verify_noninheritable(token.raw())?;
    crate::account_token::runner_policy::require(
        token.raw(),
        &binding.account_sid,
        &binding.group_sid,
    )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn account_image_observer_refuses_host_actor_before_pinning_or_native_subject_lookup() {
        let binding = crate::account_install::synthetic_ready()
            .binding(AccountMode::Offline)
            .unwrap();
        let result = AccountImageObserver::prepare(&binding, 0, [1; 24]);
        assert!(
            result
                .err()
                .unwrap()
                .to_string()
                .contains("selected dedicated actor")
        );
    }
}
