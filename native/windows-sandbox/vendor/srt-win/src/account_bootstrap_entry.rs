//! Trusted bootstrap receives a bounded credential only from its original
//! authenticated broker. A separately bound request can create the selected
//! suspended account child. It cannot read the credential store, execute an
//! user command or perform profile cleanup. A fixed self-check has its own
//! separately journaled, purpose-bound one-time resume protocol.
use super::*;

#[path = "account_bootstrap_credential_wire.rs"]
pub(crate) mod credential_wire;
#[path = "account_bootstrap_initializer_wire.rs"]
pub(crate) mod initializer_wire;

#[cfg(test)]
#[path = "account_paired_family_fixture.rs"]
pub(crate) mod paired_family;

const TIMEOUT: Duration = Duration::from_secs(10);
const PREFIX: &[u8] = b"alysis-bootstrap-ready-v1:";

pub(crate) fn challenge(nonce: &str) -> Result<Vec<u8>> {
    crate::account_install::validate_random_id(nonce)?;
    let mut value = Vec::with_capacity(PREFIX.len() + nonce.len());
    value.extend_from_slice(PREFIX);
    value.extend_from_slice(nonce.as_bytes());
    Ok(value)
}
fn validate_challenge(bytes: &[u8]) -> Result<()> {
    ensure!(
        bytes.len() == PREFIX.len() + 32 && bytes.starts_with(PREFIX),
        "invalid bootstrap readiness challenge"
    );
    let nonce = std::str::from_utf8(&bytes[PREFIX.len()..])
        .context("invalid bootstrap operation encoding")?;
    crate::account_install::validate_random_id(nonce)
}
fn parse_handoff(parent: &str, address: &str) -> Result<(usize, ControlAddress)> {
    ensure!(
        !parent.is_empty() && parent.len() <= 20 && parent.bytes().all(|b| b.is_ascii_digit()),
        "invalid original broker reference"
    );
    let raw: usize = parent
        .parse()
        .context("invalid original broker reference")?;
    ensure!(
        raw > 0 && raw < isize::MAX as usize,
        "invalid original broker reference"
    );
    ensure!(address.len() <= 256, "bootstrap address exceeds bound");
    let address: ControlAddress =
        serde_json::from_str(address).context("invalid bootstrap control address")?;
    address.binding_nonce()?;
    Ok((raw, address))
}
pub(crate) fn run() -> Result<()> {
    run_inner(false)
}
fn run_inner(fixture: bool) -> Result<()> {
    #[cfg(not(test))]
    ensure!(!fixture, "bootstrap fixture admission unavailable");
    crate::account_impersonation::require_process_identity()?;
    let (raw, address) = parse_handoff(
        &std::env::var("ALYSIS_BOOTSTRAP_ORIGINAL_PEER")
            .context("original broker reference missing")?,
        &std::env::var("ALYSIS_BOOTSTRAP_ADDRESS").context("bootstrap control address missing")?,
    )?;
    let raw = HANDLE(raw as *mut _);
    let mut flags = 0;
    unsafe {
        GetHandleInformation(raw, &mut flags)?;
    }
    ensure!(
        flags & HANDLE_FLAG_INHERIT.0 != 0,
        "original broker reference was not inherited"
    );
    let peer = ExpectedPeer::retain(raw)?; // Duplicate before owning any supplied integer.
    let inherited = OwnedHandle(raw);
    let mut session = 0;
    unsafe {
        windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
            std::process::id(),
            &mut session,
        )?;
    }
    peer.token
        .require_account_runner(&crate::sid::current_user_sid()?, session)?;
    drop(inherited); // Retained duplicate is noninheritable and query/synchronize only.
    let mut channel = ControlChannel::connect(&address, peer)?;
    let frame = FrameStorage(channel.receive(TIMEOUT)?);
    validate_challenge(&frame.0)?;
    let ready = std::str::from_utf8(&frame.0[PREFIX.len()..])?.to_owned();
    channel.send(&frame.0, TIMEOUT)?;
    drop(frame);
    // The parent retains every startup process before this entry may create
    // any later initializer, account child or fixed family descendant.
    ensure!(
        channel.receive(TIMEOUT)? == super::STARTUP_CAPTURED,
        "original broker did not acknowledge startup capture"
    );
    #[cfg(test)]
    if std::env::var("ALYSIS_TEST_PAIRED_FAMILY").ok().as_deref() == Some("v1") {
        ensure!(
            fixture,
            "paired family fixture requires test image admission"
        );
        return paired_family::run(&mut channel);
    }
    let selection_frame = initializer_wire::before_selection(&mut channel)?;
    let selection = credential_wire::selection_frame(&selection_frame)?;
    #[cfg(test)]
    let image = if fixture {
        PinnedRunnerImage::fixture(&std::env::current_exe()?)?
    } else {
        PinnedRunnerImage::prepare_bootstrap_selection(&selection.account)?
    };
    #[cfg(not(test))]
    let image = PinnedRunnerImage::prepare_bootstrap_selection(&selection.account)?;
    let received = credential_wire::receive(channel, selection, &ready, &image)?;
    // Discard or one separately bound suspended birth request is accepted.
    // No packet or receipt can reach a generic Exec adapter.
    received.wait_for_retirement()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    #[ignore = "fixed bounded paired child/grandchild subprocess only"]
    fn paired_family_descendant_fixture() {
        paired_family::descendant();
    }
    #[test]
    #[ignore = "bounded trusted-bootstrap subprocess invoked by journaled startup tests"]
    fn atomic_bootstrap_entry_fixture() {
        let fixture = std::env::var("ALYSIS_TEST_BOOTSTRAP_INSTALLED_IMAGE")
            .ok()
            .as_deref()
            != Some("1");
        run_inner(fixture).unwrap();
    }
    #[test]
    fn atomic_bootstrap_entry_rejects_malformed_handoffs_and_nonready_commands() {
        let address = serde_json::to_string(&ControlAddress::fresh().unwrap()).unwrap();
        parse_handoff("123", &address).unwrap();
        for bad in ["", "0", "-1", " 123", "18446744073709551615", "1x"] {
            assert!(parse_handoff(bad, &address).is_err());
        }
        for bad in ["{}", "{\"nonce\":[0]}", "[]", &"x".repeat(257)] {
            assert!(parse_handoff("123", bad).is_err());
        }
        let valid = challenge("0123456789abcdef0123456789abcdef").unwrap();
        validate_challenge(&valid).unwrap();
        for length in 0..valid.len() {
            assert!(validate_challenge(&valid[..length]).is_err());
        }
        let mut trailing = valid.clone();
        trailing.push(0);
        assert!(validate_challenge(&trailing).is_err());
        for invalid in [b"exec".as_slice(), b"logon", b"cleanup", b"credentials"] {
            assert!(validate_challenge(invalid).is_err());
        }
        let mut altered = valid;
        altered[0] ^= 1;
        assert!(validate_challenge(&altered).is_err());
    }
}
