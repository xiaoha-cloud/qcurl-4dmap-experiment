#!/usr/bin/env python3
"""Validate diagnostic Q-ACCeSS-T shadow or active experiment artifacts."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

EXPECTED_MODEL = "qaccess_t_model_delta_bw_1s.pkl"
INITIAL_COEFFS = {"alpha": 0.6, "beta": 0.3, "gamma": 0.1}


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _json_lines(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def request_serials(request_ids: list[str]) -> list[int]:
    serials = []
    for request_id in request_ids:
        match = re.search(r"_(\d+)$", request_id)
        if not match:
            raise ValueError(f"request ID has no trailing serial: {request_id}")
        serials.append(int(match.group(1)))
    return serials


def continuous_request_serials(request_ids: list[str]) -> bool:
    serials = request_serials(request_ids)
    return bool(serials) and serials == list(range(1, len(serials) + 1))


def classify_elapsed(elapsed: float, start: float, end: float) -> str:
    if elapsed < start:
        return "PRE_DETERIORATION"
    if elapsed < end:
        return "DURING_DETERIORATION"
    return "POST_DETERIORATION"


def _tc_start_and_complete(session: Path) -> tuple[float | None, bool]:
    logs = sorted((session / "combined_qaccess_t_dynamic").rglob("tc_deterioration*.log"))
    if not logs:
        return None, False
    text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in logs)
    match = re.search(r"\[([^]]+)\].*step 1/3 at=0s", text)
    start_ms = None
    if match:
        start_ms = datetime.fromisoformat(match.group(1)).timestamp() * 1000
    complete = all(token in text for token in ("step 1/3", "step 2/3", "step 3/3", "finished all steps"))
    complete = complete and bool(re.search(r"exiting status=0 .*completed=1", text))
    return start_ms, complete


def _coefficients_match_initial(doc: dict[str, Any]) -> bool:
    return all(abs(float(doc.get(key, -1)) - value) < 1e-9 for key, value in INITIAL_COEFFS.items())


def scoring_path_coverage(scored_path_sets: list[set[int]], media_paths: list[int]) -> tuple[bool, bool]:
    union = set().union(*scored_path_sets) if scored_path_sets else set()
    all_media_seen = bool(scored_path_sets) and set(media_paths).issubset(union)
    multipath_request_seen = any(len(paths) > 1 for paths in scored_path_sets)
    return all_media_seen, multipath_request_seen


def validate_session(session: Path, mode: str, deterioration_start: float, deterioration_end: float) -> int:
    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"{'PASS' if ok else 'FAIL'} {label}{(': ' + detail) if detail else ''}")
        failures += 0 if ok else 1

    ready_path = session / "worker_ready.json"
    check("worker_ready.json exists", ready_path.is_file(), str(ready_path))
    ready = _json(ready_path) if ready_path.is_file() else {}
    resolved_model = str(ready.get("resolved_model_path") or "")
    verified_target = str(ready.get("verified_model_target") or ready.get("model_target") or "")
    compatible = ready.get("model_target_compatible") is True
    shadow = bool(ready.get("shadow_mode", ready.get("shadow", False)))
    check("correct delta model", Path(resolved_model).name == EXPECTED_MODEL, resolved_model)
    check("verified target is delta_bw_1s", verified_target == "delta_bw_1s", verified_target)
    check("model target compatibility", compatible, str(ready.get("compatibility_status", compatible)))
    check("execution mode", str(ready.get("execution_mode") or "") == mode, str(ready.get("execution_mode")))

    owner_rows = _json_lines(session / "qaccess_owner_audit.jsonl")
    owners = [
        row for row in owner_rows
        if row.get("phase2_owner") is True and row.get("controller_created") is True
    ]
    check("exactly one qserver Phase 2 owner", len(owners) == 1, f"count={len(owners)}")
    owner = owners[0] if len(owners) == 1 else {}
    check("owner is server_downlink_sender", owner.get("endpoint_role") == "server_downlink_sender", str(owner.get("endpoint_role")))
    owner_pid = str(owner.get("pid") or "")
    check("controller PID equals qserver PID", bool(owner_pid) and str(owner.get("controller_pid") or "") == owner_pid, owner_pid)
    owner_state_dir = str(owner.get("phase2_state_dir") or "")
    check("owner state dir is absolute", bool(owner_state_dir) and Path(owner_state_dir).is_absolute(), owner_state_dir)
    timeline_rows: list[dict[str, Any]] = []
    for timeline in (session / "combined_qaccess_t_dynamic").glob("experiment_timeline_*.jsonl"):
        timeline_rows.extend(_json_lines(timeline))
    identities = [row for row in timeline_rows if row.get("event") == "phase2_identity"]
    for role in ("client_push_publisher", "client_pull_receiver"):
        matches = [row for row in identities if row.get("endpoint_role") == role]
        check(f"{role} is Phase 2 disabled", len(matches) == 1 and matches[0].get("phase2_enabled") is False and matches[0].get("phase2_owner") is False and matches[0].get("controller_created") is False)
    ingress = [row for row in owner_rows if row.get("lease_decision") == "publisher_ingress_disabled"]
    check("server publisher ingress is Phase 2 disabled", bool(ingress) and all(row.get("phase2_enabled") is False and row.get("phase2_owner") is False and row.get("controller_created") is False for row in ingress))

    trigger_rows = _json_lines(session / "qaccess_trigger_audit.jsonl")
    failed_writes = [r for r in trigger_rows if r.get("trigger_decision") == "request_write_failed"]
    written = [r for r in trigger_rows if r.get("trigger_decision") == "request_written"]
    request_ids = [str(r.get("request_id")) for r in written if r.get("request_id")]
    check("no request_write_failed", not failed_writes, f"count={len(failed_writes)}")
    try:
        continuous = continuous_request_serials(request_ids)
        serial_detail = str(request_serials(request_ids))
    except ValueError as exc:
        continuous, serial_detail = False, str(exc)
    check("continuous request serials", continuous, serial_detail)

    dynamic_dir = session / "combined_qaccess_t_dynamic"
    sample_files = sorted(dynamic_dir.rglob("qaccess_runtime_samples_*_all_paths.csv"))
    sample_rows: list[dict[str, str]] = []
    for path in sample_files:
        with path.open(newline="", encoding="utf-8") as handle:
            sample_rows.extend(csv.DictReader(handle))
    if not sample_rows:
        tail = dynamic_dir / "qaccess_runtime_samples_tail.csv.gz"
        if tail.is_file():
            with gzip.open(tail, "rt", newline="", encoding="utf-8") as handle:
                sample_rows.extend(csv.DictReader(handle))
    check("all-path runtime samples exist", bool(sample_rows), f"rows={len(sample_rows)}")
    check("all samples come from qserver owner PID", bool(sample_rows) and all(row.get("producer_pid") == owner_pid for row in sample_rows), owner_pid)
    check("all samples have downlink sender role", bool(sample_rows) and all(row.get("endpoint_role") == "server_downlink_sender" for row in sample_rows))
    check("all samples use owner state dir", bool(sample_rows) and all(row.get("phase2_state_dir") == owner_state_dir for row in sample_rows))
    check("sample transport identity is populated", bool(sample_rows) and all(row.get("connection_id") and row.get("rtmp_session_id") and row.get("stream_key") and row.get("local_endpoint") and row.get("remote_endpoint") for row in sample_rows))
    rows_by_path: dict[int, list[dict[str, str]]] = {}
    for row in sample_rows:
        rows_by_path.setdefault(int(float(row.get("path_id") or 0)), []).append(row)
    eligible_paths: list[int] = []
    idle_paths: list[int] = []
    for path_id, rows in sorted(rows_by_path.items()):
        sender = [int(float(row.get("sender_bytes_total") or 0)) for row in rows]
        inflight = [int(float(row.get("inflight_bytes") or 0)) for row in rows]
        cwnd = [int(float(row.get("cwnd_bytes") or 0)) for row in rows]
        bw = [float(row.get("bw_bps") or 0) for row in rows]
        loss = [float(row.get("loss_rate") or 0) for row in rows]
        retrans = [float(row.get("retrans_bytes_delta") or 0) for row in rows]
        endpoint = next((row.get("remote_endpoint", "") for row in rows if row.get("remote_endpoint")), "")
        delta = sum(max(0, after-before) for before, after in zip(sender, sender[1:]))
        reset = any(after < before for before, after in zip(sender, sender[1:]))
        telemetry_valid = bool(rows) and all("sender_bytes_total" in row and "cwnd_bytes" in row and "inflight_bytes" in row for row in rows)
        eligible = len(sender) >= 2 and delta > 0 and telemetry_valid
        if not telemetry_valid:
            classification = "BROKEN_TELEMETRY_PATH"
        elif len(sender) < 2:
            classification = "INSUFFICIENT_DATA_PATH"
        elif delta <= 0:
            classification = "IDLE_CONTROL_PATH"
        else:
            classification = "ACTIVE_MEDIA_PATH"
        (eligible_paths if eligible else idle_paths).append(path_id)
        print(
            f"INFO path={path_id} endpoint={endpoint} rows={len(rows)} eligible={str(eligible).lower()} "
            f"classification={classification} sender_first={sender[0] if sender else 0} "
            f"sender_last={sender[-1] if sender else 0} sender_delta={delta} counter_reset={str(reset).lower()} "
            f"bw_range={min(bw, default=0):.3f}..{max(bw, default=0):.3f} "
            f"inflight_range={min(inflight, default=0)}..{max(inflight, default=0)} "
            f"cwnd_range={min(cwnd, default=0)}..{max(cwnd, default=0)} "
            f"loss_range={min(loss, default=0):.6f}..{max(loss, default=0):.6f} "
            f"retrans_range={min(retrans, default=0):.3f}..{max(retrans, default=0):.3f}"
        )
    check("authoritative media paths exist", bool(eligible_paths), str(eligible_paths))
    check(
        "multiple media paths are eligible for aggregate scoring",
        len(eligible_paths) >= 2,
        str(eligible_paths),
    )
    check("idle/control paths are classified separately", bool(idle_paths), str(idle_paths))
    live_congestion = all(
        max(int(float(row.get("inflight_bytes") or 0)) for row in rows_by_path[path_id]) > 0
        and max(int(float(row.get("cwnd_bytes") or 0)) for row in rows_by_path[path_id]) > 0
        for path_id in eligible_paths
    ) if eligible_paths else False
    check("eligible media paths have live congestion telemetry", live_congestion)
    check("loss/retransmission instrumentation is present", bool(sample_rows) and all("loss_rate" in row and "retrans_bytes_delta" in row for row in sample_rows))
    path_b_ids = [path_id for path_id, rows in rows_by_path.items() if any((row.get("remote_endpoint") or "").startswith("10.0.2.") for row in rows)]
    path_b_reflects_impairment = any(
        max(float(row.get("loss_rate") or 0) for row in rows_by_path[path_id]) > 0
        or len({float(row.get("owd_ms") or 0) for row in rows_by_path[path_id]}) > 1
        for path_id in path_b_ids
    )
    check("Path B telemetry reflects loss or delay variation", bool(path_b_ids) and path_b_reflects_impairment, str(path_b_ids))

    tc_start_ms, tc_complete = _tc_start_and_complete(session)
    check("TC deterioration completed all steps", tc_complete)
    classified: list[tuple[str, float, str]] = []
    if tc_start_ms is not None:
        for row in written:
            elapsed = (float(row.get("timestamp_ms", 0)) - tc_start_ms) / 1000
            classified.append((str(row.get("request_id")), elapsed, classify_elapsed(elapsed, deterioration_start, deterioration_end)))
    during = [row for row in classified if row[2] == "DURING_DETERIORATION"]
    check("request during deterioration", bool(during), f"window={deterioration_start:g}-{deterioration_end:g}s")
    for request_id, elapsed, classification in classified:
        print(f"INFO request={request_id} elapsed_s={elapsed:.3f} classification={classification}")

    score_files = sorted(session.rglob("qaccess_candidate_scores_*.csv"))
    check("candidate-score artifacts exist", bool(score_files), f"count={len(score_files)}")
    diverse = False
    for path in score_files:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
            scores = {
                float(row.get("mean_prediction") or row.get("byte_weighted_score") or 0)
                for row in rows
            }
        diverse = diverse or len(scores) > 1
    check("candidate predictions are non-constant", diverse)

    worker_rows = _json_lines(session / "worker.log")
    statuses = [str(row.get("status") or "").upper() for row in worker_rows]
    before_path = session / "combined_qaccess_t_dynamic_coeffs_before.json"
    after_path = session / "combined_qaccess_t_dynamic_coeffs_after.json"
    before = _json(before_path) if before_path.is_file() else {}
    after = _json(after_path) if after_path.is_file() else {}

    if mode == "shadow":
        check("shadow mode is true", shadow)
        check("shadow proposal passes gate", any(bool(r.get("would_apply")) for r in worker_rows))
        check("active coefficients remain initial", _coefficients_match_initial(before) and before == after)
        check("no real APPLIED result", "APPLIED" not in statuses)
        check("aggregate stepped proposals recorded", any(r.get("equal_weight_proposed_stepped_coefficients") and r.get("traffic_weighted_proposed_stepped_coefficients") for r in worker_rows))
        aggregate_audits = sorted(session.rglob("qaccess_multipath_shadow_audit_*.json"))
        check("aggregate Shadow artifacts exist", bool(aggregate_audits), f"count={len(aggregate_audits)}")
        per_path_artifacts = sorted(session.rglob("qaccess_candidate_scores_*_per_path.csv"))
        aggregate_artifacts = sorted(session.rglob("qaccess_candidate_scores_*_aggregate.csv"))
        eligibility_artifacts = sorted(session.rglob("qaccess_path_eligibility_*.json"))
        check("per-path candidate artifacts exist", bool(per_path_artifacts), f"count={len(per_path_artifacts)}")
        check("aggregate candidate artifacts exist", bool(aggregate_artifacts), f"count={len(aggregate_artifacts)}")
        check("path eligibility artifacts exist", bool(eligibility_artifacts), f"count={len(eligibility_artifacts)}")
        scored_path_sets = [set(int(x) for x in row.get("eligible_path_ids", [])) for row in worker_rows]
        all_media_seen, multipath_seen = scoring_path_coverage(scored_path_sets, eligible_paths)
        check("all run-level media paths enter scoring at least once", all_media_seen, str(scored_path_sets))
        check("at least one request uses aggregate multipath scoring", multipath_seen, str(scored_path_sets))
        check("idle paths are excluded from scoring", bool(scored_path_sets) and all(not set(idle_paths).intersection(paths) for paths in scored_path_sets), str(scored_path_sets))
        check("equal and traffic-weighted diagnostics recorded", bool(worker_rows) and all("equal_weight_gain" in row and "traffic_weighted_gain" in row and "aggregate_methods_agree" in row for row in worker_rows))
    else:
        check("shadow mode is false", not shadow)
        check("at least one APPLIED result", "APPLIED" in statuses)
        classifications = {request_id: (elapsed, label) for request_id, elapsed, label in classified}
        for row in worker_rows:
            if str(row.get("status") or "").upper() != "APPLIED":
                continue
            request_id = str(row.get("request_id") or "")
            elapsed, label = classifications.get(request_id, (float("nan"), "UNKNOWN"))
            print(f"INFO applied_request={request_id} elapsed_s={elapsed:.3f} classification={label}")
        check("runtime coefficients changed", bool(before) and bool(after) and before != after)
        diagnostics = session / "combined_qaccess_t_dynamic" / "control_law_diagnostics.csv"
        reload_confirmed = False
        if diagnostics.is_file():
            with diagnostics.open(newline="", encoding="utf-8") as handle:
                reload_confirmed = any(
                    abs(float(row.get("alpha", 0.6)) - 0.6) > 1e-9
                    or abs(float(row.get("beta", 0.3)) - 0.3) > 1e-9
                    or abs(float(row.get("gamma", 0.1)) - 0.1) > 1e-9
                    for row in csv.DictReader(handle)
                )
        check("controller coefficient reload confirmed", reload_confirmed, str(diagnostics))

    print("INFO temporal co-occurrence is not proof that deterioration caused an update")
    print(f"SUMMARY {'PASS' if failures == 0 else 'FAIL'} failures={failures}")
    return 0 if failures == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=("shadow", "active"))
    parser.add_argument("--deterioration-start", required=True, type=float)
    parser.add_argument("--deterioration-end", required=True, type=float)
    args = parser.parse_args()
    if args.deterioration_end <= args.deterioration_start:
        parser.error("deterioration end must be greater than start")
    sys.exit(validate_session(args.session.resolve(), args.mode, args.deterioration_start, args.deterioration_end))


if __name__ == "__main__":
    main()
