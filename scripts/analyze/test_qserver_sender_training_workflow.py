from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

import pandas as pd
import sys

REPO = Path(__file__).resolve().parents[2]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


runner = load("qserver_sweep", REPO / "scripts/mininet/run_qaccess_qserver_sender_sweep.py")
builder = load("qserver_builder", REPO / "scripts/analyze/build_qserver_sender_training.py")
auditor = load("qserver_auditor", REPO / "scripts/analyze/audit_qserver_sender_training.py")
variant_builder = load("variant_builder", REPO / "scripts/analyze/build_qaccess_variant_target.py")
sys.path.insert(0, str(REPO / "scripts/analyze"))
try:
    trainer = load("qserver_trainer", REPO / "scripts/analyze/train_qaccess_qserver_sender.py")
    d_rtt_trainer = load("qaccess_d_rtt_trainer", REPO / "scripts/analyze/train_qaccess_d_rtt.py")
except ModuleNotFoundError:
    trainer = None
    d_rtt_trainer = None


def write_fixture(root: Path, *, run_id: str = "run1", alpha: float = 0.6, seconds: int = 4, include_rtt: bool = False) -> Path:
    run = root / run_id
    run.mkdir(parents=True)
    rows = []
    for path_id, remote in [(1, "10.0.1.1:1"), (3, "10.0.2.1:1")]:
        for second in range(seconds):
            row = {
                "timestamp_ms": 1_000_000 + second * 1000, "run_id": run_id, "path_id": path_id,
                "alpha": alpha, "beta": 0.2, "gamma": 0.1, "endpoint_role": "server_downlink_sender",
                "producer_pid": 12, "connection_id": f"c-{run_id}", "local_endpoint": "[::]:1935",
                "remote_endpoint": remote, "sender_bytes_total": second * (100 + path_id),
                "bw_bps": path_id * 1_000_000 + second * 1000, "owd_ms": 20 + second,
                "delay_gradient_ms": 1, "loss_rate": 0.001, "lost_bytes_delta": 0,
                "retrans_bytes_delta": 0, "cwnd_bytes": 10000, "inflight_bytes": 5000,
                "cwnd_room": 5000, "utility": 1, "gain": 1, "backoff": 1,
            }
            if include_rtt:
                row.update({
                    "rtt_latest_ms": 100 + second * 10,
                    "rtt_smoothed_ms": 95 + second * 10,
                    "rtt_min_ms": 80,
                })
            rows.append(row)
    samples = run / "qaccess_runtime_samples.csv"
    pd.DataFrame(rows).to_csv(samples, index=False)
    meta = {
        "runtime_samples": str(samples), "run_id": run_id, "sweep_name": runner.tuple_id((alpha, 0.2, 0.1)),
        "coefficient_tuple_id": runner.tuple_id((alpha, 0.2, 0.1)), "alpha": alpha, "beta": 0.2, "gamma": 0.1,
        "endpoint_role": "server_downlink_sender", "deterioration_start_s": 1,
        "deterioration_end_s": 2, "impaired_interface": "h2-eth1",
    }
    path = run / "sweep_metadata.json"
    path.write_text(json.dumps(meta), encoding="utf-8")
    return path


class WorkflowTests(unittest.TestCase):
    def test_default_grid_and_tuple_file(self):
        self.assertEqual(len(runner.GRID), 27)
        self.assertEqual(len(set(runner.GRID)), 27)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tuples.csv"
            path.write_text("alpha,beta,gamma\n0.6,0.3,0.1\n0.7,0.3,0.2\n", encoding="utf-8")
            self.assertEqual(runner.parse_tuples_file(path), [(0.6, 0.3, 0.1), (0.7, 0.3, 0.2)])

    def test_metadata_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            coeffs = root / "coeffs.json"
            runner.write_coefficients(coeffs, (0.6, 0.3, 0.1), "smoke")
            self.assertEqual(json.loads(coeffs.read_text())["source"], "qserver_sender_sweep:smoke")
            timeline = root / "owner.jsonl"
            timeline.write_text(json.dumps({"phase2_owner": True, "controller_created": True,
                                            "endpoint_role": "server_downlink_sender", "pid": 42}) + "\n")
            self.assertEqual(runner.owner_identity(timeline)["pid"], 42)
            self.assertEqual(
                runner.runtime_samples_path(root / "phase2_state"),
                root / "phase2_state/qaccess_runtime_samples.csv",
            )
            samples = root / "fixed.csv"
            samples.write_text(
                "path_id,alpha,beta,gamma\n3,0.6,0.3,0.1\n1,0.6,0.3,0.1\n",
                encoding="utf-8",
            )
            self.assertEqual(
                runner.validate_fixed_samples(samples, (0.6, 0.3, 0.1)),
                {"runtime_sample_rows": 2, "runtime_sample_path_ids": [1, 3]},
            )
            profile = root / "delay.env"
            profile.write_text("IFACE=h2-eth1\n0 40\n50 80\n100 40\n")
            self.assertEqual(runner.profile_transition_times(profile), (50, 100))

    def test_non_sudo_ownership_restore_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_uid, old_gid = os.environ.pop("SUDO_UID", None), os.environ.pop("SUDO_GID", None)
            try:
                runner.restore_sudo_ownership(Path(tmp))
            finally:
                if old_uid is not None:
                    os.environ["SUDO_UID"] = old_uid
                if old_gid is not None:
                    os.environ["SUDO_GID"] = old_gid

    def test_phase_labels_and_grouped_future_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = builder.build_run(write_fixture(root, run_id="one", alpha=0.6))
            second = builder.build_run(write_fixture(root, run_id="two", alpha=0.7))
            result = pd.concat([first, second])
            self.assertEqual(set(result.phase_label), {"PRE", "DURING", "POST"})
            self.assertTrue((result.delta_bw_1s == 1000).all())
            self.assertTrue((result.delta_owd_1s == 1).all())
            self.assertTrue((result.delta_loss_1s == 0).all())
            self.assertEqual(result.groupby(["run_id", "path_id"]).size().to_dict(),
                             {("one", 1): 3, ("one", 3): 3, ("two", 1): 3, ("two", 3): 3})

    def test_clean_d_rtt_rows_use_causal_history_and_complete_future_median(self):
        with tempfile.TemporaryDirectory() as tmp:
            frame = builder.build_run(write_fixture(Path(tmp), seconds=8, include_rtt=True))
            path = frame[frame.path_id == 3].sort_values("time_s")
            row = path[path.time_s == 2].iloc[0]
            self.assertEqual(row.rtt_latest_median_ms, 120)
            self.assertEqual(row.rtt_history_median_3s_ms, 110)
            self.assertEqual(row.rtt_delta_1s_ms, 10)
            self.assertEqual(row.rtt_slope_3s_ms_per_s, 10)
            self.assertEqual(row.future_path_rtt_median_3s_ms, 140)
            self.assertEqual(row.delta_path_rtt_median_3s_ms, 20)
            self.assertEqual(row.rtt_future_window_sample_count, 3)
            self.assertTrue(path[path.time_s >= 5].future_path_rtt_median_3s_ms.isna().all())

    def test_clean_d_rtt_target_rejects_nonconsecutive_seconds(self):
        frame = pd.DataFrame({
            "run_id": ["run"] * 5,
            "connection_id": ["connection"] * 5,
            "endpoint_role": ["server_downlink_sender"] * 5,
            "path_id": [3] * 5,
            "alpha": [0.6] * 5,
            "beta": [0.2] * 5,
            "gamma": [0.1] * 5,
            "time_s": [0, 1, 2, 4, 5],
            "rtt_latest_median_ms": [100, 110, 120, 140, 150],
        })
        result = builder.add_rtt_history_and_target(
            frame,
            ["run_id", "connection_id", "endpoint_role", "path_id", "alpha", "beta", "gamma"],
        )
        self.assertTrue(result.future_path_rtt_median_3s_ms.isna().all())
        self.assertTrue(pd.isna(result.loc[result.time_s == 4, "rtt_history_median_3s_ms"]).all())

    def test_audit_failures_and_partial_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frame = builder.build_run(write_fixture(root))
            good = root / "good.csv"
            frame.to_csv(good, index=False)
            self.assertEqual(auditor.audit(good, allow_partial=True), [])
            missing_role = root / "missing-role.csv"
            frame.drop(columns=["endpoint_role"]).to_csv(missing_role, index=False)
            self.assertTrue(any("missing required" in x for x in auditor.audit(missing_role, True)))
            no_path_b = root / "no-path-b.csv"
            frame[frame.physical_path_label != "Path B"].to_csv(no_path_b, index=False)
            failures = auditor.audit(no_path_b, True)
            self.assertTrue(any("Path B" in x for x in failures))

    def test_model_report_records_identity_and_coverage(self):
        frame = pd.DataFrame({
            "endpoint_role": ["server_downlink_sender"] * 2,
            "alpha": [0.6, 0.7], "beta": [0.2, 0.3], "gamma": [0.1, 0.2],
        })
        report = builder.model_metadata(frame, Path("input.csv"), Path("model.pkl"), {"R2": 0.5}, ["bw_bps"], "abc", True)
        self.assertEqual(report["endpoint_role_distribution"], {"server_downlink_sender": 2})
        self.assertEqual(len(report["coefficient_coverage"]), 2)
        self.assertEqual(report["controller_variant"], "qaccess_t")
        self.assertEqual(report["worker_target_mode"], "delta_bw_1s")
        self.assertEqual(report["model_type"], "RandomForestRegressor")
        self.assertFalse(report["aggregate_active_ready"])

    def test_variant_target_summary_records_missing_future_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frame = pd.concat([
                builder.build_run(write_fixture(root, run_id="one", alpha=0.6)),
                builder.build_run(write_fixture(root, run_id="two", alpha=0.7)),
            ])
            source = root / "aggregated.csv"
            frame.to_csv(source, index=False)
            output, summary = variant_builder.build_target(source, "delta_owd_1s")
            self.assertEqual(len(output), len(frame) - 4)
            self.assertEqual(summary["missing_future_rows_dropped"], 4)
            self.assertEqual(summary["runs"], 2)
            self.assertEqual(summary["paths"], 2)
            self.assertEqual(summary["target_mean"], 1.0)

    def test_loss_risk_target_uses_next_aggregated_second(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frame = builder.build_run(write_fixture(root))
            frame["lost_bytes_delta"] = [0, 100, 0, 0, 200, 0]
            source = root / "loss.csv"
            frame.to_csv(source, index=False)
            output, summary = variant_builder.build_target(source, "loss_risk_1s")
            self.assertEqual(summary["missing_future_rows_dropped"], 2)
            self.assertEqual(sorted(output.loss_risk_1s.tolist()), [0, 0, 100, 200])

    @unittest.skipIf(trainer is None, "training dependencies are unavailable")
    def test_training_excludes_idle_path_group_but_keeps_zero_windows_on_media_path(self):
        frame = pd.DataFrame({
            "run_id": ["r"] * 6, "connection_id": ["c"] * 6,
            "path_id": [0, 0, 0, 3, 3, 3],
            "sender_byte_delta": [0, 0, 0, 0, 100, 0],
        })
        filtered, summary = trainer.filter_active_media_groups(frame)
        self.assertEqual(filtered.path_id.unique().tolist(), [3])
        self.assertEqual(len(filtered), 3)
        self.assertEqual(summary["excluded_path_ids"], [0])

    @unittest.skipIf(d_rtt_trainer is None, "training dependencies are unavailable")
    def test_clean_d_rtt_trainer_filters_complete_active_rows_and_rescores_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frame = pd.concat([
                builder.build_run(write_fixture(root, run_id="one", alpha=0.6, seconds=9, include_rtt=True)),
                builder.build_run(write_fixture(root, run_id="two", alpha=0.7, seconds=9, include_rtt=True)),
            ])
            source = root / "clean_d.csv"
            frame.to_csv(source, index=False)
            loaded = d_rtt_trainer.load_frame(source)
            self.assertGreater(len(loaded), 0)
            self.assertFalse(loaded[d_rtt_trainer.FEATURES + [builder.RTT_TARGET]].isna().any().any())
            candidate = d_rtt_trainer.candidate_frame(loaded.head(2), 0.8, 0.3, 0.2)
            self.assertEqual(candidate.columns.tolist(), d_rtt_trainer.FEATURES)
            self.assertTrue((candidate.alpha == 0.8).all())
            self.assertTrue((candidate.beta == 0.3).all())
            self.assertTrue((candidate.gamma == 0.2).all())

    @unittest.skipIf(d_rtt_trainer is None, "training dependencies are unavailable")
    def test_clean_d_rtt_trainer_rejects_coefficient_change_within_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frame = builder.build_run(write_fixture(root, run_id="one", seconds=9, include_rtt=True))
            complete = frame.dropna(subset=[builder.RTT_TARGET]).copy()
            complete.loc[complete.index[-1], "alpha"] = 0.8
            source = root / "invalid.csv"
            complete.to_csv(source, index=False)
            with self.assertRaisesRegex(ValueError, "coefficient changed within runs"):
                d_rtt_trainer.load_frame(source)


if __name__ == "__main__":
    unittest.main()
