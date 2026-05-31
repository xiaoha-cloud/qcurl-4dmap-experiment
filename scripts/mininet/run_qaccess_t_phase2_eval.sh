#!/usr/bin/env bash
# Q-ACCeSS-T Phase 2 evaluation: baseline, qaccess_t static, qaccess_t dynamic.
#
# Usage (VM, repo root):
#   chmod +x scripts/mininet/run_qaccess_t_phase2_eval.sh
#   sudo TIMEOUT=220 SAVE_LOGS=1 ./scripts/mininet/run_qaccess_t_phase2_eval.sh
#
# Dynamic run: start worker in another terminal before/during experiment:
#   python3 scripts/analyze/qaccess_t_update_worker.py --poll-interval 5
#
# Compare throughput:
#   python3 scripts/analyze/qaccess_t_throughput_compare.py \\
#     -r baseline:logs_exp/session_*/fig7_baseline \\
#     -r qaccess_t_static:logs_exp/session_*/fig7_qaccess_t_static \\
#     -r qaccess_t_dynamic:logs_exp/session_*/fig7_qaccess_t_dynamic

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MP="$ROOT/scripts/mininet/mp_topo.py"
BW_PROFILE="${BW_PROFILE:-scripts/mininet/bw_profile.fig7_200s.env}"
TIMEOUT="${TIMEOUT:-220}"
SAVE_LOGS="${SAVE_LOGS:-1}"
INPUT_FLV="${INPUT_FLV:-}"
LOG_CONTROL="${LOG_CONTROL:-0}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "[error] run with sudo (Mininet needs root)" >&2
  exit 1
fi

cd "$ROOT"
mkdir -p derived
SESSION_DIR="logs_exp/session_qaccess_t_$(date +%Y%m%d_%H%M%S)"
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
  echo "[phase2_eval] utility-mode=$um label=$label env=$*"
  env "$@" "${cmd[@]}"
}

# baseline: no utility control
run_one baseline fig7_baseline

# qaccess_t static: Phase 1 style (no reload / trigger / runtime export)
run_one qaccess_t fig7_qaccess_t_static \
  QACCESS_COEFF_RELOAD=0 \
  QACCESS_TRIGGER_UPDATE=0 \
  QACCESS_RUNTIME_SAMPLE_EXPORT=0

# qaccess_t dynamic: Phase 2
run_one qaccess_t fig7_qaccess_t_dynamic \
  QACCESS_COEFF_RELOAD=1 \
  QACCESS_TRIGGER_UPDATE=1 \
  QACCESS_RUNTIME_SAMPLE_EXPORT=1 \
  QACCESS_COEFF_RELOAD_INTERVAL_MS=5000 \
  QACCESS_COEFF_SMOOTHING=0.2 \
  QACCESS_TRIGGER_DROP_PCT=5 \
  QACCESS_TRIGGER_ON_BUFFER_READY=1 \
  QACCESS_TRIGGER_WARMUP_SAMPLES=200 \
  QACCESS_TRIGGER_PERIODIC_MS=0 \
  QACCESS_TRIGGER_COOLDOWN_MS=30000 \
  QACCESS_TRIGGER_MIN_SAMPLES=100 \
  QACCESS_RUNTIME_BUFFER_SIZE=10000

echo "[phase2_eval] done. session=$ROOT/$SESSION_DIR"
echo "[phase2_eval] plots:"
echo "  python3 scripts/analyze/qaccess_t_throughput_compare.py -r baseline:$SESSION_DIR/fig7_baseline -r qaccess_t_static:$SESSION_DIR/fig7_qaccess_t_static -r qaccess_t_dynamic:$SESSION_DIR/fig7_qaccess_t_dynamic"
echo "  python3 scripts/analyze/plot_qaccess_t_compare.py --dir derived/qaccess_t_compare"
