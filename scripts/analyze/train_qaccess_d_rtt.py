#!/usr/bin/env python3
"""Train frozen clean-D RF on fixed-coefficient per-path RTT time-series rows."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import GroupKFold, LeaveOneGroupOut

from build_qserver_sender_training import RTT_TARGET
from qaccess_math import normalize_d, normalize_g, normalize_l, phase2_candidate_triples, qaccess_gain_backoff, qaccess_utility

REPO = Path(__file__).resolve().parents[2]
COEFFICIENT_FEATURES = ["alpha", "beta", "gamma", "utility", "gain", "backoff"]
FEATURES = [
    "bw_bps",
    "rtt_latest_median_ms",
    "rtt_smoothed_median_ms",
    "rtt_min_median_ms",
    "rtt_history_median_3s_ms",
    "rtt_history_std_3s_ms",
    "rtt_delta_1s_ms",
    "rtt_slope_3s_ms_per_s",
    "delay_gradient_ms",
    "loss_rate",
    "lost_bytes_delta",
    "retrans_bytes_delta",
    "cwnd_bytes",
    "inflight_bytes",
    "cwnd_room",
    *COEFFICIENT_FEATURES,
]


def metrics(y_true: np.ndarray, predicted: np.ndarray) -> dict[str, float | int]:
    return {
        "RMSE_ms": float(math.sqrt(mean_squared_error(y_true, predicted))),
        "MAE_ms": float(mean_absolute_error(y_true, predicted)),
        "R2": float(r2_score(y_true, predicted)),
        "n": int(len(y_true)),
    }


def metrics_with_direction(
    y_true: np.ndarray,
    predicted: np.ndarray,
    current: np.ndarray,
) -> dict[str, float | int]:
    result = metrics(y_true, predicted)
    actual_delta = y_true - current
    predicted_delta = predicted - current
    directional = np.abs(actual_delta) > 1e-9
    result["direction_accuracy"] = (
        float(np.mean(np.sign(actual_delta[directional]) == np.sign(predicted_delta[directional])))
        if directional.any() else float("nan")
    )
    result["direction_evaluable_n"] = int(directional.sum())
    return result


def load_frame(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    required = {
        RTT_TARGET, "run_id", "connection_id", "path_id", "sender_byte_delta",
        "endpoint_role", "owd_ms", *FEATURES,
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"missing required clean-D RTT columns: {missing}")
    for column in [RTT_TARGET, "sender_byte_delta", "owd_ms", *FEATURES]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame[frame.endpoint_role == "server_downlink_sender"].copy()
    active_groups = (
        frame.groupby(["run_id", "connection_id", "path_id"], dropna=False).sender_byte_delta.sum()
    )
    active_groups = active_groups[active_groups > 0].reset_index()[["run_id", "connection_id", "path_id"]]
    frame = frame.merge(active_groups, on=["run_id", "connection_id", "path_id"], how="inner")
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna(subset=[RTT_TARGET, *FEATURES])
    if frame.empty:
        raise ValueError("no complete active clean-D RTT rows")
    coefficients_per_run = frame.groupby("run_id")[["alpha", "beta", "gamma"]].apply(
        lambda values: len(values.drop_duplicates())
    )
    invalid_runs = coefficients_per_run[coefficients_per_run != 1]
    if not invalid_runs.empty:
        raise ValueError(
            "clean-D fixed-sweep invariant failed; coefficient changed within runs: "
            + ", ".join(map(str, invalid_runs.index.tolist()))
        )
    return frame.reset_index(drop=True)


def new_model(n_estimators: int, max_depth: int, random_state: int) -> RandomForestRegressor:
    return RandomForestRegressor(
        n_estimators=n_estimators,
        max_depth=max_depth,
        min_samples_leaf=5,
        max_features="sqrt",
        random_state=random_state,
        n_jobs=-1,
    )


def grouped_validation(frame: pd.DataFrame, n_estimators: int, max_depth: int, random_state: int) -> dict:
    groups = frame.run_id.astype(str).to_numpy()
    unique = np.unique(groups)
    if len(unique) < 2:
        return {"scheme": "GroupKFold by run_id", "ready": False, "reason": "need at least two runs"}
    splitter = GroupKFold(n_splits=min(5, len(unique)))
    actual, predicted, current, folds = [], [], [], []
    for fold, (train, test) in enumerate(splitter.split(frame[FEATURES], frame[RTT_TARGET], groups), 1):
        model = new_model(n_estimators, max_depth, random_state)
        model.fit(frame.iloc[train][FEATURES], frame.iloc[train][RTT_TARGET])
        pred = model.predict(frame.iloc[test][FEATURES])
        fold_metrics = metrics_with_direction(
            frame.iloc[test][RTT_TARGET].to_numpy(),
            pred,
            frame.iloc[test].rtt_latest_median_ms.to_numpy(),
        )
        fold_metrics.update({"fold": fold, "test_runs": sorted(frame.iloc[test].run_id.astype(str).unique().tolist())})
        folds.append(fold_metrics)
        actual.extend(frame.iloc[test][RTT_TARGET].tolist())
        predicted.extend(pred.tolist())
        current.extend(frame.iloc[test].rtt_latest_median_ms.tolist())
    return {"scheme": "GroupKFold by run_id", "ready": True, "folds": folds,
            "overall": metrics_with_direction(np.asarray(actual), np.asarray(predicted), np.asarray(current))}


def leave_one_coefficient_out(frame: pd.DataFrame, n_estimators: int, max_depth: int, random_state: int) -> dict:
    labels = frame[["alpha", "beta", "gamma"]].round(4).astype(str).agg(",".join, axis=1).to_numpy()
    if len(np.unique(labels)) < 2:
        return {"scheme": "leave-one-coefficient-combination-out", "ready": False, "reason": "need at least two tuples"}
    actual, predicted, current = [], [], []
    for train, test in LeaveOneGroupOut().split(frame[FEATURES], frame[RTT_TARGET], labels):
        model = new_model(n_estimators, max_depth, random_state)
        model.fit(frame.iloc[train][FEATURES], frame.iloc[train][RTT_TARGET])
        pred = model.predict(frame.iloc[test][FEATURES])
        actual.extend(frame.iloc[test][RTT_TARGET].tolist())
        predicted.extend(pred.tolist())
        current.extend(frame.iloc[test].rtt_latest_median_ms.tolist())
    return {"scheme": "leave-one-coefficient-combination-out", "ready": True,
            "overall": metrics_with_direction(np.asarray(actual), np.asarray(predicted), np.asarray(current))}


def candidate_frame(frame: pd.DataFrame, alpha: float, beta: float, gamma: float) -> pd.DataFrame:
    work = frame.copy()
    work["alpha"], work["beta"], work["gamma"] = alpha, beta, gamma
    utilities, gains, backoffs = [], [], []
    for row in work.itertuples(index=False):
        norm_g = normalize_g(float(row.bw_bps))
        norm_d = normalize_d(float(row.owd_ms), float(row.delay_gradient_ms))
        norm_l = normalize_l(float(row.loss_rate))
        utilities.append(qaccess_utility(norm_g, norm_d, norm_l, alpha, beta, gamma))
        gain, backoff = qaccess_gain_backoff(norm_g, norm_d, norm_l, alpha, beta, gamma)
        gains.append(gain)
        backoffs.append(backoff)
    work["utility"], work["gain"], work["backoff"] = utilities, gains, backoffs
    return work[FEATURES]


def candidate_separation(frame: pd.DataFrame, model: RandomForestRegressor, max_rows: int = 2000) -> dict:
    sample = frame.sample(n=min(max_rows, len(frame)), random_state=42)
    candidates = []
    for alpha, beta, gamma in phase2_candidate_triples():
        prediction = float(np.mean(model.predict(candidate_frame(sample, alpha, beta, gamma))))
        candidates.append({"alpha": alpha, "beta": beta, "gamma": gamma, "predicted_future_rtt_ms": prediction})
    values = np.asarray([row["predicted_future_rtt_ms"] for row in candidates])
    best = min(candidates, key=lambda row: row["predicted_future_rtt_ms"])
    return {
        "optimization_direction": "minimize",
        "candidate_count": len(candidates),
        "candidate_pred_min_ms": float(values.min()),
        "candidate_pred_max_ms": float(values.max()),
        "candidate_spread_ms": float(values.max() - values.min()),
        "candidate_pred_std_ms": float(values.std()),
        "best_candidate": best,
        "candidates": candidates,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=REPO / "derived/qaccess_d_rtt_fixed_sweep")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--n-estimators", type=int, default=80)
    parser.add_argument("--max-depth", type=int, default=16)
    parser.add_argument("--random-state", type=int, default=42)
    args = parser.parse_args()
    frame = load_frame(args.input.resolve())
    tuple_count = frame.groupby(["alpha", "beta", "gamma"]).ngroups
    if tuple_count < 27 and not args.allow_partial:
        raise SystemExit(f"[error] coefficient coverage {tuple_count}/27; use --allow-partial only for smoke")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    cv = grouped_validation(frame, args.n_estimators, args.max_depth, args.random_state)
    loco = leave_one_coefficient_out(frame, args.n_estimators, args.max_depth, args.random_state)
    model = new_model(args.n_estimators, args.max_depth, args.random_state)
    model.fit(frame[FEATURES], frame[RTT_TARGET])
    model_path = args.out_dir / "qaccess_d_model_future_path_rtt_median_3s_ms.pkl"
    joblib.dump(model, model_path)
    separation = candidate_separation(frame, model)
    group_cv_mae = cv.get("overall", {}).get("MAE_ms") if cv.get("ready") else None
    separation_to_mae = (
        separation["candidate_spread_ms"] / group_cv_mae
        if group_cv_mae is not None and group_cv_mae > 0 else None
    )
    report = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
        "controller_variant": "qaccess_d",
        "model_type": "RandomForestRegressor",
        "target": RTT_TARGET,
        "target_unit": "ms",
        "target_semantics": "median per-path QUIC LatestRTT over complete future seconds t+1 through t+3",
        "feature_list": FEATURES,
        "input_csv": str(args.input.resolve()),
        "model_path": str(model_path.resolve()),
        "n_samples": len(frame),
        "training_sessions": sorted(frame.run_id.astype(str).unique().tolist()),
        "coefficient_coverage": tuple_count,
        "fixed_coefficient_per_run": True,
        "grouped_validation": cv,
        "leave_one_coefficient_out": loco,
        "candidate_separation": separation,
        "candidate_spread_to_group_cv_mae_ratio": separation_to_mae,
        "per_path_active_ready": False,
        "aggregate_active_ready": False,
        "evaluation_readiness": "SHADOW_ONLY_PENDING_INDEPENDENT_VALIDATION",
    }
    report_path = args.out_dir / "qaccess_d_rtt_report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"model": str(model_path), "report": str(report_path), "rows": len(frame), "runs": frame.run_id.nunique(), "tuples": tuple_count}, sort_keys=True))


if __name__ == "__main__":
    main()
