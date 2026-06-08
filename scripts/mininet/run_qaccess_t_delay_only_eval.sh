#!/usr/bin/env bash
# Delay-only diagnostic on stable d8df254 stack: baseline vs qaccess_t_dynamic.
#
# Topology (--scenario fig7):
#   Path A: 20 Mbps, 40 ms, 0%
#   Path B: 20 Mbps, 20 ms, 0.001% (static TCLink; loss unchanged)
#
# Dynamic delay on Path B server egress (h2-eth1), delay_profile.pathB_200s.env:
#   0s: 20ms  90s: 80ms  100s: 20ms
#
# Runs: delay_baseline, delay_qaccess_t_dynamic
#
# Usage (VM, repo root):
#   chmod +x scripts/mininet/run_qaccess_t_delay_only_eval.sh
#   sudo -E ./scripts/mininet/run_qaccess_t_delay_only_eval.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MP="$ROOT/scripts/mininet/mp_topo.py"
RESET="$ROOT/scripts/mininet/reset_qaccess_phase2_runtime.sh"
SCENARIO="${SCENARIO:-fig7}"
DELAY_PROFILE="${DELAY_PROFILE:-scripts/mininet/delay_profile.pathB_200s.env}"
TIMEOUT="${TIMEOUT:-220}"
SAVE_LOGS="${SAVE_LOGS:-0}"
INPUT_FLV="${INPUT_FLV:-}"
LOG_CONTROL="${LOG_CONTROL:-0}"
RUNTIME_COEFFS="${QACCESS_COEFFS_JSON:-derived/qaccess_t_runtime_coefficients.json}"
BUFFER_SIZE="${QACCESS_RUNTIME_BUFFER_SIZE:-5000}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "[error] run with sudo (Mininet needs root)" >&2
  exit 1
fi

cd "$ROOT"
mkdir -p derived logs_exp

SESSION_DIR="logs_exp/session_delay_only_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$SESSION_DIR"
echo "$SESSION_DIR" > "logs_exp/.last_session"

run_one() {
  local um="$1"
  local label="$2"
  shift 2
  local -a cmd=(
    python3 "$MP" --run-exp --scenario "$SCENARIO" --utility-mode "$um"
    --timeout "$TIMEOUT" --log-parent "$SESSION_DIR" --run-label "$label"
    --dynamic-delay-profile "$DELAY_PROFILE"
  )
  [[ "$SAVE_LOGS" == "1" ]] || cmd+=(--disable-logs)
  [[ -n "$INPUT_FLV" ]] && cmd+=(--input-flv "$INPUT_FLV")
  [[ "$LOG_CONTROL" == "1" ]] && cmd+=(--log-control)
  echo "[delay_only_eval] scenario=$SCENARIO utility-mode=$um label=$label profile=$DELAY_PROFILE env=$*"
  env "$@" "${cmd[@]}"
}

echo "[delay_only_eval] baseline leg (no Phase 2 env)"
run_one baseline delay_baseline

echo "[delay_only_eval] resetting Phase 2 runtime coefficients"
bash "$RESET"
echo "[delay_only_eval] runtime coefficients before dynamic:"
cat "$ROOT/derived/qaccess_t_runtime_coefficients.json"

echo "[delay_only_eval] dynamic leg uses QACCESS_COEFFS_JSON=$RUNTIME_COEFFS buffer=$BUFFER_SIZE"
run_one qaccess_t delay_qaccess_t_dynamic \
  QACCESS_COEFFS_JSON="$RUNTIME_COEFFS" \
  QACCESS_COEFF_RELOAD=1 \
  QACCESS_TRIGGER_UPDATE=1 \
  QACCESS_RUNTIME_SAMPLE_EXPORT=1 \
  QACCESS_TRIGGER_ON_BUFFER_FULL=1 \
  QACCESS_RUNTIME_BUFFER_SIZE="$BUFFER_SIZE" \
  QACCESS_TRIGGER_COOLDOWN_MS="${QACCESS_TRIGGER_COOLDOWN_MS:-60000}" \
  QACCESS_TRIGGER_PERIODIC_MS=0 \
  QACCESS_TRIGGER_ON_THROUGHPUT_DROP=0

echo ""
echo "[delay_only_eval] session: $ROOT/$SESSION_DIR"
echo "Analyze (pcap throughput, all UDP, 1s bins):"
echo "  python3 scripts/analyze/qaccess_t_throughput_compare.py --source pcap --baseline baseline \\"
echo "    -r baseline:$SESSION_DIR/delay_baseline \\"
echo "    -r qaccess_t_dynamic:$SESSION_DIR/delay_qaccess_t_dynamic"
