#!/usr/bin/env python3
"""
Train Q-ACCeSS-T Random Forest Regression model on collect-mode CSV.

Input:  derived/qaccess_training_samples.csv
Output: derived/qaccess_t_model.pkl
        derived/qaccess_t_validation_metrics.json
        derived/qaccess_t_feature_importance.csv
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import train_test_split

_REPO = Path(__file__).resolve().parents[2]
DEFAULT_CSV = _REPO / "derived" / "qaccess_training_samples.csv"
DEFAULT_MODEL = _REPO / "derived" / "qaccess_t_model.pkl"
DEFAULT_METRICS = _REPO / "derived" / "qaccess_t_validation_metrics.json"
DEFAULT_IMPORTANCE = _REPO / "derived" / "qaccess_t_feature_importance.csv"

TARGET = "next_bw_bps"
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


def main() -> None:
    ap = argparse.ArgumentParser(description="Train Q-ACCeSS-T RFR on collect CSV")
    ap.add_argument("--input", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--model-out", type=Path, default=DEFAULT_MODEL)
    ap.add_argument("--metrics-out", type=Path, default=DEFAULT_METRICS)
    ap.add_argument("--importance-out", type=Path, default=DEFAULT_IMPORTANCE)
    ap.add_argument("--test-size", type=float, default=0.2)
    ap.add_argument("--random-state", type=int, default=42)
    ap.add_argument("--n-estimators", type=int, default=200)
    args = ap.parse_args()

    csv_path = args.input.resolve()
    if not csv_path.is_file():
        print(f"[error] missing training CSV: {csv_path}", file=sys.stderr)
        sys.exit(1)

    df = pd.read_csv(csv_path)
    if TARGET not in df.columns:
        print(f"[error] CSV missing target column {TARGET!r}", file=sys.stderr)
        sys.exit(1)

    df[TARGET] = pd.to_numeric(df[TARGET], errors="coerce")
    df = df.dropna(subset=[TARGET])
    if df.empty:
        print("[error] no rows with valid next_bw_bps", file=sys.stderr)
        sys.exit(1)

    for col in FEATURES:
        if col not in df.columns:
            df[col] = 0.0
    X = df[FEATURES].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    y = df[TARGET].astype(float)

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=args.test_size, random_state=args.random_state
    )

    model = RandomForestRegressor(
        n_estimators=args.n_estimators,
        random_state=args.random_state,
        n_jobs=-1,
    )
    model.fit(X_train, y_train)
    y_pred = model.predict(X_test)

    mse = float(mean_squared_error(y_test, y_pred))
    metrics = {
        "target": TARGET,
        "n_samples": int(len(df)),
        "n_train": int(len(X_train)),
        "n_test": int(len(X_test)),
        "MSE": mse,
        "RMSE": float(np.sqrt(mse)),
        "MAE": float(mean_absolute_error(y_test, y_pred)),
        "R2": float(r2_score(y_test, y_pred)),
        "input_csv": str(csv_path),
    }

    imp = pd.DataFrame({
        "feature": FEATURES,
        "importance": model.feature_importances_,
    }).sort_values("importance", ascending=False)

    for p in (args.model_out, args.metrics_out, args.importance_out):
        p.parent.mkdir(parents=True, exist_ok=True)

    joblib.dump(model, args.model_out.resolve())
    args.metrics_out.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    imp.to_csv(args.importance_out.resolve(), index=False)

    print(f"[train_qaccess_t] samples={metrics['n_samples']} test={metrics['n_test']}")
    print(f"[train_qaccess_t] RMSE={metrics['RMSE']:.2f} MAE={metrics['MAE']:.2f} R2={metrics['R2']:.4f}")
    print(f"[train_qaccess_t] model → {args.model_out.resolve()}")
    print(f"[train_qaccess_t] metrics → {args.metrics_out.resolve()}")
    print(f"[train_qaccess_t] importance → {args.importance_out.resolve()}")


if __name__ == "__main__":
    main()
