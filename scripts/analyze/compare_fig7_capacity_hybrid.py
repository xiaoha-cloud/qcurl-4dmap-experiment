#!/usr/bin/env python3
"""Analyze a Fig.7-style baseline-vs-dynamic capacity-change session."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

FILES = {
    "total": "throughput_all_down.csv",
    "path_a": "throughput_pathA_down.csv",
    "path_b": "throughput_pathB_down.csv",
}
ARMS = {"baseline": "fig7_baseline", "dynamic": "fig7_qaccess_t_dynamic"}


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_bw_profile(path: Path, run_timeout: int) -> list[dict[str, float]]:
    iface = ""
    steps: list[tuple[int, float]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("IFACE="):
            iface = line.split("=", 1)[1].strip()
            continue
        match = re.match(r"^(\d+)\s+(\d+(?:\.\d+)?)$", line)
        if match:
            steps.append((int(match.group(1)), float(match.group(2))))
    if not steps:
        raise ValueError(f"no capacity steps in {path}")
    segments = []
    for index, (start, cap) in enumerate(steps):
        end = steps[index + 1][0] if index + 1 < len(steps) else run_timeout
        segments.append({
            "iface": iface,
            "start_s": float(start),
            "end_s": float(end),
            "path_b_capacity_mbps": float(cap),
            "path_a_capacity_mbps": 20.0,
            "total_capacity_mbps": float(cap) + 20.0,
        })
    return segments


def _default_windows(segments: list[dict[str, float]]) -> list[tuple[str, float, float]]:
    starts = [int(seg["start_s"]) for seg in segments]
    if starts[:3] == [0, 50, 100]:
        return [
            ("PHASE1_20_50", 20.0, 50.0),
            ("HIGH_CAPACITY_60_100", 60.0, 100.0),
            ("LOW_CAPACITY_120_200", 120.0, min(200.0, segments[-1]["end_s"])),
        ]
    windows = []
    for idx, seg in enumerate(segments):
        margin = 20.0 if idx in (0, len(segments) - 1) else 10.0
        lo = seg["start_s"] + margin
        hi = seg["end_s"]
        if hi - lo < 10.0:
            lo = seg["start_s"]
        label = f"SEGMENT_{idx + 1}_{int(lo)}_{int(hi)}"
        windows.append((label, lo, hi))
    return windows


def _means(path: Path, windows: list[tuple[str, float, float]]) -> dict[str, float]:
    df = pd.read_csv(path)
    result: dict[str, float] = {}
    for label, lo, hi in windows:
        result[label] = float(df[(df.elapsed_s >= lo) & (df.elapsed_s < hi)].throughput_mbps.mean())
    return result


def _continuous(ids: list[str]) -> bool:
    serials = [int(re.search(r"_(\d+)$", value).group(1)) for value in ids]
    return serials == list(range(1, len(serials) + 1))


def _load_worker_rows(session: Path) -> list[dict[str, Any]]:
    path = session / "worker.log"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _request_write_failed(session: Path) -> int:
    path = session / "qaccess_trigger_audit.jsonl"
    if not path.is_file():
        return 0
    return sum('"trigger_decision":"request_write_failed"' in line.replace(" ", "") for line in path.read_text().splitlines())


def _controller_reload_confirmed(session: Path) -> bool | None:
    logs = sorted((session.parent / "validation_logs").glob(f"{session.name}*validate*.log"))
    if not logs:
        return None
    text = logs[-1].read_text(encoding="utf-8", errors="replace")
    if "PASS controller coefficient reload confirmed" in text:
        return True
    if "FAIL controller coefficient reload confirmed" in text:
        return False
    return None


def _path_b_migration_notes(session: Path) -> str:
    artifacts = sorted((session / "fig7_qaccess_t_dynamic").rglob("qaccess_path_eligibility_*.json"))
    if not artifacts:
        return "No Path B eligibility artifacts found."
    ever_eligible = False
    ever_zero_growth = False
    for artifact in artifacts:
        rows = json.loads(artifact.read_text(encoding="utf-8"))
        for row in rows:
            if row.get("physical_path") != "Path B":
                continue
            if row.get("eligible") is True:
                ever_eligible = True
            if row.get("eligible") is False and row.get("exclusion_reason") == "no_sender_byte_growth":
                ever_zero_growth = True
    if ever_eligible and ever_zero_growth:
        return "Path B was initially eligible, then later left scoring when sender-byte growth dropped to zero, consistent with traffic shifting away from the constrained path."
    if ever_eligible:
        return "Path B remained eligible in at least one request window."
    return "Path B never became eligible in the captured request windows."


def analyze(session: Path) -> dict[str, Any]:
    session = session.resolve()
    metadata = _json(session / "experiment_metadata.json")
    profile = Path(metadata["bw_profile"])
    run_timeout = int(metadata.get("timeout", 220))
    segments = _parse_bw_profile(profile, run_timeout)
    windows = _default_windows(segments)

    report: dict[str, Any] = {
        "session": str(session),
        "capacity_profile": segments,
        "window_definitions": [{"label": label, "start_s": lo, "end_s": hi} for label, lo, hi in windows],
        "gate_mode": metadata.get("gate_mode"),
        "execution_mode": metadata.get("execution_mode"),
        "min_relative_gain": metadata.get("min_relative_gain"),
        "min_delta_gain_bps": metadata.get("min_delta_gain_bps"),
        "arms": {},
    }

    capacity_by_window = {}
    for label, lo, hi in windows:
        midpoint = (lo + hi) / 2.0
        segment = next(seg for seg in segments if seg["start_s"] <= midpoint < seg["end_s"])
        capacity_by_window[label] = segment["total_capacity_mbps"]

    for arm, directory in ARMS.items():
        arm_dir = session / directory
        totals = {metric: _means(arm_dir / filename, windows) for metric, filename in FILES.items()}
        total_util = {
            label: totals["total"][label] / capacity_by_window[label]
            for label in totals["total"]
        }
        report["arms"][arm] = {
            "total": totals["total"],
            "path_a": totals["path_a"],
            "path_b": totals["path_b"],
            "total_utilization_fraction": total_util,
        }

    labels = [label for label, _, _ in windows]
    phase1, high, low = labels[:3]
    base = report["arms"]["baseline"]
    dynamic = report["arms"]["dynamic"]
    high_diff = dynamic["total"][high] - base["total"][high]
    low_diff = dynamic["total"][low] - base["total"][low]
    high_util_diff = dynamic["total_utilization_fraction"][high] - base["total_utilization_fraction"][high]
    low_util_diff = dynamic["total_utilization_fraction"][low] - base["total_utilization_fraction"][low]

    worker_rows = _load_worker_rows(session)
    applied_rows = [row for row in worker_rows if bool(row.get("actual_applied"))]
    request_ids = [str(row.get("request_id")) for row in worker_rows if row.get("request_id")]

    if high_diff > 0 and (low_diff > 0 or low_util_diff >= 0):
        verdict = "dynamic_better"
    elif high_diff < 0 and low_diff < 0:
        verdict = "dynamic_worse"
    else:
        verdict = "mixed"

    report.update({
        "adaptation_after_capacity_increase": {
            "baseline_delta_from_phase1_mbps": base["total"][high] - base["total"][phase1],
            "dynamic_delta_from_phase1_mbps": dynamic["total"][high] - dynamic["total"][phase1],
            "dynamic_minus_baseline_high_capacity_mbps": high_diff,
            "dynamic_minus_baseline_high_capacity_utilization": high_util_diff,
        },
        "behavior_after_capacity_decrease": {
            "baseline_delta_from_high_to_low_mbps": base["total"][low] - base["total"][high],
            "dynamic_delta_from_high_to_low_mbps": dynamic["total"][low] - dynamic["total"][high],
            "dynamic_minus_baseline_low_capacity_mbps": low_diff,
            "dynamic_minus_baseline_low_capacity_utilization": low_util_diff,
        },
        "coefficient_events": [
            {key: row.get(key) for key in (
                "request_id", "timestamp_ms", "request_classification", "status", "gate_mode",
                "absolute_gain_bps", "relative_gain", "would_apply_under_gate", "actual_applied",
                "eligible_path_ids", "traffic_weighted_proposed_candidate",
                "traffic_weighted_proposed_stepped_coefficients", "applied_coefficients",
            )}
            for row in worker_rows
        ],
        "dynamic_updates_applied": bool(applied_rows),
        "applied_update_count": len(applied_rows),
        "applied_request_phases": [str(row.get("request_classification") or "UNKNOWN") for row in applied_rows],
        "request_serial_continuity": _continuous(request_ids) if request_ids else False,
        "request_write_failed": _request_write_failed(session),
        "controller_reload_confirmed": _controller_reload_confirmed(session),
        "path_b_migration_notes": _path_b_migration_notes(session),
        "verdict": verdict,
    })
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Fig.7 Capacity Hybrid Comparison",
        "",
        f"Session: `{Path(report['session']).name}`",
        "",
        f"Gate mode: `{report['gate_mode']}`",
        f"Execution mode: `{report['execution_mode']}`",
        "",
        "## Capacity Profile",
        "",
    ]
    for seg in report["capacity_profile"]:
        lines.append(
            f"- `{int(seg['start_s'])}-{int(seg['end_s'])}s`: Path A `{seg['path_a_capacity_mbps']:.0f} Mbps`, Path B `{seg['path_b_capacity_mbps']:.0f} Mbps`, total `{seg['total_capacity_mbps']:.0f} Mbps`"
        )
    lines.extend([
        "",
        "| Window | Baseline Total | Dynamic Total | Baseline Path A | Dynamic Path A | Baseline Path B | Dynamic Path B | Baseline Util | Dynamic Util |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for window in report["arms"]["baseline"]["total"]:
        lines.append(
            "| {window} | {bt:.6f} | {dt:.6f} | {ba:.6f} | {da:.6f} | {bb:.6f} | {db:.6f} | {bu:.2%} | {du:.2%} |".format(
                window=window,
                bt=report["arms"]["baseline"]["total"][window],
                dt=report["arms"]["dynamic"]["total"][window],
                ba=report["arms"]["baseline"]["path_a"][window],
                da=report["arms"]["dynamic"]["path_a"][window],
                bb=report["arms"]["baseline"]["path_b"][window],
                db=report["arms"]["dynamic"]["path_b"][window],
                bu=report["arms"]["baseline"]["total_utilization_fraction"][window],
                du=report["arms"]["dynamic"]["total_utilization_fraction"][window],
            )
        )
    lines.extend([
        "",
        "## Summary",
        "",
        f"- Applied update count: `{report['applied_update_count']}`",
        f"- Applied phases: `{', '.join(report['applied_request_phases']) if report['applied_request_phases'] else 'none'}`",
        f"- Request serial continuity: `{report['request_serial_continuity']}`",
        f"- request_write_failed: `{report['request_write_failed']}`",
        f"- Controller reload confirmed: `{report['controller_reload_confirmed']}`",
        f"- Verdict: `{report['verdict']}`",
        "",
        "## Path B Migration Notes",
        "",
        report["path_b_migration_notes"],
        "",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--md-out", type=Path)
    args = parser.parse_args()

    report = analyze(args.session)
    session = args.session.resolve()
    json_out = args.json_out or (session / "fig7_capacity_hybrid_comparison.json")
    md_out = args.md_out or (session / "fig7_capacity_hybrid_comparison.md")
    json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md_out.write_text(render_markdown(report), encoding="utf-8")
    print(f"[fig7_compare] wrote {json_out}")
    print(f"[fig7_compare] wrote {md_out}")


if __name__ == "__main__":
    main()
