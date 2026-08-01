#!/usr/bin/env python3
"""Post-process one experiment leg: diagnostics CSV, retention cleanup, size summary."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "scripts" / "analyze") not in sys.path:
    sys.path.insert(0, str(_REPO / "scripts" / "analyze"))

from qaccess_math import qaccess_gain_backoff  # noqa: E402

BW_REF_BPS = 30_000_000
DELAY_REF_MS = 100.0
DELAY_TREND_REF_MS = 50.0
LOSS_REF = 0.01
LEGACY_GAIN_MIN, LEGACY_GAIN_MAX = 0.80, 1.20
LEGACY_RET_MIN, LEGACY_RET_MAX = 0.90, 1.10

DIAG_COLUMNS = [
    "elapsed_s",
    "timestamp_ms",
    "run_id",
    "leg",
    "path_id",
    "alpha",
    "beta",
    "gamma",
    "throughput_reward_term_mean",
    "loss_penalty_term_mean",
    "delay_penalty_term_mean",
    "gain_raw_mean",
    "gain_applied_mean",
    "gain_min",
    "gain_max",
    "gain_hit_min_fraction",
    "gain_hit_max_fraction",
    "retention_raw_mean",
    "retention_applied_mean",
    "retention_min",
    "retention_max",
    "retention_hit_min_fraction",
    "retention_hit_max_fraction",
    "cwnd_bytes_mean",
    "inflight_bytes_mean",
    "cwnd_room_mean",
    "bw_bps_mean",
    "owd_ms_mean",
    "delay_gradient_ms_mean",
    "loss_rate_mean",
    "lost_bytes_delta_sum",
    "retrans_bytes_delta_sum",
    "n_samples",
]

THROUGHPUT_REQUIRED = (
    "throughput_all_down.csv",
    "throughput_pathA_down.csv",
    "throughput_pathB_down.csv",
)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _norm_g(bw: float) -> float:
    return _clamp(bw / BW_REF_BPS, 0, 1)


def _norm_l(loss: float) -> float:
    return _clamp(loss / LOSS_REF, 0, 1)


def _norm_d(owd: float, dgrad: float) -> float:
    delay_level = _clamp(owd / DELAY_REF_MS, 0, 1)
    trend = _clamp(dgrad / DELAY_TREND_REF_MS, 0, 1) if dgrad > 0 else 0.0
    return _clamp(0.7 * delay_level + 0.3 * trend, 0, 1)


def _g_pow(g_total: float, alpha: float) -> float:
    return (g_total**alpha) if g_total > 0 else 0.0


def _compute_gain_terms(g_total: float, norm_d: float, norm_l: float, alpha: float, beta: float, gamma: float):
    gp = _g_pow(g_total, alpha)
    tp = 0.20 * gp
    loss_pen = -0.10 * beta * 5.0 * norm_l
    delay_pen = -0.05 * gamma * 5.0 * norm_d
    gain_raw = 1.0 + tp + loss_pen + delay_pen
    gain_applied = _clamp(gain_raw, LEGACY_GAIN_MIN, LEGACY_GAIN_MAX)
    ret_raw = 1.0 - 0.08 * gp + 0.05 * beta * 5.0 * norm_l + 0.03 * gamma * 5.0 * norm_d
    retention = _clamp(ret_raw, LEGACY_RET_MIN, LEGACY_RET_MAX)
    return tp, loss_pen, delay_pen, gain_raw, gain_applied, ret_raw, retention


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _git_info(repo: Path) -> tuple[str, str]:
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True).strip()
        return commit, branch
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "", ""


def load_runtime_frames(leg_dir: Path, repo: Path) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for pattern in (
        leg_dir / "processed_buffers" / "qaccess_runtime_samples_*.csv",
        leg_dir / "derived_snapshots" / "qaccess_runtime_samples.csv",
        repo / "derived" / "qaccess_runtime_samples.csv",
    ):
        for p in sorted(pattern.parent.glob(pattern.name)):
            if p.is_file():
                frames.append(pd.read_csv(p))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(
        subset=["timestamp_ms", "path_id", "bw_bps", "owd_ms", "gain", "backoff"],
        keep="last",
    )
    return df.sort_values(["timestamp_ms", "path_id"]).reset_index(drop=True)


def compute_g_total_series(df: pd.DataFrame) -> pd.Series:
    active = (df["bw_bps"].fillna(0) > 0) | (df["owd_ms"].fillna(0) > 0) | (df["inflight_bytes"].fillna(0) > 1024)
    tmp = df[active].copy()
    tmp["ng"] = tmp["bw_bps"].fillna(0) / BW_REF_BPS
    by_ts = tmp.groupby("timestamp_ms")["ng"].sum().clip(0, 1)
    g = df["timestamp_ms"].map(by_ts).fillna(0)
    fallback = df["bw_bps"].fillna(0) / BW_REF_BPS
    return g.where(g > 0, fallback).clip(0, 1)


def enrich_runtime_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["g_total_used"] = compute_g_total_series(out)
    for col in [
        "throughput_reward_term",
        "loss_penalty_term",
        "delay_penalty_term",
        "gain_raw",
        "gain_clamped",
        "retention_raw",
        "retention_clamped",
    ]:
        if col not in out.columns:
            out[col] = float("nan")

    rows = []
    for _, r in out.iterrows():
        row = r.to_dict()
        alpha = float(r.get("alpha", 0.6) or 0.6)
        beta = float(r.get("beta", 0.3) or 0.3)
        gamma = float(r.get("gamma", 0.1) or 0.1)
        owd = float(r.get("owd_ms", 0) or 0)
        dgrad = float(r.get("delay_gradient_ms", 0) or 0)
        loss = float(r.get("loss_rate", 0) or 0)
        g_total = float(r.get("g_total_used", 0) or 0)
        nd = _norm_d(owd, dgrad)
        nl = _norm_l(loss)
        tp, lp, dp, gr, ga, rr, ra = _compute_gain_terms(g_total, nd, nl, alpha, beta, gamma)

        if pd.isna(row.get("throughput_reward_term")):
            row["throughput_reward_term"] = tp
        if pd.isna(row.get("loss_penalty_term")):
            row["loss_penalty_term"] = lp
        if pd.isna(row.get("delay_penalty_term")):
            row["delay_penalty_term"] = dp
        if pd.isna(row.get("gain_raw")):
            row["gain_raw"] = gr
        if pd.isna(row.get("gain_clamped")):
            row["gain_clamped"] = ga
        if pd.isna(row.get("retention_raw")):
            row["retention_raw"] = rr
        if pd.isna(row.get("retention_clamped")):
            row["retention_clamped"] = ra
        row["gain_applied"] = float(r.get("gain", ga) or ga)
        row["retention_applied"] = float(r.get("backoff", ra) or ra)
        row["gain_hit_min"] = abs(row["gain_applied"] - LEGACY_GAIN_MIN) < 1e-9
        row["gain_hit_max"] = abs(row["gain_applied"] - LEGACY_GAIN_MAX) < 1e-9
        row["retention_hit_min"] = abs(row["retention_applied"] - LEGACY_RET_MIN) < 1e-9
        row["retention_hit_max"] = abs(row["retention_applied"] - LEGACY_RET_MAX) < 1e-9
        rows.append(row)
    return pd.DataFrame(rows)


def build_control_law_diagnostics(df: pd.DataFrame, leg: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=DIAG_COLUMNS)
    work = enrich_runtime_rows(df)
    t0 = int(work["timestamp_ms"].min())
    work["elapsed_s"] = ((work["timestamp_ms"] - t0) // 1000).astype(int)

    agg_rows = []
    for (elapsed_s, path_id), grp in work.groupby(["elapsed_s", "path_id"]):
        agg_rows.append(
            {
                "elapsed_s": int(elapsed_s),
                "timestamp_ms": int(grp["timestamp_ms"].median()),
                "run_id": str(grp["run_id"].iloc[-1]) if "run_id" in grp.columns else "",
                "leg": leg,
                "path_id": int(path_id),
                "alpha": float(grp["alpha"].mean()),
                "beta": float(grp["beta"].mean()),
                "gamma": float(grp["gamma"].mean()),
                "throughput_reward_term_mean": float(grp["throughput_reward_term"].mean()),
                "loss_penalty_term_mean": float(grp["loss_penalty_term"].mean()),
                "delay_penalty_term_mean": float(grp["delay_penalty_term"].mean()),
                "gain_raw_mean": float(grp["gain_raw"].mean()),
                "gain_applied_mean": float(grp["gain_applied"].mean()),
                "gain_min": float(grp["gain_applied"].min()),
                "gain_max": float(grp["gain_applied"].max()),
                "gain_hit_min_fraction": float(grp["gain_hit_min"].mean()),
                "gain_hit_max_fraction": float(grp["gain_hit_max"].mean()),
                "retention_raw_mean": float(grp["retention_raw"].mean()),
                "retention_applied_mean": float(grp["retention_applied"].mean()),
                "retention_min": float(grp["retention_applied"].min()),
                "retention_max": float(grp["retention_applied"].max()),
                "retention_hit_min_fraction": float(grp["retention_hit_min"].mean()),
                "retention_hit_max_fraction": float(grp["retention_hit_max"].mean()),
                "cwnd_bytes_mean": float(grp["cwnd_bytes"].mean()) if "cwnd_bytes" in grp else 0.0,
                "inflight_bytes_mean": float(grp["inflight_bytes"].mean()) if "inflight_bytes" in grp else 0.0,
                "cwnd_room_mean": float(grp["cwnd_room"].mean()) if "cwnd_room" in grp else 0.0,
                "bw_bps_mean": float(grp["bw_bps"].mean()) if "bw_bps" in grp else 0.0,
                "owd_ms_mean": float(grp["owd_ms"].mean()) if "owd_ms" in grp else 0.0,
                "delay_gradient_ms_mean": float(grp["delay_gradient_ms"].mean())
                if "delay_gradient_ms" in grp
                else 0.0,
                "loss_rate_mean": float(grp["loss_rate"].mean()) if "loss_rate" in grp else 0.0,
                "lost_bytes_delta_sum": int(grp["lost_bytes_delta"].sum()) if "lost_bytes_delta" in grp else 0,
                "retrans_bytes_delta_sum": int(grp["retrans_bytes_delta"].sum())
                if "retrans_bytes_delta" in grp
                else 0,
                "n_samples": int(len(grp)),
            }
        )
    return pd.DataFrame(agg_rows, columns=DIAG_COLUMNS).sort_values(["elapsed_s", "path_id"])


def _generate_throughput_from_pcaps(leg_dir: Path, interval: float = 1.0) -> bool:
    pcap_dir = leg_dir / "pcaps"
    if not pcap_dir.is_dir():
        return False
    pcaps_a = sorted(pcap_dir.glob("pathA_h1_*.pcap"))
    pcaps_b = sorted(pcap_dir.glob("pathB_h1_*.pcap"))
    if not pcaps_a or not pcaps_b:
        return False
    analyzer = _REPO / "scripts" / "analyze" / "pcap_throughput.py"
    if not analyzer.is_file():
        return False
    import subprocess

    r = subprocess.run(
        [
            "python3",
            str(analyzer),
            "--pcap-a",
            str(pcaps_a[-1]),
            "--pcap-b",
            str(pcaps_b[-1]),
            "--per-path-dir",
            str(leg_dir),
            "--interval",
            str(interval),
            "--direction",
            "down",
        ],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


def _convert_legacy_throughput_csv(leg_dir: Path) -> bool:
    """Convert old csv/throughput_*_{run_id}.csv layout to required leg-root files."""
    csv_dir = leg_dir / "csv"
    if not csv_dir.is_dir():
        return False
    merged = sorted(csv_dir.glob("throughput_all_down_*.csv"))
    if not merged:
        return False
    src = merged[-1]
    rows = list(csv.reader(src.open(newline="", encoding="utf-8")))
    if len(rows) < 2:
        return False
    header = rows[0]
    idx_time = header.index("time_s") if "time_s" in header else 0
    idx_a = header.index("pathA_Mbps") if "pathA_Mbps" in header else -1
    idx_b = header.index("pathB_Mbps") if "pathB_Mbps" in header else -1
    idx_t = header.index("total_Mbps") if "total_Mbps" in header else -1
    if idx_a < 0 or idx_b < 0 or idx_t < 0:
        return False
    interval = 10.0
    if len(rows) >= 3:
        try:
            interval = float(rows[1][idx_time]) - float(rows[0][idx_time])
            if interval <= 0:
                interval = 10.0
        except (ValueError, IndexError):
            interval = 10.0

    def write_one(dest: Path, col: int):
        with dest.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["elapsed_s", "throughput_mbps", "bytes", "packets"])
            for row in rows[1:]:
                try:
                    elapsed = float(row[idx_time])
                    mbps = float(row[col])
                    nbytes = int(mbps * 1_000_000 * interval / 8)
                    w.writerow([f"{elapsed:.3f}", f"{mbps:.6f}", nbytes, ""])
                except (ValueError, IndexError):
                    continue

    write_one(leg_dir / "throughput_pathA_down.csv", idx_a)
    write_one(leg_dir / "throughput_pathB_down.csv", idx_b)
    write_one(leg_dir / "throughput_all_down.csv", idx_t)
    return True


def _find_throughput_sources(leg_dir: Path) -> dict[str, Path]:
    """Locate throughput CSVs (leg root or csv/ with run_id suffix)."""
    found: dict[str, Path] = {}
    for name in THROUGHPUT_REQUIRED:
        direct = leg_dir / name
        if direct.is_file() and direct.stat().st_size > 0:
            found[name] = direct
            continue
        matches = sorted(leg_dir.glob(f"csv/{name.replace('.csv', '_*.csv')}"))
        if not matches:
            alt = {
                "throughput_all_down.csv": "throughput_all_down_*.csv",
                "throughput_pathA_down.csv": "throughput_pathA_down_*.csv",
                "throughput_pathB_down.csv": "throughput_pathB_down_*.csv",
            }
            matches = sorted(leg_dir.glob(f"csv/{alt[name]}"))
        if matches and matches[-1].stat().st_size > 0:
            found[name] = matches[-1]
    return found


def _promote_throughput_csvs(leg_dir: Path) -> dict[str, Path]:
    sources = _find_throughput_sources(leg_dir)
    promoted: dict[str, Path] = {}
    for name, src in sources.items():
        dest = leg_dir / name
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        promoted[name] = dest
    return promoted


def _verify_throughput_csvs(paths: dict[str, Path]) -> bool:
    if set(paths.keys()) != set(THROUGHPUT_REQUIRED):
        return False
    for p in paths.values():
        if not p.is_file() or p.stat().st_size == 0:
            return False
        with p.open(newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        if len(rows) < 2:
            return False
    return True


def _copy_tc_log(leg_dir: Path) -> Path | None:
    logs = leg_dir / "logs"
    if not logs.is_dir():
        return None
    matches = sorted(logs.glob("tc_deterioration_*.log"))
    if not matches:
        matches = sorted(logs.glob("tc_*.log"))
    if not matches:
        return None
    dest = leg_dir / "tc_deterioration.log"
    shutil.copy2(matches[-1], dest)
    return dest


def _copy_response_json(leg_dir: Path, repo: Path) -> Path | None:
    for src in (
        leg_dir / "derived_snapshots" / "qaccess_update_response.json",
        repo / "derived" / "qaccess_update_response.json",
    ):
        if src.is_file():
            dest = leg_dir / "qaccess_update_response.json"
            shutil.copy2(src, dest)
            return dest
    return None


def _runtime_tail_gzip(df: pd.DataFrame, out_gz: Path, tail_seconds: int = 30) -> None:
    if df.empty:
        return
    t0 = int(df["timestamp_ms"].min())
    t_max = int(df["timestamp_ms"].max())
    cutoff = t_max - tail_seconds * 1000
    tail = df[df["timestamp_ms"] >= cutoff]
    tmp = out_gz.with_suffix("")
    tail.to_csv(tmp, index=False)
    with tmp.open("rb") as fin, gzip.open(out_gz, "wb") as fout:
        fout.writelines(fin)
    tmp.unlink(missing_ok=True)


def _prune_processed_buffers(leg_dir: Path, repo: Path, keep_all: bool) -> None:
    if keep_all:
        return
    for buf_dir in (leg_dir / "processed_buffers", repo / "derived" / "qaccess_processed_buffers"):
        if not buf_dir.is_dir():
            continue
        responses = sorted(buf_dir.glob("qaccess_update_response_*.json"), key=lambda p: p.stat().st_mtime)
        accepted = [p for p in responses if "UPDATED" in p.read_text(encoding="utf-8", errors="replace")]
        skipped = [p for p in responses if "SKIPPED" in p.read_text(encoding="utf-8", errors="replace")]
        keep: set[Path] = set()
        if accepted:
            keep.add(accepted[-1])
        if skipped:
            keep.add(skipped[-1])
        for p in buf_dir.iterdir():
            if p in keep:
                continue
            if p.is_file():
                p.unlink(missing_ok=True)


def _cleanup_leg_dir(
    leg_dir: Path,
    *,
    throughput_ok: bool,
    keep_pcap: bool,
    save_flv: bool,
    keep_logs: bool,
    keep_monitor_logs: bool = False,
) -> list[str]:
    if not throughput_ok:
        return sorted(str(p.relative_to(leg_dir)) for p in leg_dir.rglob("*") if p.is_file())

    # Remove bulky artifacts after successful throughput export.
    if not keep_pcap and (leg_dir / "pcaps").is_dir():
        shutil.rmtree(leg_dir / "pcaps", ignore_errors=True)
    if not save_flv:
        for flv in leg_dir.glob("output_*.flv"):
            flv.unlink(missing_ok=True)
    logs_dir = leg_dir / "logs"
    if not keep_logs and logs_dir.is_dir():
        if keep_monitor_logs:
            for path in logs_dir.iterdir():
                if path.is_file() and (
                    path.name.startswith("pull_") or path.name.startswith("tc_")
                ):
                    continue
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
        else:
            shutil.rmtree(logs_dir, ignore_errors=True)
    if (leg_dir / "csv").is_dir():
        shutil.rmtree(leg_dir / "csv", ignore_errors=True)
    if (leg_dir / "derived_snapshots").is_dir():
        shutil.rmtree(leg_dir / "derived_snapshots", ignore_errors=True)

    kept = sorted(str(p.relative_to(leg_dir)) for p in leg_dir.rglob("*") if p.is_file())
    return kept


def write_experiment_metadata(
    session_dir: Path,
    repo: Path,
    *,
    profile_path: str,
    timeout: int,
    control_law: str,
    model_path: str,
    target_mode: str,
    gate_threshold: str,
    initial_coeffs: Path | None,
    final_coeffs: Path | None,
    start_time: str,
    end_time: str,
    keep_pcap: int,
    keep_raw: int,
    save_flv: int,
) -> Path:
    commit, branch = _git_info(repo)
    meta = {
        "git_commit": commit,
        "branch": branch,
        "session_id": session_dir.name,
        "start_time": start_time,
        "end_time": end_time,
        "control_law": control_law,
        "model_path": model_path,
        "target_mode": target_mode,
        "gate_threshold": gate_threshold,
        "initial_coefficients": json.loads(initial_coeffs.read_text()) if initial_coeffs and initial_coeffs.is_file() else {},
        "final_coefficients": json.loads(final_coeffs.read_text()) if final_coeffs and final_coeffs.is_file() else {},
        "profile_path": profile_path,
        "timeout": timeout,
        "KEEP_PCAP": keep_pcap,
        "KEEP_RAW_RUNTIME": keep_raw,
        "SAVE_OUTPUT_FLV": save_flv,
    }
    dest = session_dir / "experiment_metadata.json"
    dest.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return dest


def finalize_leg(
    leg_dir: Path,
    repo: Path,
    *,
    leg_label: str,
    keep_pcap: bool,
    keep_raw_runtime: bool,
    save_flv: bool,
    keep_all_buffers: bool,
    keep_logs: bool,
    keep_monitor_logs: bool = False,
) -> dict:
    leg_dir = leg_dir.resolve()
    repo = repo.resolve()

    throughput_sources = _promote_throughput_csvs(leg_dir)
    throughput_ok = _verify_throughput_csvs(throughput_sources)
    if not throughput_ok:
        if _convert_legacy_throughput_csv(leg_dir):
            throughput_sources = _promote_throughput_csvs(leg_dir)
            throughput_ok = _verify_throughput_csvs(throughput_sources)
    if not throughput_ok:
        if _generate_throughput_from_pcaps(leg_dir, interval=float(os.environ.get("THROUGHPUT_INTERVAL", "1"))):
            throughput_sources = _promote_throughput_csvs(leg_dir)
            throughput_ok = _verify_throughput_csvs(throughput_sources)

    runtime_df = load_runtime_frames(leg_dir, repo)
    diag = build_control_law_diagnostics(runtime_df, leg_label)
    diag_path = leg_dir / "control_law_diagnostics.csv"
    diag.to_csv(diag_path, index=False)

    tail_gz = leg_dir / "qaccess_runtime_samples_tail.csv.gz"
    if not runtime_df.empty:
        _runtime_tail_gzip(runtime_df, tail_gz, tail_seconds=30)

    raw_paths = [
        repo / "derived" / "qaccess_runtime_samples.csv",
    ]
    if not keep_raw_runtime:
        for p in raw_paths:
            if p.is_file():
                p.unlink(missing_ok=True)
    elif raw_paths[0].is_file():
        gz_path = leg_dir / "qaccess_runtime_samples_full.csv.gz"
        with raw_paths[0].open("rb") as fin, gzip.open(gz_path, "wb") as fout:
            fout.writelines(fin)

    _copy_tc_log(leg_dir)
    _copy_response_json(leg_dir, repo)
    _prune_processed_buffers(leg_dir, repo, keep_all_buffers)

    # Drop stale request/response temps from repo derived (leg keeps snapshot).
    for name in ("qaccess_update_request.json",):
        p = repo / "derived" / name
        if p.is_file():
            p.unlink(missing_ok=True)

    kept = _cleanup_leg_dir(
        leg_dir,
        throughput_ok=throughput_ok,
        keep_pcap=keep_pcap,
        save_flv=save_flv,
        keep_logs=keep_logs,
        keep_monitor_logs=keep_monitor_logs,
    )

    pcap_retained = any(leg_dir.rglob("*.pcap"))
    flv_retained = any(leg_dir.glob("output_*.flv"))
    logs_retained = (leg_dir / "logs").is_dir() and any((leg_dir / "logs").iterdir())
    raw_retained = (repo / "derived" / "qaccess_runtime_samples.csv").is_file() or (
        leg_dir / "qaccess_runtime_samples_full.csv.gz"
    ).is_file()
    experiment_completed = throughput_ok
    postprocess_ok = throughput_ok

    summary = {
        "leg_dir": str(leg_dir),
        "leg_label": leg_label,
        "throughput_ok": throughput_ok,
        "postprocess_ok": postprocess_ok,
        "experiment_completed": experiment_completed,
        "session_bytes": _dir_size(leg_dir.parent),
        "leg_bytes": _dir_size(leg_dir),
        "diagnostics_bytes": diag_path.stat().st_size if diag_path.is_file() else 0,
        "pcap_retained": pcap_retained,
        "raw_runtime_retained": raw_retained,
        "output_flv_retained": flv_retained,
        "logs_retained": logs_retained,
        "kept_files": kept,
        "control_law_diagnostics_rows": int(len(diag)),
    }
    (leg_dir / "leg_status.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Finalize one control-law experiment leg")
    ap.add_argument("--leg-dir", type=Path, required=True)
    ap.add_argument("--repo", type=Path, default=_REPO)
    ap.add_argument("--leg-label", type=str, default="")
    ap.add_argument("--keep-pcap", type=int, default=int(os.environ.get("KEEP_PCAP", "0")))
    ap.add_argument("--keep-raw-runtime", type=int, default=int(os.environ.get("KEEP_RAW_RUNTIME", "0")))
    ap.add_argument("--save-output-flv", type=int, default=int(os.environ.get("SAVE_OUTPUT_FLV", "0")))
    ap.add_argument("--keep-logs", type=int, default=int(os.environ.get("SAVE_VERBOSE_LOGS", "0")))
    ap.add_argument(
        "--keep-monitor-logs",
        type=int,
        default=int(os.environ.get("QACCESS_RETAIN_MONITOR_LOG", "0")),
    )
    ap.add_argument(
        "--keep-all-processed-buffers",
        type=int,
        default=int(os.environ.get("KEEP_ALL_PROCESSED_BUFFERS", "0")),
    )
    args = ap.parse_args()

    leg_label = args.leg_label or args.leg_dir.name
    summary = finalize_leg(
        args.leg_dir,
        args.repo,
        leg_label=leg_label,
        keep_pcap=bool(args.keep_pcap),
        keep_raw_runtime=bool(args.keep_raw_runtime),
        save_flv=bool(args.save_output_flv),
        keep_all_buffers=bool(args.keep_all_processed_buffers),
        keep_logs=bool(args.keep_logs),
        keep_monitor_logs=bool(args.keep_monitor_logs),
    )

    print(f"[finalize] leg={summary['leg_dir']}")
    print(f"[finalize] experiment_completed={summary['experiment_completed']}")
    print(f"[finalize] postprocess_ok={summary['postprocess_ok']}")
    print(f"[finalize] throughput_ok={summary['throughput_ok']}")
    print(f"[finalize] session_size={summary['session_bytes'] / 1e6:.2f} MB")
    print(f"[finalize] leg_size={summary['leg_bytes'] / 1e6:.2f} MB")
    print(f"[finalize] diagnostics_size={summary['diagnostics_bytes'] / 1024:.1f} KB rows={summary['control_law_diagnostics_rows']}")
    print(f"[finalize] pcap_retained={summary['pcap_retained']}")
    print(f"[finalize] raw_runtime_retained={summary['raw_runtime_retained']}")
    print(f"[finalize] output_flv_retained={summary['output_flv_retained']}")
    print(f"[finalize] logs_retained={summary['logs_retained']}")
    print("[finalize] kept files:")
    for f in summary["kept_files"]:
        print(f"  {f}")
    if not summary["experiment_completed"]:
        return 1
    if not summary["postprocess_ok"]:
        print("[finalize] warn: post-processing incomplete; experiment artifacts retained", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
