#!/usr/bin/env bash
# Path B stress validation: baseline vs Q-ACCeSS-T dynamic only (no static).
#
# Purpose:
# This validation reduces Path A bandwidth so that Path B is more likely to be used
# before the 100s capacity decrease. If Path B downlink exceeds 10 Mbps during 50–100s,
# the 30→10 Mbps drop after 100s should be visible.
#
# Uses scenario pathB_stress (Path A 10 Mbps, Path B 20 Mbps) + Fig.7 dynamic TBF on
# h2-eth1 (Path B server egress): 0–50s 20 Mbps, 50–100s 30 Mbps, 100s+ 10 Mbps.
# Same bw_profile.fig7_200s.env as fig7 — capacity change only, not delay/loss deterioration.
#
# Usage (VM, repo root):
#   chmod +x scripts/mininet/run_qaccess_t_pathB_stress_eval.sh
#   sudo ./scripts/mininet/run_qaccess_t_pathB_stress_eval.sh
#
# Optional stronger Path A cap (5 Mbps):
#   sudo SCENARIO=pathB_stress_strong ./scripts/mininet/run_qaccess_t_pathB_stress_eval.sh
#
# Dynamic run: start worker before/during qaccess_t_dynamic leg:
#   python3 scripts/analyze/qaccess_t_update_worker.py --poll-interval 5

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MP="$ROOT/scripts/mininet/mp_topo.py"
SCENARIO="${SCENARIO:-pathB_stress}"
BW_PROFILE="${BW_PROFILE:-scripts/mininet/bw_profile.fig7_200s.env}"
TIMEOUT="${TIMEOUT:-220}"
SAVE_LOGS="${SAVE_LOGS:-0}"
INPUT_FLV="${INPUT_FLV:-}"
LOG_CONTROL="${LOG_CONTROL:-0}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "[error] run with sudo (Mininet needs root)" >&2
  exit 1
fi

cd "$ROOT"
mkdir -p derived logs_exp
SESSION_DIR="logs_exp/session_pathB_stress_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$SESSION_DIR"
echo "$SESSION_DIR" > "logs_exp/.last_session"

run_one() {
  local um="$1"
  local label="$2"
  shift 2
  local -a cmd=(
    python3 "$MP" --run-exp --scenario "$SCENARIO" --utility-mode "$um"
    --timeout "$TIMEOUT" --log-parent "$SESSION_DIR" --run-label "$label"
    --dynamic-bw-profile "$BW_PROFILE"
  )
  [[ "$SAVE_LOGS" == "1" ]] || cmd+=(--disable-logs)
  [[ -n "$INPUT_FLV" ]] && cmd+=(--input-flv "$INPUT_FLV")
  [[ "$LOG_CONTROL" == "1" ]] && cmd+=(--log-control)
  echo "[pathB_stress_eval] scenario=$SCENARIO utility-mode=$um label=$label env=$*"
  env "$@" "${cmd[@]}"
}

run_one baseline pathB_stress_baseline

run_one qaccess_t pathB_stress_qaccess_t_dynamic \
  QACCESS_COEFF_RELOAD=1 \
  QACCESS_TRIGGER_UPDATE=1 \
  QACCESS_RUNTIME_SAMPLE_EXPORT=1 \
  QACCESS_TRIGGER_ON_BUFFER_READY=1 \
  QACCESS_TRIGGER_WARMUP_SAMPLES=200 \
  QACCESS_TRIGGER_DROP_PCT=5 \
  QACCESS_TRIGGER_COOLDOWN_MS=30000 \
  QACCESS_RUNTIME_BUFFER_SIZE=10000

echo "[pathB_stress_eval] session path: $ROOT/$SESSION_DIR"
echo "[pathB_stress_eval] baseline run path: $SESSION_DIR/pathB_stress_baseline"
echo "[pathB_stress_eval] qaccess_t_dynamic run path: $SESSION_DIR/pathB_stress_qaccess_t_dynamic"
echo "[pathB_stress_eval] This experiment only compares baseline vs Q-ACCeSS-T dynamic. qaccess_t_static is not used."
