//! Recoverable ACLs for a unique, never-reused AppContainer package SID.
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use srt_win::acl::{DenyMask, GrantMask, SbAceSet};
use srt_win::path_id::{FileId, canonical_parent_of, canonicalize_path, capture_file_id};
use std::collections::BTreeMap;

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ContainerPolicy {
    pub name: String,
    pub sid: String,
    objects: Vec<Object>,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Object {
    path: String,
    file_id: Vec<u8>,
    grant: Option<String>,
    deny: Option<String>,
    deny_fdc: bool,
    // Read-only verification, never an ACL mutation/recovery intent.
    #[serde(default)]
    system_parent: bool,
}

impl ContainerPolicy {
    pub fn build(
        read: &[String],
        write: &[String],
        traverse: &[String],
        deny_read: &[String],
        deny_write: &[String],
    ) -> Result<Self> {
        use windows::Win32::Security::Cryptography::{
            BCRYPT_USE_SYSTEM_PREFERRED_RNG, BCryptGenRandom,
        };
        let mut random = [0u8; 16];
        unsafe {
            BCryptGenRandom(None, &mut random, BCRYPT_USE_SYSTEM_PREFERRED_RNG).ok()?;
        }
        let suffix: String = random.iter().map(|b| format!("{b:02x}")).collect();
        let name = format!("Alysis.Execution.{suffix}");
        let sid = srt_win::appcontainer::derive(&name)?.string()?;
        let mut map: BTreeMap<String, (Option<String>, Option<String>, bool)> = BTreeMap::new();
        // The stronger grant wins for overlaps, and a deny always remains.
        for (roots, level) in [(traverse, "traverse"), (read, "read"), (write, "modify")] {
            for path in roots {
                let (path, _) = canonicalize_path(path)?;
                map.entry(path).or_default().0 = Some(level.to_owned());
            }
        }
        for (roots, level) in [(deny_write, "write"), (deny_read, "read")] {
            for path in roots {
                let (path, _) = canonicalize_path(path)?;
                if let Some(parent) = canonical_parent_of(&path) {
                    map.entry(parent).or_default().2 = true;
                }
                map.entry(path).or_default().1 = Some(level.to_owned());
            }
        }
        // Package SID deny ACEs are not a read boundary. Never install a
        // positive package grant that overlaps a confidential read-deny root.
        for denied in deny_read {
            let (denied, _) = canonicalize_path(denied)?;
            let denied = denied.trim_end_matches('\\').to_lowercase();
            for allowed in read.iter().chain(write) {
                let (allowed, _) = canonicalize_path(allowed)?;
                let allowed = allowed.trim_end_matches('\\').to_lowercase();
                ensure!(
                    allowed != denied
                        && !allowed.starts_with(&(denied.clone() + "\\"))
                        && !denied.starts_with(&(allowed + "\\")),
                    "AppContainer read grant overlaps a confidential path"
                );
            }
        }
        let objects = map
            .into_iter()
            .map(|(path, (grant, deny, deny_fdc))| {
                let id = capture_file_id(&path)?;
                let system_parent = grant.is_none()
                    && deny.is_none()
                    && deny_fdc
                    && srt_win::acl::system_parent_protected(&path, id)?;
                let file_id = id.as_bytes().to_vec();
                Ok(Object {
                    path,
                    file_id,
                    grant,
                    deny,
                    deny_fdc,
                    system_parent,
                })
            })
            .collect::<Result<Vec<_>>>()?;
        Ok(Self { name, sid, objects })
    }

    pub fn validate(&self) -> Result<()> {
        ensure!(
            srt_win::appcontainer::derive(&self.name)?.string()? == self.sid,
            "AppContainer recovery identity mismatch"
        );
        for object in &self.objects {
            ensure!(
                !object.system_parent
                    || (object.grant.is_none() && object.deny.is_none() && object.deny_fdc),
                "system parent cannot contain ACL mutation intent"
            );
        }
        Ok(())
    }

    pub fn apply(&self) -> Result<()> {
        self.validate()?;
        for object in &self.objects {
            if object.system_parent {
                ensure!(
                    srt_win::acl::system_parent_protected(
                        &object.path,
                        FileId::from_bytes(&object.file_id)?,
                    )?,
                    "system parent protection changed before execution"
                );
                continue;
            }
            let grant = match object.grant.as_deref() {
                Some("traverse") => Some(GrantMask::Traverse),
                Some("read") => Some(GrantMask::ReadOnly),
                Some("modify") => Some(GrantMask::Modify),
                None => None,
                _ => anyhow::bail!("invalid recorded grant"),
            };
            let deny = match object.deny.as_deref() {
                Some("read") => Some(DenyMask::ReadDeny),
                Some("write") => Some(DenyMask::WriteDeny),
                None => None,
                _ => anyhow::bail!("invalid recorded deny"),
            };
            srt_win::acl::apply_sandbox_aces_for_id(
                &object.path,
                &self.sid,
                SbAceSet {
                    grant,
                    deny,
                    deny_fdc: object.deny_fdc,
                    ..Default::default()
                },
                Some(FileId::from_bytes(&object.file_id)?),
            )?;
        }
        Ok(())
    }

    pub fn cleanup(&self) -> Result<()> {
        self.validate()?;
        for object in &self.objects {
            if object.system_parent {
                continue; // No ACE was installed, even if the descriptor changed later.
            }
            let expected = FileId::from_bytes(&object.file_id)?;
            let path = match capture_file_id(&object.path) {
                Ok(actual) if actual == expected => object.path.clone(),
                Ok(_) => match srt_win::path_id::locate_by_file_id_checked(&expected)
                    .context("cannot locate moved AppContainer ACL; recovery remains pending")?
                {
                    Some(path) => path,
                    None => continue, // Exact object was deleted; no ACE can remain on it.
                },
                Err(error) if srt_win::path_id::is_not_found(&error) => {
                    match srt_win::path_id::locate_by_file_id_checked(&expected).context(
                        "cannot locate missing AppContainer ACL; recovery remains pending",
                    )? {
                        Some(path) => path,
                        None => continue,
                    }
                }
                Err(error) => {
                    return Err(error).context("cannot inspect AppContainer ACL for recovery");
                }
            };
            srt_win::acl::apply_sandbox_aces_for_id(
                &path,
                &self.sid,
                SbAceSet::default(),
                Some(expected),
            )?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn verified_system_parent_is_never_mutated_or_recovered() {
        let path = std::env::var("ProgramFiles").unwrap();
        let (path, _) = canonicalize_path(&path).unwrap();
        let mut policy = ContainerPolicy::build(&[], &[], &[], &[], &[]).unwrap();
        policy.objects.push(Object {
            file_id: capture_file_id(&path).unwrap().as_bytes().to_vec(),
            path,
            grant: None,
            deny: None,
            deny_fdc: true,
            system_parent: true,
        });
        policy.apply().unwrap();
        policy.cleanup().unwrap();
        // Recovery must not touch the path even after it becomes unavailable:
        // no mutation intent was ever recorded for this object.
        policy.objects[0].path = r"C:\not-a-recorded-mutation".into();
        policy.cleanup().unwrap();
        policy.objects[0].grant = Some("read".into());
        assert!(policy.validate().is_err());
    }

    #[test]
    fn older_recovery_objects_still_require_acl_cleanup() {
        let object: Object = serde_json::from_value(serde_json::json!({
            "path": r"C:\old-parent", "file_id": [0], "grant": null,
            "deny": null, "deny_fdc": true
        })).unwrap();
        assert!(!object.system_parent);
    }
}
