#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export QACCESS_WORKER_MODEL="${QACCESS_WORKER_MODEL:-$ROOT/derived/qaccess_t_qserver_sender/qaccess_t_model_delta_bw_1s.pkl}"
export QACCESS_WORKER_MODEL_METADATA="${QACCESS_WORKER_MODEL_METADATA:-$ROOT/derived/qaccess_t_qserver_sender/qaccess_t_qserver_sender_report.json}"
export QACCESS_WORKER_TARGET_MODE=delta_bw_1s
export QACCESS_EXECUTION_MODE=active
export QACCESS_GATE_MODE=hybrid
export QACCESS_MIN_RELATIVE_GAIN="${QACCESS_MIN_RELATIVE_GAIN:-0.03}"
export QACCESS_GATE_BPS="${QACCESS_GATE_BPS:-100000}"
export QACCESS_POST_UPDATE_OBSERVE_SEC="${QACCESS_POST_UPDATE_OBSERVE_SEC:-15}"
export DETERIORATION_PROFILE="$ROOT/scripts/mininet/combined_deterioration_profile_90_150.env"
exec "$ROOT/scripts/mininet/run_qaccess_t_combined_deterioration_eval.sh" "$@"
