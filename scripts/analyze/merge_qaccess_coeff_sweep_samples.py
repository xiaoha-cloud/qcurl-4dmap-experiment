#!/usr/bin/env python3
"""
Merge per-coefficient sweep sample CSVs into one training file.

Input:  derived/coeff_sweep/qaccess_samples_<name>.csv
Output: derived/qaccess_training_samples_coeff_sweep.csv

Post-train diagnostics (after train_qaccess_t.py):
  python3 scripts/analyze/merge_qaccess_coeff_sweep_samples.py --diagnose-importance \\
    --importance derived/qaccess_t_feature_importance_coeff_sweep.csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
DEFAULT_INDIR = _REPO / "derived" / "coeff_sweep"
DEFAULT_OUT = _REPO / "derived" / "qaccess_training_samples_coeff_sweep.csv"

REQUIRED_COLS = [
    "alpha",
    "beta",
    "gamma",
    "utility",
    "gain",
    "backoff",
    "next_bw_bps",
]

COEFF_COLS = ["alpha", "beta", "gamma"]

DIAG_FEATURES = [
    "alpha",
    "beta",
    "gamma",
    "utility",
    "gain",
    "backoff",
    "bw_bps",
    "cwnd_bytes",
    "inflight_bytes",
    "cwnd_room",
]

IMPORTANCE_WARN_THRESHOLD = 0.01


def _sweep_name_from_path(path: Path) -> str:
    stem = path.stem
    prefix = "qaccess_samples_"
    if stem.startswith(prefix):
        return stem[len(prefix) :]
    return stem


def _load_sweep_file(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"{path}: missing required columns {missing}")
    df = df.copy()
    df["sweep_name"] = _sweep_name_from_path(path)
    for col in COEFF_COLS + ["utility", "gain", "backoff", "next_bw_bps"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["next_bw_bps"])
    return df


def merge_sweep_samples(indir: Path, out_path: Path) -> pd.DataFrame:
    files = sorted(indir.glob("qaccess_samples_*.csv"))
    if not files:
        print(f"[merge_coeff_sweep] error: no qaccess_samples_*.csv under {indir}", file=sys.stderr)
        sys.exit(1)

    parts: list[pd.DataFrame] = []
    for f in files:
        part = _load_sweep_file(f)
        print(f"[merge_coeff_sweep] {f.name}: {len(part)} rows (after next_bw_bps filter)")
        parts.append(part)

    merged = pd.concat(parts, ignore_index=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(out_path, index=False)
    print(f"[merge_coeff_sweep] wrote {out_path} ({len(merged)} rows, {out_path.stat().st_size} bytes)")
    return merged


def print_merge_summary(df: pd.DataFrame) -> None:
    print("\n[merge_coeff_sweep] row count per sweep_name:")
    print(df.groupby("sweep_name", dropna=False).size().sort_index().to_string())

    print("\n[merge_coeff_sweep] row count per (alpha, beta, gamma):")
    combo = (
        df.groupby(COEFF_COLS, dropna=False)
        .size()
        .reset_index(name="rows")
        .sort_values(COEFF_COLS)
    )
    print(combo.to_string(index=False))

    print("\n[merge_coeff_sweep] unique coefficient values:")
    for col in COEFF_COLS:
        vals = sorted(df[col].dropna().unique())
        print(f"  {col}: {vals}")

    print(f"\n[merge_coeff_sweep] total rows: {len(df)}")
    print(f"[merge_coeff_sweep] unique (alpha,beta,gamma) combos: {df.groupby(COEFF_COLS).ngroups}")


def diagnose_importance(importance_path: Path) -> None:
    if not importance_path.is_file():
        print(f"[merge_coeff_sweep] error: missing importance CSV: {importance_path}", file=sys.stderr)
        sys.exit(1)

    imp = pd.read_csv(importance_path)
    if "feature" not in imp.columns or "importance" not in imp.columns:
        print(f"[merge_coeff_sweep] error: expected feature,importance columns in {importance_path}", file=sys.stderr)
        sys.exit(1)

    subset = imp[imp["feature"].isin(DIAG_FEATURES)].copy()
    subset = subset.sort_values("importance", ascending=False)
    print("\n[merge_coeff_sweep] feature importance (coeff sweep model):")
    print(subset.to_string(index=False))

    coeff_imp = imp[imp["feature"].isin(COEFF_COLS)]
    max_coeff = float(coeff_imp["importance"].max()) if not coeff_imp.empty else 0.0
    if max_coeff < IMPORTANCE_WARN_THRESHOLD:
        print(
            "\n[merge_coeff_sweep] WARNING: The RF still appears insensitive to coefficient features.",
            file=sys.stderr,
        )
        print(
            f"[merge_coeff_sweep] WARNING: max(alpha,beta,gamma) importance={max_coeff:.6f} "
            f"(threshold={IMPORTANCE_WARN_THRESHOLD})",
            file=sys.stderr,
        )
    else:
        print(
            f"\n[merge_coeff_sweep] coefficient features show non-trivial importance "
            f"(max={max_coeff:.6f})"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="Merge coeff-sweep sample CSVs for RF retraining")
    ap.add_argument("--indir", type=Path, default=DEFAULT_INDIR, help="Directory with qaccess_samples_*.csv")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Merged training CSV path")
    ap.add_argument(
        "--diagnose-importance",
        action="store_true",
        help="Print coefficient-related feature importance from a trained model CSV",
    )
    ap.add_argument(
        "--importance",
        type=Path,
        default=_REPO / "derived" / "qaccess_t_feature_importance_coeff_sweep.csv",
        help="Feature importance CSV for --diagnose-importance",
    )
    args = ap.parse_args()

    if args.diagnose_importance:
        diagnose_importance(args.importance.resolve())
        return

    indir = args.indir.resolve()
    out_path = args.out.resolve()

    if not indir.is_dir():
        print(f"[merge_coeff_sweep] error: missing input dir: {indir}", file=sys.stderr)
        sys.exit(1)

    merged = merge_sweep_samples(indir, out_path)
    print_merge_summary(merged)


if __name__ == "__main__":
    main()
