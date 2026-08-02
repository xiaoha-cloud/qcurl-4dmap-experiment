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
        "title": "Delay-only (primary: delay/RTT/recovery)",
        "baseline_dir": "delay_baseline",
        "dynamic_dirs": ("delay_qaccess_d_dynamic", "delay_qaccess_dynamic"),
        "out_subdir": "delay_only_compare",
        "file_prefix": "delay",
        "recovery_start_s": 150.0,
    },
    "loss": {
        "title": "Loss-only (primary: loss/retrans/recovery)",
        "baseline_dir": "loss_baseline",
        "dynamic_dirs": ("loss_qaccess_l_dynamic", "loss_qaccess_dynamic"),
        "out_subdir": "loss_only_compare",
        "file_prefix": "loss",
        "recovery_start_s": 100.0,
    },
}


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


def load_wire_timeseries(run_dir: Path) -> pd.DataFrame:
    pcap_dir = run_dir / "pcaps"
    pcaps = sorted(pcap_dir.glob("pathA_*.pcap")) + sorted(pcap_dir.glob("pathB_*.pcap"))
    if not pcaps:
        return pd.DataFrame()

    epochs = [epoch for p in pcaps if (epoch := _first_pcap_epoch(p)) is not None]
    if not epochs:
        return pd.DataFrame()

    global_t0 = min(epochs)
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


def _metric_log_candidates(run_dir: Path) -> list[Path]:
    logs = run_dir / "logs"
    patterns = ("pull_*.log", "server_*.log", "combined_*.log")
    hits: list[Path] = []
    for pattern in patterns:
        hits.extend(sorted(logs.glob(pattern)) if logs.is_dir() else [])
    if not hits:
        for pattern in patterns:
            hits.extend(sorted(run_dir.glob(f"**/{pattern}")))
    return list(dict.fromkeys(hits))


def load_pull_frames(run_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    candidates = _metric_log_candidates(run_dir)
    if not candidates:
        return pd.DataFrame(), pd.DataFrame()
    try:
        from parse_logs import load_pull_log  # type: ignore
    except ImportError:
        return pd.DataFrame(), pd.DataFrame()
    best_util, best_mon = pd.DataFrame(), pd.DataFrame()
    best_score = -1
    for candidate in candidates:
        df_util, df_mon = load_pull_log(candidate, label=run_dir.name)
        util = df_util if df_util is not None else pd.DataFrame()
        mon = df_mon if df_mon is not None else pd.DataFrame()
        score = len(util) + len(mon)
        if score > best_score:
            best_util, best_mon, best_score = util, mon, score
    return best_util, best_mon


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
    # Pull utility logs use path=2 for Path B; qserver runtime samples use path_id=3.
    preferred_ids = (3, 2) if path_col == "path_id" else (2, 3)
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
    stable_sec: int = 10,
) -> float:
    if series.empty or vcol not in series.columns:
        return float("nan")
    ref = _window_mask(series.rename(columns={tcol: "time_s"}), ref_lo, ref_hi)
    if ref.empty:
        return float("nan")
    target = float(ref[vcol].mean())
    if not math.isfinite(target) or target <= 0:
        return float("nan")
    post = series[series[tcol] >= recover_after].sort_values(tcol)
    if post.empty:
        return float("nan")
    thresh = target * (1.0 + tol_frac)
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
        "rtt_ms_mean", "rtt_ms_p95", "jitter_ms_mean",
    ]
    pieces: list[pd.DataFrame] = []
    if not util.empty and "owd_ms" in util.columns:
        u = util.rename(columns={"t": "time_s"}) if "t" in util.columns else util.copy()
        u = _path_b_rows(u)
        if not u.empty:
            u = u.copy()
            u["time_s"] = pd.to_numeric(u["time_s"], errors="coerce").floordiv(1)
            pieces.append(
                u.groupby("time_s", as_index=False)["owd_ms"]
                .agg(owd_ms_mean="mean", owd_ms_p95=lambda s: s.quantile(0.95))
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
            if "rtt_mean_dev_ms" in m.columns:
                agg["jitter_ms_mean"] = ("rtt_mean_dev_ms", "mean")
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
        "jitter_ms_mean": False,
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
            rows.append({
                "window": window,
                "metric": metric,
                "baseline": baseline,
                "qaccess": dynamic,
                "improvement_pct": _pct_change(baseline, dynamic, higher_is_better),
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


def _loss_span_from_tc_logs(session: Path) -> tuple[float, float] | None:
    steps: list[tuple[float, float]] = []
    pattern = re.compile(r"profile_step\[\d+\] at=([0-9.]+)s loss=([0-9.]+)%")
    for log_path in sorted(session.glob("*/logs/tc_loss_*.log")):
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = pattern.search(line)
            if match:
                steps.append((float(match.group(1)), float(match.group(2))))
        if steps:
            break
    if len(steps) < 2:
        return None
    steps = sorted(steps)
    for idx, (start, loss_pct) in enumerate(steps):
        if loss_pct <= 0:
            continue
        end = steps[idx + 1][0] if idx + 1 < len(steps) else start
        if end > start:
            return start, end
    return None


def _plot_timeseries(
    throughput: pd.DataFrame,
    delay: pd.DataFrame,
    out: Path,
    prefix: str,
    impairment_span: tuple[float, float] | None = None,
) -> None:
    if throughput.empty and delay.empty:
        return

    fig, axes = plt.subplots(3, 1, figsize=(11, 9), sharex=True)
    for method, group in throughput.groupby("method"):
        axes[0].plot(group["time_s"], group["total_quic_wire_mbps"], label=method, linewidth=1.5)
    axes[0].set_ylabel("Throughput (Mbps)")
    axes[0].set_title("Total throughput from all captured frames")
    axes[0].grid(alpha=0.25)
    axes[0].legend()

    for method, group in throughput.groupby("method"):
        axes[1].plot(
            group["time_s"], group["path_a_quic_wire_mbps"],
            label=f"{method} Path A", linewidth=1.1,
        )
        axes[1].plot(
            group["time_s"], group["path_b_quic_wire_mbps"],
            label=f"{method} Path B", linewidth=1.1, linestyle="--",
        )
    axes[1].set_ylabel("Per-path (Mbps)")
    axes[1].grid(alpha=0.25)
    axes[1].legend(ncol=2, fontsize=8)

    if not delay.empty and delay["owd_ms_mean"].notna().any():
        for method, group in delay.groupby("method"):
            axes[2].plot(group["time_s"], group["owd_ms_mean"], label=method, linewidth=1.5)
        axes[2].set_ylabel("Path B OWD (ms)")
        axes[2].legend()
    else:
        axes[2].text(0.5, 0.5, "No OWD samples found", ha="center", va="center",
                     transform=axes[2].transAxes)
        axes[2].set_ylabel("Path B OWD (ms)")
    axes[2].set_xlabel("Time (s)")
    axes[2].grid(alpha=0.25)
    span = impairment_span or ((90.0, 150.0) if prefix == "delay" else (90.0, 100.0))
    for axis in axes:
        axis.axvspan(span[0], span[1], color="tab:red", alpha=0.08)
    fig.tight_layout()
    fig.savefig(out / f"{prefix}_throughput_delay_over_time.png", dpi=180)
    plt.close(fig)


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

    out = {
        "owd_ms_mean": float(ub["owd_ms"].mean()) if "owd_ms" in ub.columns and len(ub) else float("nan"),
        "owd_ms_p95": _p95(ub["owd_ms"]) if "owd_ms" in ub.columns else float("nan"),
        "rtt_ms_mean": float(mb["rtt_smoothed_ms"].mean()) if "rtt_smoothed_ms" in mb.columns and len(mb) else float("nan"),
        "rtt_ms_p95": _p95(mb["rtt_smoothed_ms"]) if "rtt_smoothed_ms" in mb.columns else float("nan"),
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
    preset: str,
    full_hi: float,
    recovery_start_s: float,
) -> tuple[pd.DataFrame, dict, pd.DataFrame, pd.DataFrame]:
    util, mon = load_pull_frames(run_dir)
    wire = load_wire_timeseries(run_dir)
    samples = load_runtime_samples(run_dir)

    if not wire.empty:
        wire = wire.copy()
        wire.insert(0, "method", method)
    delay_timeseries = _per_second_delay(util, mon, method)

    recovery: dict[str, float] = {}
    if preset == "delay":
        ref_series = util.rename(columns={"t": "time_s"}) if not util.empty else pd.DataFrame()
        if not ref_series.empty and "owd_ms" in ref_series.columns:
            recovery["recovery_time_s_owd"] = _recovery_time_s(
                _path_b_rows(ref_series), tcol="time_s", vcol="owd_ms", ref_lo=50.0, ref_hi=90.0,
                recover_after=recovery_start_s,
            )
        share_series = wire if not wire.empty else pd.DataFrame()
        if not share_series.empty:
            recovery["recovery_time_s_path_b_share"] = _recovery_time_s(
                share_series, tcol="time_s", vcol="path_b_share_pct",
                ref_lo=50.0, ref_hi=90.0, recover_after=recovery_start_s, tol_frac=0.10,
            )
    else:
        if not samples.empty and "loss_rate" in samples.columns:
            sb = _path_b_rows(samples, path_col="path_id")
            recovery["recovery_time_s_loss_rate"] = _recovery_time_s(
                sb, tcol="time_s", vcol="loss_rate", ref_lo=50.0, ref_hi=90.0,
                recover_after=recovery_start_s,
            )
        share_series = wire if not wire.empty else pd.DataFrame()
        if not share_series.empty:
            recovery["recovery_time_s_path_b_share"] = _recovery_time_s(
                share_series, tcol="time_s", vcol="path_b_share_pct",
                ref_lo=50.0, ref_hi=90.0, recover_after=recovery_start_s, tol_frac=0.10,
            )

    rows: list[dict] = []
    windows = [*EVAL_WINDOWS, ("0-200", 0.0, full_hi)]
    for wname, lo, hi in windows:
        if preset == "delay":
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
            **metrics,
        })

    return pd.DataFrame(rows), recovery, wire, delay_timeseries


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Delay/loss-primary analysis (throughput is secondary)",
    )
    ap.add_argument("--session", type=Path, required=True)
    ap.add_argument("--preset", choices=sorted(PRESETS), required=True)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--full-hi", type=float, default=200.0)
    args = ap.parse_args()

    session = args.session.resolve()
    if not session.is_dir():
        print(f"[error] session not found: {session}", file=sys.stderr)
        sys.exit(1)

    cfg = PRESETS[args.preset]
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

    all_windows: list[pd.DataFrame] = []
    all_wire: list[pd.DataFrame] = []
    all_delay: list[pd.DataFrame] = []
    recovery_rows: list[dict] = []
    for method, rdir in runs.items():
        if not rdir.is_dir():
            print(f"[warn] missing run dir: {rdir}", file=sys.stderr)
            continue
        df, rec, wire, delay = analyze_run(
            rdir, method, args.preset, args.full_hi, float(cfg["recovery_start_s"]),
        )
        if not df.empty:
            all_windows.append(df)
        if not wire.empty:
            all_wire.append(wire)
        if not delay.empty:
            all_delay.append(delay)
        recovery_rows.append({"method": method, "run_dir": str(rdir), **rec})

    if not all_windows:
        print("[error] no window metrics produced (need pcaps and/or SAVE_LOGS=1 pull logs)", file=sys.stderr)
        sys.exit(2)

    prefix = cfg["file_prefix"]
    df_win = pd.concat(all_windows, ignore_index=True)
    df_wire = pd.concat(all_wire, ignore_index=True) if all_wire else pd.DataFrame()
    df_delay = pd.concat(all_delay, ignore_index=True) if all_delay else pd.DataFrame()
    df_win.to_csv(out / f"{prefix}_primary_metrics_windows.csv", index=False)
    pd.DataFrame(recovery_rows).to_csv(out / f"{prefix}_recovery_times.csv", index=False)
    if not df_wire.empty:
        df_wire.to_csv(out / f"{prefix}_throughput_timeseries.csv", index=False)
    if not df_delay.empty:
        df_delay.to_csv(out / f"{prefix}_delay_timeseries.csv", index=False)

    # Secondary throughput table (explicitly labeled).
    sec_cols = [
        "method", "window", "secondary_total_quic_wire_mbps_mean",
        "secondary_path_a_quic_wire_mbps_mean", "secondary_path_b_quic_wire_mbps_mean",
    ]
    df_win[sec_cols].to_csv(out / f"{prefix}_secondary_throughput_windows.csv", index=False)
    dynamic_method = str(cfg["dynamic_dirs"][0]).removesuffix("_dynamic")
    comparison = build_improvement_table(df_win, dynamic_method)
    comparison.to_csv(out / f"{prefix}_baseline_vs_qaccess_improvement.csv", index=False)
    impairment_span = _loss_span_from_tc_logs(session) if args.preset == "loss" else None
    _plot_timeseries(df_wire, df_delay, out, prefix, impairment_span)

    metadata_path = session / "experiment_metadata.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
    execution_mode = metadata.get("execution_mode", "unknown")

    print(f"Experiment: {cfg['title']}")
    print(f"Session: {session}")
    print(f"Primary metrics: {out / f'{prefix}_primary_metrics_windows.csv'}")
    print(f"Recovery times:  {out / f'{prefix}_recovery_times.csv'}")
    print(f"Secondary TP:    {out / f'{prefix}_secondary_throughput_windows.csv'}")
    print(f"Comparison:      {out / f'{prefix}_baseline_vs_qaccess_improvement.csv'}")
    print(f"Time-series plot:{out / f'{prefix}_throughput_delay_over_time.png'}")
    print(f"\nWorker execution mode recorded by runner: {execution_mode}")
    print("Evaluation is read-only and does not start or stop the worker.")
    print(df_win.to_string(index=False, float_format="%.3f"))


if __name__ == "__main__":
    main()
