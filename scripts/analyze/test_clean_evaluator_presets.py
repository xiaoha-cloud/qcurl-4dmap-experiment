#!/usr/bin/env python3

import json
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import clean_evaluator_presets as clean
import evaluate_qaccess_t_no_deterioration as stability_eval
import qaccess_delay_loss_eval_analyze as delay_loss_eval
import qaccess_t_throughput_compare as throughput_eval


EXPECTED_WINDOWS = (
    ("0-50", 0.0, 50.0),
    ("50-60", 50.0, 60.0),
    ("60-100", 60.0, 100.0),
    ("100-110", 100.0, 110.0),
    ("110-200", 110.0, 200.0),
    ("0-200", 0.0, 200.0),
)


class CleanEvaluatorPresetTest(unittest.TestCase):
    def test_all_clean_presets_have_required_windows(self) -> None:
        self.assertEqual(
            set(clean.CLEAN_PRESETS),
            {"bandwidth_clean", "delay_clean", "loss_clean", "stability_clean"},
        )
        metadata = {"transitions_sec": [0, 50, 100]}
        for preset in clean.CLEAN_PRESETS:
            windows = clean.clean_windows(preset, metadata)
            self.assertEqual(
                tuple((window.name, window.start_s, window.end_s) for window in windows),
                EXPECTED_WINDOWS,
                preset,
            )

    def test_metadata_selects_clean_profile_type(self) -> None:
        for kind, preset in (
            ("bandwidth", "bandwidth_clean"),
            ("delay", "delay_clean"),
            ("loss", "loss_clean"),
            ("none", "stability_clean"),
        ):
            metadata = {
                "experiment_family": "clean_controlled",
                "scenario": "clean_equal_paths",
                "profile_kind": kind,
                "transitions_sec": [0, 50, 100],
            }
            self.assertEqual(clean.preset_from_metadata(metadata), preset)

    def test_metadata_transition_times_are_preferred_when_available(self) -> None:
        windows = clean.clean_windows(
            "bandwidth_clean", {"transitions_sec": [0, 55, 105]},
        )
        self.assertEqual(
            [(window.start_s, window.end_s) for window in windows],
            [(0, 55), (55, 65), (65, 105), (105, 115), (115, 200), (0, 200)],
        )

    def test_final_window_labels_match_clean_interpretation(self) -> None:
        bandwidth = clean.clean_windows("bandwidth_clean")[-2]
        delay = clean.clean_windows("delay_clean")[-2]
        loss = clean.clean_windows("loss_clean")[-2]
        self.assertEqual(bandwidth.role, "stable")
        self.assertIn("stable low bandwidth", bandwidth.condition)
        self.assertEqual(delay.role, "recovery")
        self.assertIn("recovery", delay.condition)
        self.assertEqual(loss.role, "recovery")
        self.assertIn("recovery", loss.condition)

    def test_historical_window_constants_are_unchanged(self) -> None:
        self.assertEqual(delay_loss_eval.EVAL_WINDOWS, [
            ("0-50", 0.0, 50.0),
            ("50-90", 50.0, 90.0),
            ("90-100", 90.0, 100.0),
            ("100-150", 100.0, 150.0),
            ("150-200", 150.0, 200.0),
        ])
        self.assertEqual(stability_eval.WINDOWS, [
            (0.0, 50.0), (50.0, 100.0), (100.0, 150.0), (150.0, 200.0),
        ])
        self.assertEqual(throughput_eval.WINDOWS, [
            ("0-50", 0.0, 50.0),
            ("50-60", 50.0, 60.0),
            ("50-100", 50.0, 100.0),
            ("100-110", 100.0, 110.0),
            ("100-150", 100.0, 150.0),
        ])

    def test_clean_clip_excludes_every_row_outside_zero_to_200(self) -> None:
        frame = pd.DataFrame({"time_s": [-1.0, 0.0, 50.0, 199.999, 200.0, 201.0]})
        clipped = clean.clip_to_clean_run(frame)
        self.assertEqual(clipped.time_s.tolist(), [0.0, 50.0, 199.999])

    def test_bandwidth_path_loader_produces_total_path_a_and_path_b(self) -> None:
        fake_paths = [Path("pathA_h1_test.pcap"), Path("pathB_h1_test.pcap")]
        values = {
            "pathA_h1_test.pcap": {0.0: 1_000_000},
            "pathB_h1_test.pcap": {0.0: 2_000_000},
        }
        with patch.object(throughput_eval, "_find_pcaps", return_value=fake_paths), \
             patch.object(
                 throughput_eval,
                 "_pcap_to_bytes_by_bin",
                 side_effect=lambda path, **_: values[path.name],
             ):
            frame, _ = throughput_eval.load_clean_path_tp_mbps_from_pcaps(Path("unused"))
        self.assertAlmostEqual(frame.path_a_mbps.iloc[0], 8.0)
        self.assertAlmostEqual(frame.path_b_mbps.iloc[0], 16.0)
        self.assertAlmostEqual(frame.tp_mbps.iloc[0], 24.0)

    def test_clean_delay_output_contains_primary_and_per_path_throughput(self) -> None:
        util = pd.DataFrame([
            {"t": 20.0, "path": 2, "owd_ms": 40.0},
            {"t": 70.0, "path": 2, "owd_ms": 80.0},
            {"t": 210.0, "path": 2, "owd_ms": 999.0},
        ])
        wire = pd.DataFrame([
            {"time_s": 20.0, "total_quic_wire_mbps": 20.0, "path_a_quic_wire_mbps": 10.0,
             "path_b_quic_wire_mbps": 10.0, "path_b_share_pct": 50.0},
            {"time_s": 70.0, "total_quic_wire_mbps": 18.0, "path_a_quic_wire_mbps": 10.0,
             "path_b_quic_wire_mbps": 8.0, "path_b_share_pct": 44.4},
            {"time_s": 210.0, "total_quic_wire_mbps": 999.0, "path_a_quic_wire_mbps": 999.0,
             "path_b_quic_wire_mbps": 999.0, "path_b_share_pct": 50.0},
        ])
        with patch.object(delay_loss_eval, "load_pull_frames", return_value=(util, pd.DataFrame())), \
             patch.object(delay_loss_eval, "load_wire_timeseries", return_value=wire), \
             patch.object(delay_loss_eval, "load_runtime_samples", return_value=pd.DataFrame()):
            result, _, clipped_wire, _ = delay_loss_eval.analyze_run(
                Path("unused"), "baseline", "delay", clean.clean_windows("delay_clean"),
                100.0, (0.0, 50.0), clean=True,
            )
        self.assertEqual(result.window.tolist(), [item[0] for item in EXPECTED_WINDOWS])
        for column in (
            "owd_ms_mean", "secondary_total_quic_wire_mbps_mean",
            "secondary_path_a_quic_wire_mbps_mean", "secondary_path_b_quic_wire_mbps_mean",
        ):
            self.assertIn(column, result.columns)
        self.assertLess(clipped_wire.time_s.max(), 200.0)

    def test_stability_update_count_deduplicates_applied_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory)
            (session / "worker.log").write_text("\n".join((
                json.dumps({"request_id": "one", "actual_applied": True}),
                json.dumps({"request_id": "one", "actual_applied": True}),
                json.dumps({"request_id": "two", "actual_applied": False}),
            )), encoding="utf-8")
            self.assertEqual(stability_eval.count_applied_updates(session), 1)


if __name__ == "__main__":
    unittest.main()
