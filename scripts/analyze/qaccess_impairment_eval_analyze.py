#!/usr/bin/env python3
"""
Analyze baseline vs qaccess_t_dynamic for network impairment experiments.

Presets:
  fig8   — Fig.8-style sudden link quality deterioration (delay+loss together)
  delay  — Delay-only diagnostic (ablation)
  loss   — Loss-only diagnostic (ablation)

Metrics: QUIC wire throughput (total / Path A / Path B), standard windows, FLV size,
optional pull-log metrics around 85–105s.
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

IMPAIRMENT_WINDOWS = [
    ("0-50", 0.0, 50.0),
    ("50-90", 50.0, 90.0),
    ("90-100", 90.0, 100.0),
    ("100-110", 100.0, 110.0),
    ("100-150", 100.0, 150.0),
    ("150-200", 150.0, 200.0),
    ("0-200", 0.0, 200.0),
]

PRESETS: dict[str, dict[str, str]] = {
    "combined": {
        "title": "Fig.8-style combined sudden link quality deterioration",
        "baseline_dir": "combined_baseline",
        "dynamic_dir": "combined_qaccess_t_dynamic",
        "out_subdir": "combined_deterioration_compare",
        "file_prefix": "combined",
    },
    "fig8": {
        "title": "Fig.8-style sudden link quality deterioration (legacy dir names)",
        "baseline_dir": "fig8_baseline",
        "dynamic_dir": "fig8_qaccess_t_dynamic",
        "out_subdir": "fig8_compare",
        "file_prefix": "fig8",
    },
    "delay": {
        "title": "Delay-only diagnostic",
        "baseline_dir": "delay_baseline",
        "dynamic_dir": "delay_qaccess_t_dynamic",
        "out_subdir": "delay_only_compare",
        "file_prefix": "delay",
    },
    "loss": {
        "title": "Loss-only diagnostic",
        "baseline_dir": "loss_baseline",
        "dynamic_dir": "loss_qaccess_t_dynamic",
        "out_subdir": "loss_only_compare",
        "file_prefix": "loss",
    },
}


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


def load_run_wire_timeseries(run_dir: Path) -> pd.DataFrame:
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
        rows.append({
            "time_s": float(s),
            "path_a_quic_wire_mbps": ba * 8 / 1e6,
            "path_b_quic_wire_mbps": bb * 8 / 1e6,
            "total_quic_wire_mbps": (ba + bb) * 8 / 1e6,
        })
    return pd.DataFrame(rows)


def mean_in_window(ts: pd.DataFrame, col: str, lo: float, hi: float) -> float:
    if ts.empty:
        return float("nan")
    w = ts[(ts["time_s"] >= lo) & (ts["time_s"] < hi)]
    if w.empty:
        return float("nan")
    return float(w[col].mean())


def _find_pull(run_dir: Path) -> Path | None:
    logs = run_dir / "logs"
    hits = sorted(logs.glob("pull_*.log")) if logs.is_dir() else []
    if hits:
        return hits[-1]
    hits = sorted(run_dir.glob("**/pull_*.log"))
    return hits[-1] if hits else None


def load_log_metrics_85_105(run_dir: Path) -> dict:
    pull = _find_pull(run_dir)
    if pull is None:
        return {"pull_log": ""}
    try:
        from parse_logs import load_pull_log  # type: ignore
    except ImportError:
        return {"pull_log": str(pull)}

    df_util, df_mon = load_pull_log(pull, label=run_dir.name)
    out: dict = {"pull_log": str(pull)}
    for name, df in [("utility", df_util), ("monitor", df_mon)]:
        if df is None or df.empty or "t" not in df.columns:
            continue
        w = df[(df["t"] >= 85) & (df["t"] < 105)]
        out[f"n_{name}_85_105"] = int(len(w))
        for col in ("owd_ms", "rtt_smoothed", "loss", "gain", "backoff", "retrans_bytes_delta"):
            if col in w.columns and not w.empty:
                out[f"{name}_{col}_mean_85_105"] = float(w[col].mean())
    return out


def flv_size_mb(run_dir: Path) -> float:
    hits = sorted(run_dir.glob("output_*.flv"))
    return hits[-1].stat().st_size / 1_000_000.0 if hits else float("nan")


def analyze_session(
    session: Path,
    out: Path,
    *,
    title: str,
    baseline_dir: str,
    dynamic_dir: str,
    file_prefix: str,
    full_hi: float,
) -> None:
    runs = {
        "baseline": session / baseline_dir,
        "qaccess_t_dynamic": session / dynamic_dir,
    }
    out.mkdir(parents=True, exist_ok=True)

    window_rows: list[dict] = []
    flv_rows: list[dict] = []
    log_rows: list[dict] = []

    for label, rdir in runs.items():
        if not rdir.is_dir():
            print(f"[warn] missing run dir: {rdir}", file=sys.stderr)
            continue
        ts = load_run_wire_timeseries(rdir)
        if not ts.empty:
            ts.to_csv(out / f"{file_prefix}_wire_timeseries_{label}.csv", index=False)
        flv_rows.append({"method": label, "output_flv_mb": flv_size_mb(rdir), "run_dir": str(rdir)})
        log_rows.append({"method": label, **load_log_metrics_85_105(rdir)})

        for wname, lo, hi in [*IMPAIRMENT_WINDOWS, ("full", 0.0, full_hi)]:
            window_rows.append({
                "experiment": title,
                "method": label,
                "window": wname,
                "t_lo": lo,
                "t_hi": hi,
                "total_quic_wire_mbps_mean": mean_in_window(ts, "total_quic_wire_mbps", lo, hi),
                "path_a_quic_wire_mbps_mean": mean_in_window(ts, "path_a_quic_wire_mbps", lo, hi),
                "path_b_quic_wire_mbps_mean": mean_in_window(ts, "path_b_quic_wire_mbps", lo, hi),
            })

    if not window_rows:
        print("[error] no window data", file=sys.stderr)
        sys.exit(2)

    prefix = file_prefix
    df_win = pd.DataFrame(window_rows)
    df_win.to_csv(out / f"{prefix}_throughput_windows.csv", index=False)
    pd.DataFrame(flv_rows).to_csv(out / f"{prefix}_output_flv.csv", index=False)
    pd.DataFrame(log_rows).to_csv(out / f"{prefix}_log_metrics_85_105.csv", index=False)

    base = df_win[df_win["method"] == "baseline"].set_index("window")
    dyn = df_win[df_win["method"] == "qaccess_t_dynamic"].set_index("window")
    imp_rows = []
    for w in dyn.index:
        b = float(base.loc[w, "total_quic_wire_mbps_mean"]) if w in base.index else float("nan")
        e = float(dyn.loc[w, "total_quic_wire_mbps_mean"])
        pct = (e - b) / b * 100.0 if b == b and b > 0 and e == e else float("nan")
        imp_rows.append({
            "window": w,
            "baseline_quic_wire_mbps": b,
            "dynamic_quic_wire_mbps": e,
            "improvement_pct": pct,
        })
    pd.DataFrame(imp_rows).to_csv(out / f"{prefix}_improvement_vs_baseline.csv", index=False)

    print(f"Experiment: {title}")
    print(f"Session: {session}")
    print(f"Wrote: {out}/{prefix}_throughput_windows.csv")
    print(f"Wrote: {out}/{prefix}_improvement_vs_baseline.csv")
    print(f"Wrote: {out}/{prefix}_output_flv.csv")
    print("\nImprovement vs baseline (total QUIC wire Mbps, %):")
    print(pd.DataFrame(imp_rows).to_string(index=False, float_format="%.2f"))


def main() -> None:
    ap = argparse.ArgumentParser(description="Analyze baseline vs qaccess_t_dynamic impairment eval")
    ap.add_argument("--session", type=Path, required=True)
    ap.add_argument(
        "--preset", choices=sorted(PRESETS), default="combined",
        help="combined=main Fig.8 eval; fig8=legacy dir names; delay/loss=diagnostics only",
    )
    ap.add_argument("--baseline-dir", default=None, help="override preset baseline run folder name")
    ap.add_argument("--dynamic-dir", default=None, help="override preset dynamic run folder name")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--full-hi", type=float, default=220.0)
    args = ap.parse_args()

    session = args.session.resolve()
    if not session.is_dir():
        print(f"[error] session not found: {session}", file=sys.stderr)
        sys.exit(1)

    cfg = PRESETS[args.preset]
    out = args.out or (_REPO / "derived" / cfg["out_subdir"] / session.name)
    analyze_session(
        session,
        out.resolve(),
        title=cfg["title"],
        baseline_dir=args.baseline_dir or cfg["baseline_dir"],
        dynamic_dir=args.dynamic_dir or cfg["dynamic_dir"],
        file_prefix=cfg["file_prefix"],
        full_hi=args.full_hi,
    )


if __name__ == "__main__":
    main()
