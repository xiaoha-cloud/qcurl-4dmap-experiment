#!/usr/bin/env python3
"""Build one offline Q-ACCeSS variant target from aggregated sender samples."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from build_qserver_sender_training import add_future_delta_targets

SUPPORTED_TARGETS = ("delta_owd_1s", "loss_risk_1s")
CORRELATION_FIELDS = [
    "bw_bps",
    "owd_ms",
    "delay_gradient_ms",
    "loss_rate",
    "lost_bytes_delta",
    "retrans_bytes_delta",
    "alpha",
    "beta",
    "gamma",
    "gain",
    "backoff",
]


def target_summary(frame: pd.DataFrame, target: str, *, input_rows: int) -> dict:
    values = pd.to_numeric(frame[target], errors="coerce")
    correlations: dict[str, float | None] = {}
    for column in CORRELATION_FIELDS:
        if column not in frame:
            continue
        other = pd.to_numeric(frame[column], errors="coerce")
        valid = values.notna() & other.notna()
        correlation = values[valid].corr(other[valid]) if valid.sum() >= 2 else np.nan
        correlations[column] = None if not np.isfinite(correlation) else float(correlation)
    return {
        "target": target,
        "target_semantics": {
            "delta_owd_1s": "per_path_future_owd_1s_minus_current_owd",
            "loss_risk_1s": "per_path_sum_lost_retrans_bytes_within_next_1s",
        }[target],
        "target_window_method": (
            "nearest timestamp around t+1s"
            if target == "delta_owd_1s"
            else "next one-second aggregate bin"
        ),
        "input_rows": int(input_rows),
        "rows": int(len(frame)),
        "runs": int(frame["run_id"].nunique()),
        "paths": int(frame["path_id"].nunique()),
        "missing_future_rows_dropped": int(input_rows - len(frame)),
        "target_mean": float(values.mean()),
        "target_std": float(values.std()),
        "target_min": float(values.min()),
        "target_max": float(values.max()),
        "target_zero_fraction": float(values.eq(0).mean()),
        "target_nonzero_fraction": float(values.ne(0).mean()),
        "correlations": correlations,
    }


def add_loss_risk_target(frame: pd.DataFrame, group_columns: list[str]) -> pd.DataFrame:
    pieces: list[pd.DataFrame] = []
    for _, group in frame.groupby(group_columns, sort=False, dropna=False):
        work = group.sort_values("timestamp_ms").copy()
        lost = pd.to_numeric(work["lost_bytes_delta"], errors="coerce").fillna(0).clip(lower=0)
        retrans = pd.to_numeric(work["retrans_bytes_delta"], errors="coerce").fillna(0).clip(lower=0)
        event_bytes = lost + retrans
        work["loss_event_bytes"] = event_bytes
        work["future_loss_risk_1s"] = event_bytes.shift(-1)
        work["loss_risk_1s"] = work["future_loss_risk_1s"]
        pieces.append(work)
    return pd.concat(pieces, ignore_index=True) if pieces else frame.copy()


def build_target(input_path: Path, target: str) -> tuple[pd.DataFrame, dict]:
    source = pd.read_csv(input_path)
    required = {"run_id", "path_id", "timestamp_ms", "owd_ms", "loss_rate"}
    missing = sorted(required - set(source.columns))
    if missing:
        raise ValueError(f"missing required columns: {missing}")
    group_columns = [
        column
        for column in (
            "run_id",
            "connection_id",
            "endpoint_role",
            "path_id",
            "alpha",
            "beta",
            "gamma",
        )
        if column in source.columns
    ]
    if target == "loss_risk_1s":
        required_loss = {"lost_bytes_delta", "retrans_bytes_delta"}
        missing_loss = sorted(required_loss - set(source.columns))
        if missing_loss:
            raise ValueError(f"missing loss-risk columns: {missing_loss}")
        labelled = add_loss_risk_target(source, group_columns)
    else:
        labelled = add_future_delta_targets(source, group_columns)
    output = labelled.dropna(subset=[target]).reset_index(drop=True)
    return output, target_summary(output, target, input_rows=len(source))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--target", choices=SUPPORTED_TARGETS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary-out", type=Path, required=True)
    args = parser.parse_args()

    output, summary = build_target(args.input.resolve(), args.target)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.summary_out.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(args.output, index=False)
    args.summary_out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"[build-variant-target] output={args.output.resolve()}")
    print(f"[build-variant-target] summary={args.summary_out.resolve()}")


if __name__ == "__main__":
    main()
