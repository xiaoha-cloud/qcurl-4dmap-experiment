#!/usr/bin/env python3
"""Plot Fig.7 Q-ACCeSS-T throughput over time.

This is a lightweight companion to the D/L evaluation plot.

Preferred source is pcap, when pcaps are retained. In pcap mode the script
counts every frame in each path capture and does not apply an IP direction
filter. If pcaps are not available, it falls back to the saved throughput CSVs
in each Fig.7 leg:

  fig7_baseline/throughput_all_down.csv
  fig7_baseline/throughput_pathA_down.csv
  fig7_baseline/throughput_pathB_down.csv
  fig7_qaccess_t_dynamic/throughput_all_down.csv
  fig7_qaccess_t_dynamic/throughput_pathA_down.csv
  fig7_qaccess_t_dynamic/throughput_pathB_down.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import subprocess
from pathlib import Path
from statistics import mean


def parse_windows(spec: str) -> list[tuple[float, float]]:
    windows: list[tuple[float, float]] = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        lo_s, hi_s = item.split(":", 1)
        lo = float(lo_s)
        hi = float(hi_s)
        if hi <= lo:
            raise ValueError(f"invalid window {item!r}: end must be greater than start")
        windows.append((lo, hi))
    if not windows:
        raise ValueError("no windows configured")
    return windows


def read_tp_csv(path: Path) -> dict[float, float]:
    if not path.exists():
        raise FileNotFoundError(path)
    out: dict[float, float] = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            out[float(row["elapsed_s"])] = float(row["throughput_mbps"])
    return out


def read_run(run_dir: Path) -> list[dict[str, float]]:
    total = read_tp_csv(run_dir / "throughput_all_down.csv")
    path_a = read_tp_csv(run_dir / "throughput_pathA_down.csv")
    path_b = read_tp_csv(run_dir / "throughput_pathB_down.csv")
    times = sorted(set(total) | set(path_a) | set(path_b))
    rows: list[dict[str, float]] = []
    for t in times:
        a = path_a.get(t, 0.0)
        b = path_b.get(t, 0.0)
        tot = total.get(t, a + b)
        share = (b / tot * 100.0) if tot > 0 else math.nan
        rows.append(
            {
                "time_s": t,
                "total_mbps": tot,
                "pathA_mbps": a,
                "pathB_mbps": b,
                "path_b_share_pct": share,
            }
        )
    return rows


def find_path_pcap(run_dir: Path, path_name: str) -> Path | None:
    pcap_dir = run_dir / "pcaps"
    if not pcap_dir.is_dir():
        return None
    matches = sorted(pcap_dir.glob(f"{path_name}_*.pcap")) + sorted(pcap_dir.glob(f"{path_name}_*.pcapng"))
    return matches[0] if matches else None


def read_pcap_all_frames(pcap: Path, tshark_bin: str, bin_seconds: float) -> dict[float, int]:
    proc = subprocess.run(
        [tshark_bin, "-r", str(pcap), "-T", "fields", "-e", "frame.time_epoch", "-e", "frame.len"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"tshark failed for {pcap}: {proc.stderr[:2000]}")
    first_ts: float | None = None
    bins: dict[float, int] = {}
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        ts = float(parts[0])
        frame_len = int(float(parts[1]))
        if first_ts is None:
            first_ts = ts
        rel = ts - first_ts
        b = math.floor(rel / bin_seconds) * bin_seconds
        bins[b] = bins.get(b, 0) + frame_len
    return bins


def read_run_from_pcaps(run_dir: Path, tshark_bin: str, bin_seconds: float) -> list[dict[str, float]]:
    pcap_a = find_path_pcap(run_dir, "pathA_h1")
    pcap_b = find_path_pcap(run_dir, "pathB_h1")
    if pcap_a is None or pcap_b is None:
        raise FileNotFoundError(f"missing pathA_h1/pathB_h1 pcaps under {run_dir / 'pcaps'}")
    bins_a = read_pcap_all_frames(pcap_a, tshark_bin, bin_seconds)
    bins_b = read_pcap_all_frames(pcap_b, tshark_bin, bin_seconds)
    observed_times = set(bins_a) | set(bins_b)
    if not observed_times:
        return []
    # Preserve fixed-duration window semantics: a one-second bin with no
    # captured frames is a real 0 Mbps observation, not a missing sample.
    max_index = int(math.floor(max(observed_times) / bin_seconds))
    times = [index * bin_seconds for index in range(max_index + 1)]
    rows: list[dict[str, float]] = []
    for t in times:
        a = bins_a.get(t, 0) * 8.0 / bin_seconds / 1_000_000.0
        b = bins_b.get(t, 0) * 8.0 / bin_seconds / 1_000_000.0
        total = a + b
        rows.append(
            {
                "time_s": t,
                "total_mbps": total,
                "pathA_mbps": a,
                "pathB_mbps": b,
                "path_b_share_pct": (b / total * 100.0) if total > 0 else math.nan,
            }
        )
    return rows


def read_run_auto(run_dir: Path, source: str, tshark_bin: str, bin_seconds: float) -> tuple[list[dict[str, float]], str]:
    if source in ("auto", "pcap"):
        if shutil.which(tshark_bin) is None:
            if source == "pcap":
                raise RuntimeError(f"tshark not found: {tshark_bin}")
        else:
            try:
                return read_run_from_pcaps(run_dir, tshark_bin, bin_seconds), "pcap_all_frames"
            except FileNotFoundError:
                if source == "pcap":
                    raise
    return read_run(run_dir), "saved_throughput_csv"


def read_evaluation_timeseries(path: Path) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
    """Read an existing evaluator time series without changing its data alignment."""
    by_method: dict[str, list[dict[str, float]]] = {"baseline": [], "qaccess_t": []}
    with path.open(newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            method = str(raw.get("method", "")).strip().lower()
            if method not in by_method:
                continue
            by_method[method].append({
                "time_s": float(raw["time_s"]),
                "pathA_mbps": float(raw["pathA_mbps"]),
                "pathB_mbps": float(raw["pathB_mbps"]),
                "total_mbps": float(raw["total_mbps"]),
                "path_b_share_pct": (
                    float(raw["path_b_share_pct"])
                    if raw.get("path_b_share_pct") not in (None, "") else math.nan
                ),
            })
    if not by_method["baseline"] or not by_method["qaccess_t"]:
        raise ValueError(f"missing baseline or qaccess_t rows in {path}")
    return by_method["baseline"], by_method["qaccess_t"]


def mean_in_window(rows: list[dict[str, float]], key: str, lo: float, hi: float) -> float:
    vals = [r[key] for r in rows if lo <= r["time_s"] < hi and not math.isnan(r[key])]
    return mean(vals) if vals else math.nan


def pct_change(new: float, old: float) -> float:
    if old == 0 or math.isnan(old) or math.isnan(new):
        return math.nan
    return (new - old) / old * 100.0


def find_qaccess_dir(session: Path) -> Path:
    for name in ("fig7_qaccess_t_dynamic", "fig7_qaccess_t", "no_deterioration_qaccess_t"):
        path = session / name
        if path.is_dir():
            return path
    raise FileNotFoundError("missing fig7_qaccess_t_dynamic or fig7_qaccess_t directory")


def find_baseline_dir(session: Path) -> Path:
    for name in ("fig7_baseline", "no_deterioration_baseline"):
        path = session / name
        if path.is_dir():
            return path
    raise FileNotFoundError("missing fig7_baseline or no_deterioration_baseline directory")


def load_capacity_profile(session: Path) -> list[dict[str, float]]:
    path = session / "fig7_capacity_hybrid_comparison.json"
    if not path.exists():
        return [
            {"start_s": 0.0, "end_s": 50.0, "path_a_capacity_mbps": 20.0, "path_b_capacity_mbps": 20.0},
            {"start_s": 50.0, "end_s": 100.0, "path_a_capacity_mbps": 20.0, "path_b_capacity_mbps": 30.0},
            {"start_s": 100.0, "end_s": 200.0, "path_a_capacity_mbps": 20.0, "path_b_capacity_mbps": 10.0},
        ]
    with path.open() as f:
        data = json.load(f)
    return list(data.get("capacity_profile", []))


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def build_window_rows(
    baseline: list[dict[str, float]],
    qaccess: list[dict[str, float]],
    windows: list[tuple[float, float]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    window_rows: list[dict[str, object]] = []
    compare_rows: list[dict[str, object]] = []
    by_method = {"baseline": baseline, "qaccess_t": qaccess}
    for method, rows in by_method.items():
        for lo, hi in windows:
            window_rows.append(
                {
                    "method": method,
                    "window": f"{lo:g}-{hi:g}",
                    "t_lo": lo,
                    "t_hi": hi,
                    "total_mbps_mean": mean_in_window(rows, "total_mbps", lo, hi),
                    "pathA_mbps_mean": mean_in_window(rows, "pathA_mbps", lo, hi),
                    "pathB_mbps_mean": mean_in_window(rows, "pathB_mbps", lo, hi),
                    "path_b_share_pct_mean": mean_in_window(rows, "path_b_share_pct", lo, hi),
                }
            )
    for lo, hi in windows:
        b_total = mean_in_window(baseline, "total_mbps", lo, hi)
        q_total = mean_in_window(qaccess, "total_mbps", lo, hi)
        b_a = mean_in_window(baseline, "pathA_mbps", lo, hi)
        q_a = mean_in_window(qaccess, "pathA_mbps", lo, hi)
        b_b = mean_in_window(baseline, "pathB_mbps", lo, hi)
        q_b = mean_in_window(qaccess, "pathB_mbps", lo, hi)
        compare_rows.append(
            {
                "window": f"{lo:g}-{hi:g}",
                "baseline_total_mbps_mean": b_total,
                "qaccess_t_total_mbps_mean": q_total,
                "total_delta_mbps": q_total - b_total,
                "total_improvement_pct": pct_change(q_total, b_total),
                "baseline_pathA_mbps_mean": b_a,
                "qaccess_t_pathA_mbps_mean": q_a,
                "pathA_delta_mbps": q_a - b_a,
                "baseline_pathB_mbps_mean": b_b,
                "qaccess_t_pathB_mbps_mean": q_b,
                "pathB_delta_mbps": q_b - b_b,
            }
        )
    return window_rows, compare_rows


def import_pyplot(out_dir: Path):
    mpl_dir = out_dir / ".mplconfig"
    cache_dir = out_dir / ".cache"
    mpl_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(mpl_dir))
    os.environ.setdefault("XDG_CACHE_HOME", str(cache_dir))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def plot(
    out_png: Path,
    baseline: list[dict[str, float]],
    qaccess: list[dict[str, float]],
    capacity_profile: list[dict[str, float]],
    highlight_window: tuple[float, float] | None = None,
    show_capacity_guides: bool = True,
    plot_end_s: float | None = None,
    title: str | None = None,
) -> None:
    plt = import_pyplot(out_png.parent)
    fig, axes = plt.subplots(3, 1, figsize=(12, 9), sharex=True)

    colors = {
        "baseline_total": "#0072B2",
        "qaccess_total": "#D55E00",
        "baseline_path_a": "#009E73",
        "baseline_path_b": "#CC79A7",
        "qaccess_path_a": "#E69F00",
        "qaccess_path_b": "#56B4E9",
    }

    def xs(rows: list[dict[str, float]]) -> list[float]:
        return [r["time_s"] for r in rows]

    def ys(rows: list[dict[str, float]], key: str) -> list[float]:
        return [r[key] for r in rows]

    if highlight_window is not None:
        highlight_lo, highlight_hi = highlight_window
        for ax in axes:
            ax.axvspan(highlight_lo, highlight_hi, color="tab:red", alpha=0.08)

    if show_capacity_guides:
        for step in capacity_profile:
            lo = float(step["start_s"])
            hi = float(step["end_s"])
            path_a_cap = float(step.get("path_a_capacity_mbps", 0.0))
            path_b_cap = float(step.get("path_b_capacity_mbps", 0.0))
            axes[0].hlines(path_a_cap + path_b_cap, lo, hi, colors="0.65", linestyles="dashed", linewidth=1.2)
            axes[1].hlines(path_a_cap, lo, hi, colors="C2", linestyles="dashed", linewidth=1.0)
            axes[1].hlines(path_b_cap, lo, hi, colors="C3", linestyles="dashed", linewidth=1.0)
            for ax in axes:
                ax.axvline(lo, color="0.8", linestyle=":", linewidth=0.9)
        if capacity_profile:
            for ax in axes:
                ax.axvline(float(capacity_profile[-1]["end_s"]), color="0.8", linestyle=":", linewidth=0.9)

    axes[0].plot(
        xs(baseline), ys(baseline, "total_mbps"), label="Baseline total",
        linewidth=1.8, color=colors["baseline_total"],
    )
    axes[0].plot(
        xs(qaccess), ys(qaccess, "total_mbps"), label="Q-ACCeSS-T total",
        linewidth=1.8, color=colors["qaccess_total"],
    )
    axes[0].set_ylabel("Total throughput (Mbps)")
    axes[0].legend(loc="upper left")

    axes[1].plot(
        xs(baseline), ys(baseline, "pathA_mbps"), label="Baseline Path A",
        linewidth=1.2, color=colors["baseline_path_a"],
    )
    axes[1].plot(
        xs(baseline), ys(baseline, "pathB_mbps"), label="Baseline Path B",
        linewidth=1.2, linestyle="--", color=colors["baseline_path_b"],
    )
    axes[1].plot(
        xs(qaccess), ys(qaccess, "pathA_mbps"), label="Q-ACCeSS-T Path A",
        linewidth=1.2, color=colors["qaccess_path_a"],
    )
    axes[1].plot(
        xs(qaccess), ys(qaccess, "pathB_mbps"), label="Q-ACCeSS-T Path B",
        linewidth=1.2, linestyle="--", color=colors["qaccess_path_b"],
    )
    axes[1].set_ylabel("Path throughput (Mbps)")
    axes[1].legend(loc="upper left", ncol=2)

    axes[2].plot(
        xs(baseline), ys(baseline, "pathB_mbps"), label="Baseline Path B",
        linewidth=1.4, color=colors["baseline_total"],
    )
    axes[2].plot(
        xs(qaccess), ys(qaccess, "pathB_mbps"), label="Q-ACCeSS-T Path B",
        linewidth=1.4, color=colors["qaccess_total"],
    )
    axes[2].set_ylabel("Path B throughput (Mbps)")
    axes[2].set_xlabel("Time (s)")
    axes[2].legend(loc="upper left")

    for ax in axes:
        ax.grid(True, alpha=0.25)
        if plot_end_s is not None:
            ax.set_xlim(0.0, plot_end_s)

    if title:
        fig.suptitle(title, fontsize=15)
        fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.97))
    else:
        fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=160)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description="Plot Q-ACCeSS-T Fig.7 throughput over time")
    ap.add_argument("--session", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--windows", default="0:50,50:100,100:200")
    ap.add_argument("--source", choices=("auto", "pcap", "csv"), default="auto")
    ap.add_argument("--tshark-bin", default="tshark")
    ap.add_argument("--bin-seconds", type=float, default=1.0)
    ap.add_argument(
        "--highlight-window", default="",
        help="optional START:END interval shown with one light-red background",
    )
    ap.add_argument(
        "--hide-capacity-guides", action="store_true",
        help="omit capacity reference lines and phase boundary markers",
    )
    ap.add_argument(
        "--plot-end", type=float, default=None,
        help="optional right edge of the plotted time range",
    )
    ap.add_argument("--title", default="", help="optional figure title")
    ap.add_argument(
        "--timeseries-csv", type=Path, default=None,
        help="reuse an existing method/time_s/pathA/pathB/total evaluator CSV",
    )
    ap.add_argument(
        "--output-name", default="fig7_throughput_over_time.png",
        help="output PNG filename inside --out",
    )
    args = ap.parse_args()

    session = args.session.resolve()
    out = args.out.resolve() if args.out else session / "evaluation_fig7_t"
    windows = parse_windows(args.windows)
    highlight_window = None
    if args.highlight_window:
        parsed_highlights = parse_windows(args.highlight_window)
        if len(parsed_highlights) != 1:
            raise ValueError("--highlight-window must contain exactly one START:END interval")
        highlight_window = parsed_highlights[0]

    if args.timeseries_csv is not None:
        timeseries_source = args.timeseries_csv.resolve()
        baseline, qaccess = read_evaluation_timeseries(timeseries_source)
        baseline_source = qaccess_source = f"existing_timeseries:{timeseries_source}"
    else:
        baseline_dir = find_baseline_dir(session)
        qaccess_dir = find_qaccess_dir(session)
        baseline, baseline_source = read_run_auto(baseline_dir, args.source, args.tshark_bin, args.bin_seconds)
        qaccess, qaccess_source = read_run_auto(qaccess_dir, args.source, args.tshark_bin, args.bin_seconds)
    capacity_profile = load_capacity_profile(session)

    timeseries_rows: list[dict[str, object]] = []
    for method, rows in (("baseline", baseline), ("qaccess_t", qaccess)):
        for row in rows:
            timeseries_rows.append({"method": method, **row})
    window_rows, compare_rows = build_window_rows(baseline, qaccess, windows)

    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "fig7_t_throughput_timeseries.csv", timeseries_rows)
    write_csv(out / "fig7_t_throughput_windows.csv", window_rows)
    write_csv(out / "fig7_t_baseline_vs_qaccess_improvement.csv", compare_rows)
    output_png = out / args.output_name
    plot(
        output_png,
        baseline,
        qaccess,
        capacity_profile,
        highlight_window=highlight_window,
        show_capacity_guides=not args.hide_capacity_guides,
        plot_end_s=args.plot_end,
        title=args.title or None,
    )

    with (out / "fig7_t_evaluation_metadata.json").open("w") as f:
        json.dump(
            {
                "session": str(session),
                "baseline_source": baseline_source,
                "qaccess_t_source": qaccess_source,
                "source_note": (
                    "pcap_all_frames counts every frame in each path pcap without direction filtering; "
                    "saved_throughput_csv uses the throughput CSVs already present in the session."
                ),
                "windows": [{"start_s": lo, "end_s": hi} for lo, hi in windows],
                "highlight_window": (
                    {"start_s": highlight_window[0], "end_s": highlight_window[1]}
                    if highlight_window is not None else None
                ),
                "capacity_guides_shown": not args.hide_capacity_guides,
                "plot_end_s": args.plot_end,
            },
            f,
            indent=2,
        )

    print(f"Session: {session}")
    print(f"Source:  baseline={baseline_source} qaccess_t={qaccess_source}")
    print(f"Throughput windows: {out / 'fig7_t_throughput_windows.csv'}")
    print(f"Comparison:         {out / 'fig7_t_baseline_vs_qaccess_improvement.csv'}")
    print(f"Time-series plot:   {output_png}")


if __name__ == "__main__":
    main()
