"""Synthetic file/assembly regressions; no Windows APIs or fixtures executed."""

import importlib.util
import struct
import unittest
import uuid
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "rpc_observation", Path(__file__).parents[1] / "scripts/windows_rpc_interface_observation.py"
)
rpc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rpc)
BASE = 0x180000000


class RpcInterfaceObservationTests(unittest.TestCase):
    def assembly(self, middle="", branch=""):
        return [
            dict(
                start=0x100,
                end=0x180,
                disassembly=f"""
  {BASE + 0x100:016X}: {branch or "nop"}
  {BASE + 0x110:016X}: xor         edx,edx
  {BASE + 0x112:016X}: lea         rcx,[{BASE + 0x200:016X}h]
  {BASE + 0x120:016X}: {middle or "nop"}
  {BASE + 0x130:016X}: call        qword ptr [{BASE + 0x400:016X}h]
""",
            )
        ]

    def fixture(self, client=True):
        data = bytearray(1024)
        struct.pack_into("<Q", data, 0x200, BASE + 0x220)
        struct.pack_into("<Q", data, 0x220, BASE + 0x280)
        record = 0x280 if client else 0x200
        struct.pack_into("<I", data, record, 96)
        data[record + 4 : record + 20] = uuid.UUID("12345678-1234-abcd-ef00-0123456789ab").bytes_le
        struct.pack_into("<HH", data, record + 20, 1, 0)
        data[record + 24 : record + 40] = uuid.UUID("8a885d04-1ceb-11c9-9fe8-08002b104860").bytes_le
        struct.pack_into("<HH", data, record + 40, 2, 0)
        return data

    def observe(self, data, client=True, functions=None):
        def offset(rva, size):
            if not 0 <= rva <= len(data) - size:
                raise ValueError("not file backed")
            return rva

        symbol = "NdrClientCall3" if client else "RpcServerRegisterIf3"
        return rpc.observe_interfaces(
            data,
            offset,
            BASE,
            functions or self.assembly(),
            {f"{BASE + 0x400:016X}": "rpcrt4.dll!" + symbol},
            "sspicli.dll" if client else "sspisrv.dll",
            "LsaConnectUntrusted" if client else "SspiSrvInitialize",
        )

    def test_client_two_hops_and_server_direct_identity_match(self):
        c, s = self.observe(self.fixture()), self.observe(self.fixture(False), False)
        self.assertEqual(c["calls"][0]["stub_descriptor_rva"], 0x220)
        self.assertEqual(c["calls"][0]["interface_rva"], 0x280)
        compared = rpc.compare_interfaces(c, s)
        self.assertTrue(compared["complete"])
        self.assertEqual(len(compared["matches"]), 1)
        self.assertFalse(compared["live_descriptor_verified"])

    def test_interface_version_mismatch_is_not_a_match(self):
        data = self.fixture(False)
        struct.pack_into("<H", data, 0x214, 2)
        compared = rpc.compare_interfaces(self.observe(self.fixture()), self.observe(data, False))
        self.assertTrue(compared["complete"])
        self.assertEqual(compared["matches"], [])

    def test_bad_pointer_null_external_unaligned_and_truncated_are_unresolved(self):
        for value in [0, BASE + 0x10000000, BASE + 0x221, BASE + 1020]:
            data = self.fixture()
            struct.pack_into("<Q", data, 0x200, value)
            self.assertFalse(self.observe(data)["calls"][0]["resolved"])

    def test_wrong_structure_length_is_unresolved(self):
        data = self.fixture()
        struct.pack_into("<I", data, 0x280, 64)
        self.assertFalse(self.observe(data)["calls"][0]["resolved"])

    def test_rcx_clobber_calls_and_ambiguous_branches_refuse_reference(self):
        for middle in [
            "xor ecx,ecx",
            "call 0000000180000800",
            "jne 0000000180000170",
            "xchg rax,rcx",
            "xadd rax,rcx",
            "mul rax",
            "cpuid",
            "loop 0000000180000130",
            "rep movsb",
        ]:
            value = self.observe(self.fixture(), functions=self.assembly(middle))
            self.assertFalse(value["calls"][0]["resolved"])
        value = self.observe(self.fixture(), functions=self.assembly(branch="je 0000000180000120"))
        self.assertFalse(value["calls"][0]["resolved"])

    def test_branch_to_rcx_assignment_cannot_establish_earlier_procedure_zero(self):
        value = self.observe(self.fixture(), functions=self.assembly(branch="je 0000000180000112"))
        self.assertTrue(value["calls"][0]["resolved"])
        self.assertIsNone(value["calls"][0]["procedure"])
        self.assertFalse(
            rpc.compare_interfaces(value, self.observe(self.fixture(False), False))["complete"]
        )
        self.assertEqual(
            rpc.compare_interfaces(value, self.observe(self.fixture(False), False))["matches"], []
        )

    def test_no_evidence_is_incomplete_not_proof_of_mismatch(self):
        self.assertFalse(rpc.compare_interfaces(None, None)["complete"])
        data = self.observe(self.fixture(), functions=self.assembly("xor ecx,ecx"))
        self.assertFalse(
            rpc.compare_interfaces(data, self.observe(self.fixture(False), False))["complete"]
        )


if __name__ == "__main__":
    unittest.main()
