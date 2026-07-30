#!/usr/bin/env python3
"""Train and independently validate the shadow-only clean-D intervention model."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error

from build_qaccess_d_intervention_training import FEATURES, TARGET


def make_model(seed: int) -> RandomForestRegressor:
    return RandomForestRegressor(
        n_estimators=400,
        min_samples_leaf=3,
        max_features=0.8,
        random_state=seed,
        n_jobs=-1,
    )


def wilson_lower(successes: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    p = successes / total
    denominator = 1.0 + z * z / total
    center = p + z * z / (2 * total)
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return (center - spread) / denominator


def leave_one_replicate_out(df: pd.DataFrame, seed: int) -> np.ndarray:
    predictions = np.full(len(df), np.nan)
    for replicate in sorted(df["replicate"].unique()):
        test = df["replicate"] == replicate
        train = ~test
        model = make_model(seed)
        model.fit(df.loc[train, FEATURES], df.loc[train, TARGET])
        predictions[test.to_numpy()] = model.predict(df.loc[test, FEATURES])
    return predictions


def direction_metrics(df: pd.DataFrame, prediction: str) -> dict[str, float | int]:
    comparisons = correct = 0
    for _, group in df.groupby("replicate"):
        sham = group[
            (group["alpha"] == 0.6) & (group["beta"] == 0.3) & (group["gamma"] == 0.1)
        ]
        if len(sham) != 1:
            continue
        actual_sham = float(sham.iloc[0][TARGET])
        predicted_sham = float(sham.iloc[0][prediction])
        for _, row in group.iterrows():
            if row["candidate_id"] == sham.iloc[0]["candidate_id"]:
                continue
            actual_delta = float(row[TARGET]) - actual_sham
            predicted_delta = float(row[prediction]) - predicted_sham
            if abs(actual_delta) < 1e-9:
                continue
            comparisons += 1
            correct += int((actual_delta < 0) == (predicted_delta < 0))
    accuracy = correct / comparisons if comparisons else 0.0
    return {
        "comparisons": comparisons,
        "correct": correct,
        "accuracy": accuracy,
        "wilson_95pct_lower": wilson_lower(correct, comparisons),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20260730)
    args = parser.parse_args()
    df = pd.read_csv(args.input)
    required = set(FEATURES + [TARGET, "run_id", "candidate_id", "replicate"])
    missing = required.difference(df.columns)
    if missing:
        raise SystemExit(f"[error] missing columns: {sorted(missing)}")
    if df["run_id"].duplicated().any():
        raise SystemExit("[error] run_id is not independent: duplicate intervention rows found")
    if not np.isfinite(df[FEATURES + [TARGET]].to_numpy(dtype=float)).all():
        raise SystemExit("[error] NaN or infinity in training data")
    counts = df.groupby("candidate_id")["replicate"].nunique()
    minimum_replicates = int(counts.min()) if not counts.empty else 0
    if len(counts) != 27:
        raise SystemExit(f"[error] complete 27-candidate coverage required, got {len(counts)}")
    if minimum_replicates < 5:
        raise SystemExit(
            f"[error] five independent replicates per candidate required, got {minimum_replicates}"
        )

    oof = leave_one_replicate_out(df, args.seed)
    df = df.copy()
    df["oof_prediction_ms"] = oof
    mae = float(mean_absolute_error(df[TARGET], oof))
    direction = direction_metrics(df, "oof_prediction_ms")
    actual_candidate_medians = df.groupby("candidate_id")[TARGET].median()
    candidate_separation = float(actual_candidate_medians.max() - actual_candidate_medians.min())
    sham = df[
        (df["alpha"] == 0.6) & (df["beta"] == 0.3) & (df["gamma"] == 0.1)
    ]
    sham_false_improvements = int(
        (sham["oof_prediction_ms"] <= sham["pre_rtt_median_ms"] - 10.0).sum()
    )
    sham_fp_rate = sham_false_improvements / len(sham) if len(sham) else 1.0
    acceptance = {
        "minimum_replicates_per_candidate_at_least_5": minimum_replicates >= 5,
        "direction_accuracy_at_least_0_70": direction["accuracy"] >= 0.70,
        "direction_wilson_lower_above_0_50": direction["wilson_95pct_lower"] > 0.50,
        "mae_at_most_5_ms": mae <= 5.0,
        "candidate_separation_exceeds_mae": candidate_separation > mae,
        "sham_false_positive_rate_at_most_0_05": sham_fp_rate <= 0.05,
    }
    acceptance_met = all(acceptance.values())

    model = make_model(args.seed)
    model.fit(df[FEATURES], df[TARGET])
    artifact_dir = args.artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    model_path = artifact_dir / "qaccess_d_model_candidate_post_rtt_median_ms.pkl"
    report_path = artifact_dir / "qaccess_d_intervention_report.json"
    oof_path = artifact_dir / "qaccess_d_intervention_oof_predictions.csv"
    importance_path = artifact_dir / "qaccess_d_intervention_feature_importance.csv"
    joblib.dump(model, model_path)
    df.to_csv(oof_path, index=False)
    pd.DataFrame({"feature": FEATURES, "importance": model.feature_importances_}).sort_values(
        "importance", ascending=False
    ).to_csv(importance_path, index=False)
    report = {
        "controller_variant": "qaccess_d",
        "target": TARGET,
        "target_unit": "ms",
        "target_semantics": "post_intervention_sender_path_raw_rtt_median",
        "model_out": str(model_path),
        "n_samples": len(df),
        "independent_run_count": int(df["run_id"].nunique()),
        "candidate_count": int(df["candidate_id"].nunique()),
        "minimum_replicates_per_candidate": minimum_replicates,
        "feature_schema": FEATURES,
        "validation": {
            "method": "leave_one_replicate_out",
            "mae_ms": mae,
            "direction": direction,
            "candidate_separation_ms": candidate_separation,
            "sham_false_positive_rate": sham_fp_rate,
        },
        "acceptance_criteria": acceptance,
        "acceptance_criteria_met": acceptance_met,
        "evaluation_readiness": "shadow_only",
        "per_path_active_ready": False,
        "aggregate_label_defined": False,
        "aggregate_active_ready": False,
        "active_promotion": "manual_independent_review_required",
    }
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"model": str(model_path), "report": str(report_path), "acceptance_criteria_met": acceptance_met}, sort_keys=True))


if __name__ == "__main__":
    main()
