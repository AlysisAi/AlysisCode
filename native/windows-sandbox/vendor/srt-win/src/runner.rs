//! `srt-win runner` — the inside-the-logon half of the two-hop
//! launch.
//!
//! The broker (running as the **real** user) decrypts the sandbox
//! user's password and `CreateProcessWithLogonW`s **this**
//! subcommand under the `alysis-sandbox` account. The runner reads a
//! [`RunnerCmd`] from stdin and either runs
//! [`crate::launch::run_lockdown`] (restricted token, job, desktop,
//! mitigations, handle whitelist), or — at install time — writes
//! the MITM CA into the sandbox user's `CurrentUser\Root` (see
//! [`crate::cert_store`]). The child inherits the runner's stdio,
//! which are the broker's pipes, so stdout/stderr flow broker ←
//! runner ← child without an extra pump.
//!
//! All state-DB work happens in the **broker**, never here: the
//! state-DB directory carries an explicit DENY for
//! `alysis-sandbox-users`, so the runner cannot open it.

use anyhow::{Context, Result, anyhow};
use serde::{Deserialize, Serialize};
use std::io::Read;

use crate::launch;

/// What the broker asks the runner to do. Passed over stdin (4-byte
/// LE length prefix + JSON). Stdin — not argv or env — because the
/// env overlay can exceed what `lpCommandLine` reliably carries, and
/// a temp file would need a path the sandbox user can read (the
/// broker's `%TEMP%` may not be).
#[derive(Debug, Serialize, Deserialize)]
pub enum RunnerCmd {
    /// Candidate only: arbitrary commands always run inside a dedicated-account
    /// AppContainer. Its credential must never be supplied to legacy Exec.
    #[cfg(feature = "account-appcontainer-candidate")]
    AccountAppContainer(crate::account_launch::AccountContainerSpec),
    /// Candidate crash recovery after the named execution job is empty.
    #[cfg(feature = "account-appcontainer-candidate")]
    CleanupAccountAppContainer(crate::account_launch::AccountProfileCleanup),
    /// Per-exec: run the target under [`crate::launch::run_lockdown`].
    Exec(RunnerSpec),
    /// Install-time, one-shot: write the DER-encoded CA into the
    /// **sandbox user's** `CurrentUser\Root` (direct
    /// `HKEY_USERS\<own-SID>\…\Root\Certificates\<thumb>` registry
    /// write — see [`crate::cert_store`]). Persistent until
    /// `srt-win uninstall` deletes the profile. Exit non-zero on
    /// failure.
    InstallCa { der: crate::cert_store::CertDer },
    /// `wfp verify` probe: attempt a direct TCP connect to `target`
    /// (`host:port`) **as the sandbox user**. The WFP block-user
    /// filter fires at `ALE_AUTH_CONNECT` — before any packet
    /// leaves — so an active fence yields WSAEACCES immediately;
    /// a missing fence lets the connect through. Exit **0** =
    /// WSAEACCES (fence active), **3** = connected (fence
    /// MISSING), **2** = any other error (timeout/unreachable —
    /// the broker treats it as failure). Exit 1 is reserved for
    /// the runner's own anyhow `Err` path so a malformed target
    /// or future runner bug isn't misread as `connected`. No
    /// desk/grants/lockdown — the probe runs as the bare runner
    /// (same as [`InstallCa`](Self::InstallCa)); the WFP filter
    /// keys on the user SID, which the runner already carries.
    ProbeEgress { target: String },
    /// Verify ambient directory write permissions with the sandbox identity.
    /// Opening an existing directory for each individual right changes no data.
    ProbeFilesystem {
        paths: Vec<String>,
        #[serde(default)]
        delete_paths: Vec<String>,
    },
}

/// Inputs to a single [`RunnerCmd::Exec`].
#[derive(Debug, Serialize, Deserialize)]
pub struct RunnerSpec {
    /// Trusted host identity retains access to files created by the command.
    #[serde(default)]
    pub owner_sid: Option<String>,
    /// `argv[0]` = target executable; `argv[1..]` = its arguments.
    pub argv: Vec<String>,
    /// `(KEY, VALUE)` pairs overlaid on the runner's own environment
    /// (= the sandbox user's `LOGON_WITH_PROFILE` defaults) when
    /// building the child's env block. Overlay wins on key conflict
    /// (case-insensitive), so the broker's `PATH` replaces the
    /// sandbox user's machine-only `PATH` while `USERPROFILE` /
    /// `TEMP` stay isolated. The proxy var set rides here too.
    pub env_overlay: Vec<(String, String)>,
}

/// Read a 4-byte little-endian length prefix followed by that many
/// bytes of JSON from stdin. The length prefix lets the runner know
/// when the spec ends without the broker closing the write end
/// (which it does anyway — the prefix is just robustness against a
/// future stdin-after-spec use).
fn read_cmd_from_stdin() -> Result<RunnerCmd> {
    let mut stdin = std::io::stdin().lock();
    let mut len_buf = [0u8; 4];
    stdin
        .read_exact(&mut len_buf)
        .context("runner: read spec length prefix from stdin")?;
    let len = u32::from_le_bytes(len_buf) as usize;
    // Sanity cap — the spec is a few KB; anything in the MB range
    // means the broker/runner are out of sync.
    if len > 4 * 1024 * 1024 {
        return Err(anyhow!(
            "runner: spec length {len} exceeds 4 MiB sanity cap"
        ));
    }
    let mut buf = vec![0u8; len];
    stdin
        .read_exact(&mut buf)
        .context("runner: read spec body from stdin")?;
    serde_json::from_slice(&buf).context("runner: parse spec JSON")
}

/// Serialize `cmd` as `<u32 LE length><JSON>`. Broker-side helper —
/// lives here so the wire format has one definition.
pub fn encode_cmd(cmd: &RunnerCmd) -> Result<Vec<u8>> {
    let json = serde_json::to_vec(cmd).context("runner: encode cmd")?;
    let mut out = Vec::with_capacity(4 + json.len());
    out.extend_from_slice(&(json.len() as u32).to_le_bytes());
    out.extend_from_slice(&json);
    Ok(out)
}

/// Entry point for `srt-win runner`. Reads the command from stdin,
/// dispatches, and returns the exit code.
pub fn run() -> Result<u32> {
    dispatch_cmd(read_cmd_from_stdin()?)
}

fn dispatch_cmd(command: RunnerCmd) -> Result<u32> {
    match command {
        #[cfg(feature = "account-appcontainer-candidate")]
        RunnerCmd::AccountAppContainer(spec) => crate::account_launch::run(&spec),
        #[cfg(feature = "account-appcontainer-candidate")]
        RunnerCmd::CleanupAccountAppContainer(spec) => {
            crate::account_launch::cleanup_profile(&spec)?;
            Ok(0)
        }
        RunnerCmd::ProbeFilesystem {
            paths,
            delete_paths,
        } => {
            probe_filesystem(&paths)?;
            probe_delete_protection(&delete_paths)?;
            Ok(0)
        }
        RunnerCmd::Exec(spec) => {
            if spec.argv.is_empty() {
                return Err(anyhow!("runner: spec.argv is empty"));
            }
            if std::env::var_os("SANDBOX_RUNTIME_WIN_DEBUG").is_some() {
                eprintln!(
                    "srt-win: runner: spec read (argv={} env_overlay={})",
                    spec.argv.len(),
                    spec.env_overlay.len(),
                );
            }
            let exe = std::path::PathBuf::from(&spec.argv[0]);
            launch::run_lockdown(
                &exe,
                &spec.argv[1..],
                &spec.env_overlay,
                spec.owner_sid.as_deref(),
            )
        }
        RunnerCmd::InstallCa { der } => {
            let thumb = crate::cert_store::install_root_ca(&der)?;
            eprintln!(
                "srt-win: runner: CA installed into sandbox-user \
                 CurrentUser\\Root (thumb={thumb})"
            );
            Ok(0)
        }
        RunnerCmd::ProbeEgress { target } => {
            use std::net::{SocketAddr, TcpStream};
            use std::time::Duration;
            // WSAEACCES — what WFP returns from ALE_AUTH_CONNECT
            // when the block-user filter denies the connect. Match
            // on the raw code (not `ErrorKind::PermissionDenied`)
            // so `ERROR_ACCESS_DENIED` (5) — which would mean
            // something OTHER than the WFP fence — falls through
            // to `unreachable`.
            const WSAEACCES: i32 = 10013;
            let addr: SocketAddr = target
                .parse()
                .with_context(|| format!("runner: ProbeEgress target '{target}'"))?;
            // stderr (not stdout): `spawn_runner` pumps the
            // runner's stdout straight to the broker's stdout, and
            // the broker writes its own JSON there. The exit code
            // is the contract; the line is diagnostic.
            match TcpStream::connect_timeout(&addr, Duration::from_secs(2)) {
                Err(e) if e.raw_os_error() == Some(WSAEACCES) => {
                    eprintln!(
                        "srt-win: runner: egress probe {target}: \
                         BLOCKED ({e})"
                    );
                    Ok(0)
                }
                Ok(_) => {
                    eprintln!(
                        "srt-win: runner: egress probe {target}: \
                         CONNECTED — WFP block-user filter is NOT in \
                         effect"
                    );
                    Ok(3)
                }
                Err(e) => {
                    eprintln!(
                        "srt-win: runner: egress probe {target}: \
                         UNREACHABLE: {e} (kind={:?}, os={:?})",
                        e.kind(),
                        e.raw_os_error(),
                    );
                    Ok(2)
                }
            }
        }
    }
}

pub fn probe_filesystem(paths: &[String]) -> Result<()> {
    use windows::Win32::Storage::FileSystem::{
        DELETE, FILE_ADD_FILE, FILE_ADD_SUBDIRECTORY, FILE_DELETE_CHILD, FILE_WRITE_ATTRIBUTES,
        FILE_WRITE_EA, WRITE_DAC, WRITE_OWNER,
    };
    anyhow::ensure!(!paths.is_empty(), "empty filesystem enforcement probe");
    probe_denied_access(
        paths,
        &[
            FILE_ADD_FILE,
            FILE_ADD_SUBDIRECTORY,
            FILE_DELETE_CHILD,
            FILE_WRITE_ATTRIBUTES,
            FILE_WRITE_EA,
            DELETE,
            WRITE_DAC,
            WRITE_OWNER,
        ],
    )
}

/// Protected ancestors still allow ordinary content changes; their identities
/// and ACLs must be immutable or a child can move the denied subtree aside.
pub fn probe_delete_protection(paths: &[String]) -> Result<()> {
    use windows::Win32::Storage::FileSystem::{DELETE, FILE_DELETE_CHILD, WRITE_DAC, WRITE_OWNER};
    probe_denied_access(paths, &[DELETE, FILE_DELETE_CHILD, WRITE_DAC, WRITE_OWNER])
}

/// Preserve admitted local filesystem names when calling Win32 directly. Rust's
/// directory enumeration supports long paths, but its returned child names need
/// not carry the extended prefix. This is only an encoding step: never resolve
/// aliases or reparse points here, especially while impersonating the child.
fn filesystem_probe_path(path: &str) -> Result<Vec<u16>> {
    const INVALID: &str = "filesystem probe requires an unambiguous absolute local path";
    let local = path.strip_prefix(r"\\?\").unwrap_or(path);
    let bytes = local.as_bytes();
    anyhow::ensure!(
        bytes.len() >= 3 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':' && bytes[2] == b'\\',
        INVALID
    );
    let tail = &local[3..];
    anyhow::ensure!(
        !tail
            .chars()
            .any(|ch| ch <= '\u{1f}' || "/:*?\"<>|".contains(ch)),
        INVALID
    );
    // A single final separator is valid for a directory. Empty interior
    // components, dot navigation, streams, and DOS devices must not acquire
    // different semantics merely because an extended prefix was added.
    if !tail.is_empty() {
        for component in tail.strip_suffix('\\').unwrap_or(tail).split('\\') {
            anyhow::ensure!(
                !component.is_empty() && !component.ends_with(['.', ' ']),
                INVALID
            );
            let stem = component
                .split('.')
                .next()
                .unwrap()
                .trim_end()
                .to_ascii_uppercase();
            let device = matches!(
                stem.as_str(),
                "CON" | "PRN" | "AUX" | "NUL" | "CONIN$" | "CONOUT$"
            ) || ["COM", "LPT"].iter().any(|prefix| {
                stem.strip_prefix(prefix).is_some_and(|number| {
                    matches!(
                        number,
                        "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9" | "¹" | "²" | "³"
                    )
                })
            });
            anyhow::ensure!(!device, INVALID);
        }
    }
    let extended = format!(r"\\?\{local}");
    anyhow::ensure!(extended.encode_utf16().count() < 32767, INVALID);
    Ok(crate::util::wstr(&extended))
}

fn probe_denied_access(
    paths: &[String],
    rights: &[windows::Win32::Storage::FileSystem::FILE_ACCESS_RIGHTS],
) -> Result<()> {
    use windows::Win32::Foundation::ERROR_ACCESS_DENIED;
    use windows::Win32::Storage::FileSystem::{
        CreateFileW, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT, FILE_SHARE_DELETE,
        FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING,
    };
    use windows::core::PCWSTR;
    for path in paths {
        let wide = filesystem_probe_path(path)?;
        // A combined mask could report denial even when one dangerous right is
        // allowed. Check each independently, and accept ONLY access denied.
        for access in rights {
            match unsafe {
                CreateFileW(
                    PCWSTR(wide.as_ptr()),
                    access.0,
                    FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                    None,
                    OPEN_EXISTING,
                    FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                    None,
                )
            } {
                Err(error) if error.code() == ERROR_ACCESS_DENIED.to_hresult() => (),
                Err(error) => {
                    return Err(anyhow!(
                        "filesystem probe '{path}' ({:#x}): {error}",
                        access.0
                    ));
                }
                Ok(handle) => {
                    let _handle = crate::util::OwnedHandle(handle);
                    return Err(anyhow!(
                        "filesystem protection missing: '{path}' allows {:#x}",
                        access.0
                    ));
                }
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn filesystem_probe_encoding_preserves_local_names_and_rejects_aliases() {
        for local in [
            r"C:\",
            r"C:\ordinary\file.pyc",
            r"C:\ordinary\",
            r"c:\sp ace\日本語",
        ] {
            let expected = crate::util::wstr(&format!(r"\\?\{local}"));
            assert_eq!(filesystem_probe_path(local).unwrap(), expected);
            assert_eq!(
                filesystem_probe_path(&format!(r"\\?\{local}")).unwrap(),
                expected
            );
        }
        for invalid in [
            "relative",
            r"C:relative",
            r"\rooted",
            r"C:/forward/slashes",
            r"\\server\share\file",
            r"\\?\UNC\server\share\file",
            r"\\.\C:\file",
            r"\\?\GLOBALROOT\Device\HarddiskVolume1\file",
            r"C:\a\..\b",
            r"C:\a\.\b",
            r"C:\a\\b",
            r"C:\\",
            r"C:\a.\b",
            r"C:\a \b",
            r"C:\file:stream",
            r"C:\NUL",
            r"C:\nul.txt",
            r"C:\COM1",
            r"C:\LPT².txt",
            r"C:\CONIN$",
            "C:\\a\0b",
            r"C:\wild*",
            r"C:\wild?",
        ] {
            assert!(filesystem_probe_path(invalid).is_err(), "{invalid:?}");
            assert!(
                filesystem_probe_path(&format!(r"\\?\{invalid}")).is_err(),
                "{invalid:?}"
            );
        }
    }

    #[test]
    fn long_filesystem_probes_require_each_right_denied_and_refuse_missing_paths() -> Result<()> {
        use crate::util::OwnedHandle;
        use windows::Win32::Security::{ImpersonateLoggedOnUser, RevertToSelf};
        use windows::Win32::Storage::FileSystem::{
            DELETE, FILE_ADD_FILE, FILE_ADD_SUBDIRECTORY, FILE_DELETE_CHILD, FILE_WRITE_ATTRIBUTES,
            FILE_WRITE_EA, WRITE_DAC, WRITE_OWNER,
        };
        const RESTRICTING_SID: &str = "S-1-5-21-891-892-893-894";
        struct Revert;
        impl Drop for Revert {
            fn drop(&mut self) {
                if unsafe { RevertToSelf() }.is_err() {
                    std::process::abort();
                }
            }
        }
        let under_token = |token, operation: &dyn Fn() -> Result<()>| -> Result<()> {
            unsafe { ImpersonateLoggedOnUser(token)? };
            let _revert = Revert;
            operation()
        };
        struct Cleanup(std::path::PathBuf);
        impl Drop for Cleanup {
            fn drop(&mut self) {
                let _ = std::fs::remove_dir_all(&self.0);
            }
        }
        struct RestoreAcl {
            handle: OwnedHandle,
            sddl: String,
        }
        impl Drop for RestoreAcl {
            fn drop(&mut self) {
                let _ = crate::acl::set_handle_dacl_from_sddl(
                    &self.handle,
                    &self.sddl,
                    "restore synthetic probe fixture",
                );
            }
        }
        let unique = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_nanos();
        let base = crate::util::test_temp_dir()
            .join(format!("alysis-long-probe-{}-{unique}", std::process::id()));
        let _cleanup = Cleanup(base.clone());
        let directory = base.join("a".repeat(110)).join("b".repeat(110));
        std::fs::create_dir_all(&directory)?;
        let file = directory.join("protected.cpython-312.pyc");
        std::fs::write(&file, b"unchanged containment canary")?;
        let owner = crate::sid::current_user_sid()?;
        let original = OwnedHandle(crate::token::open_self_token()?);
        let restricted = OwnedHandle(crate::token::make_write_restricted_token(
            original.raw(),
            RESTRICTING_SID,
        )?);
        let rights = [
            FILE_ADD_FILE,
            FILE_ADD_SUBDIRECTORY,
            FILE_DELETE_CHILD,
            FILE_WRITE_ATTRIBUTES,
            FILE_WRITE_EA,
            DELETE,
            WRITE_DAC,
            WRITE_OWNER,
        ];
        let all = rights.iter().fold(0, |mask, right| mask | right.0);
        let delete = DELETE.0 | FILE_DELETE_CHILD.0 | WRITE_DAC.0 | WRITE_OWNER.0;
        for path in [&file, &directory] {
            let raw = crate::path_id::strip_extended_prefix(path.to_str().unwrap()).to_owned();
            assert!(
                raw.encode_utf16().count() > 260,
                "fixture must exceed MAX_PATH"
            );
            let extended = format!(r"\\?\{raw}");
            // DELETE on an object can also be granted by FILE_DELETE_CHILD
            // on its parent. Model the production parent guard as well.
            let parent = std::fs::canonicalize(path.parent().unwrap())?;
            let parent_restore = RestoreAcl {
                handle: crate::acl::open_for_security_no_follow(parent.to_str().unwrap())?,
                sddl: format!("D:P(A;;FA;;;{owner})"),
            };
            crate::acl::set_handle_dacl_from_sddl(
                &parent_restore.handle,
                &format!(
                    "D:P(D;;0x40;;;{owner})(D;;0x40;;;{RESTRICTING_SID})(A;OICI;FA;;;{owner})(A;;FA;;;WD)(A;;RC;;;OW)"
                ),
                "synthetic probe parent delete guard",
            )?;
            // Keep a host handle before denying any rights. It restores only
            // this fresh fixture even when the current user's WRITE_DAC is
            // denied; cleanup never needs to take ownership or elevate.
            let restore = RestoreAcl {
                handle: crate::acl::open_for_security_no_follow(&extended)?,
                sddl: format!("D:P(A;;FA;;;{owner})"),
            };
            let stamp = |denied| {
                crate::acl::set_handle_dacl_from_sddl(
                    &restore.handle,
                    &format!(
                        "D:P(D;;0x{denied:x};;;{owner})(D;;0x{denied:x};;;{RESTRICTING_SID})(A;;FA;;;{owner})(A;;FA;;;WD)(A;;RC;;;OW)"
                    ),
                    "synthetic long-path probe fixture",
                )
            };
            stamp(all)?;
            for spelling in [&raw, &extended] {
                let paths = [spelling.clone()];
                under_token(restricted.raw(), &|| probe_filesystem(&paths))?;
                under_token(restricted.raw(), &|| probe_delete_protection(&paths))?;
            }
            // Grant exactly one dangerous right at a time. A combined-mask
            // implementation would incorrectly accept these partial denies.
            for right in rights {
                stamp(all & !right.0)?;
                let paths = [raw.clone()];
                let error =
                    under_token(restricted.raw(), &|| probe_filesystem(&paths)).unwrap_err();
                assert!(
                    error
                        .to_string()
                        .contains(&format!("allows {:#x}", right.0)),
                    "{error:#}"
                );
                if right.0 & delete != 0 {
                    let error = under_token(restricted.raw(), &|| probe_delete_protection(&paths))
                        .unwrap_err();
                    assert!(
                        error
                            .to_string()
                            .contains(&format!("allows {:#x}", right.0)),
                        "{error:#}"
                    );
                }
            }
            drop(restore);
            drop(parent_restore);
        }
        let missing = file
            .with_file_name("missing.pyc")
            .to_string_lossy()
            .into_owned();
        let error = under_token(restricted.raw(), &|| {
            probe_filesystem(std::slice::from_ref(&missing))
        })
        .unwrap_err();
        assert!(
            !error.to_string().contains("protection missing"),
            "{error:#}"
        );
        assert!(error.to_string().contains("filesystem probe"), "{error:#}");
        assert_eq!(std::fs::read(&file)?, b"unchanged containment canary");
        std::fs::remove_dir_all(&base)?;
        Ok(())
    }

    #[test]
    fn cmd_roundtrip() {
        let s = RunnerCmd::Exec(RunnerSpec {
            owner_sid: None,
            argv: vec!["cmd.exe".into(), "/c".into(), "echo hi".into()],
            env_overlay: vec![("PATH".into(), r"C:\a;C:\b".into())],
        });
        let bytes = encode_cmd(&s).unwrap();
        assert_eq!(
            u32::from_le_bytes(bytes[..4].try_into().unwrap()) as usize,
            bytes.len() - 4
        );
        let back: RunnerCmd = serde_json::from_slice(&bytes[4..]).unwrap();
        match back {
            RunnerCmd::Exec(r) => {
                assert_eq!(r.argv, ["cmd.exe", "/c", "echo hi"]);
                assert_eq!(r.env_overlay.len(), 1);
            }
            _ => panic!("wrong variant"),
        }
        let ca = RunnerCmd::InstallCa {
            der: crate::cert_store::CertDer::raw(vec![0x30, 0x82]),
        };
        let bytes = encode_cmd(&ca).unwrap();
        let back: RunnerCmd = serde_json::from_slice(&bytes[4..]).unwrap();
        assert!(matches!(
            back, RunnerCmd::InstallCa { der } if der.as_bytes() == [0x30, 0x82]
        ));
        let probe = RunnerCmd::ProbeEgress {
            target: "127.0.0.1:49999".into(),
        };
        let bytes = encode_cmd(&probe).unwrap();
        let back: RunnerCmd = serde_json::from_slice(&bytes[4..]).unwrap();
        assert!(matches!(
            back, RunnerCmd::ProbeEgress { target } if target == "127.0.0.1:49999"
        ));
    }

    #[cfg(feature = "account-appcontainer-candidate")]
    #[test]
    fn account_runner_wire_requires_lifecycle_authority_before_native_effects() {
        // Invalid native arguments make the pre-fix regression harmless: the
        // old route fails validation, never reaching profile creation/deletion.
        let spec = crate::account_launch::AccountContainerSpec {
            account_sid: "invalid".into(),
            owner_sid: "invalid".into(),
            container: "not-an-execution-profile".into(),
            cwd: "relative".into(),
            argv: vec![],
            env: vec![],
            network: crate::appcontainer::NetworkAccess::Off,
            protected_paths: vec![],
            protected_ancestors: vec![],
        };
        for command in [
            RunnerCmd::AccountAppContainer(spec),
            RunnerCmd::CleanupAccountAppContainer(crate::account_launch::AccountProfileCleanup {
                account_sid: "invalid".into(),
                container: "not-an-execution-profile".into(),
            }),
        ] {
            let bytes = encode_cmd(&command).unwrap();
            let decoded = serde_json::from_slice(&bytes[4..]).unwrap();
            let error = dispatch_cmd(decoded).unwrap_err();
            assert!(
                error
                    .to_string()
                    .contains("candidate runner lifecycle authority unavailable"),
                "{error:#}"
            );
        }
    }
}
