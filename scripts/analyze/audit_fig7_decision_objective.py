#!/usr/bin/env python3
"""Audit the Fig.7 capacity-change decision objective without changing behavior."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _json_lines(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _request_id_from_name(path: Path, suffix: str) -> str:
    stem = path.stem
    prefix = "qaccess_candidate_scores_"
    if not stem.startswith(prefix) or not stem.endswith(suffix):
        raise ValueError(f"unexpected artifact name: {path.name}")
    return stem[len(prefix):-len(suffix)]


def _coeff_tuple(row: pd.Series) -> tuple[float, float, float]:
    return (float(row["alpha"]), float(row["beta"]), float(row["gamma"]))


def _coeff_dict(row: pd.Series | dict[str, Any] | None) -> dict[str, float] | None:
    if row is None:
        return None
    return {
        "alpha": float(row["alpha"]),
        "beta": float(row["beta"]),
        "gamma": float(row["gamma"]),
    }


def _match_coeffs(df: pd.DataFrame, coeffs: dict[str, Any] | None) -> pd.DataFrame:
    if coeffs is None:
        return df.iloc[0:0]
    return df[
        (df["alpha"] == float(coeffs["alpha"])) &
        (df["beta"] == float(coeffs["beta"])) &
        (df["gamma"] == float(coeffs["gamma"]))
    ]


def _gate_pass(gate_mode: str, absolute_gain: float, relative_gain: float,
               min_delta_gain_bps: float, min_relative_gain: float) -> bool:
    if gate_mode == "absolute":
        return absolute_gain >= min_delta_gain_bps
    if gate_mode == "relative":
        return relative_gain >= min_relative_gain
    return absolute_gain >= min_delta_gain_bps and relative_gain >= min_relative_gain


def _classify_request(
    path_entries: list[dict[str, Any]],
    would_apply: bool,
    raw_gate_pass: bool,
    stepped_gate_pass: bool,
    raw_coeffs: dict[str, float] | None,
    stepped_coeffs: dict[str, float] | None,
) -> list[str]:
    gains = [float(entry["gain_bps"]) for entry in path_entries]
    positives = [gain for gain in gains if gain > 0]
    negatives = [gain for gain in gains if gain < 0]
    diagnoses: list[str] = []

    if would_apply:
        diagnoses.append("aggregate_accepts")
    elif positives and negatives:
        diagnoses.append("aggregate_blocks_due_to_cross_path_tradeoff")
    elif not positives:
        diagnoses.append("aggregate_blocks_no_path_improvement")
    else:
        diagnoses.append("aggregate_blocks_below_threshold")

    if raw_coeffs is not None and stepped_coeffs is not None and raw_coeffs != stepped_coeffs:
        if raw_gate_pass == stepped_gate_pass:
            diagnoses.append("step_limit_not_decisive")

    return diagnoses


def _path_sensitive_diagnostics(
    path_entries: list[dict[str, Any]],
    aggregate_gain: float,
    min_delta_gain_bps: float,
) -> dict[str, Any]:
    best = max(path_entries, key=lambda row: float(row["gain_bps"]))
    path_b_rows = [row for row in path_entries if row.get("physical_path") == "Path B"]
    changed_path_gain = float(path_b_rows[0]["gain_bps"]) if path_b_rows else None
    changed_path_id = int(path_b_rows[0]["path_id"]) if path_b_rows else None
    hypothetical_apply = float(best["gain_bps"]) >= min_delta_gain_bps and aggregate_gain > 0
    return {
        "best_single_path_gain": float(best["gain_bps"]),
        "best_single_path_id": int(best["path_id"]),
        "changed_or_impaired_path_id": changed_path_id,
        "changed_or_impaired_path_gain": changed_path_gain,
        "path_sensitive_would_apply": hypothetical_apply,
        "path_sensitive_rule": {
            "single_path_gain_threshold_bps": min_delta_gain_bps,
            "aggregate_gain_must_be_positive": True,
        },
    }


def _recommendation(path_entries: list[dict[str, Any]], aggregate_gain: float, would_apply: bool) -> str | None:
    if would_apply:
        return None
    path_b = next((row for row in path_entries if row.get("physical_path") == "Path B"), None)
    path_a_negative = any(
        row.get("physical_path") == "Path A" and float(row["gain_bps"]) < 0 for row in path_entries
    )
    if path_b is None:
        return None
    if float(path_b["gain_bps"]) > 0 and path_a_negative and aggregate_gain <= 0:
        return (
            "The Fig.7 capacity-change decision is blocked by the aggregate multipath objective. "
            "The model detects a beneficial direction for the changed path, but the aggregate scorer "
            "rejects it because another active path degrades. This suggests that Fig.7 may require a "
            "capacity-change-specific objective or a bottleneck/changing-path-aware gate."
        )
    if float(path_b["gain_bps"]) > 0 and path_a_negative:
        return (
            "The Fig.7 capacity-change decision is blocked by the aggregate multipath objective. "
            "The model detects a beneficial direction for the changed path, but the aggregate scorer "
            "rejects it because another active path degrades. This suggests that Fig.7 may require a "
            "capacity-change-specific objective or a bottleneck/changing-path-aware gate."
        )
    return None


def analyze(session: Path) -> dict[str, Any]:
    session = session.resolve()
    metadata = _json(session / "experiment_metadata.json")
    dynamic_root = session / "fig7_qaccess_t_dynamic" / "processed_buffers"

    aggregate_files = {
        _request_id_from_name(path, "_aggregate"): path
        for path in dynamic_root.glob("qaccess_candidate_scores_*_aggregate.csv")
    }
    per_path_files = {
        _request_id_from_name(path, "_per_path"): path
        for path in dynamic_root.glob("qaccess_candidate_scores_*_per_path.csv")
    }
    eligibility_files = {
        path.stem[len("qaccess_path_eligibility_"):]: path
        for path in dynamic_root.glob("qaccess_path_eligibility_*.json")
    }

    worker_rows = _json_lines(session / "worker.log")
    gate_mode = str(metadata.get("gate_mode") or "hybrid")
    min_relative_gain = float(metadata.get("min_relative_gain", 0.0))
    min_delta_gain_bps = float(metadata.get("min_delta_gain_bps", 0.0))

    requests: list[dict[str, Any]] = []
    for worker in worker_rows:
        request_id = str(worker["request_id"])
        aggregate_df = pd.read_csv(aggregate_files[request_id])
        per_path_df = pd.read_csv(per_path_files[request_id])
        eligibility_rows = {int(row["path_id"]): row for row in _json(eligibility_files[request_id])}

        current_row = aggregate_df[aggregate_df["is_current_tuple"] == 1].iloc[0]
        raw_rank_col = "byte_weighted_rank" if "byte_weighted_rank" in aggregate_df.columns else "equal_weight_rank"
        raw_gain_col = "byte_weighted_gain" if "byte_weighted_gain" in aggregate_df.columns else "equal_weight_gain"
        raw_best_row = aggregate_df.sort_values([raw_rank_col, "is_current_tuple"]).iloc[0]
        if int(raw_best_row[raw_rank_col]) != 1:
            raw_best_row = aggregate_df.sort_values(raw_gain_col, ascending=False).iloc[0]

        current_coeffs = _coeff_dict(worker.get("current_coefficients")) or _coeff_dict(current_row)
        raw_candidate = worker.get("traffic_weighted_proposed_candidate") or worker.get("equal_weight_proposed_candidate")
        raw_candidate = raw_candidate or _coeff_dict(raw_best_row)
        stepped_candidate = (
            worker.get("traffic_weighted_proposed_stepped_coefficients")
            or worker.get("equal_weight_proposed_stepped_coefficients")
            or raw_candidate
        )

        raw_candidate_row = _match_coeffs(aggregate_df, raw_candidate)
        stepped_candidate_row = _match_coeffs(aggregate_df, stepped_candidate)
        if raw_candidate_row.empty:
            raw_candidate_row = pd.DataFrame([raw_best_row])
        if stepped_candidate_row.empty:
            stepped_candidate_row = raw_candidate_row

        raw_candidate_row = raw_candidate_row.iloc[0]
        stepped_candidate_row = stepped_candidate_row.iloc[0]

        relative_gain = float(worker.get("relative_gain", 0.0))
        aggregate_gain = float(worker.get("traffic_weighted_gain", worker.get("equal_weight_gain", 0.0)))
        stepped_aggregate_gain = float(stepped_candidate_row.get(raw_gain_col, aggregate_gain))
        raw_aggregate_gain = float(raw_candidate_row.get(raw_gain_col, aggregate_gain))
        absolute_gate_pass = aggregate_gain >= min_delta_gain_bps
        relative_gate_pass = relative_gain >= min_relative_gain
        would_apply = _gate_pass(gate_mode, aggregate_gain, relative_gain, min_delta_gain_bps, min_relative_gain)
        raw_gate_pass = _gate_pass(
            gate_mode,
            raw_aggregate_gain,
            relative_gain,
            min_delta_gain_bps,
            min_relative_gain,
        )
        stepped_gate_pass = _gate_pass(
            gate_mode,
            stepped_aggregate_gain,
            relative_gain,
            min_delta_gain_bps,
            min_relative_gain,
        )

        path_entries: list[dict[str, Any]] = []
        for path_id, path_df in per_path_df.groupby("path_id"):
            current_path = path_df[path_df["is_current_tuple"] == 1].iloc[0]
            candidate_path_matches = _match_coeffs(path_df, raw_candidate)
            candidate_path = candidate_path_matches.iloc[0] if not candidate_path_matches.empty else current_path
            gain = float(candidate_path["path_pred_candidate"]) - float(current_path["path_pred_candidate"])
            effect = "helps" if gain > 0 else ("hurts" if gain < 0 else "neutral")
            eligibility = eligibility_rows.get(int(path_id), {})
            path_entries.append({
                "path_id": int(path_id),
                "physical_path": eligibility.get("physical_path"),
                "weight": float((worker.get("path_weights") or {}).get(str(path_id), 0.0)),
                "current_score": float(current_path["path_pred_candidate"]),
                "candidate_score": float(candidate_path["path_pred_candidate"]),
                "gain_bps": gain,
                "effect": effect,
            })

        diagnoses = _classify_request(
            path_entries=path_entries,
            would_apply=would_apply,
            raw_gate_pass=raw_gate_pass,
            stepped_gate_pass=stepped_gate_pass,
            raw_coeffs=raw_candidate,
            stepped_coeffs=stepped_candidate,
        )
        path_sensitive = _path_sensitive_diagnostics(path_entries, aggregate_gain, min_delta_gain_bps)
        recommendation = _recommendation(path_entries, aggregate_gain, would_apply)

        requests.append({
            "request_id": request_id,
            "phase_classification": worker.get("request_classification"),
            "current_coefficients": current_coeffs,
            "aggregate_proposed_raw_candidate": raw_candidate,
            "aggregate_proposed_stepped_coefficients": stepped_candidate,
            "aggregate_equal_gain": float(worker.get("equal_weight_gain", 0.0)),
            "aggregate_traffic_weighted_gain": aggregate_gain,
            "relative_gain": relative_gain,
            "absolute_gate_pass": absolute_gate_pass,
            "relative_gate_pass": relative_gate_pass,
            "would_apply": bool(worker.get("would_apply_under_gate", would_apply)),
            "actual_applied": bool(worker.get("actual_applied")),
            "path_weights": worker.get("path_weights") or {},
            "per_path": path_entries,
            "diagnoses": diagnoses,
            "step_limit_not_decisive": "step_limit_not_decisive" in diagnoses,
            "raw_candidate_aggregate_gain": raw_aggregate_gain,
            "stepped_candidate_aggregate_gain": stepped_aggregate_gain,
            "recommendation": recommendation,
            **path_sensitive,
        })

    return {
        "session": str(session),
        "gate_mode": gate_mode,
        "execution_mode": metadata.get("execution_mode"),
        "min_relative_gain": min_relative_gain,
        "min_delta_gain_bps": min_delta_gain_bps,
        "requests": requests,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Fig.7 Decision Objective Audit",
        "",
        f"Session: `{Path(report['session']).name}`",
        "",
        f"Gate mode: `{report['gate_mode']}`",
        f"Execution mode: `{report['execution_mode']}`",
        f"Thresholds: absolute `{report['min_delta_gain_bps']:.0f} bps`, relative `{report['min_relative_gain']:.4f}`",
        "",
    ]
    for request in report["requests"]:
        lines.extend([
            f"## {request['request_id']}",
            "",
            f"- Phase: `{request['phase_classification']}`",
            f"- Current coefficients: `{request['current_coefficients']}`",
            f"- Aggregate raw candidate: `{request['aggregate_proposed_raw_candidate']}`",
            f"- Aggregate stepped coefficients: `{request['aggregate_proposed_stepped_coefficients']}`",
            f"- Aggregate equal gain: `{request['aggregate_equal_gain']:.3f} bps`",
            f"- Aggregate traffic-weighted gain: `{request['aggregate_traffic_weighted_gain']:.3f} bps`",
            f"- Relative gain: `{request['relative_gain']:.6f}`",
            f"- Absolute gate pass: `{request['absolute_gate_pass']}`",
            f"- Relative gate pass: `{request['relative_gate_pass']}`",
            f"- Would apply: `{request['would_apply']}`",
            f"- Actual applied: `{request['actual_applied']}`",
            f"- Diagnoses: `{', '.join(request['diagnoses'])}`",
            f"- Path-sensitive would apply: `{request['path_sensitive_would_apply']}`",
            f"- Best single-path gain: `{request['best_single_path_gain']:.3f} bps` on path `{request['best_single_path_id']}`",
        ])
        if request["changed_or_impaired_path_id"] is not None:
            lines.append(
                f"- Changed/impaired path gain: `{request['changed_or_impaired_path_gain']:.3f} bps` on path `{request['changed_or_impaired_path_id']}`"
            )
        lines.extend([
            "",
            "| Path | Physical | Weight | Current Score | Candidate Score | Gain (bps) | Effect |",
            "| --- | --- | ---: | ---: | ---: | ---: | --- |",
        ])
        for path_row in request["per_path"]:
            lines.append(
                "| {path_id} | {physical} | {weight:.6f} | {current:.3f} | {candidate:.3f} | {gain:.3f} | {effect} |".format(
                    path_id=path_row["path_id"],
                    physical=path_row.get("physical_path") or "unknown",
                    weight=path_row["weight"],
                    current=path_row["current_score"],
                    candidate=path_row["candidate_score"],
                    gain=path_row["gain_bps"],
                    effect=path_row["effect"],
                )
            )
        if request["recommendation"]:
            lines.extend(["", request["recommendation"]])
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--md-out", type=Path)
    args = parser.parse_args()

    report = analyze(args.session)
    session = args.session.resolve()
    json_out = args.json_out or (session / "fig7_decision_objective_audit.json")
    md_out = args.md_out or (session / "fig7_decision_objective_audit.md")
    json_out.parent.mkdir(parents=True, exist_ok=True)
    md_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md_out.write_text(render_markdown(report), encoding="utf-8")
    print(f"[fig7_audit] wrote {json_out}")
    print(f"[fig7_audit] wrote {md_out}")


if __name__ == "__main__":
    main()
