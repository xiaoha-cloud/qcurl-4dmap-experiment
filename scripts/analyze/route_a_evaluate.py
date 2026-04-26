#!/usr/bin/env python3
"""
Route A: aggregate metrics from existing Mininet logs (no experiment runner).

- Parses pull_*.log + tc_bw_*.log via parse_logs.
- Per-phase steady windows: route_a_four_steady_windows (0/50/100s design, 4×50s in pull time).
- Writes CSVs under --out and optional matplotlib figures (Agg backend; safe headless).

This does **not** start mp_topo, sudo, or the client. Run experiments first, then point this
script at one directory per {baseline,T,D,L,learn,auto} (or multiple replicates per method).

Replicates: use -r T:path1,path2,path3 to get mean±std in summary_method_phase.

Metrics not always in logs: retransmissions, frame loss, playback — columns show NaN with note in CSV.

Usage
-----
    cd /path/to/qcurl-4dmap-experiment
    python3 scripts/analyze/route_a_evaluate.py --out /tmp/ra -r T:logs_exp/vm_run_A -r learn:logs_exp/vm_run_B

    python3 scripts/analyze/route_a_evaluate.py --out /tmp/ra --no-figures \\
        -r T:logs_exp/a,logs_exp/b -r learn:logs_exp/c
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# Import parse_logs (same package when run as script from repo root)
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO / "scripts" / "analyze"))

import parse_logs as pl  # noqa: E402

_RE_MEAN_TP = re.compile(
    r"^(?P<date>\d{4}/\d{2}/\d{2}) (?P<time>\d{2}:\d{2}:\d{2}) path :(?P<path>\d) mean tp: (?P<tp>[\d.]+)Mbps"
)


def _load_mean_tp_total_mbps(pull_path: Path) -> pd.DataFrame:
    """Per-second sum of per-path mean tp (Mbps) for rough goodput, if those lines exist."""
    rows: list[dict] = []
    t0: float | None = None
    with open(pull_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _RE_MEAN_TP.match(line)
            if not m:
                continue
            h = float(int(m["time"][:2]) * 3600 + int(m["time"][3:5]) * 60 + int(m["time"][6:8]))
            if t0 is None:
                t0 = h
            t = h - t0
            p = int(m["path"])
            tp = float(m["tp"])
            rows.append({"t": t, "path": p, "tp_mbps": tp})
    if not rows:
        return pd.DataFrame(columns=["t", "tp_mbps_sum"])
    df = pd.DataFrame(rows)
    return df.groupby("t", as_index=False)["tp_mbps"].sum().rename(columns={"tp_mbps": "tp_mbps_sum"})


def _phase_metrics(
    lo: float,
    hi: float,
    u: pd.DataFrame,
    m: pd.DataFrame,
    tp_total: pd.DataFrame,
) -> dict[str, Any]:
    uu = u[(u["t"] >= lo) & (u["t"] < hi)].copy()
    mm = m[(m["t"] >= lo) & (m["t"] < hi)].copy()
    tpt = (
        tp_total[(tp_total["t"] >= lo) & (tp_total["t"] < hi)]
        if not tp_total.empty
        else pd.DataFrame()
    )

    def _q(s: pd.Series) -> float:
        if s.empty or s.isna().all():
            return float("nan")
        return float(s.quantile(0.95))

    # Throughput: prefer summed mean-tp; else utility bw per timestamp
    tp_mbps = float("nan")
    if not tpt.empty and tpt["tp_mbps_sum"].notna().any():
        tp_mbps = float(tpt["tp_mbps_sum"].mean())
    elif not uu.empty and "bw_mbps" in uu.columns:
        s = uu.groupby("t")["bw_mbps"].sum()
        if not s.empty:
            tp_mbps = float(s.mean())

    return {
        "tp_mbps": tp_mbps,
        "owd_ms_mean": float(uu["owd_ms"].mean()) if "owd_ms" in uu and len(uu) else float("nan"),
        "owd_ms_p95": _q(uu["owd_ms"]) if "owd_ms" in uu else float("nan"),
        "rtt_ms_mean": float(mm["rtt_smoothed_ms"].mean()) if "rtt_smoothed_ms" in mm and len(mm) else float("nan"),
        "rtt_ms_p95": _q(mm["rtt_smoothed_ms"]) if "rtt_smoothed_ms" in mm else float("nan"),
        "loss_mean": float(uu["loss"].mean()) if "loss" in uu and len(uu) else float("nan"),
        "U_mean": float(uu["U"].mean()) if "U" in uu and len(uu) else float("nan"),
        "gain_mean": float(uu["gain"].mean()) if "gain" in uu and len(uu) else float("nan"),
        "backoff_mean": float(uu["backoff"].mean()) if "backoff" in uu and len(uu) else float("nan"),
        "mon_loss_mean": float(mm["loss"].mean()) if "loss" in mm and len(mm) else float("nan"),
        "cwnd_mean": float(mm["cwnd_full"].mean()) if "cwnd_full" in mm and len(mm) else float("nan"),
        "inflight_mean": float(mm["inflight"].mean()) if "inflight" in mm and len(mm) else float("nan"),
        "bw_bps_mon_mean": float((mm["bw_bytes"] * 8.0 / 1e6).mean()) if "bw_bytes" in mm and len(mm) else float("nan"),
        "n_util_rows": int(len(uu)),
        "n_mon_rows": int(len(mm)),
        "n_rows_mean_tp": int(len(tpt)) if not tpt.empty else 0,
    }


def _one_run(
    run_dir: Path, method: str, run_id: str
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    pull = next(run_dir.glob("pull_*.log"), None)
    tc = next(run_dir.glob("tc_bw_*.log"), None)
    if pull is None:
        raise FileNotFoundError(f"no pull_*.log in {run_dir}")
    if tc is None:
        raise FileNotFoundError(f"no tc_bw_*.log in {run_dir} (Route A needs dynamic bandwidth run)")

    u, m = pl.load_pull_log(pull, label=method)
    ph = pl.route_a_four_steady_windows(tc, pull, transition_sec=10.0, experiment_end_sec=200.0)
    if ph.empty:
        ph = pl.phase_steady_windows_from_tc_bw(tc, pull, transition_sec=10.0, experiment_end_sec=200.0)
    tpt = _load_mean_tp_total_mbps(pull)

    out_rows: list[dict] = []
    for _, pr in ph.iterrows():
        lo, hi = float(pr["t_steady_start"]), float(pr["t_steady_end"])
        met = _phase_metrics(lo, hi, u, m, tpt)
        met.update(
            {
                "method": method,
                "run_id": run_id,
                "dir": str(run_dir),
                "phase": int(pr["phase"]),
                "retransmissions": float("nan"),
                "frame_loss": float("nan"),
                "note": "retrans/frame not in log",
            }
        )
        if "tc_design_lo" in pr:
            met["tc_design_lo"] = float(pr["tc_design_lo"])
            met["tc_design_hi"] = float(pr["tc_design_hi"])
        out_rows.append(met)

    ts_u = u.sort_values("t") if not u.empty else u
    ts_m = m.sort_values("t") if not m.empty else m
    return pd.DataFrame(out_rows), ts_u, ts_m


def _parse_r_arg(s: str) -> tuple[str, list[Path]]:
    if ":" not in s:
        raise ValueError(f"expected METHOD:path or METHOD:p1,p2, got {s!r}")
    method, rest = s.split(":", 1)
    paths = [Path(p.strip()) for p in rest.split(",") if p.strip()]
    if not paths:
        raise ValueError("empty path list")
    return method, paths


def main() -> None:
    ap = argparse.ArgumentParser(description="Route A: summarize logs + optional figures")
    ap.add_argument("--out", type=Path, default=Path("derived/route_a_report"), help="output directory")
    ap.add_argument(
        "-r",
        action="append",
        dest="runs",
        default=[],
        help="method:one_dir or method:dir1,dir2 (replicates)",
    )
    ap.add_argument("--no-figures", action="store_true", help="skip matplotlib PNGs")
    ap.add_argument("--json-meta", action="store_true", help="write run_meta.json for provenance")
    args = ap.parse_args()

    if not args.runs:
        ap.error("provide at least one -r T:path")

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    (out / "figs").mkdir(exist_ok=True)

    all_phase: list[pd.DataFrame] = []
    meta: list[dict] = []

    for rspec in args.runs:
        method, paths = _parse_r_arg(rspec)
        for p in paths:
            if not p.is_dir():
                ap.error(f"not a directory: {p}")
            run_id = p.name
            dfp, ts_u, ts_m = _one_run(p, method, run_id)
            dfp["replicate_of"] = method
            all_phase.append(dfp)
            meta.append({"method": method, "run_id": run_id, "path": str(p)})

    combined = pd.concat(all_phase, ignore_index=True)
    combined.to_csv(out / "summary_by_run_phase.csv", index=False)

    # mean±std over replicates: group by method × phase
    skip = {"method", "run_id", "dir", "note", "replicate_of", "label"}
    num_cols = [
        c
        for c in combined.columns
        if c not in skip
        and c != "phase"
        and (
            str(combined[c].dtype).startswith("float")
            or str(combined[c].dtype).startswith("int")
        )
    ]
    part: list[dict] = []
    for (m, ph), sub in combined.groupby(["method", "phase"]):
        row: dict = {"method": m, "phase": int(ph)}
        for c in num_cols:
            s = sub[c].dropna()
            row[c + "_mean"] = float(s.mean()) if len(s) else float("nan")
            row[c + "_std"] = float(s.std(ddof=0)) if len(s) > 1 else 0.0
        part.append(row)
    mpm = pd.DataFrame(part).sort_values(["method", "phase"])
    mpm.to_csv(out / "summary_method_phase_meanstd.csv", index=False)

    if args.json_meta:
        (out / "run_meta.json").write_text(json.dumps({"runs": meta}, indent=2), encoding="utf-8")

    if args.no_figures:
        print("Wrote:", out / "summary_by_run_phase.csv", out / "summary_method_phase_meanstd.csv")
        return

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    methods = sorted(mpm["method"].unique())
    phases = sorted(mpm["phase"].unique().tolist())
    for metric, title, fname in [
        ("tp_mbps_mean", "Throughput (proxy, Mbps, phase mean of steady window)", "bar_tp_mbps.png"),
        ("owd_ms_mean", "Mean OWD (ms)", "bar_owd_mean.png"),
        ("rtt_ms_mean", "Mean RTT smoothed (ms)", "bar_rtt_mean.png"),
        ("loss_mean", "Loss (utility, mean)", "bar_loss_utility.png"),
    ]:
        if metric not in mpm.columns:
            continue
        base = metric.replace("_mean", "")
        err_col = base + "_std"
        x = np.arange(len(phases))
        w = 0.8 / max(len(methods), 1)
        fig, ax = plt.subplots(figsize=(8, 4))
        for i, meth in enumerate(methods):
            sub = mpm[mpm["method"] == meth].set_index("phase")
            ys = [float(sub.loc[p, metric]) if p in sub.index and metric in sub.columns else float("nan") for p in phases]
            yerr = None
            if err_col in mpm.columns and metric.endswith("_mean"):
                yerr = [
                    float(sub.loc[p, err_col]) if p in sub.index and err_col in sub.columns else 0.0
                    for p in phases
                ]
            offset = (i - len(methods) / 2) * w + w / 2
            if yerr is not None:
                ax.bar(x + offset, ys, width=w, label=meth, yerr=yerr, capsize=2)
            else:
                ax.bar(x + offset, ys, width=w, label=meth)
        ax.set_xticks(x, [f"P{p}" for p in phases])
        ax.set_title(title)
        ax.legend()
        fig.tight_layout()
        fig.savefig(out / "figs" / fname, dpi=150)
        plt.close(fig)

    for rspec in args.runs:
        method, paths = _parse_r_arg(rspec)
        p0 = paths[0]
        if not p0.is_dir():
            continue
        try:
            _, ts_u, ts_m = _one_run(p0, method, p0.name)
        except (FileNotFoundError, OSError):
            continue
        if ts_u.empty and ts_m.empty:
            continue
        fig, axs = plt.subplots(5, 1, figsize=(10, 12), sharex=True)
        if not ts_u.empty:
            if "U" in ts_u.columns:
                axs[0].plot(ts_u["t"], ts_u["U"], label="U", linewidth=0.8)
            if "owd_ms" in ts_u.columns:
                axs[1].plot(ts_u["t"], ts_u["owd_ms"], label="owd", linewidth=0.8)
            if "loss" in ts_u.columns:
                axs[2].plot(ts_u["t"], ts_u["loss"], label="loss (util)", linewidth=0.8)
            if "gain" in ts_u.columns:
                axs[3].plot(ts_u["t"], ts_u["gain"], label="gain", linewidth=0.8, alpha=0.7)
            if "backoff" in ts_u.columns:
                axs[3].plot(ts_u["t"], ts_u["backoff"], label="backoff", linewidth=0.8, alpha=0.7)
        if not ts_m.empty and "rtt_smoothed_ms" in ts_m.columns:
            axs[4].plot(ts_m["t"], ts_m["rtt_smoothed_ms"], label="rtt", linewidth=0.8, color="C2")
        for ax, title in zip(
            axs, ["U", "OWD (ms)", "Loss (util)", "gain / backoff", "RTT smoothed (ms)"]
        ):
            ax.set_title(f"{method} | {title}")
            ax.grid(True, alpha=0.3)
        axs[0].legend(loc="upper right", fontsize=7)
        axs[3].legend(loc="upper right", fontsize=7)
        fig.tight_layout()
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", method)
        fig.savefig(out / "figs" / f"timeseries_{safe}.png", dpi=150)
        plt.close(fig)

    print("Wrote report under", out)


if __name__ == "__main__":
    main()
