#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
echo "[warning] ACTIVE diagnostic run: Phase 2 owner and selected-path semantics remain unresolved" >&2
export INPUT_FLV="${INPUT_FLV:-/home/mininet/Videos/push_input.flv}"
export QACCESS_WORKER_MODEL="${QACCESS_WORKER_MODEL:-$ROOT/derived/qaccess_t_redesign/qaccess_t_model_delta_bw_1s.pkl}"
export QACCESS_WORKER_MODEL_METADATA="${QACCESS_WORKER_MODEL_METADATA:-$ROOT/derived/qaccess_t_redesign/qaccess_t_redesign_report.json}"
export QACCESS_WORKER_TARGET_MODE=delta_bw_1s
export QACCESS_EXECUTION_MODE=active
export DETERIORATION_PROFILE="$ROOT/scripts/mininet/combined_deterioration_profile_90_150.env"

exec "$ROOT/scripts/mininet/run_qaccess_t_combined_deterioration_eval.sh" "$@"
