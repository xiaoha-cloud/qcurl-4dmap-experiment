#!/usr/bin/env python3

import tempfile
import unittest
from pathlib import Path

import finalize_control_law_leg as finalizer


class FinalizeLogRetentionTest(unittest.TestCase):
    def _leg(self) -> Path:
        leg = Path(tempfile.mkdtemp())
        (leg / "logs").mkdir()
        (leg / "logs" / "pull_test.log").write_text("metric\n", encoding="utf-8")
        (leg / "csv").mkdir()
        (leg / "csv" / "temporary.csv").write_text("x\n", encoding="utf-8")
        return leg

    def test_verbose_logs_are_retained(self):
        leg = self._leg()
        finalizer._cleanup_leg_dir(
            leg,
            throughput_ok=True,
            keep_pcap=False,
            save_flv=False,
            keep_logs=True,
        )
        self.assertTrue((leg / "logs" / "pull_test.log").is_file())
        self.assertFalse((leg / "csv").exists())

    def test_logs_are_removed_by_default(self):
        leg = self._leg()
        finalizer._cleanup_leg_dir(
            leg,
            throughput_ok=True,
            keep_pcap=False,
            save_flv=False,
            keep_logs=False,
        )
        self.assertFalse((leg / "logs").exists())


if __name__ == "__main__":
    unittest.main()
