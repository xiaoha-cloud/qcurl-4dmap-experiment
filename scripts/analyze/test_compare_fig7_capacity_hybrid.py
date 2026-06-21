from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compare_fig7_capacity_hybrid as fig7_compare


def _write_profile(path: Path) -> None:
    path.write_text(
        "IFACE=h2-eth1\n\n"
        "0 20\n"
        "50 30\n"
        "100 10\n",
        encoding="utf-8",
    )


class Fig7CompareTests(unittest.TestCase):
    def test_analyze_fig7_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs_root = root / "logs_exp"
            session = logs_root / "session_fig7_capacity_hybrid_test"
            (session / "fig7_baseline").mkdir(parents=True)
            (session / "fig7_qaccess_t_dynamic" / "processed_buffers").mkdir(parents=True)
            validation_dir = logs_root / "validation_logs"
            validation_dir.mkdir()
            profile = root / "bw_profile.env"
            _write_profile(profile)

            seconds = list(range(220))
            def make_series(phase1: float, high: float, low: float) -> pd.DataFrame:
                values = []
                for s in seconds:
                    if s < 50:
                        values.append(phase1)
                    elif s < 100:
                        values.append(high)
                    else:
                        values.append(low)
                return pd.DataFrame({"elapsed_s": seconds, "throughput_mbps": values})

            baseline_total = make_series(10.0, 12.0, 8.0)
            dynamic_total = make_series(10.5, 14.0, 9.0)
            baseline_a = make_series(5.0, 5.0, 5.0)
            dynamic_a = make_series(5.5, 6.0, 6.0)
            baseline_b = make_series(5.0, 7.0, 3.0)
            dynamic_b = make_series(5.0, 8.0, 3.0)
            for arm in ("fig7_baseline", "fig7_qaccess_t_dynamic"):
                (session / arm).mkdir(exist_ok=True)
            baseline_total.to_csv(session / "fig7_baseline" / "throughput_all_down.csv", index=False)
            dynamic_total.to_csv(session / "fig7_qaccess_t_dynamic" / "throughput_all_down.csv", index=False)
            baseline_a.to_csv(session / "fig7_baseline" / "throughput_pathA_down.csv", index=False)
            dynamic_a.to_csv(session / "fig7_qaccess_t_dynamic" / "throughput_pathA_down.csv", index=False)
            baseline_b.to_csv(session / "fig7_baseline" / "throughput_pathB_down.csv", index=False)
            dynamic_b.to_csv(session / "fig7_qaccess_t_dynamic" / "throughput_pathB_down.csv", index=False)

            (session / "experiment_metadata.json").write_text(json.dumps({
                "bw_profile": str(profile),
                "timeout": 220,
                "gate_mode": "hybrid",
                "execution_mode": "active",
                "min_relative_gain": 0.03,
                "min_delta_gain_bps": 100000,
            }), encoding="utf-8")
            (session / "worker.log").write_text(
                json.dumps({
                    "request_id": "run_1",
                    "request_classification": "DURING_DETERIORATION",
                    "status": "APPLIED_AGGREGATE",
                    "actual_applied": True,
                }) + "\n",
                encoding="utf-8",
            )
            (session / "qaccess_trigger_audit.jsonl").write_text(
                json.dumps({"request_id": "run_1", "trigger_decision": "request_written"}) + "\n",
                encoding="utf-8",
            )
            (session / "fig7_qaccess_t_dynamic" / "processed_buffers" / "qaccess_path_eligibility_run_1.json").write_text(
                json.dumps([
                    {"physical_path": "Path B", "eligible": True, "exclusion_reason": "", "sender_byte_delta": 1000},
                    {"physical_path": "Path B", "eligible": False, "exclusion_reason": "no_sender_byte_growth", "sender_byte_delta": 0},
                ]),
                encoding="utf-8",
            )
            (validation_dir / f"{session.name}_active_validate.log").write_text(
                "PASS controller coefficient reload confirmed\n",
                encoding="utf-8",
            )

            report = fig7_compare.analyze(session)

            self.assertEqual(report["gate_mode"], "hybrid")
            self.assertEqual(report["verdict"], "dynamic_better")
            self.assertEqual(report["applied_update_count"], 1)
            self.assertEqual(report["applied_request_phases"], ["DURING_DETERIORATION"])
            self.assertTrue(report["request_serial_continuity"])
            self.assertEqual(report["request_write_failed"], 0)
            self.assertTrue(report["controller_reload_confirmed"])
            self.assertIn("traffic shifting away from the constrained path", report["path_b_migration_notes"])

            markdown = fig7_compare.render_markdown(report)
            self.assertIn("HIGH_CAPACITY_60_100", markdown)
            self.assertIn("dynamic_better", markdown)


if __name__ == "__main__":
    unittest.main()
