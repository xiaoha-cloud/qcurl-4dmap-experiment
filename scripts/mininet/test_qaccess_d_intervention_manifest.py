import unittest
from pathlib import Path

from generate_qaccess_d_intervention_manifest import GRID, build_rows


class InterventionManifestTests(unittest.TestCase):
    def test_manifest_is_deterministic_balanced_and_randomized(self):
        first = build_rows(20260730, 5)
        second = build_rows(20260730, 5)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 135)
        for candidate in {row["candidate_id"] for row in first}:
            rows = [row for row in first if row["candidate_id"] == candidate]
            self.assertEqual({row["replicate"] for row in rows}, set(range(1, 6)))
        ordered = [
            f"a{a:.1f}_b{b:.1f}_g{g:.1f}"
            for _ in range(5)
            for a, b, g in GRID
        ]
        self.assertNotEqual([row["candidate_id"] for row in first], ordered)
        self.assertEqual({row["intervention_s"] for row in first}, {65, 70, 75})

    def test_collector_routes_clean_profile_to_delay_parser(self):
        runner = (Path(__file__).resolve().parent / "run_qaccess_d_clean_intervention_collect.sh").read_text()
        self.assertIn('--dynamic-delay-profile "$PROFILE"', runner)
        self.assertNotIn('--dynamic-deterioration-profile "$PROFILE"', runner)
        self.assertIn("QACCESS_RUNTIME_BUFFER_SIZE=0", runner)


if __name__ == "__main__":
    unittest.main()
