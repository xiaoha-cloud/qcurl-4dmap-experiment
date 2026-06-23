#!/usr/bin/env python3
"""
Validate next_bw_bps labels in Q-ACCeSS-T sample CSVs.

Usage:
  python3 scripts/analyze/validate_qaccess_next_labels.py
  python3 scripts/analyze/validate_qaccess_next_labels.py derived/qaccess_runtime_samples.csv
  python3 scripts/analyze/validate_qaccess_next_labels.py \\
    derived/qaccess_training_samples_coeff_sweep_windowed.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
DEFAULT_CSV = _REPO / "derived" / "qaccess_training_samples.csv"

FRAC_SAME_FAIL_THRESHOLD = 0.95


def validate_labels(csv_path: Path) -> int:
    if not csv_path.is_file():
        print(f"[validate_next_labels] error: missing file: {csv_path}", file=sys.stderr)
        return 1

    df = pd.read_csv(csv_path)
    required = ["path_id", "bw_bps", "next_bw_bps"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        print(f"[validate_next_labels] error: missing columns {missing}", file=sys.stderr)
        return 1

    for col in required:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["bw_bps", "next_bw_bps"])
    n = len(df)
    if n == 0:
        print("[validate_next_labels] error: no valid rows", file=sys.stderr)
        return 1

    same = (df["next_bw_bps"] == df["bw_bps"]).sum()
    frac_same = float(same) / float(n)
    corr = float(df[["bw_bps", "next_bw_bps"]].corr().iloc[0, 1])

    if "delta_bw_1s" in df.columns:
        df["delta_bw"] = pd.to_numeric(df["delta_bw_1s"], errors="coerce")
    else:
        df["delta_bw"] = df["next_bw_bps"] - df["bw_bps"]

    print(f"[validate_next_labels] file: {csv_path}")
    print(f"[validate_next_labels] total rows: {n}")
    print(f"[validate_next_labels] fraction next_bw_bps == bw_bps: {frac_same:.6f}")
    print(f"[validate_next_labels] corr(bw_bps, next_bw_bps): {corr:.6f}")
    print("[validate_next_labels] delta_bw describe:")
    print(df["delta_bw"].describe().to_string())

    if "relative_delta_bw_1s" in df.columns:
        rel = pd.to_numeric(df["relative_delta_bw_1s"], errors="coerce")
        print("\n[validate_next_labels] relative_delta_bw_1s describe:")
        print(rel.describe().to_string())

    if "path_id" in df.columns:
        print("\n[validate_next_labels] per path_id delta_bw stats:")
        per_path = (
            df.groupby("path_id")["delta_bw"]
            .agg(["count", "mean", "std", "min", "max"])
            .reset_index()
        )
        print(per_path.to_string(index=False))

    delta_std = float(df["delta_bw"].std())
    print(f"\n[validate_next_labels] delta_bw std: {delta_std:.6f}")

    if frac_same > FRAC_SAME_FAIL_THRESHOLD:
        print(
            f"\n[validate_next_labels] FAIL: next_bw_bps too often equals bw_bps "
            f"(fraction={frac_same:.6f} > {FRAC_SAME_FAIL_THRESHOLD}).",
            file=sys.stderr,
        )
        return 2

    all_delta_zero = bool((df["delta_bw"] == 0).all())
    if not np.isfinite(delta_std) or (delta_std == 0.0 and all_delta_zero):
        print(
            "\n[validate_next_labels] FAIL: delta_bw has zero variance "
            "(all labels identical to current bw_bps).",
            file=sys.stderr,
        )
        return 2

    print(
        "\n[validate_next_labels] PASS: labels show usable bandwidth variation. "
        f"(corr={corr:.6f} is acceptable for windowed data.)"
    )
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Validate next_bw_bps labels")
    ap.add_argument(
        "csv",
        nargs="?",
        type=Path,
        default=DEFAULT_CSV,
        help="Sample CSV to validate",
    )
    args = ap.parse_args()
    raise SystemExit(validate_labels(args.csv.resolve()))


if __name__ == "__main__":
    main()
