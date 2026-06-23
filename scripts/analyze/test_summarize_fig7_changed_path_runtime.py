from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import summarize_fig7_changed_path_runtime as changed_summary


class ChangedPathRuntimeSummaryTests(unittest.TestCase):
    def test_runtime_summary_flags_only_during_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            processed = root / "logs_exp" / "session_fig7_capacity_hybrid_test" / "fig7_qaccess_t_dynamic" / "processed_buffers"
            processed.mkdir(parents=True)

            fixtures = [
                ("req1", "PRE_DETERIORATION", False, 2000.0, 0.0, {"alpha": 0.8, "beta": 0.3, "gamma": 0.1}, ["aggregate_blocks_below_threshold"]),
                ("req2", "DURING_DETERIORATION", True, 95000.0, 93000.0, {"alpha": 0.7, "beta": 0.3, "gamma": 0.1}, ["aggregate_blocks_due_to_cross_path_tradeoff", "changed_path_priority_would_apply"]),
                ("req3", "POST_DETERIORATION", False, 30000.0, 60000.0, {"alpha": 0.6, "beta": 0.3, "gamma": 0.1}, ["changed_path_priority_blocks_no_changed_path_gain"]),
            ]
            for rid, phase, would_apply, agg_gain, other_loss, coeffs, diagnoses in fixtures:
                payload = {
                    "request_id": rid,
                    "phase_classification": phase,
                    "aggregate_decision": {
                        "aggregate_gain_bps": agg_gain,
                        "aggregate_would_apply": False,
                    },
                    "changed_path_priority_decision": {
                        "coefficients": coeffs,
                        "changed_path_weighted_gain_bps": 123456.0 if would_apply else 1000.0,
                        "other_path_loss_bps": other_loss,
                        "fig7_changed_path_would_apply": would_apply,
                    },
                    "diagnoses": diagnoses,
                }
                (processed / f"qaccess_changed_path_priority_{rid}.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )

            report = changed_summary.analyze(processed.parents[1])
            self.assertEqual(report["request_count"], 3)
            self.assertTrue(report["summary"]["pre_requests_blocked"])
            self.assertTrue(report["summary"]["during_any_would_apply"])
            self.assertTrue(report["summary"]["post_requests_blocked"])
            markdown = changed_summary.render_markdown(report)
            self.assertIn("DURING request would apply: `True`", markdown)


if __name__ == "__main__":
    unittest.main()
