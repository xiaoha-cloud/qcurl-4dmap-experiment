#!/usr/bin/env python3
"""Train a qserver-sender Q-ACCeSS-D or Q-ACCeSS-L model."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import (
    average_precision_score,
    balanced_accuracy_score,
    f1_score,
    mean_absolute_error,
    mean_squared_error,
    precision_score,
    r2_score,
    recall_score,
)
from sklearn.model_selection import GroupShuffleSplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import train_qaccess_t_grouped as grouped
from build_qserver_sender_training import model_metadata
from train_qaccess_qserver_sender import filter_active_media_groups

REPO = Path(__file__).resolve().parents[2]
VARIANTS = {
    "qaccess_d": {
        "target": "delta_owd_1s",
        "out_dir": REPO / "derived/qaccess_d_qserver_sender",
        "report": "qaccess_d_qserver_sender_report.json",
    },
    "qaccess_l": {
        "target": "loss_risk_1s",
        "out_dir": REPO / "derived/qaccess_l_qserver_sender",
        "report": "qaccess_l_qserver_sender_report.json",
    },
}


def grouped_holdout_sanity(
    df,
    target: str,
    *,
    n_estimators: int,
    max_depth: int,
    random_state: int,
) -> dict:
    work = df.dropna(subset=[target]).reset_index(drop=True)
    groups = work["run_id"].astype(str).to_numpy()
    splitter = GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=random_state)
    train_index, test_index = next(splitter.split(work, groups=groups))
    model = RandomForestRegressor(
        n_estimators=n_estimators,
        max_depth=max_depth,
        random_state=random_state,
        n_jobs=-1,
    )
    model.fit(work.iloc[train_index][grouped.FEATURES], work.iloc[train_index][target])
    actual = work.iloc[test_index][target].astype(float).to_numpy()
    predicted = model.predict(work.iloc[test_index][grouped.FEATURES])
    rmse = float(np.sqrt(mean_squared_error(actual, predicted)))
    correlation = float(np.corrcoef(actual, predicted)[0, 1]) if len(actual) > 1 else float("nan")
    result = {
        "split": {
            "strategy": "GroupShuffleSplit by run_id",
            "test_size": 0.2,
            "random_state": random_state,
            "train_rows": int(len(train_index)),
            "test_rows": int(len(test_index)),
            "train_runs": sorted(np.unique(groups[train_index]).tolist()),
            "test_runs": sorted(np.unique(groups[test_index]).tolist()),
        },
        "prediction_vs_actual": {
            "RMSE": rmse,
            "MAE": float(mean_absolute_error(actual, predicted)),
            "R2": float(r2_score(actual, predicted)),
            "pearson_correlation": correlation if np.isfinite(correlation) else None,
        },
    }
    if target == "loss_risk_1s":
        actual_risk = actual > 0
        predicted_risk = predicted >= 0.5
        result["risk_accuracy"] = {
            "threshold_bytes": 0.5,
            "actual_positive_rate": float(np.mean(actual_risk)),
            "predicted_positive_rate": float(np.mean(predicted_risk)),
            "precision": float(precision_score(actual_risk, predicted_risk, zero_division=0)),
            "recall": float(recall_score(actual_risk, predicted_risk, zero_division=0)),
            "f1": float(f1_score(actual_risk, predicted_risk, zero_division=0)),
            "balanced_accuracy": float(balanced_accuracy_score(actual_risk, predicted_risk)),
            "average_precision": float(average_precision_score(actual_risk, predicted)),
        }
    else:
        result["direction_accuracy"] = float(np.mean((predicted > 0) == (actual > 0)))
        result["actual_worsening_rate"] = float(np.mean(actual > 0))
        result["predicted_worsening_rate"] = float(np.mean(predicted > 0))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", choices=sorted(VARIANTS), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--n-estimators", type=int, default=80)
    parser.add_argument("--max-depth", type=int, default=16)
    parser.add_argument("--random-state", type=int, default=42)
    args = parser.parse_args()

    spec = VARIANTS[args.variant]
    target = spec["target"]
    out_dir = (args.out_dir or spec["out_dir"]).resolve()
    audit_cmd = [
        sys.executable,
        str(REPO / "scripts/analyze/audit_qserver_sender_training.py"),
        "--input",
        str(args.input),
        "--target",
        target,
    ]
    if args.allow_partial:
        audit_cmd.append("--allow-partial")
    subprocess.run(audit_cmd, cwd=REPO, check=True)

    df = grouped.load_training_frame(args.input.resolve(), min_path_id=0, min_bw_bps_relative=0)
    df = df[df.endpoint_role == "server_downlink_sender"].copy()
    df, media_filter = filter_active_media_groups(df)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = grouped.train_target(
        df,
        target,
        out_dir,
        n_estimators=args.n_estimators,
        max_depth=args.max_depth,
        random_state=args.random_state,
    )
    sanity = grouped_holdout_sanity(
        df,
        target,
        n_estimators=args.n_estimators,
        max_depth=args.max_depth,
        random_state=args.random_state,
    )
    model_path = out_dir / grouped.MODEL_OUT[target]
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    report = model_metadata(
        df,
        args.input,
        model_path,
        result,
        grouped.FEATURES,
        commit,
        args.allow_partial,
        controller_variant=args.variant,
        target=target,
    )
    report["media_path_filter"] = media_filter
    report["train_test_split"] = sanity["split"]
    report["sanity_metrics"] = sanity
    report_path = out_dir / spec["report"]
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"[train-variant] variant={args.variant} target={target} model={model_path} report={report_path}")


if __name__ == "__main__":
    main()
