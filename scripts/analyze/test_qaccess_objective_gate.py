#!/usr/bin/env python3
import unittest
import sys
import json
import math
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qaccess_t_update_worker import (
    evaluate_gain_gate,
    evaluate_objective_gate,
    evaluate_policy_gate,
    objective_decision_log_fields,
    _worker_log_line,
)


class ObjectiveAwareGateTest(unittest.TestCase):
    def test_t_requires_absolute_and_relative_thresholds(self) -> None:
        below_relative = evaluate_objective_gate(
            20_000_000, 20_600_000,
            objective="throughput", absolute_threshold=500_000, relative_threshold=0.05,
        )
        self.assertFalse(below_relative["would_apply"])
        below_absolute = evaluate_objective_gate(
            1_000_000, 1_060_000,
            objective="throughput", absolute_threshold=500_000, relative_threshold=0.05,
        )
        self.assertFalse(below_absolute["would_apply"])
        passed = evaluate_objective_gate(
            10_000_000, 10_600_000,
            objective="throughput", absolute_threshold=500_000, relative_threshold=0.05,
        )
        self.assertTrue(passed["would_apply"])

    def test_d_accepts_primary_delay_reduction_without_throughput_gate(self) -> None:
        gate = evaluate_objective_gate(
            -50, -44,
            objective="delay", absolute_threshold=10, relative_threshold=0.10,
        )
        self.assertTrue(gate["would_apply"])
        self.assertFalse(gate["absolute_gate_pass"])
        self.assertTrue(gate["relative_gate_pass"])
        self.assertNotIn("throughput", gate)

    def test_d_rejects_insufficient_reduction(self) -> None:
        gate = evaluate_objective_gate(
            -50, -48,
            objective="delay", absolute_threshold=10, relative_threshold=0.10,
        )
        self.assertFalse(gate["would_apply"])

    def test_l_accepts_primary_risk_reduction_without_throughput_gate(self) -> None:
        gate = evaluate_objective_gate(
            -10_000, -7_000,
            objective="loss", absolute_threshold=4096, relative_threshold=0.25,
        )
        self.assertTrue(gate["would_apply"])
        self.assertFalse(gate["absolute_gate_pass"])
        self.assertTrue(gate["relative_gate_pass"])
        self.assertNotIn("throughput", gate)

    def test_l_rejects_insufficient_risk_reduction(self) -> None:
        gate = evaluate_objective_gate(
            -10_000, -9_000,
            objective="loss", absolute_threshold=4096, relative_threshold=0.25,
        )
        self.assertFalse(gate["would_apply"])

    def test_legacy_policy_delegates_to_unchanged_gate(self) -> None:
        expected = evaluate_gain_gate(
            10_000_000, 10_600_000,
            gate_mode="absolute", min_delta_gain_bps=500_000, min_relative_gain=0.03,
        )
        actual = evaluate_policy_gate(
            10_000_000, 10_600_000,
            gate_policy="legacy", objective="throughput", gate_mode="absolute",
            absolute_threshold=500_000, relative_threshold=0.03,
        )
        self.assertEqual(actual, expected)

    def test_legacy_absolute_relative_and_hybrid_semantics(self) -> None:
        absolute = evaluate_gain_gate(
            10_000_000, 10_600_000,
            gate_mode="absolute", min_delta_gain_bps=500_000, min_relative_gain=0.10,
        )
        relative = evaluate_gain_gate(
            10_000_000, 10_600_000,
            gate_mode="relative", min_delta_gain_bps=700_000, min_relative_gain=0.05,
        )
        hybrid = evaluate_gain_gate(
            10_000_000, 10_600_000,
            gate_mode="hybrid", min_delta_gain_bps=500_000, min_relative_gain=0.10,
        )
        self.assertTrue(absolute["would_apply"])
        self.assertTrue(relative["would_apply"])
        self.assertFalse(hybrid["would_apply"])
        self.assertEqual(
            hybrid["would_apply"],
            hybrid["absolute_gate_pass"] and hybrid["relative_gate_pass"],
        )

    def test_decision_log_fields_for_accept_reject_and_non_trigger(self) -> None:
        req = {
            "controller_variant": "qaccess_l",
            "path_id": 3,
            "trigger_mode": "objective_l",
            "reference_value": 0.001,
            "current_value": 0.003,
            "absolute_change": 0.002,
            "relative_change": 2.0,
            "trigger_streak": 3,
            "triggered": True,
        }
        required = {
            "variant", "path_id", "trigger_mode", "gate_policy", "gate_objective",
            "reference_value", "current_value", "absolute_change", "relative_change",
            "trigger_streak", "triggered", "current_candidate_score", "best_candidate_score",
            "absolute_improvement", "relative_improvement", "gate_passed",
            "actual_applied", "skip_reason",
        }
        cases = (
            dict(current_candidate_score=10_000, best_candidate_score=5_000,
                 absolute_improvement=5_000, relative_improvement=0.5,
                 gate_passed=True, actual_applied=True, skip_reason=""),
            dict(current_candidate_score=10_000, best_candidate_score=9_500,
                 absolute_improvement=500, relative_improvement=0.05,
                 gate_passed=False, actual_applied=False, skip_reason="primary_gate_failed"),
            dict(current_candidate_score=None, best_candidate_score=None,
                 absolute_improvement=None, relative_improvement=None,
                 gate_passed=False, actual_applied=False, skip_reason="objective_trigger_not_satisfied"),
        )
        for values in cases:
            row = objective_decision_log_fields(
                req,
                target_mode="loss_risk_1s",
                gate_policy="objective_aware",
                gate_objective="loss",
                **values,
            )
            self.assertTrue(required.issubset(row))
            self.assertEqual(row["trigger_value_unit"], "ratio_0_to_1")
            self.assertEqual(row["candidate_score_unit"], "loss_risk_bytes")
            self.assertEqual(
                row["secondary_guardrails"],
                "NOT_AVAILABLE_FOR_PRE_UPDATE_EVALUATION",
            )

    def test_decision_log_never_writes_nan_or_infinity(self) -> None:
        req = {
            "controller_variant": "qaccess_t", "path_id": 3,
            "trigger_mode": "objective_t", "reference_value": math.nan,
            "current_value": math.inf, "absolute_change": -math.inf,
            "relative_change": math.nan, "trigger_streak": 1, "triggered": False,
        }
        row = objective_decision_log_fields(
            req,
            target_mode="delta_bw_1s",
            gate_policy="objective_aware",
            gate_objective="throughput",
            current_candidate_score=math.nan,
            best_candidate_score=math.inf,
            absolute_improvement=-math.inf,
            relative_improvement=math.nan,
            gate_passed=False,
            actual_applied=False,
            skip_reason="trigger_streak_incomplete",
        )
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "worker.log"
            _worker_log_line(log, row)
            text = log.read_text(encoding="utf-8")
        self.assertNotIn("NaN", text)
        self.assertNotIn("Infinity", text)
        parsed = json.loads(text)
        self.assertIsNone(parsed["current_candidate_score"])


if __name__ == "__main__":
    unittest.main()
