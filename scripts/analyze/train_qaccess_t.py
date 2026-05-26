#!/usr/bin/env python3
"""
Train Q-ACCeSS-T Random Forest Regression model on collect-mode CSV.

Preferred input: derived/qaccess_training_samples_clean.csv
  (from scripts/analyze/preprocess_qaccess_training.py)
Fallback input:  derived/qaccess_training_samples.csv

Output: derived/qaccess_t_model.pkl
        derived/qaccess_t_validation_metrics.json
        derived/qaccess_t_feature_importance.csv

next_goodput_bps is reserved for future receiver side goodput labelling and is
ignored in Phase 1. Training uses FEATURES with target next_bw_bps only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import train_test_split

_REPO = Path(__file__).resolve().parents[2]
DEFAULT_CSV_RAW = _REPO / "derived" / "qaccess_training_samples.csv"
DEFAULT_CSV_CLEAN = _REPO / "derived" / "qaccess_training_samples_clean.csv"
DEFAULT_MODEL = _REPO / "derived" / "qaccess_t_model.pkl"
DEFAULT_METRICS = _REPO / "derived" / "qaccess_t_validation_metrics.json"
DEFAULT_IMPORTANCE = _REPO / "derived" / "qaccess_t_feature_importance.csv"

TARGET = "next_bw_bps"
# next_goodput_bps: present in qaccess_collect CSV header but unused in Phase 1; not loaded.
FEATURES = [
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


def resolve_training_csv(explicit: Path | None) -> Path:
    """Prefer preprocessed clean CSV; fall back to raw collect CSV."""
    if explicit is not None:
        path = explicit.resolve()
        print(f"[train_qaccess_t] input CSV (explicit): {path}")
        return path

    clean = DEFAULT_CSV_CLEAN.resolve()
    raw = DEFAULT_CSV_RAW.resolve()
    if clean.is_file():
        print(f"[train_qaccess_t] input CSV (preprocessed): {clean}")
        return clean

    if raw.is_file():
        print(
            "[train_qaccess_t] warning: preprocessed CSV not found; using raw collect CSV",
            file=sys.stderr,
        )
        print(
            "[train_qaccess_t] hint: run python3 scripts/analyze/preprocess_qaccess_training.py",
            file=sys.stderr,
        )
        print(f"[train_qaccess_t] input CSV (raw collect): {raw}")
        return raw

    print(f"[train_qaccess_t] input CSV (expected clean, missing): {clean}")
    return clean


def _load_csv(csv_path: Path, max_samples: int, from_tail: bool) -> pd.DataFrame:
    """Load CSV; optionally keep only the last max_samples rows (fast via tail)."""
    cols = FEATURES + [TARGET]
    if max_samples <= 0:
        print(f"[train_qaccess_t] loading full CSV (may be slow): {csv_path}")
        return pd.read_csv(csv_path)

    if from_tail:
        print(f"[train_qaccess_t] loading last {max_samples} data rows via tail ...")
        header = subprocess.run(
            ["head", "-n", "1", str(csv_path)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        body = subprocess.run(
            ["tail", "-n", str(max_samples), str(csv_path)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        df = pd.read_csv(StringIO(header + body))
    else:
        print(f"[train_qaccess_t] loading full CSV then sampling {max_samples} rows ...")
        df = pd.read_csv(csv_path)
        if len(df) > max_samples:
            df = df.sample(n=max_samples, random_state=42)
    missing = [c for c in cols if c not in df.columns]
    for c in missing:
        df[c] = 0.0
    return df


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write bytes via temp file + rename; fail clearly on disk errors."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        print(
            f"[error] failed to write {path} ({exc}). "
            "Check disk space (df -h) and remove corrupt partial files.",
            file=sys.stderr,
        )
        sys.exit(1)


def _safe_joblib_dump(model: object, path: Path) -> None:
    """Overwrite existing model safely using a temp file."""
    path = path.resolve()
    if path.exists():
        print(f"[train_qaccess_t] removing existing model (will overwrite): {path}")
        try:
            path.unlink()
        except OSError as exc:
            print(f"[error] cannot remove existing model {path}: {exc}", file=sys.stderr)
            sys.exit(1)
    tmp = path.with_name(path.name + ".tmp")
    try:
        joblib.dump(model, tmp)
        os.replace(tmp, path)
    except OSError as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        print(
            f"[error] failed to write model {path} ({exc}). "
            "Disk may be full — run: df -h derived/",
            file=sys.stderr,
        )
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description="Train Q-ACCeSS-T RFR on collect CSV")
    ap.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Training CSV (default: clean CSV if present, else raw collect CSV)",
    )
    ap.add_argument("--model-out", type=Path, default=DEFAULT_MODEL)
    ap.add_argument("--metrics-out", type=Path, default=DEFAULT_METRICS)
    ap.add_argument("--importance-out", type=Path, default=DEFAULT_IMPORTANCE)
    ap.add_argument("--test-size", type=float, default=0.2)
    ap.add_argument("--random-state", type=int, default=42)
    ap.add_argument(
        "--n-estimators", type=int, default=80,
        help="RandomForest tree count (default 80; lower = faster)",
    )
    ap.add_argument("--max-depth", type=int, default=16, help="Tree depth cap (default 16)")
    ap.add_argument(
        "--max-samples", type=int, default=200_000,
        help="Max rows to load (default 200000; 0 = all rows)",
    )
    ap.add_argument(
        "--from-tail", "--tail", action="store_true", default=True,
        dest="from_tail",
        help="Use last N rows when --max-samples > 0 (default: on)",
    )
    ap.add_argument(
        "--no-from-tail", "--no-tail", action="store_false", dest="from_tail",
        help="Random sample instead of tail when subsampling",
    )
    args = ap.parse_args()

    csv_path = resolve_training_csv(args.input)
    model_path = args.model_out.resolve()
    metrics_path = args.metrics_out.resolve()
    importance_path = args.importance_out.resolve()

    if not csv_path.is_file():
        print(f"[error] missing training CSV: {csv_path}", file=sys.stderr)
        sys.exit(1)

    sampling = "tail" if args.from_tail else "random"
    if args.max_samples > 0:
        print(f"[train_qaccess_t] subsample: max_samples={args.max_samples} ({sampling})")
    else:
        print("[train_qaccess_t] subsample: disabled (loading full CSV)")

    df = _load_csv(csv_path, args.max_samples, args.from_tail)
    n_loaded = len(df)
    print(f"[train_qaccess_t] rows loaded: {n_loaded}")

    if TARGET not in df.columns:
        print(f"[error] CSV missing target column {TARGET!r}", file=sys.stderr)
        sys.exit(1)

    df[TARGET] = pd.to_numeric(df[TARGET], errors="coerce")
    df = df.dropna(subset=[TARGET])
    n_after_drop = len(df)
    print(f"[train_qaccess_t] rows after dropping missing {TARGET}: {n_after_drop}")

    if df.empty:
        print(f"[error] no rows with valid {TARGET}", file=sys.stderr)
        sys.exit(1)

    for col in FEATURES:
        if col not in df.columns:
            df[col] = 0.0
    X = df[FEATURES].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    y = df[TARGET].astype(float)

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=args.test_size, random_state=args.random_state
    )
    n_train = len(X_train)
    n_test = len(X_test)
    print(f"[train_qaccess_t] rows used for training: {n_train} (test holdout: {n_test})")

    print(
        f"[train_qaccess_t] fitting RandomForest "
        f"(n_estimators={args.n_estimators}, max_depth={args.max_depth}) ..."
    )
    model = RandomForestRegressor(
        n_estimators=args.n_estimators,
        max_depth=args.max_depth,
        random_state=args.random_state,
        n_jobs=-1,
    )
    model.fit(X_train, y_train)
    y_pred = model.predict(X_test)

    mse = float(mean_squared_error(y_test, y_pred))
    metrics = {
        "target": TARGET,
        "n_rows_loaded": int(n_loaded),
        "n_rows_after_dropna": int(n_after_drop),
        "n_samples_raw": int(n_loaded),
        "n_samples": int(n_after_drop),
        "n_train": int(n_train),
        "n_test": int(n_test),
        "max_samples": args.max_samples,
        "from_tail": args.from_tail,
        "n_estimators": args.n_estimators,
        "max_depth": args.max_depth,
        "MSE": mse,
        "RMSE": float(np.sqrt(mse)),
        "MAE": float(mean_absolute_error(y_test, y_pred)),
        "R2": float(r2_score(y_test, y_pred)),
        "input_csv": str(csv_path),
        "model_out": str(model_path),
        "metrics_out": str(metrics_path),
        "importance_out": str(importance_path),
    }

    imp = pd.DataFrame({
        "feature": FEATURES,
        "importance": model.feature_importances_,
    }).sort_values("importance", ascending=False)

    print(f"[train_qaccess_t] writing model: {model_path}")
    _safe_joblib_dump(model, model_path)

    print(f"[train_qaccess_t] writing validation metrics: {metrics_path}")
    _atomic_write_bytes(
        metrics_path,
        (json.dumps(metrics, indent=2) + "\n").encode("utf-8"),
    )

    print(f"[train_qaccess_t] writing feature importance: {importance_path}")
    try:
        imp.to_csv(importance_path, index=False)
    except OSError as exc:
        print(
            f"[error] failed to write {importance_path} ({exc}). Check disk space.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[train_qaccess_t] RMSE={metrics['RMSE']:.2f} MAE={metrics['MAE']:.2f} R2={metrics['R2']:.4f}")
    print("[train_qaccess_t] done.")


if __name__ == "__main__":
    main()
