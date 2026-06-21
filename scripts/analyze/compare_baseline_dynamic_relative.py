#!/usr/bin/env python3
"""Compare observed Baseline and Dynamic Utility throughput for one paired session."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
import pandas as pd

WINDOWS = {"PRE_70_90": (70, 90), "DURING_90_150": (90, 150), "POST_150_200": (150, 200)}
ARMS = {"baseline": "combined_baseline", "dynamic": "combined_qaccess_t_dynamic"}
FILES = {"total": "throughput_all_down.csv", "path_a": "throughput_pathA_down.csv", "path_b": "throughput_pathB_down.csv"}


def means(path: Path) -> dict[str, float]:
    df = pd.read_csv(path)
    return {label: float(df[(df.elapsed_s >= lo) & (df.elapsed_s < hi)].throughput_mbps.mean())
            for label, (lo, hi) in WINDOWS.items()}


def continuous(ids: list[str]) -> bool:
    serials = [int(re.search(r"_(\d+)$", value).group(1)) for value in ids]
    return serials == list(range(1, len(serials) + 1))


def analyze(session: Path) -> dict:
    report: dict = {"session": str(session.resolve()), "arms": {}, "fixed_utility_arm": False}
    metadata_path = session / "experiment_metadata.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
    report["gate_mode"] = metadata.get("gate_mode")
    report["min_relative_gain"] = metadata.get("min_relative_gain")
    report["min_delta_gain_bps"] = metadata.get("min_delta_gain_bps")
    for arm, directory in ARMS.items():
        report["arms"][arm] = {metric: means(session / directory / filename) for metric, filename in FILES.items()}
        total = report["arms"][arm]["total"]
        report["arms"][arm]["total_utilization_fraction_of_50mbps"] = {key: value / 50.0 for key, value in total.items()}
        report["arms"][arm]["throughput_drop_mbps"] = total["DURING_90_150"] - total["PRE_70_90"]
        report["arms"][arm]["recovery_mbps"] = total["POST_150_200"] - total["DURING_90_150"]
    base = report["arms"]["baseline"]["total"]["DURING_90_150"]
    dynamic = report["arms"]["dynamic"]["total"]["DURING_90_150"]
    relative = (dynamic - base) / max(abs(base), 1e-9)
    report["observed_during_difference_mbps"] = dynamic - base
    report["observed_during_relative_difference"] = relative
    observed_direction = "dynamic_better" if relative > 0.02 else ("dynamic_worse" if relative < -0.02 else "inconclusive")
    report["observed_throughput_direction"] = observed_direction
    worker_rows = [json.loads(line) for line in (session / "worker.log").read_text().splitlines() if line.strip()]
    report["coefficient_events"] = [{key: row.get(key) for key in (
        "request_id", "timestamp_ms", "request_classification", "status", "gate_mode", "absolute_gain_bps",
        "relative_gain", "would_apply_under_gate", "actual_applied", "eligible_path_ids",
        "traffic_weighted_proposed_candidate", "traffic_weighted_proposed_stepped_coefficients", "applied_coefficients")}
        for row in worker_rows]
    applied = any(bool(row.get("actual_applied")) for row in worker_rows)
    report["dynamic_updates_applied"] = applied
    applied_rows = [row for row in worker_rows if bool(row.get("actual_applied"))]
    report["applied_update_count"] = len(applied_rows)
    report["applied_request_classifications"] = [str(row.get("request_classification") or "UNKNOWN") for row in applied_rows]
    blocked_pre = [row for row in worker_rows if (
        row.get("request_classification") == "PRE_DETERIORATION"
        and row.get("relative_gate_pass") is True
        and row.get("absolute_gate_pass") is False
        and not row.get("actual_applied")
    )]
    report["pre_small_gain_updates_blocked"] = bool(blocked_pre)
    report["blocked_pre_request_ids"] = [str(row.get("request_id")) for row in blocked_pre]
    report["baseline_during_mbps"] = report["arms"]["baseline"]["total"]["DURING_90_150"]
    report["dynamic_during_mbps"] = report["arms"]["dynamic"]["total"]["DURING_90_150"]
    report["verdict"] = observed_direction if applied else "inconclusive_no_active_update"
    request_ids = [str(row.get("request_id")) for row in worker_rows if row.get("request_id")]
    report["request_serial_continuity"] = continuous(request_ids) if request_ids else False
    report["request_write_failed"] = sum('"trigger_decision":"request_write_failed"' in line.replace(" ", "")
                                         for line in (session / "qaccess_trigger_audit.jsonl").read_text().splitlines())
    eligibility = []
    for path in sorted(session.rglob("qaccess_path_eligibility_*.json")):
        rows = json.loads(path.read_text())
        eligibility.append({"artifact": path.name, "path_b": [row for row in rows if row.get("physical_path") == "Path B"]})
    report["path_b_activity"] = eligibility
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = analyze(args.session)
    text = json.dumps(report, indent=2)
    print(text)
    output = args.output or args.session / "baseline_vs_dynamic_relative_comparison.json"
    try:
        output.write_text(text + "\n", encoding="utf-8")
    except PermissionError as exc:
        raise SystemExit(
            f"cannot write {output}: {exc}; repair session ownership or pass --output to a writable path"
        )
    print(f"[compare] wrote {output} verdict={report['verdict']}")


if __name__ == "__main__":
    main()
