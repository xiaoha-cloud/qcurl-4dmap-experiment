#!/usr/bin/env python3

import unittest

import pandas as pd

import qaccess_delay_loss_eval_analyze as evaluation


class DelayLossEvaluationTest(unittest.TestCase):
    def test_runtime_path_b_uses_endpoint_mapping(self):
        frame = pd.DataFrame([
            {"path_id": 1, "remote_endpoint": "10.0.1.1:5000"},
            {"path_id": 3, "remote_endpoint": "10.0.2.1:5001"},
        ])
        selected = evaluation._path_b_rows(frame, path_col="path_id")
        self.assertEqual(selected["path_id"].tolist(), [3])

    def test_pull_log_path_b_uses_path_two(self):
        frame = pd.DataFrame([{"path": 1}, {"path": 2}])
        selected = evaluation._path_b_rows(frame)
        self.assertEqual(selected["path"].tolist(), [2])

    def test_improvement_sign_depends_on_metric_direction(self):
        frame = pd.DataFrame([
            {
                "method": "baseline",
                "window": "90-150",
                "owd_ms_mean": 80.0,
                "secondary_total_quic_wire_mbps_mean": 10.0,
                "path_b_share_pct_mean": 60.0,
            },
            {
                "method": "delay_qaccess_d",
                "window": "90-150",
                "owd_ms_mean": 60.0,
                "secondary_total_quic_wire_mbps_mean": 12.0,
                "path_b_share_pct_mean": 50.0,
            },
        ])
        result = evaluation.build_improvement_table(frame, "delay_qaccess_d")
        improvements = result.set_index("metric")["improvement_pct"]
        self.assertAlmostEqual(improvements["owd_ms_mean"], 25.0)
        self.assertAlmostEqual(
            improvements["secondary_total_quic_wire_mbps_mean"], 20.0,
        )
        share = result[result.metric == "path_b_share_pct_mean"].iloc[0]
        self.assertAlmostEqual(share["change_percentage_points"], -10.0)


if __name__ == "__main__":
    unittest.main()
