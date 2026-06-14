#!/usr/bin/env python3
"""
Build windowed Q-ACCeSS-T training CSV with forward-looking bandwidth labels.

Aggregates dense per-tick samples into fixed time windows, then labels each window
with future bandwidth over a real horizon (not the next raw row).

Input:  derived/qaccess_training_samples_coeff_sweep.csv
Output: derived/qaccess_training_samples_coeff_sweep_windowed.csv

Usage:
  python3 scripts/analyze/build_qaccess_windowed_training.py
  python3 scripts/analyze/build_qaccess_windowed_training.py \\
    --input derived/qaccess_training_samples_coeff_sweep.csv \\
    --output derived/qaccess_training_samples_coeff_sweep_windowed.csv \\
    --window-ms 1000 --horizon-windows 1 --future-avg-windows 5
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = _REPO / "derived" / "qaccess_training_samples_coeff_sweep.csv"
DEFAULT_OUTPUT = _REPO / "derived" / "qaccess_training_samples_coeff_sweep_windowed.csv"
DEFAULT_OLIA_OUTPUT = _REPO / "derived" / "qaccess_training_samples_coeff_sweep_olia_windowed.csv"

REQUIRED_COLS = [
    "timestamp_ms",
    "run_id",
    "path_id",
    "bw_bps",
    "owd_ms",
    "delay_gradient_ms",
    "loss_rate",
    "lost_bytes_delta",
    "retrans_bytes_delta",
    "cwnd_bytes",
    "inflight_bytes",
    "cwnd_room",
    "alpha",
    "beta",
    "gamma",
    "utility",
    "gain",
    "backoff",
]

OPTIONAL_COLS = ["sweep_name"]

COEFF_COLS = ["alpha", "beta", "gamma"]

MEAN_COLS = [
    "bw_bps",
    "owd_ms",
    "delay_gradient_ms",
    "loss_rate",
    "cwnd_bytes",
    "inflight_bytes",
    "cwnd_room",
    "utility",
    "gain",
    "backoff",
]

SUM_COLS = ["lost_bytes_delta", "retrans_bytes_delta"]


def _resolve_group_cols(df: pd.DataFrame) -> list[str]:
    cols = []
    if "sweep_name" in df.columns and df["sweep_name"].notna().any():
        cols.append("sweep_name")
    cols.extend(["run_id", "path_id", "alpha", "beta", "gamma"])
    return cols


def _to_numeric(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = df.copy()
    for col in columns:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def _print_label_diagnostics(df: pd.DataFrame, *, label: str) -> None:
    if df.empty:
        print(f"[build_windowed] {label}: no rows")
        return

    work = df.dropna(subset=["bw_bps", "next_bw_bps"]).copy()
    n = len(work)
    if n == 0:
        print(f"[build_windowed] {label}: no valid label rows")
        return

    same = int((work["next_bw_bps"] == work["bw_bps"]).sum())
    frac_same = float(same) / float(n)
    corr = float(work[["bw_bps", "next_bw_bps"]].corr().iloc[0, 1])

    if "delta_bw_1s" in work.columns:
        delta = work["delta_bw_1s"]
    else:
        delta = work["next_bw_bps"] - work["bw_bps"]

    print(f"[build_windowed] {label} total rows: {n}")
    print(f"[build_windowed] {label} fraction next_bw_bps == bw_bps: {frac_same:.6f}")
    print(f"[build_windowed] {label} corr(bw_bps, next_bw_bps): {corr:.6f}")
    print(f"[build_windowed] {label} delta_bw_1s describe:")
    print(delta.describe().to_string())

    if "relative_delta_bw_1s" in work.columns:
        print(f"[build_windowed] {label} relative_delta_bw_1s describe:")
        print(work["relative_delta_bw_1s"].describe().to_string())


def _add_time_windows(df: pd.DataFrame, group_cols: list[str], window_ms: int) -> pd.DataFrame:
    work = df.copy()
    if "run_id" not in work.columns or work["run_id"].isna().all():
        work["run_id"] = "default"
    work["run_id"] = work["run_id"].astype(str)
    if "sweep_name" in work.columns:
        work["sweep_name"] = work["sweep_name"].astype(str)

    work["timestamp_ms"] = pd.to_numeric(work["timestamp_ms"], errors="coerce")
    work = work.dropna(subset=["timestamp_ms"])

    min_ts = work.groupby(group_cols, sort=False)["timestamp_ms"].transform("min")
    work["time_s"] = np.floor((work["timestamp_ms"] - min_ts) / float(window_ms)).astype(np.int64)
    return work


def _aggregate_windows(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    agg: dict[str, str] = {"timestamp_ms": "min"}
    for col in MEAN_COLS:
        agg[col] = "mean"
    for col in SUM_COLS:
        agg[col] = "sum"

    return (
        df.groupby(group_cols + ["time_s"], as_index=False, sort=False)
        .agg(agg)
        .sort_values(group_cols + ["time_s"], kind="mergesort")
        .reset_index(drop=True)
    )


def _add_future_labels(
    df: pd.DataFrame,
    group_cols: list[str],
    *,
    horizon_windows: int,
    future_avg_windows: int,
) -> pd.DataFrame:
    work = df.copy()
    grouped = work.groupby(group_cols, sort=False)

    work["future_bw_1s"] = grouped["bw_bps"].shift(-horizon_windows)

    fwd_parts = [grouped["bw_bps"].shift(-offset) for offset in range(1, future_avg_windows + 1)]
    work["future_bw_5s"] = pd.concat(fwd_parts, axis=1).mean(axis=1)

    work["delta_bw_1s"] = work["future_bw_1s"] - work["bw_bps"]
    work["relative_delta_bw_1s"] = work["delta_bw_1s"] / np.maximum(work["bw_bps"], 1.0)
    work["next_bw_bps"] = work["future_bw_1s"]
    return work


def build_windowed_dataset(
    df: pd.DataFrame,
    *,
    window_ms: int,
    horizon_windows: int,
    future_avg_windows: int,
    min_path_id: int = 0,
    min_bw_bps_relative: float = 0.0,
) -> pd.DataFrame:
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"missing required columns: {missing}")

    keep_cols = [c for c in REQUIRED_COLS + OPTIONAL_COLS if c in df.columns]
    work = _to_numeric(df[keep_cols].copy(), REQUIRED_COLS)
    work = work.dropna(subset=["path_id", "bw_bps", *COEFF_COLS])
    work["path_id"] = work["path_id"].astype(int)
    if min_path_id > 0:
        work = work.loc[work["path_id"] >= min_path_id].copy()

    group_cols = _resolve_group_cols(work)
    work = _add_time_windows(work, group_cols, window_ms)
    windowed = _aggregate_windows(work, group_cols)
    windowed = _add_future_labels(
        windowed,
        group_cols,
        horizon_windows=horizon_windows,
        future_avg_windows=future_avg_windows,
    )
    windowed = windowed.dropna(subset=["future_bw_1s"]).reset_index(drop=True)

    if min_bw_bps_relative > 0 and not windowed.empty:
        windowed = windowed.loc[windowed["bw_bps"] >= min_bw_bps_relative].copy()
        windowed = windowed.replace([np.inf, -np.inf], np.nan)
        windowed = windowed.dropna(subset=["relative_delta_bw_1s"]).reset_index(drop=True)

    return windowed


def _print_group_counts(df: pd.DataFrame) -> None:
    if df.empty:
        return

    if "sweep_name" in df.columns:
        print("\n[build_windowed] rows per sweep_name:")
        per_sweep = df.groupby("sweep_name", dropna=False).size().reset_index(name="rows")
        print(per_sweep.to_string(index=False))

    print("\n[build_windowed] rows per coefficient combination:")
    combo = (
        df.groupby(COEFF_COLS, dropna=False)
        .size()
        .reset_index(name="rows")
        .sort_values(COEFF_COLS)
    )
    print(combo.to_string(index=False))

    print("\n[build_windowed] rows per path_id:")
    per_path = df.groupby("path_id", dropna=False).size().reset_index(name="rows")
    print(per_path.to_string(index=False))

    if "run_id" in df.columns:
        print("\n[build_windowed] rows per run_id:")
        per_run = df.groupby("run_id", dropna=False).size().reset_index(name="rows")
        print(per_run.to_string(index=False))

        print("\n[build_windowed] rows per (run_id, path_id):")
        per_run_path = (
            df.groupby(["run_id", "path_id"], dropna=False)
            .size()
            .reset_index(name="rows")
            .sort_values(["run_id", "path_id"])
        )
        print(per_run_path.to_string(index=False))


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Aggregate Q-ACCeSS samples into time windows with forward bandwidth labels",
    )
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument("--window-ms", type=int, default=1000, help="Aggregation window size in ms")
    ap.add_argument(
        "--horizon-windows",
        type=int,
        default=1,
        help="Forward label horizon in windows (default 1 = next 1s window)",
    )
    ap.add_argument(
        "--future-avg-windows",
        type=int,
        default=5,
        help="Number of forward windows averaged into future_bw_5s",
    )
    ap.add_argument(
        "--min-path-id",
        type=int,
        default=0,
        help="Drop rows with path_id < this value (use 1 to exclude Cubic path 0)",
    )
    ap.add_argument(
        "--min-bw-bps-relative",
        type=float,
        default=0.0,
        help="Drop rows with bw_bps below this before keeping relative_delta_bw_1s",
    )
    ap.add_argument(
        "--olia-only",
        action="store_true",
        help="Shortcut for --min-path-id 1 and default OLIA output path",
    )
    args = ap.parse_args()

    if args.olia_only:
        args.min_path_id = max(args.min_path_id, 1)
        if args.output == DEFAULT_OUTPUT:
            args.output = DEFAULT_OLIA_OUTPUT
        if args.min_bw_bps_relative <= 0:
            args.min_bw_bps_relative = 100_000.0

    if args.window_ms <= 0:
        print("[build_windowed] error: --window-ms must be > 0", file=sys.stderr)
        sys.exit(1)
    if args.horizon_windows <= 0:
        print("[build_windowed] error: --horizon-windows must be > 0", file=sys.stderr)
        sys.exit(1)
    if args.future_avg_windows <= 0:
        print("[build_windowed] error: --future-avg-windows must be > 0", file=sys.stderr)
        sys.exit(1)

    input_path = args.input.resolve()
    output_path = args.output.resolve()

    if not input_path.is_file():
        print(f"[build_windowed] error: missing input CSV: {input_path}", file=sys.stderr)
        sys.exit(1)

    print(f"[build_windowed] input: {input_path}")
    print(
        f"[build_windowed] window_ms={args.window_ms} "
        f"horizon_windows={args.horizon_windows} "
        f"future_avg_windows={args.future_avg_windows} "
        f"min_path_id={args.min_path_id} "
        f"min_bw_bps_relative={args.min_bw_bps_relative}"
    )

    optional_read = set(OPTIONAL_COLS + ["next_bw_bps"])
    raw = pd.read_csv(input_path, usecols=lambda c: c in set(REQUIRED_COLS) | optional_read)
    n_raw = len(raw)
    print(f"[build_windowed] raw rows: {n_raw}")

    if "next_bw_bps" in raw.columns:
        raw_diag = _to_numeric(raw, ["bw_bps", "next_bw_bps"]).dropna(subset=["bw_bps", "next_bw_bps"])
        if raw_diag.empty:
            print("[build_windowed] raw (before): no valid next_bw_bps labels in input")
        else:
            _print_label_diagnostics(raw_diag, label="raw (before)")
    else:
        print("[build_windowed] raw (before): input has no next_bw_bps column; skipping raw label stats")

    windowed = build_windowed_dataset(
        raw,
        window_ms=args.window_ms,
        horizon_windows=args.horizon_windows,
        future_avg_windows=args.future_avg_windows,
        min_path_id=args.min_path_id,
        min_bw_bps_relative=args.min_bw_bps_relative,
    )
    n_windowed = len(windowed)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    windowed.to_csv(output_path, index=False)
    out_size = output_path.stat().st_size

    print(f"\n[build_windowed] windowed rows: {n_windowed}")
    _print_label_diagnostics(windowed, label="windowed (after)")
    _print_group_counts(windowed)

    print(f"\n[build_windowed] wrote {output_path} ({n_windowed} rows, {out_size} bytes)")
    print("[build_windowed] done.")


if __name__ == "__main__":
    main()
