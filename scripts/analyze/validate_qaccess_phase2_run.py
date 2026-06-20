#!/usr/bin/env python3
"""Validate diagnostic Q-ACCeSS-T shadow or active experiment artifacts."""

from __future__ import annotations

import argparse
import csv
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
            scores = {float(row["mean_prediction"]) for row in csv.DictReader(handle)}
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
        check("proposed stepped coefficients recorded", any(r.get("proposed_stepped_coefficients") for r in worker_rows))
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
