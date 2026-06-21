from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import summarize_gate_experiments as summary


def _write_session(root: Path, name: str, *, gate_mode: str, execution_mode: str, verdict: str,
                   updates_applied: bool, applied_count: int, phases: list[str],
                   baseline_during: float, dynamic_during: float,
                   path_b_rows: list[dict], request_write_failed: int = 0) -> Path:
    session = root / name
    session.mkdir(parents=True)
    (session / "experiment_metadata.json").write_text(json.dumps({
        "gate_mode": gate_mode,
        "execution_mode": execution_mode,
        "min_relative_gain": 0.03,
        "min_delta_gain_bps": 100000 if gate_mode == "hybrid" else 500000,
    }))
    compare = {
        "gate_mode": gate_mode,
        "dynamic_updates_applied": updates_applied,
        "applied_update_count": applied_count,
        "applied_request_classifications": phases,
        "arms": {
            "baseline": {"total": {"PRE_70_90": 1.0, "DURING_90_150": baseline_during, "POST_150_200": 3.0}},
            "dynamic": {"total": {"PRE_70_90": 1.5, "DURING_90_150": dynamic_during, "POST_150_200": 3.5}},
        },
        "observed_during_difference_mbps": dynamic_during - baseline_during,
        "observed_during_relative_difference": (dynamic_during - baseline_during) / baseline_during,
        "verdict": verdict,
        "request_serial_continuity": True,
        "request_write_failed": request_write_failed,
        "path_b_activity": [{"artifact": "eligibility.json", "path_b": path_b_rows}],
    }
    (session / "baseline_vs_dynamic_relative_comparison.json").write_text(json.dumps(compare))
    worker_rows = []
    for index, phase in enumerate(phases, start=1):
        worker_rows.append({
            "request_id": f"req_{index}",
            "request_classification": phase,
            "actual_applied": updates_applied,
        })
    (session / "worker.log").write_text("".join(json.dumps(row) + "\n" for row in worker_rows))
    return session


class SummaryTests(unittest.TestCase):
    def test_build_summary_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            validation_dir = root / "validation"
            validation_dir.mkdir()
            shadow = _write_session(
                root, "relative_shadow", gate_mode="relative", execution_mode="shadow",
                verdict="dynamic_better", updates_applied=False, applied_count=0, phases=[],
                baseline_during=4.0, dynamic_during=5.0,
                path_b_rows=[{"eligible": True, "exclusion_reason": ""}],
            )
            active_bad = _write_session(
                root, "relative_active", gate_mode="relative", execution_mode="active",
                verdict="dynamic_worse", updates_applied=True, applied_count=2,
                phases=["PRE_DETERIORATION", "PRE_DETERIORATION"],
                baseline_during=4.0, dynamic_during=3.0,
                path_b_rows=[{"eligible": True, "exclusion_reason": ""}],
            )
            hybrid = _write_session(
                root, "hybrid_active", gate_mode="hybrid", execution_mode="active",
                verdict="dynamic_better", updates_applied=True, applied_count=2,
                phases=["PRE_DETERIORATION", "DURING_DETERIORATION"],
                baseline_during=5.465818, dynamic_during=7.598039,
                path_b_rows=[
                    {"eligible": True, "exclusion_reason": ""},
                    {"eligible": False, "exclusion_reason": "no_sender_byte_growth"},
                ],
            )
            (validation_dir / "hybrid_active_active_validate.log").write_text(
                "PASS controller coefficient reload confirmed\nSUMMARY PASS failures=0\n",
                encoding="utf-8",
            )

            result = summary.build_summary(
                {
                    "Relative Shadow": shadow,
                    "Relative Active": active_bad,
                    "Hybrid Active": hybrid,
                },
                validation_dir,
            )
            self.assertEqual(result["final_candidate_session"], "hybrid_active")
            hybrid_row = next(row for row in result["sessions"] if row["label"] == "Hybrid Active")
            self.assertTrue(hybrid_row["controller_reload_confirmed"])
            self.assertIn("traffic migration away from the impaired path", hybrid_row["path_b_migration_notes"])
            self.assertEqual(hybrid_row["applied_request_phases"], ["PRE_DETERIORATION", "DURING_DETERIORATION"])

            markdown = summary.render_markdown(result)
            self.assertIn("| Hybrid Active |", markdown)
            self.assertIn("final candidate; blocks low-confidence updates", markdown)
            self.assertIn("`7.598039 Mbps`", markdown)


if __name__ == "__main__":
    unittest.main()
