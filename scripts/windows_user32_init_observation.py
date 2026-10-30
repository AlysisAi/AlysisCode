"""Bounded static Windows initialization evidence; never loads or executes DLLs."""

import hashlib
import json
import os
import re
import struct
import subprocess
import sys
from pathlib import Path

from windows_rpc_interface_observation import compare_interfaces, observe_interfaces


def fixed_rpc_error_leads(data, executable_sections, containing, locations=(40,)):
    assert locations in {(40,), (500, 950)}
    leads = []
    for virtual_size, address, raw_size, raw in executable_sections:
        end = raw + min(virtual_size, raw_size)
        if not 0 <= raw <= end <= len(data):
            raise ValueError("RPC executable section outside file")
        for location, pattern in [
            (n, prefix + struct.pack(fmt, n))
            for n in locations
            for prefix, fmt in [(b"\x41\xb8", "<I"), (b"\x66\x41\xb8", "<H")]
        ]:
            begin = raw
            while (found := data.find(pattern, begin, end)) >= 0:
                begin = found + len(pattern)
                site = address + found - raw
                bounds = containing(site)
                if bounds is not None:
                    if len(leads) >= 8:
                        raise ValueError("RPC fixed-location lead bound")
                    leads.append(
                        {
                            "pattern_rva": site,
                            "function_rva": bounds[0],
                            "pattern_hex": pattern.hex(),
                            "candidate_only": True,
                            "location": location,
                        }
                    )
    return leads


def observe(
    image_name="user32.dll",
    entry_name="UserClientDllInitialize",
    *,
    rpc_error_focus=False,
    rpc_endpoint_focus=False,
):
    assert not (rpc_error_focus and rpc_endpoint_focus)
    assert (image_name, entry_name) in {
        ("user32.dll", "UserClientDllInitialize"),
        ("gdi32full.dll", "GdiDllInitialize"),
        ("kernelbase.dll", "CreateAppContainerToken"),
        ("user32.dll", "OpenWindowStationW"),
        ("win32kfull.sys", "xxxResolveDesktop"),
        ("sspicli.dll", "AcquireCredentialsHandleW"),
        ("sspicli.dll", "QuerySecurityPackageInfoW"),
        ("sspicli.dll", "LsaConnectUntrusted"),
        ("sspisrv.dll", "SspiSrvInitialize"),
        ("rpcrt4.dll", "RpcServerRegisterIf3"),
        ("rpcrt4.dll", "I_RpcGetDefaultSD"),
        ("rpcrt4.dll", "I_RpcFree"),
        ("ntdll.dll", "RtlCreateAndSetSD"),
    }
    image = Path(os.environ["SystemRoot"]) / "System32" / image_name
    data = image.read_bytes()
    if not 4096 <= len(data) <= 8_000_000:
        raise ValueError("System image file size outside observation bound")

    def unpack(fmt, offset):
        return struct.unpack_from(fmt, data, offset)

    pe = unpack("<I", 0x3C)[0]
    assert data[:2] == b"MZ" and data[pe : pe + 4] == b"PE\0\0"
    assert unpack("<H", pe + 4)[0] == 0x8664
    count = unpack("<H", pe + 6)[0]
    optional_size = unpack("<H", pe + 20)[0]
    optional = pe + 24
    assert unpack("<H", optional)[0] == 0x20B and 1 <= count <= 32
    base = unpack("<Q", optional + 24)[0]
    sections = [unpack("<IIII", optional + optional_size + 40 * i + 8) for i in range(count)]
    executable_sections = [
        section
        for i, section in enumerate(sections)
        if unpack("<I", optional + optional_size + 40 * i + 36)[0] & 0x20000000
    ]

    def offset(rva, size=1):
        for virtual_size, address, raw_size, raw in sections:
            if address <= rva and rva + size <= address + min(virtual_size, raw_size):
                result = raw + rva - address
                assert result + size <= len(data)
                return result
        raise ValueError("RVA outside file-backed section")

    def cstring(rva):
        start = offset(rva)
        end = data.find(b"\0", start, start + 256)
        assert end >= start
        return data[start:end].decode("ascii")

    export_rva, export_size = unpack("<II", optional + 112)
    export = offset(export_rva, 40)
    functions, names, table, name_table, ordinals = unpack("<IIIII", export + 20)
    assert 0 < names <= functions <= 8192
    entries = {}
    for i in range(names):
        name = cstring(unpack("<I", offset(name_table + 4 * i, 4))[0])
        ordinal = unpack("<H", offset(ordinals + 2 * i, 2))[0]
        assert ordinal < functions
        rva = unpack("<I", offset(table + 4 * ordinal, 4))[0]
        entries[name] = rva
    imports = {}
    import_rva, import_size = unpack("<II", optional + 120)
    assert import_size <= 65536
    for i in range(import_size // 20):
        original, _, _, name, first = unpack("<IIIII", offset(import_rva + i * 20, 20))
        if not name:
            break
        dll = cstring(name)
        for index in range(8192):
            target = unpack("<Q", offset((original or first) + 8 * index, 8))[0]
            if not target:
                break
            symbol = f"ordinal:{target & 0xFFFF}" if target >> 63 else cstring(target + 2)
            imports[f"{base + first + 8 * index:016X}"] = dll + "!" + symbol
    entry = entries[entry_name]
    assert not export_rva <= entry < export_rva + export_size
    pdata, pdata_size = unpack("<II", optional + 112 + 3 * 8)
    assert pdata_size % 12 == 0 and pdata_size <= 1_000_000
    ranges = [unpack("<III", offset(pdata + i, 12))[:2] for i in range(0, pdata_size, 12)]

    def containing(rva):
        return next(((a, b) for a, b in ranges if a <= rva < b), None)

    installer = (
        Path(os.environ["ProgramFiles(x86)"])
        / "Microsoft Visual Studio"
        / "Installer"
        / "vswhere.exe"
    )
    tools = subprocess.check_output(
        [
            str(installer),
            "-latest",
            "-products",
            "*",
            "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-find",
            "VC/Tools/MSVC/*/bin/Hostx64/x64/dumpbin.exe",
        ],
        text=True,
        timeout=10,
    ).splitlines()
    tools = sorted(tools)
    assert 1 <= len(tools) <= 8 and all(Path(tool).is_file() for tool in tools)
    tool = tools[-1]
    queue = [(entry, 0)]
    entry_thunk = None
    if entry_name == "I_RpcFree" and containing(entry) is None:
        # Leaf export jump thunks need no unwind record. Admit only this exact
        # five-byte direct E9 form and a file-backed target with unwind bounds.
        thunk = data[offset(entry, 5) : offset(entry, 5) + 5]
        assert thunk[0] == 0xE9, "unsupported RPC free export thunk"
        target = entry + 5 + struct.unpack_from("<i", thunk, 1)[0]
        assert containing(target) is not None, "RPC free target lacks unwind bounds"
        entry_thunk = {
            "rva": entry,
            "target_rva": target,
            "bytes_hex": thunk.hex(),
            "sha256": hashlib.sha256(thunk).hexdigest(),
        }
        queue = [(target, 0)]
    access_check_callers = []
    descriptor_prepare_callers = []
    error_location_leads = []
    error_call_leads = []
    error_caller_leads = []
    if (image_name, entry_name) == ("rpcrt4.dll", "RpcServerRegisterIf3"):
        # Registration stores a descriptor that may be used later by dispatch,
        # outside its direct call graph. Seed only file-backed functions with
        # an x64 indirect CALL to this exact imported AccessCheck slot. This
        # byte pattern is a static lead, not proof of an executed instruction.
        slots = {
            int(address, 16) - base: symbol.rsplit("!", 1)[1]
            for address, symbol in imports.items()
            if symbol.endswith(("!AccessCheck", "!RtlNewSecurityObjectEx"))
        }
        assert list(slots.values()).count("AccessCheck") == 1
        for virtual_size, address, raw_size, raw in sections:
            end = raw + min(virtual_size, raw_size)
            begin = raw
            while (found := data.find(b"\xff\x15", begin, end)) >= 0:
                begin = found + 2
                if found + 6 > end:
                    continue
                site = address + found - raw
                target = site + 6 + struct.unpack_from("<i", data, found + 2)[0]
                bounds = containing(site)
                if target in slots and bounds is not None:
                    output = (
                        access_check_callers
                        if slots[target] == "AccessCheck"
                        else descriptor_prepare_callers
                    )
                    assert len(output) < 8, "fixed import callsite bound"
                    output.append({"call_rva": site, "function_rva": bounds[0]})
                    queue.append((bounds[0], 0))
    ss_sources = {"sspicli.dll", "sspisrv.dll", "rpcrt4.dll", "ntdll.dll"}
    max_functions, max_depth = (24, 3) if image_name in ss_sources | {"kernelbase.dll"} else (12, 2)
    if entry_name == "LsaConnectUntrusted":
        max_functions, max_depth = 32, 4
    if entry_name in {"I_RpcGetDefaultSD", "I_RpcFree"}:
        max_functions, max_depth = 8, 2
    if rpc_error_focus or rpc_endpoint_focus:
        assert (image_name, entry_name) == ("rpcrt4.dll", "RpcServerRegisterIf3")
        # The observed EEInfo row has location40. These immediate-byte patterns
        # only seed bounded disassembly: they are NOT instruction-boundary or
        # argument proofs. Review the call and its writer before associating it
        # with an error. Never query a live service or process for this capture.
        error_location_leads = fixed_rpc_error_leads(
            data, executable_sections, containing, (500, 950) if rpc_endpoint_focus else (40,)
        )
        queue = [(lead["function_rva"], 0) for lead in error_location_leads]
        max_functions, max_depth = 16, 1
        if rpc_endpoint_focus:
            # One generation of direct callers allows review of the numeric
            # arguments passed into a fixed error-writer wrapper. Byte matches
            # are leads only; disassembly must confirm instruction boundaries.
            targets = {lead["function_rva"] for lead in error_location_leads}
            for virtual_size, address, raw_size, raw in executable_sections:
                end = raw + min(virtual_size, raw_size)
                begin = raw
                while (found := data.find(b"\xe8", begin, end)) >= 0:
                    begin = found + 1
                    if found + 5 > end:
                        continue
                    site = address + found - raw
                    target = site + 5 + struct.unpack_from("<i", data, found + 1)[0]
                    bounds = containing(site)
                    if target in targets and bounds is not None:
                        assert len(error_caller_leads) < 8, "fixed RPC error caller bound"
                        error_caller_leads.append(
                            {
                                "call_rva": site,
                                "target_rva": target,
                                "function_rva": bounds[0],
                                "candidate_only": True,
                            }
                        )
                        queue.append((bounds[0], 0))
            max_functions = 24
    seen = set()
    observations = []
    while queue and len(observations) < max_functions:
        rva, depth = queue.pop(0)
        bounds = containing(rva)
        if bounds is None or bounds[0] in seen:
            continue
        start, end = bounds
        seen.add(start)
        if not 0 < end - start <= 16384:
            observations.append(
                {"rva": rva, "start": start, "end": end, "omitted": "function size bound"}
            )
            continue
        code = data[offset(start, end - start) : offset(start, end - start) + end - start]
        result = subprocess.run(
            [tool, "/disasm:nobytes", f"/range:{base + start:#x},{base + end - 1:#x}", str(image)],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert len(result.stdout) <= 300_000
        observations.append(
            {
                "rva": rva,
                "start": start,
                "end": end,
                "depth": depth,
                "code_sha256": hashlib.sha256(code).hexdigest(),
                "disassembly": result.stdout,
            }
        )
        if (rpc_error_focus or rpc_endpoint_focus) and depth == 0:
            # Prioritize the first direct call following each confirmed MOV
            # instruction so the writer itself does not fall beyond the normal
            # call-graph bound. This remains a lead, not argument dataflow proof.
            lines = re.findall(r"^\s*([0-9A-Fa-f]{16}):\s+(.+)$", result.stdout, re.MULTILINE)
            sites = {base + lead["pattern_rva"]: lead["location"] for lead in error_location_leads}
            for index, (address, instruction) in enumerate(lines):
                site = int(address, 16)
                if site not in sites or not re.fullmatch(
                    r"mov\s+r8[dw]," + format(sites[site], "X") + "h",
                    instruction.strip(),
                    re.IGNORECASE,
                ):
                    continue
                for call_address, instruction in lines[index + 1 : index + 13]:
                    if re.match(r"(?:j\w+|ret|int)\b", instruction):
                        break
                    if instruction.startswith("call"):
                        direct = re.fullmatch(r"call\s+([0-9A-Fa-f]{16})", instruction.strip())
                        if direct and containing(int(direct[1], 16) - base) is not None:
                            target = int(direct[1], 16) - base
                            error_call_leads.append(
                                {
                                    "call_rva": int(call_address, 16) - base,
                                    "target_rva": target,
                                    "candidate_only": True,
                                }
                            )
                            queue.insert(0, (target, 1))
                        break
        if depth < max_depth:
            # SSPI has split unwind regions. Follow bounded conditional targets
            # and actual adjacent fallthrough too; this is static reachability,
            # never evidence that any branch ran in the observed child.
            branches = (
                r"\b(?:call|j[a-z]+)\s+([0-9A-Fa-f]{8,16})\b"
                if image_name in ss_sources
                else r"\b(?:call|jmp)\s+([0-9A-Fa-f]{8,16})\b"
            )
            targets = [int(address, 16) - base for address in re.findall(branches, result.stdout)]
            if image_name == "rpcrt4.dll":
                # Registration can install an internal authorization callback.
                # Follow only explicit LEA references to exact function starts,
                # not indirect tables, runtime memory or arbitrary addresses.
                references = re.findall(r"\blea\s+\w+,\[([0-9A-Fa-f]{8,16})h\]", result.stdout)
                targets += [
                    int(address, 16) - base
                    for address in references
                    if (bounds := containing(int(address, 16) - base)) is not None
                    and bounds[0] == int(address, 16) - base
                ]
            if image_name in ss_sources:
                instructions = re.findall(
                    r"^\s*[0-9A-Fa-f]{16}:\s+(\w+)", result.stdout, re.MULTILINE
                )
                if instructions and instructions[-1].lower() not in {
                    "ret",
                    "jmp",
                    "int",
                    "int3",
                    "ud2",
                }:
                    targets.append(end)
            for target in targets:
                if containing(target) is not None and len(queue) < 64:
                    queue.append((target, depth + 1))
    assert observations or rpc_error_focus or rpc_endpoint_focus
    fixed_strings = {}
    public_constants = {
        "sspicli.dll": [
            r"\SECURITY\LSA_AUTHENTICATION_INITIALIZED",
            "ncalrpc",
            "lsasspirpc",
            "lpacIdentityServices",
        ],
        "sspisrv.dll": [
            r"\SECURITY\LSA_AUTHENTICATION_INITIALIZED",
            "ncalrpc",
            "lsasspirpc",
            "lpacIdentityServices",
        ],
        "kernelbase.dll": [
            r"\Device\NamedPipe%ws",
            "\\Device\\NamedPipe\\",
            r"\BaseNamedObjects",
            "RPC Control",
            "Global",
            "Local",
            "Session",
        ],
    }
    if image_name in public_constants:
        # Fixed public OS identifiers only, never arbitrary DLL text or runtime
        # process memory. RVAs let readers bind a referenced constant to bytes.
        for value in public_constants[image_name]:
            needle = (value + "\0").encode("utf-16-le")
            positions = []
            for _, address, raw_size, raw in sections:
                begin = raw
                while len(positions) < 8:
                    found = data.find(needle, begin, raw + raw_size)
                    if found < 0:
                        break
                    positions.append(address + found - raw)
                    begin = found + len(needle)
            fixed_strings[value] = positions
    return {
        "schema": 1,
        "diagnostic_only": True,
        "native_effects": False,
        "production_ready": False,
        "source_commit": os.environ.get("GITHUB_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "file": str(image),
        "file_sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "timestamp": unpack("<I", pe + 8)[0],
        "image_base": base,
        "entry_rva": entry,
        "entry_thunk": entry_thunk,
        "limits": {
            "max_functions": max_functions,
            "max_depth": max_depth,
            "max_function_bytes": 16384,
        },
        "functions": observations,
        "imports_by_preferred_address": imports,
        "dumpbin": tool,
        "unvisited_targets": len(queue),
        "fixed_utf16_strings": fixed_strings,
        "access_check_callers": access_check_callers,
        "descriptor_prepare_callers": descriptor_prepare_callers,
        "rpc_error_location_leads": error_location_leads,
        "rpc_error_call_leads": error_call_leads,
        "rpc_error_caller_leads": error_caller_leads,
        "rpc_error_focus": rpc_error_focus,
        "rpc_endpoint_focus": rpc_endpoint_focus,
        "rpc_interfaces": observe_interfaces(
            data, offset, base, observations, imports, image_name, entry_name
        ),
    }


if __name__ == "__main__":
    # Invoked only by the admitted CI wrapper; a local explicit invocation is
    # also read-only with respect to Windows. Output must be a fresh file.
    if len(sys.argv) == 3 and sys.argv[2] == "--schannel-discovery":
        observation = observe("sspicli.dll", "AcquireCredentialsHandleW")
        observation["package_query"] = observe("sspicli.dll", "QuerySecurityPackageInfoW")
        observation["lsa_connect"] = observe("sspicli.dll", "LsaConnectUntrusted")
        observation["lsa_server"] = observe("sspisrv.dll", "SspiSrvInitialize")
        observation["rpc_interface_comparison"] = compare_interfaces(
            observation["lsa_connect"]["rpc_interfaces"],
            observation["lsa_server"]["rpc_interfaces"],
        )
        observation["rpc_registration"] = observe("rpcrt4.dll", "RpcServerRegisterIf3")
        observation["rpc_error_location"] = observe(
            "rpcrt4.dll", "RpcServerRegisterIf3", rpc_error_focus=True
        )
        observation["rpc_default_descriptor"] = observe("rpcrt4.dll", "I_RpcGetDefaultSD")
        observation["rpc_free"] = observe("rpcrt4.dll", "I_RpcFree")
        observation["descriptor_construction"] = observe("ntdll.dll", "RtlCreateAndSetSD")
        observation["appcontainer_constructor"] = observe(
            "kernelbase.dll", "CreateAppContainerToken"
        )
    elif len(sys.argv) == 3 and sys.argv[2] == "--rpc-endpoint":
        observation = observe("rpcrt4.dll", "RpcServerRegisterIf3", rpc_endpoint_focus=True)
    elif len(sys.argv) == 3 and sys.argv[2] == "--desktop-resolution":
        observation = observe("user32.dll", "OpenWindowStationW")
        observation["win32kfull"] = observe("win32kfull.sys", "xxxResolveDesktop")
    else:
        assert len(sys.argv) == 2
        observation = observe()
        observation["gdi32full"] = observe("gdi32full.dll", "GdiDllInitialize")
        observation["kernelbase"] = observe("kernelbase.dll", "CreateAppContainerToken")
    result = json.dumps(observation, indent=2)
    assert len(result.encode()) <= 1_000_000
    with Path(sys.argv[1]).open("x", encoding="utf-8") as stream:
        stream.write(result + "\n")
