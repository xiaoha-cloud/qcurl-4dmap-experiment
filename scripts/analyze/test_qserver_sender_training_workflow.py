from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

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


def write_fixture(root: Path, *, run_id: str = "run1", alpha: float = 0.6) -> Path:
    run = root / run_id
    run.mkdir(parents=True)
    rows = []
    for path_id, remote in [(1, "10.0.1.1:1"), (3, "10.0.2.1:1")]:
        for second in range(4):
            rows.append({
                "timestamp_ms": 1_000_000 + second * 1000, "run_id": run_id, "path_id": path_id,
                "alpha": alpha, "beta": 0.2, "gamma": 0.1, "endpoint_role": "server_downlink_sender",
                "producer_pid": 12, "connection_id": f"c-{run_id}", "local_endpoint": "[::]:1935",
                "remote_endpoint": remote, "sender_bytes_total": second * (100 + path_id),
                "bw_bps": path_id * 1_000_000 + second * 1000, "owd_ms": 20 + second,
                "delay_gradient_ms": 1, "loss_rate": 0.001, "lost_bytes_delta": 0,
                "retrans_bytes_delta": 0, "cwnd_bytes": 10000, "inflight_bytes": 5000,
                "cwnd_room": 5000, "utility": 1, "gain": 1, "backoff": 1,
            })
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

    def test_phase_labels_and_grouped_future_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = builder.build_run(write_fixture(root, run_id="one", alpha=0.6))
            second = builder.build_run(write_fixture(root, run_id="two", alpha=0.7))
            result = pd.concat([first, second])
            self.assertEqual(set(result.phase_label), {"PRE", "DURING", "POST"})
            self.assertTrue((result.delta_bw_1s == 1000).all())
            self.assertEqual(result.groupby(["run_id", "path_id"]).size().to_dict(),
                             {("one", 1): 3, ("one", 3): 3, ("two", 1): 3, ("two", 3): 3})

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
        self.assertFalse(report["aggregate_active_ready"])


if __name__ == "__main__":
    unittest.main()
