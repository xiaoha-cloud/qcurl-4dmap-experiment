#!/usr/bin/env python3
"""Build one causal clean-D training row per validated real intervention."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from qaccess_math import normalize_d, normalize_g, normalize_l, qaccess_gain_backoff, qaccess_utility  # noqa: E402

FEATURES = [
    "bw_bps", "owd_ms", "delay_gradient_ms", "loss_rate", "lost_bytes_delta",
    "retrans_bytes_delta", "cwnd_bytes", "inflight_bytes", "cwnd_room",
    "alpha", "beta", "gamma", "utility", "gain", "backoff",
]
STATE_FEATURES = FEATURES[:9]
TARGET = "candidate_post_rtt_median_ms"


def finite(value: object) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"non-finite value: {value!r}")
    return result


def median_rows(rows: list[dict[str, str]], field: str) -> float:
    values = [finite(row[field]) for row in rows if row.get(field, "") != ""]
    if not values:
        raise ValueError(f"no finite values for {field}")
    return float(statistics.median(values))


def build_leg(leg: Path, pre_start: float, pre_end: float, post_start: float, post_end: float) -> dict[str, object]:
    validation = json.loads((leg / "intervention_validation.json").read_text(encoding="utf-8"))
    if validation.get("valid") is not True:
        raise ValueError("reload validation is not valid")
    intervention = json.loads((leg / "intervention_metadata.json").read_text(encoding="utf-8"))
    apply_ms = int(intervention["intervention_wall_timestamp_ms"])
    path_id = int(intervention["path_id"])
    with (leg / "qaccess_runtime_samples.csv").open(newline="", encoding="utf-8") as handle:
        rows = [
            row for row in csv.DictReader(handle)
            if int(row["path_id"]) == path_id and finite(row["rtt_latest_ms"]) > 0
        ]
    relative = [(finite(row["timestamp_ms"]) - apply_ms) / 1000.0 for row in rows]
    pre = [row for row, seconds in zip(rows, relative) if pre_start <= seconds <= pre_end]
    post = [row for row, seconds in zip(rows, relative) if post_start <= seconds <= post_end]
    if len(pre) < 3 or len(post) < 3:
        raise ValueError(f"insufficient windows: pre={len(pre)} post={len(post)}")

    result: dict[str, object] = {field: median_rows(pre, field) for field in STATE_FEATURES}
    alpha, beta, gamma = (finite(intervention[key]) for key in ("alpha", "beta", "gamma"))
    norm_g = normalize_g(float(result["bw_bps"]))
    norm_d = normalize_d(float(result["owd_ms"]), float(result["delay_gradient_ms"]))
    norm_l = normalize_l(float(result["loss_rate"]))
    gain, backoff = qaccess_gain_backoff(norm_g, norm_d, norm_l, alpha, beta, gamma)
    result.update({
        "alpha": alpha,
        "beta": beta,
        "gamma": gamma,
        "utility": qaccess_utility(norm_g, norm_d, norm_l, alpha, beta, gamma),
        "gain": gain,
        "backoff": backoff,
        TARGET: median_rows(post, "rtt_latest_ms"),
        "pre_rtt_median_ms": median_rows(pre, "rtt_latest_ms"),
        "observed_rtt_change_ms": median_rows(post, "rtt_latest_ms") - median_rows(pre, "rtt_latest_ms"),
        "run_id": leg.name,
        "candidate_id": intervention["candidate_id"],
        "replicate": intervention["replicate"],
        "run_order": intervention["run_order"],
        "path_id": path_id,
        "intervention_s": intervention["intervention_s"],
        "intervention_wall_timestamp_ms": apply_ms,
        "pre_sample_count": len(pre),
        "post_sample_count": len(post),
        "reload_ack_delay_ms": validation.get("reload_ack_delay_ms"),
        "label_source": "sender_rtt_latest_ms_post_intervention_median",
    })
    return result


def build_session(session: Path, strict: bool = True) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    rows, exclusions = [], []
    for validation in sorted(session.glob("d_intervention_*/intervention_validation.json")):
        leg = validation.parent
        try:
            rows.append(build_leg(leg, -10.0, -2.0, 5.0, 15.0))
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            exclusions.append({"run_id": leg.name, "reason": str(exc)})
    if strict and exclusions:
        raise ValueError(f"{len(exclusions)} invalid intervention runs; see exclusion report")
    return rows, exclusions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exclusions-out", type=Path)
    parser.add_argument("--allow-exclusions", action="store_true")
    args = parser.parse_args()
    rows, exclusions = build_session(args.session.resolve(), strict=False)
    exclusions_out = args.exclusions_out or args.output.with_name(args.output.stem + "_exclusions.csv")
    exclusions_out.parent.mkdir(parents=True, exist_ok=True)
    with exclusions_out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=("run_id", "reason"))
        writer.writeheader()
        writer.writerows(exclusions)
    if exclusions and not args.allow_exclusions:
        raise SystemExit(f"[error] excluded {len(exclusions)} runs; inspect {exclusions_out}")
    if not rows:
        raise SystemExit("[error] no valid intervention rows")
    fields = FEATURES + [
        TARGET, "pre_rtt_median_ms", "observed_rtt_change_ms", "run_id", "candidate_id",
        "replicate", "run_order", "path_id", "intervention_s", "intervention_wall_timestamp_ms", "pre_sample_count",
        "post_sample_count", "reload_ack_delay_ms", "label_source",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"rows": len(rows), "excluded": len(exclusions), "target": TARGET, "output": str(args.output)}, sort_keys=True))


if __name__ == "__main__":
    main()
