#!/usr/bin/env python3
"""
Q-ACCeSS-T Phase 2 update worker (buffer-full trigger, per-subflow coefficients).

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
DEFAULT_INITIAL_COEFFS = _REPO / "derived" / "qaccess_t_initial_coefficients.json"
DEFAULT_COEFFS = _REPO / "derived" / "qaccess_t_runtime_coefficients.json"
DEFAULT_RESPONSE = _REPO / "derived" / "qaccess_update_response.json"
DEFAULT_STATE = _REPO / "derived" / "qaccess_worker_state.json"
DEFAULT_ARCHIVE_DIR = _REPO / "derived" / "qaccess_processed_buffers"
DEFAULT_AUDIT_CSV = _REPO / "derived" / "qaccess_per_path_update_audit.csv"

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

TARGET_MODES = ("next_bw_bps", "delta_bw_1s", "relative_delta_bw_1s")

DEFAULT_MIN_IMPROVEMENT_PCT = 3.0
DEFAULT_MIN_DELTA_GAIN_BPS = 500_000.0
DEFAULT_MIN_RELATIVE_DELTA_GAIN = 0.01
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
    for col in FEATURES + ["next_bw_bps", "path_id"]:
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
) -> tuple[list[dict[str, Any]], float, float, float, float, float]:
    pred_current = _mean_prediction(samples, model, cur_alpha, cur_beta, cur_gamma)
    candidates: list[dict[str, Any]] = []
    best_alpha, best_beta, best_gamma = cur_alpha, cur_beta, cur_gamma
    best_pred = pred_current

    for alpha, beta, gamma in _candidate_triples(fixed_gamma):
        mean_pred = _mean_prediction(samples, model, alpha, beta, gamma)
        candidates.append({
            "alpha": alpha,
            "beta": beta,
            "gamma": gamma,
            "mean_prediction": mean_pred,
            "is_current": (
                abs(alpha - cur_alpha) < 1e-9
                and abs(beta - cur_beta) < 1e-9
                and abs(gamma - cur_gamma) < 1e-9
            ),
        })
        if mean_pred > best_pred:
            best_pred = mean_pred
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
    if shadow:
        return
    archive_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_archive_name(request_id)
    if samples_path.is_file() and samples_path.stat().st_size > 0:
        _ensure_path_writable(samples_path)
        df = pd.read_csv(samples_path)
        path_df = _filter_samples_by_path(_clean_runtime_samples(df), path_id)
        if not path_df.empty:
            dest = archive_dir / f"qaccess_runtime_samples_{safe}_path{path_id}.csv"
            path_df.to_csv(dest, index=False)
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
        "delta_bw_1s": "predicted_delta_bw_1s",
        "relative_delta_bw_1s": "predicted_relative_delta_bw_1s",
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

    raise ValueError(f"unsupported target_mode: {target_mode}")


def _write_candidate_scores_csv(
    out_path: Path,
    *,
    candidates: list[dict[str, Any]],
    target_mode: str,
    path_id: int,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["path_id", "alpha", "beta", "gamma", "mean_prediction", "is_current", "is_best", "target_mode"]
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
    samples = _filter_samples_by_path(all_samples, path_id)
    if samples.empty:
        print(f"[worker] skip: no valid runtime samples for path_id={path_id}", file=sys.stderr)
        return None, pd.DataFrame(), request_id, path_id

    return req, samples, request_id, path_id


def _worker_log_line(log_file: Path | None, payload: dict[str, Any]) -> None:
    line = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    else:
        print(line)


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
    fixed_gamma: float | None,
    log_file: Path | None = None,
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
    )

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
    final_alpha = applied_alpha if improvement_ok else cur_alpha
    final_beta = applied_beta if improvement_ok else cur_beta
    final_gamma = applied_gamma if improvement_ok else cur_gamma
    mean_gain_after, mean_backoff_after = _mean_gain_backoff(samples, final_alpha, final_beta, final_gamma)

    ts_ms = int(time.time() * 1000)
    metric_field = _metric_field(target_mode)
    score_gain = pred_best - pred_current
    run_id = str(req.get("run_id") or "")
    reason = str(req.get("reason") or "")
    n_samples = int(len(samples))

    response: dict[str, Any] = {
        "request_id": request_id,
        "path_id": path_id,
        "timestamp_ms": ts_ms,
        "status": "ok" if improvement_ok else "skipped",
        "request_reason": reason,
        "n_samples": n_samples,
        "run_id": run_id,
        "target_mode": target_mode,
        "shadow": shadow,
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
        "mean_gain_before": mean_gain_before,
        "mean_gain_after": mean_gain_after,
        "mean_backoff_before": mean_backoff_before,
        "mean_backoff_after": mean_backoff_after,
        "skip_reason": "" if improvement_ok else skip_reason,
        "fixed_gamma": fixed_gamma,
    }

    if target_mode == "delta_bw_1s":
        response["pred_current_delta_bps"] = pred_current
        response["pred_best_delta_bps"] = pred_best

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

    active_update = improvement_ok and not shadow
    if active_update:
        backup_path = _save_coeffs_backup(coeffs_out, archive_dir, request_id, prev_coeffs_out)
        metadata = {
            "source": "qaccess_t_update_worker.py",
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
                "request_id": request_id,
                "target_mode": target_mode,
                "path_id": path_id,
                "current_coefficients": {"alpha": cur_alpha, "beta": cur_beta, "gamma": cur_gamma},
                "candidate_coefficients": {"alpha": best_alpha, "beta": best_beta, "gamma": best_gamma},
                "applied_coefficients": {"alpha": applied_alpha, "beta": applied_beta, "gamma": applied_gamma},
                "pred_current": pred_current,
                "pred_best": pred_best,
                "score_gain": score_gain_bps if score_gain_bps is not None else score_gain,
                "gate_threshold": gate_threshold,
                "status": "UPDATED",
                "skip_reason": "",
            },
        )
    else:
        response.update({
            "alpha": cur_alpha,
            "beta": cur_beta,
            "gamma": cur_gamma,
        })
        status = "SHADOW" if shadow else "SKIPPED"
        _worker_log_line(
            log_file,
            {
                "request_id": request_id,
                "target_mode": target_mode,
                "path_id": path_id,
                "current_coefficients": {"alpha": cur_alpha, "beta": cur_beta, "gamma": cur_gamma},
                "candidate_coefficients": {"alpha": best_alpha, "beta": best_beta, "gamma": best_gamma},
                "applied_coefficients": {"alpha": cur_alpha, "beta": cur_beta, "gamma": cur_gamma},
                "pred_current": pred_current,
                "pred_best": pred_best,
                "score_gain": score_gain_bps if score_gain_bps is not None else score_gain,
                "gate_threshold": gate_threshold,
                "status": status,
                "skip_reason": "" if improvement_ok else skip_reason,
            },
        )

    atomic_write_json(response_out, response)

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
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Q-ACCeSS-T Phase 2 buffer-full update worker (per-subflow)")
    ap.add_argument("--request", type=Path, default=DEFAULT_REQUEST)
    ap.add_argument("--runtime-samples", type=Path, default=DEFAULT_SAMPLES)
    ap.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    ap.add_argument("--coeffs-out", type=Path, default=DEFAULT_COEFFS)
    ap.add_argument("--response-out", type=Path, default=DEFAULT_RESPONSE)
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    ap.add_argument("--audit-csv", type=Path, default=DEFAULT_AUDIT_CSV)
    ap.add_argument("--mode", choices=["rf"], default="rf")
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
        "--min-relative-delta-gain",
        type=float,
        default=DEFAULT_MIN_RELATIVE_DELTA_GAIN,
        help="Minimum pred_best - pred_current for relative_delta_bw_1s gate (default: 0.01)",
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
        help="Evaluate per-path candidates and write audit artifacts only; no runtime coeff/buffer changes",
    )
    ap.add_argument(
        "--fixed-gamma",
        type=float,
        default=None,
        help="Filter candidate grid to this exact gamma before scoring (e.g. 0.1)",
    )
    ap.add_argument("--once", action="store_true", help="Process one request if present, then exit")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Alias for --shadow-per-subflow --once",
    )
    ap.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Append structured JSON update lines to this file (worker.log)",
    )
    args = ap.parse_args()

    shadow = bool(args.shadow_per_subflow or args.dry_run)
    fixed_gamma = args.fixed_gamma
    log_file = args.log_file.resolve() if args.log_file else None

    gate_desc = (
        f"min_improvement_pct={args.min_improvement_pct}"
        if args.target_mode == "next_bw_bps"
        else (
            f"min_delta_gain_bps={args.min_delta_gain_bps}"
            if args.target_mode == "delta_bw_1s"
            else f"min_relative_delta_gain={args.min_relative_delta_gain}"
        )
    )
    print(
        f"[worker] per-subflow mode shadow={shadow} fixed_gamma={fixed_gamma} "
        f"polling {args.request.resolve()} every {args.poll_interval}s "
        f"target_mode={args.target_mode} {gate_desc}",
        file=sys.stderr,
        flush=True,
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
            args.min_delta_gain_bps,
            args.min_relative_delta_gain,
            shadow=shadow,
            fixed_gamma=fixed_gamma,
            log_file=log_file,
        )
        if args.once or args.dry_run:
            break
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()
