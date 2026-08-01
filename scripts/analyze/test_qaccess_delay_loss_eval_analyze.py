#!/usr/bin/env python3

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

import qaccess_delay_loss_eval_analyze as evaluation
from parse_logs import load_pull_log


class DelayLossEvaluationTest(unittest.TestCase):
    def test_publication_method_labels(self):
        self.assertEqual(evaluation._plot_method_label("baseline", "delay"), "Baseline")
        self.assertEqual(
            evaluation._plot_method_label("clean_delay_qaccess_d", "delay"),
            "Q-Access-D",
        )

    def test_timeseries_plot_uses_six_distinct_line_colors(self):
        method_colors, path_colors = evaluation._timeseries_color_maps(
            ["qaccess_t", "baseline"],
        )
        self.assertEqual(set(method_colors), {"baseline", "qaccess_t"})
        self.assertEqual(
            set(path_colors),
            {
                ("baseline", "A"), ("baseline", "B"),
                ("qaccess_t", "A"), ("qaccess_t", "B"),
            },
        )
        all_colors = list(method_colors.values()) + list(path_colors.values())
        self.assertEqual(len(all_colors), 6)
        self.assertEqual(len(set(all_colors)), 6)

    def test_runtime_path_b_uses_endpoint_mapping(self):
        frame = pd.DataFrame([
            {"path_id": 1, "remote_endpoint": "10.0.1.1:5000"},
            {"path_id": 3, "remote_endpoint": "10.0.2.1:5001"},
        ])
        selected = evaluation._path_b_rows(frame, path_col="path_id")
        self.assertEqual(selected["path_id"].tolist(), [3])

    def test_current_pull_log_path_b_uses_path_three(self):
        frame = pd.DataFrame([{"path": 1}, {"path": 3}])
        selected = evaluation._path_b_rows(frame)
        self.assertEqual(selected["path"].tolist(), [3])

    def test_legacy_pull_log_path_b_falls_back_to_path_two(self):
        frame = pd.DataFrame([{"path": 1}, {"path": 2}])
        selected = evaluation._path_b_rows(frame)
        self.assertEqual(selected["path"].tolist(), [2])

    def test_metric_source_uses_pull_and_never_combined_process_log(self):
        with TemporaryDirectory() as td:
            run = Path(td)
            logs = run / "logs"
            logs.mkdir()
            pull = logs / "pull_test.log"
            server = logs / "server_test.log"
            combined = logs / "combined_test.log"
            for path in (pull, server, combined):
                path.write_text("test\n", encoding="utf-8")
            self.assertEqual(evaluation._metric_log_candidates(run), [pull])

    def test_wire_timeseries_falls_back_to_retained_throughput_csvs(self):
        with TemporaryDirectory() as td:
            run = Path(td)
            for filename, values in (
                ("throughput_all_down.csv", [10.0, 12.0]),
                ("throughput_pathA_down.csv", [8.0, 9.0]),
                ("throughput_pathB_down.csv", [2.0, 3.0]),
            ):
                pd.DataFrame({
                    "elapsed_s": [0.0, 1.0],
                    "throughput_mbps": values,
                    "bytes": [0, 0],
                    "packets": [0, 0],
                }).to_csv(run / filename, index=False)

            result = evaluation.load_wire_timeseries(run)

        self.assertEqual(result["time_s"].tolist(), [0.0, 1.0])
        self.assertEqual(result["total_quic_wire_mbps"].tolist(), [10.0, 12.0])
        self.assertEqual(result["path_a_quic_wire_mbps"].tolist(), [8.0, 9.0])
        self.assertEqual(result["path_b_quic_wire_mbps"].tolist(), [2.0, 3.0])
        self.assertEqual(result["path_b_share_pct"].tolist(), [20.0, 25.0])

    def test_timeseries_plot_does_not_crash_without_throughput(self):
        quality = pd.DataFrame([{
            "method": "baseline",
            "time_s": 1.0,
            "monitor_loss_mean": 0.01,
        }])
        with TemporaryDirectory() as td:
            out = Path(td)
            evaluation._plot_timeseries(
                pd.DataFrame(), quality, out, "loss_clean", "loss",
            )
            self.assertTrue(
                (out / "loss_clean_throughput_quality_over_time.png").is_file()
            )

    def test_monitor_time_is_aligned_to_tc_profile_start(self):
        monitor_line = (
            "2026/07/26 15:00:04 [m]monitor path=3 "
            "rtt_smoothed=160ms rtt_min=80ms rtt_latest=175ms "
            "rtt_mean_dev=12ms owd=80ms bw=1B/s inflight=0B "
            "cwnd_full=1B cwnd_room=1B loss=0 lost_B=0 serverinx=0\n"
        )
        tc_line = (
            "[2026-07-26T15:00:00+00:00] [tc_delay] "
            "step 1/3 at=0s delay=40ms dev=h2-eth1\n"
        )
        with TemporaryDirectory() as td:
            run = Path(td)
            logs = run / "logs"
            logs.mkdir()
            (logs / "pull_test.log").write_text(monitor_line, encoding="utf-8")
            (logs / "tc_delay_test.log").write_text(tc_line, encoding="utf-8")
            _, monitor = evaluation.load_pull_frames(run)
        self.assertEqual(monitor["t"].tolist(), [4])

    def test_fixed_trailing_median_does_not_forward_fill_sparse_seconds(self):
        raw = pd.DataFrame([
            {"t": 0.0, "rtt_latest_ms": 10.0},
            {"t": 2.0, "rtt_latest_ms": 30.0},
            {"t": 4.0, "rtt_latest_ms": 50.0},
        ])
        result = evaluation._rtt_per_second(raw).set_index("time_s")
        self.assertTrue(pd.isna(result.loc[1, "rtt_1s_median_ms"]))
        self.assertTrue(pd.isna(result.loc[1, "rtt_3s_trailing_median_ms"]))
        self.assertEqual(result.loc[2, "rtt_3s_trailing_median_ms"], 20.0)
        self.assertEqual(result.loc[4, "rtt_3s_trailing_median_ms"], 40.0)

    def test_rtt_audit_windows_cover_required_transitions(self):
        self.assertEqual(
            [name for name, _, _ in evaluation.RTT_AUDIT_WINDOWS],
            ["0-50", "30-50", "50-60", "60-100", "80-100",
             "100-110", "110-200", "130-160"],
        )

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

    def test_delay_plot_can_explicitly_render_owd_proxy_without_replacing_rtt_plot(self):
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
                "owd_ms_mean": 80.0,
                "rtt_latest_ms_mean": 175.0,
            }
        ])

        with TemporaryDirectory() as td:
            out = Path(td)
            evaluation._plot_timeseries(
                throughput,
                quality,
                out,
                "delay_clean",
                "delay",
            )
            evaluation._plot_timeseries(
                throughput,
                quality,
                out,
                "delay_clean",
                "delay",
                quality_metric="owd_ms_mean",
                output_name="delay_clean_throughput_owd_proxy_over_time.png",
            )
            self.assertTrue((out / "delay_clean_throughput_quality_over_time.png").is_file())
            self.assertTrue((out / "delay_clean_throughput_owd_proxy_over_time.png").is_file())

    def test_monitor_parser_and_delay_series_preserve_raw_latest_rtt(self):
        line = (
            "2026/07/26 15:00:00 [m]monitor path=2 "
            "rtt_smoothed=160ms rtt_min=80ms rtt_latest=175ms "
            "rtt_mean_dev=12ms owd=80ms bw=1250000B/s inflight=0B "
            "cwnd_full=46720B cwnd_room=46720B loss=0 lost_B=0 serverinx=0\n"
        )
        with TemporaryDirectory() as td:
            log = Path(td) / "pull.log"
            log.write_text(line, encoding="utf-8")
            _, monitor = load_pull_log(log)
        self.assertEqual(monitor["rtt_latest_ms"].tolist(), [175.0])
        self.assertEqual(monitor["rtt_mean_dev_ms"].tolist(), [12.0])
        self.assertEqual(monitor["owd_ms"].tolist(), [80.0])
        result = evaluation._per_second_delay(pd.DataFrame(), monitor, "qaccess_d")
        self.assertEqual(result["rtt_latest_ms_mean"].tolist(), [175.0])
        self.assertEqual(result["owd_ms_mean"].tolist(), [80.0])

    def test_delay_window_metrics_fall_back_to_monitor_owd(self):
        monitor = pd.DataFrame([{
            "time_s": 1.0,
            "path": 2,
            "owd_ms": 80.0,
            "rtt_smoothed_ms": 160.0,
            "rtt_latest_ms": 175.0,
            "rtt_mean_dev_ms": 12.0,
        }])
        metrics = evaluation._delay_window_metrics(
            pd.DataFrame(), monitor, pd.DataFrame(), 0.0, 50.0,
        )
        self.assertEqual(metrics["owd_ms_mean"], 80.0)
        self.assertEqual(metrics["owd_ms_p95"], 80.0)


if __name__ == "__main__":
    unittest.main()
