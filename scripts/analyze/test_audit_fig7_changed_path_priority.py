from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_fig7_changed_path_priority as fig7_changed


class Fig7ChangedPathPriorityAuditTests(unittest.TestCase):
    def test_during_request_would_apply_under_changed_path_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "logs_exp" / "session_fig7_capacity_hybrid_test"
            processed = session / "fig7_qaccess_t_dynamic" / "processed_buffers"
            processed.mkdir(parents=True)

            (session / "experiment_metadata.json").write_text(json.dumps({
                "execution_mode": "active",
                "gate_mode": "hybrid",
                "min_relative_gain": 0.03,
                "min_delta_gain_bps": 100000,
            }), encoding="utf-8")

            request_id = "run_3"
            (session / "worker.log").write_text(json.dumps({
                "request_id": request_id,
                "request_classification": "DURING_DETERIORATION",
                "current_coefficients": {"alpha": 0.6, "beta": 0.3, "gamma": 0.1},
                "traffic_weighted_proposed_candidate": {"alpha": 0.8, "beta": 0.3, "gamma": 0.1},
                "traffic_weighted_proposed_stepped_coefficients": {"alpha": 0.7, "beta": 0.3, "gamma": 0.1},
                "traffic_weighted_gain": 95428.5604303414,
                "equal_weight_gain": 87129.05922701489,
                "relative_gain": 0.0284344337509647,
                "would_apply_under_gate": False,
                "actual_applied": False,
                "path_weights": {"1": 0.48178630763882996, "3": 0.51821369236117},
            }) + "\n", encoding="utf-8")

            aggregate = pd.DataFrame([
                {
                    "request_id": request_id, "alpha": 0.6, "beta": 0.3, "gamma": 0.1,
                    "equal_weight_score": 3495542, "byte_weighted_score": 3356091,
                    "equal_weight_gain": 0.0, "byte_weighted_gain": 0.0,
                    "is_current_tuple": 1, "eligible_path_count": 2, "eligible_path_ids": "1,3",
                    "equal_weight_rank": 3, "byte_weighted_rank": 3,
                },
                {
                    "request_id": request_id, "alpha": 0.7, "beta": 0.3, "gamma": 0.1,
                    "equal_weight_score": 3582494, "byte_weighted_score": 3449601,
                    "equal_weight_gain": 86952.168636, "byte_weighted_gain": 93509.217553,
                    "is_current_tuple": 0, "eligible_path_count": 2, "eligible_path_ids": "1,3",
                    "equal_weight_rank": 2, "byte_weighted_rank": 2,
                },
                {
                    "request_id": request_id, "alpha": 0.8, "beta": 0.3, "gamma": 0.1,
                    "equal_weight_score": 3582671, "byte_weighted_score": 3451520,
                    "equal_weight_gain": 87129.059227, "byte_weighted_gain": 95428.560430,
                    "is_current_tuple": 0, "eligible_path_count": 2, "eligible_path_ids": "1,3",
                    "equal_weight_rank": 1, "byte_weighted_rank": 1,
                },
            ])
            aggregate.to_csv(processed / f"qaccess_candidate_scores_{request_id}_aggregate.csv", index=False)

            per_path = pd.DataFrame([
                {"path_id": 1, "alpha": 0.6, "beta": 0.3, "gamma": 0.1, "path_pred_candidate": 7323719, "is_current_tuple": 1},
                {"path_id": 1, "alpha": 0.7, "beta": 0.3, "gamma": 0.1, "path_pred_candidate": 7230668, "is_current_tuple": 0},
                {"path_id": 1, "alpha": 0.8, "beta": 0.3, "gamma": 0.1, "path_pred_candidate": 7183011, "is_current_tuple": 0},
                {"path_id": 3, "alpha": 0.6, "beta": 0.3, "gamma": 0.1, "path_pred_candidate": -332635.035492, "is_current_tuple": 1},
                {"path_id": 3, "alpha": 0.7, "beta": 0.3, "gamma": 0.1, "path_pred_candidate": -65679.581769, "is_current_tuple": 0},
                {"path_id": 3, "alpha": 0.8, "beta": 0.3, "gamma": 0.1, "path_pred_candidate": -17669.109397, "is_current_tuple": 0},
            ])
            per_path.to_csv(processed / f"qaccess_candidate_scores_{request_id}_per_path.csv", index=False)

            (processed / f"qaccess_path_eligibility_{request_id}.json").write_text(json.dumps([
                {"path_id": 1, "physical_path": "Path A", "eligible": True, "sender_byte_delta": 352048, "exclusion_reason": ""},
                {"path_id": 3, "physical_path": "Path B", "eligible": True, "sender_byte_delta": 378666, "exclusion_reason": ""},
            ]), encoding="utf-8")

            report = fig7_changed.analyze(session=session, changed_path_ids={3})
            self.assertEqual(len(report["requests"]), 1)
            request = report["requests"][0]
            self.assertIn("aggregate_blocks_due_to_cross_path_tradeoff", request["diagnoses"])
            self.assertIn("changed_path_priority_would_apply", request["diagnoses"])
            self.assertIn("step_limit_not_decisive", request["diagnoses"])
            best = request["changed_path_priority_best_candidate"]
            self.assertTrue(best["fig7_changed_path_would_apply"])
            self.assertEqual(best["coefficients"], {"alpha": 0.7, "beta": 0.3, "gamma": 0.1})
            self.assertGreater(best["changed_path_raw_gain_bps"], 250000)
            self.assertGreater(best["changed_path_weighted_gain_bps"], 100000)
            self.assertIn("capacity-change-specific objective", request["recommendation"])

            markdown = fig7_changed.render_markdown(report)
            self.assertIn("changed_path_priority_would_apply", markdown)
            self.assertIn("Other Path Loss", markdown)


if __name__ == "__main__":
    unittest.main()
