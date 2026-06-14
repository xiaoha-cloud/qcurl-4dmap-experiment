#!/usr/bin/env bash
# Fig.8-style combined sudden link-quality deterioration evaluation.
#
# Primary objective: QUIC wire throughput robustness during simultaneous delay+loss spike.
# Delay/loss signals are explanatory (runtime samples, tc log), not the optimization target.
#
# Topology (--scenario fig8):
#   Path A: 20 Mbps, 40 ms, 0%
#   Path B: 30 Mbps, 20 ms, 0% static; combined deterioration on h2-eth1:
#     0–90s:   20 ms, 0%
#     90–100s: 80 ms, 0.05%
#     100s+:   20 ms, 0%
#
# Comparison (same profile, timeout, input, pcaps):
#   combined_baseline         — utility-mode baseline, no worker
#   combined_qaccess_t_dynamic — utility-mode qaccess_t + buffer-full worker
#
# Usage (VM, repo root):
#   # Terminal 1 — start worker BEFORE dynamic leg:
#   python3 scripts/analyze/qaccess_t_update_worker.py --poll-interval 5 \
#     --model derived/qaccess_t_model.pkl \
#     --coeffs-out derived/qaccess_t_runtime_coefficients.json \
#     --min-improvement-pct 1.0
#
#   # Terminal 2:
#   INPUT_FLV=~/Videos/push_input.flv \
#     sudo -E ./scripts/mininet/run_qaccess_t_combined_deterioration_eval.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MP="$ROOT/scripts/mininet/mp_topo.py"
RESET="$ROOT/scripts/mininet/reset_qaccess_phase2_runtime.sh"
RUNTIME_COEFFS="${QACCESS_COEFFS_JSON:-derived/qaccess_t_runtime_coefficients.json}"
SCENARIO="${SCENARIO:-fig8}"
DETERIORATION_PROFILE="${DETERIORATION_PROFILE:-scripts/mininet/combined_deterioration_profile.env}"
TIMEOUT="${TIMEOUT:-220}"
SAVE_LOGS="${SAVE_LOGS:-0}"
INPUT_FLV="${INPUT_FLV:-}"
LOG_CONTROL="${LOG_CONTROL:-0}"
BUFFER_SIZE="${QACCESS_RUNTIME_BUFFER_SIZE:-3000}"
ARCHIVE_DIR="${QACCESS_ARCHIVE_DIR:-derived/qaccess_processed_buffers}"

WORKER_CMD=(
  python3 scripts/analyze/qaccess_t_update_worker.py
  --poll-interval 5
  --model derived/qaccess_t_model.pkl
  --coeffs-out derived/qaccess_t_runtime_coefficients.json
  --min-improvement-pct 1.0
)

if [[ "$(id -u)" -ne 0 ]]; then
  echo "[error] run with sudo (Mininet needs root)" >&2
  exit 1
fi

cd "$ROOT"
mkdir -p derived logs_exp
chmod +x scripts/mininet/tc_deterioration_steps.sh 2>/dev/null || true

SESSION_DIR="logs_exp/session_combined_deterioration_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$SESSION_DIR"
echo "$SESSION_DIR" > "logs_exp/.last_session"

print_worker_instructions() {
  echo ""
  echo "================================================================"
  echo "Start the Phase 2 worker in another terminal before the dynamic leg:"
  echo ""
  echo "  cd $ROOT"
  printf '  %q ' "${WORKER_CMD[@]}"
  echo ""
  echo "================================================================"
  echo ""
}

read_coeffs() {
  python3 -c "
import json, sys
p = sys.argv[1]
try:
    c = json.load(open(p))
    print('alpha={} beta={} gamma={} source={}'.format(
        c.get('alpha'), c.get('beta'), c.get('gamma'), c.get('source', '')))
except Exception as e:
    print('(unreadable: {})'.format(e))
" "$1"
}

archive_runtime_artifacts() {
  local dest_dir="$1"
  mkdir -p "$dest_dir/derived_snapshots"
  local -a files=(
    "derived/qaccess_runtime_samples.csv"
    "derived/qaccess_update_request.json"
    "derived/qaccess_update_response.json"
    "$RUNTIME_COEFFS"
    "derived/qaccess_t_runtime_coefficients_prev.json"
    "derived/qaccess_worker_state.json"
  )
  for rel in "${files[@]}"; do
    local src="$ROOT/$rel"
    if [[ -f "$src" ]]; then
      cp "$src" "$dest_dir/derived_snapshots/$(basename "$rel")"
    fi
  done
  if [[ -d "$ROOT/$ARCHIVE_DIR" ]]; then
    mkdir -p "$dest_dir/processed_buffers"
    cp -a "$ROOT/$ARCHIVE_DIR/." "$dest_dir/processed_buffers/" 2>/dev/null || true
  fi
}

run_one() {
  local um="$1"
  local label="$2"
  shift 2
  local -a cmd=(
    python3 "$MP" --run-exp --scenario "$SCENARIO" --utility-mode "$um"
    --timeout "$TIMEOUT" --log-parent "$SESSION_DIR" --run-label "$label"
    --dynamic-deterioration-profile "$DETERIORATION_PROFILE"
  )
  [[ "$SAVE_LOGS" == "1" ]] || cmd+=(--disable-logs)
  [[ -n "$INPUT_FLV" ]] && cmd+=(--input-flv "$INPUT_FLV")
  [[ "$LOG_CONTROL" == "1" ]] && cmd+=(--log-control)
  echo "[combined_deterioration] scenario=$SCENARIO utility-mode=$um label=$label profile=$DETERIORATION_PROFILE"
  env "$@" "${cmd[@]}"
  archive_runtime_artifacts "$SESSION_DIR/$label"
}

echo "[combined_deterioration] profile contents:"
cat "$ROOT/$DETERIORATION_PROFILE"
echo ""

echo "[combined_deterioration] baseline leg (same deterioration profile, no qaccess, no worker)"
bash "$RESET"
run_one baseline combined_baseline

echo "[combined_deterioration] reset runtime + initialize coefficients from initial"
bash "$RESET"
COEFFS_BEFORE="$SESSION_DIR/combined_qaccess_t_dynamic_coeffs_before.json"
cp "$ROOT/$RUNTIME_COEFFS" "$COEFFS_BEFORE"
echo "[combined_deterioration] runtime coefficients BEFORE dynamic leg:"
cat "$COEFFS_BEFORE"
echo "[combined_deterioration] parsed: $(read_coeffs "$COEFFS_BEFORE")"

print_worker_instructions

echo "[combined_deterioration] dynamic leg: qaccess_t + buffer-full worker (1% gate)"
run_one qaccess_t combined_qaccess_t_dynamic \
  QACCESS_COEFFS_JSON="$RUNTIME_COEFFS" \
  QACCESS_COEFF_RELOAD=1 \
  QACCESS_TRIGGER_UPDATE=1 \
  QACCESS_RUNTIME_SAMPLE_EXPORT=1 \
  QACCESS_TRIGGER_ON_BUFFER_FULL=1 \
  QACCESS_TRIGGER_ON_THROUGHPUT_DROP=0 \
  QACCESS_TRIGGER_PERIODIC_MS=0 \
  QACCESS_TRIGGER_COOLDOWN_MS="${QACCESS_TRIGGER_COOLDOWN_MS:-60000}" \
  QACCESS_RUNTIME_BUFFER_SIZE="$BUFFER_SIZE"

COEFFS_AFTER="$SESSION_DIR/combined_qaccess_t_dynamic_coeffs_after.json"
cp "$ROOT/$RUNTIME_COEFFS" "$COEFFS_AFTER"
echo ""
echo "[combined_deterioration] runtime coefficients AFTER dynamic leg:"
cat "$COEFFS_AFTER"
echo "[combined_deterioration] parsed: $(read_coeffs "$COEFFS_AFTER")"

CHANGED=$(python3 -c "
import json
b=json.load(open('$COEFFS_BEFORE'))
a=json.load(open('$COEFFS_AFTER'))
keys=('alpha','beta','gamma')
print('yes' if any(abs(float(b.get(k,0))-float(a.get(k,0)))>1e-9 for k in keys) else 'no')
")
echo "[combined_deterioration] coefficients changed during dynamic leg: $CHANGED"

echo ""
echo "[combined_deterioration] session: $ROOT/$SESSION_DIR"
echo "[combined_deterioration] baseline:  $SESSION_DIR/combined_baseline"
echo "[combined_deterioration] dynamic:   $SESSION_DIR/combined_qaccess_t_dynamic"
echo "[combined_deterioration] SAVE_LOGS=$SAVE_LOGS (pcaps always kept; role logs when SAVE_LOGS=1)"
echo "[combined_deterioration] analysis:"
echo "  jupyter notebook scripts/analyze/qaccess_combined_deterioration_analysis.ipynb"
echo "  # or: python3 scripts/analyze/qaccess_impairment_eval_analyze.py --preset combined --session $SESSION_DIR"
