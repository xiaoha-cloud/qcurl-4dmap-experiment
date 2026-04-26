#!/usr/bin/env python3
"""
Time-based evaluation (paper-style): throughput vs time with tc capacity as shaded bands.

- X-axis: seconds from the first ``path :* mean tp`` line in pull (same origin as parse_logs ``t``).
- Y-axis: total throughput = sum over paths of ``mean tp`` (Mbps) per timestamp; then optional
  rolling mean for readability.
- Background: regions from ``tc_bw_*.log`` (``tc_bw_steps.sh``): each step’s cap applies until the
  next step; labels show the shaped interface’s TBF rate (e.g. h1-eth0 or h2-eth1), not multipath sum.

This script does **not** use P1/P2/P3 phase bars. For per-window CSV stats, keep using
``route_a_evaluate.py``.

Usage
-----
    cd /path/to/qcurl-4dmap-experiment
    python3 scripts/analyze/throughput_timeline_eval.py \\
        --out derived/timeline_learn.png \\
        -r learn:logs_exp/log/vm_run_20260426_033221

    python3 scripts/analyze/throughput_timeline_eval.py \\
        -r learn:path_a -r T:path_b --out derived/compare.png --smooth 3
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO / "scripts" / "analyze"))

import parse_logs as pl  # noqa: E402

_RE_MEAN_TP = re.compile(
    r"^(?P<date>\d{4}/\d{2}/\d{2}) (?P<time>\d{2}:\d{2}:\d{2}) path :(?P<path>\d) mean tp: (?P<tp>[\d.]+)Mbps"
)


def load_total_tp_mbps_timeseries(pull_path: Path) -> pd.DataFrame:
    """
    Seconds from first mean-tp sample; one row per distinct t_sec with tp = sum over paths.
    Skips NaN / non-finite tp values.
    """
    rows: list[dict] = []
    t0: Optional[float] = None
    with open(pull_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _RE_MEAN_TP.match(line)
            if not m:
                continue
            try:
                tp = float(m["tp"])
            except ValueError:
                continue
            if not (tp == tp) or tp < 0:  # NaN
                continue
            dt = datetime.strptime(m["date"] + " " + m["time"], "%Y/%m/%d %H:%M:%S")
            ts = dt.timestamp()
            if t0 is None:
                t0 = ts
            t_sec = ts - t0
            rows.append({"t_sec": t_sec, "path": int(m["path"]), "tp_mbps": tp})
    if not rows:
        return pd.DataFrame(columns=["t_sec", "tp_mbps_total"])
    df = pd.DataFrame(rows)
    total = df.groupby("t_sec", as_index=False)["tp_mbps"].sum().rename(
        columns={"tp_mbps": "tp_mbps_total"}
    )
    return total.sort_values("t_sec").reset_index(drop=True)


def tc_capacity_regions(
    tc_bw_path: Path,
    pull_path: Path,
    t_max: float,
) -> list[tuple[float, float, float, str]]:
    """
    Return list of (t_start, t_end, bw_mbit, label) in pull time coordinates.
    Last region extends to t_max.
    """
    df = pl.tc_bw_with_pull_t(tc_bw_path, pull_path)
    if df.empty or df["t_pull"].isna().all():
        return []
    df = df.sort_values("at_sec").reset_index(drop=True)
    out: list[tuple[float, float, float, str]] = []
    n = len(df)
    for i in range(n):
        t0 = float(df.loc[i, "t_pull"])
        if t0 != t0:
            continue
        t1 = float(df.loc[i + 1, "t_pull"]) if i + 1 < n else float(t_max)
        bw = float(df.loc[i, "bw_mbit"])
        dev = str(df.loc[i, "dev"])
        label = f"{bw:.0f} Mbit/s ({dev})"
        if t1 <= t0:
            t1 = max(t1, t0 + 1e-3)
        out.append((t0, min(t1, t_max), bw, label))
    return out


def _parse_r(s: str) -> tuple[str, Path]:
    if ":" not in s:
        raise ValueError("expected LABEL:path")
    lab, p = s.split(":", 1)
    return lab.strip(), Path(p.strip()).resolve()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Throughput vs time + tc_bw shaded regions (no P1/P2 phases)."
    )
    ap.add_argument(
        "-r",
        action="append",
        dest="runs",
        default=[],
        help="label:/path/to/vm_run_dir (must contain pull_*.log and tc_bw_*.log)",
    )
    ap.add_argument("--out", type=Path, default=Path("derived/throughput_timeline.png"))
    ap.add_argument(
        "--smooth",
        type=int,
        default=0,
        help="rolling mean window in samples (0 = off). Try 3–5 for less noisy lines.",
    )
    ap.add_argument(
        "--title",
        default="Throughput (sum of per-path mean tp) vs time",
        help="figure title",
    )
    ap.add_argument("--dpi", type=int, default=150)
    args = ap.parse_args()
    if not args.runs:
        ap.error("at least one -r LABEL:dir")
    args.out = args.out.resolve()
    args.out.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(10, 4.5))
    color_cycle = plt.rcParams["axes.prop_cycle"].by_key()["color"]
    band_colors = ["#fff3cd", "#f8d7da", "#d4edda", "#cce5ff", "#e2e3e5"]

    parsed: list[tuple[str, Path, Path, Path, pd.DataFrame]] = []
    all_t_max = 0.0
    for rspec in args.runs:
        label, run_dir = _parse_r(rspec)
        if not run_dir.is_dir():
            ap.error(f"not a directory: {run_dir}")
        pull = next(run_dir.glob("pull_*.log"), None)
        tc = next(run_dir.glob("tc_bw_*.log"), None)
        if pull is None or tc is None:
            ap.error(f"need pull_*.log and tc_bw_*.log in {run_dir}")
        ts = load_total_tp_mbps_timeseries(pull)
        if ts.empty:
            ap.error(f"no 'path :* mean tp' lines in {pull}")
        t_max = float(ts["t_sec"].max())
        all_t_max = max(all_t_max, t_max)
        if args.smooth and args.smooth > 1:
            ts = ts.copy()
            ts["tp_mbps_total"] = (
                ts["tp_mbps_total"].rolling(window=args.smooth, min_periods=1).mean()
            )
        parsed.append((label, run_dir, pull, tc, ts))

    # Shaded capacity regions: use first run’s tc + pull for alignment, extend to global t_max
    _, _d0, pull0, tc0, _ = parsed[0]
    regions = tc_capacity_regions(tc0, pull0, all_t_max + 5.0)
    for i, (a, b, bw, _rlab) in enumerate(regions):
        c = band_colors[i % len(band_colors)]
        ax.axvspan(
            a,
            b,
            facecolor=c,
            edgecolor="none",
            alpha=0.45,
            zorder=0,
        )
    for a, b, bw, _t in regions:
        mid = 0.5 * (a + b)
        ax.text(
            mid,
            0.02,
            f"{bw:.0f} Mbps (shaped link)",
            transform=ax.get_xaxis_transform(),
            ha="center",
            va="bottom",
            fontsize=8,
            color="#555",
        )

    for run_idx, (label, _rd, _p, _t, ts) in enumerate(parsed):
        ax.plot(
            ts["t_sec"],
            ts["tp_mbps_total"],
            label=label,
            linewidth=1.2,
            color=color_cycle[run_idx % len(color_cycle)],
            zorder=2,
        )

    ax.set_xlabel("Time (s) from first throughput sample in pull log")
    ax.set_ylabel("Throughput (Mbps)")
    ax.set_title(args.title)
    ax.grid(True, alpha=0.3, zorder=1)
    ax.legend(loc="upper right")
    if all_t_max > 0:
        ax.set_xlim(0, max(all_t_max * 1.02, 1.0))
    fig.tight_layout()
    fig.savefig(args.out, dpi=args.dpi)
    plt.close(fig)

    # CSV: first -r’s raw series (+ optional same smoothing)
    first_label, _first_dir, pull0, _tc0, _ = parsed[0]
    ts0 = load_total_tp_mbps_timeseries(pull0)
    if not ts0.empty and args.smooth and args.smooth > 1:
        ts0 = ts0.copy()
        ts0["tp_mbps_total"] = (
            ts0["tp_mbps_total"].rolling(window=args.smooth, min_periods=1).mean()
        )
    csv_path = args.out.with_suffix(".csv")
    ts0["method"] = first_label
    ts0.to_csv(csv_path, index=False)
    print("Wrote", args.out, "and", csv_path)


if __name__ == "__main__":
    main()
