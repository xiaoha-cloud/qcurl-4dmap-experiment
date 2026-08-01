#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export QACCESS_EXPERIMENT_FAMILY=clean_controlled
export QACCESS_CONTROLLER_VARIANT=qaccess_l
export QACCESS_TARGET_MODE=loss_risk_1s
export QACCESS_WORKER_TARGET_MODE=loss_risk_1s
export QACCESS_WORKER_MODEL="${QACCESS_WORKER_MODEL:-$ROOT/derived/qaccess_l_qserver_sender/qaccess_l_model_loss_risk_1s.pkl}"
export QACCESS_WORKER_MODEL_METADATA="${QACCESS_WORKER_MODEL_METADATA:-$ROOT/derived/qaccess_l_qserver_sender/qaccess_l_qserver_sender_report.json}"
export QACCESS_PROFILE_KIND=loss
export DETERIORATION_PROFILE="${DETERIORATION_PROFILE:-$ROOT/scripts/mininet/loss_profile.clean_0_0p5_0_200s.env}"
export SCENARIO="${SCENARIO:-clean_equal_paths}"
export TC_LOSS_FIXED_BW_MBIT=20
export TC_LOSS_FIXED_DELAY_MS=40
export QACCESS_RETAIN_MONITOR_LOG=1
export QACCESS_RETAIN_TC_LOG=1
export QACCESS_SESSION_KIND=clean_loss
export QACCESS_BASELINE_LABEL=clean_loss_baseline
export QACCESS_DYNAMIC_LABEL=clean_loss_qaccess_l
export QACCESS_PHASE2_STATE_DIR="${QACCESS_PHASE2_STATE_DIR:-$ROOT/derived/qaccess_clean_loss_runtime}"
export QACCESS_GATE_POLICY="${QACCESS_GATE_POLICY:-objective_aware}"
export QACCESS_GATE_OBJECTIVE="${QACCESS_GATE_OBJECTIVE:-loss}"
export QACCESS_TRIGGER_MODE="${QACCESS_TRIGGER_MODE:-objective_l}"
export QACCESS_EXECUTION_MODE="${QACCESS_EXECUTION_MODE:-active}"
export QACCESS_MIN_OBJECTIVE_IMPROVEMENT="${QACCESS_MIN_OBJECTIVE_IMPROVEMENT:-4096}"
export QACCESS_MIN_OBJECTIVE_RELATIVE_IMPROVEMENT="${QACCESS_MIN_OBJECTIVE_RELATIVE_IMPROVEMENT:-0.25}"
exec "$ROOT/scripts/mininet/run_qaccess_t_combined_deterioration_eval.sh" "$@"
