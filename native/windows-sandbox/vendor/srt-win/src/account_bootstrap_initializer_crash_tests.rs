//! Controller-only diagnostic binding checks after native initializer use.
//! No initializer token, job, namespace or profile reference crosses the kill.
use super::*;
fn value(text: &str) -> Result<serde_json::Value> {
    Ok(serde_json::from_str(text)?)
}
pub(super) fn verify_profile_use(proof: &Proof) -> Result<()> {
    ensure!(
        proof.snapshot.host_profile.len() == 3,
        "initializer profile prefix length differs"
    );
    let intent = value(&proof.snapshot.host_profile[0])?;
    let created = value(&proof.snapshot.host_profile[1])?;
    let used = value(&proof.snapshot.host_profile[2])?;
    let nonce = used["nonce"]
        .as_str()
        .context("initializer profile use nonce missing")?;
    validate_random_id(nonce)?;
    ensure!(
        used["kind"] == "use_intent"
            && used["binding"] == intent["binding"]
            && used["folder_id"] == created["folder_id"]
            && used["nonce"] != intent["binding"]["nonce"]
            && used["nonce"] != intent["binding"]["operation"],
        "initializer profile use differs from original creation"
    );
    Ok(())
}
pub(super) fn verify_prefix(proof: &Proof) -> Result<()> {
    verify_profile_use(proof)?;
    ensure!(
        proof.snapshot.initializer.len() == 5,
        "initializer cold prefix must retain stopped keeper without release"
    );
    let intent = value(&proof.snapshot.initializer[0])?;
    let born = value(&proof.snapshot.initializer[1])?;
    let stopped = value(&proof.snapshot.initializer[2])?;
    let plan = value(&proof.snapshot.plan)?;
    let selected = value(&proof.snapshot.delivery)?;
    let namespace = value(&proof.snapshot.namespace[0])?;
    let desktop = value(&proof.snapshot.desktop[0])?;
    let station = value(&proof.snapshot.station[0])?;
    let used = value(&proof.snapshot.host_profile[2])?;
    let b = &intent["binding"];
    validate_random_id(b["nonce"].as_str().context("initializer nonce missing")?)?;
    ensure!(
        intent["kind"] == "intent"
            && born["kind"] == "born"
            && stopped["kind"] == "stopped"
            && b == &born["binding"]
            && b == &stopped["binding"]
            && born["birth"] == stopped["birth"]
            && b["operation"] == plan["operation"]
            && b["nonce"] != b["operation"]
            && b["policy"] == 4
            && b["namespace"] == namespace["binding"]["nonce"]
            && b["desktop"] == desktop["binding"]["nonce"]
            && b["station"] == station["binding"]["nonce"]
            && b["profile_use"] == used["nonce"]
            && b["bootstrap_pid"] == selected["selection"]["bootstrap_pid"]
            && b["bootstrap_created"] == selected["selection"]["bootstrap_created"],
        "initializer stopped keeper binding differs"
    );
    let rpc_intent = value(&proof.snapshot.initializer[3])?;
    let rpc_granted = value(&proof.snapshot.initializer[4])?;
    ensure!(
        rpc_intent["kind"] == "rpc_intent"
            && rpc_granted["kind"] == "rpc_granted"
            && rpc_intent["binding"] == *b
            && rpc_granted["binding"] == *b
            && rpc_intent["policy"] == rpc_granted["policy"],
        "initializer RPC grant prefix differs"
    );
    struct Subjects<'a>(&'a ExecutionRecord, u32);
    impl crate::account_state::execution::resources::namespace_native::NamespacePolicySource
        for Subjects<'_>
    {
        fn execution(&self) -> &ExecutionRecord {
            self.0
        }
        fn session(&self) -> u32 {
            self.1
        }
    }
    let subjects = Subjects(
        &proof.execution,
        plan["session"]
            .as_u64()
            .context("RPC session missing")?
            .try_into()?,
    );
    let policy: crate::account_state::execution::resources::namespace_native::rpc::AccessPolicy =
        serde_json::from_value(rpc_intent["policy"].clone())?;
    policy.validate(&subjects)?;
    let image: [u8; 24] = serde_json::from_value(b["image_id"].clone())?;
    let pid = born["birth"]["pid"]
        .as_u64()
        .context("initializer PID absent")?;
    let created = born["birth"]["created"]
        .as_u64()
        .context("initializer creation time absent")?;
    ensure!(
        image != [0; 24]
            && pid > 0
            && (pid, created) != (u64::from(proof.child_pid), proof.child_created)
            && pid != u64::from(proof.execution.pid)
            && born["birth"]["pid"] != b["bootstrap_pid"]
            && created >= proof.execution.created,
        "initializer child identity invalid"
    );
    Ok(())
}
