#!/usr/bin/env python3
"""
Pick Q-ACCeSS-T (alpha, beta, gamma) by maximizing RFR-predicted next_bw_bps.

Inputs:
  derived/qaccess_t_model.pkl
  derived/qaccess_training_samples.csv

Output:
  derived/qaccess_t_best_coefficients.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "scripts" / "analyze") not in sys.path:
    sys.path.insert(0, str(_REPO / "scripts" / "analyze"))

from qaccess_math import (  # noqa: E402
    candidate_triples,
    normalize_d,
    normalize_g,
    normalize_l,
    path_active,
    qaccess_gain_backoff,
    qaccess_utility,
)

DEFAULT_CSV = _REPO / "derived" / "qaccess_training_samples.csv"
DEFAULT_MODEL = _REPO / "derived" / "qaccess_t_model.pkl"
DEFAULT_OUT = _REPO / "derived" / "qaccess_t_best_coefficients.json"

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


def _recent_active(df: pd.DataFrame, frac: float, min_rows: int, max_rows: int) -> pd.DataFrame:
    work = df.copy()
    for c in FEATURES + ["bw_bps", "owd_ms", "inflight_bytes"]:
        if c in work.columns:
            work[c] = pd.to_numeric(work[c], errors="coerce")
    mask = work.apply(
        lambda r: path_active(
            r.get("bw_bps", 0.0),
            r.get("owd_ms", 0.0),
            r.get("inflight_bytes", 0.0),
        ),
        axis=1,
    )
    work = work[mask]
    if work.empty:
        return work
    n = max(min_rows, int(len(work) * frac))
    n = min(n, max_rows, len(work))
    return work.tail(n)


def _feature_matrix(samples: pd.DataFrame, alpha: float, beta: float, gamma: float) -> np.ndarray:
    rows: list[list[float]] = []
    for _, r in samples.iterrows():
        bw = float(r.get("bw_bps", 0.0) or 0.0)
        owd = float(r.get("owd_ms", 0.0) or 0.0)
        dgrad = float(r.get("delay_gradient_ms", 0.0) or 0.0)
        loss = float(r.get("loss_rate", 0.0) or 0.0)
        norm_g = normalize_g(bw)
        norm_d = normalize_d(owd, dgrad)
        norm_l = normalize_l(loss)
        g_total = norm_g
        u = qaccess_utility(g_total, norm_d, norm_l, alpha, beta, gamma)
        gain, backoff = qaccess_gain_backoff(g_total, norm_d, norm_l, alpha, beta, gamma)
        rows.append([
            bw,
            owd,
            dgrad,
            loss,
            float(r.get("lost_bytes_delta", 0.0) or 0.0),
            float(r.get("retrans_bytes_delta", 0.0) or 0.0),
            float(r.get("cwnd_bytes", 0.0) or 0.0),
            float(r.get("inflight_bytes", 0.0) or 0.0),
            float(r.get("cwnd_room", 0.0) or 0.0),
            alpha,
            beta,
            gamma,
            u,
            gain,
            backoff,
        ])
    return np.asarray(rows, dtype=float)


def main() -> None:
    ap = argparse.ArgumentParser(description="Optimize Q-ACCeSS-T coefficients via RFR")
    ap.add_argument("--input", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--recent-frac", type=float, default=0.2)
    ap.add_argument("--min-recent", type=int, default=50)
    ap.add_argument("--max-recent", type=int, default=2000)
    args = ap.parse_args()

    csv_path = args.input.resolve()
    model_path = args.model.resolve()
    if not csv_path.is_file():
        print(f"[error] missing CSV: {csv_path}", file=sys.stderr)
        sys.exit(1)
    if not model_path.is_file():
        print(f"[error] missing model: {model_path}", file=sys.stderr)
        sys.exit(1)

    model = joblib.load(model_path)
    df = pd.read_csv(csv_path)
    samples = _recent_active(df, args.recent_frac, args.min_recent, args.max_recent)
    if samples.empty:
        print("[error] no active recent samples for optimization", file=sys.stderr)
        sys.exit(1)

    best_alpha, best_beta, best_gamma = 0.70, 0.10, 0.10
    best_pred = -1.0
    for alpha, beta, gamma in candidate_triples():
        X = _feature_matrix(samples, alpha, beta, gamma)
        preds = model.predict(X)
        mean_pred = float(np.mean(preds))
        if mean_pred > best_pred:
            best_pred = mean_pred
            best_alpha, best_beta, best_gamma = alpha, beta, gamma

    out = {
        "alpha": best_alpha,
        "beta": best_beta,
        "gamma": best_gamma,
        "source": "optimize_qaccess_t_coefficients.py",
        "metric": "predicted_next_bw_bps",
        "predicted_next_bw_bps": best_pred,
        "n_samples": int(len(samples)),
        "input_csv": str(csv_path),
        "model": str(model_path),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    print(
        f"[optimize] best alpha={best_alpha} beta={best_beta} gamma={best_gamma} "
        f"predicted_next_bw_bps={best_pred:.0f} (n={len(samples)})"
    )
    print(f"[optimize] wrote {args.output.resolve()}")


if __name__ == "__main__":
    main()
