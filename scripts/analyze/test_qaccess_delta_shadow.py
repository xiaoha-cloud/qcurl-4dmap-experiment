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


class CoefficientSensitiveModel:
    feature_names_in_ = np.array(worker.FEATURES)
    n_features_in_ = len(worker.FEATURES)

    def predict(self, frame):
        return (
            frame["alpha"].to_numpy() * 10_000_000
            - frame["beta"].to_numpy() * 2_000_000
            - frame["gamma"].to_numpy() * 1_000_000
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
    })
    pd.DataFrame([row]).to_csv(paths["samples.csv"], index=False)
    paths["coeffs.json"].write_text(json.dumps({
        "alpha": 0.6, "beta": 0.3, "gamma": 0.1, "source": "initial"
    }), encoding="utf-8")
    return paths


class WorkerTests(unittest.TestCase):
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
            self.assertEqual(response["status"], "SHADOW_WOULD_APPLY")
            self.assertTrue(response["would_apply"])
            self.assertEqual(response["candidate_count"], 27)
            self.assertGreater(response["unique_prediction_count"], 1)
            self.assertGreater(response["score_gain_bps"], 500_000)
            self.assertTrue(response["proposed_stepped_coefficients"])
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
    t0 = datetime_from_ms(t0_ms)
    (dynamic / "tc_deterioration.log").write_text(
        f"[{t0}] step 1/3 at=0s\n[{t0}] step 2/3 at=90s\n[{t0}] step 3/3 at=150s\n"
        f"[{t0}] finished all steps\n[{t0}] exiting status=0 current_step=3 completed=1\n"
    )
    with (processed / "qaccess_candidate_scores_run_1_path0.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["mean_prediction"])
        writer.writeheader(); writer.writerow({"mean_prediction": 1}); writer.writerow({"mean_prediction": 2})
    status = "SHADOW_WOULD_APPLY" if mode == "shadow" else "APPLIED"
    (session / "worker.log").write_text(json.dumps({
        "request_id": "run_1", "status": status, "would_apply": True,
        "proposed_stepped_coefficients": {"alpha": 0.7, "beta": 0.2, "gamma": 0.2},
    }) + "\n")
    before = dict(worker=mode, **{"alpha": 0.6, "beta": 0.3, "gamma": 0.1})
    after = dict(before) if mode == "shadow" else {"version": 1, "default": {"alpha": 0.6, "beta": 0.3, "gamma": 0.1}, "paths": {"0": {"alpha": 0.7, "beta": 0.2, "gamma": 0.2}}}
    (session / "combined_qaccess_t_dynamic_coeffs_before.json").write_text(json.dumps(before))
    (session / "combined_qaccess_t_dynamic_coeffs_after.json").write_text(json.dumps(after))
    if mode == "active":
        with (dynamic / "control_law_diagnostics.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["alpha", "beta", "gamma"])
            writer.writeheader(); writer.writerow({"alpha": 0.7, "beta": 0.2, "gamma": 0.2})
    return session


def datetime_from_ms(value: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


class ValidatorTests(unittest.TestCase):
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

    def test_shadow_and_active_results(self):
        for mode in ("shadow", "active"):
            temp, code, output = self._validate(mode=mode)
            try:
                self.assertEqual(code, 0, output)
            finally:
                temp.cleanup()

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
