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
        self.assertIn('QACCESS_RUNTIME_SAMPLE_INTERVAL_MS="$SAMPLE_INTERVAL_MS"', runner)
        self.assertIn("QACCESS_RETAIN_TC_LOG=1", runner)
        self.assertIn("local -a log_args=(--disable-logs)", runner)
        self.assertIn("QACCESS_INTERVENTION_VERBOSE_LOGS", runner)

    def test_fixed_sweep_wrapper_selects_clean_d_without_replacing_smoke(self):
        wrapper = (Path(__file__).resolve().parent / "run_qaccess_d_clean_fixed_sweep_collect.sh").read_text()
        self.assertIn("--controller-variant qaccess_d", wrapper)
        self.assertIn("--profile-kind delay_clean", wrapper)
        self.assertIn("--scenario clean_equal_paths", wrapper)
        self.assertIn("run_qaccess_qserver_sender_sweep.py", wrapper)
        collector = (Path(__file__).resolve().parent / "run_qaccess_qserver_sender_sweep.py").read_text()
        self.assertIn('"TC_DELAY_FIXED_BW_MBIT": "20"', collector)
        self.assertIn('"TC_DELAY_FIXED_LOSS_PERCENT": "0"', collector)
        self.assertIn("validate_clean_delay_tc_log(run_dir)", collector)


if __name__ == "__main__":
    unittest.main()
