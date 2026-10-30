//! Closed paired framing and transport for the fixed suspended initializer.
//! These bytes never prove original job/resource ownership. The live coordinator
//! must verify the consumer role and every original resource before dispatch.
#![allow(dead_code)]
use super::{Request as Subject, *};

pub(super) const PREFIX: &[u8] = b"alysis-paired-fixed-initializer-v1:";
const MAX_REQUEST: usize = 3072;

/// Both original jobs and every resource lifetime must belong to one live pair.
/// The coordinator must compare these with its original guards before dispatch;
/// successful parsing alone cannot establish those relationships.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Resources {
    pub owner_job: String,
    pub consumer_job: String,
    pub namespace: String,
    pub host_profile: String,
    pub host_profile_use: String,
    pub station: String,
    pub desktop: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Request {
    pub subject: Subject,
    pub resources: Resources,
}

impl Request {
    pub(crate) fn validate(&self) -> Result<()> {
        self.subject.validate()?;
        let r = &self.resources;
        let nonces = [
            self.subject.operation.as_str(),
            self.subject.nonce.as_str(),
            self.subject
                .container
                .strip_prefix("Alysis.Execution.")
                .context("paired initializer execution profile differs")?,
            &r.owner_job,
            &r.consumer_job,
            &r.namespace,
            &r.host_profile,
            &r.host_profile_use,
            &r.station,
            &r.desktop,
        ];
        for (index, nonce) in nonces.iter().enumerate() {
            crate::account_install::validate_random_id(nonce)?;
            ensure!(
                !nonces[..index].contains(nonce),
                "paired initializer nonce aliases another lifetime"
            );
        }
        Ok(())
    }

    pub(crate) fn encode(&self) -> Result<Vec<u8>> {
        self.validate()?;
        let mut bytes = PREFIX.to_vec();
        bytes.extend(serde_json::to_vec(self)?);
        ensure!(
            bytes.len() <= MAX_REQUEST,
            "paired initializer request exceeds bounds"
        );
        Ok(bytes)
    }

    pub(crate) fn decode(bytes: &[u8]) -> Result<Self> {
        ensure!(
            bytes.len() <= MAX_REQUEST && bytes.starts_with(PREFIX),
            "paired initializer request format differs"
        );
        let request: Self = serde_json::from_slice(&bytes[PREFIX.len()..])
            .context("invalid paired initializer request")?;
        request.validate()?;
        Ok(request)
    }

    /// The only desktop admitted by this domain. Neither a serialized desktop
    /// pathname nor arbitrary application/arguments/environment are accepted.
    pub(crate) fn desktop_name(&self) -> Result<String> {
        self.validate()?;
        Ok(format!(
            "WinSta0\\alysis-paired-desktop-{}",
            self.subject.operation
        ))
    }

    pub(crate) fn frame(&self, phase: &str, pid: u32, created: u64) -> Result<Vec<u8>> {
        ensure!(
            matches!(phase, "stop" | "stopped")
                && pid != 0
                && pid != self.subject.bootstrap_pid
                && created >= self.subject.bootstrap_created,
            "paired initializer phase identity invalid"
        );
        let mut bytes = self.encode()?;
        bytes.extend_from_slice(phase.as_bytes());
        bytes.extend_from_slice(&pid.to_le_bytes());
        bytes.extend_from_slice(&created.to_le_bytes());
        Ok(bytes)
    }
}

/// A different request type and offer tag keep both response domains closed.
/// The single-job v2 Offer encoding in the parent module remains unchanged.
#[derive(Serialize, Deserialize)]
#[serde(tag = "kind", deny_unknown_fields)]
enum Offer {
    #[serde(rename = "paired_born")]
    Born {
        request: Request,
        source_handle: u64,
        pid: u32,
        created: u64,
    },
    #[serde(rename = "paired_failed")]
    Failed {
        request: Request,
        hresult: Option<i32>,
    },
}

impl InitializerRequest for Request {
    fn subject(&self) -> &Subject {
        &self.subject
    }
    fn validate(&self) -> Result<()> {
        Request::validate(self)
    }
    fn desktop_name(&self) -> Result<String> {
        Request::desktop_name(self)
    }
    fn frame(&self, phase: &str, pid: u32, created: u64) -> Result<Vec<u8>> {
        Request::frame(self, phase, pid, created)
    }
    fn born_offer(&self, source_handle: u64, pid: u32, created: u64) -> Result<Vec<u8>> {
        Ok(serde_json::to_vec(&Offer::Born {
            request: self.clone(),
            source_handle,
            pid,
            created,
        })?)
    }
    fn failed_offer(&self, hresult: Option<i32>) -> Result<Vec<u8>> {
        Ok(serde_json::to_vec(&Offer::Failed {
            request: self.clone(),
            hresult,
        })?)
    }
    fn parse_offer(&self, bytes: &[u8]) -> Result<OfferedProcess> {
        self.validate()?;
        ensure!(
            bytes.len() <= 4096,
            "paired initializer offer exceeds bounds"
        );
        let offer: Offer =
            serde_json::from_slice(bytes).context("invalid paired initializer offer")?;
        match offer {
            Offer::Born {
                request,
                source_handle,
                pid,
                created,
            } => {
                ensure!(&request == self, "paired initializer offer binding differs");
                Ok(OfferedProcess {
                    source_handle,
                    pid,
                    created,
                })
            }
            Offer::Failed { request, hresult } => {
                ensure!(
                    &request == self,
                    "paired initializer failure binding differs"
                );
                source_failure(hresult)
            }
        }
    }
}

/// Query/synchronize observation pulled from the exact original bootstrap.
/// Original job membership is checked before return; the coordinator still
/// owes exclusive consumer membership, same-pair identity and retained guards.
pub(crate) fn receive_process(
    bootstrap: &super::super::super::SuspendedBootstrap<'_>,
    channel: &mut ControlChannel<'_>,
    request: &Request,
) -> Result<OwnedHandle> {
    super::receive_process_inner(bootstrap, channel, request)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request() -> Request {
        let id = |n: u8| format!("{n:032x}");
        let container = format!("Alysis.Execution.{}", id(3));
        Request {
            subject: Subject {
                operation: id(1),
                nonce: id(2),
                bootstrap_pid: 10,
                bootstrap_created: 100,
                package_sid: crate::appcontainer::derive(&container)
                    .unwrap()
                    .string()
                    .unwrap(),
                container,
                owner: "S-1-5-21-1-2-3-1001".into(),
                session: 1,
                image_id: [1; 24],
                omit_profile_environment: false,
            },
            resources: Resources {
                owner_job: id(4),
                consumer_job: id(5),
                namespace: id(6),
                host_profile: id(7),
                host_profile_use: id(8),
                station: id(9),
                desktop: id(10),
            },
        }
    }

    fn wire(value: &serde_json::Value) -> Vec<u8> {
        let mut bytes = PREFIX.to_vec();
        bytes.extend(serde_json::to_vec(value).unwrap());
        bytes
    }

    #[test]
    fn paired_transport_preserves_legacy_request_and_offer_bytes() {
        let subject = request().subject;
        let json = serde_json::to_string(&subject).unwrap();
        assert_eq!(
            subject.encode().unwrap(),
            format!("alysis-fixed-initializer-v2:{json}").as_bytes()
        );
        let born = subject.born_offer(123, 11, 101).unwrap();
        assert_eq!(
            born,
            format!(
                "{{\"kind\":\"born\",\"request\":{json},\"source_handle\":123,\"pid\":11,\"created\":101}}"
            ).as_bytes()
        );
        let parsed = subject.parse_offer(&born).unwrap();
        assert_eq!(
            (parsed.source_handle, parsed.pid, parsed.created),
            (123, 11, 101)
        );
        for (hresult, encoded) in [(None, "null"), (Some(-1), "-1")] {
            assert_eq!(
                subject.failed_offer(hresult).unwrap(),
                format!("{{\"kind\":\"failed\",\"request\":{json},\"hresult\":{encoded}}}")
                    .as_bytes()
            );
        }
        let mut stop = subject.encode().unwrap();
        stop.extend_from_slice(b"stop");
        stop.extend_from_slice(&11u32.to_le_bytes());
        stop.extend_from_slice(&101u64.to_le_bytes());
        assert_eq!(subject.frame("stop", 11, 101).unwrap(), stop);
    }

    #[test]
    fn paired_initializer_offers_cannot_cross_domains_or_resource_bindings() {
        let good = request();
        let paired = good.born_offer(123, 11, 101).unwrap();
        let legacy = good.subject.born_offer(123, 11, 101).unwrap();
        let parsed = good.parse_offer(&paired).unwrap();
        assert_eq!(
            (parsed.source_handle, parsed.pid, parsed.created),
            (123, 11, 101)
        );
        assert!(good.parse_offer(&legacy).is_err());
        assert!(good.subject.parse_offer(&paired).is_err());
        for (bytes, tag) in [(&paired, "born"), (&legacy, "paired_born")] {
            let mut changed: serde_json::Value = serde_json::from_slice(bytes).unwrap();
            changed["kind"] = serde_json::json!(tag);
            let changed = serde_json::to_vec(&changed).unwrap();
            assert!(good.parse_offer(&changed).is_err());
            assert!(good.subject.parse_offer(&changed).is_err());
        }
        for field in [
            "owner_job",
            "consumer_job",
            "namespace",
            "host_profile",
            "host_profile_use",
            "station",
            "desktop",
        ] {
            let mut value = serde_json::to_value(&good).unwrap();
            value["resources"][field] = serde_json::json!(format!("{:032x}", 20));
            let changed = Request::decode(&wire(&value)).unwrap();
            assert!(
                good.parse_offer(&changed.born_offer(123, 11, 101).unwrap())
                    .is_err()
            );
            assert!(
                good.parse_offer(&changed.failed_offer(Some(-1)).unwrap())
                    .is_err()
            );
        }
        // Once one initializer has completed, either request domain remains
        // an invalid credential selection; the source dispatcher never loops.
        for repeated in [good.encode().unwrap(), good.subject.encode().unwrap()] {
            assert!(
                crate::account_control::bootstrap::entry::credential_wire::selection_frame(
                    &repeated
                )
                .is_err()
            );
        }
    }

    #[test]
    fn paired_initializer_rejects_malformed_offers_and_invalid_failures() {
        let good = request();
        let born = good.born_offer(123, 11, 101).unwrap();
        let value: serde_json::Value = serde_json::from_slice(&born).unwrap();
        for field in ["kind", "request", "source_handle", "pid", "created"] {
            let mut missing = value.clone();
            missing.as_object_mut().unwrap().remove(field);
            assert!(
                good.parse_offer(&serde_json::to_vec(&missing).unwrap())
                    .is_err()
            );
            let json = serde_json::to_string(&value).unwrap();
            let fragment = format!("\"{field}\":{}", value[field]);
            let duplicate = json.replacen(&fragment, &format!("{fragment},{fragment}"), 1);
            assert_ne!(json, duplicate);
            assert!(good.parse_offer(duplicate.as_bytes()).is_err());
        }
        for field in [
            "desktop",
            "argv",
            "environment",
            "role",
            "resume",
            "handle_rights",
        ] {
            let mut unknown = value.clone();
            unknown[field] = serde_json::json!("arbitrary");
            assert!(
                good.parse_offer(&serde_json::to_vec(&unknown).unwrap())
                    .is_err()
            );
        }
        let mut trailing = born.clone();
        trailing.extend_from_slice(b"{}");
        assert!(good.parse_offer(&trailing).is_err());
        let mut oversized = born;
        oversized.resize(4097, b' ');
        assert!(good.parse_offer(&oversized).is_err());
        for hresult in [None, Some(-1), Some(0), Some(1)] {
            let bytes = good.failed_offer(hresult).unwrap();
            let error = good
                .parse_offer(&bytes)
                .err()
                .expect("failure offer returned a process");
            let native = error
                .chain()
                .find_map(|e| e.downcast_ref::<windows::core::Error>())
                .map(|e| e.code().0);
            assert_eq!(native, hresult.filter(|value| *value < 0));
            assert!(good.subject.parse_offer(&bytes).is_err());
            assert!(
                good.parse_offer(&good.subject.failed_offer(hresult).unwrap())
                    .is_err()
            );
        }
    }

    #[test]
    fn paired_initializer_rejects_cross_domain_and_arbitrary_launch_fields() {
        let good = request();
        let bytes = good.encode().unwrap();
        assert_eq!(Request::decode(&bytes).unwrap(), good);
        assert!(Subject::decode(&bytes).is_err());
        assert!(
            crate::account_control::bootstrap::entry::credential_wire::selection_frame(&bytes)
                .is_err()
        );
        assert!(Request::decode(&good.subject.encode().unwrap()).is_err());
        let expected = format!("WinSta0\\alysis-paired-desktop-{}", good.subject.operation);
        assert_eq!(good.desktop_name().unwrap(), expected);
        // A copied single-job payload with a paired prefix cannot lose the
        // required paired bindings; the inverse projection is also refused.
        assert!(Request::decode(&wire(&serde_json::to_value(&good.subject).unwrap())).is_err());
        let mut legacy = super::super::PREFIX.to_vec();
        legacy.extend(serde_json::to_vec(&good).unwrap());
        assert!(Subject::decode(&legacy).is_err());
        for field in ["desktop", "argv", "image", "environment", "role", "resume"] {
            for layer in ["top", "subject", "resources"] {
                let mut value = serde_json::to_value(&good).unwrap();
                if layer == "top" {
                    value[field] = serde_json::json!("arbitrary");
                } else {
                    value[layer][field] = serde_json::json!("arbitrary");
                }
                assert!(Request::decode(&wire(&value)).is_err(), "{layer}/{field}");
            }
        }
        let mut oversized = bytes.clone();
        oversized.resize(MAX_REQUEST + 1, b' ');
        assert!(Request::decode(&oversized).is_err());
        let mut trailing = bytes;
        trailing.extend_from_slice(b"{}");
        assert!(Request::decode(&trailing).is_err());
    }

    #[test]
    fn paired_initializer_rejects_missing_duplicate_and_aliased_resource_bindings() {
        let good = request();
        let value = serde_json::to_value(&good).unwrap();
        let resource_fields = [
            "owner_job",
            "consumer_job",
            "namespace",
            "host_profile",
            "host_profile_use",
            "station",
            "desktop",
        ];
        let mut nonces = vec![
            good.subject.operation.clone(),
            good.subject.nonce.clone(),
            good.subject
                .container
                .strip_prefix("Alysis.Execution.")
                .unwrap()
                .to_owned(),
        ];
        nonces.extend(
            resource_fields
                .iter()
                .map(|f| value["resources"][f].as_str().unwrap().to_owned()),
        );
        for field in resource_fields {
            let mut missing = value.clone();
            missing["resources"].as_object_mut().unwrap().remove(field);
            assert!(Request::decode(&wire(&missing)).is_err());
            for alias in &nonces {
                if value["resources"][field].as_str() == Some(alias) {
                    continue;
                }
                let mut bad = value.clone();
                bad["resources"][field] = serde_json::json!(alias);
                assert!(Request::decode(&wire(&bad)).is_err(), "aliased {field}");
            }
            for invalid in ["", "../relative", "000000000000000000000000000000000x"] {
                let mut bad = value.clone();
                bad["resources"][field] = serde_json::json!(invalid);
                assert!(Request::decode(&wire(&bad)).is_err());
            }
            let json = serde_json::to_string(&value).unwrap();
            let fragment = format!("\"{field}\":{}", value["resources"][field]);
            let duplicate = json.replacen(&fragment, &format!("{fragment},{fragment}"), 1);
            assert_ne!(json, duplicate);
            let mut bytes = PREFIX.to_vec();
            bytes.extend_from_slice(duplicate.as_bytes());
            assert!(Request::decode(&bytes).is_err(), "duplicated {field}");
        }
    }

    #[test]
    fn paired_initializer_stop_frames_bind_original_child_and_every_resource() {
        let good = request();
        let expected = good.frame("stop", 11, 101).unwrap();
        assert_ne!(expected, good.frame("stopped", 11, 101).unwrap());
        assert_ne!(expected, good.subject.frame("stop", 11, 101).unwrap());
        for (phase, pid, created) in [
            ("resume", 11, 101),
            ("stop", 0, 101),
            ("stop", 10, 101),
            ("stop", 11, 99),
        ] {
            assert!(good.frame(phase, pid, created).is_err());
        }
        assert_ne!(expected, good.frame("stop", 12, 101).unwrap());
        assert_ne!(expected, good.frame("stop", 11, 102).unwrap());
        for field in [
            "owner_job",
            "consumer_job",
            "namespace",
            "host_profile",
            "host_profile_use",
            "station",
            "desktop",
        ] {
            let mut value = serde_json::to_value(&good).unwrap();
            value["resources"][field] = serde_json::json!(format!("{:032x}", 20));
            let changed = Request::decode(&wire(&value)).unwrap();
            assert_ne!(expected, changed.frame("stop", 11, 101).unwrap(), "{field}");
        }
    }
}
