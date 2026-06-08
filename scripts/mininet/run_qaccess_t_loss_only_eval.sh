#!/usr/bin/env bash
# Loss-only diagnostic on stable d8df254 stack: baseline vs qaccess_t_dynamic.
#
# Topology (--scenario fig7):
#   Path A: 20 Mbps, 40 ms, 0%
#   Path B: 20 Mbps, 20 ms, 0.001% (static TCLink; delay unchanged)
#
# Dynamic loss on Path B server egress (h2-eth1), loss_profile.pathB_200s.env:
#   0s: 0%  90s: 0.05%  100s: 0%
#
# Runs: loss_baseline, loss_qaccess_t_dynamic
#
# Usage (VM, repo root):
#   chmod +x scripts/mininet/run_qaccess_t_loss_only_eval.sh
#   sudo -E ./scripts/mininet/run_qaccess_t_loss_only_eval.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MP="$ROOT/scripts/mininet/mp_topo.py"
RESET="$ROOT/scripts/mininet/reset_qaccess_phase2_runtime.sh"
SCENARIO="${SCENARIO:-fig7}"
LOSS_PROFILE="${LOSS_PROFILE:-scripts/mininet/loss_profile.pathB_200s.env}"
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

SESSION_DIR="logs_exp/session_loss_only_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$SESSION_DIR"
echo "$SESSION_DIR" > "logs_exp/.last_session"

run_one() {
  local um="$1"
  local label="$2"
  shift 2
  local -a cmd=(
    python3 "$MP" --run-exp --scenario "$SCENARIO" --utility-mode "$um"
    --timeout "$TIMEOUT" --log-parent "$SESSION_DIR" --run-label "$label"
    --dynamic-loss-profile "$LOSS_PROFILE"
  )
  [[ "$SAVE_LOGS" == "1" ]] || cmd+=(--disable-logs)
  [[ -n "$INPUT_FLV" ]] && cmd+=(--input-flv "$INPUT_FLV")
  [[ "$LOG_CONTROL" == "1" ]] && cmd+=(--log-control)
  echo "[loss_only_eval] scenario=$SCENARIO utility-mode=$um label=$label profile=$LOSS_PROFILE env=$*"
  env "$@" "${cmd[@]}"
}

echo "[loss_only_eval] baseline leg (no Phase 2 env)"
run_one baseline loss_baseline

echo "[loss_only_eval] resetting Phase 2 runtime coefficients"
bash "$RESET"
echo "[loss_only_eval] runtime coefficients before dynamic:"
cat "$ROOT/derived/qaccess_t_runtime_coefficients.json"

echo "[loss_only_eval] dynamic leg uses QACCESS_COEFFS_JSON=$RUNTIME_COEFFS buffer=$BUFFER_SIZE"
run_one qaccess_t loss_qaccess_t_dynamic \
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
echo "[loss_only_eval] session: $ROOT/$SESSION_DIR"
echo "Analyze (pcap throughput, all UDP, 1s bins):"
echo "  python3 scripts/analyze/qaccess_t_throughput_compare.py --source pcap --baseline baseline \\"
echo "    -r baseline:$SESSION_DIR/loss_baseline \\"
echo "    -r qaccess_t_dynamic:$SESSION_DIR/loss_qaccess_t_dynamic"
