#!/usr/bin/env python3
"""
Q-ACCeSS-T Phase 2 update worker (buffer-full trigger).

Polls qaccess_update_request.json written by Go when the runtime MI buffer is full.
Each request_id is processed at most once; on success the request is archived and
runtime samples are rotated (archive + truncate) so the next cycle uses a clean buffer.

Writes only derived/qaccess_t_runtime_coefficients.json (never qaccess_t_initial_coefficients.json).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
import time
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "scripts" / "analyze") not in sys.path:
    sys.path.insert(0, str(_REPO / "scripts" / "analyze"))

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

DEFAULT_MIN_IMPROVEMENT_PCT = 3.0
MAX_COEFF_STEP = 0.1
MIN_BETA_GAMMA = 0.1
DEFAULT_COEFFS_PREV = _REPO / "derived" / "qaccess_t_runtime_coefficients_prev.json"


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


def _mean_predicted_bw(samples: pd.DataFrame, model, alpha: float, beta: float, gamma: float) -> float:
    X = _feature_matrix(samples, alpha, beta, gamma)
    if X.empty:
        return -1.0
    preds = np.asarray(model.predict(X), dtype=float)
    return float(np.mean(preds))


def _pick_best_candidate(
    samples: pd.DataFrame, model
) -> tuple[float, float, float, float]:
    best_alpha, best_beta, best_gamma = 0.70, MIN_BETA_GAMMA, MIN_BETA_GAMMA
    best_pred = -1.0
    for alpha, beta, gamma in phase2_candidate_triples():
        mean_pred = _mean_predicted_bw(samples, model, alpha, beta, gamma)
        if mean_pred > best_pred:
            best_pred = mean_pred
            best_alpha, best_beta, best_gamma = alpha, beta, gamma
    return best_alpha, best_beta, best_gamma, best_pred


def _apply_max_step(current: float, target: float, max_step: float = MAX_COEFF_STEP) -> float:
    delta = max(-max_step, min(max_step, target - current))
    return current + delta


def _ensure_path_writable(path: Path) -> None:
    """Allow worker to rotate files created by root during sudo Mininet runs."""
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


def _archive_and_truncate_buffer(
    samples_path: Path,
    archive_dir: Path,
    request_id: str,
    request_path: Path,
) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_archive_name(request_id)
    if samples_path.is_file() and samples_path.stat().st_size > 0:
        _ensure_path_writable(samples_path)
        dest = archive_dir / f"qaccess_runtime_samples_{safe}.csv"
        shutil.copy2(samples_path, dest)
        header = samples_path.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
        with samples_path.open("w", encoding="utf-8", newline="") as f:
            if header:
                f.write(header[0] + "\n")
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
    """Copy runtime coefficients before an accepted update (audit / rollback)."""
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


def _fmt_pct(v: float) -> str:
    if math.isinf(v):
        return "inf"
    if math.isnan(v):
        return "nan"
    return f"{v:.2f}"


def _worker_log_fields(
    *,
    request_id: str,
    min_improvement_pct: float,
    cur_alpha: float,
    cur_beta: float,
    cur_gamma: float,
    best_alpha: float,
    best_beta: float,
    best_gamma: float,
    pred_current: float,
    pred_best: float,
    improvement_pct: float,
    n_samples: int,
) -> str:
    return (
        f"request_id={request_id} "
        f"gate_pct={min_improvement_pct:.1f} required_pct={min_improvement_pct:.1f} "
        f"current_alpha={cur_alpha:.4f} current_beta={cur_beta:.4f} current_gamma={cur_gamma:.4f} "
        f"candidate_alpha={best_alpha:.4f} candidate_beta={best_beta:.4f} candidate_gamma={best_gamma:.4f} "
        f"pred_current={pred_current:.0f} pred_best={pred_best:.0f} "
        f"improvement_pct={_fmt_pct(improvement_pct)} n={n_samples}"
    )


def _process_request(
    request_path: Path,
    samples_path: Path,
    model_path: Path,
    coeffs_out: Path,
    response_out: Path,
    state_path: Path,
    archive_dir: Path,
    prev_coeffs_out: Path,
    mode: str,
    min_improvement_pct: float,
) -> bool:
    if not request_path.is_file():
        return False
    try:
        req = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[worker] skip: invalid request json: {exc}", file=sys.stderr)
        return False

    request_id = str(req.get("request_id") or "").strip()
    if not request_id:
        print("[worker] skip: request missing request_id", file=sys.stderr)
        return False

    state = _load_state(state_path)
    processed: list[str] = list(state.get("processed_request_ids") or [])
    if request_id in processed:
        if request_path.is_file():
            request_path.unlink()
        return False

    df = _load_runtime_samples(samples_path)
    samples = _clean_runtime_samples(df)
    if samples.empty:
        print("[worker] skip: no valid runtime samples", file=sys.stderr)
        return False

    if mode != "rf":
        print(f"[worker] unsupported mode {mode!r}", file=sys.stderr)
        return False
    if not model_path.is_file():
        print(f"[worker] missing model {model_path}", file=sys.stderr)
        return False

    model = joblib.load(model_path)
    cur_alpha = float(req.get("current_alpha", 0.6) or 0.6)
    cur_beta = float(req.get("current_beta", 0.1) or 0.1)
    cur_gamma = float(req.get("current_gamma", 0.1) or 0.1)

    pred_current = _mean_predicted_bw(samples, model, cur_alpha, cur_beta, cur_gamma)
    best_alpha, best_beta, best_gamma, pred_best = _pick_best_candidate(samples, model)

    improvement_pct = _improvement_pct(pred_current, pred_best)
    need_pred = pred_current * (1.0 + min_improvement_pct / 100.0) if pred_current > 0 else 0.0
    improvement_ok = pred_current <= 0 or pred_best >= need_pred

    applied_alpha = _apply_max_step(cur_alpha, best_alpha)
    applied_beta = max(MIN_BETA_GAMMA, _apply_max_step(cur_beta, best_beta))
    applied_gamma = max(MIN_BETA_GAMMA, _apply_max_step(cur_gamma, best_gamma))

    ts_ms = int(time.time() * 1000)
    response: dict = {
        "request_id": request_id,
        "timestamp_ms": ts_ms,
        "status": "ok" if improvement_ok else "skipped",
        "request_reason": req.get("reason"),
        "n_samples": int(len(samples)),
        "predicted_current_bw_bps": pred_current,
        "predicted_best_bw_bps": pred_best,
        "improvement_pct": improvement_pct,
        "improvement_min_pct": min_improvement_pct,
        "current_alpha": cur_alpha,
        "current_beta": cur_beta,
        "current_gamma": cur_gamma,
        "candidate_alpha": best_alpha,
        "candidate_beta": best_beta,
        "candidate_gamma": best_gamma,
    }

    _assert_runtime_coeffs_path(coeffs_out)

    log_fields = _worker_log_fields(
        request_id=request_id,
        min_improvement_pct=min_improvement_pct,
        cur_alpha=cur_alpha,
        cur_beta=cur_beta,
        cur_gamma=cur_gamma,
        best_alpha=best_alpha,
        best_beta=best_beta,
        best_gamma=best_gamma,
        pred_current=pred_current,
        pred_best=pred_best,
        improvement_pct=improvement_pct,
        n_samples=len(samples),
    )

    if improvement_ok:
        backup_path = _save_coeffs_backup(coeffs_out, archive_dir, request_id, prev_coeffs_out)
        coeffs_payload = {
            "alpha": applied_alpha,
            "beta": applied_beta,
            "gamma": applied_gamma,
            "source": "qaccess_t_update_worker.py",
            "metric": "predicted_next_bw_bps",
            "predicted_next_bw_bps": pred_best,
            "n_samples": int(len(samples)),
            "request_id": request_id,
            "request": req,
            "mode": mode,
            "improvement_min_pct": min_improvement_pct,
            "improvement_pct": improvement_pct,
            "previous_alpha": cur_alpha,
            "previous_beta": cur_beta,
            "previous_gamma": cur_gamma,
            "previous_coeffs_backup": str(backup_path) if backup_path else "",
        }
        atomic_write_json(coeffs_out, coeffs_payload)
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
            "skip_reason": "",
        })
        print(
            f"[worker] status=UPDATED {log_fields} "
            f"applied_alpha={applied_alpha:.4f} applied_beta={applied_beta:.4f} applied_gamma={applied_gamma:.4f} "
            f"skip_reason=accepted backup={backup_path or 'none'}"
        )
    else:
        response.update({
            "alpha": cur_alpha,
            "beta": cur_beta,
            "gamma": cur_gamma,
            "skip_reason": "improvement_gate",
        })
        print(
            f"[worker] status=SKIPPED {log_fields} "
            f"skip_reason=improvement_gate need_pred>={need_pred:.0f}"
        )

    atomic_write_json(response_out, response)

    processed.append(request_id)
    state["processed_request_ids"] = processed[-200:]
    state["last_processed_request_id"] = request_id
    _save_state(state_path, state)

    try:
        _archive_and_truncate_buffer(samples_path, archive_dir, request_id, request_path)
    except (OSError, PermissionError) as exc:
        print(
            f"[worker] warning: archive/truncate failed for request_id={request_id}: {exc}",
            file=sys.stderr,
        )
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Q-ACCeSS-T Phase 2 buffer-full update worker")
    ap.add_argument("--request", type=Path, default=DEFAULT_REQUEST)
    ap.add_argument("--runtime-samples", type=Path, default=DEFAULT_SAMPLES)
    ap.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    ap.add_argument("--coeffs-out", type=Path, default=DEFAULT_COEFFS)
    ap.add_argument("--response-out", type=Path, default=DEFAULT_RESPONSE)
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    ap.add_argument("--mode", choices=["rf"], default="rf")
    ap.add_argument("--poll-interval", type=float, default=5.0)
    ap.add_argument(
        "--min-improvement-pct",
        type=float,
        default=DEFAULT_MIN_IMPROVEMENT_PCT,
        help="Minimum predicted throughput improvement (%%) required to accept coefficient update (default: 3.0)",
    )
    ap.add_argument(
        "--prev-coeffs-out",
        type=Path,
        default=DEFAULT_COEFFS_PREV,
        help="Latest pre-update runtime coefficients copy for audit/rollback",
    )
    ap.add_argument("--once", action="store_true", help="Process one request if present, then exit")
    args = ap.parse_args()

    print(
        f"[worker] buffer-full mode; polling {args.request.resolve()} every {args.poll_interval}s "
        f"min_improvement_pct={args.min_improvement_pct}",
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
            args.mode,
            args.min_improvement_pct,
        )
        if args.once:
            break
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()
