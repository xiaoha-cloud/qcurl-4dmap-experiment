#!/usr/bin/env bash
# Fig.7-style main evaluation: baseline vs Q-ACCeSS-T dynamic only (no static).
#
# Topology (--scenario fig7):
#   Path A / Link1: 20 Mbps, 40 ms, 0%
#   Path B / Link2: 20 Mbps, 20 ms, 0.001% (static TCLink)
# Dynamic TBF on Path B server egress (h2-eth1), bw_profile.fig7_200s.env:
#   0–50s: 20 Mbps → 50–100s: 30 Mbps → 100s+: 10 Mbps
#
# Phase 2 coefficients (do not pollute Phase 1 baseline file):
#   derived/qaccess_t_initial_coefficients.json  — stable Phase 1 coeffs (read-only for eval)
#   derived/qaccess_t_runtime_coefficients.json  — reset from initial before each dynamic run;
#                                                    Go loads this; worker writes updates here only.
#   derived/qaccess_t_best_coefficients.json     — Phase 1 optimize output (unchanged by worker)
#
# Before eval, reset runtime state:
#   ./scripts/mininet/reset_qaccess_phase2_runtime.sh
#
# Worker (separate terminal, before dynamic leg):
#   python3 scripts/analyze/qaccess_t_update_worker.py --poll-interval 5 \
#     --model derived/qaccess_t_model.pkl \
#     --coeffs-out derived/qaccess_t_runtime_coefficients.json \
#     --min-improvement-pct 1.0
#
# Usage (VM, repo root):
#   chmod +x scripts/mininet/run_qaccess_t_fig7_dynamic_eval.sh
#   sudo ./scripts/mininet/run_qaccess_t_fig7_dynamic_eval.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MP="$ROOT/scripts/mininet/mp_topo.py"
RESET="$ROOT/scripts/mininet/reset_qaccess_phase2_runtime.sh"
BW_PROFILE="${BW_PROFILE:-scripts/mininet/bw_profile.fig7_200s.env}"
TIMEOUT="${TIMEOUT:-220}"
SAVE_LOGS="${SAVE_LOGS:-0}"
INPUT_FLV="${INPUT_FLV:-}"
LOG_CONTROL="${LOG_CONTROL:-0}"
RUNTIME_COEFFS="${QACCESS_COEFFS_JSON:-derived/qaccess_t_runtime_coefficients.json}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "[error] run with sudo (Mininet needs root)" >&2
  exit 1
fi

cd "$ROOT"
mkdir -p derived logs_exp

# Reset runtime buffer + copy initial -> runtime coefficients (relative paths OK under repo root).
bash "$RESET"

SESSION_DIR="logs_exp/session_fig7_dynamic_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$SESSION_DIR"
echo "$SESSION_DIR" > "logs_exp/.last_session"

run_one() {
  local um="$1"
  local label="$2"
  shift 2
  local -a cmd=(
    python3 "$MP" --run-exp --scenario fig7 --utility-mode "$um"
    --timeout "$TIMEOUT" --log-parent "$SESSION_DIR" --run-label "$label"
    --dynamic-bw-profile "$BW_PROFILE"
  )
  [[ "$SAVE_LOGS" == "1" ]] || cmd+=(--disable-logs)
  [[ -n "$INPUT_FLV" ]] && cmd+=(--input-flv "$INPUT_FLV")
  [[ "$LOG_CONTROL" == "1" ]] && cmd+=(--log-control)
  echo "[fig7_dynamic_eval] scenario=fig7 utility-mode=$um label=$label env=$*"
  env "$@" "${cmd[@]}"
}

run_one baseline fig7_baseline

echo "[fig7_dynamic_eval] dynamic leg uses QACCESS_COEFFS_JSON=$RUNTIME_COEFFS"
run_one qaccess_t fig7_qaccess_t_dynamic \
  QACCESS_COEFFS_JSON="$RUNTIME_COEFFS" \
  QACCESS_COEFF_RELOAD=1 \
  QACCESS_TRIGGER_UPDATE=1 \
  QACCESS_RUNTIME_SAMPLE_EXPORT=1 \
  QACCESS_TRIGGER_ON_BUFFER_FULL=1 \
  QACCESS_RUNTIME_BUFFER_SIZE=5000 \
  QACCESS_TRIGGER_COOLDOWN_MS=60000 \
  QACCESS_TRIGGER_DROP_PCT=5 \
  QACCESS_TRIGGER_ON_THROUGHPUT_DROP=0 \
  QACCESS_TRIGGER_PERIODIC_MS=0

echo "[fig7_dynamic_eval] session path: $ROOT/$SESSION_DIR"
echo "[fig7_dynamic_eval] baseline run path: $SESSION_DIR/fig7_baseline"
echo "[fig7_dynamic_eval] qaccess_t_dynamic run path: $SESSION_DIR/fig7_qaccess_t_dynamic"
echo "[fig7_dynamic_eval] Verify dynamic start coeffs: grep 'loaded coefficients' $SESSION_DIR/fig7_qaccess_t_dynamic/logs/pull_*.log"
