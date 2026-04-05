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
from typing import Optional, Union

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from parse_logs import (
    estimate_tc_pull_offset_seconds,
    load_pull_log,
    load_tc_log,
    load_phase2_triple,
    load_labeled_vm_runs,
)

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

# tc verticals (do not use data line COLORS["loss"] green — loss steps are neutral gray)
TC_DELAY_VLINE_COLOR = "#E64A19"
TC_LOSS_VLINE_COLOR = "#757575"

# Utility modes T / D / L (ACCeSS-style QoS comparison for MPQUIC)
COLORS_TDL = {"T": "#B71C1C", "D": "#0D47A1", "L": "#1B5E20"}
LINE_STYLES_TDL = {"T": "-", "D": "--", "L": "-."}

SMOOTHING_WINDOW = 3  # seconds


def _smooth(series: pd.Series, win: int = SMOOTHING_WINDOW) -> pd.Series:
    return series.rolling(window=win, min_periods=1, center=True).mean()


def _draw_tc_steps(
    ax,
    tc_df: pd.DataFrame,
    col: str,
    color: str = "gray",
    offset_sec: float = 0.0,
):
    """Draw vertical lines at tc step times (x = at_sec + offset_sec) and annotate value."""
    if tc_df is None or tc_df.empty or col not in tc_df.columns:
        return
    ymin, ymax = ax.get_ylim()
    for _, row in tc_df.iterrows():
        x = float(row["at_sec"]) + offset_sec
        ax.axvline(x=x, color=color, linewidth=0.8, linestyle=":")
        val = row[col]
        if not np.isnan(val):
            if col == "loss_pct":
                txt = f"{val:g}%"
            else:
                txt = f"{val:.0f}ms"
            ax.text(
                x + 0.5,
                ymin + (ymax - ymin) * 0.94,
                txt,
                fontsize=7,
                color=color,
                rotation=90,
                va="top",
            )


def _resample_mean(df: pd.DataFrame, metric: str, path: int) -> pd.Series:
    sub = df[(df["path"] == path) & df[metric].notna()].copy()
    if sub.empty:
        return pd.Series(dtype=float)
    sub = sub.groupby("t")[metric].mean()
    return _smooth(sub)


# ── 4-panel figure ────────────────────────────────────────────────────────────

def plot_phase2(
    df_util: pd.DataFrame,
    df_mon: pd.DataFrame,
    tc_steps: dict,
    out_path: Union[str, Path],
    active_paths=None,
    tc_offsets: Optional[dict] = None,
):
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

    tc_offsets = tc_offsets or {"delay": 0.0, "loss": 0.0}

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

        # tc step markers: delay profile only on OWD; loss profile only on loss (aligned to pull t)
        if metric == "owd_ms" and "delay" in tc_steps:
            _draw_tc_steps(
                ax,
                tc_steps["delay"],
                "delay_ms",
                color=TC_DELAY_VLINE_COLOR,
                offset_sec=float(tc_offsets.get("delay", 0.0)),
            )
        if metric == "loss" and "loss" in tc_steps:
            _draw_tc_steps(
                ax,
                tc_steps["loss"],
                "loss_pct",
                color=TC_LOSS_VLINE_COLOR,
                offset_sec=float(tc_offsets.get("loss", 0.0)),
            )

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

def plot_utility_components(
    df_util: pd.DataFrame,
    out_path: Union[str, Path],
    active_paths=None,
    tc_steps: Optional[dict] = None,
    tc_offsets: Optional[dict] = None,
):
    """
    Stacked bar / line chart showing G, D, L components per label.
    One row per label, 3 sub-panels (one per utility component).
    """
    labels = df_util["label"].unique().tolist()
    if active_paths is None:
        cnts = df_util.groupby("path")["bw_mbps"].sum()
        active_paths = cnts[cnts > 0].index.tolist()

    tc_steps = tc_steps or {}
    tc_offsets = tc_offsets or {"delay": 0.0, "loss": 0.0}

    fig, axes = plt.subplots(3, 1, figsize=(10, 10), sharex=True)
    fig.suptitle("Utility decomposition: G (throughput), D (delay), L (loss)", fontsize=12)

    for ax, (comp, title) in zip(
        axes,
        [("G", "G – throughput"), ("D", "D – delay"), ("L", "L – loss")],
    ):
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
        if comp == "D" and "delay" in tc_steps:
            _draw_tc_steps(
                ax,
                tc_steps["delay"],
                "delay_ms",
                color=TC_DELAY_VLINE_COLOR,
                offset_sec=float(tc_offsets.get("delay", 0.0)),
            )
        if comp == "L" and "loss" in tc_steps:
            _draw_tc_steps(
                ax,
                tc_steps["loss"],
                "loss_pct",
                color=TC_LOSS_VLINE_COLOR,
                offset_sec=float(tc_offsets.get("loss", 0.0)),
            )
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


def _aggregate_metric_per_label(
    df_util: pd.DataFrame,
    label: str,
    metric: str,
    across_paths: str,
) -> pd.Series:
    """
    Collapse (t, path) duplicate seconds to one value per path, then aggregate across paths.

    across_paths: 'sum' | 'mean' | 'max'
    """
    sub = df_util[df_util["label"] == label]
    if sub.empty:
        return pd.Series(dtype=float)
    g = sub.groupby(["t", "path"], as_index=False)[metric].mean()
    if across_paths == "sum":
        return g.groupby("t")[metric].sum()
    if across_paths == "mean":
        return g.groupby("t")[metric].mean()
    if across_paths == "max":
        return g.groupby("t")[metric].max()
    raise ValueError(f"unknown across_paths={across_paths}")


def plot_tdl_access_style(
    df_util: pd.DataFrame,
    tc_steps: dict,
    out_path: Union[str, Path],
    tc_offsets: Optional[dict] = None,
    title: str = "MPQUIC: utility modes T vs D vs L (ACCeSS-style view)",
):
    """
    Four stacked panels (ACCeSS / IWQoS narrative applied to MPQUIC logs):

    1. Aggregate bandwidth (Mbps) — sum over paths (≈ multipath goodput proxy).
    2. Mean OWD (ms) — mean over paths.
    3. Mean utility U — mean over paths.
    4. Max loss rate — max over paths (worst subpath).

    Vertical lines: ``tc_delay`` on panel 2; ``tc_loss`` on panel 4.
    """
    tc_offsets = tc_offsets or {"delay": 0.0, "loss": 0.0}
    labels = sorted(df_util["label"].unique())

    fig, axes = plt.subplots(4, 1, figsize=(10, 13), sharex=True)
    fig.suptitle(title, fontsize=12)

    panels = [
        ("bw_mbps", "Aggregate bandwidth (Mbps, Σ paths)", "sum"),
        ("owd_ms", "Mean OWD (ms, mean over paths)", "mean"),
        ("U", "Mean utility U (mean over paths)", "mean"),
        ("loss", "Loss rate (max over paths)", "max"),
    ]

    for ax, (metric, ylabel, agg_paths) in zip(axes, panels):
        for lbl in labels:
            s = _aggregate_metric_per_label(df_util, lbl, metric, agg_paths)
            if s.empty:
                continue
            s = _smooth(s)
            c = COLORS_TDL.get(lbl, COLORS.get(lbl, "black"))
            ls = LINE_STYLES_TDL.get(lbl, LINE_STYLES.get(lbl, "-"))
            ax.plot(
                s.index,
                s.values,
                color=c,
                linestyle=ls,
                linewidth=1.4,
                label=f"mode {lbl}",
                alpha=0.9,
            )

        if metric == "owd_ms" and tc_steps.get("delay") is not None and not tc_steps["delay"].empty:
            _draw_tc_steps(
                ax,
                tc_steps["delay"],
                "delay_ms",
                color=TC_DELAY_VLINE_COLOR,
                offset_sec=float(tc_offsets.get("delay", 0.0)),
            )
        if metric == "loss" and tc_steps.get("loss") is not None and not tc_steps["loss"].empty:
            _draw_tc_steps(
                ax,
                tc_steps["loss"],
                "loss_pct",
                color=TC_LOSS_VLINE_COLOR,
                offset_sec=float(tc_offsets.get("loss", 0.0)),
            )

        ax.set_ylabel(ylabel, fontsize=9)
        ax.grid(True, linewidth=0.4, alpha=0.5)
        ax.legend(fontsize=8, loc="upper right")

    axes[-1].set_xlabel("Time (s) — relative to first [utility] in each pull log", fontsize=10)
    plt.tight_layout(rect=[0, 0, 1, 0.97])
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"[plot_tdl] saved → {out_path}")
    plt.close()


def plot_tdl_utility_components(
    df_util: pd.DataFrame,
    tc_steps: dict,
    out_path: Union[str, Path],
    tc_offsets: Optional[dict] = None,
):
    """G / D / L normalized components for modes T, D, L."""
    tc_offsets = tc_offsets or {"delay": 0.0, "loss": 0.0}
    labels = sorted(df_util["label"].unique())

    fig, axes = plt.subplots(3, 1, figsize=(10, 9.5), sharex=True)
    fig.suptitle("Utility decomposition (G / D / L) — modes T vs D vs L", fontsize=11)

    for ax, (comp, ytitle) in zip(
        axes,
        [("G", "G (throughput norm)"), ("D", "D (delay norm)"), ("L", "L (loss norm)")],
    ):
        for lbl in labels:
            s = _aggregate_metric_per_label(df_util, lbl, comp, "mean")
            if s.empty:
                continue
            s = _smooth(s)
            c = COLORS_TDL.get(lbl, "black")
            ls = LINE_STYLES_TDL.get(lbl, "-")
            ax.plot(s.index, s.values, color=c, linestyle=ls, linewidth=1.2, label=f"mode {lbl}")

        if comp == "D" and tc_steps.get("delay") is not None and not tc_steps["delay"].empty:
            _draw_tc_steps(
                ax,
                tc_steps["delay"],
                "delay_ms",
                color=TC_DELAY_VLINE_COLOR,
                offset_sec=float(tc_offsets.get("delay", 0.0)),
            )
        if comp == "L" and tc_steps.get("loss") is not None and not tc_steps["loss"].empty:
            _draw_tc_steps(
                ax,
                tc_steps["loss"],
                "loss_pct",
                color=TC_LOSS_VLINE_COLOR,
                offset_sec=float(tc_offsets.get("loss", 0.0)),
            )
        ax.set_ylabel(ytitle, fontsize=9)
        ax.legend(fontsize=7, loc="upper right")
        ax.grid(True, linewidth=0.4, alpha=0.5)

    axes[-1].set_xlabel("Time (s)", fontsize=10)
    plt.tight_layout(rect=[0, 0, 1, 0.96])
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"[plot_tdl] saved → {out_path}")
    plt.close()


def summarize_tdl_modes(df_util: pd.DataFrame) -> pd.DataFrame:
    """Per-mode summary statistics (full trace)."""
    rows = []
    for lbl in sorted(df_util["label"].unique()):
        sub = df_util[df_util["label"] == lbl]
        rows.append({
            "mode": lbl,
            "n_rows": len(sub),
            "mean_bw_mbps": sub["bw_mbps"].mean(),
            "mean_owd_ms": sub["owd_ms"].mean(),
            "mean_loss": sub["loss"].mean(),
            "mean_U": sub["U"].mean(),
            "mean_gain": sub["gain"].mean(),
            "mean_backoff": sub["backoff"].mean(),
        })
    return pd.DataFrame(rows)


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

    off_d = off_l = 0.0
    if tc_delay:
        off_d = estimate_tc_pull_offset_seconds(pull, tc_delay)
    if tc_loss:
        off_l = estimate_tc_pull_offset_seconds(pull, tc_loss)

    for ax, (metric, ylabel) in zip(axes, metrics):
        for i, path in enumerate(paths):
            s = _resample_mean(df_util, metric, path)
            if s.empty:
                continue
            ax.plot(s.index, s.values, color=pal[i % 10],
                    label=PATH_NAMES.get(path, f"p{path}"), linewidth=1.2)
        if metric == "owd_ms" and "delay" in tc_steps:
            _draw_tc_steps(
                ax, tc_steps["delay"], "delay_ms",
                color=TC_DELAY_VLINE_COLOR, offset_sec=off_d,
            )
        if metric == "loss" and "loss" in tc_steps:
            _draw_tc_steps(
                ax, tc_steps["loss"], "loss_pct",
                color=TC_LOSS_VLINE_COLOR, offset_sec=off_l,
            )
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
    mode.add_argument(
        "--tdl",
        nargs=3,
        metavar=("T_RUN", "D_RUN", "L_RUN"),
        help="Three vm_run_* dirs (utility modes T, D, L) → ACCeSS-style figures",
    )
    ap.add_argument("--delay",  help="Delay-step run dir")
    ap.add_argument("--loss",   help="Loss-step run dir")
    ap.add_argument("--out",    default="figures/phase2_comparison.pdf")
    ap.add_argument("--out-components", default="figures/phase2_components.pdf")
    ap.add_argument("--out-tdl", default="figures/access_tdl_mpquic.pdf")
    ap.add_argument("--out-tdl-gdl", default="figures/access_tdl_GDL.pdf")
    args = ap.parse_args()

    if args.single:
        plot_single_run(args.single)
        return

    if args.tdl:
        t_dir, d_dir, l_dir = args.tdl
        print("[parse] T/D/L runs …")
        df_util, df_mon, tc_steps = load_labeled_vm_runs(
            {"T": t_dir, "D": d_dir, "L": l_dir},
        )
        print(f"  utility rows: {len(df_util)}  monitor rows: {len(df_mon)}")
        tc_offsets = {"delay": 0.0, "loss": 0.0}
        dirs_tdl = [Path(t_dir), Path(d_dir), Path(l_dir)]

        def _first_pull_tc_pair(glob_tc: str):
            for d in dirs_tdl:
                pulls = list(d.glob("pull_*.log"))
                tcs = list(d.glob(glob_tc))
                if pulls and tcs:
                    return pulls[0], tcs[0]
            return None, None

        if tc_steps.get("delay") is not None and not tc_steps["delay"].empty:
            pull_x, tc_d = _first_pull_tc_pair("tc_delay_*.log")
            if pull_x is not None:
                tc_offsets["delay"] = estimate_tc_pull_offset_seconds(pull_x, tc_d)
        if tc_steps.get("loss") is not None and not tc_steps["loss"].empty:
            pull_x, tc_l = _first_pull_tc_pair("tc_loss_*.log")
            if pull_x is not None:
                tc_offsets["loss"] = estimate_tc_pull_offset_seconds(pull_x, tc_l)
        print(f"  tc offsets (sec): {tc_offsets}")
        print(summarize_tdl_modes(df_util).to_string(index=False))
        plot_tdl_access_style(df_util, tc_steps, args.out_tdl, tc_offsets=tc_offsets)
        plot_tdl_utility_components(
            df_util, tc_steps, args.out_tdl_gdl, tc_offsets=tc_offsets,
        )
        return

    if not args.delay or not args.loss:
        ap.error("--delay and --loss are required with --baseline")

    print("[parse] loading three runs …")
    df_util, df_mon, tc_steps = load_phase2_triple(args.baseline, args.delay, args.loss)
    print(f"  utility rows: {len(df_util)}  monitor rows: {len(df_mon)}")

    dd, ld = Path(args.delay), Path(args.loss)
    pull_d = next(dd.glob("pull_*.log"))
    tc_d = next(dd.glob("tc_delay_*.log"))
    pull_l = next(ld.glob("pull_*.log"))
    tc_l = next(ld.glob("tc_loss_*.log"))
    tc_offsets = {
        "delay": estimate_tc_pull_offset_seconds(pull_d, tc_d),
        "loss": estimate_tc_pull_offset_seconds(pull_l, tc_l),
    }
    print(f"  tc offsets (sec): {tc_offsets}")

    plot_phase2(df_util, df_mon, tc_steps, args.out, tc_offsets=tc_offsets)
    plot_utility_components(
        df_util, args.out_components, tc_steps=tc_steps, tc_offsets=tc_offsets,
    )


if __name__ == "__main__":
    main()
