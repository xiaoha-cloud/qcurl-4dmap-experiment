#!/usr/bin/env python3
"""
Plot baseline vs Q-ACCeSS-T throughput comparison from qaccess_t_throughput_compare.py CSVs.

Inputs (default under derived/qaccess_t_compare/):
  throughput_timeseries_baseline.csv
  throughput_timeseries_qaccess_t.csv
  qaccess_t_throughput_windows.csv
  qaccess_t_improvement_vs_baseline.csv

Outputs:
  qaccess_t_throughput_timeseries.png
  qaccess_t_window_throughput.png
  qaccess_t_improvement.png
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
DEFAULT_DIR = _REPO / "derived" / "qaccess_t_compare"

BW_STEP_SECONDS = (50, 100)


def _require_file(path: Path) -> None:
    if not path.is_file():
        print(f"[error] missing input: {path}", file=sys.stderr)
        sys.exit(1)


def plot_timeseries(
    baseline: pd.DataFrame,
    qaccess_t: pd.DataFrame,
    out_path: Path,
) -> None:
    fig, ax = plt.subplots(figsize=(12, 5))

    ax.plot(
        baseline["time_s"],
        baseline["tp_mbps"],
        label="baseline",
        linewidth=1.2,
    )
    ax.plot(
        qaccess_t["time_s"],
        qaccess_t["tp_mbps"],
        label="qaccess_t",
        linewidth=1.2,
    )

    for t in BW_STEP_SECONDS:
        ax.axvline(t, linestyle="--", linewidth=1, color="gray")

    ax.set_xlabel("time_s")
    ax.set_ylabel("tp_mbps")
    ax.set_title("Baseline vs Q-ACCeSS-T Throughput Over Time")
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"[plot_qaccess_t_compare] wrote {out_path}")


def plot_window_throughput(windows: pd.DataFrame, out_path: Path) -> None:
    order = windows.loc[windows["method"] == "baseline", "window"].tolist()
    if not order:
        order = list(dict.fromkeys(windows["window"].tolist()))

    base = (
        windows[windows["method"] == "baseline"]
        .set_index("window")["tp_mbps_mean"]
        .reindex(order)
    )
    qat = (
        windows[windows["method"] == "qaccess_t"]
        .set_index("window")["tp_mbps_mean"]
        .reindex(order)
    )

    x = range(len(order))
    width = 0.36

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.bar(
        [i - width / 2 for i in x],
        base.values,
        width=width,
        label="baseline",
    )
    ax.bar(
        [i + width / 2 for i in x],
        qat.values,
        width=width,
        label="qaccess_t",
    )

    ax.set_xticks(list(x))
    ax.set_xticklabels(order, rotation=25, ha="right")
    ax.set_xlabel("window")
    ax.set_ylabel("tp_mbps_mean")
    ax.set_title("Mean Throughput by Window")
    ax.legend(loc="best")
    ax.grid(True, axis="y", alpha=0.3)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"[plot_qaccess_t_compare] wrote {out_path}")


def plot_improvement(improvement: pd.DataFrame, out_path: Path) -> None:
    df = improvement.copy()
    if df.empty:
        print("[warn] improvement CSV is empty; skipping improvement plot", file=sys.stderr)
        return

    windows = df["window"].tolist()
    values = df["improvement_pct"].astype(float).tolist()

    fig, ax = plt.subplots(figsize=(10, 5))
    colors = ["#2ca02c" if v >= 0 else "#d62728" for v in values]
    ax.bar(windows, values, color=colors)
    ax.axhline(0.0, color="black", linewidth=0.8)

    ax.set_xlabel("window")
    ax.set_ylabel("improvement_pct")
    ax.set_title("Q-ACCeSS-T Improvement vs Baseline (%)")
    plt.setp(ax.get_xticklabels(), rotation=25, ha="right")
    ax.grid(True, axis="y", alpha=0.3)

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"[plot_qaccess_t_compare] wrote {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Plot Q-ACCeSS-T vs baseline throughput CSVs")
    ap.add_argument(
        "--dir",
        type=Path,
        default=DEFAULT_DIR,
        help="Directory containing compare CSV inputs and PNG outputs",
    )
    args = ap.parse_args()

    data_dir = args.dir.resolve()
    ts_base = data_dir / "throughput_timeseries_baseline.csv"
    ts_qat = data_dir / "throughput_timeseries_qaccess_t.csv"
    windows_csv = data_dir / "qaccess_t_throughput_windows.csv"
    improvement_csv = data_dir / "qaccess_t_improvement_vs_baseline.csv"

    out_ts = data_dir / "qaccess_t_throughput_timeseries.png"
    out_win = data_dir / "qaccess_t_window_throughput.png"
    out_imp = data_dir / "qaccess_t_improvement.png"

    for path in (ts_base, ts_qat, windows_csv, improvement_csv):
        _require_file(path)

    baseline = pd.read_csv(ts_base)
    qaccess_t = pd.read_csv(ts_qat)
    windows = pd.read_csv(windows_csv)
    improvement = pd.read_csv(improvement_csv)

    for name, df in (
        ("baseline timeseries", baseline),
        ("qaccess_t timeseries", qaccess_t),
    ):
        if "time_s" not in df.columns or "tp_mbps" not in df.columns:
            print(f"[error] {name} must have time_s and tp_mbps columns", file=sys.stderr)
            sys.exit(1)

    plot_timeseries(baseline, qaccess_t, out_ts)
    plot_window_throughput(windows, out_win)
    plot_improvement(improvement, out_imp)
    print("[plot_qaccess_t_compare] done.")


if __name__ == "__main__":
    main()
