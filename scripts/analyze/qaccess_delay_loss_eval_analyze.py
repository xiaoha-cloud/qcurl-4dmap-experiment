#!/usr/bin/env python3
"""
Analyze delay-only and loss-only Q-ACCeSS experiments.

Primary metrics differ from Fig.7 throughput eval:
  delay — OWD/RTT proxy, jitter, path-B usage shift, recovery time
  loss  — loss rate, retrans/lost-byte proxy, path-B usage shift, recovery time

Throughput (total / path A / path B) is computed from every captured frame.
The evaluator is read-only: worker execution mode is recorded by the experiment runner.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
import re
import subprocess
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from clean_evaluator_presets import (
    CLEAN_PRESETS,
    WindowSpec,
    clean_windows,
    clip_to_clean_run,
    preset_from_metadata,
)

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "scripts" / "analyze") not in sys.path:
    sys.path.insert(0, str(_REPO / "scripts" / "analyze"))

EVAL_WINDOWS = [
    ("0-50", 0.0, 50.0),
    ("50-90", 50.0, 90.0),
    ("90-100", 90.0, 100.0),
    ("100-150", 100.0, 150.0),
    ("150-200", 150.0, 200.0),
]

PRESETS: dict[str, dict[str, object]] = {
    "delay": {
        "objective_kind": "delay",
        "title": "Delay-only (primary: delay/RTT/recovery)",
        "baseline_dir": "delay_baseline",
        "dynamic_dirs": ("delay_qaccess_d_dynamic", "delay_qaccess_dynamic"),
        "out_subdir": "delay_only_compare",
        "file_prefix": "delay",
        "recovery_start_s": 150.0,
    },
    "loss": {
        "objective_kind": "loss",
        "title": "Loss-only (primary: loss/retrans/recovery)",
        "baseline_dir": "loss_baseline",
        "dynamic_dirs": ("loss_qaccess_l_dynamic", "loss_qaccess_dynamic"),
        "out_subdir": "loss_only_compare",
        "file_prefix": "loss",
        "recovery_start_s": 150.0,
    },
    "delay_clean": {
        "objective_kind": "delay",
        "title": "Clean delay (primary: SmoothedRTT()/2 delay proxy)",
        "baseline_dir": "clean_delay_baseline",
        "dynamic_dirs": ("clean_delay_qaccess_d",),
        "out_subdir": "delay_clean_compare",
        "file_prefix": "delay_clean",
        "recovery_start_s": 100.0,
        "reference_window": (0.0, 50.0),
        "clean_preset": "delay_clean",
    },
    "loss_clean": {
        "objective_kind": "loss",
        "title": "Clean loss (primary: runtime loss/loss-risk evidence)",
        "baseline_dir": "clean_loss_baseline",
        "dynamic_dirs": ("clean_loss_qaccess_l",),
        "out_subdir": "loss_clean_compare",
        "file_prefix": "loss_clean",
        "recovery_start_s": 100.0,
        "reference_window": (0.0, 50.0),
        "clean_preset": "loss_clean",
    },
}


def _load_metadata(session: Path) -> dict[str, object]:
    path = session / "experiment_metadata.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _historical_windows() -> tuple[WindowSpec, ...]:
    return tuple(WindowSpec(name, lo, hi, "historical", "historical window") for name, lo, hi in EVAL_WINDOWS) + (
        WindowSpec("0-200", 0.0, 200.0, "full_run", "full run"),
    )


def _p95(s: pd.Series) -> float:
    if s.empty or s.isna().all():
        return float("nan")
    return float(s.quantile(0.95))


def _pcap_bytes_by_second(pcap: Path, global_t0: float) -> dict[int, int]:
    cmd = [
        "tshark", "-n", "-r", "-",
        "-T", "fields", "-E", "separator=,",
        "-e", "frame.time_epoch", "-e", "frame.len",
    ]
    bins: dict[int, int] = {}
    with pcap.open("rb") as capture:
        proc = subprocess.Popen(
            cmd,
            stdin=capture,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            if not line.strip():
                continue
            parts = line.split(",", 1)
            if len(parts) < 2:
                continue
            try:
                t, n = float(parts[0]), int(float(parts[1]))
            except ValueError:
                continue
            si = int(math.floor(t - global_t0))
            if si >= 0:
                bins[si] = bins.get(si, 0) + n
        stderr = proc.stderr.read() if proc.stderr is not None else ""
        returncode = proc.wait()
    if returncode != 0:
        raise RuntimeError(
            f"tshark failed for {pcap} (exit={returncode}): {stderr.strip()[:1000]}"
        )
    return bins


def _first_pcap_epoch(pcap: Path) -> float | None:
    with pcap.open("rb") as capture:
        proc = subprocess.run(
            [
                "tshark", "-n", "-r", "-", "-c", "1",
                "-T", "fields", "-e", "frame.time_epoch",
            ],
            stdin=capture,
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode != 0:
        raise RuntimeError(
            f"tshark failed to read first packet from {pcap} "
            f"(exit={proc.returncode}): {proc.stderr.strip()[:1000]}"
        )
    for line in proc.stdout.splitlines():
        if line.strip():
            return float(line.strip())
    return None


_TC_TIMESTAMP_RE = re.compile(r"^\[(?P<timestamp>[^]]+)\] \[tc_(?:delay|loss|bw)\] step 1/")


def _tc_profile_start(run_dir: Path) -> datetime | None:
    """Return the timestamp of profile step 1, the experiment-time origin."""
    candidates = sorted((run_dir / "logs").glob("tc_*.log"))
    candidates.extend(sorted(run_dir.glob("tc_deterioration.log")))
    for path in candidates:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = _TC_TIMESTAMP_RE.match(line)
                if not match:
                    continue
                try:
                    return datetime.fromisoformat(match.group("timestamp"))
                except ValueError:
                    break
    return None


def _align_frames_to_tc(run_dir: Path, *frames: pd.DataFrame) -> tuple[pd.DataFrame, ...]:
    """Align log snapshots to tc profile time without filling missing seconds."""
    start = _tc_profile_start(run_dir)
    if start is None:
        return tuple(frames)
    start_second = start.hour * 3600 + start.minute * 60 + start.second
    aligned: list[pd.DataFrame] = []
    for frame in frames:
        if frame.empty or "wall_time_s" not in frame.columns:
            aligned.append(frame)
            continue
        work = frame.copy()
        work["t"] = (pd.to_numeric(work["wall_time_s"], errors="coerce") - start_second) % 86400
        aligned.append(work)
    return tuple(aligned)


def load_wire_timeseries(run_dir: Path) -> pd.DataFrame:
    pcap_dir = run_dir / "pcaps"
    pcaps = sorted(pcap_dir.glob("pathA_*.pcap")) + sorted(pcap_dir.glob("pathB_*.pcap"))
    if not pcaps:
        return _load_wire_timeseries_csv(run_dir)

    epochs = [epoch for p in pcaps if (epoch := _first_pcap_epoch(p)) is not None]
    if not epochs:
        return pd.DataFrame()

    profile_start = _tc_profile_start(run_dir)
    global_t0 = profile_start.timestamp() if profile_start is not None else min(epochs)
    path_bins: dict[str, dict[int, int]] = {}
    for p in pcaps:
        label = "path_a" if "pathA" in p.name else "path_b"
        path_bins[label] = _pcap_bytes_by_second(p, global_t0)

    last_s = max((max(b) for b in path_bins.values() if b), default=0)
    rows = []
    for s in range(last_s + 1):
        ba = path_bins.get("path_a", {}).get(s, 0)
        bb = path_bins.get("path_b", {}).get(s, 0)
        total = ba + bb
        rows.append({
            "time_s": float(s),
            "path_a_quic_wire_mbps": ba * 8 / 1e6,
            "path_b_quic_wire_mbps": bb * 8 / 1e6,
            "total_quic_wire_mbps": total * 8 / 1e6,
            "path_b_share_pct": (bb / total * 100.0) if total > 0 else float("nan"),
        })
    return pd.DataFrame(rows)


def _load_wire_timeseries_csv(run_dir: Path) -> pd.DataFrame:
    """Read the per-second CSVs generated before KEEP_PCAP=0 removes pcaps."""
    paths = {
        "total_quic_wire_mbps": run_dir / "throughput_all_down.csv",
        "path_a_quic_wire_mbps": run_dir / "throughput_pathA_down.csv",
        "path_b_quic_wire_mbps": run_dir / "throughput_pathB_down.csv",
    }
    if not all(path.is_file() and path.stat().st_size > 0 for path in paths.values()):
        return pd.DataFrame()

    series: list[pd.DataFrame] = []
    for output_column, path in paths.items():
        frame = pd.read_csv(path)
        time_column = "elapsed_s" if "elapsed_s" in frame.columns else "time_s"
        value_column = (
            "throughput_mbps" if "throughput_mbps" in frame.columns
            else output_column
        )
        if time_column not in frame.columns or value_column not in frame.columns:
            return pd.DataFrame()
        work = frame[[time_column, value_column]].copy()
        work.columns = ["time_s", output_column]
        work["time_s"] = pd.to_numeric(work["time_s"], errors="coerce")
        work[output_column] = pd.to_numeric(work[output_column], errors="coerce")
        series.append(work.dropna(subset=["time_s"]))

    result = series[0]
    for frame in series[1:]:
        result = result.merge(frame, on="time_s", how="outer")
    total = result["total_quic_wire_mbps"]
    result["path_b_share_pct"] = result["path_b_quic_wire_mbps"].div(total).mul(100.0)
    result.loc[total <= 0, "path_b_share_pct"] = float("nan")
    return result.sort_values("time_s").reset_index(drop=True)


def _metric_log_candidates(run_dir: Path) -> list[Path]:
    """Select one endpoint role consistently; never mix combined process logs."""
    logs = run_dir / "logs"
    for pattern in ("pull_*.log", "server_*.log"):
        hits = sorted(logs.glob(pattern)) if logs.is_dir() else []
        if not hits:
            hits = sorted(run_dir.glob(f"**/{pattern}"))
        if hits:
            return hits
    return []


def load_pull_frames(run_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    candidates = _metric_log_candidates(run_dir)
    if not candidates:
        return pd.DataFrame(), pd.DataFrame()
    try:
        from parse_logs import load_pull_log  # type: ignore
    except ImportError:
        return pd.DataFrame(), pd.DataFrame()
    candidate = candidates[0]
    df_util, df_mon = load_pull_log(candidate, label=run_dir.name)
    util = df_util if df_util is not None else pd.DataFrame()
    mon = df_mon if df_mon is not None else pd.DataFrame()
    return _align_frames_to_tc(run_dir, util, mon)


def load_runtime_samples(run_dir: Path) -> pd.DataFrame:
    for p in (
        run_dir / "qaccess_runtime_samples.csv",
        run_dir / "qaccess_runtime_samples_full.csv.gz",
        run_dir / "derived_snapshots" / "qaccess_runtime_samples.csv",
        _REPO / "derived" / "qaccess_runtime_samples.csv",
    ):
        if p.is_file() and p.stat().st_size > 0:
            df = pd.read_csv(p)
            if "timestamp_ms" in df.columns and not df.empty:
                t0 = float(df["timestamp_ms"].min())
                df = df.copy()
                df["time_s"] = (df["timestamp_ms"] - t0) / 1000.0
            return df
    return pd.DataFrame()


def _path_b_rows(df: pd.DataFrame, path_col: str = "path") -> pd.DataFrame:
    if df.empty or path_col not in df.columns:
        return df

    for label_col in ("physical_path_label", "physical_path"):
        if label_col in df.columns:
            labels = df[label_col].astype(str).str.strip().str.lower()
            pb = df[labels.isin(("path b", "path_b", "b"))]
            if not pb.empty:
                return pb

    for endpoint_col in ("remote_endpoint", "endpoint"):
        if endpoint_col in df.columns:
            endpoints = df[endpoint_col].fillna("").astype(str)
            pb = df[endpoints.str.contains(r"10\.0\.2\.", regex=True)]
            if not pb.empty:
                return pb

    numeric_ids = pd.to_numeric(df[path_col], errors="coerce")
    # Current clean runs use path 1/3 for A/B. Path 2 remains a legacy fallback.
    preferred_ids = (3, 2)
    for path_id in preferred_ids:
        pb = df[numeric_ids == path_id]
        if not pb.empty:
            return pb

    unique_ids = sorted(numeric_ids.dropna().unique())
    if len(unique_ids) >= 2:
        return df[numeric_ids == unique_ids[-1]]
    return df.iloc[0:0]


def _window_mask(df: pd.DataFrame, lo: float, hi: float) -> pd.DataFrame:
    if df.empty or "time_s" not in df.columns and "t" not in df.columns:
        return df.iloc[0:0]
    tcol = "time_s" if "time_s" in df.columns else "t"
    return df[(df[tcol] >= lo) & (df[tcol] < hi)].copy()


def _recovery_time_s(
    series: pd.DataFrame,
    *,
    tcol: str,
    vcol: str,
    ref_lo: float,
    ref_hi: float,
    recover_after: float = 100.0,
    tol_frac: float = 0.15,
    absolute_tol: float = 0.0,
    stable_sec: int = 10,
) -> float:
    if series.empty or vcol not in series.columns:
        return float("nan")
    ref = _window_mask(series.rename(columns={tcol: "time_s"}), ref_lo, ref_hi)
    if ref.empty:
        return float("nan")
    target = float(ref[vcol].mean())
    if not math.isfinite(target) or target < 0:
        return float("nan")
    post = series[series[tcol] >= recover_after].sort_values(tcol)
    if post.empty:
        return float("nan")
    thresh = max(target * (1.0 + tol_frac), absolute_tol)
    streak = 0
    for _, row in post.iterrows():
        val = float(row[vcol])
        if math.isfinite(val) and val <= thresh:
            streak += 1
            if streak >= stable_sec:
                return float(row[tcol]) - recover_after
        else:
            streak = 0
    return float("nan")


def _per_second_delay(util: pd.DataFrame, mon: pd.DataFrame, method: str) -> pd.DataFrame:
    columns = [
        "method", "time_s", "owd_ms_mean", "owd_ms_p95",
        "rtt_latest_ms_mean", "rtt_latest_ms_median", "rtt_latest_ms_p10",
        "rtt_latest_ms_p95",
        "rtt_ms_mean", "rtt_ms_p95", "jitter_ms_mean",
    ]
    pieces: list[pd.DataFrame] = []
    has_utility_owd = False
    if not util.empty and "owd_ms" in util.columns:
        u = util.rename(columns={"t": "time_s"}) if "t" in util.columns else util.copy()
        u = _path_b_rows(u)
        if not u.empty:
            has_utility_owd = u["owd_ms"].notna().any()
            u = u.copy()
            u["time_s"] = pd.to_numeric(u["time_s"], errors="coerce").floordiv(1)
            pieces.append(
                u.groupby("time_s", as_index=False).agg(
                    owd_ms_mean=("owd_ms", "mean"),
                    owd_ms_p95=("owd_ms", lambda s: s.quantile(0.95)),
                )
            )
    if not mon.empty and "rtt_smoothed_ms" in mon.columns:
        m = mon.rename(columns={"t": "time_s"}) if "t" in mon.columns else mon.copy()
        m = _path_b_rows(m)
        if not m.empty:
            m = m.copy()
            m["time_s"] = pd.to_numeric(m["time_s"], errors="coerce").floordiv(1)
            agg: dict[str, object] = {
                "rtt_ms_mean": ("rtt_smoothed_ms", "mean"),
                "rtt_ms_p95": ("rtt_smoothed_ms", lambda s: s.quantile(0.95)),
            }
            if "rtt_latest_ms" in m.columns:
                agg["rtt_latest_ms_mean"] = ("rtt_latest_ms", "mean")
                agg["rtt_latest_ms_median"] = ("rtt_latest_ms", "median")
                agg["rtt_latest_ms_p10"] = ("rtt_latest_ms", lambda s: s.quantile(0.10))
                agg["rtt_latest_ms_p95"] = ("rtt_latest_ms", lambda s: s.quantile(0.95))
            if "rtt_mean_dev_ms" in m.columns:
                agg["jitter_ms_mean"] = ("rtt_mean_dev_ms", "mean")
            if not has_utility_owd and "owd_ms" in m.columns:
                agg["owd_ms_mean"] = ("owd_ms", "mean")
                agg["owd_ms_p95"] = ("owd_ms", lambda s: s.quantile(0.95))
            pieces.append(m.groupby("time_s", as_index=False).agg(**agg))

    if not pieces:
        return pd.DataFrame(columns=columns)
    result = pieces[0]
    for piece in pieces[1:]:
        result = result.merge(piece, on="time_s", how="outer")
    result.insert(0, "method", method)
    for column in columns:
        if column not in result.columns:
            result[column] = float("nan")
    return result[columns].sort_values("time_s").reset_index(drop=True)


def _per_second_loss(
    util: pd.DataFrame,
    mon: pd.DataFrame,
    samples: pd.DataFrame,
    method: str,
) -> pd.DataFrame:
    columns = [
        "method", "time_s", "utility_loss_mean", "monitor_loss_mean",
        "sample_loss_rate_mean", "lost_bytes_delta_sum", "retrans_bytes_delta_sum",
    ]
    pieces: list[pd.DataFrame] = []
    for frame, value_col, output_col, path_col in (
        (util, "loss", "utility_loss_mean", "path"),
        (mon, "loss", "monitor_loss_mean", "path"),
        (samples, "loss_rate", "sample_loss_rate_mean", "path_id"),
    ):
        if frame.empty or value_col not in frame.columns:
            continue
        work = frame.rename(columns={"t": "time_s"}) if "t" in frame.columns else frame.copy()
        work = _path_b_rows(work, path_col=path_col)
        if work.empty or "time_s" not in work.columns:
            continue
        work = work.copy()
        work["time_s"] = pd.to_numeric(work["time_s"], errors="coerce").floordiv(1)
        pieces.append(
            work.groupby("time_s", as_index=False)[value_col]
            .mean()
            .rename(columns={value_col: output_col})
        )
    if not samples.empty and "time_s" in samples.columns:
        work = _path_b_rows(samples, path_col="path_id").copy()
        if not work.empty:
            work["time_s"] = pd.to_numeric(work["time_s"], errors="coerce").floordiv(1)
            sum_cols = [
                column for column in ("lost_bytes_delta", "retrans_bytes_delta")
                if column in work.columns
            ]
            if sum_cols:
                summed = work.groupby("time_s", as_index=False)[sum_cols].sum()
                summed = summed.rename(columns={
                    "lost_bytes_delta": "lost_bytes_delta_sum",
                    "retrans_bytes_delta": "retrans_bytes_delta_sum",
                })
                pieces.append(summed)

    if not pieces:
        return pd.DataFrame(columns=columns)
    result = pieces[0]
    for piece in pieces[1:]:
        result = result.merge(piece, on="time_s", how="outer")
    result.insert(0, "method", method)
    for column in columns:
        if column not in result.columns:
            result[column] = float("nan")
    return result[columns].sort_values("time_s").reset_index(drop=True)


def _per_second_bandwidth(samples: pd.DataFrame, method: str) -> pd.DataFrame:
    """Build the Path B bandwidth-estimate series used by the shared T/D/L plot."""
    columns = ["method", "time_s", "bw_mbps_mean"]
    if samples.empty or "time_s" not in samples.columns or "bw_bps" not in samples.columns:
        return pd.DataFrame(columns=columns)
    work = _path_b_rows(samples, path_col="path_id").copy()
    if work.empty:
        return pd.DataFrame(columns=columns)
    work["time_s"] = pd.to_numeric(work["time_s"], errors="coerce").floordiv(1)
    work["bw_mbps"] = pd.to_numeric(work["bw_bps"], errors="coerce") / 1_000_000.0
    result = work.groupby("time_s", as_index=False).agg(bw_mbps_mean=("bw_mbps", "mean"))
    result.insert(0, "method", method)
    return result[columns].sort_values("time_s").reset_index(drop=True)


def _pct_change(baseline: float, dynamic: float, higher_is_better: bool) -> float:
    if not math.isfinite(baseline) or not math.isfinite(dynamic) or baseline == 0:
        return float("nan")
    delta = dynamic - baseline
    return float((delta if higher_is_better else -delta) / abs(baseline) * 100.0)


def build_improvement_table(df_win: pd.DataFrame, dynamic_method: str) -> pd.DataFrame:
    metric_directions = {
        "owd_ms_mean": False,
        "owd_ms_p95": False,
        "rtt_ms_mean": False,
        "rtt_ms_p95": False,
        "rtt_latest_ms_mean": False,
        "rtt_latest_ms_p10": False,
        "rtt_latest_ms_median": False,
        "rtt_latest_ms_p90": False,
        "rtt_latest_ms_p95": False,
        "jitter_ms_mean": False,
        "utility_loss_mean": False,
        "monitor_loss_mean": False,
        "sample_loss_rate_mean": False,
        "retrans_bytes_delta_sum": False,
        "lost_bytes_delta_sum": False,
        "loss_event_fraction": False,
        "secondary_total_quic_wire_mbps_mean": True,
        "secondary_path_a_quic_wire_mbps_mean": True,
        "secondary_path_b_quic_wire_mbps_mean": True,
    }
    rows: list[dict[str, object]] = []
    for window in df_win["window"].drop_duplicates():
        baseline_rows = df_win[(df_win["method"] == "baseline") & (df_win["window"] == window)]
        dynamic_rows = df_win[(df_win["method"] == dynamic_method) & (df_win["window"] == window)]
        if baseline_rows.empty or dynamic_rows.empty:
            continue
        baseline_row, dynamic_row = baseline_rows.iloc[0], dynamic_rows.iloc[0]
        for metric, higher_is_better in metric_directions.items():
            if metric not in df_win.columns:
                continue
            baseline = float(baseline_row[metric])
            dynamic = float(dynamic_row[metric])
            change = dynamic - baseline
            rows.append({
                "window": window,
                **({
                    "window_role": baseline_row.get("window_role", ""),
                    "condition": baseline_row.get("condition", ""),
                    "result_type": baseline_row.get("window_role", ""),
                } if "window_role" in df_win.columns else {}),
                "metric": metric,
                "baseline": baseline,
                "qaccess": dynamic,
                "improvement_pct": _pct_change(baseline, dynamic, higher_is_better),
                "absolute_change": change,
                "improvement_absolute": change if higher_is_better else -change,
                "better_when": "higher" if higher_is_better else "lower",
            })
        if "path_b_share_pct_mean" in df_win.columns:
            baseline = float(baseline_row["path_b_share_pct_mean"])
            dynamic = float(dynamic_row["path_b_share_pct_mean"])
            rows.append({
                "window": window,
                "metric": "path_b_share_pct_mean",
                "baseline": baseline,
                "qaccess": dynamic,
                "improvement_pct": float("nan"),
                "change_percentage_points": dynamic - baseline,
                "better_when": "context-dependent",
            })
    return pd.DataFrame(rows)


_TOTAL_METHOD_COLORS = ("#0072B2", "#D55E00")
_PER_PATH_COLORS = ("#009E73", "#CC79A7", "#E69F00", "#56B4E9")


def _plot_method_label(method: str, objective_kind: str | None) -> str:
    """Use publication labels without changing method identifiers in CSV output."""
    if method.strip().lower() == "baseline":
        return "Baseline"
    return {
        "throughput": "Q-Access-T",
        "delay": "Q-Access-D",
        "loss": "Q-Access-L",
    }.get(str(objective_kind), method)


def _timeseries_color_maps(methods) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    """Return the fixed six-color mapping for a two-method evaluation plot."""
    names = list(dict.fromkeys(str(method) for method in methods))
    names.sort(key=lambda name: (name.strip().lower() != "baseline", name))
    method_colors = {
        method: color for method, color in zip(names, _TOTAL_METHOD_COLORS)
    }
    path_colors: dict[tuple[str, str], str] = {}
    for index, method in enumerate(names[:2]):
        path_colors[(method, "A")] = _PER_PATH_COLORS[index * 2]
        path_colors[(method, "B")] = _PER_PATH_COLORS[index * 2 + 1]
    return method_colors, path_colors


def _plot_timeseries(
    throughput: pd.DataFrame,
    quality: pd.DataFrame,
    out: Path,
    prefix: str,
    objective_kind: str | None = None,
    windows: tuple[WindowSpec, ...] | None = None,
    quality_metric: str | None = None,
    output_name: str | None = None,
    steady_rtt_medians: dict[str, list[tuple[float, float, float]]] | None = None,
) -> None:
    if throughput.empty and quality.empty:
        return

    method_colors, path_colors = _timeseries_color_maps(
        list(throughput.get("method", pd.Series(dtype=str)))
        + list(quality.get("method", pd.Series(dtype=str)))
    )
    fig, axes = plt.subplots(3, 1, figsize=(11, 9), sharex=True)
    throughput_available = (
        not throughput.empty
        and "method" in throughput.columns
        and "time_s" in throughput.columns
    )
    if throughput_available:
        throughput_groups = list(throughput.groupby("method"))
    else:
        throughput_groups = []

    for method, group in throughput_groups:
        axes[0].plot(
            group["time_s"], group["total_quic_wire_mbps"],
            label=_plot_method_label(str(method), objective_kind),
            linewidth=1.5, color=method_colors.get(str(method)),
        )
    axes[0].set_ylabel("Throughput (Mbps)")
    axes[0].set_title("Total throughput from all captured frames")
    axes[0].grid(alpha=0.25)
    if throughput_groups:
        axes[0].legend()
    else:
        axes[0].text(
            0.5, 0.5, "Throughput data unavailable",
            ha="center", va="center", transform=axes[0].transAxes,
        )

    for method, group in throughput_groups:
        axes[1].plot(
            group["time_s"], group["path_a_quic_wire_mbps"],
            label=f"{_plot_method_label(str(method), objective_kind)} Path A", linewidth=1.1,
            color=path_colors.get((str(method), "A")),
        )
        axes[1].plot(
            group["time_s"], group["path_b_quic_wire_mbps"],
            label=f"{_plot_method_label(str(method), objective_kind)} Path B",
            linewidth=1.1, linestyle="--",
            color=path_colors.get((str(method), "B")),
        )
    axes[1].set_ylabel("Per-path (Mbps)")
    axes[1].grid(alpha=0.25)
    if throughput_groups:
        axes[1].legend(ncol=2, fontsize=8)
    else:
        axes[1].text(
            0.5, 0.5, "Per-path throughput data unavailable",
            ha="center", va="center", transform=axes[1].transAxes,
        )

    quality_column = ""
    quality_label = ""
    if quality_metric:
        explicit_quality_labels = {
            "owd_ms_mean": "Path B OWD proxy (SmoothedRTT()/2, ms)",
            "rtt_latest_ms_mean": "Path B sampled LatestRTT, 1 s mean (ms)",
            "rtt_latest_ms_median": "Path B QUIC per-path RTT, 1 s median (ms)",
            "rtt_ms_mean": "Path B smoothed RTT (ms)",
        }
        if quality_metric in quality.columns and quality[quality_metric].notna().any():
            quality_column = quality_metric
            quality_label = explicit_quality_labels.get(quality_metric, quality_metric)
    elif (objective_kind or prefix) == "throughput":
        for candidate, label in (
            ("path_b_throughput_mbps", "Path B throughput (Mbps)"),
            ("bw_mbps_mean", "Path B bandwidth estimate (Mbps)"),
        ):
            if candidate in quality.columns and quality[candidate].notna().any():
                quality_column, quality_label = candidate, label
                break
    elif (objective_kind or prefix) == "delay":
        for candidate, label in (
            ("rtt_latest_ms_median", "Path B QUIC per-path RTT, 1 s median (ms)"),
            ("rtt_latest_ms_mean", "Path B sampled LatestRTT, 1 s mean (ms)"),
            ("owd_ms_mean", "Path B OWD (ms)"),
            ("rtt_ms_mean", "Path B RTT (ms)"),
        ):
            if candidate in quality.columns and quality[candidate].notna().any():
                quality_column, quality_label = candidate, label
                break
    else:
        for candidate, label in (
            ("utility_loss_mean", "Path B utility loss"),
            ("monitor_loss_mean", "Path B monitor loss"),
            ("sample_loss_rate_mean", "Path B runtime loss rate"),
        ):
            if candidate in quality.columns and quality[candidate].notna().any():
                quality_column, quality_label = candidate, label
                break

    if quality_column and quality[quality_column].notna().any():
        for method, group in quality.groupby("method"):
            axes[2].plot(
                group["time_s"], group[quality_column],
                label=_plot_method_label(str(method), objective_kind),
                linewidth=1.5, color=method_colors.get(str(method)),
            )
        if (objective_kind or prefix) == "delay" and quality_column == "rtt_latest_ms_median":
            axes[2].set_title("Path B QUIC RTT")
            axes[2].set_ylabel("Path B QUIC per-path RTT (ms)")
            for method, group in quality.groupby("method"):
                color = method_colors.get(str(method))
                segments = (steady_rtt_medians or {}).get(str(method), [])
                for lo, hi, median in segments:
                    midpoint = (lo + hi) / 2.0
                    axes[2].scatter(
                        [midpoint], [median], color=color, s=24, marker="o",
                        edgecolors="white", linewidths=0.6, zorder=4,
                    )
                    axes[2].annotate(
                        f"{median:.2f} ms", xy=(midpoint, median),
                        xytext=(0, 5), textcoords="offset points", ha="center",
                        fontsize=7, color=color,
                    )
        else:
            axes[2].set_ylabel(quality_label)
        if quality_column == "path_b_throughput_mbps":
            axes[2].set_title("Path B throughput (zoomed)")
        axes[2].legend()
    else:
        axes[2].text(0.5, 0.5, "No quality samples found", ha="center", va="center",
                     transform=axes[2].transAxes)
        axes[2].set_ylabel("Path B quality metric")
    axes[2].set_xlabel("Time (s)")
    axes[2].grid(alpha=0.25)
    for axis in axes:
        if windows is None:
            axis.axvspan(90, 150, color="tab:red", alpha=0.08)
        else:
            for window in windows:
                if window.role == "response":
                    axis.axvspan(window.start_s, window.end_s, color="tab:orange", alpha=0.10)
    fig.tight_layout()
    fig.savefig(out / (output_name or f"{prefix}_throughput_quality_over_time.png"), dpi=180)
    plt.close(fig)


RTT_AUDIT_WINDOWS = (
    ("0-50", 0.0, 50.0),
    ("30-50", 30.0, 50.0),
    ("50-60", 50.0, 60.0),
    ("60-100", 60.0, 100.0),
    ("80-100", 80.0, 100.0),
    ("100-110", 100.0, 110.0),
    ("110-200", 110.0, 200.0),
    ("130-160", 130.0, 160.0),
)


def _rtt_snapshot_stats(
    frame: pd.DataFrame, expected_seconds: float = 200.0,
) -> dict[str, float | int]:
    if frame.empty:
        return {
            "sample_count": 0,
            "first_valid_s": float("nan"), "last_valid_s": float("nan"),
            "minimum_ms": float("nan"), "p05_ms": float("nan"),
            "p10_ms": float("nan"), "median_ms": float("nan"),
            "mean_ms": float("nan"), "p90_ms": float("nan"),
            "maximum_ms": float("nan"), "repeated_consecutive_count": 0,
            "repeated_consecutive_pct": float("nan"),
            "longest_repeated_value_duration_s": float("nan"),
            "missing_second_pct": 100.0, "samples_per_second": 0.0,
        }
    work = frame.sort_index()
    values = work["rtt_latest_ms"]
    repeated = values.eq(values.shift())
    runs = work.assign(_run=(~repeated).cumsum()).groupby("_run").agg(
        first=("t", "min"), last=("t", "max"),
    )
    observed_seconds = work["t"].floordiv(1).astype(int).nunique()
    quantiles = values.quantile([0.05, 0.10, 0.50, 0.90])
    return {
        "sample_count": int(len(work)),
        "first_valid_s": float(work["t"].min()),
        "last_valid_s": float(work["t"].max()),
        "minimum_ms": float(values.min()),
        "p05_ms": float(quantiles.loc[0.05]),
        "p10_ms": float(quantiles.loc[0.10]),
        "median_ms": float(quantiles.loc[0.50]),
        "mean_ms": float(values.mean()),
        "p90_ms": float(quantiles.loc[0.90]),
        "maximum_ms": float(values.max()),
        "repeated_consecutive_count": int(repeated.sum()),
        "repeated_consecutive_pct": float(repeated.mean() * 100.0),
        "longest_repeated_value_duration_s": float((runs["last"] - runs["first"]).max()),
        "missing_second_pct": float((1.0 - observed_seconds / expected_seconds) * 100.0),
        "samples_per_second": float(len(work) / expected_seconds),
    }


def _rtt_per_second(frame: pd.DataFrame) -> pd.DataFrame:
    """Build fixed-time RTT statistics without forward-filling missing seconds."""
    columns = [
        "time_s", "sample_count", "rtt_1s_mean_ms", "rtt_1s_median_ms",
        "rtt_1s_p10_ms", "rtt_3s_trailing_median_ms",
        "rtt_5s_trailing_median_ms", "rtt_excess_over_run_p05_ms",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    work = frame.copy()
    work["time_s"] = pd.to_numeric(work["t"], errors="coerce").floordiv(1)
    grouped = work.groupby("time_s")["rtt_latest_ms"]
    observed = grouped.agg(
        sample_count="size",
        rtt_1s_mean_ms="mean",
        rtt_1s_median_ms="median",
    )
    observed["rtt_1s_p10_ms"] = grouped.quantile(0.10)
    full = observed.reindex(pd.Index(range(200), name="time_s"))
    present = full["sample_count"].notna()
    full["rtt_3s_trailing_median_ms"] = (
        full["rtt_1s_median_ms"].rolling(window=3, min_periods=1).median().where(present)
    )
    full["rtt_5s_trailing_median_ms"] = (
        full["rtt_1s_median_ms"].rolling(window=5, min_periods=1).median().where(present)
    )
    run_p05 = float(work["rtt_latest_ms"].quantile(0.05))
    full["rtt_excess_over_run_p05_ms"] = (
        full["rtt_1s_median_ms"].sub(run_p05).clip(lower=0).where(present)
    )
    return full.reset_index()[columns]


def _generate_rtt_audit(runs: dict[str, Path], out: Path) -> dict[str, Path]:
    """Write endpoint-consistent LatestRTT diagnostics for clean delay sessions."""
    out.mkdir(parents=True, exist_ok=True)
    overview_rows: list[dict[str, object]] = []
    window_rows: list[dict[str, object]] = []
    transition_rows: list[pd.DataFrame] = []
    series_rows: list[pd.DataFrame] = []
    raw_by_method_path: dict[tuple[str, str], pd.DataFrame] = {}

    for method, run_dir in runs.items():
        sources = _metric_log_candidates(run_dir)
        _, monitor = load_pull_frames(run_dir)
        source = str(sources[0]) if sources else ""
        for path_id, path_label in ((1, "Path A"), (3, "Path B")):
            raw = monitor[
                (pd.to_numeric(monitor.get("path"), errors="coerce") == path_id)
                & (pd.to_numeric(monitor.get("rtt_latest_ms"), errors="coerce") > 0)
                & (pd.to_numeric(monitor.get("t"), errors="coerce") >= 0)
                & (pd.to_numeric(monitor.get("t"), errors="coerce") < 200)
            ].copy()
            raw_by_method_path[(method, path_label)] = raw
            overview_rows.append({
                "method": method, "endpoint_role": "client_pull_receiver",
                "source_log": source, "path_id": path_id, "path": path_label,
                **_rtt_snapshot_stats(raw),
            })
            for name, lo, hi in RTT_AUDIT_WINDOWS:
                window = raw[(raw["t"] >= lo) & (raw["t"] < hi)]
                window_rows.append({
                    "method": method, "path_id": path_id, "path": path_label,
                    "window": name, "t_lo": lo, "t_hi": hi,
                    **_rtt_snapshot_stats(window, hi - lo),
                })
            transition = raw[
                ((raw["t"] >= 45) & (raw["t"] < 65))
                | ((raw["t"] >= 95) & (raw["t"] < 120))
            ][["t", "wall_time_s", "path", "rtt_latest_ms"]].copy()
            transition.insert(0, "path_label", path_label)
            transition.insert(0, "method", method)
            transition_rows.append(transition)
            per_second = _rtt_per_second(raw)
            per_second.insert(0, "path", path_label)
            per_second.insert(0, "path_id", path_id)
            per_second.insert(0, "method", method)
            series_rows.append(per_second)

    overview = pd.DataFrame(overview_rows)
    windows = pd.DataFrame(window_rows)
    transitions = pd.concat(transition_rows, ignore_index=True) if transition_rows else pd.DataFrame()
    series = pd.concat(series_rows, ignore_index=True) if series_rows else pd.DataFrame()
    paths = {
        "overview": out / "delay_clean_rtt_snapshot_diagnostics.csv",
        "windows": out / "delay_clean_rtt_window_diagnostics.csv",
        "transitions": out / "delay_clean_rtt_transition_samples.csv",
        "series": out / "delay_clean_rtt_statistic_comparison.csv",
        "figure": out / "delay_clean_rtt_diagnostic.png",
    }
    overview.to_csv(paths["overview"], index=False)
    windows.to_csv(paths["windows"], index=False)
    transitions.to_csv(paths["transitions"], index=False)
    series.to_csv(paths["series"], index=False)

    colors = _timeseries_color_maps(runs.keys())[0]
    fig, axes = plt.subplots(6, 1, figsize=(14, 16), sharex=True, constrained_layout=True)
    for method in runs:
        color = colors.get(method)
        for axis, path_label in ((axes[0], "Path A"), (axes[1], "Path B")):
            raw = raw_by_method_path.get((method, path_label), pd.DataFrame())
            if not raw.empty:
                axis.scatter(raw["t"], raw["rtt_latest_ms"], s=1, alpha=0.08,
                             rasterized=True, color=color, label=method)
        path_b = series[(series["method"] == method) & (series["path"] == "Path B")]
        for axis, column in (
            (axes[2], "rtt_1s_median_ms"),
            (axes[3], "rtt_3s_trailing_median_ms"),
            (axes[4], "rtt_1s_p10_ms"),
            (axes[5], "rtt_excess_over_run_p05_ms"),
        ):
            axis.plot(path_b["time_s"], path_b[column], color=color, label=method, linewidth=1.2)
    titles = (
        "Path A sampled/cached QUIC LatestRTT snapshots",
        "Path B sampled/cached QUIC LatestRTT snapshots",
        "Path B QUIC per-path RTT — one-second median",
        "Path B QUIC per-path RTT — fixed three-second trailing median",
        "Path B QUIC per-path RTT — one-second P10",
        "Path B non-negative excess RTT over per-run P05 proxy",
    )
    for index, (axis, title) in enumerate(zip(axes, titles)):
        axis.set_title(title, fontsize=10)
        axis.set_ylabel("Excess RTT (ms)" if index == 5 else "RTT (ms)")
        axis.axvline(50, color="tab:red", linestyle="--", linewidth=0.9)
        axis.axvline(100, color="tab:red", linestyle="--", linewidth=0.9)
        axis.grid(alpha=0.25)
        axis.legend(fontsize=8)
    axes[-1].set_xlabel("Seconds from tc profile start")
    axes[-1].set_xlim(0, 200)
    fig.savefig(paths["figure"], dpi=180)
    plt.close(fig)
    return paths


def _delay_window_metrics(
    util: pd.DataFrame,
    mon: pd.DataFrame,
    wire: pd.DataFrame,
    lo: float,
    hi: float,
) -> dict:
    u = _window_mask(util, lo, hi)
    m = _window_mask(mon, lo, hi)
    w = _window_mask(wire, lo, hi)
    ub = _path_b_rows(u)
    mb = _path_b_rows(m)
    owd_rows = ub if "owd_ms" in ub.columns and ub["owd_ms"].notna().any() else mb

    out = {
        "owd_ms_mean": float(owd_rows["owd_ms"].mean()) if "owd_ms" in owd_rows.columns and len(owd_rows) else float("nan"),
        "owd_ms_p95": _p95(owd_rows["owd_ms"]) if "owd_ms" in owd_rows.columns else float("nan"),
        "rtt_ms_mean": float(mb["rtt_smoothed_ms"].mean()) if "rtt_smoothed_ms" in mb.columns and len(mb) else float("nan"),
        "rtt_ms_p95": _p95(mb["rtt_smoothed_ms"]) if "rtt_smoothed_ms" in mb.columns else float("nan"),
        "rtt_latest_ms_mean": float(mb["rtt_latest_ms"].mean()) if "rtt_latest_ms" in mb.columns and len(mb) else float("nan"),
        "rtt_latest_ms_p10": float(mb["rtt_latest_ms"].quantile(0.10)) if "rtt_latest_ms" in mb.columns and len(mb) else float("nan"),
        "rtt_latest_ms_median": float(mb["rtt_latest_ms"].median()) if "rtt_latest_ms" in mb.columns and len(mb) else float("nan"),
        "rtt_latest_ms_p90": float(mb["rtt_latest_ms"].quantile(0.90)) if "rtt_latest_ms" in mb.columns and len(mb) else float("nan"),
        "rtt_latest_ms_p95": _p95(mb["rtt_latest_ms"]) if "rtt_latest_ms" in mb.columns else float("nan"),
        "jitter_ms_mean": float(mb["rtt_mean_dev_ms"].mean()) if "rtt_mean_dev_ms" in mb.columns and len(mb) else float("nan"),
        "path_b_share_pct_mean": float(w["path_b_share_pct"].mean()) if "path_b_share_pct" in w.columns and len(w) else float("nan"),
        "secondary_total_quic_wire_mbps_mean": float(w["total_quic_wire_mbps"].mean()) if len(w) else float("nan"),
        "secondary_path_a_quic_wire_mbps_mean": float(w["path_a_quic_wire_mbps"].mean()) if len(w) else float("nan"),
        "secondary_path_b_quic_wire_mbps_mean": float(w["path_b_quic_wire_mbps"].mean()) if len(w) else float("nan"),
    }
    return out


def _loss_window_metrics(
    util: pd.DataFrame,
    mon: pd.DataFrame,
    wire: pd.DataFrame,
    samples: pd.DataFrame,
    lo: float,
    hi: float,
) -> dict:
    u = _window_mask(util, lo, hi)
    m = _window_mask(mon, lo, hi)
    w = _window_mask(wire, lo, hi)
    s = _window_mask(samples, lo, hi)
    ub = _path_b_rows(u)
    mb = _path_b_rows(m)
    sb = _path_b_rows(s, path_col="path_id")

    loss_event_frac = float("nan")
    if not sb.empty:
        flags = []
        if "loss_rate" in sb.columns:
            flags.append(sb["loss_rate"].fillna(0) > 0)
        if "retrans_bytes_delta" in sb.columns:
            flags.append(sb["retrans_bytes_delta"].fillna(0) > 0)
        if "lost_bytes_delta" in sb.columns:
            flags.append(sb["lost_bytes_delta"].fillna(0) > 0)
        if flags:
            any_event = flags[0]
            for f in flags[1:]:
                any_event = any_event | f
            loss_event_frac = float(any_event.mean())

    out = {
        "utility_loss_mean": float(ub["loss"].mean()) if "loss" in ub.columns and len(ub) else float("nan"),
        "monitor_loss_mean": float(mb["loss"].mean()) if "loss" in mb.columns and len(mb) else float("nan"),
        "sample_loss_rate_mean": float(sb["loss_rate"].mean()) if "loss_rate" in sb.columns and len(sb) else float("nan"),
        "retrans_bytes_delta_sum": float(sb["retrans_bytes_delta"].fillna(0).sum()) if "retrans_bytes_delta" in sb.columns and len(sb) else float("nan"),
        "lost_bytes_delta_sum": float(sb["lost_bytes_delta"].fillna(0).sum()) if "lost_bytes_delta" in sb.columns and len(sb) else float("nan"),
        "loss_event_fraction": loss_event_frac,
        "path_b_share_pct_mean": float(w["path_b_share_pct"].mean()) if "path_b_share_pct" in w.columns and len(w) else float("nan"),
        "secondary_total_quic_wire_mbps_mean": float(w["total_quic_wire_mbps"].mean()) if len(w) else float("nan"),
        "secondary_path_a_quic_wire_mbps_mean": float(w["path_a_quic_wire_mbps"].mean()) if len(w) else float("nan"),
        "secondary_path_b_quic_wire_mbps_mean": float(w["path_b_quic_wire_mbps"].mean()) if len(w) else float("nan"),
    }
    return out


def analyze_run(
    run_dir: Path,
    method: str,
    objective_kind: str,
    windows: tuple[WindowSpec, ...],
    recovery_start_s: float,
    reference_window: tuple[float, float],
    clean: bool = False,
) -> tuple[pd.DataFrame, dict, pd.DataFrame, pd.DataFrame]:
    util, mon = load_pull_frames(run_dir)
    wire = load_wire_timeseries(run_dir)
    samples = load_runtime_samples(run_dir)
    if clean:
        util_time = "t" if "t" in util.columns else "time_s"
        mon_time = "t" if "t" in mon.columns else "time_s"
        util = clip_to_clean_run(util, util_time)
        mon = clip_to_clean_run(mon, mon_time)
        wire = clip_to_clean_run(wire)
        samples = clip_to_clean_run(samples)

    if not wire.empty:
        wire = wire.copy()
        wire.insert(0, "method", method)
    quality_timeseries = (
        _per_second_delay(util, mon, method)
        if objective_kind == "delay"
        else _per_second_loss(util, mon, samples, method)
    )

    recovery: dict[str, float] = {}
    ref_lo, ref_hi = reference_window
    if objective_kind == "delay":
        if clean and not mon.empty and "rtt_latest_ms" in mon.columns:
            path_b_monitor = _path_b_rows(mon)
            for key, lo, hi in (
                ("30_50", 30.0, 50.0),
                ("80_100", 80.0, 100.0),
                ("130_160", 130.0, 160.0),
            ):
                stable = _window_mask(path_b_monitor, lo, hi)
                recovery[f"steady_rtt_median_ms_{key}"] = (
                    float(stable["rtt_latest_ms"].median()) if not stable.empty else float("nan")
                )
        if not util.empty and "owd_ms" in util.columns and util["owd_ms"].notna().any():
            ref_series = util.rename(columns={"t": "time_s"})
        elif not mon.empty and "owd_ms" in mon.columns and mon["owd_ms"].notna().any():
            ref_series = mon.rename(columns={"t": "time_s"})
        else:
            ref_series = pd.DataFrame()
        if not ref_series.empty and "owd_ms" in ref_series.columns:
            recovery["recovery_time_s_owd"] = _recovery_time_s(
                _path_b_rows(ref_series), tcol="time_s", vcol="owd_ms", ref_lo=ref_lo, ref_hi=ref_hi,
                recover_after=recovery_start_s,
            )
        share_series = wire if not wire.empty else pd.DataFrame()
        if not share_series.empty:
            recovery["recovery_time_s_path_b_share"] = _recovery_time_s(
                share_series, tcol="time_s", vcol="path_b_share_pct",
                ref_lo=ref_lo, ref_hi=ref_hi, recover_after=recovery_start_s, tol_frac=0.10,
            )
    else:
        if not samples.empty and "loss_rate" in samples.columns:
            sb = _path_b_rows(samples, path_col="path_id")
            recovery["recovery_time_s_loss_rate"] = _recovery_time_s(
                sb, tcol="time_s", vcol="loss_rate", ref_lo=ref_lo, ref_hi=ref_hi,
                recover_after=recovery_start_s, absolute_tol=1e-6,
            )
        share_series = wire if not wire.empty else pd.DataFrame()
        if not share_series.empty:
            recovery["recovery_time_s_path_b_share"] = _recovery_time_s(
                share_series, tcol="time_s", vcol="path_b_share_pct",
                ref_lo=ref_lo, ref_hi=ref_hi, recover_after=recovery_start_s, tol_frac=0.10,
            )

    rows: list[dict] = []
    for window in windows:
        wname, lo, hi = window.name, window.start_s, window.end_s
        if objective_kind == "delay":
            metrics = _delay_window_metrics(util.rename(columns={"t": "time_s"}) if "t" in util.columns else util,
                                            mon.rename(columns={"t": "time_s"}) if "t" in mon.columns else mon,
                                            wire, lo, hi)
        else:
            metrics = _loss_window_metrics(
                util.rename(columns={"t": "time_s"}) if "t" in util.columns else util,
                mon.rename(columns={"t": "time_s"}) if "t" in mon.columns else mon,
                wire, samples, lo, hi,
            )
        rows.append({
            "method": method,
            "window": wname,
            "t_lo": lo,
            "t_hi": hi,
            "window_role": window.role,
            "condition": window.condition,
            "primary_metric": (
                CLEAN_PRESETS[f"{objective_kind}_clean"]["primary_metric"] if clean else objective_kind
            ),
            **metrics,
        })

    return pd.DataFrame(rows), recovery, wire, quality_timeseries


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Delay/loss-primary analysis (throughput is secondary)",
    )
    ap.add_argument("--session", type=Path, required=True)
    ap.add_argument("--preset", choices=["auto", *sorted(PRESETS)], default="auto")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--full-hi", type=float, default=200.0)
    ap.add_argument(
        "--rtt-audit-only", action="store_true",
        help="Generate endpoint-consistent QUIC RTT diagnostics without reading pcaps",
    )
    args = ap.parse_args()

    session = args.session.resolve()
    if not session.is_dir():
        print(f"[error] session not found: {session}", file=sys.stderr)
        sys.exit(1)

    metadata = _load_metadata(session)
    selected_preset = args.preset
    if selected_preset == "auto":
        selected_preset = preset_from_metadata(metadata) or str(metadata.get("profile_kind") or "")
        if selected_preset not in PRESETS:
            print("[error] --preset auto could not resolve delay/loss evaluator preset", file=sys.stderr)
            sys.exit(2)
    cfg = PRESETS[selected_preset]
    clean_preset = cfg.get("clean_preset")
    windows = clean_windows(str(clean_preset), metadata) if clean_preset else _historical_windows()
    if not clean_preset and args.full_hi != 200.0:
        windows = windows[:-1] + (WindowSpec("0-200", 0.0, args.full_hi, "full_run", "full run"),)
    reference_window = tuple(cfg.get("reference_window", (50.0, 90.0)))
    out = (args.out or (_REPO / "derived" / cfg["out_subdir"] / session.name)).resolve()
    out.mkdir(parents=True, exist_ok=True)

    dynamic_dir = next(
        (session / name for name in cfg["dynamic_dirs"] if (session / name).is_dir()),
        session / cfg["dynamic_dirs"][0],
    )
    runs = {
        "baseline": session / cfg["baseline_dir"],
        str(cfg["dynamic_dirs"][0]).removesuffix("_dynamic"): dynamic_dir,
    }
    if args.rtt_audit_only:
        if str(cfg["objective_kind"]) != "delay":
            ap.error("--rtt-audit-only requires a delay preset")
        audit_paths = _generate_rtt_audit(runs, out)
        for name, path in audit_paths.items():
            print(f"RTT audit {name}: {path}")
        return

    all_windows: list[pd.DataFrame] = []
    all_wire: list[pd.DataFrame] = []
    all_quality: list[pd.DataFrame] = []
    recovery_rows: list[dict] = []
    for method, rdir in runs.items():
        if not rdir.is_dir():
            print(f"[warn] missing run dir: {rdir}", file=sys.stderr)
            continue
        df, rec, wire, quality = analyze_run(
            rdir, method, str(cfg["objective_kind"]), windows, float(cfg["recovery_start_s"]),
            (float(reference_window[0]), float(reference_window[1])), clean=bool(clean_preset),
        )
        if not df.empty:
            all_windows.append(df)
        if not wire.empty:
            all_wire.append(wire)
        if not quality.empty:
            all_quality.append(quality)
        recovery_rows.append({"method": method, "run_dir": str(rdir), **rec})

    if not all_windows:
        print("[error] no window metrics produced (need pcaps and/or SAVE_LOGS=1 pull logs)", file=sys.stderr)
        sys.exit(2)

    prefix = cfg["file_prefix"]
    df_win = pd.concat(all_windows, ignore_index=True)
    df_wire = pd.concat(all_wire, ignore_index=True) if all_wire else pd.DataFrame()
    df_quality = pd.concat(all_quality, ignore_index=True) if all_quality else pd.DataFrame()
    df_win.to_csv(out / f"{prefix}_primary_metrics_windows.csv", index=False)
    pd.DataFrame(recovery_rows).to_csv(out / f"{prefix}_recovery_times.csv", index=False)
    if not df_wire.empty:
        df_wire.to_csv(out / f"{prefix}_throughput_timeseries.csv", index=False)
    if not df_quality.empty:
        quality_name = str(cfg["objective_kind"])
        df_quality.to_csv(out / f"{prefix}_{quality_name}_timeseries.csv", index=False)

    # Secondary throughput table (explicitly labeled).
    sec_cols = [
        "method", "window", "secondary_total_quic_wire_mbps_mean",
        "secondary_path_a_quic_wire_mbps_mean", "secondary_path_b_quic_wire_mbps_mean",
    ]
    if clean_preset:
        sec_cols[2:2] = ["window_role", "condition"]
    df_win[sec_cols].to_csv(out / f"{prefix}_secondary_throughput_windows.csv", index=False)
    dynamic_method = str(cfg["dynamic_dirs"][0]).removesuffix("_dynamic")
    comparison = build_improvement_table(df_win, dynamic_method)
    comparison.to_csv(out / f"{prefix}_baseline_vs_qaccess_improvement.csv", index=False)
    steady_rtt_medians: dict[str, list[tuple[float, float, float]]] = {}
    if str(cfg["objective_kind"]) == "delay" and clean_preset:
        for row in recovery_rows:
            method = str(row["method"])
            segments = []
            for key, lo, hi in (
                ("30_50", 30.0, 50.0),
                ("80_100", 80.0, 100.0),
                ("130_160", 130.0, 160.0),
            ):
                value = float(row.get(f"steady_rtt_median_ms_{key}", float("nan")))
                if math.isfinite(value):
                    segments.append((lo, hi, value))
            steady_rtt_medians[method] = segments
    _plot_timeseries(
        df_wire, df_quality, out, prefix, str(cfg["objective_kind"]),
        windows if clean_preset else None,
        steady_rtt_medians=steady_rtt_medians,
    )
    owd_plot = out / f"{prefix}_throughput_owd_proxy_over_time.png"
    if (
        str(cfg["objective_kind"]) == "delay"
        and "owd_ms_mean" in df_quality.columns
        and df_quality["owd_ms_mean"].notna().any()
    ):
        _plot_timeseries(
            df_wire,
            df_quality,
            out,
            prefix,
            str(cfg["objective_kind"]),
            windows if clean_preset else None,
            quality_metric="owd_ms_mean",
            output_name=owd_plot.name,
        )

    rtt_audit_paths: dict[str, Path] = {}
    if str(cfg["objective_kind"]) == "delay":
        rtt_audit_paths = _generate_rtt_audit(runs, out)

    execution_mode = metadata.get("execution_mode", "unknown")

    print(f"Experiment: {cfg['title']}")
    print(f"Evaluator preset: {selected_preset}")
    print(f"Session: {session}")
    print(f"Primary metrics: {out / f'{prefix}_primary_metrics_windows.csv'}")
    print(f"Recovery times:  {out / f'{prefix}_recovery_times.csv'}")
    print(f"Secondary TP:    {out / f'{prefix}_secondary_throughput_windows.csv'}")
    print(f"Comparison:      {out / f'{prefix}_baseline_vs_qaccess_improvement.csv'}")
    print(f"Time-series plot:{out / f'{prefix}_throughput_quality_over_time.png'}")
    if owd_plot.is_file():
        print(f"OWD-proxy plot:  {owd_plot}")
    for name, path in rtt_audit_paths.items():
        print(f"RTT audit {name}: {path}")
    print(f"\nWorker execution mode recorded by runner: {execution_mode}")
    print("Evaluation is read-only and does not start or stop the worker.")
    print(df_win.to_string(index=False, float_format="%.3f"))


if __name__ == "__main__":
    main()
