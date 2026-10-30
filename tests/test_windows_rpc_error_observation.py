"""Bounded file-byte leads only; no Windows API, service or sandbox effects."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from windows_user32_init_observation import fixed_rpc_error_leads


class RpcErrorLeadTests(unittest.TestCase):
    def test_endpoint_focus_cannot_select_arbitrary_location_or_mix_prior_focus(self):
        data = bytes.fromhex("41b82800000041b8f401000041b8b6030000")
        section = [(len(data), 0x100, len(data), 0)]

        def bound(r):
            return r, r + 6

        baseline = fixed_rpc_error_leads(data, section, bound)
        endpoint = fixed_rpc_error_leads(data, section, bound, (500, 950))
        self.assertEqual([r["location"] for r in baseline], [40])
        self.assertEqual([r["location"] for r in endpoint], [500, 950])
        self.assertEqual([r["pattern_rva"] for r in endpoint], [0x106, 0x10C])
        with self.assertRaises(AssertionError):
            fixed_rpc_error_leads(data, section, bound, (40, 500, 950))
        with self.assertRaises(AssertionError):
            fixed_rpc_error_leads(data, section, bound, (999,))

    def test_only_file_backed_selected_sections_and_unwind_ranges(self):
        pattern = bytes.fromhex("41b828000000")
        data = pattern * 3
        leads = fixed_rpc_error_leads(data, [(6, 0x100, 6, 6)], lambda r: (0x100, 0x106))
        self.assertEqual([r["pattern_rva"] for r in leads], [0x100])
        self.assertTrue(leads[0]["candidate_only"])
        self.assertEqual(fixed_rpc_error_leads(data, [(6, 0x100, 6, 6)], lambda r: None), [])
        self.assertEqual(
            fixed_rpc_error_leads(data, [(5, 0x100, 6, 6)], lambda r: (0x100, 0x106)), []
        )

    def test_leads_cannot_escape_file_or_exceed_bound(self):
        data = bytes.fromhex("41b828000000") * 9
        with self.assertRaisesRegex(ValueError, "lead bound"):
            fixed_rpc_error_leads(data, [(len(data), 0x100, len(data), 0)], lambda r: (r, r + 6))
        with self.assertRaisesRegex(ValueError, "outside file"):
            fixed_rpc_error_leads(
                data, [(len(data) + 1, 0x100, len(data) + 1, 0)], lambda r: (r, r + 6)
            )


if __name__ == "__main__":
    unittest.main()
