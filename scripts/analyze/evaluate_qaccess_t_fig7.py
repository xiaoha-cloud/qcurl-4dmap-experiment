#!/usr/bin/env python3
"""
Evaluate a Fig.7 Q-ACCeSS-T session from pcap traffic.

Input session layout:
  session/
    fig7_baseline/pcaps/pathA_h1_*.pcap
    fig7_baseline/pcaps/pathB_h1_*.pcap
    fig7_qaccess_t/pcaps/pathA_h1_*.pcap
    fig7_qaccess_t/pcaps/pathB_h1_*.pcap

This script intentionally does not read controller logs or pull-log fallback data.
By default it evaluates downlink-only traffic. Use --traffic-scope all-udp to
match the older notebook-style "all UDP in each path pcap" view.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DOWNLINK_FILTERS = {
    "pathA": "udp && ip.src == 10.0.1.2 && ip.dst == 10.0.1.1",
    "pathB": "udp && ip.src == 10.0.2.2 && ip.dst == 10.0.2.1",
}

ALL_UDP_FILTERS = {
    "pathA": "udp",
    "pathB": "udp",
}

RUNS = {
    "baseline": ("baseline", "fig7_baseline"),
    "qaccess_t": ("qaccess_t", "fig7_qaccess_t"),
}

DEFAULT_WINDOWS = [(0.0, 50.0), (50.0, 100.0), (100.0, 200.0)]


def filters_for_scope(scope: str) -> dict[str, str]:
    if scope == "downlink":
        return DOWNLINK_FILTERS
    if scope == "all-udp":
        return ALL_UDP_FILTERS
    raise ValueError(f"unsupported traffic scope: {scope}")


def scope_label(scope: str) -> str:
    return "downlink" if scope == "downlink" else "all_udp"


def scope_description(scope: str) -> str:
    if scope == "downlink":
        return "pcap UDP downlink only"
    return "pcap all UDP packets per path capture"


@dataclass
class PathSeries:
    mode: str
    path_name: str
    pcap: Path
    rows: list[dict]
    packet_count: int
    byte_count: int


def parse_windows(spec: str) -> list[tuple[float, float]]:
    windows: list[tuple[float, float]] = []
    for part in spec.split(","):
        item = part.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(f"invalid window {item!r}; expected START:END")
        start_s, end_s = item.split(":", 1)
        lo = float(start_s)
        hi = float(end_s)
        if hi <= lo:
            raise ValueError(f"invalid window {item!r}; END must be greater than START")
        windows.append((lo, hi))
    if not windows:
        raise ValueError("at least one window is required")
    return windows


def bin_suffix(bin_size: float) -> str:
    if abs(bin_size - round(bin_size)) < 1e-9:
        return f"{int(round(bin_size))}s"
    return f"{bin_size:g}s".replace(".", "p")


def ensure_tshark(tshark_bin: str) -> None:
    if shutil.which(tshark_bin) is None:
        raise RuntimeError(
            f"tshark not found: {tshark_bin}. Install tshark on the VM and rerun."
        )


def find_one_pcap(run_dir: Path, pattern: str) -> Path:
    matches = sorted((run_dir / "pcaps").glob(pattern))
    matches = [p for p in matches if p.is_file()]
    if not matches:
        raise FileNotFoundError(f"missing pcap matching {run_dir / 'pcaps' / pattern}")
    if len(matches) > 1:
        # Deterministic: newest-looking path by lexical order, matching run timestamp naming.
        return matches[-1]
    return matches[0]


def read_filtered_packets(
    pcap: Path,
    display_filter: str,
    tshark_bin: str,
    commands_log: Path,
) -> list[tuple[float, int]]:
    cmd = [
        tshark_bin,
        "-r",
        str(pcap),
        "-Y",
        display_filter,
        "-T",
        "fields",
        "-E",
        "separator=\t",
        "-E",
        "occurrence=f",
        "-e",
        "frame.time_epoch",
        "-e",
        "frame.len",
    ]
    with commands_log.open("a", encoding="utf-8") as f:
        f.write(" ".join(cmd) + "\n")

    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"tshark failed for {pcap}\nfilter: {display_filter}\n{proc.stderr[:2000]}"
        )

    packets: list[tuple[float, int]] = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 2:
            continue
        try:
            ts = float(parts[0])
            size = int(float(parts[1]))
        except ValueError:
            continue
        if size > 0:
            packets.append((ts, size))
    return packets


def aggregate_packets(
    packets: Iterable[tuple[float, int]],
    t0: float,
    bin_size: float,
) -> tuple[dict[int, int], int, int]:
    bins: dict[int, int] = defaultdict(int)
    packet_count = 0
    byte_count = 0
    for ts, size in packets:
        idx = int(math.floor((ts - t0) / bin_size))
        if idx < 0:
            continue
        bins[idx] += size
        packet_count += 1
        byte_count += size
    return dict(bins), packet_count, byte_count


def rows_from_bins(bins: dict[int, int], max_idx: int, bin_size: float) -> list[dict]:
    rows: list[dict] = []
    for idx in range(max_idx + 1):
        bytes_n = int(bins.get(idx, 0))
        bits = bytes_n * 8
        mbps = bits / 1_000_000.0 / bin_size
        rows.append(
            {
                "time_s": round(idx * bin_size, 6),
                "bytes": bytes_n,
                "bits": bits,
                "mbps": mbps,
            }
        )
    return rows


def capacity_phase(time_s: float) -> str:
    if time_s < 50:
        return "phase_1_20mbps"
    if time_s < 100:
        return "phase_2_30mbps"
    if time_s < 200:
        return "phase_3_10mbps"
    return "post_200s_10mbps"


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def mean(values: list[float]) -> float:
    if not values:
        return float("nan")
    return sum(values) / len(values)


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return float("nan")
    vals = sorted(values)
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * pct / 100.0
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    frac = pos - lo
    return vals[lo] * (1.0 - frac) + vals[hi] * frac


def stddev(values: list[float]) -> float:
    if len(values) <= 1:
        return 0.0 if values else float("nan")
    m = mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / len(values))


def window_rows(
    total_by_mode: dict[str, list[dict]],
    windows: list[tuple[float, float]],
) -> list[dict]:
    rows: list[dict] = []
    for mode, ts_rows in total_by_mode.items():
        for lo, hi in windows:
            sub = [r for r in ts_rows if lo <= float(r["time_s"]) < hi]
            path_a = [float(r["pathA_mbps"]) for r in sub]
            path_b = [float(r["pathB_mbps"]) for r in sub]
            total = [float(r["total_mbps"]) for r in sub]
            rows.append(
                {
                    "mode": mode,
                    "window_start_s": lo,
                    "window_end_s": hi,
                    "pathA_mean_mbps": mean(path_a),
                    "pathB_mean_mbps": mean(path_b),
                    "total_mean_mbps": mean(total),
                    "pathA_p95_mbps": percentile(path_a, 95),
                    "pathB_p95_mbps": percentile(path_b, 95),
                    "total_p95_mbps": percentile(total, 95),
                    "total_std_mbps": stddev(total),
                    "n_bins": len(sub),
                }
            )
    return rows


def comparison_rows(summary_rows: list[dict]) -> list[dict]:
    by_key: dict[tuple[float, float], dict[str, dict]] = defaultdict(dict)
    for row in summary_rows:
        key = (float(row["window_start_s"]), float(row["window_end_s"]))
        by_key[key][str(row["mode"])] = row

    rows: list[dict] = []
    for lo, hi in sorted(by_key):
        base = by_key[(lo, hi)].get("baseline")
        qacc = by_key[(lo, hi)].get("qaccess_t")
        if base is None or qacc is None:
            continue
        b = float(base["total_mean_mbps"])
        q = float(qacc["total_mean_mbps"])
        delta = q - b
        improvement = float("nan")
        if b > 0:
            improvement = delta / b * 100.0
        rows.append(
            {
                "window_start_s": lo,
                "window_end_s": hi,
                "baseline_total_mean_mbps": b,
                "qaccess_t_total_mean_mbps": q,
                "delta_mbps": delta,
                "improvement_pct": improvement,
            }
        )
    return rows


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


def add_phase_markers(plt) -> None:
    for x in (50, 100):
        plt.axvline(x, color="0.5", linestyle="--", linewidth=1)


def save_figures(
    fig_dir: Path,
    total_by_mode: dict[str, list[dict]],
    summary_rows: list[dict],
    traffic_scope: str,
) -> None:
    fig_dir.mkdir(parents=True, exist_ok=True)
    plt = import_pyplot(fig_dir.parent)

    def col(rows: list[dict], name: str) -> list[float]:
        return [float(r[name]) for r in rows]

    base = total_by_mode["baseline"]
    qacc = total_by_mode["qaccess_t"]
    ylabel = f"{scope_description(traffic_scope)} throughput Mbps"

    plt.figure(figsize=(12, 5))
    plt.plot(col(base, "time_s"), col(base, "total_mbps"), label="baseline total", linewidth=2)
    plt.plot(col(qacc, "time_s"), col(qacc, "total_mbps"), label="qaccess_t total", linewidth=2)
    add_phase_markers(plt)
    plt.xlabel("time_s")
    plt.ylabel(ylabel)
    plt.legend()
    plt.tight_layout()
    plt.savefig(fig_dir / "throughput_total_baseline_vs_qaccess_t.png", dpi=150)
    plt.close()

    for mode, rows, out_name in [
        ("baseline", base, "throughput_paths_baseline.png"),
        ("qaccess_t", qacc, "throughput_paths_qaccess_t.png"),
    ]:
        plt.figure(figsize=(12, 5))
        plt.plot(col(rows, "time_s"), col(rows, "pathA_mbps"), label=f"{mode} pathA")
        plt.plot(col(rows, "time_s"), col(rows, "pathB_mbps"), label=f"{mode} pathB")
        plt.plot(col(rows, "time_s"), col(rows, "total_mbps"), label=f"{mode} total", linewidth=2)
        add_phase_markers(plt)
        plt.xlabel("time_s")
        plt.ylabel(ylabel)
        plt.legend()
        plt.tight_layout()
        plt.savefig(fig_dir / out_name, dpi=150)
        plt.close()

    for path_col, out_name in [
        ("pathA_mbps", "throughput_pathA_baseline_vs_qaccess_t.png"),
        ("pathB_mbps", "throughput_pathB_baseline_vs_qaccess_t.png"),
    ]:
        plt.figure(figsize=(12, 5))
        plt.plot(col(base, "time_s"), col(base, path_col), label=f"baseline {path_col[:-5]}")
        plt.plot(col(qacc, "time_s"), col(qacc, path_col), label=f"qaccess_t {path_col[:-5]}")
        add_phase_markers(plt)
        plt.xlabel("time_s")
        plt.ylabel(ylabel)
        plt.legend()
        plt.tight_layout()
        plt.savefig(fig_dir / out_name, dpi=150)
        plt.close()

    labels = []
    baseline_vals = []
    qaccess_vals = []
    by_window: dict[tuple[float, float], dict[str, float]] = defaultdict(dict)
    for row in summary_rows:
        key = (float(row["window_start_s"]), float(row["window_end_s"]))
        by_window[key][str(row["mode"])] = float(row["total_mean_mbps"])
    for lo, hi in sorted(by_window):
        labels.append(f"{lo:g}-{hi:g}s")
        baseline_vals.append(by_window[(lo, hi)].get("baseline", float("nan")))
        qaccess_vals.append(by_window[(lo, hi)].get("qaccess_t", float("nan")))

    x = list(range(len(labels)))
    width = 0.38
    plt.figure(figsize=(10, 5))
    plt.bar([i - width / 2 for i in x], baseline_vals, width, label="baseline")
    plt.bar([i + width / 2 for i in x], qaccess_vals, width, label="qaccess_t")
    plt.xticks(x, labels)
    plt.xlabel("window")
    plt.ylabel(f"mean {scope_description(traffic_scope)} throughput Mbps")
    plt.legend()
    plt.tight_layout()
    plt.savefig(fig_dir / "window_mean_throughput.png", dpi=150)
    plt.close()


def markdown_table(rows: list[dict]) -> str:
    lines = [
        "| window_s | baseline_total_mean_mbps | qaccess_t_total_mean_mbps | delta_mbps | improvement_pct |",
        "|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        win = f"{float(row['window_start_s']):g}-{float(row['window_end_s']):g}"
        lines.append(
            "| {win} | {b:.6f} | {q:.6f} | {d:.6f} | {p:.6f} |".format(
                win=win,
                b=float(row["baseline_total_mean_mbps"]),
                q=float(row["qaccess_t_total_mean_mbps"]),
                d=float(row["delta_mbps"]),
                p=float(row["improvement_pct"]),
            )
        )
    return "\n".join(lines)


def write_summary(
    summary_dir: Path,
    session: Path,
    out_dir: Path,
    traffic_scope: str,
    filters: dict[str, str],
    pcap_used: dict[str, dict[str, str]],
    windows: list[tuple[float, float]],
    comparison: list[dict],
    warnings: list[str],
) -> None:
    summary_dir.mkdir(parents=True, exist_ok=True)
    readme = summary_dir / "README_eval_summary.md"
    window_text = ", ".join(f"{lo:g}:{hi:g}" for lo, hi in windows)
    filters_text = "\n".join(f"- {k}: `{v}`" for k, v in filters.items())
    pcap_text = "\n".join(
        f"- {mode} {path_name}: `{pcap}`"
        for mode, paths in pcap_used.items()
        for path_name, pcap in paths.items()
    )
    warnings_text = "\n".join(f"- {w}" for w in warnings) if warnings else "- none"
    readme.write_text(
        "\n".join(
            [
                "# Fig.7 Q-ACCeSS-T Pcap Evaluation",
                "",
                f"- input session: `{session}`",
                f"- output directory: `{out_dir}`",
                f"- traffic scope: `{traffic_scope}` ({scope_description(traffic_scope)})",
                f"- windows: `{window_text}`",
                "",
                "## Tshark Filters",
                "",
                filters_text,
                "",
                "## Pcap Files",
                "",
                pcap_text,
                "",
                "## Comparison Summary",
                "",
                markdown_table(comparison),
                "",
                "## Warnings",
                "",
                warnings_text,
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = {
        "session": str(session),
        "out_dir": str(out_dir),
        "traffic_scope": traffic_scope,
        "windows": [{"start_s": lo, "end_s": hi} for lo, hi in windows],
        "tshark_filters": filters,
        "pcap_files": pcap_used,
        "comparison_summary": comparison,
        "warnings": warnings,
    }
    (summary_dir / "eval_result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def evaluate(args: argparse.Namespace) -> Path:
    session = args.session.resolve()
    if not session.is_dir():
        raise FileNotFoundError(f"session directory not found: {session}")

    traffic_scope = args.traffic_scope
    filters = filters_for_scope(traffic_scope)
    label = scope_label(traffic_scope)
    default_out_name = "eval_fig7_qaccess_t" if traffic_scope == "downlink" else f"eval_fig7_qaccess_t_{label}"
    out_dir = args.out_dir.resolve() if args.out_dir else session / default_out_name
    csv_dir = out_dir / "csv"
    fig_dir = out_dir / "figures"
    log_dir = out_dir / "logs"
    summary_dir = out_dir / "summary"
    for d in (csv_dir, fig_dir, log_dir, summary_dir):
        d.mkdir(parents=True, exist_ok=True)

    evaluation_log = log_dir / "evaluation.log"
    commands_log = log_dir / "tshark_commands.log"
    evaluation_log.write_text("", encoding="utf-8")
    commands_log.write_text("", encoding="utf-8")

    def log(message: str) -> None:
        print(message)
        with evaluation_log.open("a", encoding="utf-8") as f:
            f.write(message + "\n")

    ensure_tshark(args.tshark_bin)
    windows = parse_windows(args.windows)
    suffix = bin_suffix(args.bin_size)

    pcap_used: dict[str, dict[str, str]] = {}
    path_series: dict[str, dict[str, PathSeries]] = defaultdict(dict)
    warnings: list[str] = []

    for mode, (_, run_subdir) in RUNS.items():
        run_dir = session / run_subdir
        if not run_dir.is_dir():
            raise FileNotFoundError(f"run directory not found: {run_dir}")
        pcap_a = find_one_pcap(run_dir, "pathA_h1_*.pcap")
        pcap_b = find_one_pcap(run_dir, "pathB_h1_*.pcap")
        pcap_used[mode] = {"pathA": str(pcap_a), "pathB": str(pcap_b)}

        log(f"[eval] reading {mode} pcaps traffic_scope={traffic_scope}")
        packets_by_path = {
            "pathA": read_filtered_packets(pcap_a, filters["pathA"], args.tshark_bin, commands_log),
            "pathB": read_filtered_packets(pcap_b, filters["pathB"], args.tshark_bin, commands_log),
        }
        all_packets = [pkt for packets in packets_by_path.values() for pkt in packets]
        if not all_packets:
            raise RuntimeError(f"no packets found for {mode} with traffic_scope={traffic_scope}")
        t0 = min(ts for ts, _ in all_packets)

        bins_by_path: dict[str, dict[int, int]] = {}
        max_idx = 0
        counts: dict[str, tuple[int, int]] = {}
        for path_name, packets in packets_by_path.items():
            bins, packet_count, byte_count = aggregate_packets(packets, t0, args.bin_size)
            bins_by_path[path_name] = bins
            counts[path_name] = (packet_count, byte_count)
            if bins:
                max_idx = max(max_idx, max(bins))
            if packet_count == 0:
                warnings.append(f"{mode} {path_name} has no packets after {traffic_scope} filter")

        for path_name, pcap in [("pathA", pcap_a), ("pathB", pcap_b)]:
            rows = rows_from_bins(bins_by_path[path_name], max_idx, args.bin_size)
            packet_count, byte_count = counts[path_name]
            path_series[mode][path_name] = PathSeries(
                mode=mode,
                path_name=path_name,
                pcap=pcap,
                rows=rows,
                packet_count=packet_count,
                byte_count=byte_count,
            )
            out_name = f"{mode}_{path_name}_{label}_{suffix}.csv"
            write_csv(csv_dir / out_name, rows, ["time_s", "bytes", "bits", "mbps"])
            log(f"[eval] wrote {out_name} packets={packet_count} bytes={byte_count}")

    total_by_mode: dict[str, list[dict]] = {}
    combined: list[dict] = []
    for mode in RUNS:
        rows_a = path_series[mode]["pathA"].rows
        rows_b = path_series[mode]["pathB"].rows
        max_len = max(len(rows_a), len(rows_b))
        total_rows: list[dict] = []
        for idx in range(max_len):
            time_s = idx * args.bin_size
            a = float(rows_a[idx]["mbps"]) if idx < len(rows_a) else 0.0
            b = float(rows_b[idx]["mbps"]) if idx < len(rows_b) else 0.0
            row = {
                "time_s": round(time_s, 6),
                "pathA_mbps": a,
                "pathB_mbps": b,
                "total_mbps": a + b,
            }
            total_rows.append(row)
            combined.append(
                {
                    "time_s": round(time_s, 6),
                    "run": mode,
                    "mode": mode,
                    "pathA_mbps": a,
                    "pathB_mbps": b,
                    "total_mbps": a + b,
                    "capacity_phase": capacity_phase(time_s),
                }
            )
        total_by_mode[mode] = total_rows
        write_csv(
            csv_dir / f"{mode}_total_{label}_{suffix}.csv",
            total_rows,
            ["time_s", "pathA_mbps", "pathB_mbps", "total_mbps"],
        )

    write_csv(
        csv_dir / f"combined_timeseries_{suffix}.csv",
        combined,
        ["time_s", "run", "mode", "pathA_mbps", "pathB_mbps", "total_mbps", "capacity_phase"],
    )

    summaries = window_rows(total_by_mode, windows)
    comparisons = comparison_rows(summaries)
    summary_fields = [
        "mode",
        "window_start_s",
        "window_end_s",
        "pathA_mean_mbps",
        "pathB_mean_mbps",
        "total_mean_mbps",
        "pathA_p95_mbps",
        "pathB_p95_mbps",
        "total_p95_mbps",
        "total_std_mbps",
        "n_bins",
    ]
    comparison_fields = [
        "window_start_s",
        "window_end_s",
        "baseline_total_mean_mbps",
        "qaccess_t_total_mean_mbps",
        "delta_mbps",
        "improvement_pct",
    ]
    write_csv(csv_dir / "window_summary.csv", summaries, summary_fields)
    write_csv(csv_dir / "comparison_summary.csv", comparisons, comparison_fields)
    save_figures(fig_dir, total_by_mode, summaries, traffic_scope)
    write_summary(summary_dir, session, out_dir, traffic_scope, filters, pcap_used, windows, comparisons, warnings)

    log(f"[eval] output directory: {out_dir}")
    return out_dir


def main() -> None:
    ap = argparse.ArgumentParser(description="Evaluate Q-ACCeSS-T Fig.7 pcap throughput")
    ap.add_argument("--session", type=Path, required=True, help="session_qaccess_t_* directory")
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="default: <session>/eval_fig7_qaccess_t for downlink, <session>/eval_fig7_qaccess_t_all_udp for all-udp",
    )
    ap.add_argument("--bin-size", type=float, default=1.0)
    ap.add_argument("--windows", default="0:50,50:100,100:200")
    ap.add_argument("--tshark-bin", default="tshark")
    ap.add_argument(
        "--traffic-scope",
        choices=["downlink", "all-udp"],
        default="downlink",
        help="downlink uses server-to-client IP filters; all-udp counts all UDP packets in each path pcap",
    )
    args = ap.parse_args()

    if args.bin_size <= 0:
        print("[error] --bin-size must be > 0", file=sys.stderr)
        sys.exit(2)

    try:
        out_dir = evaluate(args)
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Evaluation output: {out_dir}")


if __name__ == "__main__":
    main()
