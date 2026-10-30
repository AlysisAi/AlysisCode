//! Fixed numeric TCP connect after HTTPS observation; no DNS, payload or retries.
//! Distinguishes explicit connect denial from unavailable name resolution.
use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use std::time::Instant;
use windows::{Win32::Networking::WinSock::*, core::PSTR};

const TARGET: &str = "1.1.1.1:443";
const DENIED: i32 = 10013;
const PENDING: i32 = 10035;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Wait {
    result: i32,
    error: Option<i32>,
    writable: u32,
    exceptional: u32,
    option_error: Option<i32>,
    option_length: Option<i32>,
    socket_error: Option<i32>,
}
impl Wait {
    fn require(&self) -> Result<()> {
        ensure!(
            (-1..=2).contains(&self.result) && self.writable <= 1 && self.exceptional <= 1,
            "TCP select bounds differ"
        );
        ensure!(
            self.error.is_some() == (self.result == -1),
            "TCP select error context differs"
        );
        ensure!(
            self.option_error.is_some() == (self.result > 0)
                && self.option_length.is_some() == self.option_error.is_some()
                && self.socket_error.is_some()
                    == (self.option_error == Some(0) && self.option_length == Some(4)),
            "TCP socket error lacks successful exact DWORD query"
        );
        if self.result > 0 {
            ensure!(
                self.writable + self.exceptional == self.result as u32,
                "TCP selected descriptors differ"
            );
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Observation {
    target: String,
    startup_error: i32,
    version: Option<u16>,
    socket_hresult: Option<i32>,
    opened: bool,
    nonblocking_error: Option<i32>,
    connect_error: Option<i32>,
    wait: Option<Wait>,
    close_error: Option<i32>,
    cleanup_error: Option<i32>,
    elapsed_ms: u64,
}
impl Observation {
    pub(super) fn require(&self) -> Result<()> {
        ensure!(
            self.target == TARGET && self.elapsed_ms <= 120000,
            "TCP fixed subject or bounds differ"
        );
        let started = self.startup_error == 0;
        ensure!(
            self.version.is_some() == started && self.cleanup_error.is_some() == started,
            "TCP startup/cleanup prefix differs"
        );
        let socket_attempted = self.version == Some(0x202);
        ensure!(
            self.socket_hresult.is_some() == (socket_attempted && !self.opened)
                && (!self.opened || socket_attempted),
            "TCP socket prefix differs"
        );
        ensure!(
            self.nonblocking_error.is_some() == self.opened
                && self.close_error.is_some() == self.opened,
            "TCP socket closure or configuration absent"
        );
        ensure!(
            self.connect_error.is_some() == (self.nonblocking_error == Some(0)),
            "TCP connect precedes nonblocking configuration"
        );
        ensure!(
            self.wait.is_some() == (self.connect_error == Some(PENDING)),
            "TCP wait lacks pending connect"
        );
        if let Some(wait) = &self.wait {
            wait.require()?;
        }
        Ok(())
    }
    fn closed(&self) -> bool {
        self.require().is_ok()
            && self.opened
            && self.close_error == Some(0)
            && self.cleanup_error == Some(0)
            && self.elapsed_ms <= 3000
    }
    pub(super) fn denied(&self) -> bool {
        self.closed()
            && (self.connect_error == Some(DENIED)
                || self.wait.as_ref().is_some_and(|w| {
                    w.result == 1
                        && w.writable == 0
                        && w.exceptional == 1
                        && w.socket_error == Some(DENIED)
                }))
    }
    pub(super) fn connected(&self) -> bool {
        self.closed()
            && (self.connect_error == Some(0)
                || self.wait.as_ref().is_some_and(|w| {
                    w.result == 1
                        && w.writable == 1
                        && w.exceptional == 0
                        && w.socket_error == Some(0)
                }))
    }
    pub(super) fn observe() -> Self {
        let start = Instant::now();
        let mut data = WSADATA::default();
        let startup_error = unsafe { WSAStartup(0x202, &mut data) };
        let mut p = Self {
            target: TARGET.into(),
            startup_error,
            version: None,
            socket_hresult: None,
            opened: false,
            nonblocking_error: None,
            connect_error: None,
            wait: None,
            close_error: None,
            cleanup_error: None,
            elapsed_ms: 0,
        };
        if startup_error == 0 {
            let mut guard = Resources {
                socket: None,
                startup: true,
            };
            p.version = Some(data.wVersion);
            if data.wVersion == 0x202 {
                match unsafe {
                    WSASocketW(
                        AF_INET.0 as i32,
                        SOCK_STREAM.0,
                        IPPROTO_TCP.0,
                        None,
                        0,
                        WSA_FLAG_OVERLAPPED | WSA_FLAG_NO_HANDLE_INHERIT,
                    )
                } {
                    Err(e) => p.socket_hresult = Some(e.code().0),
                    Ok(socket) => {
                        guard.socket = Some(socket);
                        p.opened = true;
                        let mut enabled = 1;
                        p.nonblocking_error =
                            Some(error(unsafe { ioctlsocket(socket, FIONBIO, &mut enabled) }));
                        if p.nonblocking_error == Some(0) {
                            let address = SOCKADDR_IN {
                                sin_family: AF_INET,
                                sin_port: 443u16.to_be(),
                                sin_addr: IN_ADDR {
                                    S_un: IN_ADDR_0 {
                                        S_addr: u32::from_ne_bytes([1, 1, 1, 1]),
                                    },
                                },
                                sin_zero: [0; 8],
                            };
                            p.connect_error = Some(error(unsafe {
                                connect(
                                    socket,
                                    (&address as *const SOCKADDR_IN).cast(),
                                    std::mem::size_of::<SOCKADDR_IN>() as i32,
                                )
                            }));
                            if p.connect_error == Some(PENDING) {
                                p.wait = Some(wait(socket));
                            }
                        }
                        p.close_error =
                            Some(error(unsafe { closesocket(guard.socket.take().unwrap()) }));
                    }
                }
            }
            p.cleanup_error = Some(error(unsafe { WSACleanup() }));
            guard.startup = false;
        }
        p.elapsed_ms = start.elapsed().as_millis().min(u64::MAX as u128) as u64;
        p
    }
}
fn error(result: i32) -> i32 {
    if result == 0 {
        0
    } else {
        unsafe { WSAGetLastError().0 }
    }
}
fn wait(socket: SOCKET) -> Wait {
    let mut write = FD_SET {
        fd_count: 1,
        ..Default::default()
    };
    write.fd_array[0] = socket;
    let mut except = write;
    let timeout = TIMEVAL {
        tv_sec: 1,
        tv_usec: 0,
    };
    let result = unsafe { select(0, None, Some(&mut write), Some(&mut except), Some(&timeout)) };
    let failure = (result == -1).then(|| unsafe { WSAGetLastError().0 });
    let mut w = Wait {
        result,
        error: failure,
        writable: write.fd_count,
        exceptional: except.fd_count,
        option_error: None,
        option_length: None,
        socket_error: None,
    };
    if result > 0 {
        let (mut code, mut length) = (0i32, 4i32);
        w.option_error = Some(error(unsafe {
            getsockopt(
                socket,
                SOL_SOCKET,
                SO_ERROR,
                PSTR((&mut code as *mut i32).cast()),
                &mut length,
            )
        }));
        w.option_length = Some(length);
        if w.option_error == Some(0) && length == 4 {
            w.socket_error = Some(code);
        }
    }
    w
}
struct Resources {
    socket: Option<SOCKET>,
    startup: bool,
}
impl Drop for Resources {
    fn drop(&mut self) {
        if let Some(s) = self.socket.take() {
            unsafe {
                let _ = closesocket(s);
            }
        }
        if self.startup {
            unsafe {
                let _ = WSACleanup();
            }
        }
    }
}

#[test]
fn station_tcp_denial_requires_connect_context_exact_error_and_checked_closure() {
    let p = Observation {
        target: TARGET.into(),
        startup_error: 0,
        version: Some(0x202),
        socket_hresult: None,
        opened: true,
        nonblocking_error: Some(0),
        connect_error: Some(DENIED),
        wait: None,
        close_error: Some(0),
        cleanup_error: Some(0),
        elapsed_ms: 10,
    };
    assert!(p.denied());
    assert!(!p.connected());
    for code in [0, 5, 10060, 10061, 10065, 11001] {
        let mut changed = p.clone();
        changed.connect_error = Some(code);
        assert!(!changed.denied());
    }
    for (key, value) in [
        ("target", "127.0.0.1:443".into()),
        ("opened", false.into()),
        ("nonblocking_error", DENIED.into()),
        ("close_error", 10038.into()),
        ("cleanup_error", 10091.into()),
        ("version", 0x101.into()),
        ("elapsed_ms", 3001.into()),
    ] {
        let mut wire = serde_json::to_value(&p).unwrap();
        wire[key] = value;
        assert!(!serde_json::from_value::<Observation>(wire).is_ok_and(|v| v.denied()));
    }
}
#[test]
fn station_tcp_pending_completion_refuses_timeout_and_inconsistent_socket_error() {
    let mut p = Observation {
        target: TARGET.into(),
        startup_error: 0,
        version: Some(0x202),
        socket_hresult: None,
        opened: true,
        nonblocking_error: Some(0),
        connect_error: Some(PENDING),
        wait: Some(Wait {
            result: 1,
            error: None,
            writable: 0,
            exceptional: 1,
            option_error: Some(0),
            option_length: Some(4),
            socket_error: Some(DENIED),
        }),
        close_error: Some(0),
        cleanup_error: Some(0),
        elapsed_ms: 10,
    };
    assert!(p.denied());
    assert!(!p.connected());
    let mut positive = p.clone();
    let w = positive.wait.as_mut().unwrap();
    w.writable = 1;
    w.exceptional = 0;
    w.socket_error = Some(0);
    assert!(positive.connected());
    assert!(!positive.denied());
    for (key, value) in [
        ("result", 0.into()),
        ("writable", 1.into()),
        ("exceptional", 0.into()),
        ("option_error", 10022.into()),
        ("option_length", 3.into()),
        ("socket_error", 10061.into()),
    ] {
        let mut wire = serde_json::to_value(&p).unwrap();
        wire["wait"][key] = value;
        assert!(!serde_json::from_value::<Observation>(wire).is_ok_and(|v| v.denied()));
    }
    p.wait = None;
    assert!(p.require().is_err());
}
