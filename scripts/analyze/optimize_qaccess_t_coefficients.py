#!/usr/bin/env python3
"""
Pick Q-ACCeSS-T (alpha, beta, gamma).

Modes:
  rf     — use RFR model + CSV (needs qaccess_t_model.pkl) [strict Phase 1]
  direct — pick candidate with highest mean next_bw_bps from collect CSV (no model, VM-friendly)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from io import StringIO
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


def _load_csv_tail(csv_path: Path, tail_rows: int) -> pd.DataFrame:
    if tail_rows <= 0:
        return pd.read_csv(csv_path)
    proc = subprocess.run(
        ["tail", "-n", str(tail_rows + 1), str(csv_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return pd.read_csv(StringIO(proc.stdout))


def _load_rf_model(model_path: Path):
    """Load RFR model; fail clearly if missing or corrupt."""
    if not model_path.is_file():
        print(f"[error] missing model: {model_path}", file=sys.stderr)
        print(
            "[error] train first: python scripts/analyze/train_qaccess_t.py "
            "--max-samples 200000 --n-estimators 80",
            file=sys.stderr,
        )
        sys.exit(1)
    size = model_path.stat().st_size
    if size == 0:
        print(f"[error] model file is empty (corrupt): {model_path}", file=sys.stderr)
        sys.exit(1)
    print(f"[optimize] loading RFR model: {model_path} ({size} bytes)")
    try:
        model = joblib.load(model_path)
    except Exception as exc:
        print(
            f"[error] failed to load model (corrupt or incomplete?): {model_path}: {exc}",
            file=sys.stderr,
        )
        print(f"[hint] remove and retrain: rm -f {model_path}", file=sys.stderr)
        sys.exit(1)
    if not hasattr(model, "predict"):
        print(f"[error] {model_path} is not a valid sklearn model (no predict)", file=sys.stderr)
        sys.exit(1)
    try:
        probe = np.zeros((1, len(FEATURES)), dtype=float)
        model.predict(probe)
    except Exception as exc:
        print(f"[error] model predict probe failed (corrupt?): {exc}", file=sys.stderr)
        sys.exit(1)
    return model


def _path_active_mask(df: pd.DataFrame) -> pd.Series:
    """Vectorized PathMetricsActive (matches Go collect filter)."""
    bw = pd.to_numeric(df.get("bw_bps", 0), errors="coerce").fillna(0.0)
    owd = pd.to_numeric(df.get("owd_ms", 0), errors="coerce").fillna(0.0)
    inflight = pd.to_numeric(df.get("inflight_bytes", 0), errors="coerce").fillna(0.0)
    return (bw > 0) | (owd > 0) | (inflight > 1024)


def _samples_for_rf(df: pd.DataFrame, frac: float, min_rows: int, max_rows: int) -> pd.DataFrame:
    """
    Rows for RF coefficient scoring.

    Prefer path-active rows (same as Go collect). If the CSV tail is from experiment
    teardown (all metrics zero), fall back to rows with cwnd/utility or all tail rows.
    """
    total = len(df)
    mask = _path_active_mask(df)
    active_n = int(mask.sum())
    print(f"[optimize] path-active rows in loaded tail: {active_n} / {total}")

    if active_n > 0:
        work = df.loc[mask].copy()
    else:
        cwnd = pd.to_numeric(df.get("cwnd_bytes", 0), errors="coerce").fillna(0.0)
        util = pd.to_numeric(df.get("utility", 0), errors="coerce").fillna(0.0)
        fb = (cwnd > 0) | (util != 0)
        fb_n = int(fb.sum())
        print(
            f"[optimize] no path-active rows; fallback cwnd/utility: {fb_n} / {total}"
        )
        if fb_n > 0:
            work = df.loc[fb].copy()
        else:
            print("[optimize] fallback: using all loaded tail rows (fillna 0)")
            work = df.copy()

    for c in FEATURES:
        if c in work.columns:
            work[c] = pd.to_numeric(work[c], errors="coerce").fillna(0.0)

    n = max(min_rows, int(len(work) * frac))
    n = min(n, max_rows, len(work))
    return work.tail(n)


def _rf_predict(model, X: np.ndarray) -> np.ndarray:
    """Predict with feature names to match sklearn training on DataFrame."""
    X_df = pd.DataFrame(X, columns=FEATURES)
    return np.asarray(model.predict(X_df), dtype=float)


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


def _optimize_direct(df: pd.DataFrame) -> tuple[float, float, float, float, int]:
    """Pick (alpha,beta,gamma) with highest mean next_bw_bps in collect rows."""
    work = df.copy()
    for c in ("alpha", "beta", "gamma", "next_bw_bps"):
        work[c] = pd.to_numeric(work[c], errors="coerce")
    work = work.dropna(subset=["alpha", "beta", "gamma", "next_bw_bps"])
    if work.empty:
        raise ValueError("no rows with alpha/beta/gamma and next_bw_bps")
    work["alpha"] = work["alpha"].round(2)
    work["beta"] = work["beta"].round(2)
    work["gamma"] = work["gamma"].round(2)
    valid = set(candidate_triples())
    grouped = (
        work.groupby(["alpha", "beta", "gamma"], as_index=False)["next_bw_bps"]
        .mean()
        .sort_values("next_bw_bps", ascending=False)
    )
    for _, row in grouped.iterrows():
        key = (float(row["alpha"]), float(row["beta"]), float(row["gamma"]))
        if key in valid:
            return key[0], key[1], key[2], float(row["next_bw_bps"]), int(
                len(work[
                    (work["alpha"] == row["alpha"])
                    & (work["beta"] == row["beta"])
                    & (work["gamma"] == row["gamma"])
                ])
            )
    best_a, best_b, best_g = 0.70, 0.10, 0.10
    best_mean = -1.0
    best_n = 0
    for a, b, g in candidate_triples():
        sub = work[
            (np.isclose(work["alpha"], a))
            & (np.isclose(work["beta"], b))
            & (np.isclose(work["gamma"], g))
        ]
        if sub.empty:
            continue
        m = float(sub["next_bw_bps"].mean())
        if m > best_mean:
            best_mean, best_a, best_b, best_g, best_n = m, a, b, g, len(sub)
    if best_mean < 0:
        return 0.70, 0.10, 0.10, 0.0, 0
    return best_a, best_b, best_g, best_mean, best_n


def main() -> None:
    ap = argparse.ArgumentParser(description="Optimize Q-ACCeSS-T coefficients")
    ap.add_argument(
        "--mode", choices=("rf", "direct"), default="direct",
        help="rf=RandomForest predict (strict Phase 1); direct=mean next_bw_bps from CSV",
    )
    ap.add_argument("--input", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--recent-frac", type=float, default=0.2)
    ap.add_argument("--min-recent", type=int, default=50)
    ap.add_argument("--max-recent", type=int, default=500)
    ap.add_argument(
        "--tail-rows", type=int, default=200_000,
        help="Only read last N CSV rows before optimization (default 200000, match train tail)",
    )
    args = ap.parse_args()

    csv_path = args.input.resolve()
    if not csv_path.is_file():
        print(f"[error] missing CSV: {csv_path}", file=sys.stderr)
        sys.exit(1)

    mode_label = "RF (RandomForest)" if args.mode == "rf" else "DIRECT (CSV mean, no model)"
    print(f"[optimize] ===== mode={args.mode} ({mode_label}) =====")
    print(f"[optimize] input CSV: {csv_path}")
    print(f"[optimize] loading last {args.tail_rows} CSV rows ...")
    df = _load_csv_tail(csv_path, args.tail_rows)
    print(f"[optimize] rows loaded: {len(df)}")

    if args.mode == "direct":
        print("[optimize] DIRECT mode: selecting max mean next_bw_bps over candidate grid")
        best_alpha, best_beta, best_gamma, best_score, n_used = _optimize_direct(df)
        out = {
            "alpha": best_alpha,
            "beta": best_beta,
            "gamma": best_gamma,
            "source": "optimize_qaccess_t_coefficients.py",
            "metric": "mean_next_bw_bps",
            "mean_next_bw_bps": best_score,
            "n_samples": n_used,
            "input_csv": str(csv_path),
            "mode": "direct",
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        print(
            f"[optimize] DIRECT selected alpha={best_alpha} beta={best_beta} gamma={best_gamma} "
            f"mean_next_bw_bps={best_score:.0f} (n={n_used})"
        )
        print(f"[optimize] wrote {args.output.resolve()} (mode=direct)")
        return

    print("[optimize] RF mode: enumerating 64 candidate (alpha,beta,gamma) triples")
    model_path = args.model.resolve()
    model = _load_rf_model(model_path)

    samples = _samples_for_rf(df, args.recent_frac, args.min_recent, args.max_recent)
    if samples.empty:
        print("[error] no samples for RF optimization (try --tail-rows 200000)", file=sys.stderr)
        sys.exit(1)
    print(f"[optimize] RF scoring sample rows: {len(samples)}")

    best_alpha, best_beta, best_gamma = 0.70, 0.10, 0.10
    best_pred = -1.0
    n_candidates = len(list(candidate_triples()))
    for alpha, beta, gamma in candidate_triples():
        X = _feature_matrix(samples, alpha, beta, gamma)
        preds = _rf_predict(model, X)
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
        "n_candidates": n_candidates,
        "input_csv": str(csv_path),
        "model": str(model_path),
        "mode": "rf",
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")

    print(
        f"[optimize] RF selected alpha={best_alpha} beta={best_beta} gamma={best_gamma} "
        f"predicted_next_bw_bps={best_pred:.0f} (n={len(samples)}, candidates={n_candidates})"
    )
    print(f"[optimize] wrote {args.output.resolve()} (mode=rf)")


if __name__ == "__main__":
    main()
