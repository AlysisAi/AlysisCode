//! Allow bounded SDK data changes through original roots, never name adoption.
//! The SID mapping remains immutable. No cleanup or quiescence authority here.
use super::*;

pub(crate) struct ProfileRegistryRoots {
    sdk: RetainedKey,
    initial: KeyMetadata,
    mapping: Tree,
    ancestors: Vec<Anchor>,
    sdk_ancestors: Vec<Anchor>,
}

impl RetainedProfileRegistry {
    pub(crate) fn retain_roots_for_use(self) -> Result<ProfileRegistryRoots> {
        ensure!(
            matches!(self.observe()?, KeyObservation::Present(_)),
            "cannot retain changed or deleted registry roots for use"
        );
        let mut nodes = self.sdk.nodes.into_iter();
        let root = nodes.next().context("SDK registry root missing")?;
        for node in nodes.rev() {
            node.key.close()?;
        }
        let retained = ProfileRegistryRoots {
            sdk: root.key,
            initial: root.initial.metadata,
            mapping: self.mapping,
            ancestors: self.ancestors,
            sdk_ancestors: self.sdk_ancestors,
        };
        retained.unchanged()?;
        Ok(retained)
    }
}

impl ProfileRegistryRoots {
    #[cfg(test)]
    pub(crate) fn fixture_close(self) -> Result<()> {
        self.unchanged()?;
        self.sdk.close()?;
        self.mapping.close()?;
        close_ancestors(self.sdk_ancestors)?;
        close_ancestors(self.ancestors)
    }
    /// Hosted test only: fixed synthetic data beneath the original SDK root.
    /// No mapping writes, new token, root reopen or production mutation API.
    #[cfg(test)]
    pub(crate) fn fixture_add_data(&self, unsupported: bool) -> Result<()> {
        ensure!(
            std::env::var("ALYSIS_RUN_DISPOSABLE_ATOMIC_PROFILE").as_deref()
                == Ok("github-hosted-atomic-profile-only")
                && std::env::var("GITHUB_ACTIONS").as_deref() == Ok("true")
                && std::env::var("RUNNER_ENVIRONMENT").as_deref() == Ok("github-hosted"),
            "profile registry mutation requires disposable hosted admission"
        );
        self.fixture_write_data("AtomicRuntimeData", unsupported)
    }
    /// Fixed consumer fixture; the authenticated caller binds the original
    /// owner/profile/use before acquiring this SDK-root observation.
    #[cfg(test)]
    pub(crate) fn fixture_writer_data(&self, nonce: &str, unsupported: bool) -> Result<()> {
        crate::account_install::validate_random_id(nonce)?;
        self.fixture_write_data(&format!("FixedConsumer-{nonce}"), unsupported)
    }
    #[cfg(test)]
    fn fixture_write_data(&self, name: &str, unsupported: bool) -> Result<()> {
        self.unchanged()?;
        let name: Vec<u16> = name.encode_utf16().chain(Some(0)).collect();
        let mut raw = HKEY::default();
        let mut disposition = REG_CREATE_KEY_DISPOSITION::default();
        unsafe {
            RegCreateKeyExW(
                self.sdk.raw_for_inventory()?,
                PCWSTR(name.as_ptr()),
                None,
                PCWSTR::null(),
                REG_OPTION_NON_VOLATILE,
                KEY_ALL_ACCESS | KEY_WOW64_64KEY,
                None,
                &mut raw,
                Some(&mut disposition),
            )
        }
        .ok()?;
        let child = RetainedKey { key: Some(raw) };
        ensure!(
            disposition == REG_CREATED_NEW_KEY,
            "runtime fixture key collision"
        );
        crate::account_namespace::verify_noninheritable(HANDLE(raw.0))?;
        let kind = if unsupported { REG_LINK } else { REG_BINARY };
        let bytes = b"bounded synthetic runtime setting";
        unsafe {
            RegSetValueExW(
                raw,
                windows::core::w!("SyntheticValue"),
                None,
                kind,
                Some(bytes),
            )
        }
        .ok()?;
        let mut observed_kind = REG_VALUE_TYPE::default();
        let mut observed = [0u8; 64];
        let mut size = observed.len() as u32;
        unsafe {
            RegQueryValueExW(
                raw,
                windows::core::w!("SyntheticValue"),
                None,
                Some(&mut observed_kind),
                Some(observed.as_mut_ptr()),
                Some(&mut size),
            )
        }
        .ok()?;
        ensure!(
            observed_kind == kind
                && size as usize == bytes.len()
                && &observed[..size as usize] == bytes,
            "runtime registry fixture readback differs"
        );
        child.close()?;
        self.unchanged()
    }

    /// Read-only assertion of fixed synthetic data through original SDK roots.
    /// The child is reopened diagnostically; this does not grant cleanup authority.
    #[cfg(test)]
    pub(crate) fn fixture_require_runtime_value(&self) -> Result<()> {
        self.unchanged()?;
        let child = self
            .sdk
            .child(&"AtomicRuntimeData".encode_utf16().collect::<Vec<_>>())?;
        let observed = child.snapshot()?;
        ensure!(
            observed.children.is_empty()
                && observed.values
                    == [Value {
                        name: "SyntheticValue".encode_utf16().collect(),
                        kind: REG_BINARY.0,
                        data: b"bounded synthetic runtime setting".to_vec()
                    }],
            "original host registry runtime value changed after account removal"
        );
        child.close()?;
        self.unchanged()
    }
    pub(crate) fn unchanged(&self) -> Result<()> {
        crate::account_impersonation::require_process_identity()?;
        unchanged_ancestors(&self.ancestors)?;
        unchanged_ancestors(&self.sdk_ancestors)?;
        self.sdk.refuse_link()?;
        ensure!(
            self.sdk.metadata()? == self.initial,
            "original SDK registry root changed during use"
        );
        ensure!(
            self.mapping.observe()?.is_some(),
            "original profile SID mapping was deleted"
        );
        Ok(())
    }

    /// Original root handles are moved, never duplicated or reopened by name.
    /// A failed inventory only closes handles; retain pending lifecycle intent.
    /// Sequential observation cannot prove that command writers have stopped.
    pub(crate) fn capture_after_use(self) -> Result<RetainedProfileRegistry> {
        self.unchanged()?;
        let mut bytes = 0;
        for node in &self.mapping.nodes {
            charge_snapshot(&mut bytes, &node.initial)?;
        }
        let sdk = Tree::capture(self.sdk, &mut bytes)?;
        ensure!(
            sdk.nodes[0].initial.metadata == self.initial,
            "SDK registry root changed during post-use inventory"
        );
        let result = RetainedProfileRegistry {
            sdk,
            mapping: self.mapping,
            ancestors: self.ancestors,
            sdk_ancestors: self.sdk_ancestors,
        };
        ensure!(
            matches!(result.observe()?, KeyObservation::Present(_)),
            "post-use registry roots disappeared"
        );
        Ok(result)
    }
}
