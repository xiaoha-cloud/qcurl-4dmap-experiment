"""
plot_phase2.py — 4-panel comparison plot for Phase 2 (baseline / delay / loss).

Usage (from repo root):
    python3 scripts/analyze/plot_phase2.py \
        --baseline logs_exp/vm_run_BASELINE \
        --delay    logs_exp/vm_run_DELAY \
        --loss     logs_exp/vm_run_LOSS \
        --out      figures/phase2_comparison.pdf

Or single-run mode (generates a summary for one experiment):
    python3 scripts/analyze/plot_phase2.py --single logs_exp/vm_run_20260402_161105
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Union

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from parse_logs import load_pull_log, load_tc_log, load_phase2_triple

# ── palette ──────────────────────────────────────────────────────────────────
COLORS = {
    "baseline": "#2196F3",
    "delay":    "#FF5722",
    "loss":     "#4CAF50",
}
LINE_STYLES = {
    "baseline": "-",
    "delay":    "--",
    "loss":     "-.",
}
PATH_MARKERS = {0: "o", 1: "s", 3: "^"}
PATH_NAMES = {0: "path 0", 1: "path 1", 3: "path 3"}

SMOOTHING_WINDOW = 3  # seconds


def _smooth(series: pd.Series, win: int = SMOOTHING_WINDOW) -> pd.Series:
    return series.rolling(window=win, min_periods=1, center=True).mean()


def _draw_tc_steps(ax, tc_df: pd.DataFrame, col: str, color: str = "gray"):
    """Draw vertical lines at tc step times and annotate value."""
    if tc_df is None or tc_df.empty or col not in tc_df.columns:
        return
    for _, row in tc_df.iterrows():
        ax.axvline(x=row["at_sec"], color=color, linewidth=0.8, linestyle=":")
        val = row[col]
        if not np.isnan(val):
            ax.text(
                row["at_sec"] + 0.5, ax.get_ylim()[1] * 0.95,
                f"{val:.0f}",
                fontsize=7, color=color, rotation=90, va="top",
            )


def _resample_mean(df: pd.DataFrame, metric: str, path: int) -> pd.Series:
    sub = df[(df["path"] == path) & df[metric].notna()].copy()
    if sub.empty:
        return pd.Series(dtype=float)
    sub = sub.groupby("t")[metric].mean()
    return _smooth(sub)


# ── 4-panel figure ────────────────────────────────────────────────────────────

def plot_phase2(df_util: pd.DataFrame, df_mon: pd.DataFrame,
                tc_steps: dict, out_path: Union[str, Path],
                active_paths=None):
    """
    4-panel time-series figure:
      Panel 1: Bandwidth (Mbps) — from [utility]
      Panel 2: OWD / delay (ms) — from [utility]
      Panel 3: Utility U        — from [utility]
      Panel 4: Loss rate        — from [utility]
    """
    labels = df_util["label"].unique().tolist()

    if active_paths is None:
        cnts = df_util.groupby("path")["bw_mbps"].sum()
        active_paths = cnts[cnts > 0].index.tolist()

    fig, axes = plt.subplots(4, 1, figsize=(10, 14), sharex=True)
    fig.suptitle("4D-MAP Phase 2: baseline vs delay-step vs loss-step", fontsize=13)

    metrics = [
        ("bw_mbps",  "Bandwidth (Mbps)", "Mbps"),
        ("owd_ms",   "One-Way Delay (ms)", "ms"),
        ("U",        "Utility U", ""),
        ("loss",     "Loss rate", ""),
    ]

    for ax, (metric, ylabel, unit) in zip(axes, metrics):
        for label in labels:
            sub = df_util[df_util["label"] == label]
            c = COLORS.get(label, "black")
            ls = LINE_STYLES.get(label, "-")
            for path in active_paths:
                s = _resample_mean(sub, metric, path)
                if s.empty:
                    continue
                path_name = PATH_NAMES.get(path, f"p{path}")
                ax.plot(
                    s.index, s.values,
                    color=c, linestyle=ls, linewidth=1.2,
                    label=f"{label} {path_name}",
                    alpha=0.85,
                )

        # tc step markers
        if "delay" in tc_steps:
            _draw_tc_steps(ax, tc_steps["delay"], "delay_ms", color="#FF5722")
        if "loss" in tc_steps:
            _draw_tc_steps(ax, tc_steps["loss"], "loss_pct", color="#4CAF50")

        ax.set_ylabel(ylabel, fontsize=9)
        ax.grid(True, linewidth=0.4, alpha=0.5)
        ax.legend(fontsize=7, loc="upper right", ncol=2)

    axes[-1].set_xlabel("Time (s)", fontsize=10)

    plt.tight_layout(rect=[0, 0, 1, 0.97])
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"[plot] saved → {out_path}")
    plt.close()


# ── utility decomposition figure (G / D / L components) ──────────────────────

def plot_utility_components(df_util: pd.DataFrame, out_path: Union[str, Path],
                             active_paths=None):
    """
    Stacked bar / line chart showing G, D, L components per label.
    One row per label, 3 sub-panels (one per utility component).
    """
    labels = df_util["label"].unique().tolist()
    if active_paths is None:
        cnts = df_util.groupby("path")["bw_mbps"].sum()
        active_paths = cnts[cnts > 0].index.tolist()

    fig, axes = plt.subplots(3, 1, figsize=(10, 10), sharex=True)
    fig.suptitle("Utility decomposition: G (throughput), D (delay), L (loss)", fontsize=12)

    for ax, (comp, title) in zip(axes, [("G", "G – throughput"), ("D", "D – delay"), ("L", "L – loss")]):
        for label in labels:
            sub = df_util[df_util["label"] == label]
            c = COLORS.get(label, "black")
            ls = LINE_STYLES.get(label, "-")
            for path in active_paths:
                s = _resample_mean(sub, comp, path)
                if s.empty:
                    continue
                ax.plot(s.index, s.values, color=c, linestyle=ls,
                        linewidth=1.2, label=f"{label} p{path}", alpha=0.85)
        ax.set_ylabel(title, fontsize=9)
        ax.legend(fontsize=7, loc="upper right", ncol=2)
        ax.grid(True, linewidth=0.4, alpha=0.5)

    axes[-1].set_xlabel("Time (s)", fontsize=10)
    plt.tight_layout(rect=[0, 0, 1, 0.97])
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"[plot] saved → {out_path}")
    plt.close()


# ── single-run quick summary ──────────────────────────────────────────────────

def plot_single_run(run_dir: Union[str, Path], out_dir: Union[str, Path] = "figures"):
    run_dir = Path(run_dir)
    run_id = run_dir.name
    pull = next(run_dir.glob("pull_*.log"), None)
    if pull is None:
        print(f"[error] no pull_*.log in {run_dir}")
        return
    tc_delay = next(run_dir.glob("tc_delay_*.log"), None)
    tc_loss = next(run_dir.glob("tc_loss_*.log"), None)

    if tc_delay:
        label = "delay"
    elif tc_loss:
        label = "loss"
    else:
        label = "baseline"

    print(f"[parse] {pull} (label={label})")
    df_util, df_mon = load_pull_log(pull, label=label)

    tc_steps = {}
    if tc_delay:
        tc_steps["delay"] = load_tc_log(tc_delay)
    if tc_loss:
        tc_steps["loss"] = load_tc_log(tc_loss)

    fig, axes = plt.subplots(4, 1, figsize=(10, 12), sharex=True)
    fig.suptitle(f"Single run: {run_id} [{label}]", fontsize=12)

    metrics = [
        ("bw_mbps",  "Bandwidth (Mbps)"),
        ("owd_ms",   "OWD (ms)"),
        ("U",        "Utility U"),
        ("loss",     "Loss rate"),
    ]

    paths = sorted(df_util["path"].unique())
    pal = plt.cm.tab10.colors

    for ax, (metric, ylabel) in zip(axes, metrics):
        for i, path in enumerate(paths):
            s = _resample_mean(df_util, metric, path)
            if s.empty:
                continue
            ax.plot(s.index, s.values, color=pal[i % 10],
                    label=PATH_NAMES.get(path, f"p{path}"), linewidth=1.2)
        if "delay" in tc_steps:
            _draw_tc_steps(ax, tc_steps["delay"], "delay_ms")
        if "loss" in tc_steps:
            _draw_tc_steps(ax, tc_steps["loss"], "loss_pct")
        ax.set_ylabel(ylabel, fontsize=9)
        ax.grid(True, linewidth=0.4, alpha=0.5)
        ax.legend(fontsize=8)

    axes[-1].set_xlabel("Time (s)", fontsize=10)
    plt.tight_layout(rect=[0, 0, 1, 0.97])
    out = Path(out_dir) / f"{run_id}_summary.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out, dpi=150, bbox_inches="tight")
    print(f"[plot] saved → {out}")
    plt.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Plot Phase 2 comparisons")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--single",   help="Single run dir → quick summary")
    mode.add_argument("--baseline", help="Baseline run dir (no dynamic tc)")
    ap.add_argument("--delay",  help="Delay-step run dir")
    ap.add_argument("--loss",   help="Loss-step run dir")
    ap.add_argument("--out",    default="figures/phase2_comparison.pdf")
    ap.add_argument("--out-components", default="figures/phase2_components.pdf")
    args = ap.parse_args()

    if args.single:
        plot_single_run(args.single)
        return

    if not args.delay or not args.loss:
        ap.error("--delay and --loss are required with --baseline")

    print("[parse] loading three runs …")
    df_util, df_mon, tc_steps = load_phase2_triple(args.baseline, args.delay, args.loss)
    print(f"  utility rows: {len(df_util)}  monitor rows: {len(df_mon)}")

    plot_phase2(df_util, df_mon, tc_steps, args.out)
    plot_utility_components(df_util, args.out_components)


if __name__ == "__main__":
    main()
