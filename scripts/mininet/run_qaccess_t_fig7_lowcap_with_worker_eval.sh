#!/usr/bin/env bash
# Q-ACCeSS-T low-capacity Fig.7 sensitivity runner.
#
# Path A is selected by --scenario fig7_lowcap:
#   path A: 10 Mbps stable
# Path B is selected by bw_profile.fig7_lowcap_200s.env:
#   0-50s: 10 Mbps | 50-100s: 15 Mbps | 100s+: 5 Mbps
#
# The input video is unchanged by default: mp_topo.py uses ~/Videos/push_input.flv
# for the invoking sudo user, i.e. /home/mininet/Videos/push_input.flv on the VM.
# Evaluation is pcap-based only via evaluate_qaccess_t_fig7.py.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MP="$ROOT/scripts/mininet/mp_topo.py"
WORKER="$ROOT/scripts/analyze/qaccess_t_update_worker.py"
TRAIN="$ROOT/scripts/analyze/train_qaccess_t.py"
EVAL="$ROOT/scripts/analyze/evaluate_qaccess_t_fig7.py"

SCENARIO="${SCENARIO:-fig7_lowcap}"
BW_PROFILE="${BW_PROFILE:-scripts/mininet/bw_profile.fig7_lowcap_200s.env}"
TIMEOUT="${TIMEOUT:-220}"
SAVE_LOGS="${SAVE_LOGS:-0}"
INPUT_FLV="${INPUT_FLV:-}"
LOG_CONTROL="${LOG_CONTROL:-0}"
PYTHON="${PYTHON:-python3}"
WORKER_PYTHON="${WORKER_PYTHON:-$PYTHON}"
EVAL_PYTHON="${EVAL_PYTHON:-$PYTHON}"

POLL_INTERVAL="${POLL_INTERVAL:-5}"
RECENT_ROWS="${RECENT_ROWS:-5000}"
CLEAR_COEFFS="${CLEAR_COEFFS:-1}"
RETRAIN_MODEL="${RETRAIN_MODEL:-1}"
TRAINING_CSV="${TRAINING_CSV:-}"
WORKER_LOG="${WORKER_LOG:-0}"
WINDOWS="${WINDOWS:-0:50,50:100,100:200}"
CAPACITY_PHASES="${CAPACITY_PHASES:-0:50:phase_1_10mbps,50:100:phase_2_15mbps,100:200:phase_3_5mbps,200::post_200s_5mbps}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "[error] run with sudo (Mininet needs root)" >&2
  exit 1
fi

cd "$ROOT"
mkdir -p derived logs_exp

required=(
  "$MP"
  "$WORKER"
  "$TRAIN"
  "$EVAL"
  "$BW_PROFILE"
)
for path in "${required[@]}"; do
  if [[ ! -e "$path" ]]; then
    echo "[error] missing required file: $path" >&2
    exit 1
  fi
done

echo "[lowcap] cleaning runtime and online ML state"
rm -f \
  derived/qaccess_runtime_samples.csv \
  derived/qaccess_update_request.json \
  derived/qaccess_update_response.json \
  derived/qaccess_t_runtime_coefficients.json \
  derived/qaccess_worker_state.json

if [[ "$CLEAR_COEFFS" == "1" ]]; then
  echo "[lowcap] removing previous optimized coefficients"
  rm -f derived/qaccess_t_best_coefficients.json
else
  echo "[lowcap] warning: keeping previous optimized coefficients"
fi

if [[ "$RETRAIN_MODEL" == "1" ]]; then
  if [[ -z "$TRAINING_CSV" ]]; then
    if [[ -f derived/qaccess_training_samples_clean.csv ]]; then
      TRAINING_CSV="derived/qaccess_training_samples_clean.csv"
    else
      TRAINING_CSV="derived/qaccess_training_samples.csv"
    fi
  fi
  if [[ ! -f "$TRAINING_CSV" ]]; then
    echo "[error] missing training CSV for clean model rebuild: $TRAINING_CSV" >&2
    exit 1
  fi
  echo "[lowcap] rebuilding model with current sklearn from $TRAINING_CSV"
  rm -f \
    derived/qaccess_t_model.pkl \
    derived/qaccess_t_validation_metrics.json \
    derived/qaccess_t_feature_importance.csv
  "$WORKER_PYTHON" "$TRAIN" \
    --input "$TRAINING_CSV" \
    --model-out derived/qaccess_t_model.pkl \
    --metrics-out derived/qaccess_t_validation_metrics.json \
    --importance-out derived/qaccess_t_feature_importance.csv
elif [[ ! -f derived/qaccess_t_model.pkl ]]; then
  echo "[error] missing derived/qaccess_t_model.pkl and RETRAIN_MODEL=0" >&2
  exit 1
else
  echo "[lowcap] warning: reusing existing model because RETRAIN_MODEL=0"
fi

SESSION_DIR="logs_exp/session_qaccess_t_lowcap_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$SESSION_DIR"
echo "$SESSION_DIR" > "logs_exp/.last_session"

WORKER_PID=""
cleanup() {
  if [[ -n "${WORKER_PID}" ]] && kill -0 "$WORKER_PID" 2>/dev/null; then
    echo "[lowcap] stopping worker pid=$WORKER_PID"
    kill "$WORKER_PID" 2>/dev/null || true
    wait "$WORKER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "[lowcap] starting qaccess_t_update_worker.py"
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
echo "[lowcap] worker pid=$WORKER_PID"

run_one() {
  local um="$1"
  local label="$2"
  shift 2
  local -a cmd=(
    "$PYTHON" "$MP" --run-exp --scenario "$SCENARIO" --utility-mode "$um"
    --timeout "$TIMEOUT" --log-parent "$SESSION_DIR" --run-label "$label"
    --dynamic-bw-profile "$BW_PROFILE"
  )
  [[ "$SAVE_LOGS" == "1" ]] || cmd+=(--disable-logs)
  [[ -n "$INPUT_FLV" ]] && cmd+=(--input-flv "$INPUT_FLV")
  [[ "$LOG_CONTROL" == "1" ]] && cmd+=(--log-control)

  echo "[lowcap] utility-mode=$um label=$label scenario=$SCENARIO bw_profile=$BW_PROFILE save_logs=$SAVE_LOGS"
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

echo "[lowcap] running pcap evaluation"
"$EVAL_PYTHON" "$EVAL" --session "$SESSION_DIR" --windows "$WINDOWS" --capacity-phases "$CAPACITY_PHASES"

echo "[lowcap] done. session=$ROOT/$SESSION_DIR"
echo "[lowcap] pcap eval=$ROOT/$SESSION_DIR/eval_fig7_qaccess_t_quic"
