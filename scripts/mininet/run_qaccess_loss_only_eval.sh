#!/usr/bin/env bash
# Loss-only experiment (NOT throughput-primary, NOT Fig.8).
#
# Primary analysis: loss rate, retransmission proxy, path shift, recovery after loss spike.
# Secondary analysis: total / per-path QUIC wire throughput.
#
# Dynamic leg uses online worker updates (throughput-oriented RF model) to observe
# how alpha/beta/gamma change under loss stress — delay/loss metrics remain primary.
#
# Topology (--scenario fig7):
#   Path A: 20 Mbps, 40 ms, 0%
#   Path B: 20 Mbps, 20 ms, 0.001% static; dynamic loss on h2-eth1:
#     0s: 0%  90s: 0.05%  100s: 0%
#
# Runs:
#   loss_baseline        — no qaccess, no worker
#   loss_qaccess_dynamic — qaccess + buffer_full worker (start worker first)
#
# Usage (VM, repo root):
#   # Terminal 1 — start worker BEFORE dynamic leg:
#   python3 scripts/analyze/qaccess_t_update_worker.py --poll-interval 5 \
#     --model derived/qaccess_t_model.pkl \
#     --coeffs-out derived/qaccess_t_runtime_coefficients.json
#
#   # Terminal 2 (default SAVE_LOGS=0 — pcaps only, no pull/server log files):
#   INPUT_FLV=~/Videos/push_input.flv \
#     sudo -E ./scripts/mininet/run_qaccess_loss_only_eval.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MP="$ROOT/scripts/mininet/mp_topo.py"
RESET="$ROOT/scripts/mininet/reset_qaccess_phase2_runtime.sh"
RUNTIME_COEFFS="${QACCESS_COEFFS_JSON:-derived/qaccess_t_runtime_coefficients.json}"
SCENARIO="${SCENARIO:-fig7}"
LOSS_PROFILE="${LOSS_PROFILE:-scripts/mininet/loss_profile.pathB_200s.env}"
TIMEOUT="${TIMEOUT:-220}"
SAVE_LOGS="${SAVE_LOGS:-0}"
INPUT_FLV="${INPUT_FLV:-}"
LOG_CONTROL="${LOG_CONTROL:-0}"
BUFFER_SIZE="${QACCESS_RUNTIME_BUFFER_SIZE:-3000}"

WORKER_CMD=(
  python3 scripts/analyze/qaccess_t_update_worker.py
  --poll-interval 5
  --model derived/qaccess_t_model.pkl
  --coeffs-out derived/qaccess_t_runtime_coefficients.json
)

if [[ "$(id -u)" -ne 0 ]]; then
  echo "[error] run with sudo (Mininet needs root)" >&2
  exit 1
fi

cd "$ROOT"
mkdir -p derived logs_exp

SESSION_DIR="logs_exp/session_loss_only_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$SESSION_DIR"
echo "$SESSION_DIR" > "logs_exp/.last_session"

print_worker_instructions() {
  echo ""
  echo "================================================================"
  echo "WARNING: Start qaccess_t_update_worker.py in another terminal"
  echo "         BEFORE the dynamic leg begins."
  echo ""
  echo "  cd $ROOT"
  printf '  %q ' "${WORKER_CMD[@]}"
  echo ""
  echo ""
  echo "Note (d8df254 branch): improvement gate is hardcoded at 3% in worker."
  echo "       No --min-improvement-pct CLI on this branch."
  echo "================================================================"
  echo ""
}

archive_runtime_artifacts() {
  local dest_dir="$1"
  local samples="$ROOT/derived/qaccess_runtime_samples.csv"
  local coeffs="$ROOT/$RUNTIME_COEFFS"
  [[ -f "$samples" ]] && cp "$samples" "$dest_dir/qaccess_runtime_samples.csv"
  [[ -f "$coeffs" ]] && cp "$coeffs" "$dest_dir/qaccess_t_runtime_coefficients.json"
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
  archive_runtime_artifacts "$SESSION_DIR/$label"
}

echo "[loss_only] baseline leg (no qaccess, no worker)"
bash "$RESET"
run_one baseline loss_baseline

echo "[loss_only] reset runtime + initialize coefficients from initial (worker may update during run)"
bash "$RESET"
echo "[loss_only] runtime coefficients at dynamic leg start:"
cat "$ROOT/$RUNTIME_COEFFS"

print_worker_instructions

echo "[loss_only] dynamic leg: qaccess enabled, worker enabled, loss-only impairment"
run_one qaccess_t loss_qaccess_dynamic \
  QACCESS_COEFFS_JSON="$RUNTIME_COEFFS" \
  QACCESS_COEFF_RELOAD=1 \
  QACCESS_TRIGGER_UPDATE=1 \
  QACCESS_RUNTIME_SAMPLE_EXPORT=1 \
  QACCESS_TRIGGER_ON_BUFFER_FULL=1 \
  QACCESS_TRIGGER_ON_THROUGHPUT_DROP=0 \
  QACCESS_TRIGGER_PERIODIC_MS=0 \
  QACCESS_TRIGGER_COOLDOWN_MS="${QACCESS_TRIGGER_COOLDOWN_MS:-60000}" \
  QACCESS_RUNTIME_BUFFER_SIZE="$BUFFER_SIZE"

echo ""
echo "[loss_only] session: $ROOT/$SESSION_DIR"
echo "[loss_only] SAVE_LOGS=$SAVE_LOGS (pcaps always kept; pull/server logs discarded when 0)"
echo "[loss_only] PCAP analysis (primary — no pull logs):"
echo "  jupyter notebook scripts/analyze/qaccess_delay_loss_pcap_analysis.ipynb"
echo "  # set LOSS_SESSION=$SESSION_DIR in notebook §1"
