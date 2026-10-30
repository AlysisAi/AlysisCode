"""TCP/UDP host-boundary checks with live IPv4, IPv6 and mapped positive controls.

These tests do not qualify access to other LAN machines or remote ingress.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import subprocess
import uuid
from pathlib import Path


def tcp_sender(address: str, port: int, payload: str) -> str:
    """The identical bounded Node sender is used inside and outside containment."""
    return (
        "const s=require('node:net').connect({host:"
        + json.dumps(address)
        + f",port:{port}"
        + "});let done=false;"
        "function finish(state,rc){if(done)return;done=true;s.destroy();console.log(state);process.exitCode=rc}"
        "s.setTimeout(2500,()=>finish('TIMEOUT',2));"
        "s.on('connect',()=>s.end(" + json.dumps(payload) + ",()=>finish('CONNECTED',1)));"
        "s.on('error',e=>finish(e.code,['EACCES','EPERM'].includes(e.code)?0:3))"
    )


def tcp_boundary_outcome(event: dict) -> str:
    """Timeouts alone prove nothing; require live controls and a silent receiver."""
    assert event.get("host_identical_control_before") is True, event
    assert event.get("host_identical_control_after") is True, event
    assert event.get("host_accepted_connection") is False, event
    result = (event["exit_code"], event["stdout"].strip())
    if result in {(0, "EACCES"), (0, "EPERM")}:
        return "explicit-denial"
    if result in {(2, "TIMEOUT"), (3, "ETIMEDOUT")}:
        return "timeout-with-no-host-delivery"
    raise AssertionError(f"Unqualified TCP result: {event}")


def tcp_positive_control(node: Path, work: Path, code: str, server, payload: bytes) -> None:
    control = subprocess.run([str(node), "-e", code], cwd=work, capture_output=True, timeout=5)
    assert control.returncode == 1 and control.stdout.strip() == b"CONNECTED", control
    server.settimeout(3)
    accepted, _ = server.accept()
    with accepted:
        accepted.settimeout(3)
        received = bytearray()
        while len(received) < len(payload):
            chunk = accepted.recv(len(payload) - len(received))
            assert chunk, "Host TCP sender closed before its payload arrived"
            received.extend(chunk)
        assert bytes(received) == payload


def qualify(helper: Path, node: Path, work: Path, request, record):
    endpoints = [
        ("ipv4_loopback", socket.AF_INET, "127.0.0.1"),
        ("ipv6_loopback", socket.AF_INET6, "::1"),
        ("ipv4_mapped", socket.AF_INET6, "::ffff:127.0.0.1"),
    ]
    private = sorted(
        {
            row[4][0]
            for row in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
            if ipaddress.ip_address(row[4][0]).is_private
            and not ipaddress.ip_address(row[4][0]).is_loopback
            and not ipaddress.ip_address(row[4][0]).is_link_local
        }
    )
    # Bound one locally assigned private NIC address, never scan other devices.
    if private:
        endpoints.append(("host_private_ipv4", socket.AF_INET, private[0]))
    else:
        record({"name": "host_private_ipv4", "qualified": False, "reason": "no local private IPv4"})
    for label, family, address in endpoints:
        for protocol, socktype in [("tcp", socket.SOCK_STREAM), ("udp", socket.SOCK_DGRAM)]:
            with socket.socket(family, socktype) as server:
                if family == socket.AF_INET6:
                    server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                server.bind((address, 0))
                server.settimeout(3)
                port = server.getsockname()[1]
                token = uuid.uuid4().hex.encode()
                if protocol == "tcp":
                    server.listen(4)
                    with socket.socket(family, socktype) as client:
                        client.settimeout(3)
                        if family == socket.AF_INET6:
                            client.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                        client.connect((address, port))
                        client.sendall(token)
                        accepted, _ = server.accept()
                        with accepted:
                            accepted.settimeout(3)
                            assert accepted.recv(128) == token
                else:
                    with socket.socket(family, socktype) as client:
                        if family == socket.AF_INET6:
                            client.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                        client.sendto(token, (address, port))
                        assert server.recvfrom(128)[0] == token
                record({"name": f"host_control_{label}_{protocol}", "ok": True})
                for network in ["off", "on"]:
                    target = json.dumps(address)
                    if protocol == "tcp":
                        code = tcp_sender(address, port, token.decode())
                        tcp_positive_control(node, work, code, server, token)
                    else:
                        udp_family = "udp6" if family == socket.AF_INET6 else "udp4"
                        code = (
                            f"const s=require('node:dgram').createSocket('{udp_family}');"
                            "let done=false;const timer=setTimeout(()=>finish(null,'TIMEOUT'),2500);"
                            "function finish(e,state){if(done)return;done=true;clearTimeout(timer);s.close();setTimeout(()=>{},400);"
                            "console.log(e?.code||state);if(state!=='SENT'&&!['EACCES','EPERM'].includes(e?.code))process.exitCode=1}"
                            "s.on('error',e=>finish(e,'ERROR'));"
                            f"s.send('synthetic-mxc-boundary',{port},{target},e=>finish(e,'SENT'));"
                        )
                        if network == "off":
                            # Run the exact sender on the host, including its
                            # socket-close timing, before trusting a lack of
                            # delivery from the sandboxed copy.
                            control = subprocess.run(
                                [str(node), "-e", code],
                                cwd=work,
                                capture_output=True,
                                timeout=5,
                            )
                            assert control.returncode == 0 and control.stdout.strip() == b"SENT"
                            server.settimeout(3)
                            assert server.recvfrom(256)[0] == b"synthetic-mxc-boundary"
                            record({"name": f"host_identical_node_sender_{label}_udp", "ok": True})
                    process = subprocess.run(
                        [str(helper), "mxc-run-request"],
                        cwd=work,
                        capture_output=True,
                        input=json.dumps(request([node, "-e", code], network=network)).encode(),
                        timeout=15,
                    )
                    event = {
                        "name": f"{network}_{label}_{protocol}_denied",
                        "exit_code": process.returncode,
                        "stdout": process.stdout.decode(errors="replace"),
                        "stderr": process.stderr.decode(errors="replace"),
                    }
                    if protocol == "tcp":
                        event["host_identical_control_before"] = True
                        # Inspect the listener before accepting timeout/error as
                        # containment. Even a connection carrying no data fails.
                        server.settimeout(0.75)
                        try:
                            escaped, _ = server.accept()
                        except TimeoutError:
                            event["host_accepted_connection"] = False
                        else:
                            escaped.close()
                            event["host_accepted_connection"] = True
                        # Preserve the attempt even if the following control
                        # fails; a missing/failed control cannot qualify it.
                        record({**event, "stage": "receiver-observed"})
                        tcp_positive_control(node, work, code, server, token)
                        event["host_identical_control_after"] = True
                    if protocol == "udp":
                        # Winsock may acknowledge enqueue before WFP drops UDP.
                        # Preserve that distinction and observe the live receiver.
                        server.settimeout(0.75)
                        try:
                            packet, _ = server.recvfrom(256)
                        except TimeoutError:
                            event["host_received_datagram"] = False
                        else:
                            event["host_received_datagram"] = True
                            event["received_synthetic_payload"] = (
                                packet == b"synthetic-mxc-boundary"
                            )
                        # A positive control AFTER the attempt rules out a dead
                        # listener or an unrelated loss of local connectivity.
                        with socket.socket(family, socktype) as client:
                            if family == socket.AF_INET6:
                                client.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                            client.sendto(token, (address, port))
                            assert server.recvfrom(128)[0] == token
                        event["host_control_after"] = True
                    if protocol == "udp":
                        record(event)
                        assert process.returncode == 0 and process.stdout.strip() in (
                            b"EACCES",
                            b"EPERM",
                            b"SENT",
                        ), event
                        assert not event["host_received_datagram"], event
                    else:
                        # Record all evidence before a failed assertion. Keep
                        # explicit OS denial separate from receiver-proven drop.
                        record({**event, "stage": "controls-complete"})
                        event["boundary_outcome"] = tcp_boundary_outcome(event)
                        event["boundary_qualified"] = True
                        event["stage"] = "qualified"
                        record(event)
                server.settimeout(0.2)
                try:
                    if protocol == "tcp":
                        escaped, _ = server.accept()
                        escaped.close()
                    else:
                        server.recvfrom(128)
                except TimeoutError:
                    pass
                else:
                    raise AssertionError(f"sandbox traffic reached host {label}/{protocol}")
