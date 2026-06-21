#!/usr/bin/env python3
"""Audit a changed-path-priority diagnostic objective for Fig.7 sessions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

DEFAULT_CHANGED_PATH_IDS = (3,)
DEFAULT_CHANGED_PATH_GAIN_BPS = 100000.0
DEFAULT_MIN_AGGREGATE_GAIN_BPS = 0.0
DEFAULT_MAX_OTHER_PATH_LOSS_RATIO = 0.75
DEFAULT_MAX_OTHER_PATH_LOSS_BPS = 200000.0


def _json(path: Path) -> dict[str, Any] | list[dict[str, Any]]:
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


def _aggregate_labels(
    path_entries: list[dict[str, Any]],
    aggregate_would_apply: bool,
    absolute_gate_pass: bool,
    relative_gate_pass: bool,
) -> list[str]:
    positives = [row for row in path_entries if float(row["gain_bps"]) > 0]
    negatives = [row for row in path_entries if float(row["gain_bps"]) < 0]
    labels: list[str] = []
    if aggregate_would_apply:
        labels.append("aggregate_accepts")
    elif positives and negatives:
        labels.append("aggregate_blocks_due_to_cross_path_tradeoff")
    elif not positives:
        labels.append("aggregate_blocks_no_path_improvement")
    elif not absolute_gate_pass or not relative_gate_pass:
        labels.append("aggregate_blocks_below_threshold")
    return labels


def _recommendation(path_entries: list[dict[str, Any]], aggregate_gain_bps: float) -> str | None:
    changed_positive = any(
        row.get("is_changed_path") and float(row["gain_bps"]) > 0
        for row in path_entries
    )
    other_negative = any(
        (not row.get("is_changed_path")) and float(row["gain_bps"]) < 0
        for row in path_entries
    )
    if changed_positive and other_negative and aggregate_gain_bps > 0:
        return (
            "The Fig.7 capacity-change decision is blocked by the aggregate multipath objective. "
            "The model detects a beneficial direction for the changed path, but the aggregate scorer "
            "rejects it because another active path degrades. This suggests that Fig.7 may require a "
            "capacity-change-specific objective or a bottleneck/changing-path-aware gate."
        )
    return None


def analyze(
    session: Path,
    changed_path_ids: set[int],
    changed_path_gain_bps: float = DEFAULT_CHANGED_PATH_GAIN_BPS,
    min_aggregate_gain_bps: float = DEFAULT_MIN_AGGREGATE_GAIN_BPS,
    max_other_path_loss_ratio: float = DEFAULT_MAX_OTHER_PATH_LOSS_RATIO,
    max_other_path_loss_bps: float = DEFAULT_MAX_OTHER_PATH_LOSS_BPS,
) -> dict[str, Any]:
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
    min_relative_gain = float(metadata.get("min_relative_gain", 0.0))
    min_delta_gain_bps = float(metadata.get("min_delta_gain_bps", 0.0))

    requests: list[dict[str, Any]] = []
    for worker in worker_rows:
        request_id = str(worker["request_id"])
        aggregate_df = pd.read_csv(aggregate_files[request_id])
        per_path_df = pd.read_csv(per_path_files[request_id])
        eligibility_rows = {int(row["path_id"]): row for row in _json(eligibility_files[request_id])}

        current_row = aggregate_df[aggregate_df["is_current_tuple"] == 1].iloc[0]
        current_coeffs = _coeff_dict(worker.get("current_coefficients")) or _coeff_dict(current_row)

        aggregate_raw_candidate = (
            worker.get("traffic_weighted_proposed_candidate")
            or worker.get("equal_weight_proposed_candidate")
            or _coeff_dict(aggregate_df.sort_values("byte_weighted_rank").iloc[0])
        )
        aggregate_stepped_candidate = (
            worker.get("traffic_weighted_proposed_stepped_coefficients")
            or worker.get("equal_weight_proposed_stepped_coefficients")
            or aggregate_raw_candidate
        )

        aggregate_gain_bps = float(worker.get("traffic_weighted_gain", worker.get("equal_weight_gain", 0.0)))
        relative_gain = float(worker.get("relative_gain", 0.0))
        absolute_gate_pass = aggregate_gain_bps >= min_delta_gain_bps
        relative_gate_pass = relative_gain >= min_relative_gain
        aggregate_would_apply = bool(worker.get("would_apply_under_gate", False))
        actual_applied = bool(worker.get("actual_applied", False))

        candidate_rows: list[dict[str, Any]] = []
        for _, aggregate_row in aggregate_df.iterrows():
            coeffs = _coeff_dict(aggregate_row)
            path_entries: list[dict[str, Any]] = []
            changed_path_raw_gain = 0.0
            changed_path_weighted_gain = 0.0
            other_path_loss_bps = 0.0
            other_path_gain_bps = 0.0

            for path_id, path_df in per_path_df.groupby("path_id"):
                current_path = path_df[path_df["is_current_tuple"] == 1].iloc[0]
                candidate_match = _match_coeffs(path_df, coeffs)
                candidate_path = candidate_match.iloc[0] if not candidate_match.empty else current_path
                gain_bps = float(candidate_path["path_pred_candidate"]) - float(current_path["path_pred_candidate"])
                weight = float((worker.get("path_weights") or {}).get(str(path_id), 0.0))
                is_changed = int(path_id) in changed_path_ids
                effect = "helps" if gain_bps > 0 else ("hurts" if gain_bps < 0 else "neutral")
                path_entries.append({
                    "path_id": int(path_id),
                    "physical_path": eligibility_rows.get(int(path_id), {}).get("physical_path"),
                    "weight": weight,
                    "current_score": float(current_path["path_pred_candidate"]),
                    "candidate_score": float(candidate_path["path_pred_candidate"]),
                    "gain_bps": gain_bps,
                    "weighted_gain_bps": weight * gain_bps,
                    "effect": effect,
                    "is_changed_path": is_changed,
                })
                if is_changed:
                    changed_path_raw_gain += gain_bps
                    changed_path_weighted_gain += weight * gain_bps
                elif gain_bps < 0:
                    other_path_loss_bps += -gain_bps
                else:
                    other_path_gain_bps += gain_bps

            aggregate_gain_for_candidate = float(aggregate_row.get("byte_weighted_gain", aggregate_gain_bps))
            changed_path_gate_pass = changed_path_weighted_gain >= changed_path_gain_bps
            aggregate_safety_pass = aggregate_gain_for_candidate > min_aggregate_gain_bps
            other_path_loss_pass = (
                other_path_loss_bps <= max_other_path_loss_ratio * changed_path_weighted_gain
                and other_path_loss_bps <= max_other_path_loss_bps
            )
            changed_path_would_apply = (
                changed_path_gate_pass and aggregate_safety_pass and other_path_loss_pass
            )

            candidate_rows.append({
                "coefficients": coeffs,
                "aggregate_gain_bps": aggregate_gain_for_candidate,
                "changed_path_raw_gain_bps": changed_path_raw_gain,
                "changed_path_weighted_gain_bps": changed_path_weighted_gain,
                "other_path_loss_bps": other_path_loss_bps,
                "other_path_gain_bps": other_path_gain_bps,
                "changed_path_gate_pass": changed_path_gate_pass,
                "aggregate_safety_pass": aggregate_safety_pass,
                "other_path_loss_pass": other_path_loss_pass,
                "changed_path_would_apply": changed_path_would_apply,
                "path_entries": path_entries,
            })

        safe_candidates = [row for row in candidate_rows if row["aggregate_safety_pass"] and row["other_path_loss_pass"]]
        if safe_candidates:
            changed_path_best = sorted(
                safe_candidates,
                key=lambda row: (
                    -row["changed_path_weighted_gain_bps"],
                    -row["aggregate_gain_bps"],
                    row["other_path_loss_bps"],
                ),
            )[0]
        else:
            changed_path_best = sorted(
                candidate_rows,
                key=lambda row: (
                    -row["changed_path_weighted_gain_bps"],
                    -row["aggregate_gain_bps"],
                    row["other_path_loss_bps"],
                ),
            )[0]

        aggregate_raw_match = _match_coeffs(aggregate_df, aggregate_raw_candidate)
        aggregate_raw_row = aggregate_raw_match.iloc[0] if not aggregate_raw_match.empty else aggregate_df.sort_values("byte_weighted_rank").iloc[0]
        aggregate_stepped_match = _match_coeffs(aggregate_df, aggregate_stepped_candidate)
        aggregate_stepped_row = aggregate_stepped_match.iloc[0] if not aggregate_stepped_match.empty else aggregate_raw_row

        path_entries_raw = next(
            row["path_entries"] for row in candidate_rows
            if row["coefficients"] == aggregate_raw_candidate
        )
        diagnoses = _aggregate_labels(path_entries_raw, aggregate_would_apply, absolute_gate_pass, relative_gate_pass)
        if changed_path_best["changed_path_would_apply"]:
            diagnoses.append("changed_path_priority_would_apply")
        elif changed_path_best["changed_path_weighted_gain_bps"] < changed_path_gain_bps:
            diagnoses.append("changed_path_priority_blocks_no_changed_path_gain")
        elif changed_path_best["aggregate_gain_bps"] <= min_aggregate_gain_bps:
            diagnoses.append("changed_path_priority_blocks_negative_aggregate")
        else:
            diagnoses.append("changed_path_priority_blocks_excessive_other_path_loss")

        raw_gain = float(aggregate_raw_row.get("byte_weighted_gain", aggregate_gain_bps))
        stepped_gain = float(aggregate_stepped_row.get("byte_weighted_gain", aggregate_gain_bps))
        if aggregate_raw_candidate != aggregate_stepped_candidate:
            if (raw_gain >= min_delta_gain_bps) == (stepped_gain >= min_delta_gain_bps):
                diagnoses.append("step_limit_not_decisive")
            else:
                diagnoses.append("step_limit_decisive")

        recommendation = _recommendation(path_entries_raw, aggregate_gain_bps)

        requests.append({
            "request_id": request_id,
            "phase_classification": worker.get("request_classification"),
            "current_coefficients": current_coeffs,
            "aggregate_candidate": aggregate_raw_candidate,
            "aggregate_stepped_candidate": aggregate_stepped_candidate,
            "aggregate_gain_bps": aggregate_gain_bps,
            "relative_gain": relative_gain,
            "absolute_gate_pass": absolute_gate_pass,
            "relative_gate_pass": relative_gate_pass,
            "aggregate_would_apply": aggregate_would_apply,
            "actual_applied": actual_applied,
            "path_weights": worker.get("path_weights") or {},
            "aggregate_best_candidate": {
                "coefficients": _coeff_dict(aggregate_raw_row),
                "aggregate_gain_bps": float(aggregate_raw_row.get("byte_weighted_gain", aggregate_gain_bps)),
            },
            "changed_path_priority_best_candidate": {
                "coefficients": changed_path_best["coefficients"],
                "changed_path_raw_gain_bps": changed_path_best["changed_path_raw_gain_bps"],
                "changed_path_weighted_gain_bps": changed_path_best["changed_path_weighted_gain_bps"],
                "other_path_loss_bps": changed_path_best["other_path_loss_bps"],
                "other_path_gain_bps": changed_path_best["other_path_gain_bps"],
                "aggregate_gain_bps": changed_path_best["aggregate_gain_bps"],
                "changed_path_gate_pass": changed_path_best["changed_path_gate_pass"],
                "aggregate_safety_pass": changed_path_best["aggregate_safety_pass"],
                "other_path_loss_pass": changed_path_best["other_path_loss_pass"],
                "fig7_changed_path_would_apply": changed_path_best["changed_path_would_apply"],
            },
            "per_path_for_aggregate_candidate": path_entries_raw,
            "diagnoses": diagnoses,
            "recommendation": recommendation,
        })

    return {
        "session": str(session),
        "gate_mode": metadata.get("gate_mode"),
        "execution_mode": metadata.get("execution_mode"),
        "changed_path_ids": sorted(changed_path_ids),
        "changed_path_gain_bps": changed_path_gain_bps,
        "min_aggregate_gain_bps": min_aggregate_gain_bps,
        "max_other_path_loss_ratio": max_other_path_loss_ratio,
        "max_other_path_loss_bps": max_other_path_loss_bps,
        "requests": requests,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Fig.7 Changed-Path-Priority Audit",
        "",
        f"Session: `{Path(report['session']).name}`",
        "",
        f"Gate mode: `{report['gate_mode']}`",
        f"Execution mode: `{report['execution_mode']}`",
        f"Changed path ids: `{report['changed_path_ids']}`",
        "",
        "| Request | Phase | Aggregate Gain (bps) | Aggregate Would Apply | Changed Path Raw Gain | Changed Path Weighted Gain | Other Path Loss | Aggregate Safety Pass | Changed-Path Would Apply | Diagnoses |",
        "| --- | --- | ---: | --- | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for request in report["requests"]:
        best = request["changed_path_priority_best_candidate"]
        lines.append(
            "| {request_id} | {phase} | {agg:.3f} | {agg_apply} | {raw:.3f} | {weighted:.3f} | {loss:.3f} | {agg_safe} | {cp_apply} | {diagnoses} |".format(
                request_id=request["request_id"],
                phase=request["phase_classification"],
                agg=request["aggregate_gain_bps"],
                agg_apply=request["aggregate_would_apply"],
                raw=best["changed_path_raw_gain_bps"],
                weighted=best["changed_path_weighted_gain_bps"],
                loss=best["other_path_loss_bps"],
                agg_safe=best["aggregate_safety_pass"],
                cp_apply=best["fig7_changed_path_would_apply"],
                diagnoses=", ".join(request["diagnoses"]),
            )
        )
    for request in report["requests"]:
        best = request["changed_path_priority_best_candidate"]
        lines.extend([
            "",
            f"## {request['request_id']}",
            "",
            f"- Phase: `{request['phase_classification']}`",
            f"- Aggregate candidate: `{request['aggregate_candidate']}`",
            f"- Aggregate stepped candidate: `{request['aggregate_stepped_candidate']}`",
            f"- Aggregate gain: `{request['aggregate_gain_bps']:.3f} bps`",
            f"- Relative gain: `{request['relative_gain']:.6f}`",
            f"- Absolute gate pass: `{request['absolute_gate_pass']}`",
            f"- Relative gate pass: `{request['relative_gate_pass']}`",
            f"- Aggregate would apply: `{request['aggregate_would_apply']}`",
            f"- Actual applied: `{request['actual_applied']}`",
            f"- Changed-path-priority best candidate: `{best['coefficients']}`",
            f"- Changed path raw gain: `{best['changed_path_raw_gain_bps']:.3f} bps`",
            f"- Changed path weighted gain: `{best['changed_path_weighted_gain_bps']:.3f} bps`",
            f"- Other path loss: `{best['other_path_loss_bps']:.3f} bps`",
            f"- Aggregate safety pass: `{best['aggregate_safety_pass']}`",
            f"- Changed-path-priority would apply: `{best['fig7_changed_path_would_apply']}`",
            f"- Diagnoses: `{', '.join(request['diagnoses'])}`",
            "",
            "| Path | Physical | Weight | Current Score | Candidate Score | Gain (bps) | Effect |",
            "| --- | --- | ---: | ---: | ---: | ---: | --- |",
        ])
        for row in request["per_path_for_aggregate_candidate"]:
            lines.append(
                "| {path_id} | {physical} | {weight:.6f} | {current:.3f} | {candidate:.3f} | {gain:.3f} | {effect} |".format(
                    path_id=row["path_id"],
                    physical=row.get("physical_path") or "unknown",
                    weight=row["weight"],
                    current=row["current_score"],
                    candidate=row["candidate_score"],
                    gain=row["gain_bps"],
                    effect=row["effect"],
                )
            )
        if request["recommendation"]:
            lines.extend(["", request["recommendation"]])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--changed-path-ids", nargs="+", type=int, default=list(DEFAULT_CHANGED_PATH_IDS))
    parser.add_argument("--changed-path-gain-bps", type=float, default=DEFAULT_CHANGED_PATH_GAIN_BPS)
    parser.add_argument("--min-aggregate-gain-bps", type=float, default=DEFAULT_MIN_AGGREGATE_GAIN_BPS)
    parser.add_argument("--max-other-path-loss-ratio", type=float, default=DEFAULT_MAX_OTHER_PATH_LOSS_RATIO)
    parser.add_argument("--max-other-path-loss-bps", type=float, default=DEFAULT_MAX_OTHER_PATH_LOSS_BPS)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--md-out", type=Path)
    args = parser.parse_args()

    report = analyze(
        session=args.session,
        changed_path_ids=set(args.changed_path_ids),
        changed_path_gain_bps=args.changed_path_gain_bps,
        min_aggregate_gain_bps=args.min_aggregate_gain_bps,
        max_other_path_loss_ratio=args.max_other_path_loss_ratio,
        max_other_path_loss_bps=args.max_other_path_loss_bps,
    )
    session = args.session.resolve()
    json_out = args.json_out or (session / "fig7_changed_path_priority_audit.json")
    md_out = args.md_out or (session / "fig7_changed_path_priority_audit.md")
    json_out.parent.mkdir(parents=True, exist_ok=True)
    md_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md_out.write_text(render_markdown(report), encoding="utf-8")
    print(f"[fig7_changed_path] wrote {json_out}")
    print(f"[fig7_changed_path] wrote {md_out}")


if __name__ == "__main__":
    main()
