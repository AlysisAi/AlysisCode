use super::*;

const UNUSED_ACCOUNT: &str = "S-1-5-21-1-2-3-9999";
const TIMEOUT: Duration = Duration::from_secs(1);

fn me() -> ExpectedPeer<'static> {
    ExpectedPeer::retain(unsafe { GetCurrentProcess() }).unwrap()
}
fn pair() -> (ControlChannel<'static>, ControlChannel<'static>) {
    let (server, address) = ControlServer::prepare(UNUSED_ACCOUNT).unwrap();
    let client = ControlChannel::connect(&address, me()).unwrap();
    let server = server.accept(me(), TIMEOUT).unwrap();
    (server, client)
}

#[test]
fn account_control_scrubs_transport_copies_after_success_and_cancelled_partial_io() {
    let (mut server, mut client) = pair();
    client.send(b"synthetic-test-payload", TIMEOUT).unwrap();
    assert_eq!(FRAME_SCRUB_AUDIT.get(), Some((HEADER + 22, true, true)));
    assert_eq!(server.receive(TIMEOUT).unwrap(), b"synthetic-test-payload");
    // A completed receive transfers the original body to its caller; no second
    // plaintext allocation is made or retained by transport.
    assert_eq!(FRAME_SCRUB_AUDIT.get(), Some((0, false, true)));

    let (mut server, client) = pair();
    let mut partial = raw_frame(&client, 0, 32);
    partial.extend_from_slice(b"synthetic");
    io::transfer(
        client.handle.raw(),
        &mut partial,
        true,
        deadline(TIMEOUT).unwrap(),
    )
    .unwrap();
    // The writer stays open, forcing real overlapped read cancellation/draining.
    assert!(server.receive(Duration::from_millis(5)).is_err());
    assert_eq!(FRAME_SCRUB_AUDIT.get(), Some((32, true, true)));
    assert!(!server.usable);

    let (_server, mut client) = pair();
    client.send(&vec![0x51; MAX_FRAME], TIMEOUT).unwrap();
    assert!(
        client
            .send(&vec![0x52; MAX_FRAME], Duration::from_millis(5))
            .is_err()
    );
    assert_eq!(
        FRAME_SCRUB_AUDIT.get(),
        Some((HEADER + MAX_FRAME, true, true))
    );
    assert!(!client.usable);
}

#[test]
fn account_control_roundtrip_owns_private_noninheritable_endpoints() {
    let (mut server, mut client) = pair();
    for size in [1, 17, MAX_FRAME] {
        let body = vec![0x61; size];
        client.send(&body, TIMEOUT).unwrap();
        assert_eq!(server.receive(TIMEOUT).unwrap(), body);
        server.send(b"receipt-data-only", TIMEOUT).unwrap();
        assert_eq!(client.receive(TIMEOUT).unwrap(), b"receipt-data-only");
    }
    noninheritable(server.handle.raw()).unwrap();
    noninheritable(client.handle.raw()).unwrap();
    noninheritable(server.peer.process.raw()).unwrap();
}

#[test]
fn account_control_long_birth_wait_retains_original_io_without_extending_ordinary_calls() {
    let (mut server, _client) = pair();
    assert!(server.receive(Duration::from_secs(11)).is_err());
    assert!(!server.usable);

    let (server, address) = ControlServer::prepare(UNUSED_ACCOUNT).unwrap();
    let (release, held) = std::sync::mpsc::channel();
    let writer = std::thread::spawn(move || {
        let mut client = ControlChannel::connect(&address, me()).unwrap();
        // Cross the10s native wait slice. The original pending read must remain
        // alive; cancelling/reissuing it would invalidate the frame protocol.
        std::thread::sleep(Duration::from_millis(10_500));
        client.send(b"fixed-profile-birth-status", TIMEOUT).unwrap();
        held.recv_timeout(Duration::from_secs(10)).unwrap();
    });
    let mut server = server.accept(me(), TIMEOUT).unwrap();
    server.begin().unwrap();
    assert_eq!(
        server
            .receive_until(Instant::now() + Duration::from_secs(15))
            .unwrap(),
        b"fixed-profile-birth-status"
    );
    assert!(server.usable);
    release.send(()).unwrap();
    writer.join().unwrap();
}

#[test]
fn account_control_colliding_server_never_adopts_or_changes_original() {
    let (server, address) = ControlServer::prepare(UNUSED_ACCOUNT).unwrap();
    assert!(ControlServer::create(&address, UNUSED_ACCOUNT).is_err());
    let mut client = ControlChannel::connect(&address, me()).unwrap();
    let mut server = server.accept(me(), TIMEOUT).unwrap();
    client.send(b"original-survived", TIMEOUT).unwrap();
    assert_eq!(server.receive(TIMEOUT).unwrap(), b"original-survived");
    // nMaxInstances=1 also refuses an additional client connection.
    assert!(ControlChannel::connect(&address, me()).is_err());
}

pub(super) fn suspended() -> crate::launch::SpawnedChild {
    let mut directory = [0u16; 32768];
    let length = unsafe {
        windows::Win32::System::SystemInformation::GetSystemDirectoryW(Some(&mut directory))
    } as usize;
    assert!(length > 0 && length < directory.len());
    let exe = format!(
        "{}\\cmd.exe",
        String::from_utf16(&directory[..length]).unwrap()
    );
    let app = wstr(&exe);
    let mut command = wstr(&format!("{} /d /c exit 0", crate::launch::quote_arg(&exe)));
    let info = STARTUPINFOW {
        cb: size_of::<STARTUPINFOW>() as u32,
        ..Default::default()
    };
    let mut process = PROCESS_INFORMATION::default();
    unsafe {
        CreateProcessW(
            pcwstr(&app),
            Some(windows::core::PWSTR(command.as_mut_ptr())),
            None,
            None,
            false,
            CREATE_SUSPENDED | CREATE_NO_WINDOW,
            None,
            None,
            &info,
            &mut process,
        )
        .unwrap()
    };
    crate::launch::SpawnedChild::new(process)
}

#[test]
fn account_control_both_roles_refuse_wrong_original_process() {
    let child = suspended(); // Fixed OS image, never resumed; original guard reaps it.
    let (server, address) = ControlServer::prepare(UNUSED_ACCOUNT).unwrap();
    assert!(
        ControlChannel::connect(&address, ExpectedPeer::retain(child.process()).unwrap()).is_err()
    );
    drop(server);
    let (server, address) = ControlServer::prepare(UNUSED_ACCOUNT).unwrap();
    let _client = ControlChannel::connect(&address, me()).unwrap();
    assert!(
        server
            .accept(ExpectedPeer::retain(child.process()).unwrap(), TIMEOUT)
            .is_err()
    );
    let retained = ExpectedPeer::retain(child.process()).unwrap();
    drop(child);
    assert!(retained.alive().is_err());
    assert!(ExpectedPeer::retain(HANDLE::default()).is_err());
}

#[test]
fn account_control_deadlines_drain_real_pending_connect_read_and_write() {
    let short = Duration::from_millis(5);
    let (server, _) = ControlServer::prepare(UNUSED_ACCOUNT).unwrap();
    let start = Instant::now();
    assert!(server.accept(me(), short).is_err());
    assert!(start.elapsed() < Duration::from_secs(4));
    let (mut server, _client) = pair();
    assert!(server.receive(short).is_err());
    assert!(
        server
            .receive(TIMEOUT)
            .unwrap_err()
            .to_string()
            .contains("permanently refused")
    );
    let (_server, mut client) = pair();
    client.send(&vec![3; MAX_FRAME], TIMEOUT).unwrap();
    // The first whole frame fills the configured pipe buffer. The peer never
    // reads, so the next real overlapped write must be cancelled and drained.
    assert!(client.send(&vec![4; MAX_FRAME], short).is_err());
    assert!(client.send(b"cannot-retry", TIMEOUT).is_err());
}

fn raw_frame(client: &ControlChannel<'_>, sequence: u64, len: u32) -> Vec<u8> {
    let mut bytes = b"AC01".to_vec();
    bytes.push(2);
    bytes.extend_from_slice(&client.nonce);
    bytes.extend_from_slice(&sequence.to_le_bytes());
    bytes.extend_from_slice(&len.to_le_bytes());
    bytes
}

#[test]
fn account_control_rejects_foreign_nonce_direction_sequence_and_size_before_body() {
    for defect in ["magic", "nonce", "role", "sequence", "zero", "oversize"] {
        let (mut server, client) = pair();
        let mut bytes = raw_frame(&client, 0, 1);
        match defect {
            "magic" => bytes[0] ^= 1,
            "nonce" => bytes[5] ^= 1,
            "role" => bytes[4] = 1,
            "sequence" => bytes[21] = 1,
            "zero" => bytes[29..33].copy_from_slice(&0u32.to_le_bytes()),
            "oversize" => bytes[29..33].copy_from_slice(&((MAX_FRAME + 1) as u32).to_le_bytes()),
            _ => unreachable!(),
        }
        io::transfer(
            client.handle.raw(),
            &mut bytes,
            true,
            deadline(TIMEOUT).unwrap(),
        )
        .unwrap();
        assert!(server.receive(TIMEOUT).is_err(), "accepted {defect}");
        assert!(!server.usable);
        assert!(server.receive(TIMEOUT).is_err());
    }
}

#[test]
fn account_control_partial_frame_disconnect_and_replay_poison_connection() {
    for partial in [2, HEADER] {
        let (mut server, client) = pair();
        let mut bytes = raw_frame(&client, 0, 2);
        bytes.truncate(partial);
        io::transfer(
            client.handle.raw(),
            &mut bytes,
            true,
            deadline(TIMEOUT).unwrap(),
        )
        .unwrap();
        drop(client);
        assert!(server.receive(TIMEOUT).is_err());
        assert!(!server.usable);
    }
    let (mut server, mut client) = pair();
    client.send(b"first", TIMEOUT).unwrap();
    server.receive(TIMEOUT).unwrap();
    let mut replay = raw_frame(&client, 0, 1);
    io::transfer(
        client.handle.raw(),
        &mut replay,
        true,
        deadline(TIMEOUT).unwrap(),
    )
    .unwrap();
    assert!(server.receive(TIMEOUT).is_err());
}

#[test]
fn account_control_bounds_and_handle_drift_cannot_be_retried() {
    let (mut server, mut client) = pair();
    for _ in 0..MAX_FRAMES {
        client.send(b"x", TIMEOUT).unwrap();
        server.receive(TIMEOUT).unwrap();
    }
    assert!(client.send(b"over-count", TIMEOUT).is_err());
    assert!(server.receive(TIMEOUT).is_err());
    assert!(!server.usable);
    let (mut server, mut client) = pair();
    for _ in 0..MAX_BYTES / MAX_FRAME {
        client.send(&vec![1; MAX_FRAME], TIMEOUT).unwrap();
        server.receive(TIMEOUT).unwrap();
    }
    assert!(client.send(b"over-total", TIMEOUT).is_err());
    assert!(server.receive(TIMEOUT).is_err());
    assert!(!server.usable);
    let (mut server, _client) = pair();
    unsafe {
        SetHandleInformation(
            server.handle.raw(),
            HANDLE_FLAG_INHERIT.0,
            HANDLE_FLAG_INHERIT,
        )
        .unwrap()
    };
    assert!(server.receive(TIMEOUT).is_err());
    assert!(
        !server.usable,
        "handle drift must permanently refuse the channel"
    );
    unsafe {
        SetHandleInformation(server.handle.raw(), HANDLE_FLAG_INHERIT.0, HANDLE_FLAGS(0)).unwrap()
    };
    assert!(server.receive(TIMEOUT).is_err());
    assert!(!server.usable);
    assert!(deadline(Duration::ZERO).is_err());
    assert!(deadline(Duration::from_secs(11)).is_err());
    assert!(ControlAddress { nonce: [0; 16] }.path().is_err());
}

#[test]
fn account_control_invalid_accounts_and_inheritable_listener_are_refused() {
    for sid in [
        "WD",
        "S-1-1-0",
        "SY",
        "S-1-5-18",
        "S-1-5-32-545",
        "S-1-5-21-1-2-3",
        "S-1-5-21-1-2-3-4-5",
        "S-1-5-21-01-2-3-4",
        "S-1-5-21-1-2-3-4\0ignored",
        &crate::sid::current_user_sid().unwrap(),
    ] {
        assert!(ControlServer::prepare(sid).is_err(), "accepted {sid:?}");
    }
    let (server, address) = ControlServer::prepare(UNUSED_ACCOUNT).unwrap();
    let _client = ControlChannel::connect(&address, me()).unwrap();
    unsafe {
        SetHandleInformation(
            server.handle.raw(),
            HANDLE_FLAG_INHERIT.0,
            HANDLE_FLAG_INHERIT,
        )
        .unwrap();
    }
    assert!(server.accept(me(), TIMEOUT).is_err());
}

#[test]
fn account_control_native_descriptor_only_grants_client_data_rights() {
    use windows::Win32::Security::{
        ACCESS_ALLOWED_ACE, ACL,
        Authorization::{GetSecurityInfo, SE_FILE_OBJECT},
        DACL_SECURITY_INFORMATION, GetAce, GetSecurityDescriptorControl, GetSecurityDescriptorDacl,
        GetSecurityDescriptorOwner, GetSecurityDescriptorSacl, LABEL_SECURITY_INFORMATION,
        OWNER_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, PSID, SE_DACL_PROTECTED,
        SECURITY_DESCRIPTOR_CONTROL, SYSTEM_MANDATORY_LABEL_ACE,
    };
    let (server, _) = ControlServer::prepare(UNUSED_ACCOUNT).unwrap();
    let mut raw = PSECURITY_DESCRIPTOR::default();
    unsafe {
        GetSecurityInfo(
            server.handle.raw(),
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION | LABEL_SECURITY_INFORMATION,
            None,
            None,
            None,
            None,
            Some(&mut raw),
        )
        .ok()
        .unwrap()
    };
    let descriptor = OwnedSd::from_raw(raw);
    let mut control = SECURITY_DESCRIPTOR_CONTROL::default();
    let mut revision = 0;
    let mut present = false.into();
    let mut defaulted = false.into();
    let mut acl: *mut ACL = std::ptr::null_mut();
    let mut owner = PSID::default();
    unsafe {
        GetSecurityDescriptorControl(descriptor.ptr, &mut control.0, &mut revision).unwrap();
        GetSecurityDescriptorDacl(descriptor.ptr, &mut present, &mut acl, &mut defaulted).unwrap();
        GetSecurityDescriptorOwner(descriptor.ptr, &mut owner, &mut defaulted).unwrap();
    }
    let owner_sid = crate::sid::current_user_sid().unwrap();
    assert_eq!(crate::sid::psid_to_string(owner).unwrap(), owner_sid);
    assert_ne!(control.0 & SE_DACL_PROTECTED.0, 0);
    assert!(present.as_bool() && !acl.is_null());
    let mut entries = std::collections::BTreeMap::new();
    for index in 0..unsafe { (*acl).AceCount } {
        let mut entry = std::ptr::null_mut();
        unsafe { GetAce(acl, u32::from(index), &mut entry).unwrap() };
        let ace = unsafe { &*entry.cast::<ACCESS_ALLOWED_ACE>() };
        assert_eq!(ace.Header.AceType, 0); // ACCESS_ALLOWED_ACE_TYPE
        assert_eq!(ace.Header.AceFlags, 0);
        let sid = PSID(std::ptr::addr_of!(ace.SidStart).cast_mut().cast());
        assert!(
            entries
                .insert(crate::sid::psid_to_string(sid).unwrap(), ace.Mask)
                .is_none()
        );
    }
    assert_eq!(entries.len(), 4);
    assert_eq!(entries[UNUSED_ACCOUNT], CLIENT_RIGHTS);
    assert_eq!(entries["S-1-3-4"], 0x20000); // OWNER RIGHTS: READ_CONTROL only
    assert!(entries.contains_key("S-1-5-18") && entries.contains_key(&owner_sid));
    assert_eq!(CLIENT_RIGHTS & (0x4 | 0x40000 | 0x80000), 0); // no server/write-DAC/owner
    unsafe {
        GetSecurityDescriptorSacl(descriptor.ptr, &mut present, &mut acl, &mut defaulted).unwrap();
    }
    assert!(present.as_bool() && !acl.is_null());
    assert_eq!(unsafe { (*acl).AceCount }, 1);
    let mut entry = std::ptr::null_mut();
    unsafe {
        GetAce(acl, 0, &mut entry).unwrap();
    }
    let label = unsafe { &*entry.cast::<SYSTEM_MANDATORY_LABEL_ACE>() };
    assert_eq!(
        (label.Header.AceType, label.Header.AceFlags, label.Mask),
        (17, 0, 3)
    );
    assert_eq!(
        crate::sid::psid_to_string(PSID(std::ptr::addr_of!(label.SidStart).cast_mut().cast()))
            .unwrap(),
        "S-1-16-8192"
    );
}

#[test]
fn account_control_client_ace_allows_data_but_refuses_stronger_rights_and_low_integrity() {
    use windows::Win32::Security::*;
    let base = OwnedHandle(crate::token::open_self_token().unwrap());
    let sid = crate::sid::LocalPsid::from_string(UNUSED_ACCOUNT).unwrap();
    let restricted_code = crate::sid::LocalPsid::from_string("S-1-5-12").unwrap();
    // The second access-check pass can use only the selected client ACE. This
    // is a synthetic restricted token, not a real dedicated-account admission.
    for (low, access, allowed) in [
        (false, CLIENT_RIGHTS, true),
        (false, CLIENT_RIGHTS | 0x40000, false),
        (false, CLIENT_RIGHTS | 0x80000, false),
        (false, CLIENT_RIGHTS | 0x10000, false),
        (false, CLIENT_RIGHTS | 0x100, false),
        (true, CLIENT_RIGHTS, false),
        (true, 1, false),
        (true, 2, false),
    ] {
        let mut raw = HANDLE::default();
        unsafe {
            CreateRestrictedToken(
                base.raw(),
                CREATE_RESTRICTED_TOKEN_FLAGS(0),
                None,
                None,
                Some(&[
                    SID_AND_ATTRIBUTES {
                        Sid: sid.as_psid(),
                        Attributes: 0,
                    },
                    SID_AND_ATTRIBUTES {
                        Sid: restricted_code.as_psid(),
                        Attributes: 0,
                    },
                ]),
                &mut raw,
            )
            .unwrap();
        }
        let token = OwnedHandle(raw);
        let integrity =
            crate::sid::LocalPsid::from_string(if low { "S-1-16-4096" } else { "S-1-16-8192" })
                .unwrap();
        let label = TOKEN_MANDATORY_LABEL {
            Label: SID_AND_ATTRIBUTES {
                Sid: integrity.as_psid(),
                Attributes: 0x20,
            },
        };
        unsafe {
            SetTokenInformation(
                token.raw(),
                TokenIntegrityLevel,
                (&label as *const TOKEN_MANDATORY_LABEL).cast(),
                size_of::<TOKEN_MANDATORY_LABEL>() as u32 + GetLengthSid(integrity.as_psid()),
            )
            .unwrap();
        }
        let (server, address) = ControlServer::prepare(UNUSED_ACCOUNT).unwrap();
        let mut invoked = false;
        crate::account_impersonation::with_token(&token, || {
            invoked = true;
            let opened = unsafe {
                CreateFileW(
                    pcwstr(&wstr(&address.path()?)),
                    access,
                    FILE_SHARE_MODE(0),
                    None,
                    OPEN_EXISTING,
                    FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION,
                    None,
                )
            };
            if allowed {
                let client = OwnedHandle(opened.context("client data-rights open refused")?);
                let mut body = *b"data";
                io::transfer(client.raw(), &mut body, true, deadline(TIMEOUT)?)?;
                let mut received = [0; 4];
                io::transfer(
                    server.handle.raw(),
                    &mut received,
                    false,
                    deadline(TIMEOUT)?,
                )?;
                assert_eq!(received, body);
            } else {
                if let Ok(handle) = opened {
                    drop(OwnedHandle(handle));
                    anyhow::bail!("forbidden pipe rights admitted");
                }
                assert_eq!(
                    opened.err().unwrap().code(),
                    ERROR_ACCESS_DENIED.to_hresult()
                );
            }
            Ok(())
        })
        .with_context(|| {
            format!("pipe access fixture: low={low}, access={access:#x}, invoked={invoked}")
        })
        .unwrap();
    }
}

#[test]
fn account_control_client_open_requires_read_attributes() {
    use windows::Win32::Security::*;
    let base = OwnedHandle(crate::token::open_self_token().unwrap());
    let sid = crate::sid::LocalPsid::from_string(UNUSED_ACCOUNT).unwrap();
    let rc = crate::sid::LocalPsid::from_string("S-1-5-12").unwrap();
    let mut raw = HANDLE::default();
    unsafe {
        CreateRestrictedToken(
            base.raw(),
            CREATE_RESTRICTED_TOKEN_FLAGS(0),
            None,
            None,
            Some(&[
                SID_AND_ATTRIBUTES {
                    Sid: sid.as_psid(),
                    Attributes: 0,
                },
                SID_AND_ATTRIBUTES {
                    Sid: rc.as_psid(),
                    Attributes: 0,
                },
            ]),
            &mut raw,
        )
        .unwrap();
    }
    let token = OwnedHandle(raw);
    let mut raw = HANDLE::default();
    unsafe { DuplicateToken(token.raw(), SecurityImpersonation, &mut raw).unwrap() };
    let access_token = OwnedHandle(raw);
    let owner = crate::sid::current_user_sid().unwrap();
    for (rights, expected) in [(CLIENT_RIGHTS & !0x80, false), (CLIENT_RIGHTS, true)] {
        let address = ControlAddress::fresh().unwrap();
        let sd = OwnedSd::from_sddl(&format!(
            "O:{owner}G:{owner}D:P(A;;GA;;;{owner})(A;;0x{rights:x};;;{UNUSED_ACCOUNT})S:(ML;;NWNR;;;ME)"
        )).unwrap();
        let security = SECURITY_ATTRIBUTES {
            nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: sd.ptr.0,
            bInheritHandle: false.into(),
        };
        let server = OwnedHandle(unsafe {
            CreateNamedPipeW(
                pcwstr(&wstr(&address.path().unwrap())),
                PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE | FILE_FLAG_OVERLAPPED,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                1,
                4096,
                4096,
                0,
                Some(&security),
            )
        });
        assert!(!server.raw().is_invalid());
        // Read back the kernel-mapped descriptor rather than checking raw SDDL
        // generic ACE masks that have not yet been mapped for a file object.
        let mut raw_sd = PSECURITY_DESCRIPTOR::default();
        unsafe {
            Authorization::GetSecurityInfo(
                server.raw(),
                Authorization::SE_FILE_OBJECT,
                OWNER_SECURITY_INFORMATION | GROUP_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                None,
                None,
                None,
                None,
                Some(&mut raw_sd),
            )
            .ok()
            .unwrap();
        }
        let actual_sd = OwnedSd::from_raw(raw_sd);
        // FILE_APPEND_DATA aliases FILE_CREATE_PIPE_INSTANCE. Client opens may
        // accept the append bit without testing server creation; check the DACL
        // explicitly instead of interpreting client-open success as authority.
        for (access, allowed) in [
            (rights, true),
            (rights | 0x4, false),
            (rights | 0x40000, false),
            (rights | 0x80000, false),
        ] {
            let mapping = GENERIC_MAPPING {
                GenericRead: FILE_GENERIC_READ.0,
                GenericWrite: FILE_GENERIC_WRITE.0,
                GenericExecute: FILE_GENERIC_EXECUTE.0,
                GenericAll: FILE_ALL_ACCESS.0,
            };
            let mut privileges = [0usize; 128];
            let mut size = std::mem::size_of_val(&privileges) as u32;
            let mut granted = 0;
            let mut status = false.into();
            unsafe {
                AccessCheck(
                    actual_sd.ptr,
                    access_token.raw(),
                    access,
                    &mapping,
                    Some(privileges.as_mut_ptr().cast()),
                    &mut size,
                    &mut granted,
                    &mut status,
                )
                .unwrap()
            };
            assert_eq!(
                status.as_bool(),
                allowed,
                "DACL mask={rights:#x}, requested={access:#x}"
            );
        }
        let result = crate::account_impersonation::with_token(&token, || {
            let handle = unsafe {
                CreateFileW(
                    pcwstr(&wstr(&address.path()?)),
                    CLIENT_RIGHTS,
                    FILE_SHARE_MODE(0),
                    None,
                    OPEN_EXISTING,
                    FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION,
                    None,
                )
            }?;
            drop(OwnedHandle(handle));
            Ok(())
        });
        assert_eq!(
            result.is_ok(),
            expected,
            "pipe mask {rights:#x}: {result:?}"
        );
        if !expected {
            assert_eq!(
                result
                    .unwrap_err()
                    .downcast_ref::<windows::core::Error>()
                    .unwrap()
                    .code(),
                ERROR_ACCESS_DENIED.to_hresult()
            );
        }
    }
}

const PEER_FIXTURE: &str = "account_control::tests::account_control_peer_fixture";
const PEER_HANDLE: &str = "ALYSIS_TEST_CONTROL_ORIGINAL_PEER";
const PEER_ADDRESS: &str = "ALYSIS_TEST_CONTROL_ADDRESS";

#[test]
#[ignore = "bounded subprocess fixture invoked by account_control_separate_process_roundtrip_and_exit"]
fn account_control_peer_fixture() {
    // Test binary only. The parent explicitly inherits its original process
    // reference and the address; no PID lookup or production handoff is modeled.
    let value: usize = std::env::var(PEER_HANDLE).unwrap().parse().unwrap();
    assert!(value > 0 && value < isize::MAX as usize);
    let inherited = OwnedHandle(HANDLE(value as *mut _));
    let image =
        crate::account_state::image::PinnedRunnerImage::fixture(&std::env::current_exe().unwrap())
            .unwrap();
    let peer = ExpectedPeer::retain(inherited.raw())
        .unwrap()
        .with_image(&image)
        .unwrap();
    drop(inherited);
    assert_ne!(peer.pid, std::process::id());
    let address: ControlAddress =
        serde_json::from_str(&std::env::var(PEER_ADDRESS).unwrap()).unwrap();
    let mut client = ControlChannel::connect(&address, peer).unwrap();
    if let Ok(mode) = std::env::var("ALYSIS_TEST_CONTROL_TERMINAL") {
        if !mode.is_empty() {
            super::handoff::tests::terminal_client(client, &mode);
            unreachable!();
        }
    }
    client.send(b"ready", TIMEOUT).unwrap();
    if std::env::var("ALYSIS_TEST_CONTROL_WAIT_FOR_KILL").as_deref() == Ok("1") {
        // Test-only noncooperating child. Pipe closure cannot make it exit and
        // falsely appear to prove kernel job containment after broker death.
        unsafe {
            Sleep(30000);
        }
        panic!("owned job did not kill bounded bootstrap fixture");
    }
    client.send(b"buffered-while-alive", TIMEOUT).unwrap();
    let challenge = client.receive(TIMEOUT).unwrap();
    assert_eq!(challenge, vec![0x71; MAX_FRAME]);
    client.send(&challenge, TIMEOUT).unwrap();
    assert_eq!(client.receive(TIMEOUT).unwrap(), b"finish");
    client.send(b"buffered-before-exit", TIMEOUT).unwrap();
}

#[test]
fn account_control_separate_process_roundtrip_and_exit() {
    separate_process_exchange(PeerFault::None);
}

#[test]
fn account_control_separate_process_token_mutation_permanently_blocks_response() {
    separate_process_exchange(PeerFault::Token);
}

#[derive(Clone, Copy)]
enum PeerFault {
    None,
    Token,
    Job(&'static str, bool),
    Terminal(&'static str),
}

#[test]
fn account_handoff_terminal_observes_original_exit_without_closing_the_pipe_early() {
    for _ in 0..16 {
        separate_process_exchange(PeerFault::Terminal("complete"));
    }
    for mode in ["wrong-exit", "wait", "token", "wrong-ready"] {
        separate_process_exchange(PeerFault::Terminal(mode));
    }
}

#[test]
fn account_control_separate_process_job_mutation_permanently_blocks_response() {
    for kind in [
        "breakaway",
        "silent-breakaway",
        "kill",
        "ui",
        "dacl",
        "inherit-dacl",
        "inherit-handle",
    ] {
        separate_process_exchange(PeerFault::Job(kind, false));
    }
}

#[test]
fn account_control_job_drift_refuses_incoming_buffered_messages() {
    for kind in ["breakaway", "ui", "dacl", "inherit-handle"] {
        separate_process_exchange(PeerFault::Job(kind, true));
    }
}

fn separate_process_exchange(fault: PeerFault) {
    let job = crate::job::Job::new(false).unwrap();
    let image =
        crate::account_state::image::PinnedRunnerImage::fixture(&std::env::current_exe().unwrap())
            .unwrap();
    let (server, address) = ControlServer::prepare(UNUSED_ACCOUNT).unwrap();
    let mut inherited = HANDLE::default();
    unsafe {
        DuplicateHandle(
            GetCurrentProcess(),
            GetCurrentProcess(),
            GetCurrentProcess(),
            &mut inherited,
            (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
            true,
            DUPLICATE_HANDLE_OPTIONS(0),
        )
        .unwrap();
    }
    let inherited = OwnedHandle(inherited);
    let exe = std::env::current_exe().unwrap();
    let app = wstr(exe.to_str().unwrap());
    let mut command = wstr(&format!(
        "{} --exact {PEER_FIXTURE} --ignored --nocapture --test-threads=1",
        crate::launch::quote_arg(exe.to_str().unwrap())
    ));
    let mut environment = crate::launch::build_env_block_with_inheritance(
        &[
            (PEER_HANDLE.into(), (inherited.raw().0 as usize).to_string()),
            (
                PEER_ADDRESS.into(),
                serde_json::to_string(&address).unwrap(),
            ),
            ("SystemRoot".into(), std::env::var("SystemRoot").unwrap()),
            (
                "ALYSIS_TEST_CONTROL_TERMINAL".into(),
                match fault {
                    PeerFault::Terminal(mode) => mode.to_owned(),
                    _ => String::new(),
                },
            ),
            (
                "TEMP".into(),
                crate::util::test_temp_dir().to_str().unwrap().into(),
            ),
            (
                "TMP".into(),
                crate::util::test_temp_dir().to_str().unwrap().into(),
            ),
        ],
        false,
    );
    let mut attributes = crate::launch::ProcThreadAttrs::new(2).unwrap();
    attributes.set_handle_list(&[inherited.raw()]).unwrap();
    attributes.set_job(&job).unwrap();
    let mut info = STARTUPINFOEXW::default();
    info.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
    info.lpAttributeList = attributes.list();
    let mut process = PROCESS_INFORMATION::default();
    unsafe {
        CreateProcessW(
            pcwstr(&app),
            Some(windows::core::PWSTR(command.as_mut_ptr())),
            None,
            None,
            true,
            CREATE_SUSPENDED
                | CREATE_NO_WINDOW
                | CREATE_UNICODE_ENVIRONMENT
                | EXTENDED_STARTUPINFO_PRESENT,
            Some(environment.as_mut_ptr().cast()),
            None,
            &info.StartupInfo,
            &mut process,
        )
        .unwrap();
    }
    let child = crate::launch::SpawnedChild::new(process);
    let peer = ExpectedPeer::retain(child.process())
        .unwrap()
        .with_job(&job)
        .unwrap()
        .with_image(&image)
        .unwrap();
    // Same-user fixture only: exercise actual account/session observations, not
    // the production installed-account admission or cross-account handoff.
    let mut session = 0;
    unsafe {
        windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
            std::process::id(),
            &mut session,
        )
        .unwrap()
    };
    peer.token
        .require_account_runner(&crate::sid::current_user_sid().unwrap(), session)
        .unwrap();
    assert_ne!(peer.pid, std::process::id());
    drop(inherited); // Child owns its sole inherited reference.
    assert_ne!(unsafe { ResumeThread(child.thread()) }, u32::MAX);
    let mut server = server.accept(peer, TIMEOUT).unwrap();
    if let PeerFault::Terminal(mode) = fault {
        super::handoff::tests::terminal_source(server, &child, &image, mode);
        return;
    }
    assert_eq!(server.receive(TIMEOUT).unwrap(), b"ready");
    if matches!(fault, PeerFault::Token) {
        let token = super::token_tests::mutate_disposable_child(&child);
        assert!(server.send(b"must-not-send", TIMEOUT).is_err());
        assert!(!server.usable);
        super::token_tests::toggle_traverse(token.raw());
        assert!(server.send(b"restored", TIMEOUT).is_err());
        // The armed original child guard terminates/reaps this bounded fixture.
        return;
    }
    if let PeerFault::Job(kind, receiving) = fault {
        let result = super::containment::tests::with_fault(&job, kind, || {
            if receiving {
                server.receive(TIMEOUT).map(|_| ())
            } else {
                server.send(b"must-not-send", TIMEOUT)
            }
        });
        assert!(
            result.is_err(),
            "control channel accepted a peer after containment was weakened"
        );
        assert!(!server.usable);
        assert!(server.send(b"restored", TIMEOUT).is_err());
        assert!(server.receive(TIMEOUT).is_err());
        return;
    }
    assert_eq!(server.receive(TIMEOUT).unwrap(), b"buffered-while-alive");
    server.send(&vec![0x71; MAX_FRAME], TIMEOUT).unwrap();
    assert_eq!(server.receive(TIMEOUT).unwrap(), vec![0x71; MAX_FRAME]);
    server.write_in_flight(b"finish", TIMEOUT).unwrap();
    assert_eq!(
        unsafe { WaitForSingleObject(child.process(), 5000) },
        WAIT_OBJECT_0
    );
    let mut code = u32::MAX;
    unsafe { GetExitCodeProcess(child.process(), &mut code).unwrap() };
    assert_eq!(code, 0);
    // Even a complete buffered message cannot outlive the authenticated peer.
    assert!(server.receive(TIMEOUT).is_err());
    assert!(!server.usable);
}

#[test]
fn account_control_impersonation_failure_stays_refused_after_reversion() {
    let (mut server, _client) = pair();
    let token = OwnedHandle(crate::token::open_self_token().unwrap());
    crate::account_impersonation::with_token(&token, || {
        assert!(server.receive(TIMEOUT).is_err());
        assert!(!server.usable);
        assert!(ControlServer::prepare(UNUSED_ACCOUNT).is_err());
        assert!(ExpectedPeer::retain(unsafe { GetCurrentProcess() }).is_err());
        Ok(())
    })
    .unwrap();
    assert!(server.send(b"after-revert", TIMEOUT).is_err());
}

#[test]
fn account_control_partial_progress_cannot_extend_whole_frame_deadline() {
    let (server, address) = ControlServer::prepare(UNUSED_ACCOUNT).unwrap();
    let barrier = std::sync::Arc::new(std::sync::Barrier::new(2));
    let peer_barrier = barrier.clone();
    let writer = std::thread::spawn(move || {
        let client = ControlChannel::connect(&address, me()).unwrap();
        peer_barrier.wait();
        std::thread::sleep(Duration::from_millis(200));
        let mut header = raw_frame(&client, 0, 2);
        io::transfer(
            client.handle.raw(),
            &mut header,
            true,
            deadline(TIMEOUT).unwrap(),
        )
        .unwrap();
        std::thread::sleep(Duration::from_millis(200));
        io::transfer(
            client.handle.raw(),
            &mut [1],
            true,
            deadline(TIMEOUT).unwrap(),
        )
        .unwrap();
        std::thread::sleep(Duration::from_millis(200));
        // The server retains the failed endpoint through the join, so the last
        // byte fits its buffer even after the pending read has been cancelled.
        io::transfer(
            client.handle.raw(),
            &mut [2],
            true,
            deadline(TIMEOUT).unwrap(),
        )
        .unwrap();
    });
    let mut server = server.accept(me(), TIMEOUT).unwrap();
    barrier.wait();
    let start = Instant::now();
    let result = server.receive(Duration::from_millis(500));
    writer.join().unwrap();
    assert!(
        result.is_err(),
        "partial progress reset the whole-frame deadline"
    );
    assert!(!server.usable);
    assert!(start.elapsed() < Duration::from_secs(4));
    assert!(server.receive(TIMEOUT).is_err());
}
