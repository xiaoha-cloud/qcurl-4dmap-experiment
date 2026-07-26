#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export QACCESS_EXPERIMENT_FAMILY=clean_controlled
export QACCESS_CONTROLLER_VARIANT=qaccess_t
export QACCESS_TARGET_MODE=delta_bw_1s
export QACCESS_WORKER_TARGET_MODE=delta_bw_1s
export QACCESS_WORKER_MODEL="${QACCESS_WORKER_MODEL:-$ROOT/derived/qaccess_t_qserver_sender/qaccess_t_model_delta_bw_1s.pkl}"
export QACCESS_WORKER_MODEL_METADATA="${QACCESS_WORKER_MODEL_METADATA:-$ROOT/derived/qaccess_t_qserver_sender/qaccess_t_qserver_sender_report.json}"
export QACCESS_PROFILE_KIND=none
export DETERIORATION_PROFILE=""
export SCENARIO="${SCENARIO:-clean_equal_paths}"
export QACCESS_SESSION_KIND=clean_stability
export QACCESS_BASELINE_LABEL=clean_stability_baseline
export QACCESS_DYNAMIC_LABEL=clean_stability_qaccess_t
export QACCESS_PHASE2_STATE_DIR="${QACCESS_PHASE2_STATE_DIR:-$ROOT/derived/qaccess_clean_stability_runtime}"
export QACCESS_EXECUTION_MODE="${QACCESS_EXECUTION_MODE:-active}"
export QACCESS_GATE_MODE="${QACCESS_GATE_MODE:-absolute}"
export QACCESS_GATE_POLICY="${QACCESS_GATE_POLICY:-objective_aware}"
export QACCESS_GATE_OBJECTIVE="${QACCESS_GATE_OBJECTIVE:-throughput}"
export QACCESS_TRIGGER_MODE="${QACCESS_TRIGGER_MODE:-objective_t}"
export QACCESS_GATE_BPS="${QACCESS_GATE_BPS:-500000}"
export QACCESS_MIN_OBJECTIVE_RELATIVE_IMPROVEMENT="${QACCESS_MIN_OBJECTIVE_RELATIVE_IMPROVEMENT:-0.05}"
exec "$ROOT/scripts/mininet/run_qaccess_t_combined_deterioration_eval.sh" "$@"
