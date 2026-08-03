#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export QACCESS_CONTROLLER_VARIANT=qaccess_d
export QACCESS_WORKER_TARGET_MODE=delta_owd_1s
export QACCESS_WORKER_MODEL="${QACCESS_WORKER_MODEL:-$ROOT/derived/qaccess_d_qserver_sender/qaccess_d_model_delta_owd_1s.pkl}"
export QACCESS_WORKER_MODEL_METADATA="${QACCESS_WORKER_MODEL_METADATA:-$ROOT/derived/qaccess_d_qserver_sender/qaccess_d_qserver_sender_report.json}"
export QACCESS_PROFILE_KIND=delay
export DETERIORATION_PROFILE="${DETERIORATION_PROFILE:-$ROOT/scripts/mininet/delay_profile.pathB_40_20_80_200s.env}"
export SCENARIO="${SCENARIO:-delay_formal}"
export QACCESS_SESSION_KIND=fig8_delay
export QACCESS_BASELINE_LABEL=delay_baseline
export QACCESS_DYNAMIC_LABEL=delay_qaccess_d_dynamic
export QACCESS_MIN_OBJECTIVE_IMPROVEMENT="${QACCESS_MIN_OBJECTIVE_IMPROVEMENT:-0.1}"
exec "$ROOT/scripts/mininet/run_qaccess_t_combined_deterioration_eval.sh" "$@"
