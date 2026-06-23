#!/usr/bin/env python3
"""Summarize Fig.7 changed-path-priority runtime shadow artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def analyze(session: Path) -> dict[str, Any]:
    session = session.resolve()
    processed = session / "fig7_qaccess_t_dynamic" / "processed_buffers"
    artifacts = sorted(processed.glob("qaccess_changed_path_priority_*.json"))
    requests: list[dict[str, Any]] = []
    for artifact in artifacts:
        doc = _json(artifact)
        agg = doc["aggregate_decision"]
        changed = doc["changed_path_priority_decision"]
        requests.append({
            "request_id": doc["request_id"],
            "phase": doc["phase_classification"],
            "aggregate_gain_bps": agg["aggregate_gain_bps"],
            "aggregate_would_apply": agg["aggregate_would_apply"],
            "changed_path_candidate": changed["coefficients"],
            "changed_path_weighted_gain_bps": changed["changed_path_weighted_gain_bps"],
            "other_path_loss_bps": changed["other_path_loss_bps"],
            "changed_path_priority_would_apply": changed["fig7_changed_path_would_apply"],
            "diagnoses": doc["diagnoses"],
        })
    pre_ok = all(not row["changed_path_priority_would_apply"] for row in requests if row["phase"] == "PRE_DETERIORATION")
    during_any = any(row["changed_path_priority_would_apply"] for row in requests if row["phase"] == "DURING_DETERIORATION")
    post_ok = all(not row["changed_path_priority_would_apply"] for row in requests if row["phase"] == "POST_DETERIORATION")
    return {
        "session": str(session),
        "request_count": len(requests),
        "requests": requests,
        "summary": {
            "pre_requests_blocked": pre_ok,
            "during_any_would_apply": during_any,
            "post_requests_blocked": post_ok,
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Fig.7 Changed-Path Runtime Shadow Summary",
        "",
        f"Session: `{Path(report['session']).name}`",
        "",
        "| Request | Phase | Aggregate Gain (bps) | Aggregate Would Apply | Changed-Path Candidate | Changed-Path Weighted Gain | Other Path Loss | Changed-Path Would Apply | Diagnoses |",
        "| --- | --- | ---: | --- | --- | ---: | ---: | --- | --- |",
    ]
    for row in report["requests"]:
        lines.append(
            "| {request_id} | {phase} | {agg:.3f} | {agg_apply} | {candidate} | {weighted:.3f} | {loss:.3f} | {cp_apply} | {diagnoses} |".format(
                request_id=row["request_id"],
                phase=row["phase"],
                agg=row["aggregate_gain_bps"],
                agg_apply=row["aggregate_would_apply"],
                candidate=row["changed_path_candidate"],
                weighted=row["changed_path_weighted_gain_bps"],
                loss=row["other_path_loss_bps"],
                cp_apply=row["changed_path_priority_would_apply"],
                diagnoses=", ".join(row["diagnoses"]),
            )
        )
    lines.extend([
        "",
        "## Summary",
        "",
        f"- PRE requests remain blocked: `{report['summary']['pre_requests_blocked']}`",
        f"- DURING request would apply: `{report['summary']['during_any_would_apply']}`",
        f"- POST requests remain blocked: `{report['summary']['post_requests_blocked']}`",
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
    json_out = args.json_out or (session / "fig7_changed_path_runtime_summary.json")
    md_out = args.md_out or (session / "fig7_changed_path_runtime_summary.md")
    json_out.parent.mkdir(parents=True, exist_ok=True)
    md_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    md_out.write_text(render_markdown(report), encoding="utf-8")
    print(f"[fig7_changed_path_summary] wrote {json_out}")
    print(f"[fig7_changed_path_summary] wrote {md_out}")


if __name__ == "__main__":
    main()
