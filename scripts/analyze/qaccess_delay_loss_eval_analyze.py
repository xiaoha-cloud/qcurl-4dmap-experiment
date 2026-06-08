#!/usr/bin/env python3
"""
Analyze delay-only and loss-only Q-ACCeSS experiments.

Primary metrics differ from Fig.7 throughput eval:
  delay — OWD/RTT proxy, jitter, path-B usage shift, recovery time
  loss  — loss rate, retrans/lost-byte proxy, path-B usage shift, recovery time

Throughput (total / path A / path B) is reported as secondary only.
Worker-based throughput RF optimization is NOT the objective here.
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
from pathlib import Path

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

PRESETS: dict[str, dict[str, str]] = {
    "delay": {
        "title": "Delay-only (primary: delay/RTT/recovery)",
        "baseline_dir": "delay_baseline",
        "dynamic_dir": "delay_qaccess_dynamic",
        "out_subdir": "delay_only_compare",
        "file_prefix": "delay",
    },
    "loss": {
        "title": "Loss-only (primary: loss/retrans/recovery)",
        "baseline_dir": "loss_baseline",
        "dynamic_dir": "loss_qaccess_dynamic",
        "out_subdir": "loss_only_compare",
        "file_prefix": "loss",
    },
}


def _p95(s: pd.Series) -> float:
    if s.empty or s.isna().all():
        return float("nan")
    return float(s.quantile(0.95))


def _pcap_bytes_by_second(pcap: Path, global_t0: float) -> dict[int, int]:
    cmd = [
        "tshark", "-r", str(pcap),
        "-T", "fields", "-E", "separator=,",
        "-e", "frame.time_epoch", "-e", "frame.len",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"tshark failed for {pcap}: {proc.stderr[:500]}")
    bins: dict[int, int] = {}
    for line in proc.stdout.splitlines():
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
    return bins


def load_wire_timeseries(run_dir: Path) -> pd.DataFrame:
    pcap_dir = run_dir / "pcaps"
    pcaps = sorted(pcap_dir.glob("pathA_*.pcap")) + sorted(pcap_dir.glob("pathB_*.pcap"))
    if not pcaps:
        return pd.DataFrame()

    epochs: list[float] = []
    for p in pcaps:
        out = subprocess.run(
            ["tshark", "-r", str(p), "-T", "fields", "-e", "frame.time_epoch"],
            capture_output=True, text=True, check=True,
        )
        for line in out.stdout.splitlines():
            if line.strip():
                epochs.append(float(line.strip()))
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


def _find_pull(run_dir: Path) -> Path | None:
    logs = run_dir / "logs"
    hits = sorted(logs.glob("pull_*.log")) if logs.is_dir() else []
    if hits:
        return hits[-1]
    hits = sorted(run_dir.glob("**/pull_*.log"))
    return hits[-1] if hits else None


def load_pull_frames(run_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    pull = _find_pull(run_dir)
    if pull is None:
        return pd.DataFrame(), pd.DataFrame()
    try:
        from parse_logs import load_pull_log  # type: ignore
    except ImportError:
        return pd.DataFrame(), pd.DataFrame()
    df_util, df_mon = load_pull_log(pull, label=run_dir.name)
    return df_util if df_util is not None else pd.DataFrame(), df_mon if df_mon is not None else pd.DataFrame()


def load_runtime_samples(run_dir: Path) -> pd.DataFrame:
    for p in (
        run_dir / "qaccess_runtime_samples.csv",
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
    # Mininet path B is typically path id 2 in logs.
    pb = df[df[path_col].astype(str).isin(("2", "2.0"))]
    return pb if not pb.empty else df


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
) -> tuple[pd.DataFrame, dict]:
    util, mon = load_pull_frames(run_dir)
    wire = load_wire_timeseries(run_dir)
    samples = load_runtime_samples(run_dir)

    if not wire.empty:
        wire.to_csv(run_dir / f"wire_timeseries_{method}.csv", index=False)

    recovery: dict[str, float] = {}
    if preset == "delay":
        ref_series = util.rename(columns={"t": "time_s"}) if not util.empty else pd.DataFrame()
        if not ref_series.empty and "owd_ms" in ref_series.columns:
            recovery["recovery_time_s_owd"] = _recovery_time_s(
                _path_b_rows(ref_series), tcol="time_s", vcol="owd_ms", ref_lo=50.0, ref_hi=90.0,
            )
        share_series = wire if not wire.empty else pd.DataFrame()
        if not share_series.empty:
            recovery["recovery_time_s_path_b_share"] = _recovery_time_s(
                share_series, tcol="time_s", vcol="path_b_share_pct",
                ref_lo=50.0, ref_hi=90.0, tol_frac=0.10,
            )
    else:
        if not samples.empty and "loss_rate" in samples.columns:
            sb = _path_b_rows(samples, path_col="path_id")
            recovery["recovery_time_s_loss_rate"] = _recovery_time_s(
                sb, tcol="time_s", vcol="loss_rate", ref_lo=50.0, ref_hi=90.0,
            )
        share_series = wire if not wire.empty else pd.DataFrame()
        if not share_series.empty:
            recovery["recovery_time_s_path_b_share"] = _recovery_time_s(
                share_series, tcol="time_s", vcol="path_b_share_pct",
                ref_lo=50.0, ref_hi=90.0, tol_frac=0.10,
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

    return pd.DataFrame(rows), recovery


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

    runs = {
        "baseline": session / cfg["baseline_dir"],
        "qaccess_dynamic": session / cfg["dynamic_dir"],
    }

    all_windows: list[pd.DataFrame] = []
    recovery_rows: list[dict] = []
    for method, rdir in runs.items():
        if not rdir.is_dir():
            print(f"[warn] missing run dir: {rdir}", file=sys.stderr)
            continue
        df, rec = analyze_run(rdir, method, args.preset, args.full_hi)
        if not df.empty:
            all_windows.append(df)
        recovery_rows.append({"method": method, "run_dir": str(rdir), **rec})

    if not all_windows:
        print("[error] no window metrics produced (need pcaps and/or SAVE_LOGS=1 pull logs)", file=sys.stderr)
        sys.exit(2)

    prefix = cfg["file_prefix"]
    df_win = pd.concat(all_windows, ignore_index=True)
    df_win.to_csv(out / f"{prefix}_primary_metrics_windows.csv", index=False)
    pd.DataFrame(recovery_rows).to_csv(out / f"{prefix}_recovery_times.csv", index=False)

    # Secondary throughput table (explicitly labeled).
    sec_cols = [
        "method", "window", "secondary_total_quic_wire_mbps_mean",
        "secondary_path_a_quic_wire_mbps_mean", "secondary_path_b_quic_wire_mbps_mean",
    ]
    df_win[sec_cols].to_csv(out / f"{prefix}_secondary_throughput_windows.csv", index=False)

    print(f"Experiment: {cfg['title']}")
    print(f"Session: {session}")
    print(f"Primary metrics: {out / f'{prefix}_primary_metrics_windows.csv'}")
    print(f"Recovery times:  {out / f'{prefix}_recovery_times.csv'}")
    print(f"Secondary TP:    {out / f'{prefix}_secondary_throughput_windows.csv'}")
    print("\nNote: worker disabled in eval scripts; do not interpret as throughput-RF optimization.")
    print(df_win.to_string(index=False, float_format="%.3f"))


if __name__ == "__main__":
    main()
