#!/usr/bin/env python3
"""
Q-ACCeSS-T Phase 2 update worker (buffer-full trigger).

Polls qaccess_update_request.json written by Go when a path-specific runtime buffer is full.
Each request_id is processed at most once.

Writes only derived/qaccess_t_runtime_coefficients.json (never qaccess_t_initial_coefficients.json).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "scripts" / "analyze") not in sys.path:
    sys.path.insert(0, str(_REPO / "scripts" / "analyze"))

from qaccess_coefficients import (  # noqa: E402
    load_coeffs_doc,
    resolve_path_coeffs,
    update_path_coeffs_locked,
)
from qaccess_io import atomic_write_json  # noqa: E402
from qaccess_math import (  # noqa: E402
    normalize_d,
    normalize_g,
    normalize_l,
    phase2_candidate_triples,
    qaccess_gain_backoff,
    qaccess_utility,
)

DEFAULT_REQUEST = _REPO / "derived" / "qaccess_update_request.json"
DEFAULT_SAMPLES = _REPO / "derived" / "qaccess_runtime_samples.csv"
DEFAULT_MODEL = _REPO / "derived" / "qaccess_t_model.pkl"
DEFAULT_MODEL_METADATA = _REPO / "derived" / "qaccess_t_validation_metrics.json"
DEFAULT_INITIAL_COEFFS = _REPO / "derived" / "qaccess_t_initial_coefficients.json"
DEFAULT_COEFFS = _REPO / "derived" / "qaccess_t_runtime_coefficients.json"
DEFAULT_RESPONSE = _REPO / "derived" / "qaccess_update_response.json"
DEFAULT_STATE = _REPO / "derived" / "qaccess_worker_state.json"
DEFAULT_ARCHIVE_DIR = _REPO / "derived" / "qaccess_processed_buffers"
DEFAULT_AUDIT_CSV = _REPO / "derived" / "qaccess_per_path_update_audit.csv"
DEFAULT_READY = _REPO / "derived" / "qaccess_worker_ready.json"

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

TARGET_MODES = ("next_bw_bps", "delta_bw_1s", "relative_delta_bw_1s", "delta_owd_1s", "delta_loss_1s", "loss_risk_1s")
MINIMIZE_TARGETS = {"delta_owd_1s", "delta_loss_1s", "loss_risk_1s"}
ACTIVE_TARGET_MODES = {"delta_bw_1s", "delta_owd_1s", "delta_loss_1s", "loss_risk_1s"}
VARIANT_TARGETS = {
    "qaccess_t": {"next_bw_bps", "delta_bw_1s", "relative_delta_bw_1s"},
    "qaccess_d": {"delta_owd_1s"},
    "qaccess_l": {"delta_loss_1s", "loss_risk_1s"},
}

DEFAULT_MIN_IMPROVEMENT_PCT = 3.0
DEFAULT_MIN_DELTA_GAIN_BPS = 500_000.0
DEFAULT_MIN_RELATIVE_GAIN = 0.03
DEFAULT_OBJECTIVE_T_RELATIVE_GAIN = 0.05
DEFAULT_OBJECTIVE_D_REDUCTION_MS = 10.0
DEFAULT_OBJECTIVE_D_RELATIVE_REDUCTION = 0.10
DEFAULT_OBJECTIVE_L_REDUCTION_BYTES = 4096.0
DEFAULT_OBJECTIVE_L_RELATIVE_REDUCTION = 0.25
DEFAULT_MIN_RELATIVE_DELTA_GAIN = 0.01
DEFAULT_CHANGED_PATH_IDS = (3,)
DEFAULT_CHANGED_PATH_GAIN_BPS = 100_000.0
DEFAULT_MIN_AGGREGATE_GAIN_BPS = 0.0
DEFAULT_MAX_OTHER_PATH_LOSS_RATIO = 0.75
DEFAULT_MAX_OTHER_PATH_LOSS_BPS = 200_000.0
GATE_MODES = ("absolute", "relative", "hybrid")
GATE_POLICIES = ("legacy", "objective_aware")
OBJECTIVES = ("throughput", "delay", "loss")
VARIANT_OBJECTIVES = {"qaccess_t": "throughput", "qaccess_d": "delay", "qaccess_l": "loss"}
SECONDARY_GUARDRAILS_UNAVAILABLE = "NOT_AVAILABLE_FOR_PRE_UPDATE_EVALUATION"
RELATIVE_GAIN_EPSILON = 1e-9
MAX_COEFF_STEP = 0.1
MIN_BETA_GAMMA = 0.1
DEFAULT_COEFFS_PREV = _REPO / "derived" / "qaccess_t_runtime_coefficients_prev.json"

AUDIT_FIELDS = [
    "timestamp_ms",
    "request_id",
    "run_id",
    "path_id",
    "reason",
    "mode",
    "shadow",
    "row_count",
    "current_alpha",
    "current_beta",
    "current_gamma",
    "current_source",
    "candidate_alpha",
    "candidate_beta",
    "candidate_gamma",
    "applied_alpha",
    "applied_beta",
    "applied_gamma",
    "pred_current_delta_bps",
    "pred_best_delta_bps",
    "score_gain_bps",
    "mean_gain_before",
    "mean_gain_after",
    "mean_backoff_before",
    "mean_backoff_after",
    "gate_type",
    "status",
    "fixed_gamma",
]


def _load_model_provenance(model_path: Path, metadata_path: Path, model) -> dict[str, Any]:
    if not metadata_path.is_file():
        raise FileNotFoundError(f"missing model metadata: {metadata_path.resolve()}")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid model metadata {metadata_path.resolve()}: {exc}") from exc

    target = ""
    training_rows = 0
    recorded_model = ""
    if isinstance(metadata.get("models"), dict):
        for candidate_target, entry in metadata["models"].items():
            if not isinstance(entry, dict):
                continue
            entry_model = str(entry.get("model_path") or "")
            if entry_model and Path(entry_model).name == model_path.name:
                target = str(entry.get("target") or candidate_target)
                training_rows = int(entry.get("rows_used") or 0)
                recorded_model = entry_model
                break
    else:
        target = str(metadata.get("target") or "")
        training_rows = int(metadata.get("n_train") or metadata.get("n_samples") or 0)
        recorded_model = str(metadata.get("model_out") or "")

    if not target:
        raise ValueError(
            f"model target is missing from metadata {metadata_path.resolve()} for {model_path.resolve()}"
        )
    if recorded_model and Path(recorded_model).name != model_path.name:
        raise ValueError(
            f"model metadata mismatch: metadata records {recorded_model}, selected model is {model_path.resolve()}"
        )

    feature_names = list(getattr(model, "feature_names_in_", []))
    if not feature_names:
        n_features = int(getattr(model, "n_features_in_", 0) or 0)
        if n_features == len(FEATURES):
            feature_names = list(FEATURES)
    if feature_names != FEATURES:
        raise ValueError(
            f"model feature mismatch: expected {FEATURES}, got {feature_names or '<unknown>'}"
        )

    return {
        "model_target": target,
        "controller_variant": str(metadata.get("controller_variant") or ""),
        "model_training_rows": training_rows,
        "model_features": feature_names,
        "model_metadata": str(metadata_path.resolve()),
    }


def _validate_model_target(provenance: dict[str, Any], requested_target: str) -> str:
    model_target = str(provenance.get("model_target") or "")
    if model_target != requested_target:
        raise ValueError(
            f"incompatible model/target: model_target={model_target!r} "
            f"requested_target={requested_target!r}"
        )
    return "compatible"


def validate_model_configuration(model_path: Path, metadata_path: Path, requested_target: str) -> dict[str, Any]:
    """Load model provenance and reject target/feature mismatches before polling."""
    resolved_model = model_path.resolve()
    resolved_metadata = metadata_path.resolve()
    if not resolved_model.is_file():
        raise FileNotFoundError(f"missing model {resolved_model}")
    model = joblib.load(resolved_model)
    provenance = _load_model_provenance(resolved_model, resolved_metadata, model)
    _validate_model_target(provenance, requested_target)
    return {
        **provenance,
        "resolved_model_path": str(resolved_model),
        "verified_model_target": provenance["model_target"],
        "requested_target_mode": requested_target,
        "model_target_compatible": True,
    }


def _optimization_score(target_mode: str, prediction: float) -> float:
    return -float(prediction) if target_mode in MINIMIZE_TARGETS else float(prediction)


def _objective_improvement(target_mode: str, current: float, candidate: float) -> float:
    return _optimization_score(target_mode, candidate) - _optimization_score(target_mode, current)


def _score_unit(target_mode: str) -> str:
    return {
        "next_bw_bps": "bps",
        "delta_bw_1s": "bps",
        "relative_delta_bw_1s": "ratio",
        "delta_owd_1s": "ms",
        "delta_loss_1s": "loss_rate",
        "loss_risk_1s": "bytes",
    }[target_mode]


def _objective_name(target_mode: str) -> str:
    if target_mode == "delta_owd_1s":
        return "delay_aware"
    if target_mode in {"delta_loss_1s", "loss_risk_1s"}:
        return "loss_aware"
    return "throughput_aware"


def _load_state(path: Path) -> dict:
    if not path.is_file():
        return {"processed_request_ids": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"processed_request_ids": []}


def _save_state(path: Path, state: dict) -> None:
    atomic_write_json(path, state)


def _safe_archive_name(request_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", request_id) or "unknown"


def _load_runtime_samples(samples_path: Path) -> pd.DataFrame:
    if not samples_path.is_file():
        return pd.DataFrame()
    return pd.read_csv(samples_path)


def _clean_runtime_samples(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    work = df.copy()
    for col in FEATURES + ["next_bw_bps", "path_id", "sender_bytes_total"]:
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


def _filter_samples_by_path(samples: pd.DataFrame, path_id: int) -> pd.DataFrame:
    if samples.empty or "path_id" not in samples.columns:
        return pd.DataFrame()
    work = samples[samples["path_id"] == path_id].copy()
    return work.reset_index(drop=True)


def _candidate_triples(fixed_gamma: float | None) -> list[tuple[float, float, float]]:
    triples = phase2_candidate_triples()
    if fixed_gamma is None:
        return triples
    return [(a, b, g) for a, b, g in triples if abs(g - fixed_gamma) < 1e-9]


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


def _mean_prediction(samples: pd.DataFrame, model, alpha: float, beta: float, gamma: float) -> float:
    X = _feature_matrix(samples, alpha, beta, gamma)
    if X.empty:
        return -1.0
    preds = np.asarray(model.predict(X), dtype=float)
    return float(np.mean(preds))


def _mean_gain_backoff(samples: pd.DataFrame, alpha: float, beta: float, gamma: float) -> tuple[float, float]:
    X = _feature_matrix(samples, alpha, beta, gamma)
    if X.empty:
        return 1.0, 1.0
    return float(X["gain"].mean()), float(X["backoff"].mean())


def _score_all_candidates(
    samples: pd.DataFrame,
    model,
    *,
    cur_alpha: float,
    cur_beta: float,
    cur_gamma: float,
    fixed_gamma: float | None,
    target_mode: str = "delta_bw_1s",
) -> tuple[list[dict[str, Any]], float, float, float, float, float]:
    pred_current = _mean_prediction(samples, model, cur_alpha, cur_beta, cur_gamma)
    candidates: list[dict[str, Any]] = []
    best_alpha, best_beta, best_gamma = cur_alpha, cur_beta, cur_gamma
    best_pred = pred_current
    best_score = _optimization_score(target_mode, pred_current)

    for alpha, beta, gamma in _candidate_triples(fixed_gamma):
        mean_pred = _mean_prediction(samples, model, alpha, beta, gamma)
        candidates.append({
            "alpha": alpha,
            "beta": beta,
            "gamma": gamma,
            "mean_prediction": mean_pred,
            "optimization_score": _optimization_score(target_mode, mean_pred),
            "is_current": (
                abs(alpha - cur_alpha) < 1e-9
                and abs(beta - cur_beta) < 1e-9
                and abs(gamma - cur_gamma) < 1e-9
            ),
        })
        if _optimization_score(target_mode, mean_pred) > best_score:
            best_pred = mean_pred
            best_score = _optimization_score(target_mode, mean_pred)
            best_alpha, best_beta, best_gamma = alpha, beta, gamma

    for row in candidates:
        row["is_best"] = (
            abs(row["alpha"] - best_alpha) < 1e-9
            and abs(row["beta"] - best_beta) < 1e-9
            and abs(row["gamma"] - best_gamma) < 1e-9
        )

    return candidates, pred_current, best_alpha, best_beta, best_gamma, best_pred


def _apply_max_step(current: float, target: float, max_step: float = MAX_COEFF_STEP) -> float:
    delta = max(-max_step, min(max_step, target - current))
    return current + delta


def _candidate_changed_path_metrics(
    *,
    candidate: dict[str, Any],
    path_entries: list[dict[str, Any]],
    changed_path_ids: set[int],
    changed_path_gain_bps: float,
    min_aggregate_gain_bps: float,
    max_other_path_loss_ratio: float,
    max_other_path_loss_bps: float,
) -> dict[str, Any]:
    changed_path_rows = [row for row in path_entries if int(row["path_id"]) in changed_path_ids]
    other_rows = [row for row in path_entries if int(row["path_id"]) not in changed_path_ids]
    changed_path_raw_gain_bps = float(sum(float(row["gain_bps"]) for row in changed_path_rows))
    changed_path_weighted_gain_bps = float(sum(float(row["weighted_gain_bps"]) for row in changed_path_rows))
    other_path_loss_bps = float(sum(-float(row["gain_bps"]) for row in other_rows if float(row["gain_bps"]) < 0))
    other_path_gain_bps = float(sum(float(row["gain_bps"]) for row in other_rows if float(row["gain_bps"]) > 0))
    aggregate_gain_bps = float(candidate["byte_weighted_gain"])
    changed_path_gate_pass = changed_path_weighted_gain_bps >= changed_path_gain_bps
    aggregate_safety_pass = aggregate_gain_bps > min_aggregate_gain_bps
    other_path_loss_pass = (
        other_path_loss_bps <= max_other_path_loss_ratio * max(changed_path_weighted_gain_bps, 0.0)
        and other_path_loss_bps <= max_other_path_loss_bps
    )
    fig7_changed_path_would_apply = bool(
        changed_path_gate_pass and aggregate_safety_pass and other_path_loss_pass
    )
    return {
        "coefficients": {
            "alpha": float(candidate["alpha"]),
            "beta": float(candidate["beta"]),
            "gamma": float(candidate["gamma"]),
        },
        "aggregate_gain_bps": aggregate_gain_bps,
        "changed_path_raw_gain_bps": changed_path_raw_gain_bps,
        "changed_path_weighted_gain_bps": changed_path_weighted_gain_bps,
        "other_path_loss_bps": other_path_loss_bps,
        "other_path_gain_bps": other_path_gain_bps,
        "changed_path_gate_pass": changed_path_gate_pass,
        "aggregate_safety_pass": aggregate_safety_pass,
        "other_path_loss_pass": other_path_loss_pass,
        "fig7_changed_path_would_apply": fig7_changed_path_would_apply,
        "path_entries": path_entries,
    }


def _select_changed_path_priority_candidate(
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    safe = [row for row in candidates if row["aggregate_safety_pass"] and row["other_path_loss_pass"]]
    population = safe if safe else candidates
    return sorted(
        population,
        key=lambda row: (
            -float(row["changed_path_weighted_gain_bps"]),
            -float(row["aggregate_gain_bps"]),
            float(row["other_path_loss_bps"]),
        ),
    )[0]


def _ensure_path_writable(path: Path) -> None:
    if not path.is_file():
        return
    if os.access(path, os.W_OK):
        return
    try:
        path.chmod(0o666)
    except OSError:
        pass
    if not os.access(path, os.W_OK):
        raise PermissionError(
            f"cannot write {path.resolve()} (owned by root?). "
            "Fix once: sudo chown $USER:$USER derived/qaccess_runtime_samples.csv "
            "derived/qaccess_update_request.json; rebuild 4dmap after pull for 0666 exports."
        )


def _remove_path_rows_from_csv(samples_path: Path, path_id: int) -> None:
    if not samples_path.is_file():
        return
    _ensure_path_writable(samples_path)
    df = pd.read_csv(samples_path)
    if df.empty or "path_id" not in df.columns:
        return
    kept = df[df["path_id"] != path_id]
    tmp = samples_path.with_suffix(".csv.tmp")
    kept.to_csv(tmp, index=False)
    os.chmod(tmp, 0o666)
    os.replace(tmp, samples_path)
    try:
        os.chmod(samples_path, 0o666)
    except OSError:
        pass


def _archive_and_truncate_buffer(
    samples_path: Path,
    archive_dir: Path,
    request_id: str,
    request_path: Path,
    path_id: int,
    *,
    shadow: bool,
) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_archive_name(request_id)
    if samples_path.is_file() and samples_path.stat().st_size > 0:
        _ensure_path_writable(samples_path)
        df = pd.read_csv(samples_path)
        path_df = _filter_samples_by_path(_clean_runtime_samples(df), path_id)
        if not path_df.empty:
            dest = archive_dir / f"qaccess_runtime_samples_{safe}_path{path_id}.csv"
            path_df.to_csv(dest, index=False)
        if not shadow:
            _remove_path_rows_from_csv(samples_path, path_id)
    if request_path.is_file():
        _ensure_path_writable(request_path)
        req_dest = archive_dir / f"qaccess_update_request_{safe}.json"
        shutil.copy2(request_path, req_dest)
        request_path.unlink()


def _assert_runtime_coeffs_path(coeffs_out: Path) -> None:
    resolved = coeffs_out.resolve()
    if resolved == DEFAULT_INITIAL_COEFFS.resolve():
        raise ValueError(
            f"refusing to write initial coefficients file: {coeffs_out}\n"
            f"use --coeffs-out {DEFAULT_COEFFS.relative_to(_REPO)}"
        )
    if resolved.name == "qaccess_t_initial_coefficients.json":
        raise ValueError(f"refusing to write initial coefficients file: {coeffs_out}")


def _save_coeffs_backup(
    coeffs_out: Path,
    archive_dir: Path,
    request_id: str,
    prev_out: Path,
) -> Path | None:
    if not coeffs_out.is_file():
        return None
    archive_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_archive_name(request_id)
    archived = archive_dir / f"qaccess_t_runtime_coefficients_before_{safe}.json"
    shutil.copy2(coeffs_out, archived)
    shutil.copy2(coeffs_out, prev_out)
    return archived


def _improvement_pct(pred_current: float, pred_best: float) -> float:
    if pred_current > 0 and math.isfinite(pred_current) and math.isfinite(pred_best):
        return (pred_best - pred_current) / pred_current * 100.0
    if pred_current <= 0 and pred_best > 0:
        return float("inf")
    return float("nan")


def _metric_field(target_mode: str) -> str:
    return {
        "next_bw_bps": "predicted_next_bw_bps",
        "delta_bw_1s": "predicted_delta_bw_bps",
        "relative_delta_bw_1s": "predicted_relative_delta_bw_1s",
        "delta_owd_1s": "predicted_delta_owd_ms",
        "delta_loss_1s": "predicted_delta_loss_rate",
        "loss_risk_1s": "predicted_loss_risk_bytes",
    }[target_mode]


def _evaluate_gate(
    target_mode: str,
    pred_current: float,
    pred_best: float,
    *,
    min_improvement_pct: float,
    min_delta_gain_bps: float,
    min_relative_delta_gain: float,
) -> tuple[bool, str, str, float, float | None, str]:
    score_gain = pred_best - pred_current

    if target_mode == "next_bw_bps":
        need_pred = pred_current * (1.0 + min_improvement_pct / 100.0) if pred_current > 0 else 0.0
        ok = pred_current <= 0 or pred_best >= need_pred
        return (
            ok,
            "multiplicative_pct",
            "improvement_gate",
            min_improvement_pct,
            None,
            f"need_pred>={need_pred:.0f}",
        )

    if target_mode == "delta_bw_1s":
        score_gain_bps = score_gain
        ok = pred_best > pred_current and score_gain_bps >= min_delta_gain_bps
        return (
            ok,
            "delta_bps",
            "delta_gain_gate",
            min_delta_gain_bps,
            score_gain_bps,
            f"required_gain_bps={min_delta_gain_bps:.0f}",
        )

    if target_mode == "relative_delta_bw_1s":
        ok = pred_best > pred_current and score_gain >= min_relative_delta_gain
        return (
            ok,
            "relative_delta",
            "relative_delta_gain_gate",
            min_relative_delta_gain,
            None,
            f"required_gain={min_relative_delta_gain:.4f}",
        )

    if target_mode in MINIMIZE_TARGETS:
        improvement = pred_current - pred_best
        ok = pred_best < pred_current and improvement >= min_delta_gain_bps
        return (
            ok,
            f"{target_mode}_reduction",
            "objective_reduction_gate",
            min_delta_gain_bps,
            improvement,
            f"required_reduction={min_delta_gain_bps:g}",
        )

    raise ValueError(f"unsupported target_mode: {target_mode}")


def evaluate_gain_gate(
    current_score: float,
    best_score: float,
    *,
    gate_mode: str,
    min_delta_gain_bps: float,
    min_relative_gain: float,
    epsilon: float = RELATIVE_GAIN_EPSILON,
) -> dict[str, Any]:
    if gate_mode not in GATE_MODES:
        raise ValueError(f"unsupported gate mode: {gate_mode}")
    absolute_gain_bps = float(best_score - current_score)
    relative_gain = absolute_gain_bps / max(abs(float(current_score)), epsilon)
    strict_improvement = bool(best_score > current_score)
    absolute_gate_pass = strict_improvement and absolute_gain_bps >= min_delta_gain_bps
    relative_gate_pass = strict_improvement and relative_gain >= min_relative_gain
    if gate_mode == "absolute":
        would_apply = absolute_gate_pass
    elif gate_mode == "relative":
        would_apply = relative_gate_pass
    else:
        would_apply = absolute_gate_pass and relative_gate_pass
    return {
        "current_score": float(current_score), "best_score": float(best_score),
        "absolute_gain_bps": absolute_gain_bps, "relative_gain": relative_gain,
        "gate_mode": gate_mode, "min_delta_gain_bps": float(min_delta_gain_bps),
        "min_relative_gain": float(min_relative_gain),
        "strict_improvement": strict_improvement,
        "absolute_gate_pass": absolute_gate_pass, "relative_gate_pass": relative_gate_pass,
        "would_apply": bool(would_apply),
    }


def evaluate_objective_gate(
    current_score: float,
    best_score: float,
    *,
    objective: str,
    absolute_threshold: float,
    relative_threshold: float,
    epsilon: float = RELATIVE_GAIN_EPSILON,
) -> dict[str, Any]:
    """Evaluate only the selected primary objective; scores are optimization-oriented."""
    if objective not in OBJECTIVES:
        raise ValueError(f"unsupported objective: {objective}")
    absolute_improvement = float(best_score - current_score)
    relative_improvement = absolute_improvement / max(abs(float(current_score)), epsilon)
    strict_improvement = bool(absolute_improvement > 0)
    absolute_gate_pass = strict_improvement and absolute_improvement >= absolute_threshold
    relative_gate_pass = strict_improvement and relative_improvement >= relative_threshold
    gate_passed = (
        absolute_gate_pass and relative_gate_pass
        if objective == "throughput"
        else absolute_gate_pass or relative_gate_pass
    )
    return {
        "current_score": float(current_score),
        "best_score": float(best_score),
        "absolute_gain_bps": absolute_improvement,
        "relative_gain": relative_improvement,
        "gate_mode": "objective_aware",
        "gate_policy": "objective_aware",
        "objective": objective,
        "min_delta_gain_bps": float(absolute_threshold),
        "min_relative_gain": float(relative_threshold),
        "strict_improvement": strict_improvement,
        "absolute_gate_pass": absolute_gate_pass,
        "relative_gate_pass": relative_gate_pass,
        "would_apply": bool(gate_passed),
    }


def evaluate_policy_gate(
    current_score: float,
    best_score: float,
    *,
    gate_policy: str,
    objective: str,
    gate_mode: str,
    absolute_threshold: float,
    relative_threshold: float,
) -> dict[str, Any]:
    if gate_policy == "legacy":
        return evaluate_gain_gate(
            current_score,
            best_score,
            gate_mode=gate_mode,
            min_delta_gain_bps=absolute_threshold,
            min_relative_gain=relative_threshold,
        )
    if gate_policy == "objective_aware":
        return evaluate_objective_gate(
            current_score,
            best_score,
            objective=objective,
            absolute_threshold=absolute_threshold,
            relative_threshold=relative_threshold,
        )
    raise ValueError(f"unsupported gate policy: {gate_policy}")


def _finite_or_none(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (int, np.integer)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, (float, np.floating)):
        value = float(value)
        return value if math.isfinite(value) else None
    return value


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return _finite_or_none(value)


def _objective_decision_units(target_mode: str) -> tuple[str, str]:
    if target_mode == "delta_bw_1s":
        return "bps", "bps"
    if target_mode == "delta_owd_1s":
        return "ms", "ms"
    if target_mode == "loss_risk_1s":
        return "ratio_0_to_1", "loss_risk_bytes"
    if target_mode == "delta_loss_1s":
        return "ratio_0_to_1", "loss_ratio"
    return "", _score_unit(target_mode)


def objective_decision_log_fields(
    req: dict[str, Any],
    *,
    target_mode: str,
    gate_policy: str,
    gate_objective: str,
    current_candidate_score: float | None,
    best_candidate_score: float | None,
    absolute_improvement: float | None,
    relative_improvement: float | None,
    gate_passed: bool,
    actual_applied: bool,
    skip_reason: str,
) -> dict[str, Any]:
    trigger_unit, candidate_unit = _objective_decision_units(target_mode)
    return _json_safe({
        "decision_stage": "primary_objective_gate",
        "variant": str(req.get("controller_variant") or ""),
        "path_id": int(req.get("path_id", 0) or 0),
        "trigger_mode": str(req.get("trigger_mode") or req.get("reason") or ""),
        "gate_policy": gate_policy,
        "gate_objective": gate_objective,
        "reference_value": req.get("reference_value"),
        "current_value": req.get("current_value"),
        "absolute_change": req.get("absolute_change"),
        "relative_change": req.get("relative_change"),
        "trigger_streak": int(req.get("trigger_streak", 0) or 0),
        "triggered": bool(req.get("triggered", False)),
        "current_candidate_score": current_candidate_score,
        "best_candidate_score": best_candidate_score,
        "absolute_improvement": absolute_improvement,
        "relative_improvement": relative_improvement,
        "gate_passed": bool(gate_passed),
        "actual_applied": bool(actual_applied),
        "skip_reason": skip_reason,
        "trigger_value_unit": trigger_unit,
        "candidate_score_unit": candidate_unit,
        "absolute_improvement_unit": candidate_unit,
        "secondary_guardrails": SECONDARY_GUARDRAILS_UNAVAILABLE,
    })


def _write_candidate_scores_csv(
    out_path: Path,
    *,
    candidates: list[dict[str, Any]],
    target_mode: str,
    path_id: int,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "path_id", "alpha", "beta", "gamma", "mean_prediction",
        "optimization_score", "is_current", "is_best", "target_mode",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in candidates:
            writer.writerow({
                "path_id": path_id,
                "alpha": row["alpha"],
                "beta": row["beta"],
                "gamma": row["gamma"],
                "mean_prediction": row["mean_prediction"],
                "optimization_score": row.get("optimization_score", row["mean_prediction"]),
                "is_current": int(bool(row["is_current"])),
                "is_best": int(bool(row["is_best"])),
                "target_mode": target_mode,
            })


def _append_audit_row(audit_csv: Path, row: dict[str, Any]) -> None:
    audit_csv.parent.mkdir(parents=True, exist_ok=True)
    write_header = not audit_csv.is_file() or audit_csv.stat().st_size == 0
    with audit_csv.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=AUDIT_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)
    try:
        os.chmod(audit_csv, 0o666)
    except OSError:
        pass


def _parse_path_id(req: dict[str, Any]) -> int | None:
    if "path_id" not in req:
        return None
    try:
        return int(req["path_id"])
    except (TypeError, ValueError):
        return None


def _load_request_and_samples(
    request_path: Path,
    samples_path: Path,
    *,
    skip_processed: bool,
    state_path: Path,
) -> tuple[dict | None, pd.DataFrame, str, int | None]:
    if not request_path.is_file():
        return None, pd.DataFrame(), "", None

    try:
        req = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[worker] skip: invalid request json: {exc}", file=sys.stderr)
        return None, pd.DataFrame(), "", None

    request_id = str(req.get("request_id") or "").strip()
    if not request_id:
        print("[worker] skip: request missing request_id", file=sys.stderr)
        return None, pd.DataFrame(), "", None

    path_id = _parse_path_id(req)
    if path_id is None:
        print(f"[worker] skip: request missing path_id request_id={request_id}", file=sys.stderr)
        return None, pd.DataFrame(), request_id, None

    for field in ("reason",):
        if not str(req.get(field) or "").strip():
            print(f"[worker] skip: request missing {field} request_id={request_id}", file=sys.stderr)
            return None, pd.DataFrame(), request_id, path_id

    if skip_processed:
        state = _load_state(state_path)
        processed: list[str] = list(state.get("processed_request_ids") or [])
        if request_id in processed:
            if request_path.is_file():
                request_path.unlink()
            return None, pd.DataFrame(), request_id, path_id

    df = _load_runtime_samples(samples_path)
    all_samples = _clean_runtime_samples(df)
    if all_samples.empty:
        print(f"[worker] skip: no valid runtime samples request_id={request_id}", file=sys.stderr)
        return None, pd.DataFrame(), request_id, path_id

    return req, all_samples, request_id, path_id


def _worker_log_line(log_file: Path | None, payload: dict[str, Any]) -> None:
    line = json.dumps(_json_safe(payload), separators=(",", ":"), sort_keys=True, allow_nan=False)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    else:
        print(line)


def _write_ready_file(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)
    try:
        path.chmod(0o666)
    except OSError:
        pass


def _physical_path_label(endpoint: str) -> str:
    if endpoint.startswith("10.0.1."):
        return "Path A"
    if endpoint.startswith("10.0.2."):
        return "Path B"
    return "unknown"


def _request_classification(req: dict[str, Any]) -> str:
    elapsed = float(req.get("experiment_elapsed_s", -1) or -1)
    if elapsed < 0:
        return "UNKNOWN"
    if elapsed < 90:
        return "PRE_DETERIORATION"
    if elapsed < 150:
        return "DURING_DETERIORATION"
    return "POST_DETERIORATION"


def _active_phase_allowed(target_mode: str, request_classification: str) -> bool:
    return not (
        target_mode in MINIMIZE_TARGETS
        and request_classification == "PRE_DETERIORATION"
    )


def _normalize_objective_field_names(payload: Any, target_mode: str) -> Any:
    suffix = {
        "delta_owd_1s": "ms",
        "delta_loss_1s": "loss_rate",
        "loss_risk_1s": "bytes",
    }.get(target_mode)
    if suffix is None:
        return payload
    key_map = {
        "absolute_gain_bps": f"objective_gain_{suffix}",
        "score_gain_bps": f"objective_gain_{suffix}",
        "min_delta_gain_bps": f"min_objective_improvement_{suffix}",
        "aggregate_gain_bps": f"aggregate_objective_gain_{suffix}",
        "gain_bps": f"objective_gain_{suffix}",
    }
    if isinstance(payload, dict):
        return {
            key_map.get(key, key): _normalize_objective_field_names(value, target_mode)
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [_normalize_objective_field_names(value, target_mode) for value in payload]
    return payload


def _classify_media_paths(samples: pd.DataFrame, min_rows: int, min_sender_byte_delta: int) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    if samples.empty or "path_id" not in samples.columns:
        return diagnostics
    for path_id, path_df in samples.groupby("path_id", sort=True):
        work = path_df.reset_index(drop=True)
        local_endpoint = ""
        if "local_endpoint" in work.columns:
            local_endpoints = work["local_endpoint"].dropna().astype(str)
            if not local_endpoints.empty:
                local_endpoint = local_endpoints.iloc[-1]
        endpoint = ""
        if "remote_endpoint" in work.columns:
            endpoints = work["remote_endpoint"].dropna().astype(str)
            if not endpoints.empty:
                endpoint = endpoints.iloc[-1]
        sent = pd.to_numeric(work.get("sender_bytes_total", pd.Series(dtype=float)), errors="coerce").dropna()
        sender_values = [int(value) for value in sent.tolist()]
        sender_first = sender_values[0] if sender_values else 0
        sender_last = sender_values[-1] if sender_values else 0
        sender_resets = sum(1 for before, after in zip(sender_values, sender_values[1:]) if after < before)
        sender_delta = sum(max(0, after-before) for before, after in zip(sender_values, sender_values[1:]))
        reasons: list[str] = []
        if len(work) < min_rows:
            reasons.append("insufficient_rows")
        if len(sender_values) < 2:
            reasons.append("missing_sender_bytes")
        elif sender_delta < min_sender_byte_delta:
            reasons.append("no_sender_byte_growth")
        missing_features = [name for name in FEATURES if name not in work.columns]
        if missing_features:
            reasons.append("missing_model_features")
        diagnostics.append({
            "path_id": int(path_id),
            "local_endpoint": local_endpoint,
            "endpoint": endpoint,
            "physical_path": _physical_path_label(endpoint),
            "rows": int(len(work)),
            "sender_bytes_first": sender_first,
            "sender_bytes_last": sender_last,
            "sender_byte_delta": sender_delta,
            "sender_counter_reset": bool(sender_resets),
            "sender_counter_reset_count": sender_resets,
            "eligible": not reasons,
            "exclusion_reason": ",".join(reasons),
            "samples": work,
        })
    return diagnostics


def _write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _process_multipath_request(
    *, req: dict[str, Any], samples: pd.DataFrame, request_id: str, model,
    coeffs_out: Path, response_out: Path, request_path: Path, archive_dir: Path, prev_coeffs_out: Path,
    target_mode: str, min_delta_gain_bps: float, min_relative_gain: float,
    gate_mode: str, gate_policy: str, objective: str,
    objective_relative_threshold: float, fixed_gamma: float | None, shadow: bool,
    log_file: Path | None, min_sender_byte_delta: int,
    changed_path_priority_shadow: bool,
    changed_path_ids: set[int],
    changed_path_gain_bps: float,
    min_aggregate_gain_bps: float,
    max_other_path_loss_ratio: float,
    max_other_path_loss_bps: float,
) -> bool:
    min_rows = max(1, int(req.get("min_samples_per_path", 1) or 1))
    path_diagnostics = _classify_media_paths(samples, min_rows, min_sender_byte_delta)
    eligible = [item for item in path_diagnostics if item["eligible"]]
    safe = _safe_archive_name(request_id)
    archive_dir.mkdir(parents=True, exist_ok=True)
    all_samples_path = archive_dir / f"qaccess_runtime_samples_{safe}_all_paths.csv"
    samples.to_csv(all_samples_path, index=False)

    coeffs_doc = load_coeffs_doc(coeffs_out)
    selected_path = int(req.get("path_id", 0) or 0)
    cur_alpha, cur_beta, cur_gamma, cur_source = resolve_path_coeffs(coeffs_doc, selected_path)
    if "current_alpha" in req:
        cur_alpha = float(req.get("current_alpha", cur_alpha) or cur_alpha)
        cur_beta = float(req.get("current_beta", cur_beta) or cur_beta)
        cur_gamma = float(req.get("current_gamma", cur_gamma) or cur_gamma)

    request_classification = _request_classification(req)
    initial_decision_fields = objective_decision_log_fields(
        req,
        target_mode=target_mode,
        gate_policy=gate_policy,
        gate_objective=objective,
        current_candidate_score=None,
        best_candidate_score=None,
        absolute_improvement=None,
        relative_improvement=None,
        gate_passed=False,
        actual_applied=False,
        skip_reason="decision_not_completed",
    ) if gate_policy == "objective_aware" else {}
    base_response: dict[str, Any] = {
        "request_id": request_id,
        "timestamp_ms": int(time.time() * 1000),
        "run_id": str(req.get("run_id") or ""),
        "target_mode": target_mode,
        "controller_variant": str(req.get("controller_variant") or ""),
        "objective": _objective_name(target_mode),
        "gate_policy": gate_policy,
        "primary_objective": objective,
        "optimization_direction": "minimize" if target_mode in MINIMIZE_TARGETS else "maximize",
        "score_unit": _score_unit(target_mode),
        "execution_mode": "shadow" if shadow else "active",
        "shadow": shadow,
        "shadow_mode": shadow,
        "request_classification": request_classification,
        "active_path_ids": [item["path_id"] for item in path_diagnostics],
        "eligible_path_ids": [item["path_id"] for item in eligible],
        "excluded_paths": [
            {key: item[key] for key in ("path_id", "local_endpoint", "endpoint", "physical_path", "rows", "sender_byte_delta", "sender_counter_reset", "exclusion_reason")}
            for item in path_diagnostics if not item["eligible"]
        ],
        "path_eligibility": [
            {key: item[key] for key in ("path_id", "local_endpoint", "endpoint", "physical_path", "rows", "sender_bytes_first", "sender_bytes_last", "sender_byte_delta", "sender_counter_reset", "eligible", "exclusion_reason")}
            for item in path_diagnostics
        ],
        "current_coefficients": {"alpha": cur_alpha, "beta": cur_beta, "gamma": cur_gamma},
        "aggregate_scoring": True,
        "aggregate_control_method": "traffic_weighted",
        "changed_path_priority_shadow": bool(changed_path_priority_shadow),
        "changed_path_ids": sorted(changed_path_ids),
        **initial_decision_fields,
    }

    if not eligible:
        response = {**base_response, "status": "SHADOW_SKIPPED_NO_MEDIA_PATH" if shadow else "ACTIVE_SKIPPED_NO_MEDIA_PATH", "would_apply": False, "actual_applied": False, "skip_reason": "no_eligible_media_paths"}
        atomic_write_json(archive_dir / f"qaccess_path_eligibility_{safe}.json", base_response["path_eligibility"])
        atomic_write_json(archive_dir / f"qaccess_multipath_shadow_audit_{safe}.json", response)
        _worker_log_line(log_file, response)
        if request_path.is_file():
            shutil.copy2(request_path, archive_dir / f"qaccess_update_request_{safe}.json")
            request_path.unlink()
        atomic_write_json(response_out, response)
        return True

    scored_paths: list[dict[str, Any]] = []
    for item in eligible:
        candidates, pred_current, _, _, _, _ = _score_all_candidates(
            item["samples"], model,
            cur_alpha=cur_alpha, cur_beta=cur_beta, cur_gamma=cur_gamma,
            fixed_gamma=fixed_gamma,
            target_mode=target_mode,
        )
        best = max(candidates, key=lambda row: row["optimization_score"])
        scored_paths.append({**item, "candidates": candidates, "pred_current": pred_current, "pred_best": best["mean_prediction"]})

    total_delta = sum(item["sender_byte_delta"] for item in scored_paths)
    if total_delta <= 0:
        response = {**base_response, "status": "SHADOW_SKIPPED_NO_MEDIA_PATH" if shadow else "ACTIVE_SKIPPED_NO_MEDIA_PATH", "would_apply": False, "actual_applied": False, "skip_reason": "zero_total_sender_byte_delta"}
        atomic_write_json(archive_dir / f"qaccess_path_eligibility_{safe}.json", base_response["path_eligibility"])
        _worker_log_line(log_file, response)
        if request_path.is_file():
            shutil.copy2(request_path, archive_dir / f"qaccess_update_request_{safe}.json")
            request_path.unlink()
        atomic_write_json(response_out, response)
        return True
    raw_weights = {str(item["path_id"]): item["sender_byte_delta"] for item in scored_paths}
    normalized_weights = {str(item["path_id"]): item["sender_byte_delta"] / total_delta for item in scored_paths}
    path_rank_maps: dict[int, dict[tuple[float, float, float], int]] = {}
    path_entries_by_coeff: dict[tuple[float, float, float], list[dict[str, Any]]] = {}
    for item in scored_paths:
        ranked = sorted(
            item["candidates"],
            key=lambda row: (-float(row["optimization_score"]), not bool(row["is_current"]), row["alpha"], row["beta"], row["gamma"]),
        )
        path_rank_maps[item["path_id"]] = {
            (float(row["alpha"]), float(row["beta"]), float(row["gamma"])): rank
            for rank, row in enumerate(ranked, start=1)
        }
    per_path_rows: list[dict[str, Any]] = []
    aggregate_rows: list[dict[str, Any]] = []
    for index, triple in enumerate(_candidate_triples(fixed_gamma)):
        alpha, beta, gamma = triple
        predictions = [float(item["candidates"][index]["mean_prediction"]) for item in scored_paths]
        optimization_scores = [_optimization_score(target_mode, prediction) for prediction in predictions]
        weights = [item["sender_byte_delta"] / total_delta for item in scored_paths]
        equal_prediction = float(np.mean(predictions))
        weighted_prediction = float(sum(pred * weight for pred, weight in zip(predictions, weights)))
        equal_score = float(np.mean(optimization_scores))
        weighted_score = float(sum(score * weight for score, weight in zip(optimization_scores, weights)))
        is_current = abs(alpha-cur_alpha) < 1e-9 and abs(beta-cur_beta) < 1e-9 and abs(gamma-cur_gamma) < 1e-9
        aggregate_rows.append({
            "request_id": request_id, "alpha": alpha, "beta": beta, "gamma": gamma,
            "equal_weight_prediction": equal_prediction,
            "byte_weighted_prediction": weighted_prediction,
            "equal_weight_score": equal_score,
            "byte_weighted_score": weighted_score,
            "equal_weight_gain": equal_score,
            "byte_weighted_gain": weighted_score,
            "is_current_tuple": int(is_current),
            "eligible_path_count": len(scored_paths),
            "eligible_path_ids": ",".join(str(item["path_id"]) for item in scored_paths),
        })
        for item, prediction, weight in zip(scored_paths, predictions, weights):
            path_entry = {
                "request_id": request_id, "path_id": item["path_id"], "local_endpoint": item["local_endpoint"],
                "remote_endpoint": item["endpoint"],
                "physical_path": item["physical_path"], "alpha": alpha, "beta": beta, "gamma": gamma,
                "row_count": item["rows"], "sender_byte_delta": item["sender_byte_delta"],
                "is_current_tuple": int(is_current), "path_pred_current": item["pred_current"],
                "path_pred_candidate": prediction, "mean_prediction": prediction,
                "optimization_score": _optimization_score(target_mode, prediction),
                "path_score_gain": _objective_improvement(target_mode, item["pred_current"], prediction),
                "candidate_rank_within_path": path_rank_maps[item["path_id"]][(alpha, beta, gamma)],
                "media_activity_weight": weight,
            }
            per_path_rows.append(path_entry)
            path_entries_by_coeff.setdefault((alpha, beta, gamma), []).append({
                "path_id": int(item["path_id"]),
                "local_endpoint": item["local_endpoint"],
                "remote_endpoint": item["endpoint"],
                "physical_path": item["physical_path"],
                "weight": float(weight),
                "current_score": float(item["pred_current"]),
                "candidate_score": float(prediction),
                "gain_bps": _objective_improvement(target_mode, item["pred_current"], prediction),
                "weighted_gain_bps": float(weight * _objective_improvement(target_mode, item["pred_current"], prediction)),
                "effect": "helps" if _objective_improvement(target_mode, item["pred_current"], prediction) > 0 else ("hurts" if _objective_improvement(target_mode, item["pred_current"], prediction) < 0 else "neutral"),
                "is_changed_path": int(item["path_id"]) in changed_path_ids,
            })

    current_row = next(row for row in aggregate_rows if row["is_current_tuple"])
    equal_ranked = sorted(aggregate_rows, key=lambda row: (-float(row["equal_weight_score"]), not bool(row["is_current_tuple"]), row["alpha"], row["beta"], row["gamma"]))
    weighted_ranked = sorted(aggregate_rows, key=lambda row: (-float(row["byte_weighted_score"]), not bool(row["is_current_tuple"]), row["alpha"], row["beta"], row["gamma"]))
    equal_best, weighted_best = equal_ranked[0], weighted_ranked[0]
    for rank, row in enumerate(equal_ranked, start=1):
        row["equal_weight_rank"] = rank
    for rank, row in enumerate(weighted_ranked, start=1):
        row["byte_weighted_rank"] = rank
    for row in aggregate_rows:
        row["equal_weight_gain"] = row["equal_weight_score"] - current_row["equal_weight_score"]
        row["byte_weighted_gain"] = row["byte_weighted_score"] - current_row["byte_weighted_score"]
    effective_relative_threshold = objective_relative_threshold if gate_policy == "objective_aware" else min_relative_gain
    equal_gate = evaluate_policy_gate(
        current_row["equal_weight_score"], equal_best["equal_weight_score"],
        gate_policy=gate_policy, objective=objective, gate_mode=gate_mode,
        absolute_threshold=min_delta_gain_bps, relative_threshold=effective_relative_threshold,
    )
    weighted_gate = evaluate_policy_gate(
        current_row["byte_weighted_score"], weighted_best["byte_weighted_score"],
        gate_policy=gate_policy, objective=objective, gate_mode=gate_mode,
        absolute_threshold=min_delta_gain_bps, relative_threshold=effective_relative_threshold,
    )

    def coeffs(row: dict[str, Any]) -> dict[str, float]:
        return {name: float(row[name]) for name in ("alpha", "beta", "gamma")}

    weighted_raw = coeffs(weighted_best)
    weighted_stepped = {
        "alpha": _apply_max_step(cur_alpha, weighted_raw["alpha"]),
        "beta": max(MIN_BETA_GAMMA, _apply_max_step(cur_beta, weighted_raw["beta"])),
        "gamma": max(MIN_BETA_GAMMA, _apply_max_step(cur_gamma, weighted_raw["gamma"])),
    }
    equal_raw = coeffs(equal_best)
    equal_stepped = {
        "alpha": _apply_max_step(cur_alpha, equal_raw["alpha"]),
        "beta": max(MIN_BETA_GAMMA, _apply_max_step(cur_beta, equal_raw["beta"])),
        "gamma": max(MIN_BETA_GAMMA, _apply_max_step(cur_gamma, equal_raw["gamma"])),
    }
    methods_agree = equal_raw == weighted_raw
    def aggregate_gate_report(gate: dict[str, Any], raw: dict[str, float], stepped: dict[str, float]) -> dict[str, Any]:
        return {
            **gate, "proposed_raw_coefficients": raw,
            "proposed_stepped_coefficients": stepped,
            "shadow_mode": shadow, "actual_applied": False,
        }

    owner_roles = sorted(set(str(item["samples"]["endpoint_role"].iloc[-1]) for item in scored_paths if "endpoint_role" in item["samples"]))
    owner_ok = owner_roles == ["server_downlink_sender"]
    proposed_differs = any(abs(weighted_stepped[name] - current) > 1e-9 for name, current in
                           (("alpha", cur_alpha), ("beta", cur_beta), ("gamma", cur_gamma)))
    phase_allowed = gate_policy == "objective_aware" or _active_phase_allowed(target_mode, request_classification)
    active_safety_ok = bool(
        not shadow
        and owner_ok
        and eligible
        and target_mode in ACTIVE_TARGET_MODES
        and proposed_differs
        and phase_allowed
    )
    actual_applied = bool(active_safety_ok and weighted_gate["would_apply"])
    active_skip_reason = ""
    if shadow:
        active_skip_reason = (
            "shadow_mode" if weighted_gate["would_apply"]
            else ("primary_gate_failed" if gate_policy == "objective_aware" else "aggregate_gate_not_met")
        )
    elif not actual_applied:
        if not phase_allowed:
            active_skip_reason = "target_not_allowed" if gate_policy == "objective_aware" else "pre_deterioration_apply_disabled"
        elif not owner_ok:
            active_skip_reason = "owner_not_allowed" if gate_policy == "objective_aware" else "owner_role_not_server_downlink_sender"
        elif target_mode not in ACTIVE_TARGET_MODES:
            active_skip_reason = "target_not_allowed" if gate_policy == "objective_aware" else "model_target_not_active_safe"
        elif not proposed_differs:
            active_skip_reason = "coefficients_unchanged" if gate_policy == "objective_aware" else "proposed_coefficients_unchanged"
        elif not weighted_gate["would_apply"]:
            active_skip_reason = "primary_gate_failed" if gate_policy == "objective_aware" else "aggregate_gate_not_met"

    decision_fields = objective_decision_log_fields(
        req,
        target_mode=target_mode,
        gate_policy=gate_policy,
        gate_objective=objective,
        current_candidate_score=current_row["byte_weighted_prediction"],
        best_candidate_score=weighted_best["byte_weighted_prediction"],
        absolute_improvement=weighted_gate["absolute_gain_bps"],
        relative_improvement=weighted_gate["relative_gain"],
        gate_passed=bool(weighted_gate["would_apply"]),
        actual_applied=actual_applied,
        skip_reason=active_skip_reason,
    ) if gate_policy == "objective_aware" else {}

    changed_path_priority = None
    changed_path_diagnoses: list[str] = []
    if changed_path_priority_shadow:
        changed_path_candidates = [
            _candidate_changed_path_metrics(
                candidate=row,
                path_entries=path_entries_by_coeff[(float(row["alpha"]), float(row["beta"]), float(row["gamma"]))],
                changed_path_ids=changed_path_ids,
                changed_path_gain_bps=changed_path_gain_bps,
                min_aggregate_gain_bps=min_aggregate_gain_bps,
                max_other_path_loss_ratio=max_other_path_loss_ratio,
                max_other_path_loss_bps=max_other_path_loss_bps,
            )
            for row in aggregate_rows
        ]
        changed_path_best = _select_changed_path_priority_candidate(changed_path_candidates)
        changed_path_raw = changed_path_best["coefficients"]
        changed_path_stepped = {
            "alpha": _apply_max_step(cur_alpha, changed_path_raw["alpha"]),
            "beta": max(MIN_BETA_GAMMA, _apply_max_step(cur_beta, changed_path_raw["beta"])),
            "gamma": max(MIN_BETA_GAMMA, _apply_max_step(cur_gamma, changed_path_raw["gamma"])),
        }
        changed_path_stepped_match = next(
            (row for row in changed_path_candidates if row["coefficients"] == changed_path_stepped),
            changed_path_best,
        )
        if changed_path_best["fig7_changed_path_would_apply"]:
            changed_path_diagnoses.append("changed_path_priority_would_apply")
        elif changed_path_best["changed_path_weighted_gain_bps"] < changed_path_gain_bps:
            changed_path_diagnoses.append("changed_path_priority_blocks_no_changed_path_gain")
        elif changed_path_best["aggregate_gain_bps"] <= min_aggregate_gain_bps:
            changed_path_diagnoses.append("changed_path_priority_blocks_negative_aggregate")
        else:
            changed_path_diagnoses.append("changed_path_priority_blocks_excessive_other_path_loss")
        if changed_path_raw != changed_path_stepped:
            if bool(changed_path_best["fig7_changed_path_would_apply"]) == bool(changed_path_stepped_match["fig7_changed_path_would_apply"]):
                changed_path_diagnoses.append("step_limit_not_decisive")
            else:
                changed_path_diagnoses.append("step_limit_decisive")
        changed_path_priority = {
            "enabled": True,
            "changed_path_ids": sorted(changed_path_ids),
            "thresholds": {
                "changed_path_gain_bps": changed_path_gain_bps,
                "min_aggregate_gain_bps": min_aggregate_gain_bps,
                "max_other_path_loss_ratio": max_other_path_loss_ratio,
                "max_other_path_loss_bps": max_other_path_loss_bps,
            },
            "best_candidate": {
                "coefficients": changed_path_raw,
                "stepped_coefficients": changed_path_stepped,
                "changed_path_raw_gain_bps": changed_path_best["changed_path_raw_gain_bps"],
                "changed_path_weighted_gain_bps": changed_path_best["changed_path_weighted_gain_bps"],
                "other_path_loss_bps": changed_path_best["other_path_loss_bps"],
                "other_path_gain_bps": changed_path_best["other_path_gain_bps"],
                "aggregate_gain_bps": changed_path_best["aggregate_gain_bps"],
                "changed_path_gate_pass": changed_path_best["changed_path_gate_pass"],
                "aggregate_safety_pass": changed_path_best["aggregate_safety_pass"],
                "other_path_loss_pass": changed_path_best["other_path_loss_pass"],
                "fig7_changed_path_would_apply": changed_path_best["fig7_changed_path_would_apply"],
                "per_path": changed_path_best["path_entries"],
            },
            "diagnoses": changed_path_diagnoses,
        }
    response = {
        **base_response,
        "status": "SHADOW_AGGREGATE_EVALUATED" if shadow else ("APPLIED_AGGREGATE" if actual_applied else "ACTIVE_AGGREGATE_SKIPPED"),
        "would_apply": bool(weighted_gate["would_apply"]),
        "would_apply_under_gate": bool(weighted_gate["would_apply"]),
        "actual_applied": actual_applied,
        "skip_reason": active_skip_reason,
        **decision_fields,
        "candidate_count": len(aggregate_rows),
        "unique_prediction_count": len({float(row["byte_weighted_score"]) for row in aggregate_rows}),
        "path_sender_byte_deltas": raw_weights,
        "path_weights": normalized_weights,
        "per_path_current_predictions": {str(item["path_id"]): item["pred_current"] for item in scored_paths},
        "per_path_best_predictions": {str(item["path_id"]): item["pred_best"] for item in scored_paths},
        "per_path_best_gains": {
            str(item["path_id"]): _objective_improvement(target_mode, item["pred_current"], item["pred_best"])
            for item in scored_paths
        },
        "equal_weight_current_prediction": current_row["equal_weight_prediction"],
        "equal_weight_best_prediction": equal_best["equal_weight_prediction"],
        "equal_weight_current": current_row["equal_weight_score"],
        "equal_weight_best": equal_best["equal_weight_score"],
        "equal_weight_gain": equal_gate["absolute_gain_bps"],
        "equal_weight_gate": equal_gate,
        "equal_weight": aggregate_gate_report(equal_gate, equal_raw, equal_stepped),
        "equal_weight_would_apply": bool(equal_gate["would_apply"]),
        "equal_weight_proposed_candidate": equal_raw,
        "equal_weight_proposed_stepped_coefficients": equal_stepped,
        "traffic_weighted_current": current_row["byte_weighted_score"],
        "traffic_weighted_best": weighted_best["byte_weighted_score"],
        "traffic_weighted_current_prediction": current_row["byte_weighted_prediction"],
        "traffic_weighted_best_prediction": weighted_best["byte_weighted_prediction"],
        "traffic_weighted_gain": weighted_gate["absolute_gain_bps"],
        "traffic_weighted_gate": weighted_gate,
        "traffic_weighted": aggregate_gate_report(weighted_gate, weighted_raw, weighted_stepped),
        "traffic_weighted_would_apply": bool(weighted_gate["would_apply"]),
        "traffic_weighted_proposed_candidate": weighted_raw,
        "traffic_weighted_proposed_stepped_coefficients": weighted_stepped,
        "aggregate_methods_agree": methods_agree,
        "proposed_stepped_coefficients": equal_stepped if methods_agree else None,
        "applied_coefficients": weighted_stepped if actual_applied else {"alpha": cur_alpha, "beta": cur_beta, "gamma": cur_gamma},
        "gate_mode": gate_mode,
        "gate_policy": gate_policy,
        "min_delta_gain_bps": min_delta_gain_bps,
        "min_relative_gain": min_relative_gain,
        "absolute_gain_bps": weighted_gate["absolute_gain_bps"],
        "relative_gain": weighted_gate["relative_gain"],
        "absolute_gate_pass": weighted_gate["absolute_gate_pass"],
        "relative_gate_pass": weighted_gate["relative_gate_pass"],
        "score_gain": weighted_gate["absolute_gain_bps"],
        "score_gain_bps": weighted_gate["absolute_gain_bps"],
        "owner_role_check": owner_ok,
        "owner_roles": owner_roles,
        "proposed_coefficients_differ": proposed_differs,
        "source": cur_source,
    }
    aggregate_diagnoses: list[str] = []
    aggregate_path_entries = path_entries_by_coeff[(weighted_raw["alpha"], weighted_raw["beta"], weighted_raw["gamma"])]
    positives = [row for row in aggregate_path_entries if float(row["gain_bps"]) > 0]
    negatives = [row for row in aggregate_path_entries if float(row["gain_bps"]) < 0]
    if bool(weighted_gate["would_apply"]):
        aggregate_diagnoses.append("aggregate_accepts")
    elif positives and negatives:
        aggregate_diagnoses.append("aggregate_blocks_due_to_cross_path_tradeoff")
    elif not positives:
        aggregate_diagnoses.append("aggregate_blocks_no_path_improvement")
    else:
        aggregate_diagnoses.append("aggregate_blocks_below_threshold")
    step_limit_label = ""
    if weighted_raw != weighted_stepped:
        stepped_gate = evaluate_policy_gate(
            current_row["byte_weighted_score"],
            next(
                (row["byte_weighted_score"] for row in aggregate_rows
                 if abs(float(row["alpha"]) - weighted_stepped["alpha"]) < 1e-9
                 and abs(float(row["beta"]) - weighted_stepped["beta"]) < 1e-9
                 and abs(float(row["gamma"]) - weighted_stepped["gamma"]) < 1e-9),
                weighted_best["byte_weighted_score"],
            ),
            gate_policy=gate_policy, objective=objective, gate_mode=gate_mode,
            absolute_threshold=min_delta_gain_bps,
            relative_threshold=effective_relative_threshold,
        )
        if bool(stepped_gate["would_apply"]) == bool(weighted_gate["would_apply"]):
            step_limit_label = "step_limit_not_decisive"
        else:
            step_limit_label = "step_limit_decisive"
    if step_limit_label:
        aggregate_diagnoses.append(step_limit_label)
    response["aggregate_diagnoses"] = aggregate_diagnoses
    if changed_path_priority is not None:
        if step_limit_label:
            changed_path_diagnoses.append(step_limit_label)
            changed_path_priority["diagnoses"] = changed_path_diagnoses
        response["changed_path_priority_shadow"] = changed_path_priority
        response["changed_path_priority_diagnoses"] = changed_path_priority["diagnoses"]
    if actual_applied:
        response["traffic_weighted"]["actual_applied"] = True
        _assert_runtime_coeffs_path(coeffs_out)
        backup_path = _save_coeffs_backup(coeffs_out, archive_dir, request_id, prev_coeffs_out)
        existing_path_ids = {
            int(value) for value in (coeffs_doc.get("paths") or {})
            if str(value).isdigit()
        }
        update_path_ids = sorted(existing_path_ids | {int(item["path_id"]) for item in scored_paths})
        response["updated_path_ids"] = update_path_ids
        for update_path_id in update_path_ids:
            update_path_coeffs_locked(
                coeffs_out, update_path_id,
                alpha=weighted_stepped["alpha"], beta=weighted_stepped["beta"], gamma=weighted_stepped["gamma"],
                metadata={
                    "source": "qaccess_update_worker.py", "execution_mode": "active",
                    "aggregate_scoring": True, "aggregate_control_method": "traffic_weighted",
                    "request_id": request_id, "target_mode": target_mode, "gate_mode": gate_mode,
                    (
                        "absolute_gain_bps"
                        if target_mode == "delta_bw_1s"
                        else f"objective_gain_{_score_unit(target_mode)}"
                    ): weighted_gate["absolute_gain_bps"],
                    "relative_gain": weighted_gate["relative_gain"],
                    "previous_coeffs_backup": str(backup_path) if backup_path else "",
                },
            )
    response = _normalize_objective_field_names(response, target_mode)
    _write_rows_csv(archive_dir / f"qaccess_candidate_scores_{safe}_per_path.csv", per_path_rows)
    _write_rows_csv(archive_dir / f"qaccess_candidate_scores_{safe}_aggregate.csv", aggregate_rows)
    atomic_write_json(archive_dir / f"qaccess_path_eligibility_{safe}.json", base_response["path_eligibility"])
    atomic_write_json(archive_dir / f"qaccess_multipath_shadow_audit_{safe}.json", response)
    if changed_path_priority is not None:
        atomic_write_json(
            archive_dir / f"qaccess_changed_path_priority_{safe}.json",
            {
                "request_id": request_id,
                "phase_classification": base_response["request_classification"],
                "changed_path_ids": sorted(changed_path_ids),
                "aggregate_decision": {
                    "candidate": weighted_raw,
                    "stepped_candidate": weighted_stepped,
                    "aggregate_gain_bps": weighted_gate["absolute_gain_bps"],
                    "relative_gain": weighted_gate["relative_gain"],
                    "absolute_gate_pass": weighted_gate["absolute_gate_pass"],
                    "relative_gate_pass": weighted_gate["relative_gate_pass"],
                    "aggregate_would_apply": bool(weighted_gate["would_apply"]),
                    "actual_applied": actual_applied,
                    "diagnoses": aggregate_diagnoses,
                },
                "changed_path_priority_decision": changed_path_priority["best_candidate"],
                "diagnoses": changed_path_priority["diagnoses"],
            },
        )
    _worker_log_line(log_file, response)
    if request_path.is_file():
        shutil.copy2(request_path, archive_dir / f"qaccess_update_request_{safe}.json")
        request_path.unlink()
    atomic_write_json(response_out, response)
    return True


def _process_request(
    request_path: Path,
    samples_path: Path,
    model_path: Path,
    coeffs_out: Path,
    response_out: Path,
    state_path: Path,
    archive_dir: Path,
    prev_coeffs_out: Path,
    audit_csv: Path,
    mode: str,
    target_mode: str,
    min_improvement_pct: float,
    min_delta_gain_bps: float,
    min_relative_delta_gain: float,
    *,
    shadow: bool,
    aggregate_multipath: bool = False,
    gate_mode: str = "absolute",
    gate_policy: str = "legacy",
    objective: str = "throughput",
    objective_relative_threshold: float = DEFAULT_OBJECTIVE_T_RELATIVE_GAIN,
    min_relative_gain: float = DEFAULT_MIN_RELATIVE_GAIN,
    fixed_gamma: float | None = None,
    log_file: Path | None = None,
    min_sender_byte_delta: int = 1,
    changed_path_priority_shadow: bool = False,
    changed_path_ids: set[int] | None = None,
    changed_path_gain_bps: float = DEFAULT_CHANGED_PATH_GAIN_BPS,
    min_aggregate_gain_bps: float = DEFAULT_MIN_AGGREGATE_GAIN_BPS,
    max_other_path_loss_ratio: float = DEFAULT_MAX_OTHER_PATH_LOSS_RATIO,
    max_other_path_loss_bps: float = DEFAULT_MAX_OTHER_PATH_LOSS_BPS,
) -> bool:
    req, samples, request_id, path_id = _load_request_and_samples(
        request_path,
        samples_path,
        skip_processed=not shadow,
        state_path=state_path,
    )
    if req is None or samples.empty or not request_id or path_id is None:
        return False

    if mode != "rf":
        print(f"[worker] unsupported mode {mode!r}", file=sys.stderr)
        return False
    if not model_path.is_file():
        print(f"[worker] missing model {model_path}", file=sys.stderr)
        return False

    model = joblib.load(model_path)
    if shadow or aggregate_multipath:
        return _process_multipath_request(
            req=req, samples=samples, request_id=request_id, model=model,
            coeffs_out=coeffs_out, response_out=response_out, request_path=request_path,
            archive_dir=archive_dir, prev_coeffs_out=prev_coeffs_out, target_mode=target_mode,
            min_delta_gain_bps=min_delta_gain_bps,
            min_relative_gain=min_relative_gain, gate_mode=gate_mode,
            gate_policy=gate_policy, objective=objective,
            objective_relative_threshold=objective_relative_threshold,
            fixed_gamma=fixed_gamma, shadow=shadow, log_file=log_file,
            min_sender_byte_delta=min_sender_byte_delta,
            changed_path_priority_shadow=changed_path_priority_shadow,
            changed_path_ids=changed_path_ids or set(DEFAULT_CHANGED_PATH_IDS),
            changed_path_gain_bps=changed_path_gain_bps,
            min_aggregate_gain_bps=min_aggregate_gain_bps,
            max_other_path_loss_ratio=max_other_path_loss_ratio,
            max_other_path_loss_bps=max_other_path_loss_bps,
        )

    samples = _filter_samples_by_path(samples, path_id)
    if samples.empty:
        print(f"[worker] skip: no valid runtime samples for path_id={path_id}", file=sys.stderr)
        return False
    coeffs_doc = load_coeffs_doc(coeffs_out)
    cur_alpha, cur_beta, cur_gamma, cur_source = resolve_path_coeffs(coeffs_doc, path_id)
    if "current_alpha" in req:
        cur_alpha = float(req.get("current_alpha", cur_alpha) or cur_alpha)
        cur_beta = float(req.get("current_beta", cur_beta) or cur_beta)
        cur_gamma = float(req.get("current_gamma", cur_gamma) or cur_gamma)

    candidates, pred_current, best_alpha, best_beta, best_gamma, pred_best = _score_all_candidates(
        samples,
        model,
        cur_alpha=cur_alpha,
        cur_beta=cur_beta,
        cur_gamma=cur_gamma,
        fixed_gamma=fixed_gamma,
        target_mode=target_mode,
    )
    non_current_candidates = [row for row in candidates if not row["is_current"]]
    best_non_current = max(non_current_candidates, key=lambda row: row["optimization_score"])
    unique_prediction_count = len({float(row["mean_prediction"]) for row in candidates})
    best_non_current_gain = _objective_improvement(target_mode, pred_current, best_non_current["mean_prediction"])

    if gate_policy == "objective_aware":
        policy_gate = evaluate_policy_gate(
            _optimization_score(target_mode, pred_current),
            _optimization_score(target_mode, pred_best),
            gate_policy=gate_policy,
            objective=objective,
            gate_mode=gate_mode,
            absolute_threshold=min_delta_gain_bps,
            relative_threshold=objective_relative_threshold,
        )
        improvement_ok = bool(policy_gate["would_apply"])
        gate_type = "primary_objective_gate"
        skip_reason = "primary_gate_failed"
        gate_threshold = min_delta_gain_bps
        score_gain_bps = policy_gate["absolute_gain_bps"]
        gate_detail = (
            f"absolute_threshold={min_delta_gain_bps:g} "
            f"relative_threshold={objective_relative_threshold:g}"
        )
    else:
        policy_gate = None
        improvement_ok, gate_type, skip_reason, gate_threshold, score_gain_bps, gate_detail = _evaluate_gate(
            target_mode,
            pred_current,
            pred_best,
            min_improvement_pct=min_improvement_pct,
            min_delta_gain_bps=min_delta_gain_bps,
            min_relative_delta_gain=min_relative_delta_gain,
        )

    applied_alpha = _apply_max_step(cur_alpha, best_alpha)
    applied_beta = max(MIN_BETA_GAMMA, _apply_max_step(cur_beta, best_beta))
    applied_gamma = max(MIN_BETA_GAMMA, _apply_max_step(cur_gamma, best_gamma))
    if fixed_gamma is not None:
        applied_gamma = fixed_gamma

    mean_gain_before, mean_backoff_before = _mean_gain_backoff(samples, cur_alpha, cur_beta, cur_gamma)
    active_update = improvement_ok and not shadow
    final_alpha = applied_alpha if active_update else cur_alpha
    final_beta = applied_beta if active_update else cur_beta
    final_gamma = applied_gamma if active_update else cur_gamma
    mean_gain_after, mean_backoff_after = _mean_gain_backoff(samples, final_alpha, final_beta, final_gamma)

    ts_ms = int(time.time() * 1000)
    metric_field = _metric_field(target_mode)
    score_gain = _objective_improvement(target_mode, pred_current, pred_best)
    run_id = str(req.get("run_id") or "")
    reason = str(req.get("reason") or "")
    n_samples = int(len(samples))

    execution_mode = "shadow" if shadow else "active"
    decision_skip_reason = (
        "" if active_update
        else (
            "shadow_mode" if shadow and improvement_ok
            else ("primary_gate_failed" if gate_policy == "objective_aware" else skip_reason)
        )
    )
    decision_fields = objective_decision_log_fields(
        req,
        target_mode=target_mode,
        gate_policy=gate_policy,
        gate_objective=objective,
        current_candidate_score=pred_current,
        best_candidate_score=pred_best,
        absolute_improvement=score_gain,
        relative_improvement=(
            policy_gate["relative_gain"] if policy_gate is not None
            else score_gain / max(abs(float(pred_current)), RELATIVE_GAIN_EPSILON)
        ),
        gate_passed=bool(improvement_ok),
        actual_applied=bool(active_update),
        skip_reason=decision_skip_reason,
    ) if gate_policy == "objective_aware" else {}
    response: dict[str, Any] = {
        "request_id": request_id,
        "path_id": path_id,
        "timestamp_ms": ts_ms,
        "status": "ok" if improvement_ok else "skipped",
        "request_reason": reason,
        "n_samples": n_samples,
        "run_id": run_id,
        "target_mode": target_mode,
        "objective": _objective_name(target_mode),
        "primary_objective": objective,
        "gate_policy": gate_policy,
        "optimization_direction": "minimize" if target_mode in MINIMIZE_TARGETS else "maximize",
        "score_unit": _score_unit(target_mode),
        "execution_mode": execution_mode,
        "shadow": shadow,
        "shadow_mode": shadow,
        "would_apply": bool(improvement_ok),
        "candidate_count": len(candidates),
        "unique_prediction_count": unique_prediction_count,
        "best_non_current_coefficients": {
            "alpha": best_non_current["alpha"],
            "beta": best_non_current["beta"],
            "gamma": best_non_current["gamma"],
        },
        "best_non_current_prediction": best_non_current["mean_prediction"],
        "best_non_current_gain": best_non_current_gain,
        "pred_current": pred_current,
        "pred_best": pred_best,
        "score_gain": score_gain,
        "score_gain_bps": score_gain_bps,
        "gate_type": gate_type,
        "gate_threshold": gate_threshold,
        "current_alpha": cur_alpha,
        "current_beta": cur_beta,
        "current_gamma": cur_gamma,
        "current_source": cur_source,
        "candidate_alpha": best_alpha,
        "candidate_beta": best_beta,
        "candidate_gamma": best_gamma,
        "final_alpha": final_alpha,
        "final_beta": final_beta,
        "final_gamma": final_gamma,
        "proposed_stepped_coefficients": {
            "alpha": applied_alpha,
            "beta": applied_beta,
            "gamma": applied_gamma,
        },
        "mean_gain_before": mean_gain_before,
        "mean_gain_after": mean_gain_after,
        "mean_backoff_before": mean_backoff_before,
        "mean_backoff_after": mean_backoff_after,
        "skip_reason": decision_skip_reason,
        "fixed_gamma": fixed_gamma,
        **decision_fields,
    }

    if target_mode == "delta_bw_1s":
        response["pred_current_delta_bps"] = pred_current
        response["pred_best_delta_bps"] = pred_best
    elif target_mode == "delta_owd_1s":
        response["pred_current_delta_owd_ms"] = pred_current
        response["pred_best_delta_owd_ms"] = pred_best
    elif target_mode == "delta_loss_1s":
        response["pred_current_delta_loss_rate"] = pred_current
        response["pred_best_delta_loss_rate"] = pred_best
    elif target_mode == "loss_risk_1s":
        response["pred_current_loss_risk_bytes"] = pred_current
        response["pred_best_loss_risk_bytes"] = pred_best

    _assert_runtime_coeffs_path(coeffs_out)

    scores_path = archive_dir / f"qaccess_candidate_scores_{_safe_archive_name(request_id)}_path{path_id}.csv"
    _write_candidate_scores_csv(scores_path, candidates=candidates, target_mode=target_mode, path_id=path_id)

    audit_path = archive_dir / f"qaccess_per_path_audit_{_safe_archive_name(request_id)}_path{path_id}.json"
    audit_payload = {
        **response,
        "candidates": candidates,
        "candidate_scores_csv": str(scores_path.resolve()),
    }
    atomic_write_json(audit_path, audit_payload)

    _append_audit_row(audit_csv, {
        "timestamp_ms": ts_ms,
        "request_id": request_id,
        "run_id": run_id,
        "path_id": path_id,
        "reason": reason,
        "mode": mode,
        "shadow": int(shadow),
        "row_count": n_samples,
        "current_alpha": cur_alpha,
        "current_beta": cur_beta,
        "current_gamma": cur_gamma,
        "current_source": cur_source,
        "candidate_alpha": best_alpha,
        "candidate_beta": best_beta,
        "candidate_gamma": best_gamma,
        "applied_alpha": final_alpha,
        "applied_beta": final_beta,
        "applied_gamma": final_gamma,
        "pred_current_delta_bps": pred_current if target_mode == "delta_bw_1s" else "",
        "pred_best_delta_bps": pred_best if target_mode == "delta_bw_1s" else "",
        "score_gain_bps": score_gain_bps if score_gain_bps is not None else score_gain,
        "mean_gain_before": mean_gain_before,
        "mean_gain_after": mean_gain_after,
        "mean_backoff_before": mean_backoff_before,
        "mean_backoff_after": mean_backoff_after,
        "gate_type": gate_type,
        "status": response["status"],
        "fixed_gamma": fixed_gamma if fixed_gamma is not None else "",
    })

    if active_update:
        backup_path = _save_coeffs_backup(coeffs_out, archive_dir, request_id, prev_coeffs_out)
        metadata = {
            "source": "qaccess_update_worker.py",
            "metric": metric_field,
            metric_field: pred_best,
            "target_mode": target_mode,
            "n_samples": n_samples,
            "request_id": request_id,
            "path_id": path_id,
            "mode": mode,
            "previous_alpha": cur_alpha,
            "previous_beta": cur_beta,
            "previous_gamma": cur_gamma,
            "previous_coeffs_backup": str(backup_path) if backup_path else "",
            "gate_type": gate_type,
            "gate_threshold": gate_threshold,
            "pred_current": pred_current,
            "pred_best": pred_best,
            "score_gain": score_gain,
            "score_gain_bps": score_gain_bps,
        }
        update_path_coeffs_locked(
            coeffs_out,
            path_id,
            alpha=applied_alpha,
            beta=applied_beta,
            gamma=applied_gamma,
            metadata=metadata,
        )
        response.update({
            "status": "APPLIED",
            "alpha": applied_alpha,
            "beta": applied_beta,
            "gamma": applied_gamma,
            "applied_alpha": applied_alpha,
            "applied_beta": applied_beta,
            "applied_gamma": applied_gamma,
            "coeffs_out": str(coeffs_out.resolve()),
            "previous_coeffs_backup": str(backup_path) if backup_path else "",
            "previous_coeffs_prev": str(prev_coeffs_out.resolve()) if backup_path else "",
        })
        _worker_log_line(
            log_file,
            {
                **decision_fields,
                "timestamp_ms": ts_ms,
                "request_id": request_id,
                "target_mode": target_mode,
                "path_id": path_id,
                "selected_path": path_id,
                "execution_mode": execution_mode,
                "shadow_mode": shadow,
                "current_coefficients": {"alpha": cur_alpha, "beta": cur_beta, "gamma": cur_gamma},
                "candidate_coefficients": {"alpha": best_alpha, "beta": best_beta, "gamma": best_gamma},
                "proposed_raw_coefficients": {"alpha": best_alpha, "beta": best_beta, "gamma": best_gamma},
                "proposed_stepped_coefficients": {
                    "alpha": applied_alpha, "beta": applied_beta, "gamma": applied_gamma,
                },
                "applied_coefficients": {"alpha": applied_alpha, "beta": applied_beta, "gamma": applied_gamma},
                "would_apply": True,
                "candidate_count": len(candidates),
                "unique_score_count": unique_prediction_count,
                "unique_prediction_count": unique_prediction_count,
                "pred_current": pred_current,
                "pred_best": pred_best,
                "score_gain": score_gain_bps if score_gain_bps is not None else score_gain,
                "gate_threshold": gate_threshold,
                "status": "APPLIED",
                "skip_reason": "",
            },
        )
    else:
        response.update({
            "alpha": cur_alpha,
            "beta": cur_beta,
            "gamma": cur_gamma,
        })
        status = "SHADOW_WOULD_APPLY" if shadow and improvement_ok else ("SHADOW_SKIPPED" if shadow else "SKIPPED")
        response["status"] = status
        _worker_log_line(
            log_file,
            {
                **decision_fields,
                "timestamp_ms": ts_ms,
                "request_id": request_id,
                "target_mode": target_mode,
                "path_id": path_id,
                "selected_path": path_id,
                "execution_mode": execution_mode,
                "shadow_mode": shadow,
                "current_coefficients": {"alpha": cur_alpha, "beta": cur_beta, "gamma": cur_gamma},
                "candidate_coefficients": {"alpha": best_alpha, "beta": best_beta, "gamma": best_gamma},
                "proposed_raw_coefficients": {"alpha": best_alpha, "beta": best_beta, "gamma": best_gamma},
                "applied_coefficients": {"alpha": cur_alpha, "beta": cur_beta, "gamma": cur_gamma},
                "proposed_stepped_coefficients": {
                    "alpha": applied_alpha, "beta": applied_beta, "gamma": applied_gamma,
                },
                "would_apply": bool(improvement_ok),
                "candidate_count": len(candidates),
                "unique_score_count": unique_prediction_count,
                "unique_prediction_count": unique_prediction_count,
                "best_non_current_coefficients": {
                    "alpha": best_non_current["alpha"],
                    "beta": best_non_current["beta"],
                    "gamma": best_non_current["gamma"],
                },
                "best_non_current_prediction": best_non_current["mean_prediction"],
                "best_non_current_gain": best_non_current_gain,
                "pred_current": pred_current,
                "pred_best": pred_best,
                "score_gain": score_gain_bps if score_gain_bps is not None else score_gain,
                "gate_threshold": gate_threshold,
                "status": status,
                "skip_reason": decision_skip_reason,
            },
        )

    if not shadow:
        state = _load_state(state_path)
        processed: list[str] = list(state.get("processed_request_ids") or [])
        processed.append(request_id)
        state["processed_request_ids"] = processed[-200:]
        state["last_processed_request_id"] = request_id
        _save_state(state_path, state)

    try:
        _archive_and_truncate_buffer(
            samples_path,
            archive_dir,
            request_id,
            request_path,
            path_id,
            shadow=shadow,
        )
    except (OSError, PermissionError) as exc:
        print(
            f"[worker] warning: archive/truncate failed for request_id={request_id} path_id={path_id}: {exc}",
            file=sys.stderr,
        )
    atomic_write_json(response_out, response)
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Q-ACCeSS-T Phase 2 buffer-full update worker (per-subflow)")
    ap.add_argument("--request", type=Path, default=DEFAULT_REQUEST)
    ap.add_argument("--runtime-samples", type=Path, default=DEFAULT_SAMPLES)
    ap.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    ap.add_argument("--model-metadata", type=Path, default=DEFAULT_MODEL_METADATA)
    ap.add_argument("--coeffs-out", type=Path, default=DEFAULT_COEFFS)
    ap.add_argument("--response-out", type=Path, default=DEFAULT_RESPONSE)
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    ap.add_argument("--audit-csv", type=Path, default=DEFAULT_AUDIT_CSV)
    ap.add_argument("--mode", choices=["rf"], default="rf")
    ap.add_argument("--controller-variant", choices=sorted(VARIANT_TARGETS), default="qaccess_t")
    ap.add_argument("--poll-interval", type=float, default=5.0)
    ap.add_argument(
        "--target-mode",
        choices=TARGET_MODES,
        default="delta_bw_1s",
        help="Prediction target and gate semantics (default: delta_bw_1s)",
    )
    ap.add_argument(
        "--min-improvement-pct",
        type=float,
        default=DEFAULT_MIN_IMPROVEMENT_PCT,
        help="Minimum predicted throughput improvement (%%) for next_bw_bps gate (default: 3.0)",
    )
    ap.add_argument(
        "--min-delta-gain-bps",
        type=float,
        default=DEFAULT_MIN_DELTA_GAIN_BPS,
        help="Minimum pred_best - pred_current (bps) for delta_bw_1s gate (default: 500000)",
    )
    ap.add_argument(
        "--min-objective-improvement",
        type=float,
        default=None,
        help="Minimum predicted reduction; objective-aware defaults are D=10 ms and L=4096 bytes",
    )
    ap.add_argument("--gate-mode", choices=GATE_MODES, default="absolute")
    ap.add_argument("--gate-policy", choices=GATE_POLICIES, default="legacy")
    ap.add_argument(
        "--objective", choices=OBJECTIVES, default=None,
        help="Primary objective; defaults to the objective implied by --controller-variant",
    )
    ap.add_argument(
        "--min-relative-gain", type=float, default=DEFAULT_MIN_RELATIVE_GAIN,
        help="Minimum (best-current)/max(abs(current), epsilon) for relative/hybrid gates (default: 0.03)",
    )
    ap.add_argument(
        "--min-objective-relative-improvement",
        type=float,
        default=None,
        help="Objective-aware relative threshold (T 0.05, D 0.10, L 0.25 by default)",
    )
    ap.add_argument(
        "--min-relative-delta-gain",
        type=float,
        default=DEFAULT_MIN_RELATIVE_DELTA_GAIN,
        help="Minimum pred_best - pred_current for relative_delta_bw_1s gate (default: 0.01)",
    )
    ap.add_argument(
        "--min-sender-byte-delta",
        type=int,
        default=1,
        help="Minimum positive sender-byte growth within one request buffer for media eligibility (default: 1)",
    )
    ap.add_argument(
        "--changed-path-priority-shadow",
        action="store_true",
        help="Evaluate Fig.7 changed-path-priority diagnostics in shadow only; no coefficient changes",
    )
    ap.add_argument(
        "--changed-path-ids",
        nargs="+",
        type=int,
        default=list(DEFAULT_CHANGED_PATH_IDS),
        help="Path IDs treated as changed/impaired for changed-path shadow scoring (default: 3)",
    )
    ap.add_argument(
        "--changed-path-gain-bps",
        type=float,
        default=DEFAULT_CHANGED_PATH_GAIN_BPS,
        help="Minimum changed-path weighted gain for changed-path shadow apply diagnosis (default: 100000)",
    )
    ap.add_argument(
        "--min-aggregate-gain-bps",
        type=float,
        default=DEFAULT_MIN_AGGREGATE_GAIN_BPS,
        help="Minimum aggregate gain required by changed-path shadow safety (default: 0)",
    )
    ap.add_argument(
        "--max-other-path-loss-ratio",
        type=float,
        default=DEFAULT_MAX_OTHER_PATH_LOSS_RATIO,
        help="Maximum allowed ratio of other-path loss to changed-path weighted gain (default: 0.75)",
    )
    ap.add_argument(
        "--max-other-path-loss-bps",
        type=float,
        default=DEFAULT_MAX_OTHER_PATH_LOSS_BPS,
        help="Maximum allowed absolute other-path loss for changed-path shadow safety (default: 200000)",
    )
    ap.add_argument(
        "--prev-coeffs-out",
        type=Path,
        default=DEFAULT_COEFFS_PREV,
        help="Latest pre-update runtime coefficients copy for audit/rollback",
    )
    ap.add_argument(
        "--shadow-per-subflow",
        action="store_true",
        help="Evaluate aggregate multipath candidates and write Shadow artifacts only; no runtime coefficient changes",
    )
    ap.add_argument(
        "--aggregate-multipath", action="store_true",
        help="Use aggregate multipath scoring; required for active aggregate updates",
    )
    ap.add_argument(
        "--fixed-gamma",
        type=float,
        default=None,
        help="Filter candidate grid to this exact gamma before scoring (e.g. 0.1)",
    )
    ap.add_argument("--once", action="store_true", help="Process one request if present, then exit")
    ap.add_argument(
        "--validate-model-only",
        action="store_true",
        help="Validate model metadata, target and feature compatibility, print JSON, then exit",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Alias for Shadow evaluation --once",
    )
    ap.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Append structured JSON update lines to this file (worker.log)",
    )
    ap.add_argument(
        "--ready-file",
        type=Path,
        default=None,
        help="Write a JSON readiness marker after startup validation succeeds",
    )
    args = ap.parse_args()
    expected_objective = VARIANT_OBJECTIVES[args.controller_variant]
    if args.objective is None:
        args.objective = expected_objective
    if args.objective != expected_objective:
        ap.error(
            f"controller variant {args.controller_variant} requires objective "
            f"{expected_objective}, got {args.objective}"
        )
    if args.min_objective_relative_improvement is None:
        args.min_objective_relative_improvement = {
            "throughput": DEFAULT_OBJECTIVE_T_RELATIVE_GAIN,
            "delay": DEFAULT_OBJECTIVE_D_RELATIVE_REDUCTION,
            "loss": DEFAULT_OBJECTIVE_L_RELATIVE_REDUCTION,
        }[args.objective]
    if args.min_objective_improvement is None:
        args.min_objective_improvement = (
            {
                "delay": DEFAULT_OBJECTIVE_D_REDUCTION_MS,
                "loss": DEFAULT_OBJECTIVE_L_REDUCTION_BYTES,
            }.get(args.objective, 0.0)
            if args.gate_policy == "objective_aware"
            else 0.0
        )
    if args.target_mode not in VARIANT_TARGETS[args.controller_variant]:
        ap.error(
            f"controller variant {args.controller_variant} is incompatible with target {args.target_mode}"
        )

    shadow = bool(args.shadow_per_subflow or args.dry_run)
    fixed_gamma = args.fixed_gamma
    log_file = args.log_file.resolve() if args.log_file else None

    gate_desc = (
        f"min_improvement_pct={args.min_improvement_pct}"
        if args.target_mode == "next_bw_bps"
        else (
            f"min_delta_gain_bps={args.min_delta_gain_bps}"
            if args.target_mode == "delta_bw_1s"
            else (
                f"min_objective_improvement={args.min_objective_improvement}"
                if args.target_mode in MINIMIZE_TARGETS
                else f"min_relative_delta_gain={args.min_relative_delta_gain}"
            )
        )
    )
    if args.target_mode == "delta_bw_1s":
        gate_desc = (
            f"gate_mode={args.gate_mode} min_delta_gain_bps={args.min_delta_gain_bps} "
            f"min_relative_gain={args.min_relative_gain}"
        )
    model_path = args.model.resolve()
    metadata_path = args.model_metadata.resolve()
    provenance = validate_model_configuration(model_path, metadata_path, args.target_mode)
    recorded_variant = str(provenance.get("controller_variant") or "")
    if recorded_variant and recorded_variant != args.controller_variant:
        ap.error(
            f"model controller_variant={recorded_variant!r} does not match "
            f"--controller-variant={args.controller_variant!r}"
        )
    if args.validate_model_only:
        print(json.dumps(provenance, sort_keys=True))
        return
    if not shadow and not args.aggregate_multipath:
        ap.error("active execution requires --aggregate-multipath")
    _assert_runtime_coeffs_path(args.coeffs_out.resolve())
    load_coeffs_doc(args.coeffs_out.resolve())

    print(
        f"[worker] scoring_mode={'aggregate_multipath_shadow' if shadow else 'aggregate_multipath_active'} "
        f"shadow={shadow} fixed_gamma={fixed_gamma} "
        f"polling {args.request.resolve()} every {args.poll_interval}s "
        f"target_mode={args.target_mode} {gate_desc}",
        file=sys.stderr,
        flush=True,
    )
    if shadow:
        print(
            "[worker] aggregate_scoring_experimental=true shadow_mode=true",
            file=sys.stderr,
            flush=True,
        )
    _write_ready_file(
        args.ready_file.resolve() if args.ready_file else None,
        {
            "status": "ready",
            "timestamp_ms": int(time.time() * 1000),
            "pid": os.getpid(),
            "request": str(args.request.resolve()),
            "runtime_samples": str(args.runtime_samples.resolve()),
            "model": str(model_path),
            "resolved_model_path": str(model_path),
            "model_target": provenance["model_target"],
            "verified_model_target": provenance["verified_model_target"],
            "requested_target_mode": args.target_mode,
            "controller_variant": args.controller_variant,
            "objective": _objective_name(args.target_mode),
            "optimization_direction": "minimize" if args.target_mode in MINIMIZE_TARGETS else "maximize",
            "score_unit": _score_unit(args.target_mode),
            "compatibility_status": "compatible",
            "model_target_compatible": provenance["model_target_compatible"],
            "model_training_rows": provenance["model_training_rows"],
            "model_features": provenance["model_features"],
            "model_metadata": provenance["model_metadata"],
            "coeffs_out": str(args.coeffs_out.resolve()),
            "response_out": str(args.response_out.resolve()),
            "state": str(args.state.resolve()),
            "archive_dir": str(args.archive_dir.resolve()),
            "target_mode": args.target_mode,
            "execution_mode": "shadow" if shadow else "active",
            "gate": gate_desc,
            "shadow": shadow,
            "shadow_mode": shadow,
            "scoring_mode": "aggregate_multipath_shadow" if shadow else "aggregate_multipath_active",
            "aggregate_shadow_only": shadow,
            "aggregate_scoring_experimental": True,
            "fixed_gamma": fixed_gamma,
            "min_sender_byte_delta": args.min_sender_byte_delta,
            "gate_mode": args.gate_mode,
            "gate_policy": args.gate_policy,
            "primary_objective": args.objective,
            "min_delta_gain_bps": args.min_delta_gain_bps,
            "min_relative_gain": args.min_relative_gain,
            "min_objective_relative_improvement": args.min_objective_relative_improvement,
            "changed_path_priority_shadow": bool(args.changed_path_priority_shadow),
            "changed_path_ids": list(args.changed_path_ids),
            "changed_path_gain_bps": args.changed_path_gain_bps,
            "min_aggregate_gain_bps": args.min_aggregate_gain_bps,
            "max_other_path_loss_ratio": args.max_other_path_loss_ratio,
            "max_other_path_loss_bps": args.max_other_path_loss_bps,
        },
    )

    while True:
        _process_request(
            args.request.resolve(),
            args.runtime_samples.resolve(),
            args.model.resolve(),
            args.coeffs_out.resolve(),
            args.response_out.resolve(),
            args.state.resolve(),
            args.archive_dir.resolve(),
            args.prev_coeffs_out.resolve(),
            args.audit_csv.resolve(),
            args.mode,
            args.target_mode,
            args.min_improvement_pct,
            args.min_objective_improvement if args.target_mode in MINIMIZE_TARGETS else args.min_delta_gain_bps,
            args.min_relative_delta_gain,
            shadow=shadow,
            aggregate_multipath=args.aggregate_multipath,
            gate_mode=args.gate_mode,
            gate_policy=args.gate_policy,
            objective=args.objective,
            objective_relative_threshold=args.min_objective_relative_improvement,
            min_relative_gain=args.min_relative_gain,
            fixed_gamma=fixed_gamma,
            log_file=log_file,
            min_sender_byte_delta=args.min_sender_byte_delta,
            changed_path_priority_shadow=args.changed_path_priority_shadow,
            changed_path_ids=set(args.changed_path_ids),
            changed_path_gain_bps=args.changed_path_gain_bps,
            min_aggregate_gain_bps=args.min_aggregate_gain_bps,
            max_other_path_loss_ratio=args.max_other_path_loss_ratio,
            max_other_path_loss_bps=args.max_other_path_loss_bps,
        )
        if args.once or args.dry_run:
            break
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()
