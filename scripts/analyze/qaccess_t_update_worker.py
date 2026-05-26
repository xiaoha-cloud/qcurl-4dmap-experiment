#!/usr/bin/env python3
"""
Q-ACCeSS-T Phase 2 update worker (offline / application-layer optimization).

Polls qaccess_update_request.json written by Go when throughput drops.
Reads recent runtime samples, scores 64 coefficient candidates with the Phase 1 RFR,
writes qaccess_t_best_coefficients.json atomically for Go reload.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from io import StringIO
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "scripts" / "analyze") not in sys.path:
    sys.path.insert(0, str(_REPO / "scripts" / "analyze"))

from qaccess_io import atomic_write_json  # noqa: E402
from qaccess_math import candidate_triples, normalize_d, normalize_g, normalize_l  # noqa: E402
from qaccess_math import qaccess_gain_backoff, qaccess_utility  # noqa: E402

DEFAULT_REQUEST = _REPO / "derived" / "qaccess_update_request.json"
DEFAULT_SAMPLES = _REPO / "derived" / "qaccess_runtime_samples.csv"
DEFAULT_MODEL = _REPO / "derived" / "qaccess_t_model.pkl"
DEFAULT_COEFFS = _REPO / "derived" / "qaccess_t_best_coefficients.json"
DEFAULT_RESPONSE = _REPO / "derived" / "qaccess_update_response.json"

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
    header = subprocess.run(
        ["head", "-n", "1", str(csv_path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    body = subprocess.run(
        ["tail", "-n", str(tail_rows), str(csv_path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return pd.read_csv(StringIO(header + body))


def _clean_runtime_samples(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    work = df.copy()
    for col in FEATURES + ["next_bw_bps"]:
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")
    if "next_bw_bps" in work.columns:
        work = work.dropna(subset=["next_bw_bps"])
        work = work[work["next_bw_bps"] >= 0]
    bw = work["bw_bps"].fillna(0) if "bw_bps" in work.columns else 0
    owd = work["owd_ms"].fillna(0) if "owd_ms" in work.columns else 0
    inflight = work["inflight_bytes"].fillna(0) if "inflight_bytes" in work.columns else 0
    active = (bw > 0) | (owd > 0) | (inflight > 1024)
    work = work.loc[active]
    for col in FEATURES:
        if col not in work.columns:
            work[col] = 0.0
        work[col] = work[col].replace([np.inf, -np.inf], 0).fillna(0)
    if "delay_gradient_ms" in work.columns:
        work["delay_gradient_ms"] = work["delay_gradient_ms"].replace([np.inf, -np.inf], 0).fillna(0)
    for col in ["bw_bps", "owd_ms", "loss_rate", "cwnd_bytes", "inflight_bytes", "cwnd_room"]:
        if col in work.columns:
            work[col] = work[col].clip(lower=0)
    return work


def _feature_matrix(samples: pd.DataFrame, alpha: float, beta: float, gamma: float) -> pd.DataFrame:
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
            bw, owd, dgrad, loss,
            float(r.get("lost_bytes_delta", 0) or 0),
            float(r.get("retrans_bytes_delta", 0) or 0),
            float(r.get("cwnd_bytes", 0) or 0),
            float(r.get("inflight_bytes", 0) or 0),
            float(r.get("cwnd_room", 0) or 0),
            alpha, beta, gamma, u, gain, backoff,
        ])
    return pd.DataFrame(rows, columns=FEATURES)


def _pick_coefficients_rf(samples: pd.DataFrame, model) -> tuple[float, float, float, float]:
    best_alpha, best_beta, best_gamma = 0.70, 0.10, 0.10
    best_pred = -1.0
    for alpha, beta, gamma in candidate_triples():
        X = _feature_matrix(samples, alpha, beta, gamma)
        preds = np.asarray(model.predict(X), dtype=float)
        mean_pred = float(np.mean(preds))
        if mean_pred > best_pred:
            best_pred = mean_pred
            best_alpha, best_beta, best_gamma = alpha, beta, gamma
    return best_alpha, best_beta, best_gamma, best_pred


def _process_request(
    request_path: Path,
    samples_path: Path,
    model_path: Path,
    coeffs_out: Path,
    response_out: Path,
    mode: str,
    recent_rows: int,
    retrain: bool,
) -> bool:
    if not request_path.is_file():
        return False
    try:
        req = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    if not samples_path.is_file():
        print(f"[worker] skip: missing runtime samples {samples_path}", file=sys.stderr)
        return False

    df = _load_csv_tail(samples_path, recent_rows)
    samples = _clean_runtime_samples(df)
    if samples.empty:
        print("[worker] skip: no valid runtime samples after clean", file=sys.stderr)
        return False

    if retrain:
        print("[worker] retrain not implemented; using existing model", file=sys.stderr)

    if mode != "rf":
        print(f"[worker] unsupported mode {mode!r}; use rf", file=sys.stderr)
        return False

    if not model_path.is_file():
        print(f"[worker] missing model {model_path}", file=sys.stderr)
        return False

    model = joblib.load(model_path)
    alpha, beta, gamma, pred = _pick_coefficients_rf(samples, model)

    out = {
        "alpha": alpha,
        "beta": beta,
        "gamma": gamma,
        "source": "qaccess_t_update_worker.py",
        "metric": "predicted_next_bw_bps",
        "predicted_next_bw_bps": pred,
        "n_samples": int(len(samples)),
        "request": req,
        "mode": mode,
    }
    atomic_write_json(coeffs_out, out)

    response = {
        "timestamp_ms": int(time.time() * 1000),
        "status": "ok",
        "coeffs_out": str(coeffs_out.resolve()),
        "alpha": alpha,
        "beta": beta,
        "gamma": gamma,
        "predicted_next_bw_bps": pred,
        "n_samples": int(len(samples)),
        "request_reason": req.get("reason"),
    }
    atomic_write_json(response_out, response)

    print(
        f"[worker] updated coeffs alpha={alpha:.4f} beta={beta:.4f} gamma={gamma:.4f} "
        f"predicted_next_bw_bps={pred:.0f} n={len(samples)}"
    )
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Q-ACCeSS-T Phase 2 coefficient update worker")
    ap.add_argument("--request", type=Path, default=DEFAULT_REQUEST)
    ap.add_argument("--runtime-samples", type=Path, default=DEFAULT_SAMPLES)
    ap.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    ap.add_argument("--coeffs-out", type=Path, default=DEFAULT_COEFFS)
    ap.add_argument("--response-out", type=Path, default=DEFAULT_RESPONSE)
    ap.add_argument("--mode", choices=["rf"], default="rf")
    ap.add_argument("--poll-interval", type=float, default=5.0)
    ap.add_argument("--recent-rows", type=int, default=5000)
    ap.add_argument("--retrain", action="store_true", help="Retrain RFR (not implemented)")
    ap.add_argument("--once", action="store_true", help="Process one request if present, then exit")
    args = ap.parse_args()

    last_mtime = 0.0
    print(f"[worker] polling {args.request.resolve()} every {args.poll_interval}s")

    while True:
        req_path = args.request.resolve()
        if req_path.is_file():
            mtime = req_path.stat().st_mtime
            if mtime > last_mtime:
                if _process_request(
                    req_path,
                    args.runtime_samples.resolve(),
                    args.model.resolve(),
                    args.coeffs_out.resolve(),
                    args.response_out.resolve(),
                    args.mode,
                    args.recent_rows,
                    args.retrain,
                ):
                    last_mtime = mtime

        if args.once:
            break
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()
