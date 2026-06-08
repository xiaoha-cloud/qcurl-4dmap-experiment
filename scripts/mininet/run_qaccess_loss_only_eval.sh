#!/usr/bin/env bash
# Loss-only experiment (NOT throughput-primary, NOT Fig.8).
#
# Primary metrics: loss rate, retransmission proxy, path shift, recovery after loss spike.
# Secondary metrics: total / per-path QUIC wire throughput.
#
# Fixed loss-sensitive coefficients (worker disabled):
#   alpha=0.6  beta=0.3  gamma=0.1
#
# Topology (--scenario fig7):
#   Path A: 20 Mbps, 40 ms, 0%
#   Path B: 20 Mbps, 20 ms, 0.001% static; dynamic loss on h2-eth1:
#     0s: 0%  90s: 0.05%  100s: 0%
#
# Runs:
#   loss_baseline
#   loss_qaccess_dynamic  (fixed coeffs, no throughput-oriented worker)
#
# Usage (VM, repo root):
#   SAVE_LOGS=1 INPUT_FLV=~/Videos/push_input.flv \
#     sudo -E ./scripts/mininet/run_qaccess_loss_only_eval.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MP="$ROOT/scripts/mininet/mp_topo.py"
RESET="$ROOT/scripts/mininet/reset_qaccess_phase2_runtime.sh"
COEFF_PROFILE="$ROOT/derived/qaccess_loss_sensitive_coefficients.json"
RUNTIME_COEFFS="$ROOT/derived/qaccess_t_runtime_coefficients.json"
SCENARIO="${SCENARIO:-fig7}"
LOSS_PROFILE="${LOSS_PROFILE:-scripts/mininet/loss_profile.pathB_200s.env}"
TIMEOUT="${TIMEOUT:-220}"
SAVE_LOGS="${SAVE_LOGS:-1}"
INPUT_FLV="${INPUT_FLV:-}"
LOG_CONTROL="${LOG_CONTROL:-0}"
BUFFER_SIZE="${QACCESS_RUNTIME_BUFFER_SIZE:-5000}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "[error] run with sudo (Mininet needs root)" >&2
  exit 1
fi

if [[ ! -f "$COEFF_PROFILE" ]]; then
  echo "[error] missing coefficient profile: $COEFF_PROFILE" >&2
  exit 1
fi

cd "$ROOT"
mkdir -p derived logs_exp

SESSION_DIR="logs_exp/session_loss_only_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$SESSION_DIR"
echo "$SESSION_DIR" > "logs_exp/.last_session"

archive_runtime_samples() {
  local dest_dir="$1"
  local src="$ROOT/derived/qaccess_runtime_samples.csv"
  if [[ -f "$src" ]]; then
    cp "$src" "$dest_dir/qaccess_runtime_samples.csv"
  fi
}

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
  echo "[loss_only] scenario=$SCENARIO utility-mode=$um label=$label profile=$LOSS_PROFILE"
  env "$@" "${cmd[@]}"
  archive_runtime_samples "$SESSION_DIR/$label"
}

echo "[loss_only] baseline leg (no Q-ACCeSS dynamic env)"
bash "$RESET"
run_one baseline loss_baseline

echo "[loss_only] reset runtime + install loss-sensitive fixed coefficients"
bash "$RESET"
cp "$COEFF_PROFILE" "$RUNTIME_COEFFS"
echo "[loss_only] runtime coefficients for dynamic leg:"
cat "$RUNTIME_COEFFS"

echo "[loss_only] dynamic leg: fixed coeffs, worker DISABLED (throughput RF not used)"
run_one qaccess_t loss_qaccess_dynamic \
  QACCESS_COEFFS_JSON="$RUNTIME_COEFFS" \
  QACCESS_COEFF_RELOAD=1 \
  QACCESS_TRIGGER_UPDATE=0 \
  QACCESS_RUNTIME_SAMPLE_EXPORT=1 \
  QACCESS_TRIGGER_ON_BUFFER_FULL=0 \
  QACCESS_RUNTIME_BUFFER_SIZE="$BUFFER_SIZE" \
  QACCESS_TRIGGER_PERIODIC_MS=0 \
  QACCESS_TRIGGER_ON_THROUGHPUT_DROP=0

echo ""
echo "[loss_only] session: $ROOT/$SESSION_DIR"
echo "[loss_only] Primary analysis (loss / retrans / path usage / recovery):"
echo "  python3 scripts/analyze/qaccess_delay_loss_eval_analyze.py --preset loss --session $SESSION_DIR --full-hi 200"
