from __future__ import annotations

import csv
import importlib.util
import json
import pickle
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd

ANALYZE = Path(__file__).resolve().parent
REPO = ANALYZE.parents[1]
sys.path.insert(0, str(ANALYZE))

joblib_stub = types.ModuleType("joblib")
joblib_stub.load = lambda path: pickle.loads(Path(path).read_bytes())
joblib_stub.dump = lambda obj, path: Path(path).write_bytes(pickle.dumps(obj))
sys.modules.setdefault("joblib", joblib_stub)

import qaccess_t_update_worker as worker  # noqa: E402

validator_spec = importlib.util.spec_from_file_location(
    "phase2_validator", ANALYZE / "validate_qaccess_phase2_run.py"
)
validator = importlib.util.module_from_spec(validator_spec)
assert validator_spec.loader is not None
validator_spec.loader.exec_module(validator)
comparison_spec = importlib.util.spec_from_file_location(
    "relative_comparison", ANALYZE / "compare_baseline_dynamic_relative.py"
)
comparison = importlib.util.module_from_spec(comparison_spec)
assert comparison_spec.loader is not None
comparison_spec.loader.exec_module(comparison)


class CoefficientSensitiveModel:
    feature_names_in_ = np.array(worker.FEATURES)
    n_features_in_ = len(worker.FEATURES)

    def predict(self, frame):
        return (
            frame["alpha"].to_numpy() * 10_000_000
            - frame["beta"].to_numpy() * 2_000_000
            - frame["gamma"].to_numpy() * 1_000_000
            + frame["bw_bps"].to_numpy() * 0.2
        )


def write_model_fixture(root: Path, target: str = "delta_bw_1s") -> tuple[Path, Path]:
    model = root / "qaccess_t_model_delta_bw_1s.pkl"
    model.write_bytes(pickle.dumps(CoefficientSensitiveModel()))
    metadata = root / "metadata.json"
    metadata.write_text(
        json.dumps({
            "models": {
                target: {
                    "target": target,
                    "model_path": str(model),
                    "rows_used": 123,
                }
            }
        }),
        encoding="utf-8",
    )
    return model, metadata


def write_request_fixture(root: Path) -> dict[str, Path]:
    paths = {name: root / name for name in (
        "request.json", "samples.csv", "coeffs.json", "response.json", "state.json", "audit.csv"
    )}
    paths["request.json"].write_text(json.dumps({
        "request_id": "test_run_100000_1",
        "path_id": 0,
        "reason": "buffer_full",
        "run_id": "test_run",
        "current_alpha": 0.6,
        "current_beta": 0.3,
        "current_gamma": 0.1,
    }), encoding="utf-8")
    row = {name: 0.0 for name in worker.FEATURES}
    row.update({
        "timestamp_ms": 1, "run_id": "test_run", "path_id": 0,
        "bw_bps": 5_000_000, "owd_ms": 20, "inflight_bytes": 5000,
        "alpha": 0.6, "beta": 0.3, "gamma": 0.1, "next_bw_bps": 6_000_000,
        "sender_bytes_total": 100, "remote_endpoint": "10.0.1.1:1234",
        "endpoint_role": "server_downlink_sender", "producer_pid": 42,
    })
    row2 = dict(row)
    row2.update({"timestamp_ms": 2, "sender_bytes_total": 200})
    pd.DataFrame([row, row2]).to_csv(paths["samples.csv"], index=False)
    paths["coeffs.json"].write_text(json.dumps({
        "alpha": 0.6, "beta": 0.3, "gamma": 0.1, "source": "initial"
    }), encoding="utf-8")
    return paths


class WorkerTests(unittest.TestCase):
    def test_gain_gate_modes_and_zero_current(self):
        absolute = worker.evaluate_gain_gate(1_000_000, 1_400_000, gate_mode="absolute", min_delta_gain_bps=500_000, min_relative_gain=0.03)
        relative = worker.evaluate_gain_gate(1_000_000, 1_040_000, gate_mode="relative", min_delta_gain_bps=500_000, min_relative_gain=0.03)
        hybrid = worker.evaluate_gain_gate(1_000_000, 1_600_000, gate_mode="hybrid", min_delta_gain_bps=500_000, min_relative_gain=0.03)
        near_zero = worker.evaluate_gain_gate(0.0, 1.0, gate_mode="relative", min_delta_gain_bps=500_000, min_relative_gain=0.03)
        self.assertFalse(absolute["would_apply"])
        self.assertTrue(relative["would_apply"])
        self.assertTrue(hybrid["would_apply"])
        self.assertTrue(near_zero["would_apply"])
        self.assertAlmostEqual(relative["relative_gain"], 0.04)

    def test_aggregate_relative_shadow_reports_without_mutation(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        model, _ = write_model_fixture(root)
        paths = write_request_fixture(root)
        before = paths["coeffs.json"].read_bytes()
        worker._process_request(
            paths["request.json"], paths["samples.csv"], model, paths["coeffs.json"],
            paths["response.json"], paths["state.json"], root / "archive", root / "previous.json",
            paths["audit.csv"], "rf", "delta_bw_1s", 3.0, 500_000.0, 0.01,
            shadow=True, aggregate_multipath=True, gate_mode="relative", min_relative_gain=0.03,
        )
        response = json.loads(paths["response.json"].read_text())
        self.assertTrue(response["traffic_weighted_gate"]["relative_gate_pass"])
        self.assertFalse(response["actual_applied"])
        self.assertEqual(paths["coeffs.json"].read_bytes(), before)
        temp.cleanup()

    def _run_active_aggregate(self, *, owner_role="server_downlink_sender", sender_growth=True):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        model, _ = write_model_fixture(root)
        paths = write_request_fixture(root)
        frame = pd.read_csv(paths["samples.csv"])
        frame["endpoint_role"] = owner_role
        if not sender_growth:
            frame["sender_bytes_total"] = 100
        frame.to_csv(paths["samples.csv"], index=False)
        before = paths["coeffs.json"].read_bytes()
        worker._process_request(
            paths["request.json"], paths["samples.csv"], model, paths["coeffs.json"],
            paths["response.json"], paths["state.json"], root / "archive", root / "previous.json",
            paths["audit.csv"], "rf", "delta_bw_1s", 3.0, 500_000.0, 0.01,
            shadow=False, aggregate_multipath=True, gate_mode="relative", min_relative_gain=0.03,
        )
        return temp, paths, before, json.loads(paths["response.json"].read_text())

    def test_active_aggregate_mutates_only_after_safety_checks(self):
        temp, paths, before, response = self._run_active_aggregate()
        try:
            self.assertEqual(response["status"], "APPLIED_AGGREGATE")
            self.assertTrue(response["actual_applied"])
            self.assertNotEqual(paths["coeffs.json"].read_bytes(), before)
        finally:
            temp.cleanup()

    def test_active_aggregate_rejects_wrong_owner_and_no_media(self):
        for kwargs, reason in [({"owner_role": "client_pull_receiver"}, "owner_role_not_server_downlink_sender"),
                               ({"sender_growth": False}, "no_eligible_media_paths")]:
            temp, paths, before, response = self._run_active_aggregate(**kwargs)
            try:
                self.assertFalse(response["actual_applied"])
                self.assertEqual(paths["coeffs.json"].read_bytes(), before)
                self.assertEqual(response["skip_reason"], reason)
            finally:
                temp.cleanup()
    def test_model_target_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model, metadata = write_model_fixture(root)
            result = worker.validate_model_configuration(model, metadata, "delta_bw_1s")
            self.assertTrue(result["model_target_compatible"])
            with self.assertRaisesRegex(ValueError, "incompatible model/target"):
                worker.validate_model_configuration(model, metadata, "next_bw_bps")

    def _run_worker(self, shadow: bool):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        model, _ = write_model_fixture(root)
        paths = write_request_fixture(root)
        before = paths["coeffs.json"].read_bytes()
        ok = worker._process_request(
            paths["request.json"], paths["samples.csv"], model, paths["coeffs.json"],
            paths["response.json"], paths["state.json"], root / "archive",
            root / "previous.json", paths["audit.csv"], "rf", "delta_bw_1s",
            3.0, 500_000.0, 0.01, shadow=shadow, fixed_gamma=None,
            log_file=root / "worker.log",
        )
        self.assertTrue(ok)
        return temp, root, paths, before, json.loads(paths["response.json"].read_text())

    def test_shadow_proposes_without_mutating_coefficients(self):
        temp, root, paths, before, response = self._run_worker(True)
        try:
            self.assertEqual(paths["coeffs.json"].read_bytes(), before)
            self.assertEqual(response["status"], "SHADOW_AGGREGATE_EVALUATED")
            self.assertTrue(response["would_apply"])
            self.assertEqual(response["candidate_count"], 27)
            self.assertGreater(response["unique_prediction_count"], 1)
            self.assertGreater(response["score_gain_bps"], 500_000)
            self.assertTrue(response["equal_weight_proposed_stepped_coefficients"])
            self.assertTrue(response["traffic_weighted_proposed_stepped_coefficients"])
        finally:
            temp.cleanup()

    def test_active_mutates_temporary_coefficients(self):
        temp, root, paths, before, response = self._run_worker(False)
        try:
            self.assertNotEqual(paths["coeffs.json"].read_bytes(), before)
            self.assertEqual(response["status"], "APPLIED")
            self.assertIn("0", json.loads(paths["coeffs.json"].read_text())["paths"])
        finally:
            temp.cleanup()

    def test_extended_profile_restores_at_150_seconds(self):
        profile = REPO / "scripts/mininet/combined_deterioration_profile_90_150.env"
        rows = [line.split() for line in profile.read_text().splitlines() if line and not line.startswith(("#", "IFACE="))]
        self.assertEqual(rows, [["0", "20ms", "0%"], ["90", "80ms", "0.05%"], ["150", "20ms", "0%"]])

    def test_multipath_shadow_excludes_idle_and_aggregates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model, _ = write_model_fixture(root)
            paths = write_request_fixture(root)
            base = pd.read_csv(paths["samples.csv"]).iloc[0].to_dict()
            rows = []
            for path_id, endpoint, bw, sent_values in (
                (0, "10.0.1.1:50780", 1_000_000, (1337, 1337)),
                (1, "10.0.1.1:49264", 10_000_000, (100, 1000)),
                (3, "10.0.2.1:59496", 4_000_000, (100, 200)),
            ):
                for index, sent in enumerate(sent_values):
                    row = dict(base)
                    row.update({"path_id": path_id, "remote_endpoint": endpoint, "bw_bps": bw,
                                "sender_bytes_total": sent, "timestamp_ms": index + 1})
                    rows.append(row)
            pd.DataFrame(rows).to_csv(paths["samples.csv"], index=False)
            before = paths["coeffs.json"].read_bytes()
            ok = worker._process_request(
                paths["request.json"], paths["samples.csv"], model, paths["coeffs.json"],
                paths["response.json"], paths["state.json"], root / "archive",
                root / "previous.json", paths["audit.csv"], "rf", "delta_bw_1s",
                3.0, 500_000.0, 0.01, shadow=True, fixed_gamma=None,
                log_file=root / "worker.log",
            )
            self.assertTrue(ok)
            response = json.loads(paths["response.json"].read_text())
            self.assertEqual(response["eligible_path_ids"], [1, 3])
            self.assertEqual(response["excluded_paths"][0]["exclusion_reason"], "no_sender_byte_growth")
            self.assertNotEqual(response["equal_weight_current"], response["traffic_weighted_current"])
            self.assertEqual(paths["coeffs.json"].read_bytes(), before)
            per_path = pd.read_csv(next((root / "archive").glob("qaccess_candidate_scores_*_per_path.csv")))
            self.assertEqual(set(per_path.path_id), {1, 3})
            self.assertEqual(len(per_path), 54)
            self.assertAlmostEqual(per_path[per_path.path_id == 1].media_activity_weight.iloc[0], 0.9)
            self.assertAlmostEqual(per_path[per_path.path_id == 3].media_activity_weight.iloc[0], 0.1)
            weights = per_path.groupby("path_id").media_activity_weight.first()
            self.assertAlmostEqual(weights.sum(), 1.0)
            aggregate = pd.read_csv(next((root / "archive").glob("qaccess_candidate_scores_*_aggregate.csv")))
            self.assertEqual(len(aggregate), 27)
            first = per_path.iloc[0][["alpha", "beta", "gamma"]]
            candidate = per_path[
                (per_path.alpha == first.alpha)
                & (per_path.beta == first.beta)
                & (per_path.gamma == first.gamma)
            ]
            aggregate_row = aggregate[
                (aggregate.alpha == first.alpha)
                & (aggregate.beta == first.beta)
                & (aggregate.gamma == first.gamma)
            ].iloc[0]
            self.assertAlmostEqual(aggregate_row.equal_weight_score, candidate.path_pred_candidate.mean())
            self.assertAlmostEqual(
                aggregate_row.byte_weighted_score,
                (candidate.path_pred_candidate * candidate.media_activity_weight).sum(),
            )

    def test_multipath_shadow_handles_one_eligible_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model, _ = write_model_fixture(root)
            paths = write_request_fixture(root)
            df = pd.read_csv(paths["samples.csv"])
            df["path_id"] = 17
            df["sender_bytes_total"] = [100, 200]
            df.to_csv(paths["samples.csv"], index=False)
            worker._process_request(
                paths["request.json"], paths["samples.csv"], model, paths["coeffs.json"],
                paths["response.json"], paths["state.json"], root / "archive",
                root / "previous.json", paths["audit.csv"], "rf", "delta_bw_1s",
                3.0, 500_000.0, 0.01, shadow=True, fixed_gamma=None,
                log_file=root / "worker.log",
            )
            response = json.loads(paths["response.json"].read_text())
            self.assertEqual(response["eligible_path_ids"], [17])
            self.assertEqual(response["path_weights"], {"17": 1.0})
            self.assertEqual(response["status"], "SHADOW_AGGREGATE_EVALUATED")

    def test_multipath_shadow_skips_without_eligible_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model, _ = write_model_fixture(root)
            paths = write_request_fixture(root)
            df = pd.read_csv(paths["samples.csv"])
            df["sender_bytes_total"] = 1337
            df.to_csv(paths["samples.csv"], index=False)
            before = paths["coeffs.json"].read_bytes()
            worker._process_request(
                paths["request.json"], paths["samples.csv"], model, paths["coeffs.json"],
                paths["response.json"], paths["state.json"], root / "archive",
                root / "previous.json", paths["audit.csv"], "rf", "delta_bw_1s",
                3.0, 500_000.0, 0.01, shadow=True, fixed_gamma=None,
                log_file=root / "worker.log",
            )
            response = json.loads(paths["response.json"].read_text())
            self.assertEqual(response["status"], "SHADOW_SKIPPED_NO_MEDIA_PATH")
            self.assertEqual(response["skip_reason"], "no_eligible_media_paths")
            self.assertEqual(paths["coeffs.json"].read_bytes(), before)

    def test_sender_counter_reset_uses_positive_intervals(self):
        rows = pd.DataFrame({
            "path_id": [7, 7, 7, 7], "sender_bytes_total": [100, 200, 10, 60],
            "bw_bps": [1_000_000] * 4, "owd_ms": [20] * 4, "inflight_bytes": [1000] * 4,
            **{name: [0.0] * 4 for name in worker.FEATURES if name not in {"bw_bps", "owd_ms", "inflight_bytes"}},
        })
        result = worker._classify_media_paths(worker._clean_runtime_samples(rows), 1, 1)[0]
        self.assertTrue(result["eligible"])
        self.assertTrue(result["sender_counter_reset"])
        self.assertEqual(result["sender_byte_delta"], 150)

    def test_missing_sender_counter_is_rejected(self):
        rows = pd.DataFrame({
            "path_id": [9, 9], "bw_bps": [1_000_000] * 2, "owd_ms": [20] * 2,
            **{name: [0.0] * 2 for name in worker.FEATURES if name not in {"bw_bps", "owd_ms"}},
        })
        result = worker._classify_media_paths(worker._clean_runtime_samples(rows), 1, 1)[0]
        self.assertFalse(result["eligible"])
        self.assertIn("missing_sender_bytes", result["exclusion_reason"])


def make_validator_fixture(root: Path, mode: str = "shadow", during: bool = True, failed: bool = False) -> Path:
    session = root / "session"
    dynamic = session / "combined_qaccess_t_dynamic"
    processed = dynamic / "processed_buffers"
    processed.mkdir(parents=True)
    (session / "worker_ready.json").write_text(json.dumps({
        "resolved_model_path": "/vm/derived/qaccess_t_model_delta_bw_1s.pkl",
        "verified_model_target": "delta_bw_1s", "model_target_compatible": True,
        "execution_mode": mode, "shadow_mode": mode == "shadow",
    }))
    t0_ms = 1_750_000_000_000
    timestamp = t0_ms + (100_000 if during else 20_000)
    trigger = [{
        "timestamp_ms": timestamp, "trigger_decision": "request_written", "request_id": "run_1"
    }]
    if failed:
        trigger.append({"timestamp_ms": timestamp + 1, "trigger_decision": "request_write_failed"})
    (session / "qaccess_trigger_audit.jsonl").write_text("\n".join(json.dumps(x) for x in trigger) + "\n")
    state_dir = str((root / "state").resolve())
    owner_rows = [
        {"pid": 42, "controller_pid": 42, "phase2_owner": True, "controller_created": True,
         "endpoint_role": "server_downlink_sender", "phase2_state_dir": state_dir,
         "lease_decision": "owner_acquired"},
        {"pid": 42, "phase2_owner": False, "controller_created": False,
         "endpoint_role": "server_publisher_ingress", "phase2_enabled": False,
         "lease_decision": "publisher_ingress_disabled"},
    ]
    (session / "qaccess_owner_audit.jsonl").write_text("\n".join(json.dumps(x) for x in owner_rows) + "\n")
    with (dynamic / "experiment_timeline_test.jsonl").open("w") as handle:
        for role in ("client_push_publisher", "client_pull_receiver"):
            handle.write(json.dumps({"event": "phase2_identity", "endpoint_role": role,
                                     "phase2_enabled": False, "phase2_owner": False,
                                     "controller_created": False}) + "\n")
    t0 = datetime_from_ms(t0_ms)
    (dynamic / "tc_deterioration.log").write_text(
        f"[{t0}] step 1/3 at=0s\n[{t0}] step 2/3 at=90s\n[{t0}] step 3/3 at=150s\n"
        f"[{t0}] finished all steps\n[{t0}] exiting status=0 current_step=3 completed=1\n"
    )
    with (processed / "qaccess_candidate_scores_run_1_aggregate.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["mean_prediction"])
        writer.writeheader(); writer.writerow({"mean_prediction": 1}); writer.writerow({"mean_prediction": 2})
    with (processed / "qaccess_candidate_scores_run_1_per_path.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["mean_prediction"])
        writer.writeheader(); writer.writerow({"mean_prediction": 1}); writer.writerow({"mean_prediction": 2})
    (processed / "qaccess_path_eligibility_run_1.json").write_text(json.dumps([
        {"path_id": 0, "eligible": False}, {"path_id": 1, "eligible": True}, {"path_id": 3, "eligible": True},
    ]))
    base_sample = {
        "producer_pid": 42, "endpoint_role": "server_downlink_sender", "phase2_state_dir": state_dir,
        "connection_id": "conn", "rtmp_session_id": "sub", "stream_key": "live/test",
        "local_endpoint": "[::]:1935", "bw_bps": 1_000_000, "owd_ms": 20,
        "loss_rate": 0, "retrans_bytes_delta": 0, "cwnd_bytes": 10000, "inflight_bytes": 5000,
    }
    sample_rows = []
    for path_id, endpoint, sent_values in (
        (0, "10.0.1.1:5000", (1337, 1337)),
        (1, "10.0.1.1:5001", (100, 200)),
        (3, "10.0.2.1:5003", (100, 150)),
    ):
        for sent in sent_values:
            sample_rows.append({**base_sample, "path_id": path_id, "remote_endpoint": endpoint,
                                "sender_bytes_total": sent, "inflight_bytes": 0 if path_id == 0 else 5000,
                                "loss_rate": 0.01 if path_id == 3 and sent == sent_values[-1] else 0})
    pd.DataFrame(sample_rows).to_csv(processed / "qaccess_runtime_samples_run_1_all_paths.csv", index=False)
    status = "SHADOW_AGGREGATE_EVALUATED" if mode == "shadow" else "APPLIED_AGGREGATE"
    worker_row = {
        "request_id": "run_1", "status": status, "would_apply": True,
        "proposed_stepped_coefficients": {"alpha": 0.7, "beta": 0.2, "gamma": 0.2},
        "equal_weight_proposed_stepped_coefficients": {"alpha": 0.7, "beta": 0.2, "gamma": 0.2},
        "traffic_weighted_proposed_stepped_coefficients": {"alpha": 0.7, "beta": 0.2, "gamma": 0.2},
        "eligible_path_ids": [1, 3], "excluded_paths": [{"path_id": 0}],
        "equal_weight_gain": 1.0, "traffic_weighted_gain": 1.0, "aggregate_methods_agree": True,
    }
    if mode == "active":
        worker_row.update({
            "actual_applied": True,
            "timestamp_ms": timestamp + 500,
            "applied_coefficients": {"alpha": 0.6, "beta": 0.3, "gamma": 0.2},
        })
    (session / "worker.log").write_text(json.dumps(worker_row) + "\n")
    if mode == "shadow":
        (processed / "qaccess_multipath_shadow_audit_run_1.json").write_text(json.dumps({"eligible_path_ids": [1, 3]}))
    before = dict(worker=mode, **{"alpha": 0.6, "beta": 0.3, "gamma": 0.1})
    after = dict(before) if mode == "shadow" else {"version": 1, "default": {"alpha": 0.6, "beta": 0.3, "gamma": 0.1}, "paths": {"0": {"alpha": 0.7, "beta": 0.2, "gamma": 0.2}}}
    (session / "combined_qaccess_t_dynamic_coeffs_before.json").write_text(json.dumps(before))
    (session / "combined_qaccess_t_dynamic_coeffs_after.json").write_text(json.dumps(after))
    if mode == "active":
        with (dynamic / "control_law_diagnostics.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["timestamp_ms", "alpha", "beta", "gamma"])
            writer.writeheader()
            writer.writerow({"timestamp_ms": timestamp - 1000, "alpha": 0.6, "beta": 0.3, "gamma": 0.1})
            writer.writerow({"timestamp_ms": timestamp + 1000, "alpha": 0.6, "beta": 0.3, "gamma": 0.2})
        (session / "baseline_vs_dynamic_relative_comparison.json").write_text(json.dumps({
            "dynamic_updates_applied": True,
        }))
    return session


def datetime_from_ms(value: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


class ValidatorTests(unittest.TestCase):
    def test_two_arm_comparison_has_no_fixed_utility_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            for arm, scale in (("combined_baseline", 1.0), ("combined_qaccess_t_dynamic", 1.2)):
                directory = session / arm
                directory.mkdir()
                frame = pd.DataFrame({"elapsed_s": list(range(220)), "throughput_mbps": [scale] * 220})
                for filename in comparison.FILES.values():
                    frame.to_csv(directory / filename, index=False)
            (session / "experiment_metadata.json").write_text(json.dumps({
                "gate_mode": "hybrid", "min_relative_gain": 0.03, "min_delta_gain_bps": 100000,
            }))
            worker_events = [
                {"request_id": "run_1", "request_classification": "PRE_DETERIORATION",
                 "status": "ACTIVE_AGGREGATE_SKIPPED", "actual_applied": False,
                 "relative_gate_pass": True, "absolute_gate_pass": False},
                {"request_id": "run_2", "request_classification": "DURING_DETERIORATION",
                 "status": "APPLIED_AGGREGATE", "actual_applied": True,
                 "relative_gate_pass": True, "absolute_gate_pass": True},
            ]
            (session / "worker.log").write_text("".join(json.dumps(row) + "\n" for row in worker_events))
            (session / "qaccess_trigger_audit.jsonl").write_text(json.dumps({
                "request_id": "run_1", "trigger_decision": "request_written",
            }) + "\n")
            report = comparison.analyze(session)
            self.assertEqual(set(report["arms"]), {"baseline", "dynamic"})
            self.assertFalse(report["fixed_utility_arm"])
            self.assertEqual(report["verdict"], "dynamic_better")
            self.assertEqual(report["gate_mode"], "hybrid")
            self.assertEqual(report["applied_update_count"], 1)
            self.assertEqual(report["applied_request_classifications"], ["DURING_DETERIORATION"])
            self.assertTrue(report["pre_small_gain_updates_blocked"])

    def _validate(self, mode="shadow", during=True, failed=False):
        temp = tempfile.TemporaryDirectory()
        session = make_validator_fixture(Path(temp.name), mode=mode, during=during, failed=failed)
        output = StringIO()
        with redirect_stdout(output):
            code = validator.validate_session(session, mode, 90, 150)
        return temp, code, output.getvalue()

    def test_continuous_request_serials(self):
        self.assertTrue(validator.continuous_request_serials(["run_1", "run_2", "run_3"]))
        self.assertFalse(validator.continuous_request_serials(["run_1", "run_3"]))
        self.assertTrue(validator._coefficients_close((0.6, 0.3, 0.10000000000000003), (0.6, 0.3, 0.1)))

    def test_scoring_coverage_allows_transiently_inactive_media_path(self):
        all_seen, multipath_seen = validator.scoring_path_coverage(
            [{1, 3}, {1, 3}, {1}, {1, 3}], [1, 3]
        )
        self.assertTrue(all_seen)
        self.assertTrue(multipath_seen)
        self.assertEqual(validator.scoring_path_coverage([{1}, {1}], [1, 3]), (False, False))

    def test_shadow_and_active_results(self):
        for mode in ("shadow", "active"):
            temp, code, output = self._validate(mode=mode)
            try:
                self.assertEqual(code, 0, output)
            finally:
                temp.cleanup()

    def test_active_reload_downgrades_when_applied_near_session_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_validator_fixture(Path(tmp), mode="active", during=True, failed=False)
            dynamic = session / "combined_qaccess_t_dynamic"
            with (dynamic / "control_law_diagnostics.csv").open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["timestamp_ms", "alpha", "beta", "gamma"])
                writer.writeheader()
                writer.writerow({"timestamp_ms": 1_750_000_099_000, "alpha": 0.6, "beta": 0.3, "gamma": 0.1})
            output = StringIO()
            with redirect_stdout(output):
                code = validator.validate_session(session, "active", 90, 150)
            text = output.getvalue()
            self.assertEqual(code, 0, text)
            self.assertIn("INFO applied update occurred near session end", text)
            self.assertIn("downgraded_to_insufficient_post_update_window", text)

    def test_detects_request_write_failed(self):
        temp, code, output = self._validate(failed=True)
        try:
            self.assertNotEqual(code, 0)
            self.assertIn("FAIL no request_write_failed", output)
        finally:
            temp.cleanup()

    def test_detects_no_request_during_deterioration(self):
        temp, code, output = self._validate(during=False)
        try:
            self.assertNotEqual(code, 0)
            self.assertIn("FAIL request during deterioration", output)
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
