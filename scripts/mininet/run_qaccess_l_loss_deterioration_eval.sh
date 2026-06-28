#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export QACCESS_CONTROLLER_VARIANT=qaccess_l
export QACCESS_WORKER_TARGET_MODE=delta_loss_1s
export QACCESS_WORKER_MODEL="${QACCESS_WORKER_MODEL:-$ROOT/derived/qaccess_l_qserver_sender/qaccess_l_model_delta_loss_1s.pkl}"
export QACCESS_WORKER_MODEL_METADATA="${QACCESS_WORKER_MODEL_METADATA:-$ROOT/derived/qaccess_l_qserver_sender/qaccess_l_qserver_sender_report.json}"
export QACCESS_PROFILE_KIND=loss
export DETERIORATION_PROFILE="${DETERIORATION_PROFILE:-$ROOT/scripts/mininet/loss_profile.pathB_200s.env}"
export SCENARIO="${SCENARIO:-fig7}"
export QACCESS_SESSION_KIND=fig8_loss
export QACCESS_BASELINE_LABEL=loss_baseline
export QACCESS_DYNAMIC_LABEL=loss_qaccess_l_dynamic
export QACCESS_MIN_OBJECTIVE_IMPROVEMENT="${QACCESS_MIN_OBJECTIVE_IMPROVEMENT:-0.00001}"
exec "$ROOT/scripts/mininet/run_qaccess_t_combined_deterioration_eval.sh" "$@"
