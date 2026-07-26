#!/usr/bin/env python3

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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
        absolute = result.set_index("metric")["improvement_absolute"]
        self.assertAlmostEqual(absolute["owd_ms_mean"], 20.0)
        self.assertAlmostEqual(
            absolute["secondary_total_quic_wire_mbps_mean"], 2.0,
        )
        share = result[result.metric == "path_b_share_pct_mean"].iloc[0]
        self.assertAlmostEqual(share["change_percentage_points"], -10.0)

    def test_loss_timeseries_selects_runtime_path_b(self):
        samples = pd.DataFrame([
            {
                "path_id": 1, "remote_endpoint": "10.0.1.1:5000",
                "time_s": 1.2, "loss_rate": 0.0,
            },
            {
                "path_id": 3, "remote_endpoint": "10.0.2.1:5001",
                "time_s": 1.2, "loss_rate": 0.05,
            },
        ])
        result = evaluation._per_second_loss(
            pd.DataFrame(), pd.DataFrame(), samples, "loss_qaccess_l",
        )
        self.assertEqual(result["method"].tolist(), ["loss_qaccess_l"])
        self.assertAlmostEqual(result["sample_loss_rate_mean"].iloc[0], 0.05)

    def test_bandwidth_timeseries_selects_runtime_path_b_and_converts_to_mbps(self):
        samples = pd.DataFrame([
            {"path_id": 1, "remote_endpoint": "10.0.1.1:5000", "time_s": 1.2,
             "bw_bps": 30_000_000},
            {"path_id": 3, "remote_endpoint": "10.0.2.1:5001", "time_s": 1.2,
             "bw_bps": 10_000_000},
        ])
        result = evaluation._per_second_bandwidth(samples, "qaccess_t")
        self.assertEqual(result["method"].tolist(), ["qaccess_t"])
        self.assertAlmostEqual(result["bw_mbps_mean"].iloc[0], 10.0)

    def test_delay_plot_falls_back_to_rtt_when_owd_missing(self):
        throughput = pd.DataFrame([
            {
                "method": "baseline",
                "time_s": 1,
                "total_quic_wire_mbps": 10.0,
                "path_a_quic_wire_mbps": 9.0,
                "path_b_quic_wire_mbps": 1.0,
            }
        ])
        quality = pd.DataFrame([
            {
                "method": "baseline",
                "time_s": 1,
                "owd_ms_mean": float("nan"),
                "rtt_ms_mean": 42.0,
            }
        ])

        with TemporaryDirectory() as td, patch("matplotlib.axes.Axes.text") as text:
            evaluation._plot_timeseries(throughput, quality, Path(td), "delay")

        text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
