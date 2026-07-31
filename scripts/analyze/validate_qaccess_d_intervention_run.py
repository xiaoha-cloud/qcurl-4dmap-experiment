#!/usr/bin/env python3
"""Fail closed unless sender samples prove the scheduled Path B intervention loaded."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path


def validate(samples: Path, intervention: Path, output: Path, tolerance: float = 1e-4) -> dict[str, object]:
    meta = json.loads(intervention.read_text(encoding="utf-8"))
    path_id = int(meta["path_id"])
    applied_ms = int(meta["intervention_wall_timestamp_ms"])
    target = tuple(float(meta[key]) for key in ("alpha", "beta", "gamma"))
    tc_log = Path(meta["tc_log"])
    tc_text = tc_log.read_text(encoding="utf-8", errors="replace")
    expected_steps = (
        "step 1/3 at=0s delay=40ms",
        "step 2/3 at=50s delay=80ms",
        "step 3/3 at=100s delay=40ms",
    )
    qdisc_profile_valid = all(step in tc_text for step in expected_steps) and tc_text.count("verification_ok:") >= 3
    pre = post = matching = 0
    first_matching_ms = None
    first_sample_ms = None
    last_sample_ms = None
    with samples.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"timestamp_ms", "path_id", "alpha", "beta", "gamma", "rtt_latest_ms"}
        missing = required.difference(reader.fieldnames or ())
        if missing:
            raise ValueError(f"runtime samples missing columns: {sorted(missing)}")
        for row in reader:
            if int(row["path_id"]) != path_id:
                continue
            ts = int(float(row["timestamp_ms"]))
            rtt = float(row["rtt_latest_ms"])
            if not math.isfinite(rtt) or rtt <= 0:
                continue
            first_sample_ms = ts if first_sample_ms is None else min(first_sample_ms, ts)
            last_sample_ms = ts if last_sample_ms is None else max(last_sample_ms, ts)
            coeffs = tuple(float(row[key]) for key in ("alpha", "beta", "gamma"))
            if ts < applied_ms:
                pre += 1
            else:
                post += 1
                if all(abs(left - right) <= tolerance for left, right in zip(coeffs, target)):
                    matching += 1
                    first_matching_ms = ts if first_matching_ms is None else min(first_matching_ms, ts)
    failure_reasons = []
    if not qdisc_profile_valid:
        failure_reasons.append("qdisc_profile_invalid")
    if pre < 3:
        failure_reasons.append("insufficient_pre_intervention_samples")
    if post < 3:
        failure_reasons.append("runtime_samples_ended_before_post_intervention_window")
    if matching < 3:
        failure_reasons.append("candidate_coefficients_not_observed_post_intervention")
    result = {
        "valid": not failure_reasons,
        "path_id": path_id,
        "candidate_id": meta["candidate_id"],
        "target_coefficients": dict(zip(("alpha", "beta", "gamma"), target)),
        "pre_intervention_valid_rtt_samples": pre,
        "post_intervention_valid_rtt_samples": post,
        "matching_post_intervention_samples": matching,
        "first_valid_rtt_timestamp_ms": first_sample_ms,
        "last_valid_rtt_timestamp_ms": last_sample_ms,
        "runtime_sample_span_ms": None if first_sample_ms is None or last_sample_ms is None else last_sample_ms - first_sample_ms,
        "runtime_samples_cover_intervention": last_sample_ms is not None and last_sample_ms >= applied_ms,
        "first_matching_timestamp_ms": first_matching_ms,
        "reload_ack_delay_ms": None if first_matching_ms is None else first_matching_ms - applied_ms,
        "qdisc_profile_valid": qdisc_profile_valid,
        "tc_log": str(tc_log),
        "validation_source": "sender_runtime_samples",
        "failure_reasons": failure_reasons,
    }
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--intervention", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = validate(args.samples, args.intervention, args.output)
    print(json.dumps(result, sort_keys=True))
    if not result["valid"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
