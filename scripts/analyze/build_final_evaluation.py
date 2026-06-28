#!/usr/bin/env python3
"""Build the final Q-ACCeSS-T evaluation tables and plots.

The final evaluation compares two utility modes over the same MPQUIC transport:

* baseline: MPQUIC transport with utility/controller disabled.
* Q-ACCeSS-T: MPQUIC transport with utility/controller enabled.

The primary QoE-style figures follow the 4D-MAP metric structure, but use the
observable server-side proxies currently available in this project. Stream-delay
p95 and frame-gap p95/max values are diagnostics only, not final QoE metrics.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
if "MPLCONFIGDIR" not in os.environ:
    mpl_dir = REPO_ROOT / "derived" / ".mplconfig"
    mpl_dir.mkdir(parents=True, exist_ok=True)
    os.environ["MPLCONFIGDIR"] = str(mpl_dir)
if str(REPO_ROOT / "scripts" / "analyze") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "analyze"))

try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except Exception as exc:  # pragma: no cover - reported at runtime.
    plt = None
    MATPLOTLIB_ERROR = exc
else:
    MATPLOTLIB_ERROR = None

from qoe_from_events import group_rows, input_files, read_rows, summarize_session


RUN_LABELS = {
    "off": "Baseline\nutility off",
    "on": "Q-ACCeSS-T\nutility on",
}

PACKAGE_PROFILE_LABELS = {
    "fig7_behavior": "Fig.7 behavior",
    "fig8_behavior": "Fig.8 behavior",
    "qoe_live": "Live-streaming QoE proxy",
}


@dataclass(frozen=True)
class LegSpec:
    profile: str
    session: Path
    leg_dir: Path
    utility_mode: str
    controller_status: str
    label: str


def parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out):
        return None
    return out


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", newline="", encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def mean(values: Iterable[float | None]) -> float | None:
    clean = [v for v in values if v is not None and not math.isnan(v)]
    if not clean:
        return None
    return sum(clean) / len(clean)


def fmt(value: Any, digits: int = 3) -> str:
    number = parse_float(value)
    if number is None:
        return ""
    return f"{number:.{digits}f}"


def latest_session(pattern: str) -> Path | None:
    candidates = sorted((REPO_ROOT / "logs_exp").glob(pattern))
    return candidates[-1] if candidates else None


def find_leg(session: Path, names: list[str]) -> Path | None:
    for name in names:
        path = session / name
        if path.is_dir():
            return path
    return None


def build_leg_specs(fig7_session: Path | None, fig8_session: Path | None) -> list[LegSpec]:
    specs: list[LegSpec] = []
    if fig7_session:
        baseline = find_leg(fig7_session, ["fig7_baseline"])
        qaccess = find_leg(fig7_session, ["fig7_qaccess_t_dynamic"])
        if baseline:
            specs.append(LegSpec("Fig.7-like", fig7_session, baseline, "off", "disabled", "baseline utility off"))
        if qaccess:
            specs.append(LegSpec("Fig.7-like", fig7_session, qaccess, "on", "enabled", "Q-ACCeSS-T utility on"))
    if fig8_session:
        baseline = find_leg(fig8_session, ["combined_baseline"])
        qaccess = find_leg(fig8_session, ["combined_qaccess_t_dynamic"])
        if baseline:
            specs.append(LegSpec("Fig.8-like", fig8_session, baseline, "off", "disabled", "baseline utility off"))
        if qaccess:
            specs.append(LegSpec("Fig.8-like", fig8_session, qaccess, "on", "enabled", "Q-ACCeSS-T utility on"))
    return specs


def throughput_file(leg_dir: Path, stem: str) -> Path | None:
    candidates = [
        leg_dir / f"throughput_{stem}_down.csv",
        leg_dir / "csv" / f"throughput_{stem}_down.csv",
        leg_dir / "csv_mac" / f"throughput_{stem}_down.csv",
    ]
    if stem == "total":
        candidates.extend(
            [
                leg_dir / "throughput_all_down.csv",
                leg_dir / "csv" / "throughput_all_down.csv",
                leg_dir / "csv_mac" / "throughput_all_down.csv",
            ]
        )
    for path in candidates:
        if path.is_file():
            return path
    return None


def read_throughput_series(leg_dir: Path, stem: str = "total") -> list[tuple[float, float]]:
    path = throughput_file(leg_dir, stem)
    if not path:
        return []
    rows = read_csv_rows(path)
    series: list[tuple[float, float]] = []
    for row in rows:
        t = parse_float(row.get("elapsed_s") or row.get("time_s") or row.get("time"))
        y = parse_float(row.get("throughput_mbps") or row.get("mbps") or row.get("Mbps"))
        if t is not None and y is not None:
            series.append((t, y))
    return series


def average_throughput_mbps(leg_dir: Path, common_end_s: float | None) -> float | None:
    series = read_throughput_series(leg_dir, "total")
    if common_end_s is not None:
        series = [(t, y) for t, y in series if t <= common_end_s]
    return mean(y for _, y in series)


def path_utilisation(leg_dir: Path, common_end_s: float | None) -> tuple[float | None, float | None, str]:
    a = read_throughput_series(leg_dir, "pathA")
    b = read_throughput_series(leg_dir, "pathB")
    if common_end_s is not None:
        a = [(t, y) for t, y in a if t <= common_end_s]
        b = [(t, y) for t, y in b if t <= common_end_s]
    avg_a = mean(y for _, y in a)
    avg_b = mean(y for _, y in b)
    total = (avg_a or 0.0) + (avg_b or 0.0)
    if total <= 0:
        return avg_a, avg_b, ""
    share_a = 100.0 * (avg_a or 0.0) / total
    share_b = 100.0 * (avg_b or 0.0) / total
    return avg_a, avg_b, f"A={share_a:.1f}%, B={share_b:.1f}%"


def common_profile_window(specs: list[LegSpec], profile: str) -> float | None:
    ends: list[float] = []
    for spec in specs:
        if spec.profile != profile:
            continue
        series = read_throughput_series(spec.leg_dir, "total")
        if series:
            ends.append(max(t for t, _ in series))
    return min(ends) if len(ends) >= 2 else (ends[0] if ends else None)


def qoe_summary_for_leg(leg_dir: Path, gap_threshold_ms: float) -> dict[str, Any]:
    paths = input_files(leg_dir)
    paths = [Path(p) for p in paths if Path(p).name.startswith("qoe_events_")]
    if not paths:
        return {
            "qoe_events_available": "no",
            "qoe_notes": "qoe_events_missing",
        }
    rows = read_rows(paths)
    groups = group_rows(rows)
    if not groups:
        return {
            "qoe_events_available": "no",
            "qoe_notes": "qoe_rows_missing",
        }
    summaries = [summarize_session(session, group, gap_threshold_ms) for session, group in groups.items()]
    summaries.sort(key=lambda row: str(row.get("session_id", "")))
    summary = summaries[-1]
    return {
        "qoe_events_available": "yes",
        "qoe_session_id": summary.get("session_id", ""),
        "qoe_input_file": summary.get("input_file", ""),
        "rebuffering_candidate_duration_ms": summary.get("total_rebuffering_duration_ms", ""),
        "delivery_gap_event_count": summary.get("rebuffering_count", ""),
        "delivery_gap_ratio": summary.get("rebuffering_ratio", ""),
        "startup_latency_proxy_ms": summary.get("startup_latency_ms", ""),
        "avg_stream_delay_proxy_ms": summary.get("avg_stream_delay_ms", ""),
        "p95_stream_delay_proxy_ms": summary.get("p95_stream_delay_ms", ""),
        "p95_frame_gap_ms": summary.get("p95_frame_gap_ms", ""),
        "max_frame_gap_ms": summary.get("max_frame_gap_ms", ""),
        "video_event_count": summary.get("video_event_count", ""),
        "qoe_notes": summary.get("notes", ""),
    }


def jsonl_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def trigger_count(session: Path, utility_mode: str) -> int:
    if utility_mode != "on":
        return 0
    timeline = session / "dynamic_coefficient_timeline.jsonl"
    timeline_rows = jsonl_rows(timeline)
    if timeline_rows:
        request_ids = {str(row.get("request_id", "")) for row in timeline_rows if row.get("request_id")}
        return len(request_ids)
    audit = session / "qaccess_trigger_audit.jsonl"
    audit_rows = jsonl_rows(audit)
    request_ids = {
        str(row.get("request_id", ""))
        for row in audit_rows
        if row.get("trigger_decision") == "request_written" and row.get("request_id")
    }
    return len(request_ids)


def coefficients_changed(session: Path, profile: str, utility_mode: str) -> str:
    if utility_mode != "on":
        return "no"
    prefix = "fig7_qaccess_t_dynamic" if profile == "Fig.7-like" else "combined_qaccess_t_dynamic"
    before = session / f"{prefix}_coeffs_before.json"
    after = session / f"{prefix}_coeffs_after.json"
    if not before.is_file() or not after.is_file():
        return ""
    try:
        b = json.loads(before.read_text(encoding="utf-8"))
        a = json.loads(after.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    for key in ("alpha", "beta", "gamma"):
        if abs(float(a.get(key, 0.0)) - float(b.get(key, 0.0))) > 1e-9:
            return "yes"
    return "no"


def control_rows(leg_dir: Path) -> list[dict[str, str]]:
    return read_csv_rows(leg_dir / "control_law_diagnostics.csv")


def gain_backoff_changed(leg_dir: Path, utility_mode: str) -> str:
    if utility_mode != "on":
        return "no"
    rows = control_rows(leg_dir)
    gain_values = [parse_float(r.get("gain_applied_mean") or r.get("gain")) for r in rows]
    backoff_values = [parse_float(r.get("retention_applied_mean") or r.get("backoff")) for r in rows]
    values = [v for v in gain_values + backoff_values if v is not None]
    if not values:
        return ""
    return "yes" if max(values) - min(values) > 1e-6 else "no"


def control_value_changed(leg_dir: Path, utility_mode: str, kind: str) -> str:
    if utility_mode != "on":
        return "no"
    rows = control_rows(leg_dir)
    if kind == "gain":
        names = ("gain_applied_mean", "gain")
    elif kind == "backoff":
        names = ("retention_applied_mean", "backoff")
    else:
        raise ValueError(f"unknown control value kind: {kind}")
    values = []
    for row in rows:
        for name in names:
            value = parse_float(row.get(name))
            if value is not None:
                values.append(value)
                break
    if not values:
        return ""
    return "yes" if max(values) - min(values) > 1e-6 else "no"


def received_flv(leg_dir: Path) -> Path | None:
    files = sorted(leg_dir.glob("output_*.flv"))
    return files[-1] if files else None


def parse_visual_metrics(search_dirs: list[Path]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for directory in search_dirs:
        if not directory.is_dir():
            continue
        for ssim_path in directory.rglob("ssim.log"):
            text = ssim_path.read_text(encoding="utf-8", errors="replace")
            matches = re.findall(r"All:([0-9.]+)", text)
            if matches:
                metrics["ssim"] = matches[-1]
        for psnr_path in directory.rglob("psnr.log"):
            text = psnr_path.read_text(encoding="utf-8", errors="replace")
            matches = re.findall(r"average:([0-9.]+)", text)
            if matches:
                metrics["psnr"] = matches[-1]
        for vmaf_path in directory.rglob("vmaf.json"):
            try:
                doc = json.loads(vmaf_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            pooled = doc.get("pooled_metrics", {})
            if isinstance(pooled, dict):
                vmaf = pooled.get("vmaf", {})
                if isinstance(vmaf, dict) and "mean" in vmaf:
                    metrics["vmaf"] = vmaf["mean"]
    return metrics


def visual_summary(leg_dir: Path, out_dir: Path) -> dict[str, Any]:
    flv = received_flv(leg_dir)
    leg_name = leg_dir.name.lower()
    visual_alias = "baseline" if "baseline" in leg_name else "qaccess"
    metrics = parse_visual_metrics(
        [
            leg_dir,
            out_dir / "visual" / leg_dir.name,
            out_dir / "visual" / visual_alias,
            out_dir / visual_alias,
        ]
    )
    available = [name.upper() for name in ("ssim", "psnr", "vmaf") if name in metrics]
    return {
        "received_flv_exists": "yes" if flv else "no",
        "received_flv_path": str(flv) if flv else "",
        "visual_fidelity_metric_available": "/".join(available) if available else "no",
        "ssim": metrics.get("ssim", ""),
        "psnr": metrics.get("psnr", ""),
        "vmaf": metrics.get("vmaf", ""),
    }


def profile_step_times(session: Path, profile: str) -> list[tuple[float, str]]:
    metadata = session / "experiment_metadata.json"
    profile_path = ""
    if metadata.is_file():
        try:
            profile_path = str(json.loads(metadata.read_text(encoding="utf-8")).get("profile_path", ""))
        except json.JSONDecodeError:
            profile_path = ""
    path = Path(profile_path)
    if profile_path and not path.is_absolute():
        path = REPO_ROOT / path
    if not path.is_file():
        path = REPO_ROOT / (
            "scripts/mininet/bw_profile.fig7_200s.env"
            if profile == "Fig.7-like"
            else "scripts/mininet/combined_deterioration_profile_90_150.env"
        )
    steps: list[tuple[float, str]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("IFACE"):
                continue
            parts = line.split()
            t = parse_float(parts[0] if parts else None)
            if t is not None and t > 0:
                label = "capacity change" if profile == "Fig.7-like" else ("deterioration" if t < 150 else "recovery")
                steps.append((t, label))
    return steps


def ensure_plotting() -> None:
    if plt is None:
        raise RuntimeError(f"matplotlib is unavailable: {MATPLOTLIB_ERROR}")


def save_no_data_plot(path: Path, title: str, message: str) -> None:
    ensure_plotting()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.text(0.5, 0.5, message, ha="center", va="center", wrap=True)
    ax.set_axis_off()
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(path, dpi=180)
    plt.close(fig)


def plot_file(profile: str, key: str) -> str:
    if profile == "fig7_behavior":
        names = {
            "throughput": "fig7_1_throughput_over_time.png",
            "per_path": "fig7_2_per_path_throughput_over_time.png",
            "profile": "fig7_3_bandwidth_or_capacity_profile.png",
            "controller": "fig7_4_utility_gain_backoff_over_time.png",
        }
    elif profile == "fig8_behavior":
        names = {
            "throughput": "fig8_1_throughput_over_time.png",
            "delay_loss": "fig8_2_delay_loss_over_time.png",
            "controller": "fig8_3_utility_penalty_backoff_over_time.png",
            "per_path": "fig8_4_per_path_throughput_over_time.png",
        }
    elif profile == "qoe_live":
        names = {
            "rebuffering": "qoe_1_rebuffering_candidate_duration.png",
            "startup": "qoe_2_startup_latency_proxy.png",
            "stream_delay": "qoe_3_average_stream_delay_proxy.png",
            "visual": "qoe_4_visual_fidelity.png",
        }
    elif profile == "Fig.7-like":
        names = {
            "throughput": "fig7_1_throughput_over_time.png",
            "rebuffering": "fig7_2_rebuffering_candidate_duration.png",
            "startup": "fig7_3_startup_latency_proxy.png",
            "stream_delay": "fig7_4_average_stream_delay_proxy.png",
            "visual": "fig7_5_visual_fidelity.png",
            "controller": "fig7_6_utility_gain_timeline.png",
        }
    else:
        names = {
            "throughput": "fig8_1_throughput_over_time.png",
            "delay_loss": "fig8_2_delay_loss_over_time.png",
            "controller": "fig8_3_utility_penalty_backoff_over_time.png",
            "rebuffering": "fig8_4_rebuffering_candidate_duration.png",
            "startup": "fig8_5_startup_latency_proxy.png",
            "stream_delay": "fig8_6_average_stream_delay_proxy.png",
            "visual": "fig8_7_visual_fidelity.png",
        }
    return names[key]


def plot_throughput(profile: str, specs: list[LegSpec], out_dir: Path, out_path: Path | None = None) -> None:
    ensure_plotting()
    if out_path is None:
        out_path = out_dir / f"{slug(profile)}_throughput_timeline.png"
    fig, ax = plt.subplots(figsize=(8, 4.5))
    plotted = False
    session = None
    for spec in specs:
        if spec.profile != profile:
            continue
        session = spec.session
        series = read_throughput_series(spec.leg_dir, "total")
        if not series:
            continue
        plotted = True
        ax.plot([t for t, _ in series], [y for _, y in series], label=spec.label)
    if not plotted:
        save_no_data_plot(out_path, f"{profile} Throughput Timeline", "No throughput CSV was found.")
        return
    for t, label in profile_step_times(session or specs[0].session, profile):
        ax.axvline(t, color="0.35", linestyle="--", linewidth=1)
        ax.text(t, ax.get_ylim()[1], label, rotation=90, va="top", ha="right", fontsize=8, color="0.25")
    ax.set_title(f"{profile} Throughput Timeline")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Throughput (Mbps)")
    ax.grid(True, alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=180)
    plt.close(fig)


def plot_per_path_throughput(profile: str, specs: list[LegSpec], out_path: Path) -> None:
    ensure_plotting()
    fig, axes = plt.subplots(2, 1, figsize=(8, 6), sharex=True)
    plotted = False
    session = None
    for spec in specs:
        if spec.profile != profile:
            continue
        session = spec.session
        for stem, style in (("pathA", "-"), ("pathB", "--")):
            series = read_throughput_series(spec.leg_dir, stem)
            if not series:
                continue
            plotted = True
            ax = axes[0] if spec.utility_mode == "off" else axes[1]
            ax.plot(
                [t for t, _ in series],
                [y for _, y in series],
                linestyle=style,
                label=f"{spec.label} {stem}",
            )
    if not plotted:
        save_no_data_plot(out_path, f"{profile} Per-path Throughput Timeline", "No per-path throughput CSV was found.")
        return
    for ax in axes:
        ax.set_ylabel("Throughput (Mbps)")
        ax.grid(True, alpha=0.25)
        ax.legend()
        for t, label in profile_step_times(session or specs[0].session, profile):
            ax.axvline(t, color="0.35", linestyle="--", linewidth=1)
            if ax is axes[0]:
                ax.text(t, ax.get_ylim()[1], label, rotation=90, va="top", ha="right", fontsize=8, color="0.25")
    axes[-1].set_xlabel("Time (s)")
    fig.suptitle(f"{profile} Per-path Throughput Timeline")
    fig.tight_layout()
    fig.savefig(out_path, dpi=180)
    plt.close(fig)


def plot_profile_markers(profile: str, specs: list[LegSpec], out_path: Path) -> None:
    ensure_plotting()
    session = next((spec.session for spec in specs if spec.profile == profile), None)
    if session is None:
        save_no_data_plot(out_path, f"{profile} Network Profile", "No selected session was found.")
        return
    steps = profile_step_times(session, profile)
    if not steps:
        save_no_data_plot(out_path, f"{profile} Network Profile", "No profile step markers were found.")
        return
    fig, ax = plt.subplots(figsize=(8, 2.8))
    ax.hlines(1, 0, max(t for t, _ in steps) + 30, color="0.75")
    for t, label in steps:
        ax.axvline(t, color="#1f77b4", linestyle="--", linewidth=1.5)
        ax.text(t, 1.05, label, rotation=90, va="bottom", ha="right", fontsize=9)
    ax.set_ylim(0.8, 1.45)
    ax.set_yticks([])
    ax.set_xlabel("Time (s)")
    ax.set_title(f"{profile} Bandwidth/Capacity Profile Markers")
    fig.tight_layout()
    fig.savefig(out_path, dpi=180)
    plt.close(fig)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def rows_for_profile(rows: list[dict[str, Any]], profile: str) -> list[dict[str, Any]]:
    order = {"off": 0, "on": 1}
    return sorted([r for r in rows if r.get("profile") == profile], key=lambda r: order.get(str(r.get("utility_mode")), 99))


def bar_plot(rows: list[dict[str, Any]], profile: str, field: str, title: str, ylabel: str, out_path: Path) -> None:
    ensure_plotting()
    selected = rows_for_profile(rows, profile)
    values = [parse_float(row.get(field)) for row in selected]
    if not selected or all(v is None for v in values):
        save_no_data_plot(out_path, title, f"No data available for {title}.")
        return
    labels = [RUN_LABELS.get(str(row.get("utility_mode")), str(row.get("utility_mode"))) for row in selected]
    fig, ax = plt.subplots(figsize=(6.5, 4.2))
    y = [v if v is not None else 0.0 for v in values]
    bars = ax.bar(labels, y, color=["#7f7f7f", "#1f77b4"][: len(labels)])
    for bar, value in zip(bars, values):
        text = "n/a" if value is None else f"{value:.2f}"
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), text, ha="center", va="bottom", fontsize=9)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(out_path, dpi=180)
    plt.close(fig)


def visual_plot(rows: list[dict[str, Any]], profile: str, out_path: Path) -> None:
    ensure_plotting()
    selected = rows_for_profile(rows, profile)
    metric = None
    for candidate in ("vmaf", "ssim", "psnr"):
        if any(parse_float(row.get(candidate)) is not None for row in selected):
            metric = candidate
            break
    if not metric:
        save_no_data_plot(out_path, f"{profile} Visual Fidelity (SSIM/PSNR/VMAF)", "No FFmpeg visual fidelity metrics were found.")
        return
    ylabel = {"vmaf": "VMAF", "ssim": "SSIM", "psnr": "PSNR (dB)"}[metric]
    bar_plot(selected, profile, metric, f"{profile} Visual Fidelity (SSIM/PSNR/VMAF)", ylabel, out_path)


def aggregate_control_by_elapsed(rows: list[dict[str, str]], fields: list[str]) -> list[dict[str, float]]:
    buckets: dict[int, dict[str, list[float]]] = {}
    for row in rows:
        t_raw = parse_float(row.get("elapsed_s"))
        if t_raw is None:
            continue
        t = int(round(t_raw))
        bucket = buckets.setdefault(t, {field: [] for field in fields})
        for field in fields:
            value = parse_float(row.get(field))
            if value is not None:
                bucket[field].append(value)
    out: list[dict[str, float]] = []
    for t in sorted(buckets):
        record: dict[str, float] = {"elapsed_s": float(t)}
        for field in fields:
            value = mean(buckets[t][field])
            if value is not None:
                record[field] = value
        out.append(record)
    return out


def plot_delay_loss(profile: str, specs: list[LegSpec], out_dir: Path, out_path: Path | None = None) -> None:
    ensure_plotting()
    if out_path is None:
        out_path = out_dir / f"{slug(profile)}_delay_loss_timeline.png"
    qaccess = next((s for s in specs if s.profile == profile and s.utility_mode == "on"), None)
    if qaccess is None:
        save_no_data_plot(out_path, f"{profile} Delay/Loss Timeline", "No Q-ACCeSS-T leg was found.")
        return
    fields = ["owd_ms_mean", "loss_rate_mean", "retrans_bytes_delta_sum"]
    rows = aggregate_control_by_elapsed(control_rows(qaccess.leg_dir), fields)
    if not rows:
        save_no_data_plot(out_path, f"{profile} Delay/Loss Timeline", "No controller diagnostic rows were found.")
        return
    fig, axes = plt.subplots(3, 1, figsize=(8, 7), sharex=True)
    labels = [
        ("owd_ms_mean", "OWD (ms)"),
        ("loss_rate_mean", "Loss rate"),
        ("retrans_bytes_delta_sum", "Retrans bytes delta"),
    ]
    for ax, (field, ylabel) in zip(axes, labels):
        ax.plot([r["elapsed_s"] for r in rows], [r.get(field, math.nan) for r in rows])
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.25)
        for t, label in profile_step_times(qaccess.session, profile):
            ax.axvline(t, color="0.35", linestyle="--", linewidth=1)
            if ax is axes[0]:
                ax.text(t, ax.get_ylim()[1], label, rotation=90, va="top", ha="right", fontsize=8)
    axes[-1].set_xlabel("Time (s)")
    fig.suptitle(f"{profile} Delay/Loss Timeline")
    fig.tight_layout()
    fig.savefig(out_path, dpi=180)
    plt.close(fig)


def plot_controller(profile: str, specs: list[LegSpec], out_dir: Path, out_path: Path | None = None) -> None:
    ensure_plotting()
    if out_path is None:
        out_path = out_dir / f"{slug(profile)}_controller_timeline.png"
    qaccess = next((s for s in specs if s.profile == profile and s.utility_mode == "on"), None)
    if qaccess is None:
        save_no_data_plot(out_path, f"{profile} Controller Timeline", "No Q-ACCeSS-T leg was found.")
        return
    fields = [
        "throughput_reward_term_mean",
        "loss_penalty_term_mean",
        "delay_penalty_term_mean",
        "gain_applied_mean",
        "retention_applied_mean",
    ]
    rows = aggregate_control_by_elapsed(control_rows(qaccess.leg_dir), fields)
    if not rows:
        save_no_data_plot(out_path, f"{profile} Controller Timeline", "No controller diagnostic rows were found.")
        return
    fig, axes = plt.subplots(2, 1, figsize=(8, 6), sharex=True)
    term_fields = ["throughput_reward_term_mean", "loss_penalty_term_mean", "delay_penalty_term_mean"]
    for field in term_fields:
        if any(field in r for r in rows):
            axes[0].plot([r["elapsed_s"] for r in rows], [r.get(field, math.nan) for r in rows], label=field.replace("_mean", ""))
    for field, label in [("gain_applied_mean", "gain"), ("retention_applied_mean", "backoff")]:
        if any(field in r for r in rows):
            axes[1].plot([r["elapsed_s"] for r in rows], [r.get(field, math.nan) for r in rows], label=label)
    for ax in axes:
        ax.grid(True, alpha=0.25)
        ax.legend()
        for t, step_label in profile_step_times(qaccess.session, profile):
            ax.axvline(t, color="0.35", linestyle="--", linewidth=1)
            if ax is axes[0]:
                ax.text(t, ax.get_ylim()[1], step_label, rotation=90, va="top", ha="right", fontsize=8)
    axes[0].set_ylabel("Utility mapping terms")
    axes[1].set_ylabel("Control value")
    axes[1].set_xlabel("Time (s)")
    fig.suptitle(f"{profile} Utility/Penalty/Gain/Backoff Timeline")
    fig.tight_layout()
    fig.savefig(out_path, dpi=180)
    plt.close(fig)


def combined_row(spec: LegSpec, qoe: dict[str, Any], out_dir: Path, common_end_s: float | None) -> dict[str, Any]:
    avg_a, avg_b, util = path_utilisation(spec.leg_dir, common_end_s)
    visual = visual_summary(spec.leg_dir, out_dir)
    return {
        "profile": spec.profile,
        "run_session": spec.session.name,
        "leg": spec.leg_dir.name,
        "transport": "MPQUIC",
        "utility_mode": spec.utility_mode,
        "controller_status": spec.controller_status,
        "rebuffering_candidate_duration_ms": qoe.get("rebuffering_candidate_duration_ms", ""),
        "startup_latency_proxy_ms": qoe.get("startup_latency_proxy_ms", ""),
        "avg_stream_delay_proxy_ms": qoe.get("avg_stream_delay_proxy_ms", ""),
        "visual_fidelity_metric_available": visual["visual_fidelity_metric_available"],
        "ssim": visual["ssim"],
        "psnr": visual["psnr"],
        "vmaf": visual["vmaf"],
        "ssim_avg": visual["ssim"],
        "psnr_avg": visual["psnr"],
        "vmaf_mean": visual["vmaf"],
        "average_throughput_mbps": fmt(average_throughput_mbps(spec.leg_dir, common_end_s)),
        "pathA_average_throughput_mbps": fmt(avg_a),
        "pathB_average_throughput_mbps": fmt(avg_b),
        "path_utilisation": util,
        "trigger_count": trigger_count(spec.session, spec.utility_mode),
        "coefficient_changed": coefficients_changed(spec.session, spec.profile, spec.utility_mode),
        "gain_changed": control_value_changed(spec.leg_dir, spec.utility_mode, "gain"),
        "backoff_changed": control_value_changed(spec.leg_dir, spec.utility_mode, "backoff"),
        "gain_backoff_changed": gain_backoff_changed(spec.leg_dir, spec.utility_mode),
        "qoe_events_available": qoe.get("qoe_events_available", "no"),
        "received_flv_exists": visual["received_flv_exists"],
        "received_flv_path": visual["received_flv_path"],
        "common_window_end_s": fmt(common_end_s),
        "qoe_notes": qoe.get("qoe_notes", ""),
        "status": "ok" if qoe.get("qoe_events_available") == "yes" or visual["received_flv_exists"] == "yes" else "partial",
        "missing_inputs": qoe.get("qoe_notes", ""),
    }


def supporting_row(spec: LegSpec, qoe: dict[str, Any]) -> dict[str, Any]:
    return {
        "profile": spec.profile,
        "run_session": spec.session.name,
        "leg": spec.leg_dir.name,
        "utility_mode": spec.utility_mode,
        "p95_stream_delay_ms": qoe.get("p95_stream_delay_proxy_ms", ""),
        "p95_frame_gap_ms": qoe.get("p95_frame_gap_ms", ""),
        "max_frame_gap_ms": qoe.get("max_frame_gap_ms", ""),
        "delivery_gap_event_count": qoe.get("delivery_gap_event_count", ""),
        "delivery_gap_ratio": qoe.get("delivery_gap_ratio", ""),
        "video_event_count": qoe.get("video_event_count", ""),
        "qoe_events_available": qoe.get("qoe_events_available", "no"),
        "notes": qoe.get("qoe_notes", ""),
    }


def write_readme(out_dir: Path, package_profile: str) -> None:
    if package_profile == "fig7_behavior":
        profile_text = """This profile validates bandwidth/capacity adaptation.
It is not the main QoE evaluation. Figures are time-series for throughput,
path usage, and controller behaviour."""
    elif package_profile == "fig8_behavior":
        profile_text = """This profile validates delay/loss deterioration adaptation.
It is not the main QoE evaluation. Figures are time-series for throughput,
delay/loss, retransmission, and controller behaviour."""
    else:
        profile_text = """This profile is the final live-streaming QoE proxy evaluation.
Both baseline and Q-ACCeSS-T use MPQUIC transport. The experimental variable is
utility mode: off vs on."""
    text = f"""# Evaluation logic

## Package profile

`{package_profile}`: {profile_text}

## Core comparison

This evaluation is a controlled emulation comparison. Both variants use MPQUIC
as the transport. The experiment variable is the utility mode:

* baseline: utility/controller off.
* Q-ACCeSS-T: utility/controller on.

The QoE-style structure follows the 4D-MAP metrics: re-buffering time,
start-up latency, stream delay, and aSSIM/visual fidelity. The current project
does not include player-side buffer or playback-stall instrumentation, so the
reported metrics are named as observable proxies:

* `Re-buffering Candidate Duration (Server-side Proxy)` is derived from
  `qoe_from_events.py::total_rebuffering_duration_ms`. It is the inferred
  server-side video delivery-gap excess and must not be described as confirmed
  player re-buffering time.
* `Start-up Latency Proxy (Pusher to Server First Video Receive)` is measured
  from `pusher_start` to the first server/puller video receive event. It is not
  player first-frame display latency.
* `Average Stream Delay Proxy (Server-side)` is estimated from FLV timestamps,
  send wall-clock alignment, and server-side receive wall-clock time.
* `Visual Fidelity (SSIM/PSNR/VMAF)` uses FFmpeg-derived visual metrics when
  received FLV files are available. Strict aSSIM is not reported unless a
  dedicated aSSIM formula is implemented.

QoE summary figures, when generated, use utility mode/run type on the x-axis,
not time. Time is used only for time-series figures such as throughput,
delay/loss, and utility/gain/backoff.

The main QoE metrics follow the 4D-MAP structure: re-buffering time,
start-up latency, stream delay, and visual fidelity. `p95_stream_delay_ms` is
not used as a main metric because it is not part of the referenced 4D-MAP
evaluation metrics. Any p95/max gap values are reported only in
`supporting_metrics.csv` as diagnostics, not as primary QoE results.
"""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "README_evaluation_logic.md").write_text(text, encoding="utf-8")


def any_glob(path: Path, pattern: str) -> bool:
    return any(path.glob(pattern))


def leg_for(specs: list[LegSpec], profile: str, utility_mode: str) -> LegSpec | None:
    return next((spec for spec in specs if spec.profile == profile and spec.utility_mode == utility_mode), None)


def profile_has_data(specs: list[LegSpec], profile: str) -> bool:
    return any(spec.profile == profile for spec in specs)


def file_exists_for_both_legs(specs: list[LegSpec], profile: str, patterns: list[str]) -> tuple[bool, str]:
    missing = []
    for mode in ("off", "on"):
        spec = leg_for(specs, profile, mode)
        if spec is None:
            missing.append(f"{mode}:leg_missing")
            continue
        for pattern in patterns:
            if not any_glob(spec.leg_dir, pattern):
                missing.append(f"{spec.leg_dir.name}:{pattern}")
    return (not missing, "; ".join(missing))


def build_figure_mapping(specs: list[LegSpec], out_dir: Path, package_profile: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def add(profile: str, figure: str, required: str, columns: str, produced_by: str, output_name: str, patterns: list[str]) -> None:
        exists, missing = file_exists_for_both_legs(specs, profile, patterns)
        rows.append(
            {
                "figure_name": figure,
                "required_input_file": required,
                "required_columns": columns,
                "current_project_produces_it": "yes",
                "producer_script": produced_by,
                "exists_in_selected_session": "yes" if exists else "partial/no",
                "output_path": str(out_dir / "plots" / output_name),
                "missing_items": missing,
            }
        )

    if package_profile == "fig7_behavior" and profile_has_data(specs, "Fig.7-like"):
        add(
            "Fig.7-like",
            "Fig7-1 Throughput over time",
            "throughput_all_down.csv",
            "elapsed_s, throughput_mbps",
            "scripts/mininet/mp_topo.py -> scripts/analyze/pcap_throughput.py",
            plot_file("fig7_behavior", "throughput"),
            ["throughput_all_down.csv"],
        )
        add(
            "Fig.7-like",
            "Fig7-2 Per-path throughput over time",
            "throughput_pathA_down.csv; throughput_pathB_down.csv",
            "elapsed_s, throughput_mbps",
            "scripts/mininet/mp_topo.py -> scripts/analyze/pcap_throughput.py",
            plot_file("fig7_behavior", "per_path"),
            ["throughput_pathA_down.csv", "throughput_pathB_down.csv"],
        )
        add(
            "Fig.7-like",
            "Fig7-3 Bandwidth/capacity profile markers",
            "experiment_metadata.json; profile env file",
            "profile step timestamps",
            "scripts/mininet/run_qaccess_t_fig7_baseline_vs_dynamic_hybrid.sh",
            plot_file("fig7_behavior", "profile"),
            ["tc_deterioration.log"],
        )
        add(
            "Fig.7-like",
            "Fig7-4 Utility / gain / backoff over time",
            "control_law_diagnostics.csv",
            "elapsed_s, throughput_reward_term_mean, gain_applied_mean, retention_applied_mean",
            "scripts/analyze/finalize_control_law_leg.py",
            plot_file("fig7_behavior", "controller"),
            ["control_law_diagnostics.csv"],
        )

    if package_profile == "fig8_behavior" and profile_has_data(specs, "Fig.8-like"):
        add(
            "Fig.8-like",
            "Fig8-1 Throughput over time",
            "throughput_all_down.csv",
            "elapsed_s, throughput_mbps",
            "scripts/mininet/mp_topo.py -> scripts/analyze/pcap_throughput.py",
            plot_file("fig8_behavior", "throughput"),
            ["throughput_all_down.csv"],
        )
        add(
            "Fig.8-like",
            "Fig8-2 Delay / loss over time",
            "control_law_diagnostics.csv",
            "elapsed_s, owd_ms_mean, loss_rate_mean, retrans_bytes_delta_sum",
            "scripts/analyze/finalize_control_law_leg.py",
            plot_file("fig8_behavior", "delay_loss"),
            ["control_law_diagnostics.csv"],
        )
        add(
            "Fig.8-like",
            "Fig8-3 Utility / penalty / backoff over time",
            "control_law_diagnostics.csv",
            "elapsed_s, throughput_reward_term_mean, loss_penalty_term_mean, delay_penalty_term_mean, gain_applied_mean, retention_applied_mean",
            "scripts/analyze/finalize_control_law_leg.py",
            plot_file("fig8_behavior", "controller"),
            ["control_law_diagnostics.csv"],
        )
        add(
            "Fig.8-like",
            "Fig8-4 Per-path throughput over time",
            "throughput_pathA_down.csv; throughput_pathB_down.csv",
            "elapsed_s, throughput_mbps",
            "scripts/mininet/mp_topo.py -> scripts/analyze/pcap_throughput.py",
            plot_file("fig8_behavior", "per_path"),
            ["throughput_pathA_down.csv", "throughput_pathB_down.csv"],
        )

    if package_profile == "qoe_live" and profile_has_data(specs, "Fig.8-like"):
        add(
            "Fig.8-like",
            "QoE-1 Re-buffering candidate duration",
            "qoe/qoe_events_*.csv; qoe_summary.csv",
            "total_rebuffering_duration_ms renamed to rebuffering_candidate_duration_ms",
            "scripts/analyze/qoe_from_events.py",
            plot_file("qoe_live", "rebuffering"),
            ["qoe/qoe_events_*.csv"],
        )
        add(
            "Fig.8-like",
            "QoE-2 Start-up latency proxy",
            "qoe/qoe_events_*.csv; qoe_summary.csv",
            "startup_latency_ms",
            "scripts/analyze/qoe_from_events.py",
            plot_file("qoe_live", "startup"),
            ["qoe/qoe_events_*.csv"],
        )
        add(
            "Fig.8-like",
            "QoE-3 Average stream delay proxy",
            "qoe/qoe_events_*.csv; qoe_summary.csv",
            "avg_stream_delay_ms",
            "scripts/analyze/qoe_from_events.py",
            plot_file("qoe_live", "stream_delay"),
            ["qoe/qoe_events_*.csv"],
        )
        add(
            "Fig.8-like",
            "QoE-4 Visual fidelity",
            "output_*.flv; visual/*/(ssim.log, psnr.log, vmaf.json)",
            "SSIM All; PSNR average; VMAF mean",
            "scripts/analyze/visual_fidelity_ffmpeg.sh",
            plot_file("qoe_live", "visual"),
            ["output_*.flv"],
        )
    return rows


def write_audit_report(out_dir: Path, specs: list[LegSpec], mapping_rows: list[dict[str, Any]]) -> None:
    fig7_runner = REPO_ROOT / "scripts/mininet/run_qaccess_t_fig7_baseline_vs_dynamic_hybrid.sh"
    fig8_runner = REPO_ROOT / "scripts/mininet/run_qaccess_t_combined_deterioration_eval.sh"
    text = f"""# Evaluation figure plan audit

## Summary

A. Fig.7 figure structure is correct: {'yes' if profile_has_data(specs, 'Fig.7-like') else 'not generated for this selected session'}.

B. Fig.8 figure structure is correct: {'yes' if profile_has_data(specs, 'Fig.8-like') else 'not generated for this selected session'}.

C. Data sources are listed in `figure_data_mapping.csv`.

D. The paired runners generate raw QoE event logs, received FLV files, throughput CSVs, controller diagnostics, runtime samples, and worker logs. They do not directly generate final QoE summary tables or final plots.

E. Post-processing is required for `qoe_summary.csv`, visual fidelity metrics, final tables, figure mapping, README, and PNG plots.

F. Outputs are stored under this directory: `{out_dir}`.

G. Strict aSSIM is not implemented. Current visual fidelity output is SSIM / PSNR / VMAF. Approximate aSSIM should not be reported unless a separate formula is implemented and clearly labelled as a proxy.

H. `p95_stream_delay_ms`, `p95_frame_gap_ms`, `max_frame_gap_ms`, delivery-gap counts, and delivery-gap ratios are supporting diagnostics only. They are written to `supporting_metrics.csv` and are not primary QoE table fields or primary QoE plots.

I. A unified final evaluation script exists: `scripts/eval/run_final_eval_pipeline.sh`.

## Runners

* Fig.8 runner = `{fig8_runner if fig8_runner.is_file() else 'not found'}`
* Fig.7 runner = `{fig7_runner if fig7_runner.is_file() else 'not found'}`

## Core comparison

Both baseline and Q-ACCeSS-T use MPQUIC as the transport. The experimental variable is utility mode:

* baseline = MPQUIC transport + utility/controller off
* Q-ACCeSS-T = MPQUIC transport + utility/controller on
"""
    (out_dir / "evaluation_figure_plan_audit.md").write_text(text, encoding="utf-8")


def build(args: argparse.Namespace) -> None:
    out_dir = Path(args.output)
    plots_dir = out_dir / "plots"
    out_dir.mkdir(parents=True, exist_ok=True)
    plots_dir.mkdir(parents=True, exist_ok=True)
    package_profile = args.package_profile

    if args.fig7_session or args.fig8_session:
        fig7_session = Path(args.fig7_session) if args.fig7_session else None
        fig8_session = Path(args.fig8_session) if args.fig8_session else None
    else:
        fig7_session = latest_session("session_fig7_capacity_hybrid_*") if package_profile == "fig7_behavior" else None
        fig8_session = latest_session("session_combined_deterioration_*") if package_profile in {"fig8_behavior", "qoe_live"} else None
    specs = build_leg_specs(fig7_session, fig8_session)
    if not specs:
        raise SystemExit("No usable Fig.7/Fig.8 legs were found.")

    common_windows = {
        "Fig.7-like": common_profile_window(specs, "Fig.7-like"),
        "Fig.8-like": common_profile_window(specs, "Fig.8-like"),
    }

    rows: list[dict[str, Any]] = []
    qoe_rows: list[dict[str, Any]] = []
    supporting_rows: list[dict[str, Any]] = []
    transport_rows: list[dict[str, Any]] = []
    controller_rows: list[dict[str, Any]] = []
    visual_rows: list[dict[str, Any]] = []

    for spec in specs:
        qoe = qoe_summary_for_leg(spec.leg_dir, args.gap_threshold_ms)
        row = combined_row(spec, qoe, out_dir, common_windows.get(spec.profile))
        rows.append(row)
        supporting_rows.append(supporting_row(spec, qoe))
        qoe_rows.append(
            {
                "profile": row["profile"],
                "utility_mode": row["utility_mode"],
                "rebuffering_candidate_duration_ms": row["rebuffering_candidate_duration_ms"],
                "startup_latency_proxy_ms": row["startup_latency_proxy_ms"],
                "avg_stream_delay_proxy_ms": row["avg_stream_delay_proxy_ms"],
                "visual_fidelity_metric_available": row["visual_fidelity_metric_available"],
                "ssim_avg": row["ssim_avg"],
                "psnr_avg": row["psnr_avg"],
                "vmaf_mean": row["vmaf_mean"],
                "qoe_events_available": row["qoe_events_available"],
                "qoe_notes": row["qoe_notes"],
            }
        )
        transport_rows.append(
            {
                "profile": row["profile"],
                "utility_mode": row["utility_mode"],
                "average_throughput_mbps": row["average_throughput_mbps"],
                "pathA_average_throughput_mbps": row["pathA_average_throughput_mbps"],
                "pathB_average_throughput_mbps": row["pathB_average_throughput_mbps"],
                "path_utilisation": row["path_utilisation"],
                "common_window_end_s": row["common_window_end_s"],
            }
        )
        controller_rows.append(
            {
                "profile": row["profile"],
                "utility_mode": row["utility_mode"],
                "trigger_count": row["trigger_count"],
                "coefficient_changed": row["coefficient_changed"],
                "gain_changed": row["gain_changed"],
                "backoff_changed": row["backoff_changed"],
                "gain_backoff_changed": row["gain_backoff_changed"],
            }
        )
        visual_rows.append(
            {
                "profile": row["profile"],
                "utility_mode": row["utility_mode"],
                "received_flv_exists": row["received_flv_exists"],
                "visual_fidelity_metric_available": row["visual_fidelity_metric_available"],
                "ssim": row["ssim"],
                "psnr": row["psnr"],
                "vmaf": row["vmaf"],
                "ssim_avg": row["ssim_avg"],
                "psnr_avg": row["psnr_avg"],
                "vmaf_mean": row["vmaf_mean"],
                "received_flv_path": row["received_flv_path"],
            }
        )

    combined_fields = [
        "profile",
        "run_session",
        "leg",
        "transport",
        "utility_mode",
        "controller_status",
        "rebuffering_candidate_duration_ms",
        "startup_latency_proxy_ms",
        "avg_stream_delay_proxy_ms",
        "visual_fidelity_metric_available",
        "ssim",
        "psnr",
        "vmaf",
        "ssim_avg",
        "psnr_avg",
        "vmaf_mean",
        "average_throughput_mbps",
        "pathA_average_throughput_mbps",
        "pathB_average_throughput_mbps",
        "path_utilisation",
        "trigger_count",
        "coefficient_changed",
        "gain_changed",
        "backoff_changed",
        "gain_backoff_changed",
        "qoe_events_available",
        "received_flv_exists",
        "common_window_end_s",
        "status",
        "missing_inputs",
    ]
    write_csv(out_dir / "final_eval_combined_table.csv", rows, combined_fields)
    write_csv(out_dir / "qoe_proxy_summary.csv", qoe_rows, list(qoe_rows[0].keys()))
    write_csv(out_dir / "supporting_metrics.csv", supporting_rows, list(supporting_rows[0].keys()))
    write_csv(out_dir / "transport_summary.csv", transport_rows, list(transport_rows[0].keys()))
    write_csv(out_dir / "throughput_summary.csv", transport_rows, list(transport_rows[0].keys()))
    write_csv(out_dir / "controller_activation_summary.csv", controller_rows, list(controller_rows[0].keys()))
    write_csv(out_dir / "visual_quality_summary.csv", visual_rows, list(visual_rows[0].keys()))
    write_readme(out_dir, package_profile)

    run_inventory = [
        {
            "profile": spec.profile,
            "session": str(spec.session),
            "leg": str(spec.leg_dir),
            "transport": "MPQUIC",
            "utility_mode": spec.utility_mode,
            "controller_status": spec.controller_status,
        }
        for spec in specs
    ]
    write_csv(out_dir / "run_inventory.csv", run_inventory, list(run_inventory[0].keys()))
    mapping_rows = build_figure_mapping(specs, out_dir, package_profile)
    write_csv(
        out_dir / "figure_data_mapping.csv",
        mapping_rows,
        [
            "figure_name",
            "required_input_file",
            "required_columns",
            "current_project_produces_it",
            "producer_script",
            "exists_in_selected_session",
            "output_path",
            "missing_items",
        ],
    )
    write_audit_report(out_dir, specs, mapping_rows)

    if not args.no_plots:
        if package_profile == "fig7_behavior":
            profile = "Fig.7-like"
            plot_throughput(profile, specs, plots_dir, plots_dir / plot_file("fig7_behavior", "throughput"))
            plot_per_path_throughput(profile, specs, plots_dir / plot_file("fig7_behavior", "per_path"))
            plot_profile_markers(profile, specs, plots_dir / plot_file("fig7_behavior", "profile"))
            plot_controller(profile, specs, plots_dir, plots_dir / plot_file("fig7_behavior", "controller"))
        elif package_profile == "fig8_behavior":
            profile = "Fig.8-like"
            plot_throughput(profile, specs, plots_dir, plots_dir / plot_file("fig8_behavior", "throughput"))
            plot_delay_loss(profile, specs, plots_dir, plots_dir / plot_file("fig8_behavior", "delay_loss"))
            plot_controller(profile, specs, plots_dir, plots_dir / plot_file("fig8_behavior", "controller"))
            plot_per_path_throughput(profile, specs, plots_dir / plot_file("fig8_behavior", "per_path"))
        elif package_profile == "qoe_live":
            profile = "Fig.8-like"
            bar_plot(
                rows,
                profile,
                "rebuffering_candidate_duration_ms",
                "Re-buffering Candidate Duration (Server-side Proxy)",
                "Server-side delivery-gap excess (ms)",
                plots_dir / plot_file("qoe_live", "rebuffering"),
            )
            bar_plot(
                rows,
                profile,
                "startup_latency_proxy_ms",
                "Start-up Latency Proxy",
                "Latency proxy (ms)",
                plots_dir / plot_file("qoe_live", "startup"),
            )
            bar_plot(
                rows,
                profile,
                "avg_stream_delay_proxy_ms",
                "Average Stream Delay Proxy (Server-side)",
                "Average stream delay proxy (ms)",
                plots_dir / plot_file("qoe_live", "stream_delay"),
            )
            visual_plot(rows, profile, plots_dir / plot_file("qoe_live", "visual"))

    print(f"[final-eval] wrote {out_dir}")
    print(f"[final-eval] combined table: {out_dir / 'final_eval_combined_table.csv'}")
    print(f"[final-eval] plots: {plots_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build final Q-ACCeSS-T evaluation tables and figures.")
    parser.add_argument(
        "--package-profile",
        choices=["fig7_behavior", "fig8_behavior", "qoe_live"],
        default="qoe_live",
        help="Controls which final evaluation package and plot set is generated.",
    )
    parser.add_argument("--fig7-session", help="Path to a Fig.7-like session directory.")
    parser.add_argument("--fig8-session", help="Path to a Fig.8-like combined deterioration session directory.")
    parser.add_argument("--output", default=str(REPO_ROOT / "derived" / "final_eval"), help="Output directory.")
    parser.add_argument("--gap-threshold-ms", type=float, default=500.0, help="Delivery-gap excess threshold.")
    parser.add_argument("--no-plots", action="store_true", help="Write CSV/README outputs without PNG plots.")
    args = parser.parse_args()
    build(args)


if __name__ == "__main__":
    main()
