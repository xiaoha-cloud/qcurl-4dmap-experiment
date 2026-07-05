#!/usr/bin/env bash
# Q-ACCeSS-T one-command VM runner:
#   clean runtime state -> start update worker -> run baseline/qaccess_t Fig7
#   with pcaps kept and verbose run logs disabled -> run pcap evaluation.
#
# Usage (VM, repo root):
#   cd /home/mininet/Project/qcurl-4dmap-experiment
#   sudo -E ./scripts/mininet/run_qaccess_t_worker_pcap_eval.sh
#
# Defaults:
#   - keeps derived/qaccess_t_model.pkl (newly trained ML model)
#   - keeps derived/qaccess_t_best_coefficients.json unless CLEAR_COEFFS=1
#   - disables mp_topo server/pull/push/tc/tcpdump/tshark logs but keeps pcaps

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MP="$ROOT/scripts/mininet/mp_topo.py"
WORKER="$ROOT/scripts/analyze/qaccess_t_update_worker.py"
EVAL="$ROOT/scripts/analyze/evaluate_qaccess_t_fig7.py"

BW_PROFILE="${BW_PROFILE:-scripts/mininet/bw_profile.fig7_200s.env}"
TIMEOUT="${TIMEOUT:-220}"
SAVE_LOGS="${SAVE_LOGS:-0}"
INPUT_FLV="${INPUT_FLV:-}"
LOG_CONTROL="${LOG_CONTROL:-0}"
PYTHON="${PYTHON:-python3}"
WORKER_PYTHON="${WORKER_PYTHON:-$PYTHON}"
EVAL_PYTHON="${EVAL_PYTHON:-$PYTHON}"

POLL_INTERVAL="${POLL_INTERVAL:-5}"
RECENT_ROWS="${RECENT_ROWS:-5000}"
CLEAR_COEFFS="${CLEAR_COEFFS:-0}"
WORKER_LOG="${WORKER_LOG:-0}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "[error] run with sudo (Mininet needs root)" >&2
  exit 1
fi

cd "$ROOT"
mkdir -p derived logs_exp

required=(
  "derived/qaccess_t_model.pkl"
  "$MP"
  "$WORKER"
  "$EVAL"
  "$BW_PROFILE"
)
for path in "${required[@]}"; do
  if [[ ! -e "$path" ]]; then
    echo "[error] missing required file: $path" >&2
    exit 1
  fi
done

echo "[runner] cleaning runtime state (keeping trained model)"
rm -f \
  derived/qaccess_runtime_samples.csv \
  derived/qaccess_update_request.json \
  derived/qaccess_update_response.json

if [[ "$CLEAR_COEFFS" == "1" ]]; then
  echo "[runner] CLEAR_COEFFS=1, removing derived/qaccess_t_best_coefficients.json"
  rm -f derived/qaccess_t_best_coefficients.json
else
  echo "[runner] keeping derived/qaccess_t_best_coefficients.json if present"
fi

SESSION_DIR="logs_exp/session_qaccess_t_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$SESSION_DIR"
echo "$SESSION_DIR" > "logs_exp/.last_session"

WORKER_PID=""
cleanup() {
  if [[ -n "${WORKER_PID}" ]] && kill -0 "$WORKER_PID" 2>/dev/null; then
    echo "[runner] stopping worker pid=$WORKER_PID"
    kill "$WORKER_PID" 2>/dev/null || true
    wait "$WORKER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "[runner] starting qaccess_t_update_worker.py"
if [[ "$WORKER_LOG" == "1" ]]; then
  "$WORKER_PYTHON" "$WORKER" \
    --poll-interval "$POLL_INTERVAL" \
    --recent-rows "$RECENT_ROWS" \
    > "$SESSION_DIR/worker.log" 2>&1 &
else
  "$WORKER_PYTHON" "$WORKER" \
    --poll-interval "$POLL_INTERVAL" \
    --recent-rows "$RECENT_ROWS" &
fi
WORKER_PID="$!"
echo "[runner] worker pid=$WORKER_PID"

run_one() {
  local um="$1"
  local label="$2"
  shift 2
  local -a cmd=(
    "$PYTHON" "$MP" --run-exp --scenario fig7 --utility-mode "$um"
    --timeout "$TIMEOUT" --log-parent "$SESSION_DIR" --run-label "$label"
    --dynamic-bw-profile "$BW_PROFILE"
  )
  [[ "$SAVE_LOGS" == "1" ]] || cmd+=(--disable-logs)
  [[ -n "$INPUT_FLV" ]] && cmd+=(--input-flv "$INPUT_FLV")
  [[ "$LOG_CONTROL" == "1" ]] && cmd+=(--log-control)

  echo "[runner] utility-mode=$um label=$label save_logs=$SAVE_LOGS"
  env "$@" "${cmd[@]}"
}

run_one baseline fig7_baseline

run_one qaccess_t fig7_qaccess_t \
  QACCESS_COEFF_RELOAD=1 \
  QACCESS_TRIGGER_UPDATE=1 \
  QACCESS_RUNTIME_SAMPLE_EXPORT=1 \
  QACCESS_COEFF_RELOAD_INTERVAL_MS=5000 \
  QACCESS_COEFF_SMOOTHING=0.2 \
  QACCESS_TRIGGER_DROP_PCT=15 \
  QACCESS_TRIGGER_COOLDOWN_MS=30000 \
  QACCESS_TRIGGER_MIN_SAMPLES=100 \
  QACCESS_RUNTIME_BUFFER_SIZE=10000

cleanup
trap - EXIT INT TERM

echo "[runner] running pcap evaluation"
"$EVAL_PYTHON" "$EVAL" --session "$SESSION_DIR"

echo "[runner] done. session=$ROOT/$SESSION_DIR"
echo "[runner] pcap eval=$ROOT/$SESSION_DIR/eval_fig7_qaccess_t_quic"
