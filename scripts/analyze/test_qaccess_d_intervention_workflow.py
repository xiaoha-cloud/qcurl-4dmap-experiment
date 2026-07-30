import csv
import json
import tempfile
import unittest
from pathlib import Path

from build_qaccess_d_intervention_training import TARGET, build_leg
from validate_qaccess_d_intervention_run import validate
from qaccess_t_update_worker import validate_intervention_active_readiness


class InterventionWorkflowTests(unittest.TestCase):
    def make_leg(self, root: Path) -> Path:
        leg = root / "d_intervention_001_candidate_r1"
        leg.mkdir()
        apply_ms = 1_000_000
        metadata = {
            "candidate_id": "candidate",
            "replicate": 1,
            "run_order": 1,
            "seed": 7,
            "path_id": 3,
            "alpha": 0.7,
            "beta": 0.2,
            "gamma": 0.3,
            "intervention_s": 70,
            "intervention_wall_timestamp_ms": apply_ms,
            "tc_log": str(leg / "tc_delay.log"),
        }
        (leg / "intervention_metadata.json").write_text(json.dumps(metadata))
        (leg / "tc_delay.log").write_text(
            "step 1/3 at=0s delay=40ms\nverification_ok:\n"
            "step 2/3 at=50s delay=80ms\nverification_ok:\n"
            "step 3/3 at=100s delay=40ms\nverification_ok:\n"
        )
        fields = [
            "timestamp_ms", "path_id", "bw_bps", "owd_ms", "rtt_latest_ms",
            "delay_gradient_ms", "loss_rate", "lost_bytes_delta", "retrans_bytes_delta",
            "cwnd_bytes", "inflight_bytes", "cwnd_room", "alpha", "beta", "gamma",
        ]
        with (leg / "qaccess_runtime_samples.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for relative_s in range(-12, 18):
                after = relative_s >= 1
                writer.writerow({
                    "timestamp_ms": apply_ms + relative_s * 1000,
                    "path_id": 3,
                    "bw_bps": 20_000_000,
                    "owd_ms": 40,
                    "rtt_latest_ms": 100 if relative_s < 0 else 80,
                    "delay_gradient_ms": 0,
                    "loss_rate": 0,
                    "lost_bytes_delta": 0,
                    "retrans_bytes_delta": 0,
                    "cwnd_bytes": 10000,
                    "inflight_bytes": 5000,
                    "cwnd_room": 5000,
                    "alpha": 0.7 if after else 0.6,
                    "beta": 0.2 if after else 0.3,
                    "gamma": 0.3 if after else 0.1,
                })
        return leg

    def test_reload_validation_and_causal_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg = self.make_leg(Path(tmp))
            result = validate(
                leg / "qaccess_runtime_samples.csv",
                leg / "intervention_metadata.json",
                leg / "intervention_validation.json",
            )
            self.assertTrue(result["valid"])
            row = build_leg(leg, -10, -2, 5, 15)
            self.assertEqual(row[TARGET], 80)
            self.assertEqual(row["pre_rtt_median_ms"], 100)
            self.assertEqual(row["observed_rtt_change_ms"], -20)
            self.assertEqual((row["alpha"], row["beta"], row["gamma"]), (0.7, 0.2, 0.3))

    def test_missing_reload_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            leg = self.make_leg(Path(tmp))
            text = (leg / "qaccess_runtime_samples.csv").read_text()
            text = text.replace(",0.7,0.2,0.3\r\n", ",0.6,0.3,0.1\r\n")
            text = text.replace(",0.7,0.2,0.3\n", ",0.6,0.3,0.1\n")
            (leg / "qaccess_runtime_samples.csv").write_text(text)
            result = validate(
                leg / "qaccess_runtime_samples.csv",
                leg / "intervention_metadata.json",
                leg / "intervention_validation.json",
            )
            self.assertFalse(result["valid"])

    def test_shadow_metadata_fails_closed_for_active_execution(self):
        provenance = {"per_path_active_ready": False, "aggregate_active_ready": False}
        with self.assertRaisesRegex(ValueError, "per_path_active_ready"):
            validate_intervention_active_readiness(provenance, aggregate=True)
        with self.assertRaisesRegex(ValueError, "aggregate_active_ready"):
            validate_intervention_active_readiness(
                {"per_path_active_ready": True, "aggregate_active_ready": False},
                aggregate=True,
            )


if __name__ == "__main__":
    unittest.main()
