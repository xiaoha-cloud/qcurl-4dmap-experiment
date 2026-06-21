#!/usr/bin/env python3
"""Summarize the final Q-ACCeSS-T gate experiments."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

DEFAULT_SESSIONS = {
    "Relative Shadow": "logs_exp/session_combined_deterioration_20260621_154044",
    "Relative Active": "logs_exp/session_combined_deterioration_20260621_161904",
    "Hybrid Active": "logs_exp/session_combined_deterioration_20260621_165611",
}

DEFAULT_INTERPRETATIONS = {
    "Relative Shadow": "useful diagnostic, but no actual coefficient update.",
    "Relative Active": "active pipeline worked, but pure relative gate was too aggressive and produced dynamic_worse.",
    "Hybrid Active": "final candidate; blocks low-confidence updates, applies higher-confidence updates, reloads coefficients, and achieves dynamic_better.",
}

WINDOWS = ("PRE_70_90", "DURING_90_150", "POST_150_200")


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _json_lines(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _find_validation_log(validation_dir: Path, session_name: str) -> Path | None:
    candidate = validation_dir / f"{session_name}_active_validate.log"
    if candidate.is_file():
        return candidate
    matches = sorted(validation_dir.glob(f"{session_name}*validate*.log"))
    return matches[-1] if matches else None


def _bool_text(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "true" if value else "false"


def _path_b_migration_note(compare: dict[str, Any]) -> str:
    activities = compare.get("path_b_activity") or []
    if not activities:
        return "No Path B eligibility artifacts found."
    observed_zero_growth = False
    observed_eligible = False
    for artifact in activities:
        for row in artifact.get("path_b", []):
            if row.get("eligible") is True:
                observed_eligible = True
            if row.get("eligible") is False and row.get("exclusion_reason") == "no_sender_byte_growth":
                observed_zero_growth = True
    if observed_eligible and observed_zero_growth:
        return "Path B was initially active, then later dropped out of scoring when sender-byte growth fell to zero, consistent with traffic migration away from the impaired path."
    if observed_eligible:
        return "Path B remained active and eligible in at least one request window."
    return "Path B never became eligible for scoring in the captured request windows."


def _extract_reload_status(validation_log: Path | None) -> tuple[bool | None, str | None]:
    if validation_log is None or not validation_log.is_file():
        return None, None
    text = validation_log.read_text(encoding="utf-8", errors="replace")
    if "PASS controller coefficient reload confirmed" in text:
        return True, "PASS failures=0" if "SUMMARY PASS failures=0" in text else None
    if "FAIL controller coefficient reload confirmed" in text:
        return False, "SUMMARY FAIL" if "SUMMARY FAIL" in text else None
    return None, "PASS failures=0" if "SUMMARY PASS failures=0" in text else None


def summarize_session(label: str, session: Path, validation_dir: Path) -> dict[str, Any]:
    compare = _json(session / "baseline_vs_dynamic_relative_comparison.json")
    metadata = _json(session / "experiment_metadata.json")
    worker_rows = _json_lines(session / "worker.log")
    applied_rows = [row for row in worker_rows if bool(row.get("actual_applied"))]
    validation_log = _find_validation_log(validation_dir, session.name)
    controller_reload_confirmed, validator_summary = _extract_reload_status(validation_log)

    gate_mode = compare.get("gate_mode") or metadata.get("gate_mode")
    execution_mode = metadata.get("execution_mode")
    updates_applied = compare.get("dynamic_updates_applied")
    applied_update_count = compare.get("applied_update_count")
    if applied_update_count is None:
        applied_update_count = len(applied_rows)
    applied_phases = compare.get("applied_request_classifications")
    if applied_phases is None:
        applied_phases = [str(row.get("request_classification") or "UNKNOWN") for row in applied_rows]

    return {
        "label": label,
        "session_name": session.name,
        "session_path": str(session.resolve()),
        "gate_mode": gate_mode,
        "execution_mode": execution_mode,
        "updates_applied": updates_applied,
        "applied_update_count": applied_update_count,
        "applied_request_phases": applied_phases,
        "baseline_throughput_mbps": {
            window: float(compare["arms"]["baseline"]["total"][window]) for window in WINDOWS
        },
        "dynamic_throughput_mbps": {
            window: float(compare["arms"]["dynamic"]["total"][window]) for window in WINDOWS
        },
        "during_difference_mbps": float(compare["observed_during_difference_mbps"]),
        "during_relative_improvement": float(compare["observed_during_relative_difference"]),
        "verdict": compare.get("verdict"),
        "request_serial_continuous": bool(compare.get("request_serial_continuity")),
        "request_write_failed_zero": int(compare.get("request_write_failed", 0)) == 0,
        "controller_reload_confirmed": controller_reload_confirmed,
        "validator_summary": validator_summary,
        "path_b_migration_notes": _path_b_migration_note(compare),
        "interpretation": DEFAULT_INTERPRETATIONS[label],
    }


def build_summary(session_map: dict[str, Path], validation_dir: Path) -> dict[str, Any]:
    sessions = [summarize_session(label, path, validation_dir) for label, path in session_map.items()]
    final_candidate = next((row for row in sessions if row["label"] == "Hybrid Active"), sessions[-1])
    return {
        "final_candidate_session": final_candidate["session_name"],
        "sessions": sessions,
    }


def _fmt_num(value: float) -> str:
    return f"{value:.6f}"


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Final Q-ACCeSS-T Gate Experiment Summary",
        "",
        f"Final candidate: `{summary['final_candidate_session']}`",
        "",
        "| Experiment | Session | Gate | Mode | Updates Applied | Applied Count | Applied Phases | Baseline DURING (Mbps) | Dynamic DURING (Mbps) | DURING Diff (Mbps) | DURING Improvement | Verdict | Serials Continuous | request_write_failed=0 | Reload Confirmed | Interpretation |",
        "| --- | --- | --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- | --- |",
    ]
    for row in summary["sessions"]:
        lines.append(
            "| {label} | `{session}` | {gate} | {mode} | {updates} | {count} | {phases} | {base} | {dynamic} | {diff} | {improvement} | {verdict} | {serials} | {write_failed} | {reload} | {interpretation} |".format(
                label=row["label"],
                session=row["session_name"],
                gate=row["gate_mode"],
                mode=row["execution_mode"],
                updates=_bool_text(row["updates_applied"]),
                count=row["applied_update_count"],
                phases=", ".join(row["applied_request_phases"]) if row["applied_request_phases"] else "none",
                base=_fmt_num(row["baseline_throughput_mbps"]["DURING_90_150"]),
                dynamic=_fmt_num(row["dynamic_throughput_mbps"]["DURING_90_150"]),
                diff=_fmt_num(row["during_difference_mbps"]),
                improvement=_fmt_pct(row["during_relative_improvement"]),
                verdict=row["verdict"],
                serials=_bool_text(row["request_serial_continuous"]),
                write_failed=_bool_text(row["request_write_failed_zero"]),
                reload=_bool_text(row["controller_reload_confirmed"]),
                interpretation=row["interpretation"],
            )
        )
    lines.extend(["", "## Final Candidate", ""])
    final_candidate = next(row for row in summary["sessions"] if row["label"] == "Hybrid Active")
    lines.extend([
        f"- Session: `{final_candidate['session_name']}`",
        f"- Gate mode: `{final_candidate['gate_mode']}`",
        f"- Execution mode: `{final_candidate['execution_mode']}`",
        f"- Updates applied: `{_bool_text(final_candidate['updates_applied'])}`",
        f"- Applied update count: `{final_candidate['applied_update_count']}`",
        f"- Applied phases: `{', '.join(final_candidate['applied_request_phases'])}`",
        f"- Baseline DURING: `{_fmt_num(final_candidate['baseline_throughput_mbps']['DURING_90_150'])} Mbps`",
        f"- Dynamic DURING: `{_fmt_num(final_candidate['dynamic_throughput_mbps']['DURING_90_150'])} Mbps`",
        f"- DURING difference: `{_fmt_num(final_candidate['during_difference_mbps'])} Mbps`",
        f"- DURING relative improvement: `{_fmt_pct(final_candidate['during_relative_improvement'])}`",
        f"- Verdict: `{final_candidate['verdict']}`",
        f"- Controller reload confirmed: `{_bool_text(final_candidate['controller_reload_confirmed'])}`",
        f"- Validator: `{final_candidate['validator_summary'] or 'unknown'}`",
        "",
        "## Path B Migration Notes",
        "",
        final_candidate["path_b_migration_notes"],
        "",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--relative-shadow", type=Path, default=Path(DEFAULT_SESSIONS["Relative Shadow"]))
    parser.add_argument("--relative-active", type=Path, default=Path(DEFAULT_SESSIONS["Relative Active"]))
    parser.add_argument("--hybrid-active", type=Path, default=Path(DEFAULT_SESSIONS["Hybrid Active"]))
    parser.add_argument("--validation-dir", type=Path, default=Path("logs_exp/validation_logs"))
    parser.add_argument("--json-out", type=Path, default=Path("logs_exp/final_gate_experiment_summary.json"))
    parser.add_argument("--md-out", type=Path, default=Path("logs_exp/final_gate_experiment_summary.md"))
    args = parser.parse_args()

    session_map = {
        "Relative Shadow": args.relative_shadow.resolve(),
        "Relative Active": args.relative_active.resolve(),
        "Hybrid Active": args.hybrid_active.resolve(),
    }
    summary = build_summary(session_map, args.validation_dir.resolve())
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.md_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    args.md_out.write_text(render_markdown(summary), encoding="utf-8")
    print(f"[summary] wrote {args.json_out}")
    print(f"[summary] wrote {args.md_out}")


if __name__ == "__main__":
    main()
