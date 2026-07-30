#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export QACCESS_EXPERIMENT_FAMILY=clean_controlled
export QACCESS_CONTROLLER_VARIANT=qaccess_d
export QACCESS_TARGET_MODE=candidate_post_rtt_median_ms
export QACCESS_WORKER_TARGET_MODE=candidate_post_rtt_median_ms
export QACCESS_WORKER_MODEL="${QACCESS_WORKER_MODEL:-$ROOT/derived/qaccess_d_intervention_v1/qaccess_d_model_candidate_post_rtt_median_ms.pkl}"
export QACCESS_WORKER_MODEL_METADATA="${QACCESS_WORKER_MODEL_METADATA:-$ROOT/derived/qaccess_d_intervention_v1/qaccess_d_intervention_report.json}"
export QACCESS_PROFILE_KIND=delay
export DETERIORATION_PROFILE="${DETERIORATION_PROFILE:-$ROOT/scripts/mininet/delay_profile.clean_40_80_40_200s.env}"
export TC_DELAY_FIXED_BW_MBIT=20
export TC_DELAY_FIXED_LOSS_PERCENT=0
export SCENARIO="${SCENARIO:-clean_equal_paths}"
export QACCESS_SESSION_KIND=clean_delay_intervention_shadow
export QACCESS_BASELINE_LABEL=clean_delay_intervention_baseline
export QACCESS_DYNAMIC_LABEL=clean_delay_intervention_qaccess_d_shadow
export QACCESS_PHASE2_STATE_DIR="${QACCESS_PHASE2_STATE_DIR:-$ROOT/derived/qaccess_clean_delay_intervention_shadow_runtime}"
export QACCESS_EXECUTION_MODE=shadow
export QACCESS_GATE_POLICY=objective_aware
export QACCESS_GATE_OBJECTIVE=delay
export QACCESS_TRIGGER_MODE=objective_d
export QACCESS_MIN_OBJECTIVE_IMPROVEMENT="${QACCESS_MIN_OBJECTIVE_IMPROVEMENT:-10}"
export QACCESS_MIN_OBJECTIVE_RELATIVE_IMPROVEMENT="${QACCESS_MIN_OBJECTIVE_RELATIVE_IMPROVEMENT:-0.10}"
exec "$ROOT/scripts/mininet/run_qaccess_t_combined_deterioration_eval.sh" "$@"
