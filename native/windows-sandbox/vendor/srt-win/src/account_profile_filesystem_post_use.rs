//! Read-only post-use inventory anchored to the original package object.
//! This is not quiescence, exclusive birth or permission to delete a profile.
use super::*;

/// No path, deserialization, Clone or raw handle API. Descendant contents may
/// change, but the root's identity, location, security and attributes may not.
pub(crate) struct ProfileFilesystemRoot {
    root: OriginalFile,
    initial: Metadata,
    ancestors: Vec<Anchor>,
}

impl RetainedProfileFilesystem {
    pub(crate) fn retain_root_for_use(self) -> Result<ProfileFilesystemRoot> {
        ensure!(
            matches!(self.observe()?, FilesystemObservation::Present),
            "cannot retain a changed or deleted profile root for use"
        );
        let mut nodes = self.nodes.into_iter();
        let root = nodes.next().context("profile root missing")?;
        // Checked closes allow applications to edit descendants. The original
        // root and every ancestor survive; no name is reopened or adopted.
        for node in nodes.rev() {
            node.file.close()?;
        }
        let retained = ProfileFilesystemRoot {
            root: root.file,
            initial: root.initial.metadata,
            ancestors: self.ancestors,
        };
        retained.unchanged()?;
        Ok(retained)
    }
}

impl ProfileFilesystemRoot {
    /// Test-only original-object adapter. This still requires the raw account
    /// and SDK-selected package; it grants no filesystem or cleanup authority.
    #[cfg(test)]
    pub(crate) fn prepare_fixed_lowbox(
        &self,
        container: &str,
        account_sid: &str,
        expected_package_id: [u8; 24],
        use_nonce: &str,
    ) -> Result<super::FixedLowboxFilesystem> {
        crate::account_install::validate_random_id(use_nonce)?;
        ensure!(
            expected_package_id != [0; 24] && self.initial.id == expected_package_id,
            "fixed lowbox filesystem package binding differs"
        );
        self.unchanged()?;
        let sdk_path = package_path(container, account_sid)?;
        let prepared =
            super::lowbox_fixture::prepare(&self.root, &self.initial, &sdk_path, use_nonce)?;
        self.unchanged()?;
        Ok(prepared)
    }

    pub(crate) fn unchanged(&self) -> Result<()> {
        crate::account_impersonation::require_process_identity()?;
        ensure!(!self.ancestors.is_empty(), "profile ancestors missing");
        for anchor in &self.ancestors {
            ensure!(
                anchor.file.metadata()? == anchor.initial,
                "profile ancestor changed during use"
            );
        }
        ensure!(
            self.root.metadata()? == self.initial,
            "original profile root changed during use"
        );
        Ok(())
    }

    /// Consumes the original root into a new bounded, strictly observed tree.
    /// Failure closes handles only; the caller must preserve its journal. No
    /// SDK call, retry, new cleanup authority or restart recovery is supplied.
    /// A future lifecycle must independently prove all writers are stopped.
    pub(crate) fn capture_after_use(self) -> Result<RetainedProfileFilesystem> {
        self.unchanged()?;
        let mut result = RetainedProfileFilesystem {
            nodes: Vec::new(),
            ancestors: self.ancestors,
        };
        result.append(self.root, 0, &mut 0)?;
        ensure!(
            result.nodes[0].initial.metadata == self.initial,
            "profile root changed during post-use inventory"
        );
        ensure!(
            matches!(result.observe()?, FilesystemObservation::Present),
            "post-use profile disappeared"
        );
        Ok(result)
    }
}
