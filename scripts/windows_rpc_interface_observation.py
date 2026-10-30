"""Bounded file-only RPC interface identities at already-observed call sites.

This decodes x64 SDK layouts, never process memory or executable stubs. Matching
identifiers establish static model relevance, not the live server's descriptor.
"""

import hashlib
import re
import struct
import uuid


def call_arguments(functions, imports, image_base, symbol):
    """Conservative direct RCX LEA, with no intervening call/branch/clobber."""
    records = []
    incoming = set()
    for function in functions:
        incoming.update(
            int(value, 16) - image_base
            for value in re.findall(
                r"^[ \t]*[0-9A-Fa-f]{16}:[ \t]+(?:j\w+|call)[ \t]+([0-9A-Fa-f]{16})[ \t]*$",
                function.get("disassembly", ""),
                re.MULTILINE,
            )
        )
    for function in functions:
        instructions = re.findall(
            r"^[ \t]*([0-9A-Fa-f]{16}):[ \t]+(\w+)[ \t]*([^\r\n]*)",
            function.get("disassembly", ""),
            re.MULTILINE,
        )
        rcx = None
        procedure = None
        for address, op, operands in instructions:
            op, operands = op.lower(), operands.lower().strip()
            address = int(address, 16) - image_base
            assert function["start"] <= address < function["end"]
            if op == "call":
                target = re.fullmatch(r"qword ptr \[([0-9a-f]{16})h\]", operands)
                imported = imports.get(target[1].upper(), "") if target else ""
                if imported.endswith("!" + symbol):
                    assert len(records) < 16, "RPC callsite limit"
                    if rcx and any(rcx["lea_rva"] < target <= address for target in incoming):
                        rcx = None
                    if procedure and any(
                        procedure["assignment_rva"] < target <= address for target in incoming
                    ):
                        procedure = None
                    records.append(
                        dict(
                            function_rva=function["start"],
                            call_rva=address,
                            rcx=rcx,
                            procedure=procedure["value"] if procedure else None,
                            procedure_assignment_rva=procedure["assignment_rva"]
                            if procedure
                            else None,
                        )
                    )
                rcx = procedure = None
                continue
            if op.startswith("j") or op in {"ret", "int", "int3", "ud2", "syscall"}:
                rcx = procedure = None
                continue
            # Only instructions with explicit first-operand writes (or known
            # read-only operands) are tracked. Reject multi-output, implicit
            # register writes and prefixes, e.g. xchg/mul/cpuid/loop/rep.
            if op not in {
                "mov",
                "lea",
                "xor",
                "cmp",
                "test",
                "nop",
                "push",
                "pop",
                "add",
                "sub",
                "and",
                "or",
                "movzx",
                "movsx",
                "movsxd",
                "shr",
                "shl",
                "sar",
                "sal",
                "inc",
                "dec",
                "neg",
                "not",
            }:
                rcx = procedure = None
                continue
            first = operands.split(",", 1)[0]
            if first in {"rcx", "ecx", "cx", "cl", "ch"} and op not in {"cmp", "test"}:
                target = re.fullmatch(r"rcx,\[([0-9a-f]{16})h\]", operands)
                rcx = (
                    dict(lea_rva=address, value_rva=int(target[1], 16) - image_base)
                    if op == "lea" and target
                    else None
                )
            if first in {"rdx", "edx", "dx", "dl", "dh"} and op not in {"cmp", "test"}:
                procedure = (
                    dict(value=0, assignment_rva=address)
                    if op == "xor" and operands in {"edx,edx", "rdx,rdx"}
                    else None
                )
    return records


def observe_interfaces(data, offset, image_base, functions, imports, image_name, entry_name):
    if (image_name, entry_name) not in {
        ("sspicli.dll", "LsaConnectUntrusted"),
        ("sspisrv.dll", "SspiSrvInitialize"),
    }:
        return None
    client = image_name == "sspicli.dll"
    symbol = "NdrClientCall3" if client else "RpcServerRegisterIf3"

    def read(rva, size):
        assert 0 <= rva < 8_000_000 and 0 < size <= 96
        start = offset(rva, size)
        value = bytes(data[start : start + size])
        assert len(value) == size
        return value

    def pointer(rva):
        assert rva % 8 == 0, "unaligned RPC metadata pointer"
        value = struct.unpack("<Q", read(rva, 8))[0] - image_base
        assert 0 <= value < 8_000_000 and value % 8 == 0, "RPC pointer outside image"
        read(value, 8)
        return value

    def syntax(value):
        assert len(value) == 20 and any(value[:16]), "empty RPC syntax"
        return dict(
            uuid=str(uuid.UUID(bytes_le=value[:16])), version=list(struct.unpack("<HH", value[16:]))
        )

    calls = call_arguments(functions, imports, image_base, symbol)
    result = []
    for call in calls:
        item = dict(call, resolved=False)
        if not client:
            item.pop("procedure")  # Server RDX is MgrTypeUuid, not a procedure.
            item.pop("procedure_assignment_rva")
        if call["rcx"] is None:
            item["reason"] = "direct_rcx_reference_unresolved"
            result.append(item)
            continue
        try:
            rva = call["rcx"]["value_rva"]
            if client:
                # MIDL_STUBLESS_PROXY_INFO.pStubDesc ->
                # MIDL_STUB_DESC.RpcInterfaceInformation; both first fields.
                stub = pointer(rva)
                interface = pointer(stub)
                item["proxy_rva"] = rva
                item["stub_descriptor_rva"] = stub
                item["pointer_chain_sha256"] = hashlib.sha256(
                    read(rva, 8) + read(stub, 8)
                ).hexdigest()
                rva = interface
            value = read(rva, 96)
            assert struct.unpack_from("<I", value)[0] == 96, "unexpected x64 interface length"
            item.update(
                resolved=True,
                interface_rva=rva,
                record_sha256=hashlib.sha256(value).hexdigest(),
                interface=syntax(value[4:24]),
                transfer=syntax(value[24:44]),
                flags=struct.unpack_from("<I", value, 88)[0],
            )
        except (AssertionError, ValueError, struct.error):
            # An unsupported layout is explicitly unresolved, never a match.
            item["reason"] = "file_backed_rpc_layout_unresolved"
        result.append(item)
    return dict(
        diagnostic_only=True,
        runtime_registration_verified=False,
        layout="windows-sdk-x64",
        symbol=symbol,
        max_calls=16,
        max_pointer_hops=2 if client else 0,
        calls=result,
    )


def compare_interfaces(client, server):
    if not client or not server:
        return dict(diagnostic_only=True, complete=False, matches=[])
    candidates = [c for c in client["calls"] if c["procedure"] == 0]
    matches = []
    for c in candidates:
        if not c["resolved"]:
            continue
        for s in server["calls"]:
            if s["resolved"] and c["interface"] == s["interface"]:
                matches.append(
                    dict(
                        client_call_rva=c["call_rva"],
                        server_call_rva=s["call_rva"],
                        interface=c["interface"],
                        transfer_equal=c["transfer"] == s["transfer"],
                    )
                )
    return dict(
        diagnostic_only=True,
        complete=bool(candidates)
        and bool(server["calls"])
        and all(c["procedure"] is not None for c in client["calls"])
        and all(c["resolved"] for c in candidates + server["calls"]),
        live_descriptor_verified=False,
        matches=matches,
    )
