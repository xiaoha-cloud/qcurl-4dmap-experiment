#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export QACCESS_EXPERIMENT_FAMILY=clean_controlled
export QACCESS_CONTROLLER_VARIANT=qaccess_t
export QACCESS_TARGET_MODE=delta_bw_1s
export QACCESS_WORKER_TARGET_MODE=delta_bw_1s
export QACCESS_WORKER_MODEL="${QACCESS_WORKER_MODEL:-$ROOT/derived/qaccess_t_qserver_sender/qaccess_t_model_delta_bw_1s.pkl}"
export QACCESS_WORKER_MODEL_METADATA="${QACCESS_WORKER_MODEL_METADATA:-$ROOT/derived/qaccess_t_qserver_sender/qaccess_t_qserver_sender_report.json}"
export QACCESS_PROFILE_KIND=bandwidth
export DETERIORATION_PROFILE="${BW_PROFILE:-${DETERIORATION_PROFILE:-$ROOT/scripts/mininet/bw_profile.clean_20_30_10_200s.env}}"
export BW_PROFILE="$DETERIORATION_PROFILE"
export TC_BW_FIXED_DELAY_MS=40
export TC_BW_FIXED_LOSS_PERCENT=0
export SCENARIO="${SCENARIO:-clean_equal_paths}"
export QACCESS_SESSION_KIND=clean_bandwidth
export QACCESS_BASELINE_LABEL=clean_bandwidth_baseline
export QACCESS_DYNAMIC_LABEL=clean_bandwidth_qaccess_t
export QACCESS_PHASE2_STATE_DIR="${QACCESS_PHASE2_STATE_DIR:-$ROOT/derived/qaccess_clean_bandwidth_runtime}"
export QACCESS_EXECUTION_MODE="${QACCESS_EXECUTION_MODE:-active}"
export QACCESS_GATE_MODE="${QACCESS_GATE_MODE:-hybrid}"
export QACCESS_GATE_POLICY=legacy
export QACCESS_TRIGGER_MODE=legacy_buffer_full
exec "$ROOT/scripts/mininet/run_qaccess_t_combined_deterioration_eval.sh" "$@"
