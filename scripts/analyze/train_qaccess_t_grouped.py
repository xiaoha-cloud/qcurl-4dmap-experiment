#!/usr/bin/env python3
"""
Train Q-ACCeSS-T RF models on windowed coeff-sweep data with grouped validation.

Targets:
  - delta_bw_1s
  - relative_delta_bw_1s
  - next_bw_bps (baseline comparison only)

Validation:
  - GroupKFold by run_id
  - Leave-one-coefficient-combination-out

Does not modify the Phase 2 worker or improvement gate.

Usage (after coeff sweep + windowed OLIA dataset):
  python3 scripts/analyze/train_qaccess_t_grouped.py \\
    --input derived/qaccess_training_samples_coeff_sweep_olia_windowed.csv
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
from sklearn.inspection import permutation_importance
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import GroupKFold, LeaveOneGroupOut

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "scripts" / "analyze") not in sys.path:
    sys.path.insert(0, str(_REPO / "scripts" / "analyze"))

from qaccess_math import phase2_candidate_triples, qaccess_gain_backoff, qaccess_utility  # noqa: E402
from qaccess_math import normalize_d, normalize_g, normalize_l  # noqa: E402

DEFAULT_INPUT = _REPO / "derived" / "qaccess_training_samples_coeff_sweep_olia_windowed.csv"
DEFAULT_OUT_DIR = _REPO / "derived" / "qaccess_t_redesign"
LEGACY_MODEL = _REPO / "derived" / "qaccess_t_model.pkl"

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

COEFF_COLS = ["alpha", "beta", "gamma"]
TARGET_DELTA = "delta_bw_1s"
TARGET_REL = "relative_delta_bw_1s"
TARGET_LEGACY = "next_bw_bps"

MODEL_OUT = {
    TARGET_DELTA: "qaccess_t_model_delta_bw_1s.pkl",
    TARGET_REL: "qaccess_t_model_relative_delta_bw_1s.pkl",
}


def _to_numeric_frame(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()
    for col in FEATURES + [TARGET_DELTA, TARGET_REL, TARGET_LEGACY, "path_id", "time_s"]:
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")
    for col in COEFF_COLS:
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")
    return work


def load_training_frame(path: Path, *, min_path_id: int, min_bw_bps_relative: float) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(f"missing training CSV: {path}")
    df = pd.read_csv(path)
    df = _to_numeric_frame(df)
    if "path_id" not in df.columns:
        raise ValueError("missing path_id column")
    df = df.loc[df["path_id"] >= min_path_id].copy()
    if df.empty:
        raise ValueError(f"no rows with path_id >= {min_path_id}")

    for col in FEATURES:
        if col not in df.columns:
            df[col] = 0.0
        df[col] = df[col].replace([np.inf, -np.inf], 0.0).fillna(0.0)

    if min_bw_bps_relative > 0:
        df = df.loc[df["bw_bps"] >= min_bw_bps_relative].copy()

    return df.reset_index(drop=True)


def _coeff_group_labels(df: pd.DataFrame) -> np.ndarray:
    combos = df[COEFF_COLS].round(4).astype(str).agg(",".join, axis=1)
    codes, _ = pd.factorize(combos, sort=True)
    return codes.astype(int)


def _metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    mse = float(mean_squared_error(y_true, y_pred))
    return {
        "RMSE": float(np.sqrt(mse)),
        "MAE": float(mean_absolute_error(y_true, y_pred)),
        "R2": float(r2_score(y_true, y_pred)),
        "n": int(len(y_true)),
    }


def grouped_cv_by_run(
    df: pd.DataFrame,
    target: str,
    *,
    n_estimators: int,
    max_depth: int,
    random_state: int,
) -> dict:
    work = df.dropna(subset=[target]).copy()
    if work.empty:
        raise ValueError(f"no rows with target {target!r}")

    X = work[FEATURES]
    y = work[target].astype(float).to_numpy()
    groups = work["run_id"].astype(str).to_numpy()
    n_groups = len(np.unique(groups))
    n_splits = max(2, min(5, n_groups))

    gkf = GroupKFold(n_splits=n_splits)
    fold_rows: list[dict] = []
    y_true_all: list[float] = []
    y_pred_all: list[float] = []

    for fold, (tr, te) in enumerate(gkf.split(X, y, groups=groups), start=1):
        model = RandomForestRegressor(
            n_estimators=n_estimators,
            max_depth=max_depth,
            random_state=random_state,
            n_jobs=-1,
        )
        model.fit(X.iloc[tr], y[tr])
        pred = model.predict(X.iloc[te])
        m = _metrics(y[te], pred)
        m["fold"] = fold
        m["n_train_groups"] = int(len(np.unique(groups[tr])))
        m["n_test_groups"] = int(len(np.unique(groups[te])))
        fold_rows.append(m)
        y_true_all.extend(y[te].tolist())
        y_pred_all.extend(pred.tolist())

    overall = _metrics(np.asarray(y_true_all), np.asarray(y_pred_all))
    return {
        "scheme": "group_kfold_by_run_id",
        "n_splits": n_splits,
        "folds": fold_rows,
        "overall": overall,
    }


def loocv_by_coeff_combo(
    df: pd.DataFrame,
    target: str,
    *,
    n_estimators: int,
    max_depth: int,
    random_state: int,
) -> dict:
    work = df.dropna(subset=[target]).copy()
    X = work[FEATURES]
    y = work[target].astype(float).to_numpy()
    groups = _coeff_group_labels(work)
    n_groups = len(np.unique(groups))
    if n_groups < 2:
        return {"scheme": "leave_one_coeff_combo_out", "error": "need >= 2 coefficient groups", "folds": []}

    logo = LeaveOneGroupOut()
    fold_rows: list[dict] = []
    y_true_all: list[float] = []
    y_pred_all: list[float] = []

    for fold, (tr, te) in enumerate(logo.split(X, y, groups=groups), start=1):
        held = work.iloc[te][COEFF_COLS].drop_duplicates().iloc[0].to_dict()
        model = RandomForestRegressor(
            n_estimators=n_estimators,
            max_depth=max_depth,
            random_state=random_state,
            n_jobs=-1,
        )
        model.fit(X.iloc[tr], y[tr])
        pred = model.predict(X.iloc[te])
        m = _metrics(y[te], pred)
        m["fold"] = fold
        m["held_out_alpha"] = float(held["alpha"])
        m["held_out_beta"] = float(held["beta"])
        m["held_out_gamma"] = float(held["gamma"])
        fold_rows.append(m)
        y_true_all.extend(y[te].tolist())
        y_pred_all.extend(pred.tolist())

    overall = _metrics(np.asarray(y_true_all), np.asarray(y_pred_all))
    return {
        "scheme": "leave_one_coeff_combo_out",
        "n_groups": n_groups,
        "folds": fold_rows,
        "overall": overall,
    }


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


def candidate_score_separation(
    df: pd.DataFrame,
    model,
    *,
    current_alpha: float,
    current_beta: float,
    current_gamma: float,
    max_rows: int,
) -> dict:
    work = df.copy()
    if len(work) > max_rows:
        work = work.sample(n=max_rows, random_state=42)

    cur_pred = float(np.mean(model.predict(_feature_matrix(work, current_alpha, current_beta, current_gamma))))
    cand_preds: list[dict] = []
    for alpha, beta, gamma in phase2_candidate_triples():
        mean_pred = float(np.mean(model.predict(_feature_matrix(work, alpha, beta, gamma))))
        cand_preds.append({
            "alpha": alpha,
            "beta": beta,
            "gamma": gamma,
            "mean_pred": mean_pred,
        })

    preds = np.asarray([c["mean_pred"] for c in cand_preds], dtype=float)
    best = max(cand_preds, key=lambda c: c["mean_pred"])
    spread_abs = float(np.max(preds) - np.min(preds))
    spread_pct_vs_current = (
        (float(best["mean_pred"]) - cur_pred) / abs(cur_pred) * 100.0 if cur_pred != 0 else float("nan")
    )
    spread_pct_vs_mean = (
        spread_abs / abs(float(np.mean(preds))) * 100.0 if np.mean(preds) != 0 else float("nan")
    )
    return {
        "n_rows_used": int(len(work)),
        "current_alpha": current_alpha,
        "current_beta": current_beta,
        "current_gamma": current_gamma,
        "pred_current": cur_pred,
        "pred_best": float(best["mean_pred"]),
        "best_alpha": float(best["alpha"]),
        "best_beta": float(best["beta"]),
        "best_gamma": float(best["gamma"]),
        "candidate_pred_min": float(np.min(preds)),
        "candidate_pred_max": float(np.max(preds)),
        "candidate_pred_std": float(np.std(preds)),
        "candidate_spread_abs": spread_abs,
        "candidate_spread_pct_vs_current": spread_pct_vs_current,
        "candidate_spread_pct_vs_mean": spread_pct_vs_mean,
        "candidates": cand_preds,
    }


def fit_full_model(
    df: pd.DataFrame,
    target: str,
    *,
    n_estimators: int,
    max_depth: int,
    random_state: int,
) -> RandomForestRegressor:
    work = df.dropna(subset=[target]).copy()
    model = RandomForestRegressor(
        n_estimators=n_estimators,
        max_depth=max_depth,
        random_state=random_state,
        n_jobs=-1,
    )
    model.fit(work[FEATURES], work[target].astype(float))
    return model


def importance_tables(model, X: pd.DataFrame, y: pd.Series, *, random_state: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    imp = pd.DataFrame({
        "feature": FEATURES,
        "importance": model.feature_importances_,
    }).sort_values("importance", ascending=False)

    perm = permutation_importance(
        model,
        X,
        y,
        n_repeats=10,
        random_state=random_state,
        n_jobs=-1,
    )
    perm_df = pd.DataFrame({
        "feature": FEATURES,
        "perm_importance_mean": perm.importances_mean,
        "perm_importance_std": perm.importances_std,
    }).sort_values("perm_importance_mean", ascending=False)
    return imp, perm_df


def dataset_summary(df: pd.DataFrame) -> dict:
    summary: dict = {
        "rows": int(len(df)),
        "unique_runs": int(df["run_id"].nunique()) if "run_id" in df.columns else 0,
        "unique_paths": int(df["path_id"].nunique()) if "path_id" in df.columns else 0,
        "unique_coeff_combos": int(df.groupby(COEFF_COLS).ngroups) if set(COEFF_COLS).issubset(df.columns) else 0,
    }
    if set(COEFF_COLS).issubset(df.columns):
        summary["coeff_combinations"] = (
            df.groupby(COEFF_COLS, as_index=False)
            .size()
            .rename(columns={"size": "rows"})
            .sort_values(COEFF_COLS)
            .to_dict(orient="records")
        )
    if "run_id" in df.columns:
        summary["rows_per_run"] = (
            df.groupby("run_id", as_index=False)
            .size()
            .rename(columns={"size": "rows"})
            .sort_values("run_id")
            .to_dict(orient="records")
        )
        if "path_id" in df.columns:
            summary["rows_per_run_path"] = (
                df.groupby(["run_id", "path_id"], as_index=False)
                .size()
                .rename(columns={"size": "rows"})
                .sort_values(["run_id", "path_id"])
                .to_dict(orient="records")
            )
    for target in [TARGET_DELTA, TARGET_REL, TARGET_LEGACY]:
        if target in df.columns:
            s = pd.to_numeric(df[target], errors="coerce").dropna()
            if not s.empty:
                summary[f"target_{target}"] = {
                    "count": int(len(s)),
                    "mean": float(s.mean()),
                    "std": float(s.std()),
                    "min": float(s.min()),
                    "p50": float(s.quantile(0.5)),
                    "p95": float(s.quantile(0.95)),
                    "max": float(s.max()),
                }
    return summary


def compare_legacy_next_bw(df: pd.DataFrame, *, random_state: int) -> dict:
    if TARGET_LEGACY not in df.columns:
        return {"available": False, "reason": "next_bw_bps not in dataset"}
    if LEGACY_MODEL.is_file():
        legacy = joblib.load(LEGACY_MODEL)
        source = str(LEGACY_MODEL)
    else:
        legacy = fit_full_model(df, TARGET_LEGACY, n_estimators=80, max_depth=16, random_state=random_state)
        source = "fitted_on_same_dataset_for_comparison"

    work = df.dropna(subset=[TARGET_LEGACY]).copy()
    sep = candidate_score_separation(
        work,
        legacy,
        current_alpha=0.6,
        current_beta=0.3,
        current_gamma=0.1,
        max_rows=2000,
    )
    return {
        "available": True,
        "model_source": source,
        "group_kfold_by_run": grouped_cv_by_run(
            work, TARGET_LEGACY, n_estimators=80, max_depth=16, random_state=random_state
        ),
        "candidate_separation": sep,
    }


def train_target(
    df: pd.DataFrame,
    target: str,
    out_dir: Path,
    *,
    n_estimators: int,
    max_depth: int,
    random_state: int,
) -> dict:
    work = df.dropna(subset=[target]).copy()
    if work.empty:
        raise ValueError(f"no rows for target {target!r}")

    model = fit_full_model(work, target, n_estimators=n_estimators, max_depth=max_depth, random_state=random_state)
    model_path = out_dir / MODEL_OUT[target]
    joblib.dump(model, model_path)

    imp, perm = importance_tables(model, work[FEATURES], work[target].astype(float), random_state=random_state)
    imp_path = out_dir / f"feature_importance_{target}.csv"
    perm_path = out_dir / f"permutation_importance_{target}.csv"
    imp.to_csv(imp_path, index=False)
    perm.to_csv(perm_path, index=False)

    sep = candidate_score_separation(
        work,
        model,
        current_alpha=0.6,
        current_beta=0.3,
        current_gamma=0.1,
        max_rows=2000,
    )

    coeff_imp = imp[imp["feature"].isin(COEFF_COLS + ["utility", "gain", "backoff"])]
    return {
        "target": target,
        "model_path": str(model_path),
        "rows_used": int(len(work)),
        "group_kfold_by_run": grouped_cv_by_run(
            work, target, n_estimators=n_estimators, max_depth=max_depth, random_state=random_state
        ),
        "leave_one_coeff_combo_out": loocv_by_coeff_combo(
            work, target, n_estimators=n_estimators, max_depth=max_depth, random_state=random_state
        ),
        "feature_importance_path": str(imp_path),
        "permutation_importance_path": str(perm_path),
        "feature_importance_coeff_related": coeff_imp.to_dict(orient="records"),
        "permutation_importance_coeff_related": perm[perm["feature"].isin(COEFF_COLS + ["utility", "gain", "backoff"])].to_dict(orient="records"),
        "candidate_separation": sep,
    }


def recommend_worker_target(results: dict[str, dict]) -> dict:
    scored: list[tuple[str, float]] = []
    for target in [TARGET_DELTA, TARGET_REL]:
        if target not in results:
            continue
        sep = results[target]["candidate_separation"]
        coeff_imp = results[target]["feature_importance_coeff_related"]
        max_coeff = max((row["importance"] for row in coeff_imp), default=0.0)
        score = (
            float(sep.get("candidate_spread_pct_vs_current", 0.0) or 0.0) * 0.6
            + max_coeff * 100.0 * 0.4
        )
        scored.append((target, score))

    if not scored:
        return {"recommended_target": None, "reason": "no trained targets"}

    scored.sort(key=lambda x: x[1], reverse=True)
    best = scored[0][0]
    return {
        "recommended_target": best,
        "ranking": [{"target": t, "selection_score": s} for t, s in scored],
        "note": (
            "Worker is not changed by this script. "
            "Use recommended_target only after a follow-up worker patch that predicts the same target."
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Train grouped-validated Q-ACCeSS-T delta RF models")
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--min-path-id", type=int, default=1)
    ap.add_argument("--min-bw-bps-relative", type=float, default=100_000.0)
    ap.add_argument("--n-estimators", type=int, default=80)
    ap.add_argument("--max-depth", type=int, default=16)
    ap.add_argument("--random-state", type=int, default=42)
    args = ap.parse_args()

    input_path = args.input.resolve()
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[train_grouped] input: {input_path}")
    df = load_training_frame(
        input_path,
        min_path_id=args.min_path_id,
        min_bw_bps_relative=args.min_bw_bps_relative,
    )
    summary = dataset_summary(df)
    print(f"[train_grouped] rows after filters: {summary['rows']}")
    print(f"[train_grouped] unique runs: {summary['unique_runs']}  coeff combos: {summary['unique_coeff_combos']}")

    report: dict = {
        "input_csv": str(input_path),
        "filters": {
            "min_path_id": args.min_path_id,
            "min_bw_bps_relative": args.min_bw_bps_relative,
        },
        "dataset_summary": summary,
        "models": {},
        "legacy_next_bw_bps_comparison": compare_legacy_next_bw(df, random_state=args.random_state),
    }

    for target in [TARGET_DELTA, TARGET_REL]:
        print(f"[train_grouped] training target={target}")
        report["models"][target] = train_target(
            df,
            target,
            out_dir,
            n_estimators=args.n_estimators,
            max_depth=args.max_depth,
            random_state=args.random_state,
        )
        sep = report["models"][target]["candidate_separation"]
        print(
            f"[train_grouped] {target}: grouped R2={report['models'][target]['group_kfold_by_run']['overall']['R2']:.4f} "
            f"candidate_spread_pct_vs_current={sep['candidate_spread_pct_vs_current']:.4f}"
        )

    report["worker_target_recommendation"] = recommend_worker_target(report["models"])

    report_path = out_dir / "qaccess_t_redesign_report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"[train_grouped] wrote report: {report_path}")
    print(f"[train_grouped] recommended worker target: {report['worker_target_recommendation']['recommended_target']}")
    print("[train_grouped] done.")


if __name__ == "__main__":
    main()
