#!/usr/bin/env python3
"""
Preprocess qaccess_collect CSV before Q-ACCeSS-T Phase 1 RFR training.

Input:  derived/qaccess_training_samples.csv
Output: derived/qaccess_training_samples_clean.csv

next_goodput_bps is reserved for future receiver side goodput labelling and is
ignored in Phase 1 (not used as a feature or target).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = _REPO / "derived" / "qaccess_training_samples.csv"
DEFAULT_OUTPUT = _REPO / "derived" / "qaccess_training_samples_clean.csv"

TARGET = "next_bw_bps"
COEFF_COLS = ["alpha", "beta", "gamma"]
FEATURE_COLS = [
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
NONNEG_CLAMP_COLS = [
    "bw_bps",
    "owd_ms",
    "loss_rate",
    "lost_bytes_delta",
    "retrans_bytes_delta",
    "cwnd_bytes",
    "inflight_bytes",
    "cwnd_room",
    TARGET,
]
INFLIGHT_ACTIVE_MIN = 1024


def _to_numeric(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = df.copy()
    for col in columns:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def _replace_nan_inf(series: pd.Series, fill: float = 0.0) -> pd.Series:
    arr = series.to_numpy(dtype=float, copy=True)
    bad = ~np.isfinite(arr)
    if bad.any():
        arr[bad] = fill
    return pd.Series(arr, index=series.index)


def _active_mask(df: pd.DataFrame) -> pd.Series:
    bw = df["bw_bps"].fillna(0.0)
    owd = df["owd_ms"].fillna(0.0)
    inflight = df["inflight_bytes"].fillna(0.0)
    return (bw > 0) | (owd > 0) | (inflight > INFLIGHT_ACTIVE_MIN)


def _apply_cleaning_steps(df: pd.DataFrame) -> tuple[pd.DataFrame, int, int]:
    """Return cleaned frame (before per-coeff sample) and filter stage row counts."""
    numeric_cols = list({*FEATURE_COLS, TARGET, *COEFF_COLS})
    work = _to_numeric(df, numeric_cols)

    if TARGET not in work.columns:
        raise ValueError(f"missing target column {TARGET!r}")

    work = work.dropna(subset=[TARGET])
    work = work[work[TARGET] >= 0]
    n_after_target = len(work)

    for col in COEFF_COLS:
        if col not in work.columns:
            raise ValueError(f"missing coefficient column {col!r}")
    work = work.dropna(subset=COEFF_COLS)

    for col in FEATURE_COLS:
        if col not in work.columns:
            work[col] = 0.0
        work[col] = _replace_nan_inf(work[col], 0.0)

    for col in NONNEG_CLAMP_COLS:
        if col in work.columns:
            work[col] = work[col].clip(lower=0.0)

    active = _active_mask(work)
    n_after_active = int(active.sum())
    work = work.loc[active].copy()
    return work, n_after_target, n_after_active


def _sample_by_coeff(
    df: pd.DataFrame,
    *,
    max_per_coeff: int,
    random_state: int,
) -> pd.DataFrame:
    if df.empty:
        return df

    parts: list[pd.DataFrame] = []
    for _, group in df.groupby(COEFF_COLS, dropna=False):
        if len(group) <= max_per_coeff:
            parts.append(group)
        else:
            parts.append(group.sample(n=max_per_coeff, random_state=random_state))

    out = pd.concat(parts, ignore_index=True)
    return out.sample(frac=1.0, random_state=random_state).reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Clean and sample qaccess_collect training CSV for Phase 1 RFR",
    )
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument("--max-per-coeff", type=int, default=3000)
    ap.add_argument("--random-state", type=int, default=42)
    args = ap.parse_args()

    input_path = args.input.resolve()
    output_path = args.output.resolve()

    if not input_path.is_file():
        print(f"[error] missing input CSV: {input_path}", file=sys.stderr)
        sys.exit(1)

    raw = pd.read_csv(input_path)
    n_raw = len(raw)

    cleaned, n_after_target, n_after_active = _apply_cleaning_steps(raw)
    n_coeff_groups = (
        cleaned.groupby(COEFF_COLS, dropna=False).ngroups if not cleaned.empty else 0
    )
    final = _sample_by_coeff(
        cleaned,
        max_per_coeff=args.max_per_coeff,
        random_state=args.random_state,
    )
    n_final = len(final)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    final.to_csv(output_path, index=False)
    out_size = output_path.stat().st_size

    print("[preprocess_qaccess_training] summary")
    print(f"  raw rows: {n_raw}")
    print(f"  rows after target filter: {n_after_target}")
    print(f"  rows after active filter: {n_after_active}")
    print(f"  unique coefficient groups: {n_coeff_groups}")
    print(f"  final rows: {n_final}")
    print(f"  output path: {output_path}")
    print(f"  output file size: {out_size} bytes")
    print("[preprocess_qaccess_training] done.")


if __name__ == "__main__":
    main()
